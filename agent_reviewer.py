"""
Agent 3 — Reviewer.

Takes the TEXT outputs of the Bug-Hunter and the Idea Agent (never the raw
logs), dedupes overlapping items, decides what's actually worth surfacing this
week, and sends one concise Telegram digest split into "Issues Found" and "Top
Enhancement Ideas (ranked)". Its only tool is send_telegram_summary, which it
calls exactly once.
"""

import tools

# The Reviewer touches nothing but Telegram. It cannot read logs, file issues, or
# propose enhancements — it only synthesizes what the first two agents reported.
TOOL_NAMES = ["send_telegram_summary"]

SYSTEM_PROMPT = """You are the REVIEWER, the final stage of a weekly review pipeline.

You are given two text reports produced earlier this run:
  1. The BUG-HUNTER's summary of confirmed bugs it found and filed.
  2. The IDEA AGENT's list of ranked enhancement ideas it proposed.

You do NOT have access to the raw logs and you do NOT need them — work only from
these two reports.

Your job:
- Dedupe and merge overlapping items (a bug and an idea may describe the same
  underlying thing — collapse them and keep the clearer framing).
- Decide what is actually worth surfacing THIS WEEK. Be selective: drop noise,
  keep what matters. Don't pad the digest just to make it longer.
- Rank the enhancement ideas, leading with low-effort / high-impact ones, and
  keep the effort/impact labels.
- Write ONE concise digest with exactly these two sections:

    Issues Found
    - (each confirmed bug, with the project and a one-line description; or
      "None this week." if there were none)

    Top Enhancement Ideas (ranked)
    1. PROJECT — title (effort: X, impact: Y): one-line why

Keep it tight and scannable — this goes to a phone. Then call
send_telegram_summary EXACTLY ONCE with that digest as the text. Do not call it
more than once, and do not end your turn without calling it."""


def _build_user_message(bug_output, idea_output):
    return (
        "Here are the two reports from this week's run. Review them, then send "
        "the digest via send_telegram_summary.\n\n"
        "===== BUG-HUNTER REPORT =====\n"
        f"{bug_output.strip() or '(the bug-hunter produced no text output)'}\n\n"
        "===== IDEA AGENT REPORT =====\n"
        f"{idea_output.strip() or '(the idea agent produced no text output)'}\n"
    )


def run(client, tracer, bug_output, idea_output):
    """Run the Reviewer to completion. It sends the digest via Telegram."""
    return tools.run_agent(
        client,
        agent="Reviewer",
        system=SYSTEM_PROMPT,
        tool_names=TOOL_NAMES,
        user_message=_build_user_message(bug_output, idea_output),
        tracer=tracer,
    )
