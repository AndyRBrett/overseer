"""
Decide whether an automated weekly review should actually run.

THE PROBLEM THIS SOLVES. The review fires once a week. On 2026-08-17 it fired
into nine seconds of GitHub 503s, aborted at the preflight, and the next attempt
was seven days away — no digest, no push, and nothing to say so. The in-job
retry (see weekly-review.yml) covers an outage measured in minutes; the catch-up
crons at 16:00 and 18:00 UTC cover one measured in hours.

THE SECOND FAILURE MODE, 2026-08-31. None of that helps when the job never
starts. That Monday GitHub delivered *no* scheduled event at all: 14:00, 16:00
and 18:00 all passed with no run created — not a failure, not a queue, simply
nothing, while push- and PR-triggered runs in the same repo fired normally. The
dashboard sat on a seven-day-old digest and nothing alerted, because every alarm
here is downstream of a job starting. The catch-ups were no redundancy at all:
they are `schedule:` entries in the same workflow, queued through the same
deprioritised scheduler that dropped the primary. So the review now also has an
off-GitHub trigger — a Cloudflare cron in worker/overseer-ask.js that fires
`repository_dispatch` — and this guard is what keeps the two from colliding.

WHAT COUNTS AS LANDED: a digest generated today (UTC) by a run that completed.
Anything else — no digest, an unreadable one, yesterday's, or one from a run
that crashed partway — means the review still owes us today's update, so the
run proceeds. When in doubt it runs: a redundant review costs money, but a
skipped one costs the week.

EVERY AUTOMATED TRIGGER IS ASKED — schedule and repository_dispatch alike. It
used to be only the catch-up crons, on the reasoning that the 14:00 cron *is*
the review and should never second-guess itself. Two triggers on independent
schedulers make that unsafe: the Cloudflare cron fires at 14:05, and a GitHub
14:00 cron delivered late (30 minutes late is routine; 45 has happened) would
then run a second full pipeline over a digest published minutes earlier. So the
question is no longer "is this a catch-up?" but "has today's review already
landed?", which is the same answer for whichever trigger arrives second. Only
workflow_dispatch bypasses it: a human clicking "Run workflow" means it, and is
the documented way to force a re-review on a day that already has a digest.

Writes `should_run=true|false` to $GITHUB_OUTPUT for the workflow's `if:`
conditions, and prints the reasoning for the run log. Always exits 0 — this is a
decision, not a verdict, and it must never be the thing that fails the workflow.
"""

import json
import os
from datetime import datetime, timezone

DIGEST_PATH = os.getenv("DIGEST_PATH", "docs/digest.json")

# The one trigger that is a human, not a scheduler. Everything else is asked
# whether today's review already landed.
UNGUARDED_EVENTS = {"workflow_dispatch"}


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


def should_run(event, digest, now=None):
    """(run?, reason) for a job fired by `event` against `digest`."""
    if (event or "").strip() in UNGUARDED_EVENTS:
        return True, "a human asked for this run — not second-guessing it."
    if digest_landed_today(digest, now):
        return False, (f"today's completed digest is already published "
                       f"({digest.get('generated')}).")
    stamp = (digest or {}).get("generated") or "no digest"
    return True, f"no completed digest for today (latest: {stamp})."


def main():
    run, reason = should_run(os.getenv("FIRED_BY_EVENT"), load_digest())
    print(f"[guard] {'running' if run else 'skipping'}: {reason}")

    out = os.getenv("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"should_run={'true' if run else 'false'}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
