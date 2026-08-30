"""Ask the overseer a question. One model call, no tool loop, no agent.

    python ask.py "why has crypto-trading had nothing implemented?"
    python ask.py --format text "what shipped this month?"
    python ask.py --published "what is this costing me?"

THIS IS THE COST DESIGN, and it is the whole reason the assistant is affordable
enough to talk to casually. The three agents in the pipeline each run a tool
loop of up to 25 iterations, which is why a review costs $0.34 and an
implementation $1.49. A question does none of that: the facts are already
assembled by ask_context, so there is exactly one request, no tools, and no
thinking budget to burn. That puts a question at well under a cent — cheap
enough that asking is never the thing you hesitate over.

The temptation, the first time it answers "the snapshot doesn't cover that",
will be to hand it the read tools so it can go and look. Do the arithmetic
first: tools mean a loop, a loop means iterations, and the pipeline's own
numbers say that is 40x this. If live reads are genuinely needed, publish them
into the pack on a schedule instead — that keeps the per-question cost flat.

This CLI and the Cloudflare Worker are two front doors onto the same pack and
the same prompt (ask_context.system_for), so they build byte-identical prefixes
and the prompt cache is shared between them.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import ask_context
import tools
from tracer import price_usd

MODEL = os.getenv("OVERSEER_ASK_MODEL", "claude-sonnet-5")

# Voice answers are two or three sentences; text answers a short paragraph.
# Nothing here needs room to ramble, and output tokens are the expensive half.
MAX_TOKENS = 1024


def build_pack(published=False):
    """The pack, either rebuilt from docs/ or read as the Worker would read it."""
    if published:
        return ask_context.load_published()

    def _load(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, ValueError):
            return None

    return ask_context.build_context(_load(tools.DIGEST_PATH),
                                     _load(tools.HISTORY_PATH),
                                     _load(tools.LEDGER_PATH))


def ask(question, *, fmt="voice", published=False, model=None, client=None):
    """Ask one question. Returns (answer_text, usage_dict)."""
    model = model or MODEL
    context = build_pack(published=published)
    system = ask_context.system_for(context, fmt)

    if client is None:
        import anthropic  # imported late so --dry-run needs no SDK and no key
        client = anthropic.Anthropic()

    response = client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        # No thinking and no tools. The answer is a lookup in a fact block the
        # pipeline already computed — reasoning about it would be paying twice
        # for work Python did for free.
        thinking={"type": "disabled"},
        # The pack is identical question to question, so it caches; the volatile
        # part (the question) goes after it, never inside it. Spelled out as an
        # explicit cached block rather than the top-level `cache_control`
        # shorthand because the Worker has to send this over raw HTTP, and the
        # two only share a cache if they render the same prefix.
        system=[{"type": "text", "text": system,
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user",
                   "content": ask_context.user_turn(question)}],
    )

    answer = "\n".join(b.text for b in response.content
                       if b.type == "text" and b.text.strip())
    usage = getattr(response, "usage", None)
    return answer.strip(), {
        "input": getattr(usage, "input_tokens", 0) or 0,
        "output": getattr(usage, "output_tokens", 0) or 0,
        "cache_write": getattr(usage, "cache_creation_input_tokens", 0) or 0,
        "cache_read": getattr(usage, "cache_read_input_tokens", 0) or 0,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Ask the overseer a question.")
    parser.add_argument("question", nargs="*", help="the question, in plain English")
    parser.add_argument("--format", dest="fmt", choices=("voice", "text"),
                        default="text", help="voice answers are written to be heard")
    parser.add_argument("--published", action="store_true",
                        help="answer from docs/ask-context.json, exactly as the Worker does")
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--dry-run", action="store_true",
                        help="print the assembled prompt and its size; no API call")
    args = parser.parse_args(argv)

    question = " ".join(args.question).strip()
    if not question and not args.dry_run:
        parser.error("ask a question, e.g. python ask.py \"what is in the queue?\"")

    if args.dry_run:
        system = ask_context.system_for(build_pack(published=args.published), args.fmt)
        print(system)
        print(f"\n[ask] {len(system):,} chars (~{len(system) // 4:,} tokens) of prompt, "
              f"model {args.model}", file=sys.stderr)
        return 0

    answer, usage = ask(question, fmt=args.fmt, published=args.published,
                        model=args.model)
    print(answer)

    # Same discipline as the pipeline's spend panel: report what it actually
    # cost, so "is this cheap?" stays a measured question rather than a belief.
    usd = price_usd(args.model, usage)
    cost = f"${usd:.4f}" if usd is not None else "unpriced"
    print(f"\n[ask] {cost} · in {usage['input']} "
          f"(+{usage['cache_read']} cached, {usage['cache_write']} written) "
          f"· out {usage['output']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
