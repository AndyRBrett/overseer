"""Tests for the tracer: digest assembly, tool summaries, blind-spot health."""

import json
from datetime import datetime, timezone

import tracer
from tracer import RunTracer, _is_idle, _status_score, activity_idle


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


def test_activity_idle_edge_cases():
    assert activity_idle({}) is False                          # no counters → unknown, not idle
    assert activity_idle({"trades": 0, "signals_evaluated": 0}) is True
    assert activity_idle({"trades": 2}) is False
    assert activity_idle({"idle": True}) is True
    assert activity_idle({"unknown_field": 1}) is False        # no known counters
    assert activity_idle("not a dict") is False                # malformed → no crash
    assert activity_idle(None) is False


def test_is_idle_handles_malformed_status_json():
    assert _is_idle({"stale": True}) is True
    assert _is_idle({"idle": True}) is True
    assert _is_idle({"status": "ok", "data": {"footage_processed": 0, "frames_processed": 0}}) is True
    assert _is_idle({"status": "ok", "data": {"trades": 2}}) is False
    assert _is_idle({"status": "ok"}) is False                 # no data, no flags
    assert _is_idle({"status": "ok", "data": None}) is False   # null data → no crash
    # empty-fingerprint event with zero activity (the UFC #14 shape) → idle, no crash
    assert _is_idle({"status": "ok", "data": {"odds_fingerprint": "", "events_tracked": 0}}) is True


def test_project_health_error_counts_as_blind(tmp_path):
    t = _tracer(tmp_path)
    t.tool_call(0, "read_trading_bot_log", {}, "Tool 'read_trading_bot_log' failed: boom", True)
    ph = t.project_health()
    assert ph["Trading bot"]["status"] == "error" and ph["Trading bot"]["blind_cycles"] == 1


def test_status_score_maps_health_to_trend_value():
    # ok is healthy (1.0), idle is half (0.5), error/blind/unknown bottom out (0).
    assert _status_score("ok") == 1.0
    assert _status_score("idle") == 0.5
    assert _status_score("error") == 0.0
    assert _status_score("blind") == 0.0
    assert _status_score(None) == 0.0


def test_write_history_appends_and_scores(tmp_path):
    t = _tracer(tmp_path)
    t.tool_call(0, "read_ufc_scraper_status", {}, '{"status": "ok", "runs_7d": 50}', False)
    t.tool_call(0, "read_trading_bot_log", {}, '{"status": "not_configured"}', False)
    t.finish("completed")
    hpath = tmp_path / "history.json"
    t.write_history(str(hpath))
    runs = json.load(open(hpath))["runs"]
    assert len(runs) == 1
    rec = runs[0]
    assert rec["projects"]["UFC dashboard"]["score"] == 1.0      # ok
    assert rec["projects"]["Trading bot"]["score"] == 0.0        # blind
    assert rec["counts"] == t.counts and rec["status"] == "completed"


def test_write_history_replaces_same_day_run(tmp_path):
    hpath = tmp_path / "history.json"
    # First run today: UFC ok.
    t1 = _tracer(tmp_path)
    t1.tool_call(0, "read_ufc_scraper_status", {}, '{"status": "ok", "runs_7d": 1}', False)
    t1.write_history(str(hpath))
    # Re-run the SAME day: UFC now errors — must replace, not append a 2nd record.
    t2 = _tracer(tmp_path)
    t2.tool_call(0, "read_ufc_scraper_status", {}, "boom", True)
    t2.write_history(str(hpath))
    runs = json.load(open(hpath))["runs"]
    assert len(runs) == 1
    assert runs[0]["projects"]["UFC dashboard"]["score"] == 0.0  # latest state wins


def test_write_history_caps_length(tmp_path):
    hpath = tmp_path / "history.json"
    # Pre-seed more records than the cap, each a distinct date.
    seed = {"runs": [{"date": f"2026-01-{i:02d}", "counts": {}, "projects": {}} for i in range(1, 11)]}
    hpath.write_text(json.dumps(seed))
    t = _tracer(tmp_path)
    t.tool_call(0, "read_ufc_scraper_status", {}, '{"status": "ok"}', False)
    t.write_history(str(hpath), max_runs=5)
    runs = json.load(open(hpath))["runs"]
    assert len(runs) == 5
    assert runs[-1]["date"] == datetime.now(timezone.utc).strftime("%Y-%m-%d")


def test_rollup_flags_idle_project_past_threshold(tmp_path, monkeypatch):
    monkeypatch.setattr(tracer, "NUDGE_CYCLES", 2)
    t = _tracer(tmp_path)
    t.prev_projects = {"Trading bot": {"status": "idle", "last_ok": "x", "idle_cycles": 2}}
    # Trading bot idle for a 3rd cycle → past threshold, must be nudged.
    t.tool_call(0, "read_trading_bot_log", {},
                '{"status": "ok", "stale": true, "data": {"trades": 0}}', False)
    # UFC healthy → counts toward "ok", never appears in attention.
    t.tool_call(0, "read_ufc_scraper_status", {}, '{"status": "ok", "runs_7d": 49}', False)
    r = t.rollup()
    assert r["ok"] == 1 and r["total"] == 2 and r["nudge_threshold"] == 2
    assert [a["name"] for a in r["attention"]] == ["Trading bot"]
    nudged = r["attention"][0]
    assert nudged["status"] == "idle" and nudged["cycles"] == 3 and nudged["nudge"] is True
    assert "idle 3 cycles" in nudged["detail"]


def test_rollup_below_threshold_is_not_nudged(tmp_path, monkeypatch):
    monkeypatch.setattr(tracer, "NUDGE_CYCLES", 3)
    t = _tracer(tmp_path)
    # First idle cycle, threshold 3 → listed for attention but not yet nudged.
    t.tool_call(0, "read_trading_bot_log", {},
                '{"status": "ok", "stale": true, "data": {"trades": 0}}', False)
    r = t.rollup()
    assert r["attention"][0]["cycles"] == 1
    assert r["attention"][0]["nudge"] is False


def test_rollup_sorts_nudged_first(tmp_path, monkeypatch):
    monkeypatch.setattr(tracer, "NUDGE_CYCLES", 2)
    t = _tracer(tmp_path)
    t.prev_projects = {"UFC dashboard": {"status": "blind", "last_ok": None, "blind_cycles": 2}}
    # UFC blind for a 3rd cycle (nudged); Trading bot freshly idle (not nudged).
    t.tool_call(0, "read_ufc_scraper_status", {}, '{"status": "not_configured"}', False)
    t.tool_call(0, "read_trading_bot_log", {},
                '{"status": "ok", "stale": true, "data": {"trades": 0}}', False)
    r = t.rollup()
    # Nudged project sorts ahead of the merely-flagged one.
    assert [a["name"] for a in r["attention"]] == ["UFC dashboard", "Trading bot"]
    assert r["attention"][0]["nudge"] is True and r["attention"][1]["nudge"] is False


def test_rollup_present_in_digest(tmp_path):
    t = _tracer(tmp_path)
    t.tool_call(0, "read_ufc_scraper_status", {}, '{"status": "ok", "runs_7d": 9}', False)
    t.finish("completed")
    t.write_digest(str(tmp_path / "d.json"))
    d = json.load(open(tmp_path / "d.json"))
    assert "rollup" in d
    assert d["rollup"]["ok"] == 1 and d["rollup"]["attention"] == []


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
