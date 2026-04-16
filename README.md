# SLM Issues List Voting

A small, always-on web app for Sun City Church's Strategic Leadership Meeting.

## What it does

Each week, automatically:

1. **Sunday 5:00 PM Pacific** — voting opens. The app pulls the current incomplete tasks from the **Short Term Issues List** section of the *Strategic Leadership Meeting* project in Asana and puts them on the voting page.
2. **All week** — the 7 leaders visit the site, pick their name, choose **exactly 2 topics**, and submit. Submitting again replaces the prior picks.
3. **Monday 9:00 AM Pacific** — voting closes. The app creates one Asana task titled `Issues List Results <date>` in the **Issues List Voting Results** section of the project, with a formatted PDF attached (top 5 highlighted, full ranked list, links back to source tasks).

No texting, no PDFs emailed around, no manual steps during a normal week. Asana is the source of truth for results.

## One-time setup (≈ 10 minutes)

You're deploying to [Render](https://render.com)'s free tier. Everything happens in the browser — no Terminal, no Git CLI.

### 1. Put the code on GitHub

If you don't have a GitHub account, create one at https://github.com/signup (free, takes 30 seconds).

1. Go to https://github.com/new
2. Repository name: `slm-voting`
3. Keep it **Private**
4. Click **Create repository**
5. On the empty-repo page, click the link that says **"uploading an existing file"**
6. Drag every file and folder inside the `sllm-voting` folder (not the folder itself — its contents) into the upload area
7. Scroll down, click **Commit changes**

### 2. Deploy to Render

1. Go to https://dashboard.render.com/register and sign up **with GitHub** (one click)
2. In the Render dashboard, click **New +** → **Blueprint**
3. Click **Connect a repository** → pick your `slm-voting` repo → **Connect**
4. Render will see the `render.yaml` file and show you what it'll create (one web service + one 1GB disk). Click **Apply**
5. Render will start building. It'll ask you for two secret values:
   - `ASANA_TOKEN` → paste your Asana Personal Access Token (from https://app.asana.com/0/my-apps → "+ Create new token")
   - `ADMIN_PASSWORD` → pick any password for the admin page
   (`SCHEDULE_TOKEN` and `FLASK_SECRET_KEY` are auto-generated for you)
6. Wait 2–3 minutes for the build. When done, Render shows you a URL like `https://slm-voting.onrender.com`. **This is the URL you share with the team.**

### 3. Note your scheduler URLs

On your Render service page, click **Environment** in the sidebar and copy the value of `SCHEDULE_TOKEN` (click the eye icon to reveal it). You'll need it for the two cron jobs.

Your two scheduled URLs are:

- **Open voting** (Sundays 5pm PT): `https://YOUR-APP.onrender.com/scheduled/open?token=YOUR_SCHEDULE_TOKEN`
- **Close voting** (Mondays 9am PT): `https://YOUR-APP.onrender.com/scheduled/close?token=YOUR_SCHEDULE_TOKEN`

### 4. Set up the weekly schedule

Use **Cowork's scheduled tasks** (the same thing you've been using here) to fire those two URLs at the right times. Ask me in Cowork:

> "Schedule a weekly task that hits `https://slm-voting.onrender.com/scheduled/open?token=...` every Sunday at 5pm Pacific."
>
> "Schedule a weekly task that hits `https://slm-voting.onrender.com/scheduled/close?token=...` every Monday at 9am Pacific."

(Free alternative if you prefer not to use Cowork: https://cron-job.org is a free web service that will hit those URLs on a schedule. Create a free account, add two cron jobs with the URLs above and the times in Pacific.)

## Running a meeting — what you'll actually do

**Nothing on normal weeks.** Sunday evening the team gets the voting link, they vote through the week, Monday morning the results task appears in Asana before your meeting starts.

If you need to run it off-cycle (e.g., the rhythm moved), log into `https://YOUR-APP.onrender.com/admin/login` with your admin password, and you'll see **Start new round** and **Close voting & post to Asana** buttons. The manual buttons work exactly like the cron — same result.

## Heads-up: Render free tier sleeps

On the free tier, Render puts the web service to sleep after 15 minutes of no traffic. **The first request to a sleeping app takes ~30 seconds to wake up**, but it always wakes up and runs correctly. The scheduled open/close calls still work — they just take a bit longer the first time they fire.

If the 30-second delay ever becomes annoying, upgrade Render to the $7/mo Starter plan and the app stays awake 24/7. For 7 people voting once a week, the free tier is fine.

## Files in this folder

| File | What it is |
| --- | --- |
| `app.py` | The Flask app (routes, Asana calls, PDF generation, SQLite) |
| `templates/` | HTML pages (vote, admin, thanks, login) |
| `static/style.css` | Styling |
| `requirements.txt` | Python dependencies |
| `render.yaml` | Render Blueprint — tells Render how to deploy this |
| `.env.example` | Template for local-only testing (not used on Render) |
| `Procfile`, `runtime.txt` | Backup deploy config for hosts other than Render |
| `test_smoke.py` | End-to-end test with mocked Asana calls |
| `sample_results.pdf` | Example of what the PDF looks like (delete anytime) |

## Troubleshooting

- **Results task didn't appear Monday morning** — Go to your Render dashboard → service → **Logs**. Search for `/scheduled/close`. If the request arrived but errored, the log will tell you why (most common: Asana token expired, re-generate at https://app.asana.com/0/my-apps).
- **"Issues List Voting Results" section doesn't exist in Asana** — The app creates it automatically the first time it posts results. If you'd rather have it sit in a specific spot in the project, create the section yourself (same exact name) and the app will find it.
- **Team member missing from the dropdown** — Edit the `LEADERSHIP_TEAM` list at the top of `app.py` and commit+push the change (GitHub web editor works fine). Render auto-redeploys on push.
- **Wrong vote count / need to re-open voting** — Log into `/admin`, hit **Start new round** manually. You can also reset one voter's picks from there.
- **I want to change "Top 5" to Top 3 / give people 3 votes** — In Render → **Environment**, change `TOP_N_HIGHLIGHT` or add `VOTES_PER_PERSON=3`. Render redeploys automatically.
