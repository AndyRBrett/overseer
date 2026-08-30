"""
Refresh docs/shipped.json on its own schedule, decoupled from the weekly review.

Why this is separate from orchestrator.py: the ledger is pure GitHub reads. It
needs no Anthropic key, no agents, and no model calls, so it costs essentially
nothing to run — while the weekly review costs real money and takes minutes.
Tying the two together meant a PR merged on Tuesday showed as "in flight" until
the following Monday, which makes the Shipped panel a stale scoreboard rather
than a live one.

Cheap enough to run hourly because the refresh is INCREMENTAL: outcomes that
cannot change again (shipped / duplicate / not planned) are carried forward from
the published ledger, so the per-issue timeline lookup — the expensive call —
only runs for entries still in motion. Of 65 entries today, 9 are live.

Usage:
    python scripts/refresh_ledger.py            # incremental
    python scripts/refresh_ledger.py --full     # re-walk every issue
    python scripts/refresh_ledger.py --dry-run  # report, write nothing

Exit codes: 0 on a successful read (whether or not anything changed), and on a
run skipped because GitHub was briefly unreachable while the published ledger is
still fresh; 1 when the ledger could not be built at all, so a broken credential
fails the workflow instead of quietly publishing an empty scoreboard.

A GitHub outage is NOT a broken credential. This runs on a short cron against an
API that serves the odd 503, and a run that cannot read is not a run that read something
wrong — the published file simply stands for another hour. Failing those runs
means a red workflow and a failure email for a wobble that fixed itself before
anyone opened the log. So a transient failure skips, and only a ledger that has
gone stale past LEDGER_MAX_STALE_HOURS (i.e. the outage is no longer brief) is
allowed to fail the workflow.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools  # noqa: E402

# How long the panel may coast on its last good read before a skipped refresh
# becomes a real alarm.
#
# THIS WAS 6, AND 6 MEANT THE WRONG THING. It was chosen to mean "six
# consecutive missed refreshes" back when the cron was believed to fire hourly.
# It does not: measured over 29 consecutive scheduled runs (2026-08-25 → 08-30),
# GitHub delivers this schedule about six times a day, median gap 2.6h and worst
# 13.3h. So 6h had quietly become roughly ONE ordinary gap, and the skip path
# below — the whole point of which is to ride out a transient 503 — almost never
# applied: a wobble found the published ledger already past the limit and
# hard-failed the run instead. That is the exact red workflow and failure email
# the 2026-08-17 incident added this guard to prevent.
#
# 24h restores the original intent against the real cadence: comfortably past
# the worst observed gap, still inside a day so a genuinely stuck refresh
# surfaces before the next weekly review reads from it.
MAX_STALE_HOURS = float(os.getenv("LEDGER_MAX_STALE_HOURS", "24"))


def load_published(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return None


def published_age_hours(published, now=None):
    """Hours since the published ledger was written, or None if undatable.

    None is not 'fresh'. A ledger we cannot date is one we cannot vouch for, and
    the caller treats it as too stale to coast on.
    """
    stamp = (published or {}).get("generated")
    if not stamp:
        return None
    try:
        written = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    return ((now or datetime.now(timezone.utc)) - written).total_seconds() / 3600


def skip_or_fail(reason, published):
    """Exit code for a run that could not read GitHub properly.

    Skips (0) while the published ledger is still fresh, fails (1) once it is
    not. Either way the panel keeps its last good state — the only question is
    whether this run is worth waking anyone for.
    """
    age = published_age_hours(published)
    if age is not None and age <= MAX_STALE_HOURS:
        print(f"[ledger] SKIP: {reason} Published ledger is {age:.1f}h old "
              f"(limit {MAX_STALE_HOURS:g}h) — leaving it in place for the next run.")
        if os.getenv("GITHUB_ACTIONS"):
            print(f"::warning title=Ledger refresh skipped::{reason} "
                  f"Panel is {age:.1f}h old; the next hourly run will retry.")
        return 0
    stale = "never published" if age is None else f"{age:.1f}h old"
    print(f"[ledger] ABORT: {reason} Published ledger is {stale}, past the "
          f"{MAX_STALE_HOURS:g}h limit — the panel is going stale.", file=sys.stderr)
    return 1


def diff_statuses(before, after):
    """(repo, number, old_status, new_status) for every entry that moved.

    The point of the run log: "3 files changed" tells you nothing, whereas
    "ufc-dashboard#66 in_flight -> shipped" is the whole story.
    """
    old = {(e.get("repo"), e.get("number")): e.get("status")
           for e in (before or {}).get("entries", [])}
    moved = []
    for e in after.get("entries", []):
        key = (e.get("repo"), e.get("number"))
        was = old.get(key)
        if was != e.get("status"):
            moved.append((key[0], key[1], was, e.get("status")))
    return moved


def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().split("\n")[0])
    parser.add_argument("--full", action="store_true",
                        help="re-walk every issue instead of carrying settled outcomes forward")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change without writing the file")
    args = parser.parse_args()

    path = tools.LEDGER_PATH
    published = load_published(path)

    check = tools.preflight_github()
    if check.get("transient"):
        # GitHub was down, not the token. Nothing to fix and nothing to publish.
        return skip_or_fail(f"GitHub is unreachable: {check['detail']}", published)
    if check.get("fatal"):
        print(f"[ledger] ABORT: {check['detail']}", file=sys.stderr)
        return 1

    ledger = tools.delivery_ledger(known=None if args.full else published)

    # Never publish a ledger emptier than the one already live. Entries do not
    # spontaneously disappear, so a collapse to zero means a bad credential, an
    # unconfigured environment, or a GitHub outage — never an empty backlog.
    # Without this, running the script locally with no repo variables set would
    # cheerfully overwrite the panel with nothing.
    had = len((published or {}).get("entries", []))
    if not ledger["entries"] and had:
        print(f"[ledger] ABORT: read 0 entries but {had} are published — refusing "
              f"to blank the panel. errors: {ledger.get('errors') or 'none (no repos configured?)'}",
              file=sys.stderr)
        return 1
    if not ledger["entries"] and ledger.get("errors"):
        print(f"[ledger] ABORT: no entries read; errors: {ledger['errors']}", file=sys.stderr)
        return 1

    # The partial version of the same rule. Preflight can pass and a repo still
    # fail mid-walk — delivery_ledger swallows that into `errors` and returns
    # what it got, which for this script would mean committing a panel with one
    # project's history quietly missing. A shrunken ledger plus a read error is
    # a bad read, not a shorter backlog.
    if ledger.get("errors") and len(ledger["entries"]) < had:
        return skip_or_fail(
            f"read {len(ledger['entries'])} entries against {had} published, "
            f"with errors: {ledger['errors']}.", published)

    moved = diff_statuses(published, ledger)
    t = ledger["totals"]
    print(f"[ledger] {t['proposed']} filed · {t['shipped']} shipped · "
          f"{t['in_flight']} in flight · {t['open']} open · "
          f"{t['duplicate']} duplicate · {t['not_planned']} not planned")
    if moved:
        print(f"[ledger] {len(moved)} item(s) moved:")
        for repo, number, was, now in moved:
            print(f"[ledger]   {repo.split('/')[-1]}#{number}: {was or 'new'} -> {now}")
    else:
        print("[ledger] no status changes since the last refresh.")
    for slug, err in (ledger.get("errors") or {}).items():
        print(f"[ledger] warning: {slug}: {err}", file=sys.stderr)

    if args.dry_run:
        print("[ledger] dry run — not written.")
        return 0

    tools.write_ledger(ledger, path)
    print(f"[ledger] wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
