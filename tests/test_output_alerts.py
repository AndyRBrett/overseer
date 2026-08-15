"""Tests for the silent-agent guard and the Idea Agent's filing contract.

The Idea Agent filed 6, 7, 7 enhancements on three consecutive runs, then 0 on
the next two — while still writing a confident list of ideas in its final
message. No error, no failed tool, no visible symptom. The cause was the dedupe
block added between those runs, which the agent read as permission to report
instead of file.

Two things are pinned here: the prompt must say filing is the deliverable, and
the run must raise an alarm when an agent talks without acting — so the next
instance of this is caught by the pipeline rather than by comparing history
counts against a commit timestamp.
"""

import json

from tracer import RunTracer

import agent_idea


def _tracer(tmp_path):
    t = RunTracer(jsonl_path=str(tmp_path / "x.jsonl"), html_path=str(tmp_path / "x.html"))
    t.heavy_model = "claude-opus-4-8"
    return t


def _ran(tracer, agent="Idea-Agent"):
    """Mark an agent as having run, which is what output_alerts keys off."""
    tracer.set_agent_model(agent, "claude-opus-4-8", "heavy")


# ── the prompt's filing contract ─────────────────────────────────────────


def test_prompt_states_that_filing_not_reporting_is_the_deliverable():
    p = agent_idea.build_system_prompt("(none)")
    assert "FILING IS THE DELIVERABLE" in p
    # The specific failure: ideas that exist only in the closing summary.
    assert "only in your final message" in p
    assert "propose_enhancement" in p


def test_dedupe_block_scopes_which_ideas_not_whether_to_file():
    # The regression was absolutist dedupe language ("treat that list as
    # binding") with no counterweight, which read as "filing is risky".
    p = agent_idea.build_system_prompt("#1 [SHIPPED] a thing")
    assert "never tells you to stop filing" in p
    assert "NEXT\nbest one" in p or "NEXT best one" in p
    assert "not to propose nothing" in p


def test_known_work_is_still_injected():
    p = agent_idea.build_system_prompt("#42 [SHIPPED] the exact thing")
    assert "#42 [SHIPPED] the exact thing" in p
    assert "{known_work}" not in p and "{project_block}" not in p


def test_propose_enhancement_is_actually_available_to_the_agent():
    # A prompt demanding a tool the agent was never given would fail silently
    # in exactly the same way.
    assert "propose_enhancement" in agent_idea.TOOL_NAMES


# ── the silent-agent guard ───────────────────────────────────────────────


def test_an_agent_that_files_nothing_raises_an_alert(tmp_path):
    t = _tracer(tmp_path)
    _ran(t)
    alerts = t.output_alerts()
    assert len(alerts) == 1
    assert alerts[0]["agent"] == "Idea-Agent"
    assert "never called propose_enhancement" in alerts[0]["detail"]


def test_no_alert_when_the_agent_did_its_job(tmp_path):
    t = _tracer(tmp_path)
    _ran(t)
    t.tool_call(0, "propose_enhancement", {"repo": "a/b", "title": "x"},
                '{"status": "logged"}', False)
    assert t.output_alerts() == []


def test_a_failed_filing_still_counts_as_silent(tmp_path):
    # The counter only increments on success, so an agent whose every call
    # errored has still delivered nothing and must still be flagged.
    t = _tracer(tmp_path)
    _ran(t)
    t.tool_call(0, "propose_enhancement", {"repo": "a/b", "title": "x"},
                "Tool 'propose_enhancement' failed: 403", True)
    assert len(t.output_alerts()) == 1


def test_an_agent_that_never_ran_is_not_reported_as_silent(tmp_path):
    # A run that aborted before the Idea Agent started has a different problem;
    # claiming it went quiet would be a false alarm.
    t = _tracer(tmp_path)
    assert t.output_alerts() == []


def test_alert_reaches_the_dashboard_payload(tmp_path):
    t = _tracer(tmp_path)
    _ran(t)
    t.finish("completed")
    t.write_digest(str(tmp_path / "d.json"))
    payload = json.load(open(tmp_path / "d.json"))
    assert payload["output_alerts"][0]["agent"] == "Idea-Agent"


def test_alert_is_printed_at_the_end_of_the_run(tmp_path, capsys):
    t = _tracer(tmp_path)
    _ran(t)
    t.finish("completed")
    out = capsys.readouterr().out
    assert "WARNING" in out and "Idea-Agent" in out
