# Project Overseer

Agentic weekly review of personal automation projects, run as a **three-agent
pipeline** on Claude (Opus 4.8). The agents investigate three projects, file
issues on GitHub, send a digest to **Telegram**, and publish that digest to an
**installable web app** (PWA) you can add to your phone's home screen and get a
weekly push notification from.

The issues it files don't have to wait for you, either: a gated handful of them
are handed each week to a coding agent running in each project's own repo, which
opens the pull request — see [Implementing what it files](#implementing-what-it-files).

Everything is hosted by GitHub: the pipeline runs on **GitHub Actions** (weekly
cron), the dashboard is served by **GitHub Pages** from `docs/`, and the push
notification is sent by the same Action. No third-party servers.

Every run also produces a visual report (`overseer_report.html`, uploaded as an
Actions artifact) showing each agent's reasoning and every tool call.

Working on the code? **`CLAUDE.md`** is the orientation file — the invariants
that must not be broken, the gotchas that have already cost a week or a bill,
and what a run actually costs. It is written for whoever (or whatever) picks
this up next.

## Design — three agents, separated concerns

The work is split across three sequential agents (orchestrated by
`orchestrator.py`) so no single agent ever conflates "this is broken" with
"this could be better". Each agent is its own `client.messages.create` tool-use
loop and is only given the tools it's allowed to use. The Bug-Hunter and Idea
agents review the three external projects **and Project Overseer itself**, held
to the same bar as any other project (`read_overseer_status`):

1. **Bug-Hunter** (`agent_bug_hunter.py`) — investigates and calls `file_issue()`
   for **confirmed bugs only**. It never proposes enhancements (it isn't even
   shown that tool). Outputs a structured summary of what it found and filed.
2. **Idea Agent** (`agent_idea.py`) — ignores what's broken and brainstorms at
   least three `propose_enhancement()` ideas across the projects, each ranked by
   effort vs impact. Outputs a structured idea list.
3. **Reviewer** (`agent_reviewer.py`) — receives the two agents' **text outputs**
   (not the raw logs), dedupes overlap, decides what's worth surfacing this week,
   and calls `send_telegram_summary()` exactly once with a digest split into
   "Issues Found" and "Top Enhancement Ideas (ranked)".

All tool implementations live in `tools.py`, which every agent imports from, so
tool logic is never duplicated. The Reviewer's digest is also captured into
`docs/digest.json` (updating the web app) and pushed as a notification.

**Telemetry is read once per run**, before any agent starts, and injected into
the Bug-Hunter's and Idea Agent's prompts (`tools.read_all_projects`). The two
agents were each opening the same four status files in the same run, and each
spent a full API turn — carrying its entire context — doing it. Reading once
removes that turn from both loops, and means both agents reason over the *same*
snapshot; they used to read minutes apart, so a feed that published in between
could show them different pictures of one run. The Bug-Hunter keeps the read
tools for re-checking a specific feed while confirming a bug; the Idea Agent
does not have them, so the saving isn't optional.

### Model tiers — paying for judgment, not for text

The three agents don't all need the same class of model, and running them all on
the most expensive one was buying nothing on two of the three.

| Agent | Tier | Why |
| --- | --- | --- |
| Bug-Hunter | **heavy** (`OVERSEER_MODEL`) | Its calls are the consequential ones: telling a feed that's legitimately quiet apart from one that has died, and deciding whether to file a bug on a real repo. Getting that wrong means a false alarm — or another silent four-week outage, which is the failure this pipeline exists to catch. |
| Idea Agent | light (`OVERSEER_LIGHT_MODEL`) | Enhancement ideation and effort/impact ranking. Its output is filtered by the Reviewer and then by a human, so a weak idea costs one line in a digest. |
| Reviewer | light | Dedupes and summarizes two reports it's handed. It reads no data and files nothing. |

Defaults: `OVERSEER_MODEL=claude-opus-4-8`, `OVERSEER_LIGHT_MODEL=claude-sonnet-5`.
On a representative run that lands around **20% cheaper** — the light agents are
roughly 40% of the spend and cost 60% as much per token.

Three knobs, all optional:

- `OVERSEER_MODEL` — the heavy tier.
- `OVERSEER_LIGHT_MODEL` — the light tier. **Set it to the same value as
  `OVERSEER_MODEL` to put the whole pipeline back on one model.** The tiering has
  an off switch that needs no code change.
- `OVERSEER_HEAVY_AGENTS` — comma-separated agents that get the heavy tier
  (default `Bug-Hunter`; valid names are `Bug-Hunter`, `Idea-Agent`, `Reviewer`).
  A name matching no agent is reported at startup rather than silently ignored —
  a typo here would quietly demote the Bug-Hunter.

**The panel scopes itself to the review run, and says so.** The implementer
runs in each project's own repo, so none of its spend reaches these token
counts — and it is ~4x the review. Left unsaid, "$0.34 this run" reads as the
week's bill when the week is nearer $4.80. The panel therefore names what it
excludes, with the queued attempts' estimated cost, labelled as an estimate
because everything else on it is measured.

**The saving is measured, not asserted.** Every response's token usage is
recorded per agent, and each run's digest carries a `spend` block that the
dashboard renders as a **Model spend** panel: what each agent cost, the run
total, and a baseline that reprices *the same token counts* at the heavy rate —
i.e. what this run would have cost with every agent on the heavy tier. Cumulative
saving is trended across runs in `docs/history.json`. Figures come from published
list prices (`MODEL_PRICES` in `tracer.py`); Anthropic's invoice is the authority,
and a model missing from that table is reported as *unpriced* rather than free.

### Dry run (test safely)

`python orchestrator.py --dry-run` runs the entire pipeline but intercepts the
three mutating tools — `file_issue`, `propose_enhancement`, and
`send_telegram_summary` — so they **print what they WOULD do** instead of
touching GitHub or Telegram. Use it to preview changes before anything goes live.

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

**Shipped means merged.** A closed issue whose fix sits on an unreviewed branch
is `in_flight`, not delivered — otherwise the panel flatters itself. Duplicates
are excluded from the delivery-rate denominator (one dedupe failure shouldn't be
punished twice) and reported separately as their own rate.

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
| `schedule` hourly at :20 | the other three repos | **~2–6h in practice, see below** |
| `repository_dispatch: ledger-refresh` | opt-in, any repo | seconds |
| `workflow_dispatch` (`full: true`) | manual, full re-walk | on demand |

**The hourly cron is not delivered hourly.** GitHub deprioritises scheduled
workflows on free public repos, and measured over 29 consecutive scheduled runs
(2026-08-25 to 08-30) this one fires **about 6 times a day, not 24**:

| | |
|---|---|
| Shortest gap | 0.9h |
| Median gap | 2.6h (6.2h over the last three days) |
| Longest gap | **13.3h** |

Only 8 of 28 gaps were under 90 minutes. Nothing downstream breaks — the panel
and the assistant both carry the timestamp they were built from and report their
own age — but anything that assumes "at most an hour old" is wrong, and
`LEDGER_MAX_STALE_HOURS` was tuned on that assumption until it was measured
(see below).

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
blanking the panel would destroy the record it exists to keep. The same rule
applies to partial losses — if a repo errored mid-walk *and* the ledger came
back shorter than the published one, the run declines to publish rather than
dropping that project's history from the panel.

**A GitHub outage is not a failure of this workflow.** Running on a short cron
against an API that serves the occasional 503 means the odd run simply can't read; on
2026-08-17 nine seconds of 503s on `/user` turned into a red workflow and a
failure email for a panel that was 45 minutes old and refreshed normally an hour
later. So transient failures (5xx, rate limiting, connection errors) are told
apart from credential failures and handled by waiting:

| Situation | Result |
|---|---|
| GitHub 5xx / unreachable | retried 3× with backoff (`OVERSEER_PREFLIGHT_ATTEMPTS`, `OVERSEER_PREFLIGHT_BACKOFF`) |
| Still unreachable, panel fresh | **skip, exit 0** with a `::warning` on the run — the next scheduled run retries |
| Still unreachable, panel older than `LEDGER_MAX_STALE_HOURS` (24h) | **fail** — the panel is going stale |
| Token expired / revoked (401) | **fail immediately**, never retried — a 401 will 401 again |

**Re-running a refresh that already succeeded will go red, and that is not a
bug.** A re-run replays the *original* checkout, so it rebuilds the ledger
against a commit that its own first attempt has already superseded, then races
to push over it. The publish step exhausts its three retries and exits 1 —
about 31 seconds of sleeps, which is the tell. Read attempt 1 before believing
attempt 2: if the first attempt was green, the work is published and there is
nothing to fix.

**`LEDGER_MAX_STALE_HOURS` is 24h, and it used to be 6h for the wrong reason.**
6 was chosen to mean "six consecutive missed refreshes", back when the cron was
believed to fire hourly. Against the measured cadence above it had quietly
become roughly *one* ordinary gap — so the skip path almost never applied: a
transient 503 found the published ledger already past the limit and hard-failed
the run, which is precisely the red workflow and failure email the 2026-08-17
incident added this guard to prevent. 24h restores the original intent: past the
worst gap GitHub has actually delivered (13.3h), still inside a day, so a
genuinely stuck refresh surfaces before the next weekly review reads from it.

## Implementing what it files

The pipeline proposed work every week and a human implemented it — or, going by
the ledger's own delivery rate, mostly didn't. The implementer closes that gap:
each Monday, an hour after the review, a few of the issues just filed are handed
to a coding agent that opens a pull request against them.

**It is not a fourth agent in the pipeline.** The three review agents are
`client.messages.create` tool-use loops with deliberately narrow tool lists —
none of them can edit a file, run a test, or push a branch, and the weekly Action
checks out the overseer, not the four projects. Making one of them write code
would mean rebuilding a coding agent inside `tools.py` and installing four
unrelated codebases on one runner. So the overseer keeps planning and *dispatches*
the implementing:

```
weekly review files issues
  └── implement.yml (Mon 15:00) — reads the ledger, applies the gate, picks ≤3
        └── repository_dispatch → each project's OWN repo
              └── implement.yml there — Claude Code branches, tests, opens a PR
                    └── ledger-refresh.yml sees the PR merge → Shipped panel
```

The last line is the point: **the summary already existed and was empty.** The
delivery ledger has always walked issue → linked PR → merged, so once something
implements the issues, the Shipped panel and the digest's `IMPLEMENTED` block
fill themselves in with no new bookkeeping.

### The gate is the design

"Implement everything filed" is the failure mode, not the goal. The Idea Agent
files at least three ideas a week by design, across four repos; auto-PRing all of
them produces a review queue bigger than the digest it replaced. And because
*shipped means merged*, unreviewed PRs park in `in_flight` forever — the delivery
rate would get **worse** while the spend climbed. So `tools.implementable` only
lets through:

| Rule | Why |
|---|---|
| Confirmed bugs, and enhancements the Idea Agent itself labelled `effort:low` | A bug is a defect with evidence attached — the easiest thing to hand an agent and the thing you most want fixed. Effort is the Idea Agent's own sizing, so the gate takes it at its word. `OVERSEER_IMPLEMENT_EFFORT=low,medium` widens it. |
| `status: open` only | `in_flight` means a PR already exists; `shipped` / `duplicate` / `not_planned` are finished. |
| ≤ `OVERSEER_IMPLEMENT_MAX` per run (default **3**) | Three is an evening's review, not a throughput target. Raise it once PRs are landing rather than piling up. |
| Round-robin across repos | The overseer files against itself more than anything else and would otherwise take every slot every week. |
| Never `overseer:no-implement` | Your opt-out. Label anything you want to decide yourself. |
| Never twice | A dispatched issue gets `overseer:implementing`; if that label fails to apply, the PR's own link to the issue moves it to `in_flight` and the gate skips it anyway. |
| Never a burned issue | A failed attempt swaps that label for `overseer:implement-failed` and says so on the issue. Both halves matter: without the swap the issue reads as in progress forever — filed and silently dropped — and without the exclusion Monday's run would retry an attempt that already spent its turn budget once. Remove the label to re-queue it. |
| Unless it wasn't the issue's fault | An attempt that died because the Anthropic key was out of credit is handed back **clean** and retried next run. Benching those would quietly retire every issue picked while the balance was dry — three a week, each needing a manual label removal to come back. |

Against today's ledger — 75 filed issues — that selects **3**: two confirmed bugs
and one `effort:low / impact:high` enhancement, one per repo. See for yourself,
without dispatching anything:

```bash
python scripts/dispatch_implement.py --dry-run --explain
```

`--explain` prints every filed issue the gate rejected and the reason, because
"why was my issue skipped?" is the first question anyone asks of a gate, and a
dispatcher that can't answer it gets switched off.

**A pull request is where this stops.** Nothing merges anything. The agent is
told to run the project's own suite before opening a PR, never to weaken a test
to get green, to stay inside the issue's scope, and — if the issue turns out to
be already fixed or simply wrong — to say so in a comment and stop rather than
invent work.

### Wiring it up

Each project repo gets a copy of `examples/implementer/implement.yml` (see that
directory's README) plus its own `ANTHROPIC_API_KEY`. The overseer's own copy is
`.github/workflows/implement-worker.yml`, already installed.

**What this costs you in credentials, stated plainly:** `OVERSEER_GITHUB_TOKEN`
now needs **Actions: write** alongside Issues: write on all four repos, so it can
fire the dispatch. That is the only new scope on the shared token — the ability
to *write code* stays in each repo with its own key and its own blast radius,
rather than becoming one cross-repo PAT with `contents: write` on everything.
That is the credential sprawl the July outage was made of, and this deliberately
avoids it. A dispatch rejected for missing scope is reported as a failed
hand-over and the issue is left unlabelled, so the next run retries it.

**One setting has to be on, per repo.** *Settings → Actions → General → Workflow
permissions → "Allow GitHub Actions to create and approve pull requests"*. It is
off by default, and it fails late and quietly: the run does all the work, pushes
the branch, and only then gets
`GitHub Actions is not permitted to create or approve pull requests` from
`gh pr create` — after which the job still reports **success** with no PR to show
for it. The first green run on this repo ended exactly there, with the finished
work sitting on `overseer/issue-26`. The agent is told to comment on the issue
when it can't open a PR, so the work isn't lost, but flip the setting first in
every repo you install the implementer into.

Two more things worth knowing before you turn the schedule on:

- **Your PR checks won't run on these PRs.** A pull request opened with the
  built-in `GITHUB_TOKEN` doesn't trigger further workflows — GitHub's loop
  guard, not a bug here. It's why the agent runs the suite itself first. Pass a
  PAT as the action's `github_token` if you want normal CI on them too.
- **The implementer is the expensive half, by a wide margin.** Measured on this
  repo: the whole three-agent review costs **$0.34**, and one successful
  implementation costs **$1.49** (54 turns). A week at the default cap is
  therefore ~$4.80 — and the model tiering the section above is proud of saves
  $0.10 of it. If you want to control spend, the cap and the tier are the only
  levers that matter now.

### Choosing the tier

Every attempt runs on a **tier**, not a model name you type at the point of use:

| Tier | Model | Set by |
| --- | --- | --- |
| `light` (default) | `claude-sonnet-5` | `OVERSEER_IMPLEMENT_MODEL` |
| `heavy` | `claude-opus-4-8` | `OVERSEER_IMPLEMENT_HEAVY_MODEL` |

Light is the measured working default — the first successful implementation ran
on it and produced a real change to a 70KB module with tests. Reach for heavy on
an issue you already know is hard, and expect several times the cost.

You are asked which one **before anything runs**, on both manual entry points:
*Actions → Implement a filed issue → Run workflow* has a **Model tier** dropdown,
and so does *Hand filed issues to the implementer* (where it applies to every
attempt that run). A **scheduled** Monday run has nobody to ask, so it uses
`OVERSEER_IMPLEMENT_TIER` (default `light`).

The dashboard carries the same picture. An **Implementer** panel sits under
Shipped and answers the three questions the ledger can't: what the next run will
attempt (and what it will cost), what is under way right now, and what is
stalled waiting on you. Its figures come from a `queue` block the ledger
publishes (`tools.queue_state`) rather than from rules re-implemented in
`app.js` — a second copy of the gate there would drift from the dispatcher's the
first time the rules changed, and describe a queue that never runs. It rides
along with the ledger refresh, so the panel is live without a second workflow or
a single model call.

The dispatcher also prints what the run is about to cost before it fires:

```
[implement] tier: light
[implement] estimate: 3 attempt(s) x ~$1.50 = ~$4.50 at the light tier
```

That figure is `OVERSEER_IMPLEMENT_COST_HINT` — the measured mean of a successful
light-tier attempt here — so retune it once your own runs have a track record.

**The tier is a name, never a model id, and that is a security property rather
than a style choice.** It travels in a `repository_dispatch` payload, which
arrives over the API, and the workflow interpolates the resolved model into the
Claude CLI's arguments. If the payload could carry the model string, anyone able
to fire a dispatch at your repo could choose what those arguments say. So
`resolve_tier` refuses anything outside `light`/`heavy`, the workflow refuses it
again, and the model ids themselves come from repo variables that only a writer
can set.
- **`--max-turns` is a real limit, and hitting it costs you the whole attempt.**
  The first run tried this repo on 40 turns, died at 41 having pushed nothing,
  and still billed $1.04 — a coding session that reads an issue, explores a
  codebase, edits, tests and opens a PR does not fit in 40. It is 150 now, which
  raises the ceiling on what one attempt can cost as well as what it can finish.
  Watch the first few runs' reports before raising it further.

The first run should be a manual one: **Actions → Hand filed issues to the
implementer → Run workflow** defaults to dry-run, so you see the queue before
anything fires.

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
| GitHub itself unreachable (5xx / network) | retried with backoff, then `status: "unavailable"` with `transient: True` — still fatal for the weekly review, but reported as an outage rather than a bad token |

**1b. Retrying a review that hit an outage.** Aborting is right, but a *weekly*
job that aborts has to wait a week — on 2026-08-17 the review died at 14:15 on
nine seconds of 503s, so there was no digest and no push notification, and the
next scheduled attempt was seven days out. The orchestrator now exits **75**
(`EX_TEMPFAIL`) for a transient preflight, and only for that, which the workflow
keys on in two layers:

| Layer | Covers | Cost when nothing is wrong |
|---|---|---|
| In-job retry — 3 attempts, 2 then 4 minutes apart | an outage lasting minutes | nothing; the preflight runs before the first agent, so a deferred attempt spends no budget |
| Catch-up crons at 16:00 and 18:00 UTC Monday | an outage lasting hours | a checkout and a file read — `scripts/weekly_guard.py` skips them once today's digest is published |

Any other exit code fails on the first attempt, because retrying a dead
credential only delays the diagnosis. A `weekly-review` concurrency group keeps
a catch-up from joining a run that is still retrying.

Both workflows also retry a *rejected push*. They commit to `main` from a
checkout they may hold for minutes, and the ledger refresh writes on its cron
and on every PR close, so either can find the remote has moved. For the review
that costs more than the file: `Send push notification` is the step after the
publish, so a lost race would drop the notification too. Collisions resolve in
favour of the run doing the pushing — these files are regenerated whole from a
fresh GitHub read, so the newer complete file beats a merge of two.

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
| 2 | **GitHub token** (PAT) | github.com → Settings → Developer settings → Fine-grained tokens. Give it your 3 project repos with **Issues: Read and write** — plus **Actions: Read and write** if you want the implementer to hand issues over. |
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
digest stayed quiet).

**An SLA is derived from how often the project actually publishes, never picked
by feel:**

```
sla_hours = publish_interval_hours + SLA_GRACE_HOURS   # grace defaults to 24h
```

One full missed publish cycle, plus a day of slack because GitHub's scheduled
crons are best-effort and routinely drift. A daily publisher lands on 48h and a
weekly one on 192h — which is where both of this repo's existing conventions
already sat, so the rule formalises them rather than replacing them.

| Project | Publishes | SLA | Set by |
| --- | --- | --- | --- |
| Crypto trading bot | ~daily — bot ticks hourly, but `run-bot.yml` gates the status publish behind `MIN_AGE_HOURS=20` | 48h | the *publish* cadence, not the tick cadence |
| UFC dashboard | daily 09:00 UTC, plus every 4h Thu–Sat and every 5 min on fight nights | 48h | the **slowest guaranteed** cadence — sizing it on fight-night frequency would page you every ordinary Tuesday |
| coachvision | weekly, Mondays 06:00 UTC (`overseer-status.yml`) | 192h | publishes whether or not footage arrived, so the overseer can tell idle from broken |
| Project Overseer | weekly, Mondays 14:00 UTC | 192h | the same rule applied to itself (`SCHEDULE_STALE_HOURS`) |

coachvision is why this is a rule and not a table of numbers. It was graded
against the shared 48h default, so from every Wednesday onward it read STALE for
behaving exactly as designed — five consecutive runs raising a correct-looking
alarm about a healthy project. **An alert that fires on normal operation is worse
than no alert**, because it teaches you to skim past the panel where the real one
will appear.

Override per project with `<PROJECT>_PUBLISH_INTERVAL_HOURS` (state the cadence,
let the rule derive the deadline — preferred, since it keeps the two consistent)
or `<PROJECT>_SLA_HOURS` (set the deadline outright, for a feed the rule doesn't
fit). `SLA_GRACE_HOURS` retunes the slack for everything at once, and
`FRESHNESS_SLA_HOURS` is the fallback for a project whose cadence nobody has
recorded yet.
When any feed is past-due, a machine-generated **`STALENESS ALERTS`** block —
listing each feed with how far past its SLA it is (`data 153h old, SLA 48h`) — is
prepended to the top of the digest **before it's sent**, so it leads both the
Telegram message and the dashboard, independent of what the review agents wrote. A
halted feed can no longer hide behind a quiet summary.

The **foot** of the digest is machine-generated for the same reason: an
`IMPLEMENTED (LAST 7 DAYS)` block listing what the implementer actually merged,
plus what is still sitting in review, read straight off the delivery ledger. The
Reviewer is never asked to write that section — a "what shipped" summary that
depends on an agent remembering to include it is one that will eventually go
quiet without anything failing.

Right below it, an `AGING BACKLOG (OPEN OVER 60 DAYS)` block lists open
enhancement ideas that have sat untriaged past that threshold, straight off the
same ledger's `created_at` — so idea rot is visible alongside pipeline
staleness instead of only showing up as a growing pile of "STILL OPEN" lines in
the known-work block agents see.

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

## Asking it questions out loud

The overseer reviews four projects, files issues and ships a few of them — and
until now the only way to find out what it was thinking was to read the digest.
This lets you ask instead, from a phone, hands-free:

> *"Hey Siri, ask Overseer."*
> *"Why hasn't the coachvision upload thing been built yet?"*
> *"That's a medium-effort enhancement, and the gate only picks up low-effort
> ones. Two bugs are queued ahead of it."*

Speech-to-text and text-to-speech both happen **on the phone**, for free — the
only thing that costs money is one model call, and the whole exchange runs at
well under a cent.

### How it fits together

```
scripts/build_ask_context.py     docs/ask-context.json     worker/overseer-ask.js
  reads digest + history +   →     the facts, the       →    fetch, concatenate,
  ledger, applies the gate         prompt, the format         one API call
                                        ↑                          ↑
                            published on GitHub Pages    Siri Shortcut asks it
```

**The Worker is deliberately stupid.** It fetches the pack, joins four strings,
makes one call, and returns plain text. It does not know what the gate is, why
an issue is queued, or what any field means. That is invariant 4 again: the
dashboard once re-derived the gate's rules in `app.js` and confidently described
a queue the dispatcher would never produce, and a Worker is the same hazard one
platform further away — deployed separately, and out of sight when the Python
changes. Everything that could drift lives in the pack, and `tests/test_ask.py`
greps the Worker to keep it that way.

That is also what makes the assistant testable, which a thing that answers from
live model calls would not be. The tests pin that every "why wasn't this done?"
answer is **quoted from `tools.implementable`**, never paraphrased — because
nobody cross-checks a sentence they heard in the car against `docs/shipped.json`.

### Try it from the terminal first

```bash
python ask.py "what is queued for the next implementation run?"
python ask.py --format voice "what did we ship this month?"
python ask.py --dry-run          # print the assembled prompt, call nothing
```

It prints what the question cost underneath the answer, the same way the
pipeline reports its own spend.

### Deploying the Worker

```bash
cd worker
npx wrangler secret put ANTHROPIC_API_KEY   # the same key the pipeline uses
npx wrangler secret put ASK_SHARED_SECRET   # any long random string
npx wrangler deploy
```

Set `PACK_URL` in `wrangler.toml` to your Pages URL for `ask-context.json`.
Cloudflare's free tier covers this comfortably — 100k requests a day against a
thing you will use a dozen times a week.

`ASK_SHARED_SECRET` is checked before anything else happens, and specifically
before the API key is touched. An endpoint that answers to anybody is somebody
else spending your key.

### Three things that will bite you deploying this

All three cost real time the first time through, and none of them announce
themselves.

**`wrangler secret put` takes the NAME as its argument, not the value.** Type
`npx wrangler secret put ANTHROPIC_API_KEY` and paste the key at the prompt it
gives you. Pasting the key onto the command line instead creates a secret
*named* after your key — wrangler cheerfully reports `✨ Success! Uploaded secret
sk-ant-...`, echoing your credential to the terminal, and `ANTHROPIC_API_KEY` is
still unset. The Worker then fails with "Something went wrong reaching the
model", which points nowhere near the actual mistake. `npx wrangler secret list`
prints the names and settles it in one line. This is also how a credential ends
up pasted into a chat window; if that happens, revoke it rather than reasoning
about whether it was exposed.

**macOS is zsh, and `read -s -p` is bash.** In zsh `-p` means "read from a
coprocess", so the whole command fails, the variable is empty, and a curl built
around it sends an empty header — which the API reports as a missing header, not
a bad key. Use `printf` for the prompt and a bare `read -s VAR`.

**Nothing is deployed until `wrangler deploy` prints a URL.** Before that, any
`*.workers.dev` address returns Cloudflare's "nothing is here" page, including
the placeholder one in this README. That page is not a symptom of anything.

### The Siri Shortcut

In the Shortcuts app, new shortcut, four actions:

1. **Dictate Text** — this is the free, on-device speech-to-text.
2. **Get Contents of URL** — your Worker URL, method `POST`, headers
   `Authorization: Bearer <your ASK_SHARED_SECRET>` and
   `Content-Type: application/json`, request body JSON:
   `{"q": <Dictated Text>, "format": "voice"}`
3. **Speak Text** — the contents of the previous step.
4. Name it **"Ask Overseer"**, and turn on *Show in Siri*.

Then *"Hey Siri, Ask Overseer"* works from a locked screen, from AirPods, and
from CarPlay.

### What it costs, measured

| | |
|---|---|
| The prompt (pack + rules) | ~3.5k tokens, cached |
| One question, cold cache | **~$0.008** |
| One question, warm cache | **~$0.002** |
| Speech-to-text, text-to-speech | **$0.00** — both on the phone |
| Cloudflare Worker | **$0.00** — free tier |

For scale: one question is about **2% of a review** and **0.5% of a single
implementation**. Asking is never the thing to hesitate over.

Two decisions keep it there, and both are load-bearing:

- **One call, no tool loop.** The three pipeline agents each run a loop of up to
  25 iterations, which is why a review costs $0.34 and an implementation $1.49.
  A question does none of that, because `build_ask_context.py` already did the
  looking. The temptation, the first time it answers *"the snapshot doesn't
  cover that"*, is to hand it the read tools — that is a 40× change, and the
  cheaper fix is to publish the missing facts into the pack on a schedule.
- **The clock lives in the question, not the prompt.** Anything volatile inside
  the cached prefix invalidates it, so a timestamp one line higher would quietly
  pay full price for 3.5k tokens on every question with nothing visibly wrong.
  It is also why the pack carries absolute timestamps instead of pre-computed
  ages: ages baked in at build time read as fresh forever, so a pack built on
  Monday would still say "two hours old" on Thursday — out loud, confidently.

### What it can and cannot do

It answers from a snapshot rebuilt behind the ledger refresh — scheduled hourly
but in practice landing every few hours (see *Closing the loop* above) — so it
knows
the digest, the delivery record, the queue, the backlog with the gate's verdict
on each item, and what the last few runs cost. It has no tools, so it cannot go
and look at anything, and it will tell you so rather than guess.

It also cannot *do* anything — no dispatching an implementation, no kicking off
a review. That is invariant 6 holding for voice as well: a pull request is where
the automation stops, and a spoken command is a poor place to start spending
$1.49 a go.

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

- `orchestrator.py` — runs the three agents sequentially; `--dry-run` flag
- `agent_bug_hunter.py` / `agent_idea.py` / `agent_reviewer.py` — the three agents
- `tools.py` — shared tool implementations, schemas, config, and the agent runtime
- `tracer.py` — live console trace, HTML report, `docs/digest.json` writer, and
  the append-only `docs/history.json` trend log
- `docs/` — the installable web app (GitHub Pages): `index.html`, `app.js`,
  `sw.js` (service worker / push handler), `manifest.webmanifest`, icons
- `ask.py` — ask the overseer one question; one model call, no tool loop
- `ask_context.py` — builds the context pack **and owns the assistant's prompt**,
  so the Worker carries neither
- `scripts/build_ask_context.py` — publishes `docs/ask-context.json`; skips the
  write when only the build stamp moved, so a rebuild that changed nothing is
  silent
- `worker/overseer-ask.js` — the Cloudflare Worker the Siri Shortcut talks to
- `scripts/notify_push.py` — sends the weekly push (run by the Action)
- `scripts/heartbeat.py` — dead-man's switch: alerts if the weekly run stops
  happening, or completes while blind (stdlib only, no token)
- `scripts/refresh_ledger.py` — incremental ledger refresh between weekly runs
- `scripts/weekly_guard.py` — lets the Monday catch-up runs no-op once the
  review has published, so retrying a missed week costs nothing in a healthy one
- `scripts/delete-merged-branches.sh` — cleanup of branches already merged into
  `main` across all four repos; dry-run by default, `--go` to apply. Runnable
  from a phone via the **Clean up merged branches** Action (Actions tab → Run
  workflow → mode). A delete run needs `OVERSEER_GITHUB_TOKEN` to carry
  **Contents: write** on all four repos; a dry run needs no token at all.
- `.github/workflows/weekly-review.yml` — cron, digest commit, push, report artifact
- `.github/workflows/heartbeat.yml` — daily heartbeat, independent of the above
- `.github/workflows/cleanup-branches.yml` — manual branch cleanup (never
  scheduled); writes the result to the run's job summary so it's readable on a
  phone without opening the log
