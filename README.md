# Project Overseer

Agentic weekly review of personal automation projects. Claude (Opus 4.8) is
given a set of tools and decides on its own what to investigate, whether
something is a real bug, and what enhancements to propose — then files issues on
GitHub and publishes a digest to an **installable web app** (PWA) you can add to
your phone's home screen and get a weekly push notification from.

Everything is hosted by GitHub: the agent runs on **GitHub Actions** (weekly
cron), the dashboard is served by **GitHub Pages** from `docs/`, and the push
notification is sent by the same Action. No third-party servers.

Every run also produces a visual report (`overseer_report.html`, uploaded as an
Actions artifact) showing the agent's reasoning and every tool call.

## Design

Two distinct tool types so the agent never conflates "this is broken" with
"this could be better":

- `file_issue()` — confirmed bugs / failures only
- `propose_enhancement()` — ideas, always ranked by effort vs impact, even when
  nothing is broken (filed as a labelled GitHub issue)

`publish_digest()` writes the weekly summary, which the Action commits to
`docs/digest.json` (updating the web app) and pushes as a notification.

**Overseer reviews itself, too.** It treats the overseer repo as a fourth
project: `read_overseer_status` checks its own weekly-run health, and it files
bugs/enhancements against itself like any other project. The repo defaults to
`GITHUB_REPOSITORY` (override with an `OVERSEER_REPO` variable). For self-filing
to work, `OVERSEER_GITHUB_TOKEN` must include **this** repo with Issues: write.

## What you need to provide (and how to get each)

Only the Anthropic key is required. Anything unset just makes that tool report
"not configured", and the agent works around it.

| # | Thing | How to get it |
|---|-------|---------------|
| 1 | **Anthropic API key** | console.anthropic.com → API Keys → Create. The only required value. |
| 2 | **GitHub token** (PAT) | github.com → Settings → Developer settings → Fine-grained tokens. Give it your 3 project repos with **Issues: Read and write**. |
| 3 | **Project repo slugs** | `owner/name` for each repo, so issues file in the right place. |
| 4 | **Data source paths** | Where each project's data lives (see below). Skip any you don't have. |

Add these in your repo settings (Settings → Secrets and variables → Actions):
- **Secrets:** `ANTHROPIC_API_KEY`, `OVERSEER_GITHUB_TOKEN`
- **Variables:** `TRADING_REPO`, `VOLLEYBALL_REPO`, `UFC_REPO`,
  `TRADING_DB_PATH`, `VOLLEYBALL_RESULTS_PATH`

### Data sources (for the `read_*` tools)

- **Trading bot** — `TRADING_DB_PATH`: SQLite trade log. The query in
  `project_overseer.py` (`TRADING_QUERY`) assumes a `trades(ts, pnl)` table;
  edit it to match your schema.
- **Volleyball** — `VOLLEYBALL_RESULTS_PATH`: a JSON file your pipeline writes.
- **UFC** — `UFC_REPO`: the scraper repo. Its GitHub Actions run history is read
  automatically via the token above — no extra setup.

## The phone app + push notifications

The dashboard lives in `docs/` and is served by GitHub Pages.

**1. Turn on Pages (once):** repo → Settings → Pages → Source = *Deploy from a
branch*, branch = `main`, folder = `/docs`. Your app URL appears there (like
`https://<you>.github.io/overseer/`).

**2. Install it on your phone (once):** open that URL in your phone browser →
- iPhone (Safari): Share → **Add to Home Screen** (needs iOS 16.4+)
- Android (Chrome): menu → **Install app / Add to Home Screen**

The app shows the latest digest and run stats, refreshed each week. That alone
needs nothing further.

**3. Enable push (optional, one-time wiring):** push has to be *sent* by
something — here, the weekly Action. To set that up:

  a. **Generate VAPID keys** (the keypair that authorises pushes). Easiest:
     ```
     npx web-push generate-vapid-keys
     ```
     (or `pip install py-vapid && vapid --gen`). You get a **public** and a
     **private** key.
  b. Put the **public** key in `docs/vapid-public.txt` and commit it (it's
     public by design).
  c. Add secrets: `VAPID_PRIVATE_KEY` (the private key), `VAPID_SUBJECT`
     (`mailto:you@example.com`).
  d. Open the installed app, tap **Enable weekly push notifications**, allow it.
     The app shows a blob of text — copy it into a secret named
     `PUSH_SUBSCRIPTION`. (This is your device telling the Action where to push;
     it can't be automated on static hosting.)

After that, each weekly run pushes "Weekly review ready" to your phone. If you
skip step 3, the app still updates every week — you just open it to read the
digest instead of being pinged.

## Running locally

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=...
export OVERSEER_GITHUB_TOKEN=github_pat_...
export TRADING_REPO=owner/trading-bot   ; export TRADING_DB_PATH=/path/to/trades.db
export VOLLEYBALL_REPO=owner/volleyball ; export VOLLEYBALL_RESULTS_PATH=/path/to/results.json
export UFC_REPO=owner/ufc-dashboard
python project_overseer.py
```

This writes `docs/digest.json` and `overseer_report.html` locally so you can
preview both.

## Files

- `project_overseer.py` — config, tools, agentic loop
- `tracer.py` — live console trace, HTML report, and `docs/digest.json` writer
- `docs/` — the installable web app (GitHub Pages): `index.html`, `app.js`,
  `sw.js` (service worker / push handler), `manifest.webmanifest`, icons
- `scripts/notify_push.py` — sends the weekly push (run by the Action)
- `.github/workflows/weekly-review.yml` — cron, digest commit, push, report artifact
