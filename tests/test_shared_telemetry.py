"""Tests for the once-per-run telemetry read shared by both agents.

The Bug-Hunter and the Idea Agent were each opening the same four status files
in the same run — eight reads for four files, and a full API turn per agent
spent fetching what the other had just fetched.

The risk in hoisting those reads out of the agent loops is that project health,
the freshness alerts, the nudges and the history scores are ALL derived from
read-tool `tool_call` events. Moving the reads without recording them would
blank every one of those while the run still reported success. That invariant is
what most of this file is about.
"""

import json

import pytest

import agent_bug_hunter
import agent_idea
import tools
from tracer import RunTracer


OK_TRADING = {"status": "ok", "trades": 3, "signals_evaluated": 12}
STALE_COACH = {"status": "ok", "stale": True, "age_hours": 122.8, "sla_hours": 48,
               "frames_processed": 0}


@pytest.fixture
def stub_reads(monkeypatch):
    """Replace the four read tools with canned payloads."""
    def _stub(**overrides):
        payloads = {
            "read_trading_bot_log": OK_TRADING,
            "read_volleyball_results": STALE_COACH,
            "read_ufc_scraper_status": {"status": "ok", "events_tracked": 11},
            "read_overseer_status": {"status": "ok", "runs_7d": 1},
        }
        payloads.update(overrides)
        funcs = dict(tools.TOOL_FUNCTIONS)
        for name, data in payloads.items():
            funcs[name] = (lambda d=data: d) if not isinstance(data, Exception) else \
                          (lambda d=data: (_ for _ in ()).throw(d))
        monkeypatch.setattr(tools, "TOOL_FUNCTIONS", funcs)
        return payloads
    return _stub


def _tracer(tmp_path):
    t = RunTracer(jsonl_path=str(tmp_path / "x.jsonl"), html_path=str(tmp_path / "x.html"))
    t.read_tools = tools.READ_TOOLS
    t.heavy_model = "claude-opus-4-8"
    return t


# ── reading once ─────────────────────────────────────────────────────────


def test_every_project_is_read_exactly_once(stub_reads, monkeypatch):
    calls = []
    funcs = dict(tools.TOOL_FUNCTIONS)
    for name in tools.READ_TOOLS:
        funcs[name] = lambda n=name: (calls.append(n), {"status": "ok"})[1]
    monkeypatch.setattr(tools, "TOOL_FUNCTIONS", funcs)

    tools.read_all_projects()
    assert sorted(calls) == sorted(tools.READ_TOOLS)
    assert len(calls) == len(set(calls)), "a project was read more than once"


def test_a_raising_read_becomes_an_error_entry_not_a_crash(stub_reads):
    # One unreadable project must not take the review down — the same contract
    # the tools have when an agent calls them directly.
    stub_reads(read_ufc_scraper_status=RuntimeError("502 from GitHub"))
    readings = tools.read_all_projects()
    assert readings["read_ufc_scraper_status"]["status"] == "error"
    assert "502 from GitHub" in readings["read_ufc_scraper_status"]["detail"]
    assert readings["read_trading_bot_log"]["status"] == "ok"  # others unaffected


def test_telemetry_block_carries_the_data_the_agents_reason_over(stub_reads):
    stub_reads()
    block = tools.telemetry_block(tools.read_all_projects())
    # Field names must survive verbatim: the agents' existing reasoning about
    # stale / idle / status was written against the raw tool-result shape.
    assert '"stale": true' in block and '"age_hours": 122.8' in block
    for label in tools.READ_TOOLS.values():
        assert label in block


def test_empty_telemetry_is_stated_rather_than_left_blank():
    assert "no telemetry" in tools.telemetry_block({}).lower()


# ── the invariant: health must survive the move ──────────────────────────


def test_project_health_still_works_when_reads_are_shared(tmp_path, stub_reads):
    stub_reads()
    t = _tracer(tmp_path)
    t.shared_reads(tools.read_all_projects())

    health = t.project_health()
    assert set(health) == set(tools.READ_TOOLS.values())
    assert health["Trading bot"]["status"] == "ok"
    assert health["coachvision"]["status"] == "stale"


