"""
The dispatch guard on implement.yml.

On 2026-08-31 the 15:00 implement cron did not fire at its hour at all and landed
at 20:30 — five and a half hours late, and only still inside Monday by luck. The
catch-up crons that fixes cannot be added naively: the dispatcher labels what it
hands over, so a second run picks three DIFFERENT issues and doubles a $4.50
Monday. These pin both halves — a covering run happens when the dispatch was
missed, and costs nothing when it wasn't.

Then on 2026-09-07 the half that was missing collected. The guard asked its
question only of the crons it recognised as catch-ups, so when GitHub delivered
the PRIMARY `0 15 * * 1` cron 3h55m late — after a manual run had already handed
over the day's batch — it read "not a catch-up run, this is the dispatch itself"
and bought three more attempts. ~$9 for a Monday designed to cost $4.50. So the
question is now asked of every automated trigger and the classification is gone;
several of these tests exist to keep it gone.
"""

import sys

import pytest

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import implement_guard as ig  # noqa: E402

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"

PRIMARY = "0 15 * * 1"
CATCHUP = "0 17 * * 1"
NOW = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)


def _run(run_id, conclusion="success", at=None, title="Hand filed issues to the implementer"):
    return SimpleNamespace(
        id=run_id, conclusion=conclusion, display_title=title,
        created_at=at or (NOW - timedelta(hours=4)))


def _dry_run(run_id, **kw):
    kw.setdefault("title", f"Hand filed issues to the implementer {ig.DRY_RUN_MARKER}")
    return _run(run_id, **kw)


def _stamp(days_ago=0):
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _digest(days_ago=0):
    """A published digest whose review landed `days_ago` days before NOW."""
    return {"generated": _stamp(days_ago)}


def test_only_a_manual_dispatch_skips_the_question():
    # A human asking for a second batch has seen the first one. Everything else
    # here is a machine that cannot tell.
    run, reason = ig.should_run([_run(1)], NOW, event="workflow_dispatch",
                                digest=_digest())
    assert run is True
    assert "manual dispatch" in reason


@pytest.mark.parametrize("event", ["schedule", "repository_dispatch", "", None])
def test_every_automated_trigger_asks(event):
    # THE 2026-09-07 REGRESSION TEST. The primary cron arrived 3h55m late, after
    # a manual run had already dispatched, and was waved through as "the dispatch
    # itself" — three more attempts, ~$9 for the day. A trigger this guard cannot
    # classify is a trigger that must ask, so the default is asking.
    run, reason = ig.should_run([_run(7)], NOW, event=event, digest=_digest())
    assert run is False, event
    assert "already ran" in reason


def test_the_late_primary_cron_is_the_case_that_cost_the_money():
    # Named for the incident rather than the mechanism: run #6 at 17:32Z was a
    # manual dispatch, run #7 at 18:55Z was `schedule` on `0 15 * * 1`.
    manual = _run(6, at=NOW - timedelta(hours=1))
    run, reason = ig.should_run([manual], NOW, event="schedule", digest=_digest())
    assert run is False
    assert "already ran" in reason


def test_the_guard_holds_no_list_of_catch_up_crons():
    # The list was the bug: it had to be kept in step with the workflow, a test
    # pinned it, and it was still wrong about the one cron that mattered.
    assert not hasattr(ig, "CATCHUP_SCHEDULES")
    assert not hasattr(ig, "CATCHUP_EVENTS")


def test_a_covering_run_still_runs_when_the_dispatch_was_missed():
    # The 2026-09-07 shape: no run created at all for either the 15:00 cron or
    # the 17:00 catch-up, so a later trigger is the week's only automatic chance.
    run, reason = ig.should_run([], NOW, event="repository_dispatch", digest=_digest())
    assert run is True
    assert "no dispatch yet today" in reason


# ── the review has to have landed (2026-09-07, Codex P2) ─────────────────

