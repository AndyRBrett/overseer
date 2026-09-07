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

EVERY AUTOMATED TRIGGER ASKS; ONLY workflow_dispatch DOESN'T (2026-09-07). This
module used to ask only the crons it recognised as catch-ups, on the premise that
`0 15 * * 1` IS the dispatch and not a second guess at it. That premise was false,
and it cost a duplicate batch the same evening it was written down:

    run #6  17:32Z  workflow_dispatch  -> coachvision#38, overseer#72, crypto#71
    run #7  18:55Z  schedule 0 15 * * 1 -> coachvision#39, overseer#77, coachvision#43
    [guard] running: not a catch-up run — this is the dispatch itself.

GitHub delivered the PRIMARY cron three hours fifty-five minutes late, after a
manual run had already handed over the day's batch. Six attempts, ~$9, where the
design intends three at ~$4.50 — the exact $9.00 Monday described above, reached
without any second scheduler being involved at all.

So the question is no longer "which trigger am I?" but "has today's dispatch
already landed?", which is invariant 6b and what weekly_guard has always done.
Every automated trigger asks it: a cron, whenever delivered, and the Cloudflare
repository_dispatch alike. FIRED_BY_EVENT (`github.event_name`) exempts only
`workflow_dispatch`, because a human at the keyboard asking for a run twice means
it. That also makes the successful-run check a hard per-day cap on automated
spend rather than a rule that has to classify a trigger correctly first — the
classification is what failed.

AND TODAY'S REVIEW HAS TO HAVE LANDED. The dispatcher reads a ledger the weekly
review fills. Fire it before the review and it picks from last week's backlog,
spending the week's budget on a stale queue — reachable now that two schedulers
aim at the same Monday and the Cloudflare poke for `implement` can arrive while
the review is still running. So an automated run also checks that
docs/digest.json carries today's date, which is the same file and the same
question weekly_guard asks, read on the GitHub side per invariant 8.

The cost of that check: on a week where the review never lands at all, the
implementer does not run either. That is deliberate — there is nothing new to
implement — but it IS a second way for this stage to go quiet, which is this
system's characteristic failure mode. It is a skipped week of delivery, never a
skipped alarm: the heartbeat still trips on the standing-still digest within a
day, which is the thing that actually needs saying out loud.

WHAT A DRY RUN IS NOT (2026-09-07). A `--dry-run` run hands nothing over, so it
must not count as today's dispatch. The API does not expose a run's
workflow_dispatch inputs, so implement.yml marks its own run-name instead and
this module reads the marker back. It matters because dry_run DEFAULTS to true on
a manual run: looking at the queue before firing — the careful thing to do — would
otherwise disarm every catch-up left in the day, and would have eaten the last
one on 09-07.

Writes `should_run=true|false` to $GITHUB_OUTPUT for the workflow's `if:`
conditions, and prints the reasoning for the run log. Always exits 0 — this is a
decision, not a verdict, and it must never be the thing that fails the workflow.
"""

import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools  # noqa: E402

WORKFLOW_FILE = os.getenv("IMPLEMENT_WORKFLOW_FILE", "implement.yml")

# The only trigger that does not ask. A human choosing to dispatch a second batch
# has seen the first one; every other trigger here is a machine that cannot tell.
#
# There is deliberately no list of which crons are "catch-ups" any more. That list
# existed, it had to be kept in step with the workflow by a test, and it was still
# wrong in the way that mattered: it classified `0 15 * * 1` as the dispatch, so a
# late delivery of the primary sailed past the check and bought a second batch.
MANUAL_EVENT = "workflow_dispatch"

# What implement.yml's `run-name` carries when the run was a dry run. A substring
# match on the run's title is not elegant; it is the only signal GitHub gives back
# about how a workflow_dispatch was parameterised. Keep in step with the run-name
# expression in implement.yml.
DRY_RUN_MARKER = "(dry run)"


def _was_dry_run(run):
    """Did this run hand nothing over because it was a dry run?

    Runs created before the run-name marker existed carry no title of their own
    and so read as real dispatches. That is the conservative direction for
    history — it can only make a catch-up skip, never double-spend — and it ages
    out after one Monday.
    """
    title = getattr(run, "display_title", None) or getattr(run, "name", "") or ""
    return DRY_RUN_MARKER in title


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
        if _was_dry_run(run):
            # Green, and it dispatched nothing. See DRY_RUN_MARKER.
            continue
        created = getattr(run, "created_at", None)
        if created is None:
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if created.astimezone(timezone.utc).date() == today:
            return run
    return None


def reviewed_today(digest, now=None):
    """Did the weekly review publish a digest today (UTC)? None if unreadable.

    Reads `generated`, which invariant 13 reserves for the REVIEW's timestamp —
    refresh_status.py writes `refreshed` precisely so this question keeps its
    meaning between reviews.
    """
    stamp = (digest or {}).get("generated")
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        when = datetime.strptime(stamp[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    return when == (now or datetime.now(timezone.utc)).date()


def should_run(runs, now=None, exclude_id=None, event=None, digest=None):
    """(run?, reason) for a dispatch fired by `event`, against `runs`/`digest`."""
    if (event or "").strip() == MANUAL_EVENT:
        return True, "manual dispatch — a human asked for this run."

    if runs is None:
        return True, "could not read this workflow's own run history; proceeding."
    earlier = dispatched_today(runs, now, exclude_id)
    if earlier is not None:
        return False, (f"today's dispatch already ran successfully "
                       f"(run {earlier.id} at {earlier.created_at}).")

    reviewed = reviewed_today(digest, now)
    if reviewed is False:
        return False, ("today's review has not landed yet — the ledger is last "
                       "week's, so there is nothing new to hand over.")
    if reviewed is None:
        # Unreadable digest is the in-doubt case, and in doubt this RUNS: a
        # skipped week costs more than a batch picked from a slightly stale
        # queue, and the cap above still bounds it to one batch.
        return True, "could not read the published digest; proceeding."

    return True, "no dispatch yet today and the review has landed."


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


def published_digest(path=None):
    """The published digest, or None if it cannot be read.

    None and a digest with no `generated` are both "cannot tell", which
    should_run treats as a reason to proceed rather than to skip.
    """
    try:
        with open(path or tools.DIGEST_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as exc:
        print(f"[guard] could not read the digest: {exc}", file=sys.stderr)
        return None


def main():
    run, reason = should_run(
        recent_runs(),
        exclude_id=os.getenv("GITHUB_RUN_ID"),
        event=os.getenv("FIRED_BY_EVENT"),
        digest=published_digest(),
    )
    print(f"[guard] {'running' if run else 'skipping'}: {reason}")

    out = os.getenv("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"should_run={'true' if run else 'false'}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