def test_freshness_alerts_still_fire_from_shared_reads(tmp_path, stub_reads):
    stub_reads()
    t = _tracer(tmp_path)
    t.shared_reads(tools.read_all_projects())
    alerts = t.freshness_alerts()
    assert [a["name"] for a in alerts] == ["coachvision"]
    assert t.freshness_banner().startswith("STALENESS ALERTS")


def test_a_raising_read_is_graded_error_and_a_returned_error_is_graded_blind(
        tmp_path, stub_reads):
    # These were distinct before the reads moved, and the difference is carried
    # by the tool-result envelope rather than the payload. Collapsing them would
    # have silently reclassified every missing status file as a crash.
    stub_reads(read_ufc_scraper_status=RuntimeError("502"),
               read_trading_bot_log={"status": "error", "detail": "no status file"})
    t = _tracer(tmp_path)
    t.shared_reads(tools.read_all_projects())
    health = t.project_health()
    assert health["UFC dashboard"]["status"] == "error"   # the tool threw
    assert health["Trading bot"]["status"] == "blind"     # the tool reported


def test_the_raised_marker_never_reaches_a_healthy_reading(stub_reads):
    stub_reads()
    for data in tools.read_all_projects().values():
        assert "_tool_raised" not in data


def test_shared_reads_reach_the_digest_and_history(tmp_path, stub_reads):
    stub_reads()
    t = _tracer(tmp_path)
    t.shared_reads(tools.read_all_projects())
    t.finish("completed")
    t.write_digest(str(tmp_path / "d.json"))
    t.write_history(str(tmp_path / "h.json"))

    payload = json.load(open(tmp_path / "d.json"))
    assert payload["rollup"]["total"] == 4
    assert any(a["name"] == "coachvision" for a in payload["rollup"]["attention"])
    run = json.load(open(tmp_path / "h.json"))["runs"][-1]
    assert run["projects"]["coachvision"]["status"] == "stale"


def test_shared_reads_are_attributed_to_telemetry_not_to_an_agent(tmp_path, stub_reads):
    # The dashboard groups its timeline by agent; reads no agent made should not
    # be filed under one.
    stub_reads()
    t = _tracer(tmp_path)
    t.set_agent("Bug-Hunter")
    t.shared_reads(tools.read_all_projects())
    reads = [e for e in t.events if e["kind"] == "tool_call"]
    assert reads and all(e["agent"] == "Telemetry" for e in reads)
    # ...and the caller's agent context is restored afterwards.
    assert t.agent == "Bug-Hunter"


# ── the agents ───────────────────────────────────────────────────────────


def test_idea_agent_no_longer_holds_read_tools(stub_reads):
    # Leaving them available would have made the saving optional. check_duplicate
    # is not one of them: it queries an in-memory index built from the ledger the
    # run already fetched, so it costs no API call and no second telemetry read.
    assert agent_idea.TOOL_NAMES == ["check_duplicate", "propose_enhancement"]
    assert not any(n in agent_idea.TOOL_NAMES for n in tools.READ_TOOLS)


def test_bug_hunter_keeps_read_tools_for_confirming_a_bug():
    # It is the investigator; re-checking a specific feed is part of the job.
    for name in tools.READ_TOOLS:
        assert name in agent_bug_hunter.TOOL_NAMES


@pytest.mark.parametrize("mod", [agent_bug_hunter, agent_idea])
def test_telemetry_is_injected_into_both_prompts(mod, stub_reads):
    stub_reads()
    block = tools.telemetry_block(tools.read_all_projects())
    prompt = mod.build_system_prompt("(none)", block)
    assert '"age_hours": 122.8' in prompt
    assert "{telemetry}" not in prompt and "{known_work}" not in prompt


@pytest.mark.parametrize("mod", [agent_bug_hunter, agent_idea])
def test_missing_telemetry_degrades_with_a_stated_fallback(mod):
    prompt = mod.build_system_prompt("(none)", None)
    assert "telemetry unavailable" in prompt
    assert "{telemetry}" not in prompt
