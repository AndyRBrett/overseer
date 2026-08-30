# Project Overseer — working notes for Claude / contributors

A weekly review pipeline that **reviews four projects, files issues, and then
implements a few of them**. Four stages, three of them agents:

```
Mon 14:00 UTC  weekly-review.yml    Bug-Hunter → Idea-Agent → Reviewer
                                    → issues filed → digest → Telegram + PWA
Mon 15:00 UTC  implement.yml        ledger → gate → ≤3 picks → repository_dispatch
      ~10 min  implement-worker.yml the coding agent, IN EACH TARGET REPO
                                    → branch → tests → pull request
      ~6×/day  ledger-refresh.yml   PR merged → docs/shipped.json → dashboard
                                    → docs/ask-context.json → voice assistant
    on demand  worker/              "Hey Siri, ask Overseer" → one model call
```

The projects reviewed are `crypto-trading`, `coachvision`, `ufc-dashboard`, and
**this repo itself** — held to the same bar as the others.

## ⛔ Run `python -m pytest -q` before you commit

Test deps are `pytest` and `pyyaml` (CI installs both alongside
`requirements.txt`; neither is a runtime dependency).

308 tests, under a second. There is no JS test runner, so dashboard behaviour is
pinned from Python instead (see *Testing what has no test runner* below).

## Where things live

| File | |
|---|---|
| `orchestrator.py` | runs the three agents in sequence; preflight, ledger, telemetry |
| `agent_bug_hunter.py` / `agent_idea.py` / `agent_reviewer.py` | one prompt + tool list each |
| `tools.py` | **everything shared** — tool implementations, the delivery ledger, the implementation gate, `run_agent` |
| `tracer.py` | per-run recording, spend accounting, digest/history writers |
| `scripts/dispatch_implement.py` | picks issues and hands them to the implementer |
| `scripts/refresh_ledger.py` | cron every ~2–6h (see below), pure GitHub reads, no model calls |
| `scripts/heartbeat.py` | daily; stdlib-only and tokenless **by design** |
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
7. **Deterministic digest sections stay deterministic.** The staleness banner,
   `IMPLEMENTED`, and `AGING BACKLOG` are computed in Python and stitched into
   the digest in `run_agent`, because a section that depends on an agent
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
  That is why the agent is told to run the suite itself before opening one.
- **Labels are never cleaned off a closed issue.** Anything keying on
  `overseer:implement-failed` must also check the entry is still open, or
  settled work reports as needing attention forever.
- **Speech is not an API input.** Claude takes text, images and PDFs, not audio.
  Voice works here only because the iPhone does speech-to-text and text-to-speech
  on-device for free; any server-side transcription would add a provider, a key
  and a per-minute bill to something that currently costs nothing.
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
- **Re-running a green `ledger-refresh` goes red, and means nothing.** The
  re-run replays the original checkout, rebuilds against a commit its own first
  attempt already superseded, and races to push over it; the publish step burns
  its three retries (~31s) and exits 1. Read attempt 1 before believing
  attempt 2.
- **Beware time-of-day tests.** `_ts(hours_ago=2)` run after UTC midnight stamps
  *yesterday*; two tests failed for two hours every night because of it.

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
  regex `formatDigest` uses to make them section headings.
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
