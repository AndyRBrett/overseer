"""
Janitor — one-off issue-tracker triage, run manually (not part of the weekly
pipeline).

The Fixer's investigation found that many open enhancement issues across the
project repos are already implemented but were never closed. The Janitor works
through them: for each open issue it verifies IN THE CLONE whether the thing
the issue asks for actually exists (code present, commit history showing when
it landed), and closes verifiably-done issues with a comment citing the commit.
Anything it cannot pin to concrete evidence stays open.

It can read code and history but physically cannot change code: it is not given
write_workspace_file, commit_and_push, or open_pull_request.

Run:  python agent_janitor.py            # closes issues for real
      python agent_janitor.py --dry-run  # prints what it would close
"""

import argparse
import os

import tools
from tracer import RunTracer

MAX_ITERATIONS = int(os.getenv("JANITOR_MAX_ITERATIONS", "60"))

# Read + verify + comment/close only. No code-editing or PR tools.
TOOL_NAMES = [
    "list_open_issues",
    "setup_fix_workspace",
    "run_in_workspace",
    "read_workspace_file",
    "comment_on_issue",
    "close_issue",
]

SYSTEM_PROMPT = f"""You are the JANITOR. You triage the open GitHub issues of these
projects, whose repos have accumulated issues describing work that has since
been implemented but never closed:
{tools.project_block()}

Use the exact repo slugs above in every tool call.

Your single job: find open issues whose request is ALREADY IMPLEMENTED in the
repo, and close each one with evidence. You do not write code, fix bugs, or
propose anything — you only verify and close.

Process, per repo:
1. list_open_issues. Skip issues that are clearly still-open work.
2. setup_fix_workspace once per repo to get a clone (use any issue number; you
   will not be pushing). Investigate with run_in_workspace and
   read_workspace_file: does the code the issue asks for exist? Which commit
   introduced it? `git log --oneline`, `git log -S<keyword>`, and grep are your
   main tools.
3. Close an issue ONLY when you can cite the specific commit SHA (or merged PR)
   that implemented it AND you have seen the implementation in the clone with
   your own tool calls. Call close_issue with a comment that states: what the
   issue asked for, where it is implemented (file + commit SHA), and that the
   Janitor verified it in a fresh clone. The comment is the audit trail — the
   owner will spot-check it.
   Close each issue IN THE SAME TURN you finish verifying it — do not batch
   the closes for the end of the run. Verification without the close is a
   failure mode: an interrupted run would leave verified-done issues open with
   nothing recorded.
4. If a request is only PARTIALLY implemented, or you cannot find clear
   evidence, leave it open. If your findings would still help (e.g. "80% of
   this landed in <sha>, the remaining piece is X"), record them with
   comment_on_issue instead of closing.
5. Never close bug reports about live behaviour (crashes, stale data, failed
   runs) based on code reading alone — code that LOOKS right doesn't prove the
   incident is resolved. Those stay open unless the issue itself says otherwise.

Hard rules:
- Every close_issue call must cite a commit SHA or PR number in its comment.
- When in doubt, leave it open. A wrongly-closed issue erodes trust in the
  whole system; an issue left open costs nothing.
- Do not touch issues labelled or titled as in-progress work by others.

When you're done, STOP calling tools and write a structured summary: per repo,
which issues you closed (number, title, citing commit), which you commented on
but left open, and which you skipped — with one line of reasoning each. Only
claim what a tool result in this session actually showed."""

USER_MESSAGE = (
    "Triage the open issues across all configured project repos. Verify and "
    "close the already-implemented ones with commit evidence; leave everything "
    "doubtful open."
)


def run(client, tracer):
    """Run the Janitor to completion; return its summary text."""
    return tools.run_agent(
        client,
        agent="Janitor",
        system=SYSTEM_PROMPT,
        tool_names=TOOL_NAMES,
        user_message=USER_MESSAGE,
        tracer=tracer,
        max_iterations=MAX_ITERATIONS,
    )


def main():
    parser = argparse.ArgumentParser(description="Verify and close already-implemented issues.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be closed/commented instead of doing it.")
    args = parser.parse_args()
    if args.dry_run:
        tools.set_dry_run(True)
        print("\n*** DRY RUN — close_issue and comment_on_issue are intercepted. ***\n")

    import anthropic
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    # Own report files so a same-directory pipeline run doesn't overwrite them.
    tracer = RunTracer(jsonl_path="janitor_run.jsonl", html_path="janitor_report.html")
    tracer.read_tools = tools.READ_TOOLS
    tracer.start()
    status = "completed"
    try:
        run(client, tracer)
    except Exception as exc:  # noqa: BLE001 — record, render, then re-raise
        status = f"crashed: {exc}"
        raise
    finally:
        tools.cleanup_workspaces()
        tracer.finish(status)
        tracer.write()


if __name__ == "__main__":
    main()
