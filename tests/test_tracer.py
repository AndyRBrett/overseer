"""Tests for the tracer: digest assembly, tool summaries, blind-spot health."""

import json

from tracer import RunTracer


def _tracer(tmp_path):
    t = RunTracer(jsonl_path=str(tmp_path / "x.jsonl"), html_path=str(tmp_path / "x.html"))
    t.read_tools = {"read_ufc_scraper_status": "UFC dashboard",
                    "read_trading_bot_log": "Trading bot"}
    return t


def test_tool_summary_enhancement_is_readable(tmp_path):
    t = _tracer(tmp_path)
    t.tool_call(0, "propose_enhancement",
                {"repo": "a/b", "title": "Gate entries", "effort": "low", "impact": "high"},
                '{"status": "logged", "number": 3}', False)
    t.write_digest(str(tmp_path / "d.json"))
    row = next(r for r in json.load(open(tmp_path / "d.json"))["timeline"]
               if r["label"].startswith("propose_enhancement"))
    assert "Gate entries" in row["text"] and "low" in row["text"] and "high" in row["text"]
    assert "{" not in row["text"]  # not raw JSON


def test_project_health_ok_resets_and_blind_increments(tmp_path):
    t = _tracer(tmp_path)
    t.prev_projects = {"Trading bot": {"status": "blind", "last_ok": None, "blind_cycles": 1}}
    t.tool_call(0, "read_ufc_scraper_status", {}, '{"status": "ok", "runs_7d": 50}', False)
    t.tool_call(0, "read_trading_bot_log", {}, '{"status": "not_configured"}', False)
    ph = t.project_health()
    assert ph["UFC dashboard"]["status"] == "ok" and ph["UFC dashboard"]["blind_cycles"] == 0
    assert ph["Trading bot"]["status"] == "blind" and ph["Trading bot"]["blind_cycles"] == 2


def test_project_health_idle_on_zero_activity(tmp_path):
    t = _tracer(tmp_path)
    t.prev_projects = {"Trading bot": {"status": "idle", "last_ok": "x", "idle_cycles": 1}}
    # read OK but zero trades + stale published data → IDLE, not OK
    t.tool_call(0, "read_trading_bot_log", {},
                '{"status": "ok", "stale": true, "data": {"trades": 0, "pnl": 0}}', False)
    # UFC: read OK with real activity → OK
    t.tool_call(0, "read_ufc_scraper_status", {}, '{"status": "ok", "runs_7d": 49}', False)
    ph = t.project_health()
    assert ph["Trading bot"]["status"] == "idle"
    assert ph["Trading bot"]["idle_cycles"] == 2     # carried forward + 1
    assert ph["Trading bot"]["last_ok"]              # idle still read fine
    assert ph["UFC dashboard"]["status"] == "ok"


def test_project_health_error_counts_as_blind(tmp_path):
    t = _tracer(tmp_path)
    t.tool_call(0, "read_trading_bot_log", {}, "Tool 'read_trading_bot_log' failed: boom", True)
    ph = t.project_health()
    assert ph["Trading bot"]["status"] == "error" and ph["Trading bot"]["blind_cycles"] == 1


def test_digest_assembly(tmp_path):
    t = _tracer(tmp_path)
    t.set_digest("ISSUES FOUND\n- none")
    t.tool_call(0, "file_issue", {"repo": "a/b", "title": "bug"},
                '{"status": "filed", "number": 7}', False)
    t.finish("completed")
    t.write_digest(str(tmp_path / "d.json"))
    d = json.load(open(tmp_path / "d.json"))
    assert d["counts"]["issues"] == 1
    assert d["status"] == "completed"
    assert d["summary"].startswith("ISSUES FOUND")
    assert "projects" in d and "timeline" in d
