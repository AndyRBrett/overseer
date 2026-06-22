"""Tests for the read tools, the shared prompt block, and the dry-run safety
switch.

These guard against a schema change silently skipping a project (self-review #2)
and against the dry-run flag failing to intercept a mutating tool.
"""

import sqlite3
from datetime import datetime, timezone

import tools as o


def test_env_strips_whitespace():
    # A repo slug pasted into a GitHub Variable can carry a trailing CRLF
    # (overseer #3) — _env must trim it so downstream API calls don't 404.
    import os
    os.environ["X_OVERSEER_SLUG"] = "AndyRBrett/volleyball\r\n"
    try:
        assert o._env("X_OVERSEER_SLUG") == "AndyRBrett/volleyball"
    finally:
        del os.environ["X_OVERSEER_SLUG"]
    assert o._env("X_OVERSEER_SLUG") is None
    assert o._env("X_OVERSEER_SLUG", "fallback") == "fallback"


def test_schedule_stale_threshold():
    # A weekly run older than ~8 days means the schedule lapsed (overseer #5).
    assert o._schedule_stale(200) is True
    assert o._schedule_stale(50) is False
    assert o._schedule_stale(None) is False
    assert o._schedule_stale(o.SCHEDULE_STALE_HOURS) is False  # exactly at threshold isn't stale


def test_read_tools_all_registered():
    # Every read tool used for health tracking must be a real, dispatchable tool.
    for name in o.READ_TOOLS:
        assert name in o.TOOL_FUNCTIONS


def test_tool_specs_subset_is_isolated():
    # Each agent only ever sees its own tools — separation of concerns enforced
    # at the schema level. The Bug-Hunter must not see propose_enhancement, and
    # the Idea agent must not see file_issue.
    bug = {t["name"] for t in o.tool_specs(
        ["read_trading_bot_log", "search_existing_issues", "file_issue"])}
    assert "propose_enhancement" not in bug and "file_issue" in bug


def test_trading_not_configured():
    o.PROJECTS["trading_bot"]["db_path"] = None
    assert o.read_trading_bot_log()["status"] == "not_configured"


def test_trading_missing_file_is_error(tmp_path):
    o.PROJECTS["trading_bot"]["db_path"] = str(tmp_path / "nope.db")
    assert o.read_trading_bot_log()["status"] == "error"


def test_trading_parses_aggregates(tmp_path):
    db = tmp_path / "t.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE trades(ts TEXT, pnl REAL)")
    now = datetime.now(timezone.utc).isoformat()
    con.executemany("INSERT INTO trades VALUES (?, ?)", [(now, 5.0), (now, -2.0)])
    con.commit()
    con.close()
    o.PROJECTS["trading_bot"]["db_path"] = str(db)
    r = o.read_trading_bot_log(days=7)
    assert r["status"] == "ok"
    assert r["trades"] == 2
    assert round(r["win_rate"], 2) == 0.5


def test_volleyball_not_configured():
    o.PROJECTS["volleyball"]["results_path"] = None
    assert o.read_volleyball_results()["status"] == "not_configured"


def test_volleyball_reads_json(tmp_path):
    p = tmp_path / "r.json"
    p.write_text('{"detection_rate": 0.9, "failed_frames": 3}')
    o.PROJECTS["volleyball"]["results_path"] = str(p)
    r = o.read_volleyball_results()
    assert r["status"] == "ok" and r["results"]["detection_rate"] == 0.9


def test_project_block_lists_every_reviewed_project():
    # The Bug-Hunter and Idea agents review the three external projects PLUS
    # Project Overseer itself — all four must appear in the shared prompt block.
    block = o.project_block()
    assert "Crypto trading bot" in block
    assert "Volleyball CV pipeline" in block
    assert "UFC fight card dashboard" in block
    assert "Project Overseer itself" in block


def test_send_telegram_not_configured(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    o.set_dry_run(False)
    assert o.send_telegram_summary("hello")["status"] == "not_configured"


def test_dry_run_intercepts_all_mutations(capsys):
    # The --dry-run safety switch must intercept every mutating tool so nothing
    # reaches GitHub or Telegram while testing changes.
    o.set_dry_run(True)
    try:
        assert o.file_issue("a/b", "bug", "body")["status"] == "dry_run"
        assert o.propose_enhancement("a/b", "idea", "why", "low", "high")["status"] == "dry_run"
        assert o.send_telegram_summary("digest")["status"] == "dry_run"
    finally:
        o.set_dry_run(False)
    out = capsys.readouterr().out
    assert out.count("[DRY-RUN]") == 3
