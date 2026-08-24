# Project Overseer — working notes for Claude / contributors

A weekly review pipeline that **reviews four projects, files issues, and then
implements a few of them**. Four stages, three of them agents:

```
Mon 14:00 UTC  weekly-review.yml    Bug-Hunter → Idea-Agent → Reviewer
                                    → issues filed → digest → Telegram + PWA
Mon 15:00 UTC  implement.yml        ledger → gate → ≤3 picks → repository_dispatch
      ~10 min  implement-worker.yml the coding agent, IN EACH TARGET REPO
                                    → branch → tests → pull request
       hourly  ledger-refresh.yml   PR merged → docs/shipped.json → dashboard
```

The projects reviewed are `crypto-trading`, `coachvision`, `ufc-dashboard`, and
**this repo itself** — held to the same bar as the others.

## ⛔ Run `python -m pytest -q` before you commit

263 tests, under a second. There is no JS test runner, so dashboard behaviour is
pinned from Python instead (see *Testing what has no test runner* below).

## Where things live

| File | |
|---|---|
| `orchestrator.py` | runs the three agents in sequence; preflight, ledger, telemetry |
| `agent_bug_hunter.py` / `agent_idea.py` / `agent_reviewer.py` | one prompt + tool list each |
| `tools.py` | **everything shared** — tool implementations, the delivery ledger, the implementation gate, `run_agent` |
| `tracer.py` | per-run recording, spend accounting, digest/history writers |
| `scripts/dispatch_implement.py` | picks issues and hands them to the implementer |
| `scripts/refresh_ledger.py` | hourly, pure GitHub reads, no model calls |
| `scripts/heartbeat.py` | daily; stdlib-only and tokenless **by design** |
| `docs/` | the PWA dashboard (`index.html` + `app.js`), fed by `digest.json`, `history.json`, `shipped.json` |
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
8. **Failure taxonomy is load-bearing.** An attempt that died on an exhausted
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
- **Beware time-of-day tests.** `_ts(hours_ago=2)` run after UTC midnight stamps
  *yesterday*; two tests failed for two hours every night because of it.

## What a run costs (measured, not estimated)

| | |
|---|---|
| Whole three-agent review | **$0.34** (Bug-Hunter $0.18 heavy, Idea $0.11, Reviewer $0.04) |
| One successful implementation | **$1.49** — 54 turns, ~5 min, light tier |
| A typical week (review + 3 attempts) | **~$4.80** |

An implementation is ~4.4× the entire review, so `OVERSEER_IMPLEMENT_MAX` and
the tier are the only spend levers that matter. The model tiering the README
documents saves $0.10/week — real, but noise beside the implementer.

A failed attempt costs nearly as much as a successful one: you pay for the work,
not the outcome.

## Testing what has no test runner

The dashboard is plain HTML/JS served from `docs/`. Rather than add a JS
toolchain, the Python suite pins the seams:

- `tests/test_dashboard_css.py` greps the stylesheet for rules whose absence
  caused visible layout bugs on a phone.
- `tests/test_implement_queue.py` asserts `app.js` renders into element ids
  `index.html` actually has, and that banner headings still match the ALL-CAPS
  regex `formatDigest` uses to make them section headings.

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

**`crypto-trading` currently gets nothing**: all its open ideas are
`effort:medium`, and the gate defaults to `effort:low`. Widen
`OVERSEER_IMPLEMENT_EFFORT` to `low,medium` if that matters.

## House style

Comments explain **why**, usually by naming the incident that motivated the
code — the git history and the README are written the same way. Keep it. A
comment that restates the line above it is noise; one that says "this ordering
exists because labelling first silently dropped a filed bug" is the reason the
next person doesn't undo it.
