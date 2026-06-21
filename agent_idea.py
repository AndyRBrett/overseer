"""
Agent 2 — Idea Agent.

Brainstorms enhancement ideas across the three projects, ignoring whether
anything is broken. It produces at least three ideas, each ranked by effort and
impact, via propose_enhancement. Its text output is a structured list that the
orchestrator hands to the Reviewer (Agent 3).
"""

import tools

# Brainstorm only. No file_issue, no search — this agent never decides whether
# something is "broken"; it only proposes improvements.
TOOL_NAMES = [
    "read_trading_bot_log",
    "read_volleyball_results",
    "read_ufc_scraper_status",
    "propose_enhancement",
]

SYSTEM_PROMPT = f"""You are the IDEA AGENT for three personal automation projects:
{tools.project_block()}

Use the exact repo slugs above when calling propose_enhancement.

Your single job is to BRAINSTORM ENHANCEMENTS. Ignore whether anything is
broken — bugs are a different agent's problem. You are here purely to imagine
how each project could be more capable, more useful, more robust, or more
delightful.

Process:
- Read each project's recent results with the read tools to ground your ideas in
  what the project actually does (but don't get distracted by failures).
- Produce AT LEAST THREE distinct enhancement ideas spread across the three
  projects — don't pile them all onto one project.
- For EACH idea, call propose_enhancement with:
    - a specific, technical title (not vague — say what to build/change)
    - a rationale explaining the value and roughly how you'd approach it
    - effort: low / medium / high (be honest about implementation cost)
    - impact: low / medium / high (don't inflate)
- Favour low-effort / high-impact ideas, but a few ambitious ones are fine too.
- If a read tool returns "not_configured" or "error", you can still propose
  ideas for that project from its description above — just don't invent fake data.

When you've proposed your ideas, STOP calling tools and write a concise
structured list as your final message: each idea as
  PROJECT — title (effort: X, impact: Y): one-line rationale
This list is read by a downstream reviewer, so make it self-contained."""

USER_MESSAGE = "Brainstorm at least three ranked enhancement ideas across the three projects."


def run(client, tracer):
    """Run the Idea Agent to completion; return its structured idea list text."""
    return tools.run_agent(
        client,
        agent="Idea-Agent",
        system=SYSTEM_PROMPT,
        tool_names=TOOL_NAMES,
        user_message=USER_MESSAGE,
        tracer=tracer,
    )
