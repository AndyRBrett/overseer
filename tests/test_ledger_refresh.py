"""Tests for the ledger refresh script's failure policy (scripts/refresh_ledger.py).

The case these exist for is 2026-08-17, when the hourly refresh went red because
api.github.com returned 503s for about nine seconds. Nothing was wrong with the
credential, the data, or the code; the panel was 45 minutes old and the next run
an hour later published normally. A workflow whose only job is to keep one small
JSON file fresh should not send a failure email over that — but it must still go
red if the file actually starts going stale, or the alarm is worthless.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import refresh_ledger as rl  # noqa: E402
import tools  # noqa: E402


def _ts(hours_ago):
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _published(entries=3, hours_ago=1):
    return {
        "entries": [{"repo": "A/overseer", "number": n, "status": "shipped"}
                    for n in range(entries)],
        "errors": {},
        "generated": _ts(hours_ago),
        "totals": {"proposed": entries, "shipped": entries, "in_flight": 0,
                   "open": 0, "duplicate": 0, "not_planned": 0},
    }


def _ledger(entries=3, errors=None):
    return {
        "entries": [{"repo": "A/overseer", "number": n, "status": "shipped"}
                    for n in range(entries)],
        "errors": errors or {},
        "totals": {"proposed": entries, "shipped": entries, "in_flight": 0,
                   "open": 0, "duplicate": 0, "not_planned": 0},
    }


@pytest.fixture
def run(monkeypatch, tmp_path):
    """Drive main() against a temp ledger file and a scripted GitHub read.

    Returns (exit_code, written) where `written` is the ledger main() published,
    or None if it left the file alone.
    """
    path = tmp_path / "shipped.json"
    monkeypatch.setattr(tools, "LEDGER_PATH", str(path))
    monkeypatch.setattr(sys, "argv", ["refresh_ledger.py"])
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)

    def go(preflight, ledger=None, published=None):
        if published is not None:
            path.write_text(json.dumps(published), encoding="utf-8")
        written = {}
        monkeypatch.setattr(tools, "preflight_github", lambda: preflight)
        monkeypatch.setattr(tools, "delivery_ledger",
                            lambda **kwargs: ledger if ledger is not None else _ledger())
        monkeypatch.setattr(tools, "write_ledger",
                            lambda led, p=None: written.setdefault("ledger", led))
        return rl.main(), written.get("ledger")

    return go


OK = {"status": "ok", "fatal": False, "login": "AndyRBrett", "repos": {}}
OUTAGE = {"status": "unavailable", "fatal": True, "transient": True,
          "detail": "GitHub API unreachable after 3 attempt(s) (None): 503s."}
DEAD_TOKEN = {"status": "error", "fatal": True,
              "detail": "GitHub auth failed (401): token is expired or revoked"}


# ── the age of the published file ────────────────────────────────────────

def test_age_reads_the_generated_stamp():
    assert rl.published_age_hours(_published(hours_ago=3)) == pytest.approx(3, abs=0.01)


@pytest.mark.parametrize("published", [None, {}, {"generated": "last tuesday"},
                                       {"generated": None}])
def test_undatable_ledger_is_not_fresh(published):
    # None must never read as "0 hours old" — an undatable file is one we
    # cannot vouch for, so the caller treats it as past the limit.
    assert rl.published_age_hours(published) is None


# ── outage handling ──────────────────────────────────────────────────────

def test_brief_outage_skips_without_failing_the_workflow(run, capsys):
    # THE REGRESSION TEST: 503s with a 45-minute-old panel is a skip, not a red X.
    code, written = run(OUTAGE, published=_published(hours_ago=0.75))
    assert code == 0
    assert written is None, "an unreadable run must not rewrite the panel"
    assert "SKIP" in capsys.readouterr().out


def test_sustained_outage_eventually_fails(run):
    # Six hourly misses in a row is no longer a wobble — the panel is stale and
    # somebody should hear about it.
    code, written = run(OUTAGE, published=_published(hours_ago=9))
    assert code == 1
    assert written is None


def test_outage_with_no_published_ledger_fails(run):
    # Nothing to coast on: skipping would leave the panel indefinitely absent.
    code, _ = run(OUTAGE)
    assert code == 1


def test_stale_limit_is_configurable(run, monkeypatch):
    monkeypatch.setattr(rl, "MAX_STALE_HOURS", 24)
    code, _ = run(OUTAGE, published=_published(hours_ago=9))
    assert code == 0


def test_outage_annotates_the_actions_run(run, monkeypatch, capsys):
    # A silent skip is how a broken thing stays broken. Green, but annotated.
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    code, _ = run(OUTAGE, published=_published(hours_ago=1))
    assert code == 0
    assert "::warning title=Ledger refresh skipped::" in capsys.readouterr().out


def test_dead_credential_still_fails_immediately(run, capsys):
    # The July outage's lesson is untouched: a 401 is nobody's transient blip,
    # and no amount of freshness earns it a pass.
    code, written = run(DEAD_TOKEN, published=_published(hours_ago=0.1))
    assert code == 1
    assert written is None
    assert "ABORT" in capsys.readouterr().err


# ── partial reads ────────────────────────────────────────────────────────

def test_partial_read_does_not_publish_a_shrunken_panel(run):
    # Preflight passed, then one repo 503'd mid-walk. delivery_ledger swallows
    # that into `errors` and returns what it got — publishing it would silently
    # drop that project's whole history from the panel.
    code, written = run(OK,
                        ledger=_ledger(entries=1, errors={"A/coachvision": "503"}),
                        published=_published(entries=3, hours_ago=1))
    assert code == 0
    assert written is None


def test_a_shorter_ledger_publishes_when_the_read_was_clean(run):
    # No errors means the drop is real (an issue transferred or deleted), and a
    # clean read is always allowed to publish.
    code, written = run(OK, ledger=_ledger(entries=2),
                        published=_published(entries=3, hours_ago=1))
    assert code == 0
    assert written is not None and len(written["entries"]) == 2


def test_warned_repo_still_publishes_when_nothing_was_lost(run):
    # Errors alone are not disqualifying — the existing behaviour of publishing
    # a partial ledger with a warning is what keeps one dead repo from taking
    # the panel down with it.
    code, written = run(OK,
                        ledger=_ledger(entries=3, errors={"A/coachvision": "503"}),
                        published=_published(entries=3, hours_ago=1))
    assert code == 0
    assert written is not None


def test_healthy_run_publishes(run):
    code, written = run(OK, ledger=_ledger(entries=5),
                        published=_published(entries=3, hours_ago=1))
    assert code == 0
    assert len(written["entries"]) == 5
