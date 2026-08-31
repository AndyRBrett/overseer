"""
Decide whether a catch-up implement run should actually dispatch anything.

THE PROBLEM THIS SOLVES. On 2026-08-31 the 15:00 implement cron did not fire at
its hour at all: GitHub deprioritises scheduled workflows on free public repos
(the ledger-refresh measurement in CLAUDE.md — ~6 firings a day, worst gap 13.3h)
and this workflow had exactly ONE cron and no catch-up. It eventually landed at
20:30, five and a half hours late, which happened to still be Monday. Had it
slipped past midnight the week's implementation stage would simply have been
skipped, with nothing red and nothing to say so — the review has catch-up crons
at 16:00 and 18:00 for precisely this reason, and this workflow had none.

WHY THE OBVIOUS FIX IS WRONG. Adding catch-up crons alone DOUBLES the bill. The
dispatcher labels each issue it hands over, so a catch-up an hour later does not
re-pick the same three — it picks three DIFFERENT ones. At the measured ~$1.50
an attempt that turns a $4.50 Monday into $9.00, and the implementer is already
~4.4x the entire review. The catch-up has to be free when the run it is covering
for already happened.

WHAT COUNTS AS LANDED: a successful implement run earlier today (UTC), other than
this one. That is the whole condition, and it caps the day at one dispatch batch
however many catch-ups fire. Note what it deliberately does NOT do: top up a
batch that dispatched fewer than the cap. A 15:00 run that handed over one issue
because only one was eligible has already emptied the queue; a catch-up "topping
it up" to three would be inventing work, not recovering it.

When in doubt it RUNS: a duplicate batch costs money, but a skipped week costs the
week — and every other path here (an unreadable run list, a GitHub outage, no
token) leaves the day's work undone, which is the more expensive mistake. The cap
inside tools.implementation_queue still bounds whatever a doubtful run does.

Only the catch-up crons are asked. The 15:00 run and any manual dispatch are the
run, not a second guess at it, so they always proceed — FIRED_BY_SCHEDULE (the
workflow's `github.event.schedule`, empty for a dispatch) is what tells them
apart. Keeping that comparison here rather than in workflow YAML is what makes it
testable.

Writes `should_run=true|false` to $GITHUB_OUTPUT for the workflow's `if:`
conditions, and prints the reasoning for the run log. Always exits 0 — this is a
decision, not a verdict, and it must never be the thing that fails the workflow.
"""

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools  # noqa: E402

WORKFLOW_FILE = os.getenv("IMPLEMENT_WORKFLOW_FILE", "implement.yml")

# The crons in implement.yml that are catch-ups rather than the run itself.
# Keep in step with the `schedule:` block there — a cron listed in one and not
# the other just means the catch-up dispatches unconditionally, which is the
# double-spend this whole module exists to prevent.
CATCHUP_SCHEDULES = {"0 17 * * 1", "0 19 * * 1"}


def dispatched_today(runs, now=None, exclude_id=None):
    """The first successful run from today (UTC) in `runs`, or None.

    `runs` is an iterable of objects with .id, .conclusion and .created_at, which
    is what PyGithub's workflow run listing yields. exclude_id drops the run
    asking the question — it is itself in progress and in the list.
    """
    today = (now or datetime.now(timezone.utc)).date()
    for run in runs:
        if exclude_id is not None and str(getattr(run, "id", "")) == str(exclude_id):
            continue
        if getattr(run, "conclusion", None) != "success":
            continue
        created = getattr(run, "created_at", None)
        if created is None:
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if created.astimezone(timezone.utc).date() == today:
            return run
    return None


def should_run(schedule, runs, now=None, exclude_id=None):
    """(run?, reason) for a dispatch fired by `schedule` against `runs`."""
    if (schedule or "").strip() not in CATCHUP_SCHEDULES:
        return True, "not a catch-up run — this is the dispatch itself."
    if runs is None:
        return True, "could not read this workflow's own run history; proceeding."
    earlier = dispatched_today(runs, now, exclude_id)
    if earlier is not None:
        return False, (f"today's dispatch already ran successfully "
                       f"(run {earlier.id} at {earlier.created_at}).")
    return True, "no successful dispatch yet today — covering for the missed run."


def recent_runs(limit=20):
    """This workflow's most recent runs, newest first, or None if unreadable.

    None is distinct from an empty list on purpose: empty means "definitely
    nothing ran today", None means "could not tell", and should_run treats them
    differently.
    """
    slug = os.getenv("OVERSEER_REPO") or os.getenv("GITHUB_REPOSITORY")
    if not slug:
        return None
    try:
        repo = tools._github().get_repo(slug)
        workflow = repo.get_workflow(WORKFLOW_FILE)
        return list(workflow.get_runs()[:limit])
    except Exception as exc:  # noqa: BLE001 — never fail the workflow on a read
        print(f"[guard] could not list runs of {WORKFLOW_FILE}: {exc}", file=sys.stderr)
        return None


def main():
    run, reason = should_run(
        os.getenv("FIRED_BY_SCHEDULE"),
        recent_runs(),
        exclude_id=os.getenv("GITHUB_RUN_ID"),
    )
    print(f"[guard] {'running' if run else 'skipping'}: {reason}")

    out = os.getenv("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"should_run={'true' if run else 'false'}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
