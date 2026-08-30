"""Publish docs/ask-context.json — what the voice assistant answers from.

Runs wherever the source files change: hourly after the ledger refresh, and at
the end of the weekly review. Pure file reads — no GitHub, no Anthropic key, no
model calls — so it is free to run as often as the ledger does.

Deliberately NOT part of the Worker. The Worker fetches this file over HTTPS and
sends it as a prompt prefix; building it here is what keeps the rules (and the
prompt) in one testable place. See ask_context.py for the full reasoning.

Usage:
    python scripts/build_ask_context.py             # write docs/ask-context.json
    python scripts/build_ask_context.py --dry-run   # print a size report only

Exit codes: 0 on success, 1 if no source file could be read at all — publishing
an empty pack would give the assistant nothing to answer from while still
sounding perfectly confident, which is the exact failure the freshness banner
exists to prevent.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ask_context  # noqa: E402
import tools  # noqa: E402

# Budget the PROMPT, not the file. The published file carries the facts twice
# (readable object + wire string, see ask_context.build_context), so its size is
# nearly double what any question actually pays for — budgeting the file would
# fire on a pack that is perfectly affordable, which is the kind of guard people
# raise until it means nothing. What costs money is the assembled prefix, and
# every question pays it: 20KB is ~5k tokens, comfortably above the ~13KB this
# runs at and low enough that an unbounded new field trips here rather than on
# the bill.
MAX_PROMPT_BYTES = 20_000


def _same(published, fresh):
    """Same pack apart from the build stamp?

    `generated` is deliberately excluded: it moves on every run by design and is
    the one field that never changes an answer. Everything else is derived from
    the source files, so if none of it moved, neither did the data.
    """
    return {k: v for k, v in published.items() if k != "generated"} == \
           {k: v for k, v in fresh.items() if k != "generated"}


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f), None
    except FileNotFoundError:
        return None, "missing"
    except ValueError as exc:
        return None, f"unreadable ({exc})"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be written, write nothing")
    parser.add_argument("--out", default=ask_context.ASK_CONTEXT_PATH)
    args = parser.parse_args(argv)

    sources = {
        "digest": tools.DIGEST_PATH,
        "history": tools.HISTORY_PATH,
        "ledger": tools.LEDGER_PATH,
    }
    data, problems = {}, []
    for name, path in sources.items():
        loaded, err = _read(path)
        data[name] = loaded
        if err:
            problems.append(f"{path} {err}")

    if all(value is None for value in data.values()):
        print(f"[ask] ABORT: no source file could be read ({'; '.join(problems)}).",
              file=sys.stderr)
        return 1

    # A partial read still publishes. Each source answers a different class of
    # question, and losing the history file should cost you the spend trend, not
    # the whole assistant — the pack records what was missing so the model can
    # say so instead of guessing.
    context = ask_context.build_context(data["digest"], data["history"], data["ledger"])
    if problems:
        context["facts"]["unavailable"] = problems
        print(f"[ask] warning: {'; '.join(problems)}", file=sys.stderr)

    blob = json.dumps(context, indent=2, sort_keys=True)
    size = len(blob.encode("utf-8"))
    prompt = len(ask_context.system_for(context, "voice").encode("utf-8"))
    facts = context["facts"]
    print(f"[ask] pack {size:,}B · prompt {prompt:,}B (~{prompt // 4:,} tokens) · "
          f"{len(facts.get('backlog', []))} backlog "
          f"({facts.get('backlog_total', 0)} open) · "
          f"{len(facts.get('delivery', {}).get('recent_shipped', []))} shipped · "
          f"queue {len((facts.get('queue') or {}).get('next', []))}")

    if prompt > MAX_PROMPT_BYTES:
        print(f"[ask] ABORT: the prompt is {prompt:,}B, over the "
              f"{MAX_PROMPT_BYTES:,}B budget. Every question pays for this "
              f"prefix — trim a cap in ask_context.py.", file=sys.stderr)
        return 1

    # Skip the write when only the build stamp moved. This runs hourly behind
    # the ledger refresh, and a pack that rewrote itself every hour would put 24
    # commits a day into a history that is already mostly "Refresh delivery
    # ledger" — burying the changes anyone would actually want to find.
    published, _ = _read(args.out)
    if published is not None and _same(published, context):
        print("[ask] unchanged — nothing to publish.")
        return 0

    if args.dry_run:
        print("[ask] dry run — not written.")
        return 0

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(blob + "\n")
    print(f"[ask] wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
