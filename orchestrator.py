"""
Orchestrator — runs the four-agent overseer pipeline sequentially.

  Agent 1 (Bug-Hunter) runs to completion
    → Agent 2 (Fixer) takes the Bug-Hunter's report, fixes what it can in a
      repo clone (repro test → fix → verify), and opens PRs; issues that need
      an owner decision stay as plain issues with its findings commented
      → Agent 3 (Idea Agent) runs to completion
        → all three text outputs are passed as context to Agent 4 (Reviewer)
          → Agent 4 sends the weekly Telegram digest

Each agent is its own client.messages.create tool-use loop (see
tools.run_agent), reusing the shared TOOL_FUNCTIONS dispatch. The whole run is
recorded by a single RunTracer, which streams every agent's reasoning and tool
calls to the terminal and writes the HTML report + docs/digest.json afterwards.

Run weekly on a cron (GitHub Actions or local crontab):

    python orchestrator.py            # for real — files issues, sends Telegram
    python orchestrator.py --dry-run  # intercept all mutations, print instead
"""

import argparse
import os

import agent_bug_hunter
import agent_fixer
import agent_idea
import agent_reviewer
import tools
from tracer import RunTracer


def run_pipeline(dry_run=False):
    if dry_run:
        tools.set_dry_run(True)
        print("\n*** DRY RUN — file_issue, propose_enhancement, "
              "send_telegram_summary, and the fixer's push / open_pull_request "
              "/ comment_on_issue are intercepted. Nothing will hit GitHub "
              "or Telegram. ***\n")

    import anthropic
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    tools.reset_fix_run()  # fresh per-run PR budget for the Fixer

    tracer = RunTracer()
    tracer.read_tools = tools.READ_TOOLS
    tracer.prev_projects = tools.load_prev_projects()
    tracer.start()
    status = "completed"

    try:
        # 1 → 2 → 3 → 4, strictly sequential; each agent finishes before the next.
        bug_output = agent_bug_hunter.run(client, tracer)
        fix_output = agent_fixer.run(client, tracer, bug_output)
        idea_output = agent_idea.run(client, tracer)
        agent_reviewer.run(client, tracer, bug_output, idea_output, fix_output)
    except Exception as exc:  # noqa: BLE001 — record, render, then re-raise
        status = f"crashed: {exc}"
        tracer.finish(status)
        tracer.write()
        tracer.write_digest(tools.DIGEST_PATH)
        tracer.write_history(tools.HISTORY_PATH, tools.HISTORY_MAX_RUNS)
        raise
    finally:
        tools.cleanup_workspaces()  # never leave fixer clones behind

    tracer.finish(status)
    tracer.write()
    tracer.write_digest(tools.DIGEST_PATH)
    tracer.write_history(tools.HISTORY_PATH, tools.HISTORY_MAX_RUNS)


def main():
    parser = argparse.ArgumentParser(description="Run the four-agent overseer pipeline.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the full pipeline but intercept every mutating tool (file_issue, "
             "propose_enhancement, send_telegram_summary, and the fixer's push / "
             "open_pull_request / comment_on_issue) so they print what WOULD "
             "happen instead of touching GitHub or Telegram.",
    )
    args = parser.parse_args()
    run_pipeline(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
