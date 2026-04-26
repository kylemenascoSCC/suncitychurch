"""End-to-end smoke test. Mocks ALL Asana API calls so we can verify:
  - scheduled open creates the live Asana task with an initial PDF
  - each vote replaces the PDF on the same task
  - scheduled close renames the task and replaces the PDF one final time
"""
import os, tempfile

os.environ["ASANA_TOKEN"] = "fake-token"
os.environ["ADMIN_PASSWORD"] = "admin-pass"
os.environ["SCHEDULE_TOKEN"] = "sched-pass"
os.environ["FLASK_SECRET_KEY"] = "test-secret"
os.environ["DB_PATH"] = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name

import app as app_mod

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

_sections = []
_attachment_seq = 0
_task_seq = 0

GETS, POSTS, PUTS, DELETES = [], [], [], []


def fake_get(path, params=None):
    GETS.append((path, params))
    if path == f"/sections/{app_mod.ASANA_SECTION_ID}/tasks":
        return {"data": FAKE_TOPICS}
    if path == f"/projects/{app_mod.ASANA_PROJECT_ID}/sections":
        return {"data": list(_sections)}
    raise AssertionError(f"unexpected asana_get: {path}")


def fake_post(path, json_body=None, files=None, data=None):
    global _attachment_seq, _task_seq
    POSTS.append({"path": path, "json": json_body, "files": files, "data": data})
    if path == f"/projects/{app_mod.ASANA_PROJECT_ID}/sections":
        gid = f"SEC_{len(_sections)+1}"
        _sections.append({"gid": gid, "name": json_body["data"]["name"]})
        return {"data": {"gid": gid, "name": json_body["data"]["name"]}}
    if path == "/tasks":
        _task_seq += 1
        return {"data": {"gid": f"TASK_GID_{_task_seq}"}}
    if path == "/attachments":
        _attachment_seq += 1
        return {"data": {"gid": f"ATT_GID_{_attachment_seq}"}}
    raise AssertionError(f"unexpected asana_post: {path}")


def fake_put(path, json_body=None):
    PUTS.append({"path": path, "json": json_body})
    return {"data": {}}


def fake_delete(path):
    DELETES.append(path)
    return True


app_mod.asana_get = fake_get
app_mod.asana_post = fake_post
app_mod.asana_put = fake_put
app_mod.asana_delete = fake_delete

c = app_mod.app.test_client()

# ---------- schedule auth ----------
r = c.get("/scheduled/open")
assert r.status_code == 403
r = c.get("/scheduled/open?token=wrong")
assert r.status_code == 403

# ---------- scheduled open ----------
r = c.get("/scheduled/open?token=sched-pass")
assert r.status_code == 200, (r.status_code, r.data)
payload = r.get_json()
assert payload["ok"] is True
assert payload["topic_count"] == 3

# Open should have created the live task + attached the initial PDF
task_posts_after_open = [p for p in POSTS if p["path"] == "/tasks"]
attach_posts_after_open = [p for p in POSTS if p["path"] == "/attachments"]
assert len(task_posts_after_open) == 1, "live task should be created on open"
assert len(attach_posts_after_open) == 1, "initial PDF should be attached on open"
initial_task_name = task_posts_after_open[0]["json"]["data"]["name"]
assert initial_task_name.startswith("Issues List Voting"), initial_task_name
assert "Live" in initial_task_name, initial_task_name

# Initial PDF should be a real PDF
initial_pdf = attach_posts_after_open[0]["files"]["file"][1]
assert initial_pdf[:4] == b"%PDF"

# ---------- votes ----------
conn = app_mod.get_db()
topic_ids = [row["id"] for row in conn.execute(
    "SELECT id FROM topics WHERE round_id = "
    "(SELECT id FROM rounds WHERE status='open') ORDER BY id"
).fetchall()]
conn.close()
assert len(topic_ids) == 3

attach_count_before = len([p for p in POSTS if p["path"] == "/attachments"])
delete_count_before = len(DELETES)

