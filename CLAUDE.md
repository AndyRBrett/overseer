# Project Overseer — working notes for Claude / contributors

A weekly review pipeline that **reviews four projects, files issues, and then
implements a few of them**. Four stages, three of them agents:

```
Mon 14:00 UTC  weekly-review.yml    Bug-Hunter → Idea-Agent → Reviewer
                                    → issues filed → digest → Telegram + PWA
                                    (+14:05 Cloudflare cron → repository_dispatch,
                                     because GitHub drops scheduled events)
Mon 15:00 UTC  implement.yml        ledger → gate → ≤3 picks → repository_dispatch
                                    (+15:20/17:05 Cloudflare, same reason)
      ~10 min  implement-worker.yml the coding agent, IN EACH TARGET REPO
                                    → branch → tests → pull request
      ~6×/day  ledger-refresh.yml   PR merged → docs/shipped.json → dashboard
                                    → docs/ask-context.json → voice assistant
      ~6×/day  refresh_status.py    same job: re-reads the four feeds → digest.json's
                                    health + ranking (NEVER its `generated`)
  daily 15:20  heartbeat.yml        dead-man's switch, also Cloudflare-triggered
 on push main  deploy-worker.yml    worker/** → wrangler deploy, then assert
                                    the installed schedule matches the file
    on demand  worker/              "Hey Siri, ask Overseer" → one model call
                                    (+ the crons above, off GitHub's scheduler)
```

The projects reviewed are `crypto-trading`, `coachvision`, `ufc-dashboard`, and
**this repo itself** — held to the same bar as the others.

## ⛔ Run `python -m pytest -q` before you commit

Test deps are `pytest` and `pyyaml` (CI installs both alongside
`requirements.txt`; neither is a runtime dependency).

530 tests, under a second. There is no JS test runner, so dashboard behaviour is
pinned from Python instead (see *Testing what has no test runner* below).

`python scripts/pipeline_dryrun.py` runs the WHOLE pipeline end to end against
fixtures — real orchestrator, real tools, real tracer, with GitHub and Anthropic
replaced. No key, no network, no spend, ~1s. CI runs it as its own job on every
PR because the seams between tested components are where the weekly review has
actually failed.

## Where things live

| File | |
|---|---|
| `orchestrator.py` | runs the three agents in sequence; preflight, ledger, telemetry |
| `agent_bug_hunter.py` / `agent_idea.py` / `agent_reviewer.py` | one prompt + tool list each |
| `tools.py` | **everything shared** — tool implementations, the delivery ledger, the implementation gate, proposal outcomes, `run_agent` |
| `attention.py` | the per-project attention score — pure, imports nothing of ours |
| `dedupe.py` | TF-IDF duplicate detection over the filed backlog — pure, stdlib |
| `scripts/pipeline_dryrun.py` | the whole pipeline against fixtures; CI's `e2e` job |
| `tracer.py` | per-run recording, spend accounting, digest/history writers |
| `scripts/dispatch_implement.py` | picks issues and hands them to the implementer |
| `scripts/refresh_ledger.py` | cron every ~2–6h (see below), pure GitHub reads, no model calls |
| `scripts/refresh_status.py` | same cron: the digest's health + ranking between weekly reviews |
| `scripts/implement_guard.py` | keeps implement.yml's catch-up crons and its Cloudflare poke from dispatching a second batch |
| `scripts/heartbeat.py` | daily; stdlib-only and tokenless **by design** |
| `scripts/verify_worker_triggers.py` | reads the schedule `wrangler deploy` says it installed back against `wrangler.toml`; run by `deploy-worker.yml` |
| `docs/` | the PWA dashboard (`index.html` + `app.js`), fed by `digest.json`, `history.json`, `shipped.json` |
| `ask.py` / `ask_context.py` | the voice assistant: one call, no tool loop; `ask_context` owns its prompt AND its facts |
| `worker/` | the Cloudflare Worker Siri talks to — deliberately knows nothing |
| `examples/implementer/` | the drop-in workflow the three project repos run |

## Invariants — break these and the system lies to you

Each of these exists because the opposite already happened here.

1. **Shipped means merged.** A closed issue whose fix sits on an unreviewed
   branch is `in_flight`. Loosening this makes the delivery panel flatter itself.
