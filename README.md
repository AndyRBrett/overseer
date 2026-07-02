# Project Overseer

Agentic weekly review of personal automation projects, run as a **four-agent
pipeline** on Claude (Opus 4.8). The agents investigate three projects, file
issues on GitHub, **fix what they can and open pull requests**, send a digest
to **Telegram**, and publish that digest to an **installable web app** (PWA)
you can add to your phone's home screen and get a weekly push notification
from.

Everything is hosted by GitHub: the pipeline runs on **GitHub Actions** (weekly
cron), the dashboard is served by **GitHub Pages** from `docs/`, and the push
notification is sent by the same Action. No third-party servers.

Every run also produces a visual report (`overseer_report.html`, uploaded as an
Actions artifact) showing each agent's reasoning and every tool call.

## Design — four agents, separated concerns

The work is split across four sequential agents (orchestrated by
`orchestrator.py`) so no single agent ever conflates "this is broken" with
"this could be better". Each agent is its own `client.messages.create` tool-use
loop and is only given the tools it's allowed to use. The Bug-Hunter and Idea
agents review the three external projects **and Project Overseer itself**, held
to the same bar as any other project (`read_overseer_status`):

1. **Bug-Hunter** (`agent_bug_hunter.py`) — investigates and calls `file_issue()`
   for **confirmed bugs only**. It never proposes enhancements (it isn't even
   shown that tool). Outputs a structured summary of what it found and filed.
