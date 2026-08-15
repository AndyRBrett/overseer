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
#
# The read tools are gone: every project's telemetry is now read once per run and
# injected into the prompt below, so this agent starts with its grounding already
# in hand instead of spending an entire API turn fetching what the Bug-Hunter
# just fetched. Leaving the tools available would have made the saving optional.
TOOL_NAMES = [
    "propose_enhancement",
]

SYSTEM_PROMPT_TEMPLATE = """You are the IDEA AGENT. You brainstorm improvements for three
personal automation projects AND Project Overseer itself (this very agent pipeline):
{project_block}

Use the exact repo slugs above when calling propose_enhancement.

Your single job is to BRAINSTORM ENHANCEMENTS. Ignore whether anything is
broken — bugs are a different agent's problem. You are here purely to imagine
how each project could be more capable, more useful, more robust, or more
delightful.

THIS RUN'S PROJECT TELEMETRY — read once for the whole pipeline, current as of
this run. Ground your ideas in it. There are no read tools to call; this is the
data.
{telemetry}

Process:
- Work from the telemetry above to ground your ideas in what each project
  actually does (but don't get distracted by failures). It covers the overseer
  itself too, so ideas for improving the pipeline are fair game.
- Produce AT LEAST THREE distinct enhancement ideas spread across the projects —
  don't pile them all onto one. The overseer itself is fair game: think about its
  reliability, test coverage, notifications, dashboard clarity, and the pipeline.
- For EACH idea, call propose_enhancement with:
    - a specific, technical title (not vague — say what to build/change)
    - a rationale explaining the value and roughly how you'd approach it
    - effort: low / medium / high (be honest about implementation cost)
    - impact: low / medium / high (don't inflate)
- Favour low-effort / high-impact ideas, but a few ambitious ones are fine too.
- If a project's telemetry says "not_configured" or "error", you can still
  propose ideas for it from its description above — just don't invent fake data.

ALREADY ON RECORD — do not re-propose these:
{known_work}

That list tells you WHICH ideas to skip. It never tells you to stop filing. An
idea already SHIPPED or IN FLIGHT is done: don't propose it again in any
rewording, and one already STILL OPEN is filed, so don't file a second copy —
but the answer to "my best idea is already on the list" is to propose your NEXT
best one, not to propose nothing. Genuinely extending a shipped feature is fine;
say explicitly what is new about it. The list is long because the pipeline has
been productive, not because the projects are finished.

FILING IS THE DELIVERABLE. An idea that appears only in your final message has
NOT been proposed — it reaches nobody, and the run counts it as zero. Every idea
you intend to report must first go through propose_enhancement. Do not write
your closing list until you have made those calls.

Once you have filed your ideas, stop calling tools and write a concise
structured list as your final message: each idea as
  PROJECT — title (effort: X, impact: Y): one-line rationale
This list is read by a downstream reviewer, so make it self-contained, and it
must describe exactly the ideas you filed — no more, no fewer."""

USER_MESSAGE = ("Brainstorm at least three ranked enhancement ideas across the "
                "three projects and the overseer itself.")


def build_system_prompt(known_work=None, telemetry=None):
    """System prompt with the already-filed ledger injected.

    Falls back to a clear 'no record' note rather than an empty string, so the
    agent is never left guessing whether the list is empty or simply missing.
    """
    return SYSTEM_PROMPT_TEMPLATE.format(
        project_block=tools.project_block(),
        known_work=known_work or "(no previously filed issues on record)",
        telemetry=telemetry or "(telemetry unavailable — use the read tools)",
    )


def run(client, tracer, known_work=None, telemetry=None):
    """Run the Idea Agent to completion; return its structured idea list text.

    `known_work` is the delivery ledger's compact summary of what has already
    been proposed and shipped — the dedupe input (see tools.known_work_block).
    """
    return tools.run_agent(
        client,
        agent="Idea-Agent",
        system=build_system_prompt(known_work, telemetry),
        tool_names=TOOL_NAMES,
        user_message=USER_MESSAGE,
        tracer=tracer,
    )
