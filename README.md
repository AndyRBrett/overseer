# Project Overseer

Agentic weekly review of personal automation projects. Claude (Opus 4.8) is
given a set of tools and decides on its own what to investigate, whether
something is a real bug, and what enhancements to propose — then sends a digest
to Telegram.

The key design choice: **two distinct tool types** so the agent never conflates
"this is broken" with "this could be better":

- `file_issue()` — confirmed bugs / failures only
- `propose_enhancement()` — ideas, always ranked by effort vs impact, even when
  nothing is broken

This forces a forward-looking suggestion every run, not just a health check.

## Status

The agentic loop is fully functional — Claude decides what to call and in what
order, with a `MAX_ITERATIONS` safety bound, per-tool error isolation
(`is_error` tool results), adaptive thinking, and prefix caching.

The six tool functions are **stubs marked `# TODO`**. They need wiring to your
real data sources.

## Wiring the stubs

1. `read_trading_bot_log` → your SQLite trade log
2. `read_volleyball_results` → your CV pipeline's output JSON/CSV
3. `read_ufc_scraper_status` → GitHub Actions run history / local log
4. `search_existing_issues`, `file_issue`, `propose_enhancement` → GitHub REST
   API (PyGithub is easiest). `propose_enhancement` should file as an issue
   labeled `enhancement`.
5. `send_telegram_summary` → Telegram Bot API (`requests.post` to
   `https://api.telegram.org/bot<TOKEN>/sendMessage`)

## Running

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=...
python project_overseer.py
```

## Weekly cron

`.github/workflows/weekly-review.yml` runs the review every Monday 14:00 UTC
(and on manual dispatch). Add these repository secrets under
**Settings → Secrets and variables → Actions**:

| Secret | Used by |
| --- | --- |
| `ANTHROPIC_API_KEY` | the Claude client (required) |
| `OVERSEER_GITHUB_TOKEN` | `file_issue` / `propose_enhancement` (PAT with issue scope on your project repos) |
| `TELEGRAM_BOT_TOKEN` | `send_telegram_summary` |
| `TELEGRAM_CHAT_ID` | `send_telegram_summary` |

Uncomment the matching `env:` lines in the workflow as you wire each stub.
