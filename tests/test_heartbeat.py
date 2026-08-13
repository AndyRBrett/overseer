"""Tests for the dead-man's-switch heartbeat (overseer #13 / #17).

The case these exist for is the four-week silent outage of 2026-07-20 → 08-10:
the weekly Action reported success while an expired PAT 401'd every tool call
inside it. `test_blind_run_alerts_even_when_fresh` replays that exact digest
shape and asserts the alarm fires — a fresh timestamp must not buy a pass.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import heartbeat as hb  # noqa: E402


def _ts(hours_ago):
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _digest(hours_ago=1, tools=20, errors=0, status="completed"):
    return {"generated": _ts(hours_ago), "status": status,
            "counts": {"tools": tools, "errors": errors}}


def test_healthy_run_passes():
    healthy, alerts = hb.check_digest(_digest(hours_ago=2))
    assert healthy is True
    assert alerts == []


def test_missed_weekly_run_alerts():
    # Two weeks with no completed review — the cron lapsed.
    healthy, alerts = hb.check_digest(_digest(hours_ago=336))
    assert healthy is False
    assert any("missed its schedule" in a for a in alerts)


def test_one_late_run_is_within_slack():
    # 168h is exactly one weekly cycle; the 192h threshold leaves a day of slack
    # so a merely-late run doesn't page anyone.
    healthy, _ = hb.check_digest(_digest(hours_ago=170))
    assert healthy is True


def test_blind_run_alerts_even_when_fresh():
    # THE REGRESSION TEST. This is the 2026-08-10 digest: generated minutes ago,
    # status "completed", workflow green — and 20 of 21 tool calls failed on an
    # expired token. Freshness must not excuse blindness.
    healthy, alerts = hb.check_digest(_digest(hours_ago=1, tools=21, errors=20))
    assert healthy is False
    assert any("BLIND" in a for a in alerts)
    assert any("token" in a for a in alerts)


def test_partial_errors_below_threshold_pass():
    # A couple of flaky calls in an otherwise good run is not an outage.
    healthy, _ = hb.check_digest(_digest(tools=20, errors=2))
    assert healthy is True


def test_unparseable_timestamp_is_not_treated_as_fresh():
    # A digest we cannot date is one we cannot vouch for — fail closed.
    for bad in (None, "", "not-a-date"):
        healthy, alerts = hb.check_digest({"generated": bad, "counts": {}})
        assert healthy is False
        assert any("cannot confirm" in a for a in alerts)


def test_crashed_status_alerts():
    healthy, alerts = hb.check_digest(_digest(status="crashed: boom"))
    assert healthy is False
    assert any("did not complete cleanly" in a for a in alerts)


def test_multiple_faults_are_all_reported():
    # Stale AND blind AND crashed — the operator should see every reason at once,
    # not just the first one that tripped.
    healthy, alerts = hb.check_digest(
        _digest(hours_ago=400, tools=10, errors=9, status="crashed: boom"))
    assert healthy is False
    assert len(alerts) == 3


def test_missing_digest_file_fails_loudly(tmp_path, monkeypatch):
    # Never published a digest at all → exit 1, not a silent pass.
    monkeypatch.setattr(hb, "DIGEST_PATH", str(tmp_path / "nope.json"))
    monkeypatch.setattr(hb, "send_telegram", lambda *_: None)
    assert hb.main() == 1


def test_corrupt_digest_file_fails_loudly(tmp_path, monkeypatch):
    bad = tmp_path / "digest.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(hb, "DIGEST_PATH", str(bad))
    monkeypatch.setattr(hb, "send_telegram", lambda *_: None)
    assert hb.main() == 1


def test_main_returns_zero_on_healthy_digest(tmp_path, monkeypatch):
    good = tmp_path / "digest.json"
    good.write_text(json.dumps(_digest()), encoding="utf-8")
    monkeypatch.setattr(hb, "DIGEST_PATH", str(good))
    monkeypatch.setattr(hb, "send_telegram", lambda *_: None)
    assert hb.main() == 0


def test_telegram_failure_does_not_mask_the_exit_code(tmp_path, monkeypatch):
    # The non-zero exit is the signal of record; a dead Telegram must not turn a
    # failing check into a passing one.
    stale = tmp_path / "digest.json"
    stale.write_text(json.dumps(_digest(hours_ago=400)), encoding="utf-8")
    monkeypatch.setattr(hb, "DIGEST_PATH", str(stale))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "y")

    def boom(*_args, **_kwargs):
        raise OSError("network down")

    monkeypatch.setattr(hb.urllib.request, "urlopen", boom)
    assert hb.main() == 1


def test_real_committed_digest_is_healthy():
    # Guards the live file: if docs/digest.json in the repo would trip the alarm,
    # the heartbeat workflow is about to go red and we should know here first.
    path = Path(__file__).resolve().parent.parent / "docs" / "digest.json"
    healthy, alerts = hb.check_digest(json.loads(path.read_text(encoding="utf-8")))
    assert healthy is True, f"committed digest would fail the heartbeat: {alerts}"