2. **Dispatch fires BEFORE the issue is labelled**, never after. A failed
   hand-over must leave the issue unlabelled so the next run retries it;
   labelling first silently retires a filed bug.
3. **The implementer lives in exactly one place** —
   `.github/workflows/implementer.yml`, a reusable workflow every repo calls.
   It was copied into five files instead; a two-line fix then meant five edits
   across four repos, and a security sweep hardened three copies and missed two.
   Each project repo keeps only its toolchain and test command. Tests fail if a
   second copy of the prompt or the author guard appears.
4. **The gate lives in exactly one place** — `tools.implementable` /
   `implementation_queue`. The dashboard renders the `queue` block that
   `tools.queue_state` publishes; it must never re-derive the rules in
   `app.js`, or the panel will confidently describe a queue that never runs.
5. **The model tier is a NAME (`light`/`heavy`), never a model id.** It rides in
   a `repository_dispatch` payload — API input — and becomes the `--model`
   argument the coding agent runs on. `resolve_tier` refuses unknown values and
   the workflow refuses them again. A test pushes
   `"opus --dangerously-skip-permissions"` through the dispatch path.
6. **A pull request is where the automation stops.** Nothing merges itself.
6b. **Every automated trigger asks its guard; only `workflow_dispatch`
   doesn't.** This is `weekly_guard` for the review and `implement_guard` for
   the dispatcher — the latter only since 2026-09-07, when it asked the question
   of the catch-up crons but not the primary, GitHub delivered the primary 3h55m
   late after a manual run, and the day cost ~$9 instead of $4.50. Two schedulers now aim at the same Monday (GitHub's crons and the
   Cloudflare `repository_dispatch`), so "am I the review?" is the wrong
   question and "has today's review already landed?" is the right one. The
   14:00 cron used to run unguarded; against a second trigger that is a
   duplicate $0.34 pipeline whenever GitHub delivers late, which is routine.
7. **Deterministic digest sections stay deterministic.** The staleness banner,
   `ATTENTION RANKING`, `IMPLEMENTED`, and `AGING BACKLOG` are computed in
   Python and stitched into the digest in `run_agent`, because a section that depends on an agent
   remembering to write it eventually goes quiet with nothing failing.
8. **The voice assistant's prompt and facts live in `ask_context.py`.** The
   Worker fetches `docs/ask-context.json` and concatenates strings; it holds no
   prompt text and no rules. This is invariant 4 one platform further away —
   a second copy of the gate in JavaScript would be deployed separately, out of
   sight when the Python changed, and answering out loud where nobody
   cross-checks it. Tests grep the Worker for prompt sentences and rule strings.
9. **Nothing in the pack depends on the current time.** The clock rides in the
   user turn. Two reasons, both already nearly built wrong here: a timestamp
   inside the cached prefix invalidates it and silently pays full price for 3.5k
   tokens per question; and ages computed at build time read as fresh forever,
   so a pack built Monday still says "two hours old" on Thursday.
10. **Failure taxonomy is load-bearing.** An attempt that died on an exhausted
   API key is handed back *clean* and retried; one that ran out of turns or
   couldn't get tests green is benched with `overseer:implement-failed`. A dry
   balance otherwise retires every issue picked that week, three at a time.
11. **A gate the model can decline to run is not a gate.** `check_duplicate` is
   in the Idea Agent's tool list AND `propose_enhancement` refuses a near-exact
   re-filing on its own, because the ALREADY ON RECORD list was already in the
   prompt for most of the twelve issues that got closed as duplicates. Both
   halves, or neither is worth having. `extends=<issue>` is the only override,
   and it is recorded on the issue rather than argued in a rationale.
12. **The attention score is computed once, in Python.** `attention.rank` orders
   the dashboard's project panel, the digest's ATTENTION RANKING block and the
   voice pack. This is invariant 4's rule applied to a second set of rules: a
   scoring formula re-derived in `app.js` would drift, and the three surfaces
   would then give three answers to "what should I work on".
