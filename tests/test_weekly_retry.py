"""Tests for the weekly review's outage handling (overseer, 2026-08-17).

The review fires once a week. That Monday it fired into nine seconds of GitHub
503s, aborted at the preflight, and the next attempt was seven days away — no
digest, no push notification, and a run log telling the on-call to regenerate a
credential that was never rejected.

Two mechanisms cover that, and both are only as good as the line between "GitHub
was unreachable" and "something is actually wrong":

  * orchestrator exits 75 (EX_TEMPFAIL) on a transient preflight, which is the
    signal the workflow's retry loop keys on. Any other failure keeps its own
    exit code and is not retried.
  * scripts/weekly_guard.py keeps the catch-up crons from re-running a review
    that already published, so covering a missed Monday doesn't cost a duplicate
    pipeline every healthy one.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import orchestrator  # noqa: E402
import tools  # noqa: E402
import weekly_guard as wg  # noqa: E402

CATCHUP = "0 16 * * 1"
PRIMARY = "0 14 * * 1"


def _ts(hours_ago=1):
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _digest(hours_ago=1, status="completed"):
    return {"generated": _ts(hours_ago), "status": status,
            "counts": {"tools": 20, "errors": 0}}


# ── the orchestrator's exit code ─────────────────────────────────────────

def test_outage_exits_tempfail(monkeypatch, capsys):
    # 75 is the whole contract with the workflow: retry this one.
    monkeypatch.setattr(tools, "preflight_github", lambda: {
        "status": "unavailable", "fatal": True, "transient": True,
        "detail": "GitHub API unreachable after 3 attempt(s)"})
    with pytest.raises(SystemExit) as exit_info:
        orchestrator.run_pipeline()
    assert exit_info.value.code == orchestrator.EX_TEMPFAIL == 75
    out = capsys.readouterr().out
    assert "DEFERRED" in out
    assert "regenerate" not in out, "an outage must not send anyone at the token"


def test_outage_is_refused_before_spending_anything(monkeypatch):
    # The deferral has to happen before the Anthropic client is built, or a
    # "cheap" retry costs three pipelines' worth of tokens.
    monkeypatch.setattr(tools, "preflight_github", lambda: {
        "status": "unavailable", "fatal": True, "transient": True, "detail": "503s"})
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(SystemExit) as exit_info:
        orchestrator.run_pipeline()
    # A KeyError on ANTHROPIC_API_KEY here would mean it got past the preflight.
    assert exit_info.value.code == 75


def test_dead_credential_does_not_get_retried(monkeypatch):
    # Exit 1, not 75: the workflow fails this immediately rather than waiting
    # six minutes to reach the same conclusion three times.
    monkeypatch.setattr(tools, "preflight_github", lambda: {
        "status": "error", "fatal": True,
        "detail": "GitHub auth failed (401): token is expired or revoked"})
    with pytest.raises(SystemExit) as exit_info:
        orchestrator.run_pipeline()
    assert exit_info.value.code != orchestrator.EX_TEMPFAIL
    assert "ABORTED" in str(exit_info.value.code)


# ── the catch-up guard ───────────────────────────────────────────────────

def test_primary_run_never_asks_the_guard():
    # The 14:00 review is the review. It runs even though last week's digest is
    # sitting there, and even though this week's is not.
    run, _ = wg.should_run(PRIMARY, _digest(hours_ago=168))
    assert run is True


def test_manual_dispatch_always_runs():
    # github.event.schedule is empty for workflow_dispatch — a human clicking
    # "Run workflow" means it, even an hour after a successful review.
    run, reason = wg.should_run("", _digest(hours_ago=1))
    assert run is True
    assert "not a catch-up" in reason


def test_catchup_skips_when_today_already_published():
    # The common case by far: the 14:00 run worked, so 16:00 and 18:00 cost a
    # checkout and a file read instead of a second full pipeline.
    run, reason = wg.should_run(CATCHUP, _digest(hours_ago=2))
    assert run is False
    assert "already published" in reason


def test_catchup_runs_when_the_review_failed():
    # THE REGRESSION TEST: 14:00 aborted on an outage, so nothing published
    # today and the catch-up is exactly what should happen.
    run, _ = wg.should_run(CATCHUP, _digest(hours_ago=168))
    assert run is True


def test_catchup_runs_when_the_digest_is_from_an_incomplete_run():
    # A digest written by a run that crashed partway is not this week's update.
    run, _ = wg.should_run(CATCHUP, _digest(hours_ago=1, status="failed"))
    assert run is True


@pytest.mark.parametrize("digest", [None, {}, {"generated": "sometime"},
                                    {"generated": None, "status": "completed"}])
def test_catchup_runs_when_the_digest_is_missing_or_unreadable(digest):
    # When in doubt, run: a redundant review costs money, a skipped one costs
    # the week.
    run, _ = wg.should_run(CATCHUP, digest)
    assert run is True


def test_yesterdays_digest_does_not_count_as_today(monkeypatch):
    # Same-day is the test, not "recent" — a Sunday digest must not satisfy
    # Monday's catch-up.
    now = datetime(2026, 8, 17, 16, 0, tzinfo=timezone.utc)
    digest = {"generated": "2026-08-16T23:59:00Z", "status": "completed"}
    run, _ = wg.should_run(CATCHUP, digest, now=now)
    assert run is True


# ── the workflow contract ────────────────────────────────────────────────

def test_guard_writes_the_workflow_output(monkeypatch, tmp_path, capsys):
    digest_path = tmp_path / "digest.json"
    digest_path.write_text(json.dumps(_digest(hours_ago=2)), encoding="utf-8")
    output = tmp_path / "github_output"
    monkeypatch.setattr(wg, "DIGEST_PATH", str(digest_path))
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("FIRED_BY_SCHEDULE", CATCHUP)

    assert wg.main() == 0, "the guard must never be what fails the workflow"
    assert output.read_text(encoding="utf-8").strip() == "should_run=false"
    assert "skipping" in capsys.readouterr().out


def test_guard_survives_a_missing_digest_file(monkeypatch, tmp_path):
    output = tmp_path / "github_output"
    monkeypatch.setattr(wg, "DIGEST_PATH", str(tmp_path / "nope.json"))
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("FIRED_BY_SCHEDULE", CATCHUP)

    assert wg.main() == 0
    assert output.read_text(encoding="utf-8").strip() == "should_run=true"


def test_catchup_schedules_match_the_workflow():
    # A cron in the workflow but not in CATCHUP_SCHEDULES runs unguarded, which
    # is a duplicate review every Monday. Cheap to assert, easy to forget.
    workflow = (Path(__file__).resolve().parent.parent
                / ".github" / "workflows" / "weekly-review.yml").read_text(encoding="utf-8")
    crons = {line.split("cron:")[1].strip().strip('"')
             for line in workflow.splitlines() if "- cron:" in line}
    assert crons == {PRIMARY} | wg.CATCHUP_SCHEDULES
