# Project Overseer

Agentic weekly review of personal automation projects. Claude (Opus 4.8) is
given a set of tools and decides on its own what to investigate, whether
something is a real bug, and what enhancements to propose — then files issues
on GitHub and sends a digest to Telegram.

Every run also produces a **visual report** (`overseer_report.html`) showing the
agent's reasoning, every tool it called, what came back, and where anything
failed — so the decision process is easy to follow and troubleshoot.

The key design choice: **two distinct tool types** so the agent never conflates
"this is broken" with "this could be better":

- `file_issue()` — confirmed bugs / failures only
- `propose_enhancement()` — ideas, always ranked by effort vs impact, even when
  nothing is broken (filed as a labelled GitHub issue)

This forces a forward-looking suggestion every run, not just a health check.

## What you need to provide (and how to get each)

Everything is optional except the Anthropic key — anything you leave unset just
makes the matching tool report "not configured", and the agent works around it.

| # | Thing | How to get it |
|---|-------|---------------|
| 1 | **Anthropic API key** | console.anthropic.com → **API Keys** → *Create Key*. This is the only required value. |
| 2 | **GitHub token** (PAT) | github.com → your avatar → **Settings → Developer settings → Personal access tokens → Fine-grained tokens** → *Generate new token*. Give it access to your 3 project repos, and under **Repository permissions** set **Issues: Read and write**. Copy the token (starts with `github_pat_`). |
| 3 | **Telegram bot token** | In Telegram, message **@BotFather** → send `/newbot` → follow prompts. It replies with a token like `123456:ABC-…`. |
| 4 | **Telegram chat ID** | Send any message to your new bot, then open `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser. Find `"chat":{"id":...}` — that number is your chat ID. |
| 5 | **Project repo slugs** | The `owner/name` for each repo, e.g. `andyrbrett/trading-bot`. Used so issues are filed in the right place. |
| 6 | **Data source paths** | Where each project's data lives (see below). Skip any you don't have yet. |

### Data sources (steps for the `read_*` tools)

- **Trading bot** — `TRADING_DB_PATH`: path to your SQLite trade log. The query
  in `project_overseer.py` (`TRADING_QUERY`) assumes a `trades(ts, pnl)` table;
  edit it to match your schema.
- **Volleyball** — `VOLLEYBALL_RESULTS_PATH`: path to a JSON file your pipeline
  writes (detection accuracy, failed frames, clips processed). The whole JSON is
  handed to the agent, so any shape works.
- **UFC** — `UFC_REPO`: the `owner/name` of the scraper repo. Its **GitHub
  Actions run history** is read automatically via the token in step 2 — no
  extra setup.

## Running locally

```bash
pip install -r requirements.txt

export ANTHROPIC_API_KEY=...
export OVERSEER_GITHUB_TOKEN=github_pat_...
export TELEGRAM_BOT_TOKEN=...   ;  export TELEGRAM_CHAT_ID=...
export TRADING_REPO=owner/trading-bot   ;  export TRADING_DB_PATH=/path/to/trades.db
export VOLLEYBALL_REPO=owner/volleyball ;  export VOLLEYBALL_RESULTS_PATH=/path/to/results.json
export UFC_REPO=owner/ufc-dashboard

python project_overseer.py
```

Open `overseer_report.html` afterwards to see what the agent did.

## Weekly cron + the visual report

`.github/workflows/weekly-review.yml` runs the review every Monday 14:00 UTC
(and on manual dispatch from the **Actions** tab). After each run it uploads
`overseer_report.html` + `overseer_run.jsonl` as a downloadable **artifact** —
including when a run fails, which is exactly when you want the trace.

Add the values from the table above in your repo settings:

- **Secrets** (Settings → Secrets and variables → Actions → *Secrets*):
  `ANTHROPIC_API_KEY`, `OVERSEER_GITHUB_TOKEN`, `TELEGRAM_BOT_TOKEN`,
  `TELEGRAM_CHAT_ID`
- **Variables** (same screen → *Variables*): `TRADING_REPO`, `VOLLEYBALL_REPO`,
  `UFC_REPO`, `TRADING_DB_PATH`, `VOLLEYBALL_RESULTS_PATH`

## Files

- `project_overseer.py` — config, tools, and the agentic loop
- `tracer.py` — the visual: live console trace, JSONL event log, HTML timeline
- `.github/workflows/weekly-review.yml` — weekly cron + report artifact