2. **Fixer** (`agent_fixer.py`) — takes the Bug-Hunter's report and **actually
   fixes** the clearest fixable issues (at most `FIXER_MAX_FIXES`, default 2).
   For each one it clones the repo into a scratch workspace on an
   `overseer/fix-<issue>` branch, investigates root cause with real evidence
   (runs the code, the tests, git blame), writes a test that **reproduces** the
   bug, implements the fix, verifies the test now passes plus the full suite,
   pushes the branch, and opens a PR whose body carries the evidence
   ("Fixes #N" is appended so the issue closes on merge). Issues that need an
   owner-only decision (scope/product choices, credentials, paid services, data
   deletion, deploy changes) are **escalated instead**: the plain issue stands,
   with the Fixer's findings posted as a comment. Three guarantees are enforced
   in the tools, not just the prompt: only configured project repos can be
   touched; the default branch can never be committed to or pushed (the clone's
   remote holds no credentials — the token is attached only for
   `commit_and_push`'s own guarded push); and `FIXER_MAX_FIXES` is a hard cap —
   `open_pull_request` refuses once the budget is spent.
3. **Idea Agent** (`agent_idea.py`) — ignores what's broken and brainstorms at
   least three `propose_enhancement()` ideas across the projects, each ranked by
   effort vs impact. Outputs a structured idea list.
4. **Reviewer** (`agent_reviewer.py`) — receives the three agents' **text outputs**
   (not the raw logs), dedupes overlap, decides what's worth surfacing this week,
   and calls `send_telegram_summary()` exactly once with a digest split into
   "Issues Found" (each tagged PR opened / needs your decision / not attempted),
   "Fixes Opened (PRs)", and "Top Enhancement Ideas (ranked)".

All tool implementations live in `tools.py`, which every agent imports from, so
tool logic is never duplicated. The Reviewer's digest is also captured into
`docs/digest.json` (updating the web app) and pushed as a notification.

### Dry run (test safely)

`python orchestrator.py --dry-run` runs the entire pipeline but intercepts every
mutating tool — `file_issue`, `propose_enhancement`, `send_telegram_summary`,
and the Fixer's push / `open_pull_request` / `comment_on_issue` — so they
**print what they WOULD do** instead of touching GitHub or Telegram. The Fixer
still clones, edits, tests, and commits locally in its scratch workspace, but
nothing is pushed and no PR or comment is created. Use it to preview changes
before anything goes live.

**Overseer reviews itself, too.** Both the Bug-Hunter and the Idea agent treat
the overseer repo as a fourth project: `read_overseer_status` checks its own
weekly-run health, and they file bugs / propose enhancements against the overseer
just like any other project. The repo defaults to `GITHUB_REPOSITORY` (override
with an `OVERSEER_REPO` variable). For self-filing to work,
`OVERSEER_GITHUB_TOKEN` must include **this** repo with Issues: write.

## What you need to provide (and how to get each)

Only the Anthropic key is required. Anything unset just makes that tool report
"not configured", and the agent works around it.

| # | Thing | How to get it |
|---|-------|---------------|
| 1 | **Anthropic API key** | console.anthropic.com → API Keys → Create. The only required value. |
| 2 | **GitHub token** (PAT) | github.com → Settings → Developer settings → Fine-grained tokens. Give it your 3 project repos with **Issues: Read and write**, plus **Contents: Read and write** and **Pull requests: Read and write** so the Fixer can push fix branches and open PRs. |
| 3 | **Project repo slugs** | `owner/name` for each repo, so issues file in the right place. |
| 4 | **Data source paths** | Where each project's data lives (see below). Skip any you don't have. |

Add these in your repo settings (Settings → Secrets and variables → Actions):
- **Secrets:** `ANTHROPIC_API_KEY`, `OVERSEER_GITHUB_TOKEN`, and (optional, for
  the Telegram digest) `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- **Variables:** `TRADING_REPO`, `VOLLEYBALL_REPO`, `UFC_REPO`,
  `TRADING_DB_PATH`, `VOLLEYBALL_RESULTS_PATH`

To get the Telegram values: message [@BotFather](https://t.me/BotFather) →
`/newbot` for the `TELEGRAM_BOT_TOKEN`, then send your new bot a message and read
your chat id from `https://api.telegram.org/bot<token>/getUpdates` for
`TELEGRAM_CHAT_ID`. If you skip these, the Reviewer reports "not configured" and
the run still succeeds — the digest just isn't sent to Telegram.

### Data sources (for the `read_*` tools)

- **Trading bot** — two modes:
  - *Cloud (recommended, daily bot):* the bot publishes `overseer-status.json`
    to `TRADING_REPO`; the overseer reads it via the GitHub API (flagged stale
    after 48h). Drop-in publisher: `examples/trading-bot-status/`.
  - *Local:* set `TRADING_DB_PATH` to a SQLite trade log; `TRADING_QUERY`
    assumes a `trades(ts, pnl)` table — edit to match your schema.
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

**At a glance + idle nudges.** The top of the dashboard shows a one-line rollup
of the run — how many projects are healthy, how many need attention, and how many
issues/ideas came out of it — so each weekly review is scannable in seconds. Any
project that's been idle or blind for **≥ `OVERSEER_NUDGE_CYCLES`** consecutive
runs (default 2) is promoted from a quiet badge to an explicit call-out at the
top, so a project quietly going dark (e.g. volleyball idle for several cycles)
can't hide in the timeline. The threshold is reused by the per-project health
card; set the `OVERSEER_NUDGE_CYCLES` variable to tune it without code changes.

**Trends (week over week).** Each run also appends a small record to
`docs/history.json` (per-project health score + issue/enhancement counts, capped
to the last ~26 runs). The dashboard turns it into inline sparklines — one per
project plus an overall issues/enhancements trend — so a regression (a project
sliding from healthy → idle → blind, or issue counts creeping up) is visible at a
glance instead of being lost in a point-in-time snapshot.

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
python orchestrator.py            # for real
python orchestrator.py --dry-run  # intercept all mutations, print instead
```

This writes `docs/digest.json`, appends to `docs/history.json`, and writes
`overseer_report.html` locally so you can preview all of them.

## Files

- `orchestrator.py` — runs the four agents sequentially; `--dry-run` flag
- `agent_bug_hunter.py` / `agent_fixer.py` / `agent_idea.py` / `agent_reviewer.py`
  — the four agents
- `agent_janitor.py` — standalone issue-tracker triage (not part of the weekly
  run): verifies which open issues are already implemented and closes them with
  commit evidence; `python agent_janitor.py [--dry-run]`
- `tools.py` — shared tool implementations, schemas, config, and the agent runtime
- `tracer.py` — live console trace, HTML report, `docs/digest.json` writer, and
  the append-only `docs/history.json` trend log
- `docs/` — the installable web app (GitHub Pages): `index.html`, `app.js`,
  `sw.js` (service worker / push handler), `manifest.webmanifest`, icons
- `scripts/notify_push.py` — sends the weekly push (run by the Action)
- `.github/workflows/weekly-review.yml` — cron, digest commit, push, report artifact
