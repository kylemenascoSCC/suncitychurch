"""
Strategic Leadership Meeting — voting app
-----------------------------------------
Flask app that:
  1. Opens voting Sunday 5:00 PM Pacific by pulling the current tasks from
     the "Short Term Issues List" section of the Strategic Leadership
     Meeting project in Asana.
  2. Lets each of the 7 leaders pick 2 topics via a simple web form.
  3. Closes voting Monday 9:00 AM Pacific, generates a results PDF, and
     posts one Asana task titled "Issues List Results <date>" into the
     "Issues List Voting Results" section of the project, with the PDF
     attached.

Open/close are triggered by external scheduled GETs to /scheduled/open and
/scheduled/close (see README). A password-gated /admin page is kept around
for manual overrides and live status.

Required env vars (see .env.example):
    ASANA_TOKEN          Personal Access Token
    ADMIN_PASSWORD       gates /admin
    SCHEDULE_TOKEN       gates /scheduled/{open,close}
Optional:
    ASANA_PROJECT_ID, ASANA_SECTION_ID, RESULTS_SECTION_NAME,
    VOTES_PER_PERSON, TOP_N_HIGHLIGHT, FLASK_SECRET_KEY, DB_PATH, PORT
"""

import io
import os
import sqlite3
import secrets
from datetime import datetime
from functools import wraps

import requests
from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, session, abort, jsonify,
)

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
)

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
ASANA_TOKEN          = os.environ.get("ASANA_TOKEN")
ASANA_PROJECT_ID     = os.environ.get("ASANA_PROJECT_ID", "1209071651749706")
ASANA_SECTION_ID     = os.environ.get("ASANA_SECTION_ID", "1210613623415612")
RESULTS_SECTION_NAME = os.environ.get("RESULTS_SECTION_NAME",
                                      "Issues List Voting Results")
ADMIN_PASSWORD       = os.environ.get("ADMIN_PASSWORD")
SCHEDULE_TOKEN       = os.environ.get("SCHEDULE_TOKEN")
DB_PATH              = os.environ.get("DB_PATH", "voting.db")
VOTES_PER_PERSON     = int(os.environ.get("VOTES_PER_PERSON", "2"))
TOP_N_HIGHLIGHT      = int(os.environ.get("TOP_N_HIGHLIGHT", "5"))

