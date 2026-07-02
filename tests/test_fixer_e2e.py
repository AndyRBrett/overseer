"""End-to-end test of the Fixer pipeline stage — no LLM, no network.

Drives the REAL `tools.run_agent` loop with a scripted fake Anthropic client
that plays the exact tool sequence the Fixer's prompt demands: set up the
workspace, run the test to reproduce the failure, apply the fix, re-run the
test to verify, commit-and-push, open the PR, summarize. Every tool call
executes for real against the local fake remote, so this proves the whole
chain: branch isolation, repro-before-fix, verified-after-fix, push, PR.
"""

import json
from types import SimpleNamespace

import agent_fixer
import tools as o
from tests.conftest import FIXED_CALC, remote_branches, remote_file

REPO = "AndyRBrett/ufc-dashboard"


def _tool_use(name, **input):
    return SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", name=name, input=input,
                                 id=f"toolu_{name}")],
        stop_reason="tool_use",
    )


def _text(text):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason="end_turn",
    )


class ScriptedClient:
    """Mimics anthropic.Anthropic: returns the next scripted response per
    messages.create call, recording each call's kwargs."""

    def __init__(self, responses):
        client = self

        class _Messages:
            @staticmethod
            def create(**kwargs):
                # Snapshot the messages list: run_agent keeps mutating it after
                # this call, and assertions need what the API actually saw.
                client.calls.append({**kwargs, "messages": list(kwargs.get("messages", []))})
                return client.responses.pop(0)

        self.responses = list(responses)
        self.calls = []
        self.messages = _Messages()


class StubTracer:
    def __init__(self):
        self.tool_results = []  # (tool_name, parsed_result_or_error)

    def set_agent(self, name):
        self.agent = name

    def thinking(self, i, text):
        pass

    def assistant_text(self, i, text):
        pass

    def set_digest(self, text):
        pass

    def tool_call(self, i, name, tool_input, content, is_error):
        parsed = content if is_error else json.loads(content)
        self.tool_results.append((name, parsed))


SUMMARY = ("AndyRBrett/ufc-dashboard #7 — add() subtracts. "
           "Root cause: calc.py used '-'; repro test failed, passes after fix. "
           "Action: PR opened.")

SCRIPT = [
    _tool_use("setup_fix_workspace", repo=REPO, issue_number=7),
    _tool_use("run_in_workspace", repo=REPO, command="python test_calc.py"),
    _tool_use("write_workspace_file", repo=REPO, path="calc.py",
              content=FIXED_CALC),
    _tool_use("run_in_workspace", repo=REPO, command="python test_calc.py"),
    _tool_use("commit_and_push", repo=REPO,
              message="fix: add() subtracted instead of adding (fixes #7)"),
    _tool_use("open_pull_request", repo=REPO, title="Fix add() subtraction bug",
              body="Root cause: '-' instead of '+'. Repro test now passes.",
              issue_number=7),
    _text(SUMMARY),
]


def test_run_agent_continues_after_max_tokens_truncation():
    # A response cut off by the output token limit (stop_reason "max_tokens",
    # no tool calls) must NOT end the loop — the live Janitor run "completed"
    # mid-task exactly this way. The loop should nudge and keep going.
    truncated = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="I verified #24 and will now")],
        stop_reason="max_tokens",
    )
    client = ScriptedClient([truncated, _text("done: closed #24")])
    out = o.run_agent(client, agent="T", system="s", tool_names=["list_open_issues"],
                      user_message="go", tracer=StubTracer(), max_iterations=5)
    assert out == "done: closed #24"
    assert client.responses == []  # both scripted responses were consumed
    nudge = client.calls[1]["messages"][-1]
    assert nudge["role"] == "user" and "cut off" in nudge["content"]


def test_run_agent_still_runs_tools_on_truncated_response(fake_remote, monkeypatch):
    # If truncation happened AFTER a complete tool_use block, that call must
    # still be executed rather than nudging (or worse, ending the loop).
    repo, _ = fake_remote
    monkeypatch.setattr(o, "_github", lambda: (_ for _ in ()).throw(AssertionError("unused")))
    truncated_with_tool = SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", name="setup_fix_workspace",
                                 input={"repo": repo, "issue_number": 3}, id="toolu_x")],
        stop_reason="max_tokens",
    )
    tracer = StubTracer()
    out = o.run_agent(client=ScriptedClient([truncated_with_tool, _text("ok")]),
                      agent="T", system="s", tool_names=["setup_fix_workspace"],
                      user_message="go", tracer=tracer, max_iterations=5)
    assert out == "ok"
    (name, result), = tracer.tool_results
    assert name == "setup_fix_workspace" and result["status"] == "ok"


def test_fixer_stage_end_to_end(fake_remote, monkeypatch):
    _, origin = fake_remote
    pulls = []

    class FakeGh:
        def get_repo(self, slug):
            return SimpleNamespace(create_pull=lambda **kw: (
                pulls.append(kw),
                SimpleNamespace(number=42, html_url="https://example.test/pr/42"),
            )[1])

    monkeypatch.setattr(o, "_github", FakeGh)

    client = ScriptedClient(SCRIPT)
    tracer = StubTracer()
    out = agent_fixer.run(client, tracer, bug_output="Filed #7: add() returns wrong sum")

    # The agent loop ran the whole script and returned the final summary.
    assert out == SUMMARY
    assert tracer.agent == "Fixer"
    assert client.responses == []

    # The Fixer only ever saw its own tool subset.
    for call in client.calls:
        names = {t["name"] for t in call["tools"]}
        assert names <= set(agent_fixer.TOOL_NAMES)

    by_name = {}
    for name, result in tracer.tool_results:
        by_name.setdefault(name, []).append(result)

    # Repro-first, verify-after: the same test failed before the fix (real
    # evidence of the bug) and passed after it.
    before, after = by_name["run_in_workspace"]
    assert before["exit_code"] != 0 and "add(2,3) returned -1" in before["output"]
    assert after["exit_code"] == 0 and "ok" in after["output"]

    # The fix landed on an overseer/fix-* branch on the remote; main untouched.
    push = by_name["commit_and_push"][0]
    assert push["status"] == "pushed"
    branch = push["branch"]
    assert branch.startswith("overseer/fix-7-")
    assert branch in remote_branches(origin)
    assert remote_file(origin, branch, "calc.py") == FIXED_CALC
    assert "return a - b" in remote_file(origin, "main", "calc.py")

    # The PR targets main from the fix branch and closes the issue on merge.
    pr = by_name["open_pull_request"][0]
    assert pr["status"] == "opened" and pr["url"] == "https://example.test/pr/42"
    (created,) = pulls
    assert created["head"] == branch and created["base"] == "main"
    assert "Fixes #7" in created["body"]
