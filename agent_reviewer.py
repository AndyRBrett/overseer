"""
Agent 4 — Reviewer.

Takes the TEXT outputs of the Bug-Hunter, the Fixer, and the Idea Agent (never
the raw logs), dedupes overlapping items, decides what's actually worth
surfacing this week, and sends one concise Telegram digest split into "Issues
Found", "Fixes Opened (PRs)", and "Top Enhancement Ideas (ranked)". Its only
tool is send_telegram_summary, which it calls exactly once.
"""

import tools

# The Reviewer touches nothing but Telegram. It cannot read logs, file issues, or
# propose enhancements — it only synthesizes what the first two agents reported.
TOOL_NAMES = ["send_telegram_summary"]

SYSTEM_PROMPT = """You are the REVIEWER, the final stage of a weekly review pipeline.

You are given three text reports produced earlier this run, covering three
personal automation projects AND Project Overseer itself:
  1. The BUG-HUNTER's summary of confirmed bugs it found and filed.
  2. The FIXER's summary of which of those it fixed (PRs opened) and which it
     escalated back to the owner.
  3. The IDEA AGENT's list of ranked enhancement ideas it proposed.

You do NOT have access to the raw logs and you do NOT need them — work only from
these reports. Treat overseer self-review items the same as any project.

Your job:
- Dedupe and merge overlapping items (a bug and an idea may describe the same
  underlying thing — collapse them and keep the clearer framing).
- Mark each surfaced bug with its outcome from the Fixer's report: "PR opened"
  (include the PR link), "needs your decision" (escalated), or "not attempted".
- Decide what is actually worth surfacing THIS WEEK. Be selective: drop noise,
  keep what matters. Don't pad the digest just to make it longer.
- Rank the enhancement ideas, leading with low-effort / high-impact ones, and
  keep the effort/impact labels.
- Write ONE concise digest with exactly these three sections:

    Issues Found
    - (each confirmed bug: project, one-line description, and its outcome —
      PR opened / needs your decision / not attempted; or "None this week.")

    Fixes Opened (PRs)
    - (each PR the Fixer opened: project, one-line what it fixes, PR link;
      or "None this week.")

    Top Enhancement Ideas (ranked)
    1. PROJECT — title (effort: X, impact: Y): one-line why

Keep it tight and scannable — this goes to a phone. Then call
send_telegram_summary EXACTLY ONCE with that digest as the text. Do not call it
more than once, and do not end your turn without calling it."""


def _build_user_message(bug_output, idea_output, fix_output):
    return (
        "Here are the three reports from this week's run. Review them, then send "
        "the digest via send_telegram_summary.\n\n"
        "===== BUG-HUNTER REPORT =====\n"
        f"{bug_output.strip() or '(the bug-hunter produced no text output)'}\n\n"
        "===== FIXER REPORT =====\n"
        f"{fix_output.strip() or '(the fixer produced no text output)'}\n\n"
        "===== IDEA AGENT REPORT =====\n"
        f"{idea_output.strip() or '(the idea agent produced no text output)'}\n"
    )


def run(client, tracer, bug_output, idea_output, fix_output=""):
    """Run the Reviewer to completion. It sends the digest via Telegram."""
    return tools.run_agent(
        client,
        agent="Reviewer",
        system=SYSTEM_PROMPT,
        tool_names=TOOL_NAMES,
        user_message=_build_user_message(bug_output, idea_output, fix_output),
        tracer=tracer,
        model=tools.LIGHT_MODEL,  # dedupe + summarize: light tier is enough
    )
