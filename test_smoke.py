"""End-to-end smoke test. Mocks ALL Asana API calls so we can verify:
  - scheduled open/close endpoints (gated by token)
  - PDF generation works
  - Asana create-section + create-task + attach-PDF flow is called with
    the correct payloads
"""
import os, tempfile

os.environ["ASANA_TOKEN"] = "fake-token"
os.environ["ADMIN_PASSWORD"] = "admin-pass"
os.environ["SCHEDULE_TOKEN"] = "sched-pass"
os.environ["FLASK_SECRET_KEY"] = "test-secret"
os.environ["DB_PATH"] = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name

import app as app_mod

# Intercept all outbound Asana calls
FAKE_TOPICS = [
    {"gid": "T1", "name": "Alpha topic", "notes": "notes A",
     "assignee": {"name": "Allie Antles"},
     "permalink_url": "https://app.asana.com/0/x/T1", "completed": False},
    {"gid": "T2", "name": "Bravo topic", "notes": "",
     "assignee": {"name": "Danny Schulz"},
     "permalink_url": "https://app.asana.com/0/x/T2", "completed": False},
    {"gid": "T3", "name": "Charlie topic", "notes": None,
     "assignee": None,
     "permalink_url": "https://app.asana.com/0/x/T3", "completed": False},
]

# Simulate Asana project/sections state
_sections = []   # sections already in project

GETS = []
POSTS = []

def fake_get(path, params=None):
    GETS.append((path, params))
    if path == f"/sections/{app_mod.ASANA_SECTION_ID}/tasks":
        return {"data": FAKE_TOPICS}
    if path == f"/projects/{app_mod.ASANA_PROJECT_ID}/sections":
        return {"data": list(_sections)}
    raise AssertionError(f"unexpected asana_get: {path}")

def fake_post(path, json_body=None, files=None, data=None):
    POSTS.append({"path": path, "json": json_body, "files": files, "data": data})
    if path == f"/projects/{app_mod.ASANA_PROJECT_ID}/sections":
        gid = f"SEC_{len(_sections)+1}"
        _sections.append({"gid": gid, "name": json_body["data"]["name"]})
        return {"data": {"gid": gid, "name": json_body["data"]["name"]}}
    if path == "/tasks":
        return {"data": {"gid": "TASK_GID_42"}}
    if path == "/attachments":
        return {"data": {"gid": "ATT_GID_99"}}
    raise AssertionError(f"unexpected asana_post: {path}")

app_mod.asana_get = fake_get
app_mod.asana_post = fake_post

c = app_mod.app.test_client()

# ---------- schedule auth ----------
r = c.get("/scheduled/open")
assert r.status_code == 403, r.status_code
r = c.get("/scheduled/open?token=wrong")
assert r.status_code == 403, r.status_code

# ---------- scheduled open ----------
r = c.get("/scheduled/open?token=sched-pass")
assert r.status_code == 200, (r.status_code, r.data)
payload = r.get_json()
assert payload["ok"] is True
assert payload["topic_count"] == 3

# ---------- votes ----------
conn = app_mod.get_db()
topic_ids = [row["id"] for row in conn.execute(
    "SELECT id FROM topics WHERE round_id = "
    "(SELECT id FROM rounds WHERE status='open') ORDER BY id"
).fetchall()]
conn.close()
assert len(topic_ids) == 3

def cast(email, picks):
    return c.post("/vote",
                  data={"voter_email": email,
                        "topic_id": [str(x) for x in picks]},
                  follow_redirects=True)

assert cast("allie.antles@suncitychurch.com", [topic_ids[0], topic_ids[1]]).status_code == 200
assert cast("danny@suncitychurch.com",        [topic_ids[0], topic_ids[2]]).status_code == 200
assert cast("kyle@suncitychurch.com",         [topic_ids[0], topic_ids[1]]).status_code == 200
assert cast("mel_not_a_real@example.com",     [topic_ids[0], topic_ids[1]]).status_code == 200  # rejected
# confirm replace-on-resubmit
assert cast("allie.antles@suncitychurch.com", [topic_ids[1], topic_ids[2]]).status_code == 200

# ---------- scheduled close ----------
r = c.get("/scheduled/close?token=sched-pass")
assert r.status_code == 200, (r.status_code, r.data)
payload = r.get_json()
assert payload["ok"] is True, payload
assert payload["task_gid"] == "TASK_GID_42"
assert payload["attachment_gid"] == "ATT_GID_99"

# Inspect the Asana calls we made
paths = [p["path"] for p in POSTS]
assert f"/projects/{app_mod.ASANA_PROJECT_ID}/sections" in paths, paths
assert "/tasks" in paths, paths
assert "/attachments" in paths, paths

task_post = next(p for p in POSTS if p["path"] == "/tasks")
assert task_post["json"]["data"]["name"].startswith("Issues List Results "), \
    task_post["json"]["data"]["name"]
assert task_post["json"]["data"]["memberships"][0]["project"] == app_mod.ASANA_PROJECT_ID

attach_post = next(p for p in POSTS if p["path"] == "/attachments")
assert attach_post["data"]["parent"] == "TASK_GID_42"
assert attach_post["files"]["file"][0].startswith("Issues List Results ")
pdf_bytes = attach_post["files"]["file"][1]
assert isinstance(pdf_bytes, (bytes, bytearray))
assert pdf_bytes[:4] == b"%PDF", "attachment content isn't a PDF"

# Write the generated PDF to disk so we can inspect it manually
out = os.path.join(os.path.dirname(__file__), "sample_results.pdf")
with open(out, "wb") as f:
    f.write(pdf_bytes)

# ---------- second close should fail (no open round) ----------
r = c.get("/scheduled/close?token=sched-pass")
assert r.status_code == 500
assert r.get_json()["ok"] is False

print("ALL SMOKE TESTS PASSED")
print("Sample PDF written to:", out, "size =", len(pdf_bytes), "bytes")
