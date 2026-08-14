# Project Overseer

Agentic weekly review of personal automation projects, run as a **four-agent
pipeline** on Claude. The agents investigate three projects, file issues on
GitHub, **fix what they can and open pull requests**, send a digest to
**Telegram**, and publish that digest to an **installable web app** (PWA)
you can add to your phone's home screen and get a weekly push notification
from.

To keep the weekly cost down the pipeline uses two model tiers: the **Fixer**
runs on Opus 4.8 (`OVERSEER_MODEL`) since writing correct code and tests is
the judgment-heavy stage, while the Bug-Hunter, Idea Agent, Reviewer, and
Janitor run on Sonnet 5 (`OVERSEER_LIGHT_MODEL`), which is roughly 5x cheaper
per token.

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

### Closing the loop (the delivery ledger)

The pipeline proposed work every week and never learned what came of it. That
cost two things, and one ledger fixes both.

Every issue the agents file is stamped `_Filed by Project Overseer._`, so the
overseer can read its own issues back and see what happened to them. That
becomes `docs/shipped.json` and the dashboard's **Shipped** panel — what the
overseer has actually *delivered*, not merely suggested.

**`in_flight` requires positive evidence.** A closed-as-completed issue counts as
delivered unless a linked PR is actually still open — that one case is what stops
the panel taking credit for code sitting in review. The first cut had this
backwards, treating "no merged PR found" as proof of non-delivery, which reported
six coachvision features live in production since June as "in flight": these
repos land most work by direct commit, so a missing PR link is the normal case,
not a red flag. Duplicates are excluded from the delivery-rate denominator (one
dedupe failure shouldn't be punished twice) and reported separately.

The same ledger is injected into the Bug-Hunter's and Idea agent's prompts as an
**ALREADY ON RECORD** list. That is the more valuable half. Without it the
pipeline re-proposed its own ideas constantly:

| Cluster | Filings |
|---|---|
| overseer — schema validation | #9, #12, #14, #16 (4×, over 7 weeks) |
| crypto-trading — signal near-miss telemetry | #27, #35, #38 (3×, all still open) |
| ufc-dashboard — line-movement alerts | #17 (shipped), #21, #26, #52, #67 |
| ufc-dashboard — CLV / odds time-series | #19 (shipped), #22, #68 |
| overseer — dead-man's switch | #13, #17 |
| overseer — staleness alerting | #11, #15 |
| coachvision — demo/sample output | #19, #21 |

`ufc-dashboard#68` is the sharpest case: it proposed a per-bout CLV tracker that
was already built, running, and computing CLV for 95 bouts in production.

#### Keeping the ledger live

The ledger used to be written only by the weekly review, so a PR merged on
Tuesday still read "in flight" until the following Monday. `ledger-refresh.yml`
decouples it: the refresh is **pure GitHub reads** — no Anthropic key, no agents,
no model calls — so it can run constantly for effectively nothing.

| Trigger | Covers | Latency |
|---|---|---|
| `pull_request: closed`, `issues: closed/reopened` | the overseer's own work | seconds |
| `schedule` hourly at :20 | the other three repos | ≤ 1 hour |
| `repository_dispatch: ledger-refresh` | opt-in, any repo | seconds |
| `workflow_dispatch` (`full: true`) | manual, full re-walk | on demand |

**Why the other three repos poll instead of pushing:** GitHub fires
`pull_request` events only in the repo where the PR lives, so instant cross-repo
updates need a `repository_dispatch` call *from* each project repo — which means
a PAT with `actions: write` stored in three more places. That is the credential
sprawl that caused the July outage, traded for 59 minutes of latency. The
dispatch trigger is wired up regardless, so you can opt any repo in by adding a
step to its own workflow:

```yaml
      - name: Tell the overseer something shipped
        if: github.event.pull_request.merged == true
        run: |
          curl -sS -X POST -H "Accept: application/vnd.github+json" \
            -H "Authorization: Bearer ${{ secrets.OVERSEER_DISPATCH_TOKEN }}" \
            https://api.github.com/repos/<you>/overseer/dispatches \
            -d '{"event_type":"ledger-refresh"}'
```

The refresh is **incremental**: outcomes that can't change again (shipped,
duplicate, not planned) are carried forward from the published ledger, so the
expensive per-issue timeline lookup only runs for entries still in motion — 9 of
65 today. A reopened issue drops its settled status and is recomputed. Run with
`--full` to re-walk everything.

