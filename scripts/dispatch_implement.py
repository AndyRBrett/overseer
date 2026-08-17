"""
Hand a few already-filed issues to the implementer, and stop there.

The pipeline's three agents propose work; nothing implemented it. This script is
the missing link, and deliberately the dumbest part of the system: it reads the
delivery ledger, picks at most OVERSEER_IMPLEMENT_MAX open issues that pass the
gate (tools.implementable — confirmed bugs plus effort:low enhancements), and
fires a `repository_dispatch` at each issue's OWN repo. The coding agent runs
there, against that project's tests and dependencies, and opens a PR.

Why dispatch instead of implementing here: this repo's checkout is the overseer,
not the four projects, and a runner that had to install and test four unrelated
codebases would be a worse version of four CI setups that already exist. It also
keeps the Anthropic key in each project repo rather than minting one cross-repo
token with contents: write everywhere — the credential sprawl that caused the
July outage.

What this never does: merge anything. A PR is where the automation stops and you
start.

Usage:
    python scripts/dispatch_implement.py             # dispatch up to the cap
    python scripts/dispatch_implement.py --dry-run   # show the queue, fire nothing
    python scripts/dispatch_implement.py --limit 1   # override the cap
    python scripts/dispatch_implement.py --explain   # also list what was skipped, and why

Exit codes: 0 when the queue was read (including an empty queue — a week with
nothing eligible is a normal week), 1 when the ledger could not be built or every
dispatch failed. A transient GitHub outage exits 0: this runs weekly-ish and the
work keeps until the next run, so an outage is not worth waking anyone for.
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


def describe(entry):
    sizing = ", ".join(
        f"{axis}:{entry[axis]}" for axis in ("effort", "impact") if entry.get(axis)
    )
    return (f"{entry['repo'].split('/')[-1]}#{entry['number']} [{entry.get('kind')}"
            f"{' ' + sizing if sizing else ''}] {entry.get('title', '')}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().split("\n")[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="print the queue and what would be dispatched; touch nothing")
    parser.add_argument("--limit", type=int, default=None,
                        help=f"how many issues to hand over (default {tools.IMPLEMENT_MAX}, "
                             "from OVERSEER_IMPLEMENT_MAX)")
    parser.add_argument("--explain", action="store_true",
                        help="list every filed issue the gate rejected, with the reason")
    args = parser.parse_args()

    check = tools.preflight_github()
    if check.get("transient"):
        # GitHub is down, not the credential. The queue will still be there next
        # run and nothing has been spent — this is not an incident.
        print(f"[implement] SKIP: GitHub is unreachable ({check['detail']}). "
              "The queue keeps; the next run picks it up.")
        if os.getenv("GITHUB_ACTIONS"):
            print("::warning title=Implementer skipped::GitHub unreachable; "
                  "no issues were handed over this run.")
        return 0
    if check.get("fatal"):
        print(f"[implement] ABORT: {check['detail']}", file=sys.stderr)
        return 1

    # Reuse the published ledger so settled outcomes aren't re-walked — the same
    # incremental trick as the hourly refresh. Open issues, the only ones this
    # script can act on, are always recomputed.
    ledger = tools.delivery_ledger(known=load_published(tools.LEDGER_PATH))
    if not ledger["entries"]:
        print(f"[implement] ABORT: read no filed issues at all; "
              f"errors: {ledger.get('errors') or 'none (no repos configured?)'}",
              file=sys.stderr)
        return 1
    for slug, err in (ledger.get("errors") or {}).items():
        print(f"[implement] warning: {slug}: {err}", file=sys.stderr)

    queue = tools.implementation_queue(ledger, limit=args.limit)
    picks, skipped = queue["picks"], queue["skipped"]

    print(f"[implement] gate: bugs + effort:{'/'.join(tools.IMPLEMENT_EFFORTS)} · "
          f"cap {args.limit if args.limit is not None else tools.IMPLEMENT_MAX} per run · "
          f"{queue['eligible']} eligible of {len(ledger['entries'])} filed")

    if args.explain:
        for item in skipped:
            print(f"[implement]   skip {describe(item['entry'])} — {item['reason']}")

    if not picks:
        print("[implement] nothing to hand over this run.")
        return 0

    failures = 0
    for entry in picks:
        print(f"[implement] -> {describe(entry)}")
        try:
            result = tools.dispatch_implementation(entry, dry_run=args.dry_run)
        except Exception as exc:  # noqa: BLE001 — one bad repo must not stop the rest
            failures += 1
            print(f"[implement]    FAILED: {exc}", file=sys.stderr)
            continue
        if result["status"] == "dispatched_unlabelled":
            # Not fatal: the PR it opens links the issue, which moves the entry
            # to in_flight and keeps it out of the next queue anyway.
            print(f"[implement]    dispatched, but could not apply "
                  f"{tools.IMPLEMENTING_LABEL}: {result.get('label_error')}",
                  file=sys.stderr)
        elif result["status"] == "dispatched":
            print(f"[implement]    dispatched '{result['event']}' to {result['repo']}")

    if failures and failures == len(picks):
        print(f"[implement] ABORT: all {failures} dispatch(es) failed. A token without "
              "Actions: write on the target repo is the usual cause.", file=sys.stderr)
        return 1
    if failures:
        print(f"[implement] {len(picks) - failures} handed over, {failures} failed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