def cast(email, picks):
    return c.post("/vote",
                  data={"voter_email": email,
                        "topic_id": [str(x) for x in picks]},
                  follow_redirects=True)

assert cast("allie.antles@suncitychurch.com", [topic_ids[0], topic_ids[1]]).status_code == 200
assert cast("danny@suncitychurch.com",        [topic_ids[0], topic_ids[2]]).status_code == 200
assert cast("kyle@suncitychurch.com",         [topic_ids[0], topic_ids[1]]).status_code == 200
# A non-team voter should not be recorded but the request is gracefully handled
assert cast("nope@example.com",               [topic_ids[0], topic_ids[1]]).status_code == 200
# Replace-on-resubmit
assert cast("allie.antles@suncitychurch.com", [topic_ids[1], topic_ids[2]]).status_code == 200

# Each VALID vote (4 of them) should have triggered an attachment swap:
# +4 attachments AND +4 deletes
attach_count_after = len([p for p in POSTS if p["path"] == "/attachments"])
delete_count_after = len(DELETES)
assert attach_count_after - attach_count_before == 4, \
    f"expected 4 attachment uploads, got {attach_count_after - attach_count_before}"
assert delete_count_after - delete_count_before == 4, \
    f"expected 4 attachment deletes, got {delete_count_after - delete_count_before}"

# Updates to the task notes after each vote (for the in-Asana glance)
puts_after_votes = [p for p in PUTS if p["path"].startswith("/tasks/")]
assert len(puts_after_votes) >= 4, \
    f"expected 4+ task notes updates after 4 valid votes, got {len(puts_after_votes)}"

# ---------- scheduled close ----------
task_posts_before_close = len([p for p in POSTS if p["path"] == "/tasks"])
r = c.get("/scheduled/close?token=sched-pass")
assert r.status_code == 200, (r.status_code, r.data)
payload = r.get_json()
assert payload["ok"] is True, payload
assert payload["task_gid"] == "TASK_GID_1", payload  # SAME task as open
assert payload["attachment_gid"].startswith("ATT_GID_"), payload

# Close MUST NOT create a new task (we update the existing live one)
task_posts_after_close = len([p for p in POSTS if p["path"] == "/tasks"])
assert task_posts_after_close == task_posts_before_close, \
    "close should update the live task in place, not create a new one"

# Close should rename via PUT to "Issues List Results <date>"
final_puts = [p for p in PUTS if p["path"] == "/tasks/TASK_GID_1"]
final_rename = next((p for p in final_puts
                     if (p["json"]["data"].get("name") or "").startswith("Issues List Results")),
                    None)
assert final_rename is not None, f"no rename PUT found among {final_puts}"

# Final PDF was uploaded after close
final_attach = [p for p in POSTS if p["path"] == "/attachments"][-1]
final_pdf_filename = final_attach["data"]["name"]
assert final_pdf_filename.startswith("Issues List Results "), final_pdf_filename
final_pdf = final_attach["files"]["file"][1]
assert final_pdf[:4] == b"%PDF"

# Write the final closed PDF for manual inspection
out = os.path.join(os.path.dirname(__file__), "sample_results.pdf")
with open(out, "wb") as f:
    f.write(final_pdf)

# Also write a sample "live mid-week" PDF — pick the most recent live attachment
live_attaches = [p for p in POSTS if p["path"] == "/attachments"][:-1]
live_out = None
if live_attaches:
    live_pdf = live_attaches[-1]["files"]["file"][1]
    live_out = os.path.join(os.path.dirname(__file__), "sample_live.pdf")
    with open(live_out, "wb") as f:
        f.write(live_pdf)

# ---------- second close should fail (no open round) ----------
r = c.get("/scheduled/close?token=sched-pass")
assert r.status_code == 500
assert r.get_json()["ok"] is False

print("ALL SMOKE TESTS PASSED")
print("Sample final PDF:", out, "size =", len(final_pdf), "bytes")
if live_out:
    print("Sample live PDF: ", live_out, "size =", len(live_pdf), "bytes")