It refuses to publish a ledger emptier than the live one: entries don't vanish,
so a collapse to zero means a bad credential or an unconfigured environment, and
blanking the panel would destroy the record it exists to keep.

### Failing loudly (credential preflight + heartbeat)

Between 2026-07-20 and 2026-08-10 the weekly Action reported **success** four
times in a row while an expired PAT made every GitHub tool call return 401. The
agents handled each tool error gracefully and still wrote a digest, so nothing
went red and all four projects sat unreviewed for a month. Two independent
guards now make that failure loud:

**1. Credential preflight** (`tools.preflight_github`, run by the orchestrator
before any agent starts). It checks that a token exists, that it authenticates,
and that each configured repo is actually reachable with it:

| Situation | Result |
|---|---|
| No token at all | warn, continue — every GitHub tool reports `not_configured` |
| Token expired / revoked (401) | **abort with a non-zero exit** before spending API budget |
| Token reaches *some* repos | warn by name, continue — those projects go unreviewed |
| Token reaches *no* repos | **abort** — a review that reads nothing isn't a review |

**2. Heartbeat** (`scripts/heartbeat.py`, its own daily workflow). A job cannot
detect its own failure to start, so this runs separately and asks two things:
*did the review run* (is `docs/digest.json` still advancing?) and *did it see
anything* (did most tool calls fail?). The second is what catches a green-but-
blind run. It exits non-zero on failure, which turns the Action red and triggers
GitHub's own failure email, and optionally sends a Telegram alert.

The heartbeat is **standard-library only** and uses no GitHub token by design —
the outage it exists to catch is a broken credential, so it must not need one.

Tune with `HEARTBEAT_MAX_AGE_HOURS` (default 192h — one missed weekly run plus a
day of slack) and `HEARTBEAT_ERROR_RATIO` (default 0.5 of tool calls).

> **Fine-grained PATs expire.** That is what caused the outage. Set a calendar
> reminder for a week before yours lapses, or use a longer expiry — the heartbeat
> will tell you within a day either way, but a reminder avoids the gap entirely.

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

The dashboard follows your system light/dark theme, and every card is built for
phone-first reading: the digest and agent timeline have **Copy** (and, on
browsers with a native share sheet, **Share**) buttons, filed issues are
tappable links to GitHub, the step-by-step agent trace is collapsed behind a
"Show all steps" toggle, truncated reasoning expands in place, and a
**Previous runs** card archives past digests. The header shows how long ago the
last run happened (amber if a weekly run looks overdue) with a manual refresh
button.

**At a glance + idle nudges.** The top of the dashboard shows a one-line rollup
of the run — how many projects are healthy, how many need attention, and how many
issues/ideas came out of it — so each weekly review is scannable in seconds. Any
project that's been idle or blind for **≥ `OVERSEER_NUDGE_CYCLES`** consecutive
runs (default 2) is promoted from a quiet badge to an explicit call-out at the
top, so a project quietly going dark (e.g. volleyball idle for several cycles)
can't hide in the timeline. The threshold is reused by the per-project health
card; set the `OVERSEER_NUDGE_CYCLES` variable to tune it without code changes.

**Per-project freshness SLA + staleness alerts.** Every project that publishes an
`overseer-status.json` is held to a **freshness SLA** — how old that file's
`generated_at` may get before the feed is flagged **STALE** (a scheduled job that
has silently stopped, e.g. issue #34: a crypto feed that sat ~153h stale while the
digest stayed quiet). Each project sets its own SLA, since a daily bot's data goes
stale far sooner than a slower pipeline's; the default is **48h** (two missed daily
runs). Tune per project with the `TRADING_SLA_HOURS`, `VOLLEYBALL_SLA_HOURS`, and
`UFC_SLA_HOURS` variables (or change the shared default with `FRESHNESS_SLA_HOURS`).
When any feed is past-due, a machine-generated **`STALENESS ALERTS`** block —
listing each feed with how far past its SLA it is (`data 153h old, SLA 48h`) — is
prepended to the top of the digest **before it's sent**, so it leads both the
Telegram message and the dashboard, independent of what the review agents wrote. A
halted feed can no longer hide behind a quiet summary.

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
- `scripts/heartbeat.py` — dead-man's switch: alerts if the weekly run stops
  happening, or completes while blind (stdlib only, no token)
- `.github/workflows/weekly-review.yml` — cron, digest commit, push, report artifact
- `.github/workflows/heartbeat.yml` — daily heartbeat, independent of the above