LEADERSHIP_TEAM = [
    {"name": "Allie Antles",   "email": "allie.antles@suncitychurch.com"},
    {"name": "Danny Schulz",   "email": "danny@suncitychurch.com"},
    {"name": "Jamie Schulz",   "email": "jamie@suncitychurch.com"},
    {"name": "Kyle Menasco",   "email": "kyle@suncitychurch.com"},
    {"name": "Mel Scott",      "email": "melanie.scott@suncitychurch.com"},
    {"name": "Anthony Wood",   "email": "anthony.wood@suncitychurch.com"},
    {"name": "Ryan Gilbreath", "email": "ryan.gilbreath@suncitychurch.com"},
]

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(32))


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS rounds (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at        TEXT NOT NULL,
            closed_at         TEXT,
            status            TEXT NOT NULL DEFAULT 'open',
            summary_task_gid  TEXT,
            attachment_gid    TEXT
        );
        CREATE TABLE IF NOT EXISTS topics (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            round_id        INTEGER NOT NULL REFERENCES rounds(id) ON DELETE CASCADE,
            asana_task_gid  TEXT NOT NULL,
            name            TEXT NOT NULL,
            notes           TEXT,
            assignee_name   TEXT,
            permalink_url   TEXT
        );
        CREATE TABLE IF NOT EXISTS votes (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            round_id      INTEGER NOT NULL REFERENCES rounds(id) ON DELETE CASCADE,
            voter_email   TEXT NOT NULL,
            voter_name    TEXT NOT NULL,
            topic_id      INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
            voted_at      TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_votes_round  ON votes(round_id);
        CREATE INDEX IF NOT EXISTS idx_topics_round ON topics(round_id);
    """)
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------
# Asana helpers
# --------------------------------------------------------------------------
ASANA_BASE = "https://app.asana.com/api/1.0"


def _headers():
    if not ASANA_TOKEN:
        raise RuntimeError("ASANA_TOKEN env var is not set")
    return {"Authorization": f"Bearer {ASANA_TOKEN}"}


def asana_get(path, params=None):
    r = requests.get(f"{ASANA_BASE}{path}", headers=_headers(),
                     params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def asana_post(path, json_body=None, files=None, data=None):
    kwargs = {"headers": _headers(), "timeout": 60}
    if json_body is not None:
        kwargs["json"] = json_body
    if files is not None:
        kwargs["files"] = files
    if data is not None:
        kwargs["data"] = data
    r = requests.post(f"{ASANA_BASE}{path}", **kwargs)
    r.raise_for_status()
    return r.json()


def fetch_section_topics():
    """Pull live, incomplete tasks from the Short Term Issues List."""
    data = asana_get(
        f"/sections/{ASANA_SECTION_ID}/tasks",
        params={
            "opt_fields": "name,notes,assignee.name,permalink_url,completed",
            "limit": 100,
        },
    ).get("data", [])
    return [t for t in data if not t.get("completed")]


def ensure_results_section_gid():
    """Return the gid for RESULTS_SECTION_NAME, creating it if missing."""
    sections = asana_get(f"/projects/{ASANA_PROJECT_ID}/sections",
                         params={"limit": 100}).get("data", [])
    for s in sections:
        if s.get("name", "").strip().lower() == RESULTS_SECTION_NAME.strip().lower():
            return s["gid"]
    created = asana_post(
        f"/projects/{ASANA_PROJECT_ID}/sections",
        json_body={"data": {"name": RESULTS_SECTION_NAME}},
    )
    return created["data"]["gid"]


def create_results_task(title, notes, section_gid):
    """Create a task directly in a section via memberships."""
    resp = asana_post("/tasks", json_body={
        "data": {
            "name": title,
            "notes": notes,
            "projects": [ASANA_PROJECT_ID],
            "memberships": [{
                "project": ASANA_PROJECT_ID,
                "section": section_gid,
            }],
        }
    })
    return resp["data"]["gid"]


def attach_pdf_to_task(task_gid, pdf_bytes, filename):
    resp = asana_post(
        "/attachments",
        data={"parent": task_gid, "name": filename},
        files={"file": (filename, pdf_bytes, "application/pdf")},
    )
    return resp["data"]["gid"]


# --------------------------------------------------------------------------
# PDF generation
# --------------------------------------------------------------------------
def build_results_pdf(ranked, when_closed):
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=LETTER,
        leftMargin=0.75 * inch, rightMargin=0.75 * inch,
        topMargin=0.75 * inch, bottomMargin=0.75 * inch,
        title="Strategic Leadership Meeting — Issues List Results",
    )
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle(
        "h1", parent=styles["Title"], alignment=TA_LEFT,
        fontSize=20, spaceAfter=6,
    )
    sub = ParagraphStyle(
        "sub", parent=styles["Normal"],
        fontSize=11, textColor=colors.HexColor("#555555"), spaceAfter=18,
    )
    h2 = ParagraphStyle(
        "h2", parent=styles["Heading2"], fontSize=13,
        spaceBefore=16, spaceAfter=8,
    )
    body = ParagraphStyle(
        "body", parent=styles["Normal"], fontSize=10.5, leading=14,
    )
    small = ParagraphStyle(
        "small", parent=styles["Normal"], fontSize=9,
        textColor=colors.HexColor("#666666"),
    )

    story = []
    story.append(Paragraph("Issues List Results", h1))
    story.append(Paragraph(
        f"Strategic Leadership Meeting · voting closed "
        f"{when_closed.strftime('%A, %B %-d, %Y at %-I:%M %p %Z').strip() or when_closed.strftime('%A, %B %d, %Y at %H:%M')}",
        sub,
    ))

    total_voters = sum(1 for r in LEADERSHIP_TEAM if True)
    total_votes = sum(row["votes"] for row in ranked)
    story.append(Paragraph(
        f"Each leader cast up to {VOTES_PER_PERSON} votes. "
        f"{total_votes} total vote(s) recorded.",
        body,
    ))

    # Top N table
    story.append(Paragraph(f"Top {TOP_N_HIGHLIGHT} for discussion", h2))
    top = ranked[:TOP_N_HIGHLIGHT]
    if top:
        data = [["#", "Topic", "Votes", "Voted by"]]
        for i, row in enumerate(top, 1):
            voters = ", ".join(row["voters"]) if row["voters"] else "—"
            data.append([str(i), row["name"], str(row["votes"]), voters])
        t = Table(data, colWidths=[0.35*inch, 3.4*inch, 0.7*inch, 2.75*inch],
                  repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#fdf2c8")),
            ("TEXTCOLOR",  (0,0), (-1,0), colors.HexColor("#7a5a00")),
            ("FONTNAME",   (0,0), (-1,0), "Helvetica-Bold"),
            ("FONTSIZE",   (0,0), (-1,-1), 10),
            ("VALIGN",     (0,0), (-1,-1), "TOP"),
            ("ALIGN",      (0,0), (0,-1),  "CENTER"),
            ("ALIGN",      (2,0), (2,-1),  "CENTER"),
            ("GRID",       (0,0), (-1,-1), 0.5, colors.HexColor("#e0dcd0")),
            ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.white, colors.HexColor("#fbfaf7")]),
            ("LEFTPADDING",  (0,0), (-1,-1), 6),
            ("RIGHTPADDING", (0,0), (-1,-1), 6),
            ("TOPPADDING",   (0,0), (-1,-1), 6),
            ("BOTTOMPADDING",(0,0), (-1,-1), 6),
        ]))
        story.append(t)
    else:
        story.append(Paragraph("No votes cast.", body))

    # Full ranked list
    story.append(Paragraph("Full ranked results", h2))
    data = [["#", "Topic", "Votes", "Voted by"]]
    for i, row in enumerate(ranked, 1):
        voters = ", ".join(row["voters"]) if row["voters"] else "—"
        data.append([str(i), row["name"], str(row["votes"]), voters])
    t = Table(data, colWidths=[0.35*inch, 3.4*inch, 0.7*inch, 2.75*inch],
              repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#eef2f7")),
        ("TEXTCOLOR",  (0,0), (-1,0), colors.HexColor("#2a5a9c")),
        ("FONTNAME",   (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTSIZE",   (0,0), (-1,-1), 10),
        ("VALIGN",     (0,0), (-1,-1), "TOP"),
        ("ALIGN",      (0,0), (0,-1),  "CENTER"),
        ("ALIGN",      (2,0), (2,-1),  "CENTER"),
        ("GRID",       (0,0), (-1,-1), 0.5, colors.HexColor("#e0dcd0")),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.white, colors.HexColor("#f7f8fb")]),
        ("LEFTPADDING",  (0,0), (-1,-1), 6),
        ("RIGHTPADDING", (0,0), (-1,-1), 6),
        ("TOPPADDING",   (0,0), (-1,-1), 6),
        ("BOTTOMPADDING",(0,0), (-1,-1), 6),
    ]))
    story.append(t)

    story.append(Spacer(1, 0.25 * inch))
    story.append(Paragraph("Source task links", h2))
    for row in ranked:
        if row["permalink_url"]:
            story.append(Paragraph(
                f'• <a href="{row["permalink_url"]}" color="#2a5a9c">{row["name"]}</a>',
                small,
            ))
        else:
            story.append(Paragraph(f"• {row['name']}", small))

    doc.build(story)
    return buf.getvalue()


# --------------------------------------------------------------------------
# Round helpers
# --------------------------------------------------------------------------
def get_current_round(conn):
    return conn.execute(
        "SELECT * FROM rounds WHERE status='open' ORDER BY id DESC LIMIT 1"
    ).fetchone()


def ranked_results(conn, round_id):
    rows = conn.execute("""
        SELECT t.id, t.name, t.asana_task_gid, t.permalink_url, t.assignee_name,
               COUNT(v.id) AS votes,
               GROUP_CONCAT(v.voter_name, '|') AS voter_names
        FROM topics t
        LEFT JOIN votes v ON v.topic_id = t.id AND v.round_id = t.round_id
        WHERE t.round_id = ?
        GROUP BY t.id
        ORDER BY votes DESC, t.name ASC
    """, (round_id,)).fetchall()
    out = []
    for r in rows:
        out.append({
            "id": r["id"],
            "name": r["name"],
            "assignee_name": r["assignee_name"],
            "permalink_url": r["permalink_url"],
            "asana_task_gid": r["asana_task_gid"],
            "votes": r["votes"],
            "voters": [v for v in (r["voter_names"] or "").split("|") if v],
        })
    return out


def voted_emails(conn, round_id):
    return {
        row["voter_email"] for row in conn.execute(
            "SELECT DISTINCT voter_email FROM votes WHERE round_id = ?",
            (round_id,),
        ).fetchall()
    }


def open_new_round(conn):
    """Close any open round, pull fresh topics from Asana, open a new round."""
    cur = conn.cursor()
    cur.execute(
        "UPDATE rounds SET status='closed', closed_at=? WHERE status='open'",
        (datetime.utcnow().isoformat(),),
    )
    tasks = fetch_section_topics()
    if not tasks:
        conn.commit()
        raise RuntimeError("No topics found in the Short Term Issues List.")
    cur.execute("INSERT INTO rounds (created_at, status) VALUES (?, 'open')",
                (datetime.utcnow().isoformat(),))
    round_id = cur.lastrowid
    for t in tasks:
        cur.execute(
            "INSERT INTO topics "
            "(round_id, asana_task_gid, name, notes, assignee_name, permalink_url) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (round_id, t["gid"], t["name"], t.get("notes"),
             (t.get("assignee") or {}).get("name"), t.get("permalink_url")),
        )
    conn.commit()
    return round_id, len(tasks)


def close_round_and_publish(conn):
    """Close the current round, publish the Asana task + PDF."""
    r = get_current_round(conn)
    if not r:
        raise RuntimeError("No open round to close.")
    round_id = r["id"]
    results = ranked_results(conn, round_id)

    # Friendly date (local to Pacific-labeled just in notes; the actual
    # wall-clock time used is server UTC, rendered in Pacific for display).
    now = datetime.now()
    date_str = now.strftime("%B %-d, %Y") if os.name != "nt" else now.strftime("%B %d, %Y")

    # Notes = text fallback for anyone without PDF access
    lines = [f"Strategic Leadership Meeting — voting results ({date_str})", ""]
    lines.append(f"Top {TOP_N_HIGHLIGHT} for discussion:")
    for i, row in enumerate(results[:TOP_N_HIGHLIGHT], 1):
        lines.append(f"  {i}. {row['name']} — {row['votes']} vote(s)")
    lines.append("")
    lines.append("Full ranked results:")
    for row in results:
        voters = ", ".join(row["voters"]) if row["voters"] else "—"
        lines.append(f"  • {row['name']} — {row['votes']} vote(s) [voted by: {voters}]")
    notes = "\n".join(lines)
    title = f"Issues List Results {date_str}"

    # Ensure target section exists, then create task in it
    section_gid = ensure_results_section_gid()
    task_gid = create_results_task(title, notes, section_gid)

    # Generate PDF & attach
    pdf_bytes = build_results_pdf(results, now)
    filename = f"Issues List Results {date_str}.pdf"
    attachment_gid = attach_pdf_to_task(task_gid, pdf_bytes, filename)

    conn.execute(
        "UPDATE rounds SET status='closed', closed_at=?, "
        "summary_task_gid=?, attachment_gid=? WHERE id = ?",
        (datetime.utcnow().isoformat(), task_gid, attachment_gid, round_id),
    )
    conn.commit()
    return task_gid, attachment_gid


# --------------------------------------------------------------------------
# Voting routes
# --------------------------------------------------------------------------
@app.route("/")
def home():
    conn = get_db()
    r = get_current_round(conn)
    conn.close()
    if r:
        return redirect(url_for("vote"))
    return render_template("no_round.html")


@app.route("/vote", methods=["GET", "POST"])
def vote():
    conn = get_db()
    r = get_current_round(conn)
    if not r:
        conn.close()
        return render_template("no_round.html")
    round_id = r["id"]

    if request.method == "POST":
        voter_email = (request.form.get("voter_email") or "").strip().lower()
        topic_ids = request.form.getlist("topic_id")
        voter = next((m for m in LEADERSHIP_TEAM
                      if m["email"].lower() == voter_email), None)

        if not voter:
            flash("Please pick your name from the list.", "error")
            conn.close()
            return redirect(url_for("vote"))
        if len(topic_ids) != VOTES_PER_PERSON:
            flash(f"Please select exactly {VOTES_PER_PERSON} topics "
                  f"(you picked {len(topic_ids)}).", "error")
            conn.close()
            return redirect(url_for("vote"))
        if len(set(topic_ids)) != len(topic_ids):
            flash("You can't vote for the same topic twice.", "error")
            conn.close()
            return redirect(url_for("vote"))

        cur = conn.cursor()
        cur.execute(
            "DELETE FROM votes WHERE round_id = ? AND voter_email = ?",
            (round_id, voter["email"]),
        )
        now = datetime.utcnow().isoformat()
        for tid in topic_ids:
            cur.execute(
                "INSERT INTO votes (round_id, voter_email, voter_name, topic_id, voted_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (round_id, voter["email"], voter["name"], int(tid), now),
            )
        conn.commit()
        conn.close()
        return redirect(url_for("thanks", voter=voter["email"]))

    topics = conn.execute(
        "SELECT * FROM topics WHERE round_id = ? ORDER BY name", (round_id,),
    ).fetchall()
    already_voted = voted_emails(conn, round_id)
    conn.close()
    return render_template(
        "vote.html", topics=topics, team=LEADERSHIP_TEAM,
        votes_per_person=VOTES_PER_PERSON, voted_emails=already_voted, round=r,
    )


@app.route("/thanks")
def thanks():
    email = request.args.get("voter", "")
    voter = next((m for m in LEADERSHIP_TEAM
                  if m["email"].lower() == email.lower()), None)
    return render_template("thanks.html", voter=voter)


# --------------------------------------------------------------------------
# Scheduled webhook endpoints
# --------------------------------------------------------------------------
def _check_schedule_auth():
    if not SCHEDULE_TOKEN:
        abort(500, "SCHEDULE_TOKEN env var is not set")
    got = request.args.get("token") or request.headers.get("X-Schedule-Token")
    if got != SCHEDULE_TOKEN:
        abort(403)


@app.route("/scheduled/open", methods=["GET", "POST"])
def scheduled_open():
    _check_schedule_auth()
    conn = get_db()
    try:
        round_id, n = open_new_round(conn)
        conn.close()
        return jsonify(ok=True, round_id=round_id, topic_count=n)
    except Exception as e:
        conn.close()
        return jsonify(ok=False, error=str(e)), 500


@app.route("/scheduled/close", methods=["GET", "POST"])
def scheduled_close():
    _check_schedule_auth()
    conn = get_db()
    try:
        task_gid, attachment_gid = close_round_and_publish(conn)
        conn.close()
        return jsonify(ok=True, task_gid=task_gid, attachment_gid=attachment_gid)
    except Exception as e:
        conn.close()
        return jsonify(ok=False, error=str(e)), 500


@app.route("/healthz")
def healthz():
    return "ok", 200


# --------------------------------------------------------------------------
# Admin (manual overrides)
# --------------------------------------------------------------------------
def require_admin(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login", next=request.path))
        return f(*args, **kwargs)
    return wrapper


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if not ADMIN_PASSWORD:
        abort(500, "ADMIN_PASSWORD env var is not set")
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["is_admin"] = True
            return redirect(request.args.get("next") or url_for("admin"))
        flash("Wrong password.", "error")
    return render_template("admin_login.html")


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("home"))


@app.route("/admin")
@require_admin
def admin():
    conn = get_db()
    r = get_current_round(conn)
    results, already_voted = [], set()
    if r:
        results = ranked_results(conn, r["id"])
        already_voted = voted_emails(conn, r["id"])
    history = conn.execute(
        "SELECT id, created_at, closed_at, status, summary_task_gid, attachment_gid "
        "FROM rounds ORDER BY id DESC LIMIT 10"
    ).fetchall()
    conn.close()
    return render_template(
        "admin.html", current_round=r, results=results, team=LEADERSHIP_TEAM,
        voted_emails=already_voted, history=history,
        top_n=TOP_N_HIGHLIGHT, votes_per_person=VOTES_PER_PERSON,
    )


@app.route("/admin/start", methods=["POST"])
@require_admin
def admin_start():
    conn = get_db()
    try:
        _, n = open_new_round(conn)
        conn.close()
        flash(f"New voting round started with {n} topics.", "success")
    except Exception as e:
        conn.close()
        flash(f"Couldn't start round: {e}", "error")
    return redirect(url_for("admin"))


@app.route("/admin/close", methods=["POST"])
@require_admin
def admin_close():
    conn = get_db()
    try:
        task_gid, _ = close_round_and_publish(conn)
        conn.close()
        flash("Round closed. Task + PDF posted to Asana.", "success")
    except Exception as e:
        conn.close()
        flash(f"Failed to close round: {e}", "error")
    return redirect(url_for("admin"))


@app.route("/admin/reset_voter", methods=["POST"])
@require_admin
def admin_reset_voter():
    conn = get_db()
    r = get_current_round(conn)
    if not r:
        conn.close()
        flash("No open round.", "error")
        return redirect(url_for("admin"))
    email = (request.form.get("voter_email") or "").strip().lower()
    conn.execute(
        "DELETE FROM votes WHERE round_id = ? AND voter_email = ?",
        (r["id"], email),
    )
    conn.commit()
    conn.close()
    flash(f"Cleared votes for {email}.", "success")
    return redirect(url_for("admin"))


# --------------------------------------------------------------------------
# Startup
# --------------------------------------------------------------------------
init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=bool(os.environ.get("FLASK_DEBUG")))
