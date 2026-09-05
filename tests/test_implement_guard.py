"""
The catch-up guard on implement.yml.

On 2026-08-31 the 15:00 implement cron did not fire at its hour at all and landed
at 20:30 — five and a half hours late, and only still inside Monday by luck. The
catch-up crons that fixes cannot be added naively: the dispatcher labels what it
hands over, so a second run picks three DIFFERENT issues and doubles a $4.50
Monday. These pin both halves — the catch-up runs when the dispatch was missed,
and costs nothing when it wasn't.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import implement_guard as ig  # noqa: E402

PRIMARY = "0 15 * * 1"
CATCHUP = "0 17 * * 1"
NOW = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)


def _run(run_id, conclusion="success", at=None):
    return SimpleNamespace(
        id=run_id, conclusion=conclusion, created_at=at or (NOW - timedelta(hours=4)))


def test_the_dispatch_itself_never_asks():
    # The 15:00 run and a manual dispatch ARE the run. Guarding them would mean a
    # re-run of a green dispatch silently does nothing.
    for schedule in (PRIMARY, "", None, "workflow_dispatch"):
        run, reason = ig.should_run(schedule, [_run(1)], NOW)
        assert run is True, schedule
        assert "not a catch-up" in reason


def test_catchup_skips_when_today_already_dispatched():
    run, reason = ig.should_run(CATCHUP, [_run(7)], NOW)
    assert run is False
    assert "already ran" in reason


def test_catchup_runs_when_the_dispatch_was_missed():
    # The exact 2026-08-31 shape: nothing succeeded today, so the catch-up is the
    # week's only chance to hand anything over.
    run, reason = ig.should_run(CATCHUP, [], NOW)
    assert run is True
    assert "no successful dispatch yet today" in reason


def test_yesterdays_success_does_not_block_today():
    stale = _run(3, at=NOW - timedelta(days=1))
    assert ig.should_run(CATCHUP, [stale], NOW)[0] is True


def test_a_failed_dispatch_does_not_count_as_landed():
    # A run that aborted (no token, GitHub down) handed nothing over. Treating it
    # as done would skip the week on the strength of a red run.
    for conclusion in ("failure", "cancelled", "startup_failure", None):
        assert ig.should_run(CATCHUP, [_run(4, conclusion=conclusion)], NOW)[0] is True


def test_the_asking_run_does_not_see_itself():
    # The catch-up is in its own workflow's run list while it runs. Without the
    # exclusion it reads its own in-progress row and skips forever.
    me = _run(99, conclusion="success")
    assert ig.should_run(CATCHUP, [me], NOW, exclude_id=99)[0] is True
    assert ig.should_run(CATCHUP, [me], NOW, exclude_id="99")[0] is True


def test_naive_timestamps_are_read_as_utc():
    naive = SimpleNamespace(id=5, conclusion="success",
                            created_at=datetime(2026, 8, 31, 15, 0))
    assert ig.should_run(CATCHUP, [naive], NOW)[0] is False


def test_unreadable_run_history_proceeds():
    # When in doubt it runs: a duplicate batch costs money, a skipped week costs
    # the week. The cap in implementation_queue still bounds what it does.
    run, reason = ig.should_run(CATCHUP, None, NOW)
    assert run is True
    assert "could not read" in reason


def test_guard_writes_the_workflow_output(monkeypatch, tmp_path):
    output = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("FIRED_BY_SCHEDULE", CATCHUP)
    # main() reads the REAL clock, so this run has to be dated against the real
    # clock too. It was pinned to NOW (a fixed Monday in August), which meant the
    # test passed on the day it was written and has read "no dispatch today"
    # ever since — the same class of bug as the _ts(hours_ago=2) tests that
    # failed for two hours every night after UTC midnight.
    today = datetime.now(timezone.utc).replace(hour=15, minute=0, second=0, microsecond=0)
    monkeypatch.setattr(ig, "recent_runs", lambda *a, **k: [_run(1, at=today)])

    assert ig.main() == 0
    assert output.read_text(encoding="utf-8").strip() == "should_run=false"


def test_guard_never_fails_the_workflow(monkeypatch, tmp_path):
    # It is a decision, not a verdict. A guard that can exit non-zero turns a
    # cost optimisation into a red workflow every Monday.
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "out"))
    monkeypatch.setenv("FIRED_BY_SCHEDULE", CATCHUP)

    def boom(*a, **k):
        raise RuntimeError("api.github.com said no")

    monkeypatch.setattr(ig.tools, "_github", boom)
    monkeypatch.setenv("GITHUB_REPOSITORY", "AndyRBrett/overseer")
    assert ig.recent_runs() is None
    assert ig.main() == 0


WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"


def test_catchup_schedules_match_the_workflow():
    # A cron in the workflow but not in CATCHUP_SCHEDULES dispatches unguarded —
    # three extra attempts at ~$1.50 each, every Monday, for as long as nobody
    # reads the bill.
    workflow = (WORKFLOWS / "implement.yml").read_text(encoding="utf-8")
    crons = {line.split("cron:")[1].strip().strip('"')
             for line in workflow.splitlines() if "- cron:" in line}
    assert crons == {PRIMARY} | ig.CATCHUP_SCHEDULES


def test_every_catchup_trails_a_review_catchup():
    # A dispatch an hour before the review it depends on reads last week's
    # ledger. Each implement cron must sit an hour behind a weekly-review one.
    review = (WORKFLOWS / "weekly-review.yml").read_text(encoding="utf-8")
    review_hours = {int(line.split("cron:")[1].strip().strip('"').split()[1])
                    for line in review.splitlines() if "- cron:" in line}
    for cron in {PRIMARY} | ig.CATCHUP_SCHEDULES:
        assert int(cron.split()[1]) - 1 in review_hours, cron


def test_the_dispatch_step_is_gated_on_the_guard():
    # The guard is inert unless the step actually reads its output. This is the
    # line that makes the whole module do anything.
    workflow = (WORKFLOWS / "implement.yml").read_text(encoding="utf-8")
    assert "python scripts/implement_guard.py" in workflow
    assert "if: steps.guard.outputs.should_run == 'true'" in workflow
