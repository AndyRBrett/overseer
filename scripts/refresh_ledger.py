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

Exit codes: 0 always on a successful read (whether or not anything changed);
1 only when the ledger could not be built at all, so a broken credential fails
the workflow instead of quietly publishing an empty scoreboard.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools  # noqa: E402


def load_published(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return None


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
