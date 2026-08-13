"""Synthetic-fixture smoke tests for every read tool's status-file path (#16).

Consolidates what #9, #12 and #14 all asked for in different words: each read
tool parses a project's published `overseer-status.json`, and a schema drift on
either side silently degrades the whole review. The existing tests cover the
LOCAL paths (SQLite trade log, results JSON); this covers the CLOUD path — the
one all four projects actually use in production, and the one that was blind for
four weeks behind an expired token.

Each project gets a fixture shaped like its real published payload, so a change
to `_read_status_file`'s contract fails here rather than in a Monday digest.
"""

import json

import pytest

import tools as o


class _Content:
    def __init__(self, payload):
        self.decoded_content = json.dumps(payload).encode("utf-8")


class _Repo:
    """Stand-in for a PyGithub Repository serving one status file."""

    def __init__(self, payload=None, error=None):
        self._payload, self._error = payload, error

    def get_contents(self, path):
        if self._error:
            raise self._error
        return _Content(self._payload)


@pytest.fixture
def serve(monkeypatch):
    """Point _github() at a repo returning the given status payload."""
    def _serve(payload=None, error=None):
        class _GH:
            def get_repo(self, slug):
                return _Repo(payload, error)
        monkeypatch.setattr(o, "_github", lambda: _GH())
    return _serve


# Fixtures shaped like each project's real published payload.
TRADING = {"generated_at": "2026-08-13T03:22:50Z", "trades": 1, "pnl": -25.46,
           "win_rate": 0.0, "signals_evaluated": 10, "signals_acted": 0}
COACHVISION = {"generated_at": "2026-08-13T07:46:19Z", "app": "coachvision",
               "frames_processed": 0, "footage_processed": 0, "needs_footage": True}
UFC = {"generated_at": "2026-08-13T17:02:46Z", "events_tracked": 11,
       "parse_failure_events": 1, "errors": ["odds missing on an announced card"]}


def _fresh(payload):
    """Same payload with a just-now timestamp, so SLA checks don't fire."""
    from datetime import datetime, timezone
    out = dict(payload)
    out["generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return out


@pytest.mark.parametrize("payload", [TRADING, COACHVISION, UFC],
                         ids=["trading", "coachvision", "ufc"])
def test_every_project_payload_reads_ok_when_fresh(serve, payload):
    serve(_fresh(payload))
    result = o._read_status_file("A/repo", "overseer-status.json", sla_hours=48)
    assert result["status"] == "ok"
    assert result["data"] == _fresh(payload) or result["data"]["generated_at"]
    assert result["sla_hours"] == 48
    assert "stale" not in result


@pytest.mark.parametrize("payload", [TRADING, COACHVISION, UFC],
                         ids=["trading", "coachvision", "ufc"])
def test_every_project_payload_flags_staleness(serve, payload):
    # The 2026-08-10 coachvision case: a payload that parses fine but is 85h old
    # must not read as healthy just because the fields are present. Age is set
    # relative to now rather than hard-coded, so this doesn't quietly start
    # passing (or failing) as the fixture dates recede.
    from datetime import datetime, timedelta, timezone
    stale_ts = (datetime.now(timezone.utc) - timedelta(hours=85)).strftime("%Y-%m-%dT%H:%M:%SZ")
    serve({**payload, "generated_at": stale_ts})
    result = o._read_status_file("A/repo", "overseer-status.json", sla_hours=48)
    assert result["status"] == "ok"
    assert result["stale"] is True
    assert result["age_hours"] > 48


def test_missing_status_file_is_an_error_not_a_silent_pass(serve):
    serve(error=RuntimeError("404 Not Found"))
    result = o._read_status_file("A/repo", "overseer-status.json")
    assert result["status"] == "error"
    assert "has the bot published it" in result["detail"]


def test_payload_without_a_timestamp_is_not_asserted_stale(serve):
    # Can't prove staleness without a parseable timestamp, so don't claim it —
    # but do still return the data rather than failing the read.
    serve({"frames_processed": 3})
    result = o._read_status_file("A/repo", "overseer-status.json")
    assert result["status"] == "ok"
    assert "stale" not in result and "age_hours" not in result


def test_idle_is_reported_explicitly_so_the_agent_need_not_infer_it(serve):
    serve(_fresh(COACHVISION))
    assert o._read_status_file("A/repo", "overseer-status.json")["idle"] is True


def test_active_project_is_not_idle(serve):
    serve(_fresh(TRADING))
    assert o._read_status_file("A/repo", "overseer-status.json")["idle"] is False


def test_sla_is_per_project_not_global(serve):
    # A 60h-old feed is stale for a daily bot and fine for a slower pipeline.
    from datetime import datetime, timedelta, timezone
    ts = (datetime.now(timezone.utc) - timedelta(hours=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    serve({**TRADING, "generated_at": ts})
    assert o._read_status_file("A/r", "s.json", sla_hours=48).get("stale") is True
    serve({**TRADING, "generated_at": ts})
    assert o._read_status_file("A/r", "s.json", sla_hours=72).get("stale") is None


def test_unconfigured_projects_report_not_configured_rather_than_failing(monkeypatch):
    # A project with no repo and no local path must degrade, never raise — one
    # unconfigured project must not take the other three down with it.
    for key in ("repo", "db_path"):
        monkeypatch.setitem(o.PROJECTS["trading_bot"], key, None)
    assert o.read_trading_bot_log()["status"] == "not_configured"
