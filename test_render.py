"""Render the vote/admin pages end-to-end to catch template errors."""
import os, tempfile
os.environ["ASANA_TOKEN"] = "fake"
os.environ["ADMIN_PASSWORD"] = "test"
os.environ["DB_PATH"] = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name

import app as app_mod

FAKE = [
    {"gid": f"T{i}", "name": f"Topic {i}", "notes": f"note {i}" if i % 2 else "",
     "assignee": {"name": "Allie Antles"} if i % 2 == 0 else None,
     "permalink_url": f"https://app.asana.com/x/{i}"}
    for i in range(1, 6)
]
app_mod.fetch_section_topics = lambda: list(FAKE)

c = app_mod.app.test_client()
# start round
c.post("/admin/login", data={"password": "test"})
c.post("/admin/start")

# vote page
r = c.get("/vote")
assert r.status_code == 200
body = r.data.decode()
for kw in ["Pick <strong>exactly 2 topics",
           "Allie Antles", "Danny Schulz", "Jamie Schulz",
           "Kyle Menasco", "Mel Scott", "Anthony Wood", "Ryan Gilbreath",
           "Topic 1", "Topic 5", "Submit my votes"]:
    assert kw in body, f"missing {kw!r} in vote page"

# admin page (before any votes)
r = c.get("/admin")
assert r.status_code == 200
body = r.data.decode()
for kw in ["Who has voted", "Not yet", "No votes yet"]:
    assert kw in body, f"missing {kw!r} in admin page"

# no_round page
c.post("/admin/close", follow_redirects=True)   # should fail (no votes)
r = c.get("/")
# still open because close failed, but let's test no_round via manual close
conn = app_mod.get_db()
conn.execute("UPDATE rounds SET status='closed', closed_at=datetime('now')")
conn.commit()
conn.close()
r = c.get("/")
assert r.status_code == 200
assert b"Voting isn" in r.data

print("Templates render OK")
