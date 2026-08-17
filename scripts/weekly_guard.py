"""
Decide whether a catch-up weekly review should actually run.

THE PROBLEM THIS SOLVES. The review fires once a week. On 2026-08-17 it fired
into nine seconds of GitHub 503s, aborted at the preflight, and the next attempt
was seven days away — no digest, no push, and nothing to say so. The in-job
retry (see weekly-review.yml) covers an outage measured in minutes; these
catch-up crons at 16:00 and 18:00 UTC cover one measured in hours.

The catch-up runs are cheap only because they usually do nothing, which is this
script's entire job: it answers "did today's review already land?" so a healthy
Monday costs a checkout and a file read instead of a second full pipeline. That
matters — a duplicate run spends real model budget and points two Bug-Hunters at
the same four repos.

WHAT COUNTS AS LANDED: a digest generated today (UTC) by a run that completed.
Anything else — no digest, an unreadable one, yesterday's, or one from a run
that crashed partway — means the review still owes us today's update, so the
catch-up proceeds. When in doubt it runs: a redundant review costs money, but a
skipped one costs the week.

Only the catch-up crons are asked. The 14:00 review and any manual dispatch are
the run, not a second guess at it, so they always proceed — FIRED_BY_SCHEDULE
(the workflow's `github.event.schedule`, empty for a dispatch) is what tells
them apart. Keeping that comparison here rather than in workflow YAML is what
makes it testable.

Writes `should_run=true|false` to $GITHUB_OUTPUT for the workflow's `if:`
conditions, and prints the reasoning for the run log. Always exits 0 — this is a
decision, not a verdict, and it must never be the thing that fails the workflow.
"""

import json
import os
from datetime import datetime, timezone

DIGEST_PATH = os.getenv("DIGEST_PATH", "docs/digest.json")

# The crons in weekly-review.yml that are catch-ups rather than the review.
# Keep in step with the `schedule:` block there — a cron listed in one and not
# the other just means the catch-up runs unconditionally, which costs a
# duplicate review rather than a missed one.
CATCHUP_SCHEDULES = {"0 16 * * 1", "0 18 * * 1"}


def digest_landed_today(digest, now=None):
    """True when `digest` is a completed review generated today (UTC)."""
    if not isinstance(digest, dict):
        return False
    if digest.get("status") != "completed":
        return False
    stamp = digest.get("generated")
    if not stamp:
        return False
    try:
        written = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return False
    return written.date() == (now or datetime.now(timezone.utc)).date()


def load_digest(path=None):
    try:
        with open(path or DIGEST_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return None


def should_run(schedule, digest, now=None):
    """(run?, reason) for a job fired by `schedule` against `digest`."""
    if (schedule or "").strip() not in CATCHUP_SCHEDULES:
        return True, "not a catch-up run — this is the review itself."
    if digest_landed_today(digest, now):
        return False, (f"today's completed digest is already published "
                       f"({digest.get('generated')}).")
    stamp = (digest or {}).get("generated") or "no digest"
    return True, f"no completed digest for today (latest: {stamp})."


def main():
    run, reason = should_run(os.getenv("FIRED_BY_SCHEDULE"), load_digest())
    print(f"[guard] {'running' if run else 'skipping'}: {reason}")

    out = os.getenv("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"should_run={'true' if run else 'false'}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