def test_an_automated_run_waits_for_todays_review():
    # Fire before the review and the dispatcher reads last week's ledger, then
    # spends the week's budget on a stale queue. Reachable now that the
    # Cloudflare poke for implement can arrive while the review is still running.
    run, reason = ig.should_run([], NOW, event="schedule",
                                digest=_digest(days_ago=7))
    assert run is False
    assert "review has not landed" in reason


def test_a_manual_run_does_not_wait_for_the_review():
    # The override has to stay an override, or a stale digest makes the manual
    # path useless on exactly the day someone needs it.
    assert ig.should_run([], NOW, event="workflow_dispatch",
                         digest=_digest(days_ago=7))[0] is True


def test_an_unreadable_digest_proceeds():
    # In doubt it RUNS: a skipped week costs more than a batch off a slightly
    # stale queue, and the one-batch cap still bounds it.
    for digest in (None, {}, {"generated": ""}, {"generated": "not-a-date"}):
        run, reason = ig.should_run([], NOW, event="schedule", digest=digest)
        assert run is True, digest
        assert "could not read the published digest" in reason


def test_the_digest_check_reads_generated_not_refreshed():
    # Invariant 13: `generated` is the REVIEW's timestamp and refresh_status.py
    # writes `refreshed` six times a day precisely so this question keeps its
    # meaning. Reading `refreshed` here would answer "was the file touched",
    # which is true every few hours and tells you nothing.
    stale_review = {"generated": _stamp(days_ago=7), "refreshed": _stamp()}
    assert ig.reviewed_today(stale_review, NOW) is False


def test_the_implement_workflow_listens_for_the_poke():
    # A dispatch nothing listens for is accepted with 204 and reads as healthy.
    workflow = (WORKFLOWS / "implement.yml").read_text(encoding="utf-8")
    assert "repository_dispatch:" in workflow
    assert "types: [implement]" in workflow
    assert "FIRED_BY_EVENT: ${{ github.event_name }}" in workflow, (
        "the guard cannot tell the poke from the dispatch without the event name")


# ── a dry run hands nothing over (2026-09-07) ────────────────────────────

def test_a_dry_run_is_not_todays_dispatch():
    # dry_run DEFAULTS to true on a manual run, so looking at the queue before
    # firing — the careful thing to do — would otherwise disarm every catch-up
    # left in the day. On 09-07 that was the last one.
    run, reason = ig.should_run([_dry_run(11)], NOW, digest=_digest())
    assert run is True
    assert "no dispatch yet today" in reason


def test_a_real_dispatch_after_a_dry_run_still_counts():
    # The pair in the order they actually happen: look, then fire. The catch-up
    # must see the second one.
    runs = [_dry_run(11), _run(12)]
    assert ig.should_run(runs, NOW)[0] is False


def test_the_workflow_marks_its_own_dry_runs():
    # The guard reads the run TITLE because the API does not expose a run's
    # workflow_dispatch inputs. If the marker in the workflow and the one here
    # ever drift, dry runs go back to counting as dispatches — silently.
    workflow = (WORKFLOWS / "implement.yml").read_text(encoding="utf-8")
    assert "run-name:" in workflow
    assert ig.DRY_RUN_MARKER in workflow
    assert "inputs.dry_run &&" in workflow


def test_a_run_with_no_title_counts_as_a_dispatch():
    # Runs from before run-name existed. Conservative in the direction that can
    # only skip a catch-up, never double-spend.
    legacy = SimpleNamespace(id=13, conclusion="success", created_at=NOW - timedelta(hours=4))
    assert ig.should_run([legacy], NOW)[0] is False


def test_a_covering_run_skips_when_today_already_dispatched():
    run, reason = ig.should_run([_run(7)], NOW, digest=_digest())
    assert run is False
    assert "already ran" in reason


