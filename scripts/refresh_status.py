"""
Keep the dashboard's TOP half live between weekly reviews.

WHY THIS EXISTS. `docs/shipped.json` refreshes several times a day and
`docs/digest.json` refreshed once a week, so the page had two clocks on it: a
delivery panel current to the hour sitting directly below project health that
could be five days old. On 2026-09-05 the split was 0.3h against 117.8h, and the
stale half is the half that says whether anything is BROKEN.

Nothing about that was necessary. The expensive part of the weekly review is the
three agents; the parts that went stale — per-project health, the freshness
alerts, the attention ranking — are derived from four GitHub reads and some
arithmetic. They were welded to a $0.34 model run purely because that is where
the tracer happens to write the file. This is the same argument
`refresh_ledger.py` already won for the ledger, applied to the other file.

WHAT IT DOES NOT TOUCH, and why that matters more than what it does:

  * `generated` — the timestamp of the last REVIEW. Three things read it as
    exactly that: scripts/heartbeat.py (the dead-man's switch), the Worker's
    staleness check, and the dashboard's "next run overdue". A refresh that
    bumped it would tell all three that the review had run. The heartbeat exists
    because GitHub silently drops scheduled events — CLAUDE.md's own note is
    that a dropped review is only detectable BECAUSE digest.json stands still —
    so bumping this field would disable the alarm that catches the failure this
    project has actually had, and disable it invisibly, six times a day. The
    refresh writes `refreshed` instead and leaves `generated` alone.
  * `summary`, `counts`, `spend`, `timeline`, `output_alerts` — the review's own
    account of what it did. Re-reading feeds says nothing new about a model run
    that happened on Monday, and a refresh that recomputed them would be
    inventing a run.

    python scripts/refresh_status.py            # refresh in place
    python scripts/refresh_status.py --dry-run  # report, write nothing

Exit codes: 0 on success and on a skip (GitHub unreachable, or nothing moved);
1 only when the digest could not be read at all, since publishing a digest with
no project health would empty the panel this exists to keep full.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools  # noqa: E402
from tracer import RunTracer  # noqa: E402

# The fields this script owns. Everything else in digest.json belongs to the
# weekly review and is passed through untouched — listed explicitly so that a
# new key added by the review is carried forward by default rather than dropped
# by an omission here.
REFRESHED_KEYS = ("projects", "rollup", "attention", "headline", "refreshed")


def load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return None


def rebuild(published, ledger, readings):
    """The refreshed blocks, from live reads plus the published digest.

    Pure: no I/O, no clock beyond the stamp. `published` supplies the previous
    per-project health, which is what carries the cycle counters and each
    project's `last_ok` across the gap.
    """
    # count_cycles=False: an observation is not a weekly cycle. Counting these
    # would read "stale 42 cycles" by Friday and nudge on the first afternoon.
    tracer = RunTracer(jsonl_path=os.devnull, html_path=os.devnull,
                       count_cycles=False)
    tracer.read_tools = tools.READ_TOOLS
    tracer.read_repos = tools.read_tool_repos()
    tracer.prev_projects = (published or {}).get("projects") or {}
    tracer.ledger = ledger
    tracer.shared_reads(readings)
    return {
        "projects": tracer.project_health(),
        "rollup": tracer.rollup(),
        "attention": tracer.attention(),
        "headline": tracer.headline(),
        "refreshed": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def changed(published, fresh):
    """Did anything but the stamp move? Keeps a no-op run from committing.

    This runs on the ledger's cron. A file that rewrote itself every few hours
    would put six commits a day into a history that is already mostly
    "Refresh delivery ledger" — burying the changes anyone would want to find.
    """
    def _comparable(d):
        return {k: v for k, v in (d or {}).items() if k != "refreshed"}
    return _comparable({k: (published or {}).get(k) for k in REFRESHED_KEYS}) != \
        _comparable({k: fresh.get(k) for k in REFRESHED_KEYS})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.strip().split("\n")[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change without writing the file")
    parser.add_argument("--path", default=tools.DIGEST_PATH)
    args = parser.parse_args(argv)

    published = load(args.path)
    if published is None:
        print(f"[status] ABORT: cannot read {args.path} — refusing to publish a "
              f"digest with no run behind it.", file=sys.stderr)
        return 1

    check = tools.preflight_github()
    if check.get("transient"):
        # GitHub had a bad minute. The published panel stands for another hour;
        # the ledger refresh beside this one makes the same call for the same
        # reason (see refresh_ledger.skip_or_fail).
        print(f"[status] SKIP: GitHub is unreachable ({check['detail']}) — "
              f"leaving the published health in place.")
        return 0
    if check.get("fatal"):
        print(f"[status] SKIP: {check['detail']}", file=sys.stderr)
        return 0

    readings = tools.read_all_projects()
    # Every read failing is a broken credential or an unconfigured environment,
    # not four dead projects. Publishing that would replace a healthy panel with
    # a wall of BLIND — the same "never publish something emptier than what is
    # live" rule refresh_ledger.py enforces on the ledger.
    if not any((r or {}).get("status") == "ok" for r in readings.values()):
        print("[status] SKIP: no project read succeeded — refusing to blank the "
              "panel. Details: "
              + "; ".join(f"{k}: {(v or {}).get('status')}" for k, v in readings.items()),
              file=sys.stderr)
        return 0

    fresh = rebuild(published, load(tools.LEDGER_PATH), readings)

    for name, p in sorted(fresh["projects"].items()):
        was = ((published.get("projects") or {}).get(name) or {}).get("status")
        moved = "" if was == p["status"] else f"  ({was or 'new'} -> {p['status']})"
        print(f"[status] {name}: {p['status']}{moved}")
    ranked = fresh["attention"]
    if ranked:
        top = ranked[0]
        print(f"[status] most in need of attention: {top['name']} "
              f"({top['score']:.2f}) — {top['why']}")

    if not changed(published, fresh):
        print("[status] unchanged — nothing to publish.")
        return 0
    if args.dry_run:
        print("[status] dry run — not written.")
        return 0

    published.update(fresh)
    with open(args.path, "w", encoding="utf-8") as f:
        json.dump(published, f, indent=2)
        f.write("\n")
    print(f"[status] wrote {args.path} "
          f"(review timestamp left at {published.get('generated')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
