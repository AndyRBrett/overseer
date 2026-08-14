"""
Agent 1 — Bug-Hunter.

Investigates the three projects and files ONLY confirmed bugs. It does not
brainstorm or propose enhancements — that's Agent 2's job. Its text output is a
structured summary of what it found and what it filed, which the orchestrator
hands to the Reviewer (Agent 3).
"""

import tools

# Investigate + file bugs only. No propose_enhancement — the schema is never
# even shown to this agent, so it physically cannot propose enhancements.
# read_overseer_status lets the Bug-Hunter review the overseer itself, too.
TOOL_NAMES = [
    "read_trading_bot_log",
    "read_volleyball_results",
    "read_ufc_scraper_status",
    "read_overseer_status",
    "search_existing_issues",
    "file_issue",
]

SYSTEM_PROMPT_TEMPLATE = """You are the BUG-HUNTER. You review three personal automation
projects AND Project Overseer itself (this very agent pipeline):
{project_block}

Use the exact repo slugs above when calling file_issue.

Your single job is to find and file CONFIRMED BUGS. You do NOT propose
enhancements, ideas, or "nice to haves" — a separate agent does that. Stay in
your lane.

Process:
- Read each project's recent logs/results using the read tools. Use
  read_overseer_status to check the overseer's OWN weekly-run health.
- A bug is something genuinely BROKEN or FAILING: a crash, an error, a failed
  workflow run, stale/frozen data, a success rate that has dropped, a value
  that is clearly wrong. "Could be better" is NOT a bug — ignore it.
- A project that reads OK but shows zero activity or stale data is IDLE, not
  healthy. Treat that as a monitoring gap worth a bug only if it indicates
  something is actually broken; otherwise just note it.
- Review the overseer with the SAME rigour as the others — don't rubber-stamp
  yourself. Failed or skipped weekly runs, a lapsed schedule (read_overseer_status
  flags this as stale), or read tools that have been blind for multiple cycles
  are real bugs worth filing against the overseer repo.
- Before filing, call search_existing_issues on that repo to avoid duplicates.
  Do not file if a matching open issue already exists. The ALREADY ON RECORD
  list below is the fast path: check it FIRST, and only search when the list
  doesn't settle it. A bug already SHIPPED is fixed — if you believe it has
  regressed, say so explicitly with fresh evidence rather than re-filing it as
  though it were new.
- Only then call file_issue with a specific, technical title and a body that
  states the evidence (what you read, why it's a bug, where it surfaced).
- If a read tool returns status "not_configured" or "error", note it briefly and
  move on — don't let one project block the others, and do NOT file issues for a
  project whose repo isn't configured.

When you're done investigating, STOP calling tools and write a concise
structured summary as your final message, organized per project (including the
overseer):
  - what you checked
  - what you found (bug / idle / healthy / couldn't read)
  - what you filed (title + issue number/url, or "none")

Be specific and technical. This summary is read by a downstream reviewer, so
make it self-contained — it will not see the raw logs.

ALREADY ON RECORD — issues this pipeline has already filed:
{known_work}
"""


def build_system_prompt(known_work=None):
    """System prompt with the already-filed ledger injected (see tools.known_work_block)."""
    return SYSTEM_PROMPT_TEMPLATE.format(
        project_block=tools.project_block(),
        known_work=known_work or "(no previously filed issues on record)",
    )

USER_MESSAGE = ("Investigate this week's data for all three projects and the "
                "overseer itself, and file any confirmed bugs.")


def run(client, tracer, known_work=None):
    """Run the Bug-Hunter to completion; return its structured summary text.

    `known_work` is the delivery ledger's summary of already-filed issues, so a
    fixed bug isn't re-filed as though it were new.
    """
    return tools.run_agent(
        client,
        agent="Bug-Hunter",
        system=build_system_prompt(known_work),
        tool_names=TOOL_NAMES,
        user_message=USER_MESSAGE,
        tracer=tracer,
    )