def test_a_covering_run_runs_when_the_dispatch_was_missed():
    # The exact 2026-08-31 shape: nothing succeeded today, so a later trigger is
    # the week's only chance to hand anything over.
    run, reason = ig.should_run([], NOW, digest=_digest())
    assert run is True
    assert "no dispatch yet today" in reason


def test_yesterdays_success_does_not_block_today():
    stale = _run(3, at=NOW - timedelta(days=1))
    assert ig.should_run([stale], NOW, digest=_digest())[0] is True


def test_a_failed_dispatch_does_not_count_as_landed():
    # A run that aborted (no token, GitHub down) handed nothing over. Treating it
    # as done would skip the week on the strength of a red run.
    for conclusion in ("failure", "cancelled", "startup_failure", None):
        assert ig.should_run([_run(4, conclusion=conclusion)], NOW,
                             digest=_digest())[0] is True


def test_the_asking_run_does_not_see_itself():
    # The catch-up is in its own workflow's run list while it runs. Without the
    # exclusion it reads its own in-progress row and skips forever.
    me = _run(99, conclusion="success")
    assert ig.should_run([me], NOW, exclude_id=99, digest=_digest())[0] is True
    assert ig.should_run([me], NOW, exclude_id="99", digest=_digest())[0] is True


def test_naive_timestamps_are_read_as_utc():
    naive = SimpleNamespace(id=5, conclusion="success",
                            created_at=datetime(2026, 8, 31, 15, 0))
    assert ig.should_run([naive], NOW)[0] is False


def test_unreadable_run_history_proceeds():
    # When in doubt it runs: a duplicate batch costs money, a skipped week costs
    # the week. The cap in implementation_queue still bounds what it does.
    run, reason = ig.should_run(None, NOW, digest=_digest())
    assert run is True
    assert "could not read this workflow's own run history" in reason


def test_guard_writes_the_workflow_output(monkeypatch, tmp_path):
    output = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("FIRED_BY_EVENT", "schedule")
    monkeypatch.setattr(ig, "published_digest", lambda *a, **k: _digest())
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
    monkeypatch.setenv("FIRED_BY_EVENT", "schedule")

    def boom(*a, **k):
        raise RuntimeError("api.github.com said no")

    monkeypatch.setattr(ig.tools, "_github", boom)
    monkeypatch.setenv("GITHUB_REPOSITORY", "AndyRBrett/overseer")
    assert ig.recent_runs() is None
    assert ig.main() == 0


def workflow_crons(name):
    return {line.split("cron:")[1].strip().strip('"')
            for line in (WORKFLOWS / name).read_text(encoding="utf-8").splitlines()
            if "- cron:" in line}


def test_the_guard_needs_no_list_of_crons_to_stay_in_step_with():
    # This replaces a test that asserted CATCHUP_SCHEDULES matched the workflow's
    # `schedule:` block. That test passed on 2026-09-07 and the guard still let a
    # duplicate batch through, because the list was complete and its PREMISE was
    # wrong: `0 15 * * 1` was excluded on purpose. Nothing to keep in step now —
    # every cron here is guarded by virtue of not being workflow_dispatch.
    assert PRIMARY in workflow_crons("implement.yml")
    assert ig.MANUAL_EVENT == "workflow_dispatch"


def test_every_implement_cron_trails_a_review_cron():
    # A dispatch an hour before the review it depends on reads last week's
    # ledger. Each implement cron must sit an hour behind a weekly-review one.
    review_hours = {int(c.split()[1]) for c in workflow_crons("weekly-review.yml")}
    for cron in workflow_crons("implement.yml"):
        assert int(cron.split()[1]) - 1 in review_hours, cron


def test_the_dispatch_step_is_gated_on_the_guard():
    # The guard is inert unless the step actually reads its output. This is the
    # line that makes the whole module do anything.
    workflow = (WORKFLOWS / "implement.yml").read_text(encoding="utf-8")
    assert "python scripts/implement_guard.py" in workflow
    assert "if: steps.guard.outputs.should_run == 'true'" in workflow
