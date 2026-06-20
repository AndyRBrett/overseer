"""Tests for the read tools' parsing and the system-prompt assembly.

These guard against a schema change silently skipping a project (self-review #2).
"""

import sqlite3
from datetime import datetime, timezone

import project_overseer as o


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


def test_read_tools_all_registered():
    # Every read tool used for health tracking must be a real, dispatchable tool.
    for name in o.READ_TOOLS:
        assert name in o.TOOL_FUNCTIONS


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


def test_system_prompt_lists_every_project():
    sp = o.build_system_prompt()
    assert "Crypto trading bot" in sp
    assert "Volleyball CV pipeline" in sp
    assert "UFC fight card dashboard" in sp
    assert "Project Overseer itself" in sp
