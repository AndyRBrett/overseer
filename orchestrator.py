"""
Orchestrator — runs the three-agent overseer pipeline sequentially.

  Agent 1 (Bug-Hunter) runs to completion
    → Agent 2 (Idea Agent) runs to completion
      → both text outputs are passed as context to Agent 3 (Reviewer)
        → Agent 3 sends the weekly Telegram digest

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
import agent_idea
import agent_reviewer
import tools
from tracer import RunTracer


def run_pipeline(dry_run=False):
    if dry_run:
        tools.set_dry_run(True)
        print("\n*** DRY RUN — file_issue, propose_enhancement, and "
              "send_telegram_summary are intercepted. Nothing will hit GitHub "
              "or Telegram. ***\n")

    # Credential preflight — BEFORE the agents spend any API budget.
    #
    # A dead GitHub token used to produce a perfectly green run: every tool call
    # 401'd, the agents worked around the errors, a digest was written, and the
    # Action reported success. That happened four weeks running and nothing
    # surfaced it. Now a credential that cannot reach a single repo stops the
    # run loudly instead, and a partially-scoped one warns by name.
    check = tools.preflight_github()
    if check["status"] == "ok":
        print(f"[preflight] {check['detail']}")
    else:
        print(f"[preflight] {check['status'].upper()}: {check['detail']}")
        for slug, state in (check.get("repos") or {}).items():
            if state != "ok":
                print(f"[preflight]   - {slug}: {state}")
    if check.get("fatal"):
        raise SystemExit(
            "\n*** ABORTED: the GitHub credential is unusable, so this review "
            "would read nothing and file nothing.\n"
            f"*** {check['detail']}\n"
            "*** Fix: regenerate the PAT and update the OVERSEER_GITHUB_TOKEN "
            "secret (Settings → Secrets and variables → Actions).\n"
        )

    import anthropic
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    tracer = RunTracer()
    tracer.read_tools = tools.READ_TOOLS
    # The heavy tier is the yardstick the run's spend is measured against.
    tracer.heavy_model = tools.MODEL
    plan = ", ".join(f"{name} → {tools.model_for(name)}" for name in tools.AGENT_NAMES)
    print(f"[model] {plan}")
    tracer.prev_projects = tools.load_prev_projects()
    tracer.start()
    status = "completed"

    # Delivery ledger: read back the issues this pipeline has already filed and
    # what became of them. Fetched ONCE and shared, since both agents need the
    # same view and it costs a handful of API calls.
    #
    # It does two jobs. It feeds the dashboard's record of what the overseer has
    # actually delivered, and — more importantly — it goes into both agents'
    # prompts so they stop re-proposing finished work. Without it the pipeline
    # filed the same schema-validation idea four times across seven weeks and
    # once proposed a feature that was already running in production.
    try:
        ledger = tools.delivery_ledger()
        known_work = tools.known_work_block(ledger)
        t = ledger["totals"]
        print(f"[ledger] {t['proposed']} filed · {t['shipped']} shipped · "
              f"{t['in_flight']} in flight · {t['open']} open · "
              f"{t['duplicate']} duplicate")
    except Exception as exc:  # noqa: BLE001 — a missing ledger must not stop a review
        print(f"[ledger] unavailable ({exc}) — agents run without dedupe context")
        ledger, known_work = None, None

    try:
        # 1 → 2 → 3, strictly sequential; each agent finishes before the next.
        bug_output = agent_bug_hunter.run(client, tracer, known_work=known_work)
        idea_output = agent_idea.run(client, tracer, known_work=known_work)
        agent_reviewer.run(client, tracer, bug_output, idea_output)
    except Exception as exc:  # noqa: BLE001 — record, render, then re-raise
        status = f"crashed: {exc}"
        tracer.finish(status)
        tracer.write()
        tracer.write_digest(tools.DIGEST_PATH)
        tracer.write_history(tools.HISTORY_PATH, tools.HISTORY_MAX_RUNS)
        tools.write_ledger(ledger)
        raise

    tracer.finish(status)
    tracer.write()
    tracer.write_digest(tools.DIGEST_PATH)
    tracer.write_history(tools.HISTORY_PATH, tools.HISTORY_MAX_RUNS)
    tools.write_ledger(ledger)


def main():
    parser = argparse.ArgumentParser(description="Run the three-agent overseer pipeline.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the full pipeline but intercept file_issue, propose_enhancement, "
             "and send_telegram_summary so they print what WOULD happen instead of "
             "touching GitHub or Telegram.",
    )
    args = parser.parse_args()
    run_pipeline(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