13. **`digest.json`'s `generated` is the REVIEW's timestamp, and nothing else
   may set it.** `scripts/heartbeat.py`, the Worker's staleness check and the
   dashboard's "next run overdue" all read it as "when the weekly review last
   ran". `refresh_status.py` rewrites the same file six times a day and writes
   `refreshed` instead, because bumping `generated` would tell the dead-man's
   switch the review had happened — disabling the one alarm that catches a
   scheduled event GitHub silently dropped, invisibly, on a 4-hourly cron.
14. **A refresh is not a cycle.** `blind_cycles` / `stale_cycles` / `idle_cycles`
   count consecutive weekly REVIEWS, which is what the nudge threshold of 2 was
   chosen against. `RunTracer(count_cycles=False)` is how an observation reads
   the counter without advancing it; counting the status refresh would have a
   feed read "stale 42 cycles" by Friday.
15. **A count of tool calls is not a count of work.** `propose_enhancement`
   returns `duplicate` when the dedupe gate refuses a filing; the tracer reads the
   result's `status` and counts it under `duplicates_blocked`, not under
   `enhancements`. Counting the call would inflate the dashboard's "ideas
   proposed" tile with work that never reached GitHub — the same shape of lie
   `output_alerts` exists to catch.

## Gotchas that have already cost money or a week

- **`--max-turns 40` was not enough.** A real implementation of a small feature
  in a 70KB module took **54 turns**. It is 150 now. A run that exhausts turns
  pushes nothing and still bills ~$1.
- **Out of credit looks like success.** The action reports
  `subtype: "success", is_error: true` and the log says *"Credit balance is too
  low"*. It is not a max-turns failure; do not "fix" the turn budget in response.
- **`display_report: true` is why failures are diagnosable.** The SDK output is
  hidden by default, so without it a failure is one line of error text.
- **GitHub Actions cannot open PRs unless the repo setting allows it.**
  *Settings → Actions → General → Workflow permissions*. It fails at the very
  end, after all the work and spend, and the job still reports **success**.
