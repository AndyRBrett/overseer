"""Tests for the implementer's failure taxonomy (.github/workflows/implementer.yml).

Invariant 10: an attempt that died on a dead API key is handed back CLEAN and
retried; one that ran out of turns or couldn't get tests green is benched with
overseer:implement-failed. Getting that line wrong in the benching direction
retires filed issues nobody ever looks at again.

It was wrong. ufc-dashboard's implementer has never once reached the model — the
SDK returns in under 200ms with

    {"subtype": "success", "is_error": true, "num_turns": 1,
     "total_cost_usd": 0, "modelUsage": {}}

— and both issues it was ever handed (#73 on 2026-08-31, #129 on 2026-09-14)
were benched as failed attempts. The classifier only knew how to recognise the
words "credit balance is too low", and those words are in the provider's error
text, which `show_full_output: false` keeps out of the saved report on purpose.

So the rule is no longer a phrase to match but a fact to read: if nothing was
billed, no model call was made, and an attempt that never started cannot have
failed on the merits of the issue.

The classifier lives in shell inside the workflow, so these tests extract that
block between its markers and run it under bash against recorded reports. That
is why the block may only read the report file — no gh, no git.
"""

import json
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parent.parent / ".github/workflows/implementer.yml"
BEGIN = "# --- classify-begin"
END = "# --- classify-end"


def _classifier():
    """The shell between the markers, lifted out of the failure step."""
    steps = yaml.safe_load(WORKFLOW.read_text())["jobs"]["implement"]["steps"]
    script = next(s["run"] for s in steps if s.get("name") == "Hand the issue back on failure")
    body = script.split(BEGIN, 1)[1].split(END, 1)[0]
    # Nothing in the block may reach outside the report file: the test runs it
    # for real, and a stray `gh issue edit` here would edit a real issue.
    code = "\n".join(ln for ln in body.splitlines() if not ln.strip().startswith("#"))
    for forbidden in ("gh ", "git "):
        assert forbidden not in code, f"classifier must not run {forbidden.strip()}"
    return body


def _classify(tmp_path, report):
    """Run the real block against a report; returns (blameless, reason)."""
    path = tmp_path / "claude-execution-output.json"
    if report is not None:
        path.write_text(report if isinstance(report, str) else json.dumps(report, indent=2))
    script = f'report="{path}"\n{_classifier()}\nprintf "%s\\n%s\\n" "$blameless" "$reason"'
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True)
    blameless, reason = out.stdout.split("\n")[:2]
    return bool(blameless), reason


# The exact envelope both ufc-dashboard runs produced, from the run logs.
NEVER_RAN = {
    "type": "result",
    "subtype": "success",
    "is_error": True,
    "duration_ms": 199,
    "num_turns": 1,
    "total_cost_usd": 0,
    "permission_denials_count": 0,
    "modelUsage": {},
}

# A real attempt that worked and then hit the wall: turns spent, money spent.
OUT_OF_TURNS = {
    "type": "result",
    "subtype": "error_max_turns",
    "is_error": True,
    "duration_ms": 402_113,
    "num_turns": 150,
    "total_cost_usd": 1.49,
    "modelUsage": {"claude-sonnet-5": {"inputTokens": 12, "outputTokens": 40_112}},
}


def test_a_run_that_never_reached_the_model_is_blameless(tmp_path):
    blameless, reason = _classify(tmp_path, NEVER_RAN)
    assert blameless
    assert "never reached the model" in reason


def test_out_of_turns_is_still_benched(tmp_path):
    """The failure mode the bench exists for must not get swept up in the fix."""
    blameless, _ = _classify(tmp_path, OUT_OF_TURNS)
    assert not blameless


def test_a_spent_run_with_no_usage_block_is_still_benched(tmp_path):
    """Money spent is money spent, whatever the report does or doesn't carry."""
    spent = dict(OUT_OF_TURNS)
    spent.pop("modelUsage")
    blameless, _ = _classify(tmp_path, spent)
    assert not blameless


def test_the_credit_message_still_wins_and_says_so(tmp_path):
    """The original signal keeps its own wording — it is the specific answer."""
    blameless, reason = _classify(
        tmp_path,
        json.dumps(OUT_OF_TURNS) + "\nAPI Error: Your credit balance is too low",
    )
    assert blameless
    assert "out of credit" in reason


def test_no_report_at_all_is_benched(tmp_path):
    """A step that failed before the agent ever wrote a report.

    Nothing can be concluded, so it takes the conservative side: benched and
    visible, rather than silently retried at ~$1.50 a week forever.
    """
    blameless, _ = _classify(tmp_path, None)
    assert not blameless


def test_compact_json_reads_the_same_as_pretty(tmp_path):
    """The console pretty-prints the envelope; the saved file need not.

    Pinned because the check is a grep, and a grep for `"modelUsage": {}` with
    the space in it would have passed every test above and failed in production.
    """
    blameless, _ = _classify(tmp_path, json.dumps(NEVER_RAN, separators=(",", ":")))
    assert blameless


@pytest.mark.parametrize("zero", ["0", "0.0", "0.00"])
def test_zero_cost_is_recognised_however_it_is_serialised(tmp_path, zero):
    report = json.dumps(NEVER_RAN).replace('"total_cost_usd": 0', f'"total_cost_usd": {zero}')
    blameless, _ = _classify(tmp_path, report)
    assert blameless