- **PRs opened with `GITHUB_TOKEN` do not trigger the repo's other workflows.**
  That is why the agent is told to run the suite itself before opening one —
  which is the agent grading its own homework, and on 2026-09-07 that showed.
  Two implementer PRs (#78, #82) reached a human with **no CI and no Codex
  pass**: both claimed a green suite in their own descriptions, and both carried
  a P1 the moment anyone looked. One was a cost-tracking widget blind to
  duplicate-run spend — the exact thing that had cost $9 hours earlier. The
  change with the least human authorship was getting the least scrutiny, which
  is backwards. `implementer.yml` now mints a **GitHub App installation token**
  per run (`pr_app_id` + `pr_app_private_key`), falling back to a PAT
  (`pr_token`) and then to the built-in token — and **says so as a run warning**
  when it falls back, because a silent fallback is one nobody ever notices.
  Two traps, both found by Codex on the PR that fixed the first problem: an App
  token **cannot be a stored secret** (it dies in an hour, and an expired secret
  is still non-empty, so the `||` fallback never engages and every later run
  fails auth), and the token needs **`issues:write` as well** — it is the
  credential for every `gh` command the agent runs, and the prompt opens with
  `gh issue view` and closes obsolete issues with `gh issue close`. Contents +
  Pull requests looks like least privilege and breaks the close path, which
  quietly re-buys the same investigation every week.
- **Labels are never cleaned off a closed issue.** Anything keying on
  `overseer:implement-failed` must also check the entry is still open, or
  settled work reports as needing attention forever.
- **Siri decides whether to speak, and defaults to not.** The Shortcut speaks
  correctly when run from the Shortcuts app and silently prints when Siri runs
  it, so every hypothesis points at the Shortcut or the Worker. Neither is
  involved: *Settings → Siri & Search → Siri Responses* is *Automatic* until
  set to *Prefer Spoken Responses*. The last mile of a voice feature was a
  toggle three apps away from the code.
- **Speech is not an API input.** Claude takes text, images and PDFs, not audio.
  Voice works here only because the iPhone does speech-to-text and text-to-speech
  on-device for free; any server-side transcription would add a provider, a key
  and a per-minute bill to something that currently costs nothing.
- **The dashboard had two clocks and only one was visible.** `shipped.json`
  refreshed ~6×/day and `digest.json` weekly, so on 2026-09-05 the delivery
  panel was 0.3h old and the project health directly above it was 117.8h — and
  the stale half is the half that says whether anything is BROKEN. Nothing about
  it needed a model: health, freshness alerts and the attention ranking are four
  GitHub reads and arithmetic, welded to a $0.34 run only because that is where
  the tracer wrote the file. The page now prints both ages ("Checked just now ·
  full review 5 days ago"); if you ever find yourself adding a third writer to
  `digest.json`, it needs a third one.
- **The hourly cron is not delivered hourly.** GitHub deprioritises scheduled
  workflows on free public repos. Measured over 29 consecutive `ledger-refresh`
  runs (2026-08-25 → 08-30): **~6 firings a day, not 24** — median gap 2.6h
  (6.2h over the last three days), worst **13.3h**, only 8 of 28 gaps under 90
  minutes. Two consequences. `LEDGER_MAX_STALE_HOURS` was 6, chosen to mean "six
  consecutive missed refreshes"; against the real cadence that was roughly *one*
  ordinary gap, so the transient-failure skip path stopped applying and a 503
  hard-failed the run instead — the exact red workflow the guard was added to
  prevent. **It is 24 now.** And nothing built on this cron may claim to be at
  most an hour old.
- **It skips the weekly crons too, and `implement.yml` had no catch-up.** On
  2026-08-31 the 15:00 dispatch did not fire at its hour at all; it landed at
  20:30, five and a half hours late, and was inside Monday only by luck. A slip
  past midnight would have skipped the week's implementation stage with nothing
  red and nothing to say so — `weekly-review.yml` has had catch-ups at 16:00 and
  18:00 for exactly this reason. `implement.yml` now has them at 17:00 and 19:00,
  each an hour behind a review cron. **They must stay guarded.** The dispatcher
  labels what it hands over, so an ungated catch-up does not re-pick the same
  three issues — it picks three *different* ones, turning a $4.50 Monday into
  $9.00. `scripts/implement_guard.py` gates them on the workflow's own run
  history; a test asserts the crons and `CATCHUP_SCHEDULES` stay in step. Note
  what these catch-ups do NOT cover: a *dropped* event (the bullet below). They
  are `schedule:` entries queued through the same scheduler, so they help when a
  cron is late and not at all when it never fires.
- **A cron can be dropped entirely, and that looks like nothing at all.** On
  2026-08-31 GitHub delivered *none* of the weekly review's three scheduled
  events — 14:00, 16:00 and 18:00 with no run created, no failure, no queued
  job, while push- and PR-triggered runs in the same repo fired normally. A
  dropped event is not a red run you can find in the Actions tab; it is an
  absent row, indistinguishable from a quiet week, and it was caught by a human
  noticing a seven-day-old timestamp. Every alarm here is downstream of a job
  starting, so **none of them fire** — the 08-17 retry loop and catch-up crons
  included, since those live inside a job that has to start, and the catch-ups
  are `schedule:` entries queued through the very scheduler that dropped the
  primary. The heartbeat is on the same cron and was dropped too. Redundancy for
  a cron has to come from a different vendor: `worker/overseer-ask.js` now runs
  **Cloudflare** crons firing `repository_dispatch` at 14:05/17:05 Monday for
  the review and 15:20 daily for the heartbeat. The heartbeat especially — a
  dead-man's switch on the scheduler it watches shares a failure mode with it,
  which is the one thing a dead-man's switch may not do; off GitHub it is also
  what makes a dropped event *detectable*, since a review that never runs leaves
  `docs/digest.json` standing still and the heartbeat trips on that within a day.
  When diagnosing "the run didn't happen", check `total_count` on the workflow
  before reading logs — no new run number means there was never a job.
- **The dispatcher was the last stage on one scheduler, and 09-07 collected.**
  On 2026-09-07 GitHub again created *no run at all* for anything scheduled in
  this repo. The review never noticed — it came in on Cloudflare's 14:05 poke,
  and the 17:05 one no-opped against the guard exactly as designed. `implement.yml`
  had no such backup: its 15:00 cron and its 17:00 catch-up both produced no run
  (`total_count` stayed at 5), and the week's implementation stage was skipped
  until a human fired `workflow_dispatch` by hand at 17:32. The catch-ups added
  on 08-31 could not help — they are `schedule:` entries queued through the
  scheduler that dropped the primary, which is the same thing the review's
  16:00/18:00 catch-ups could not do on 08-31. **The fix added no new cron.**
  Cloudflare's docs contradict themselves on the free-plan cap (the Cron Triggers
  page says per Worker, the Limits page says 5 per *account*, third-party
  references say 3 per Worker) and three were already declared, so `implement`
  rides the existing 15:20 daily and 17:05 Monday crons as a second event on
  each — both still landing after GitHub's own 15:00 and 17:00. `DISPATCH_EVENTS`
  maps a cron to a LIST for this reason, and `MONDAY_EVENTS` is what keeps the
  daily cron from asking for a $4.50 implementation run every morning.
- **"Which trigger am I?" is the wrong question, and it cost $9.** `implement_guard`
  asked whether today's dispatch had landed only of the crons it had listed as
  catch-ups; `0 15 * * 1` was exempt because it *is* the dispatch. On 2026-09-07
  GitHub delivered that primary cron at **18:55Z, 3h55m late**, after a manual
  run at 17:32 had already handed over the day's three. The guard logged *"not a
  catch-up run — this is the dispatch itself"* and dispatched three more: six
  attempts, ~$9, on a Monday designed to cost $4.50 — the exact doubling the
  module's own docstring exists to prevent, reached without any second scheduler
  being involved. Note the test that should have caught it *passed*: it asserted
  `CATCHUP_SCHEDULES` matched the workflow's crons, and it did — the list was
  complete and its premise was wrong. The guard now asks EVERY automated trigger
  and holds no list of crons at all, which also makes the check a hard per-day
  cap rather than a rule that must classify a trigger correctly first. The
  measured cost of one attempt that day was **$1.77**, not $1.50.
- **The dispatcher must not run before the review it reads.** Two schedulers now
  aim at the same Monday, so `implement` can be poked while the weekly review is
  still filing this week's issues — and a dispatcher that fires early reads last
  week's ledger and spends the week's budget on a stale queue. The guard checks
  `docs/digest.json`'s `generated` for today's date (invariant 13 is what keeps
  that field meaning "the review ran"; `refreshed` moves six times a day and
  would answer a different question). The cost of the check is a week with no
  review is also a week with no implementation — deliberate, since there is
  nothing new to implement, but it is a second way for this stage to go quiet.
  It is a skipped week of delivery, never a skipped alarm: the heartbeat still
  trips on the standing-still digest within a day.
- **A dry run is not a dispatch, and the guard could not tell.**
  `implement_guard.dispatched_today()` counted any green run today, and the API
  does not expose a run's `workflow_dispatch` inputs — so a `--dry-run` run, which
  hands nothing over and is the DEFAULT for a manual dispatch, read as "today's
  dispatch already ran" and silently disarmed every remaining catch-up. Looking
  before firing was the move that would have thrown the week away; on 09-07 it
  would have eaten the last chance left. `implement.yml` now marks its own
  `run-name` with `(dry run)` and the guard reads the marker back. That is the
  guard's stated principle — *when in doubt it RUNS* — restored: a dry run made it
  confident, and confidently wrong in the direction that costs the week.

- **Two schedulers, two definitions of day 1.** The Cloudflare crons that back
  up the weekly review were written by mirroring `weekly-review.yml`'s
  `0 14 * * 1` field for field. Every field survives that copy except the last:
  GitHub runs POSIX cron (0-6, **Sunday is 0**, so 1 is Monday) and Cloudflare
  counts the week from 1 (**Sunday is 1**, so Monday is 2). So `5 14 * * 1`
  fired on **Sunday 2026-09-06** — a day the guard correctly saw no digest for,
  which is why it ran a full $0.43 review, filed five enhancements and pushed a
  digest a day early, with Monday's GitHub cron still owing a second one. Two
  reviews a week and nothing red. The daily heartbeat has no day field, which is
  why the first Sunday after deployment was the first symptom. `wrangler.toml`
  now reads `* * 2` on purpose, `overseer-ask.js` checks `getUTCDay()` as well
  so the next drift costs a skipped poke rather than a review, and the test that
  used to assert the day field was `"1"` — agreeing with the bug because it
  shared its premise — now pins the two conventions facing each other.
- **`wrangler secret put` takes the NAME, not the value.** Pasting the key onto
  the command line creates a secret *named* after your credential, echoes it to
  the terminal, and leaves the real slot unset — surfacing much later as an
  unrelated "went wrong reaching the model". It is how an API key or a PAT ends
  up in a chat window. `npx wrangler secret list` settles it in one line; a
  credential that has been echoed anywhere gets revoked, not reasoned about.
- **macOS is zsh; `read -s -p` is bash.** In zsh `-p` reads from a coprocess, so
  the read fails, the variable is empty, and the request goes out with an empty
  header — reported by the API as a *missing* header, which reads like a
  different bug entirely. Use `printf` then a bare `read -s VAR`.
- **Dependabot preserves the format it finds, including a bad one.** #53 and #54
  were correct bumps (setup-python v5→v7, setup-node v4→v7) and both would have
  re-landed *floating tags* in `implementer.yml` — the last two in the repo, in
  the one file that runs a coding agent with Bash, a `contents: write` token and
  an Anthropic key in four repos. The bump was right; the form was not. Taken by
  hand as SHA pins instead, and `tests/test_workflow_pins.py` now enforces the
  convention so the next bot PR cannot quietly undo it. The one deliberate
  exception is `anthropics/claude-code-action@v1`, recorded in that file:
  pinning the agent itself would freeze it at whatever it was the day someone
  last looked.
- **Never put a trailing `# comment` on a command someone will paste.**
  Interactive zsh does not set `interactive_comments`, so the `#` and every word
  after it are passed as arguments. A documented `wrangler secret put NAME
  # what the token needs` died on `Unknown arguments: #, PAT, with, ...` — the
  annotation that was supposed to prevent a mistake caused one. Put the
  explanation on its own line above.
- **Re-running a green `ledger-refresh` goes red, and means nothing.** The
  re-run replays the original checkout, rebuilds against a commit its own first
  attempt already superseded, and races to push over it; the publish step burns
  its three retries (~31s) and exits 1. Read attempt 1 before believing
  attempt 2.
- **Beware time-of-day tests.** `_ts(hours_ago=2)` run after UTC midnight stamps
  *yesterday*; two tests failed for two hours every night because of it. The
  same bug with a longer fuse: `test_guard_writes_the_workflow_output` dated its
  fake run against a fixed Monday in August while `main()` read the real clock,
  so it passed on the day it was written and asserted nothing ever after. Fixture
  timestamps go in RELATIVE (`-6h`, resolved at load) — which is why
  `tests/fixtures/pipeline/world.json` carries no absolute dates.
- **A similarity threshold means nothing without the scorer it was measured on.**
  Issue #33 asked for >0.8. On this TF-IDF that would have missed overseer #12,
  the re-filing the feature exists to catch. It is 0.75, measured against every
  pair in the filed corpus (see the table in `dedupe.py`) — and a rationale can
  only ever RAISE a score, never lower it, because the documents are titles and
  blending a long rationale in dropped a real duplicate from 0.77 to 0.66.

## What a run costs (measured, not estimated)

| | |
|---|---|
| Whole three-agent review | **$0.34** (Bug-Hunter $0.18 heavy, Idea $0.11, Reviewer $0.04) |
| One successful implementation | **$1.49** — 54 turns, ~5 min, light tier |
| A typical week (review + 3 attempts) | **~$4.80** |
| One spoken question | **~$0.008** cold, **~$0.002** cached — 2% of a review |

An implementation is ~4.4× the entire review, so `OVERSEER_IMPLEMENT_MAX` and
the tier are the only spend levers that matter. The model tiering the README
documents saves $0.10/week — real, but noise beside the implementer.

A failed attempt costs nearly as much as a successful one: you pay for the work,
not the outcome.

The assistant is cheap only because it makes **one call with no tools**. The
first time it says "the snapshot doesn't cover that", the fix that suggests
itself is to hand it the read tools — that is a tool loop, which is the 40×
difference between a question and an agent run. Publish the missing facts into
the pack instead.

## Testing what has no test runner

The dashboard is plain HTML/JS served from `docs/`. Rather than add a JS
toolchain, the Python suite pins the seams:

- `tests/test_dashboard_css.py` greps the stylesheet for rules whose absence
  caused visible layout bugs on a phone.
- `tests/test_implement_queue.py` asserts `app.js` renders into element ids
  `index.html` actually has, and that banner headings still match the ALL-CAPS
  regex `formatDigest` uses to make them section headings. It also pins that the panel
  says when the dispatcher last ran: an empty `in_flight` list means either "ran
  and found nothing" or "never ran", and until `last_dispatch` existed those
  rendered identically — on 2026-08-31 the second was true for hours while the
  panel looked like a calm week.
- `tests/test_pipeline_e2e.py` runs `scripts/pipeline_dryrun.py` in a SUBPROCESS
  and asserts on what it wrote. A subprocess because the harness sets the repo
  environment before importing `tools`, and `PROJECTS` is built at import time:
  in-process it would either read a `tools` some earlier test imported, or leave
  one configured for a fixture world behind for every test after it.
- `tests/test_dashboard_plain.py` pins the two-level page: that app.js writes
  into ids `index.html` has, that the first screen carries none of the ten words
  a stranger wouldn't know, and — the other half of that rule — that all of them
  are still there behind the toggle. A jargon check alone would pass if the
  technical half were deleted outright.
- `tests/test_ask.py` greps `worker/overseer-ask.js` for prompt sentences and
  gate-rule strings, so the Worker cannot quietly grow a second copy of either.
  It also pins that the Worker never re-serializes the facts (that would miss
  the prompt cache on every question while looking perfectly correct) and that
  the shared secret is checked before the API key is read.

To see the dashboard for real: serve `docs/` and drive it with Playwright
(Chromium is preinstalled at `/opt/pw-browsers/chromium`). Rendering it is how
the "settled work shows as stalled" bug was caught — it passed review by eye.

Workflows are validated with `python -c "import yaml; yaml.safe_load(open(...))"`
and shell steps with `bash -n` before pushing; a broken workflow fails only when
it next fires, which for the weekly review is a week away.

## Per-repo implementer notes

Each project repo runs its own copy of `examples/implementer/implement.yml`,
tailored to that repo's real CI — read the target repo's workflows before
changing one:

| Repo | Toolchain | Test command |
|---|---|---|
| `crypto-trading` | Python 3.11 + `requirements.txt` | `python -m pytest -q` |
| `coachvision` | Python only — **no pip install**, the suite is stdlib `unittest` | `python -m unittest discover -s tests -v` plus two `pipeline.py --self-test` domains |
| `ufc-dashboard` | Python 3.12 + Node 20 (`npm ci`) — two gates | `python -m pytest -q`, and `npm run verify` if the web/edge side was touched |
| `overseer` | Python 3.12 + `requirements.txt` | `python -m pytest -q` |

**This table's `crypto-trading` note used to say it "gets nothing".** That is no
longer true: as of 2026-08-30 it has bug #50 and `effort:low` enhancement #51
open, both eligible, and #50 is second in the published queue. The claim was
right when written and quietly went stale — which is the argument for asking the
assistant (`python ask.py "what is queued?"`) rather than trusting a note here.
Most of its *older* ideas are still `effort:medium`; widen
`OVERSEER_IMPLEMENT_EFFORT` to `low,medium` if you want those too.

## House style

Comments explain **why**, usually by naming the incident that motivated the
code — the git history and the README are written the same way. Keep it. A
comment that restates the line above it is noise; one that says "this ordering
exists because labelling first silently dropped a filed bug" is the reason the
next person doesn't undo it.

## Codex PR review

OpenAI Codex auto-reviews PRs in this repo. It triggers when a PR
is opened for review, when a draft is marked ready, or on a
`@codex review` comment. Findings come back as comments from
chatgpt-codex-connector[bot]; a clean pass is just a 👍 reaction.

- Default: do not merge right after opening a PR — open it, then
  stop. If I say to merge, merge.
- Once the review lands, run `gh pr view <n> --comments` and triage
  each finding: real bug / not applicable / style-only. Tell me your
  call and reasoning before changing code.
- `@codex address that feedback` makes Codex push the fix itself.
  Only do that if I ask.

In remote/web sessions there is no `gh` CLI — use the GitHub MCP tools
(`pull_request_read`, `add_issue_comment`) for the same steps.
