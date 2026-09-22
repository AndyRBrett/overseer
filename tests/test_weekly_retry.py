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
  * scripts/weekly_guard.py keeps an automated trigger from re-running a review
    that already published, so covering a missed Monday doesn't cost a duplicate
    pipeline every healthy one.

Since 2026-08-31 the guard is asked by every automated trigger rather than only
the catch-up crons: the review now also has an off-GitHub trigger (a Cloudflare
cron firing repository_dispatch, see worker/overseer-ask.js), and two schedulers
aiming at the same Monday means whichever arrives second must no-op. See
test_external_trigger.py for that trigger's own seams.
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

# What the guard actually keys on now: the event name, not the cron string.
CRON = "schedule"
DISPATCH = "repository_dispatch"
MANUAL = "workflow_dispatch"


def _ts(hours_ago=1):
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _earlier_today(hours_ago=1):
    """A timestamp from earlier TODAY in UTC.

    Not the same thing as `_ts(hours_ago)`, and the difference made this suite
    go red for two hours every night: run at 01:10 UTC, "two hours ago" is
    yesterday, so a test meaning "today's digest is already published" was
    quietly asserting that YESTERDAY's counts as today's — the exact thing
    test_yesterdays_digest_does_not_count_as_today exists to forbid. Clamped to
    just after midnight so the stamp never leaves the current UTC day.
    """
    now = datetime.now(timezone.utc)
    stamp = now - timedelta(hours=hours_ago)
    if stamp.date() != now.date():
        stamp = now.replace(hour=0, minute=1, second=0, microsecond=0)
    return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


def _digest_today(hours_ago=1, status="completed"):
    """A digest published earlier today — what the catch-up guard must skip."""
    return dict(_digest(hours_ago, status), generated=_earlier_today(hours_ago))


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

def test_manual_dispatch_always_runs():
    # A human clicking "Run workflow" means it, even an hour after a successful
    # review. This is the documented way to force a re-review on a day that
    # already has a digest, so it is the one trigger that skips the question.
    run, reason = wg.should_run(MANUAL, _digest_today(hours_ago=1))
    assert run is True
    assert "a human asked" in reason


@pytest.mark.parametrize("event", [CRON, DISPATCH])
def test_an_automated_trigger_skips_when_today_already_published(event):
    # The common case by far: the review already landed, so the later triggers
    # cost a checkout and a file read instead of a second full pipeline.
    run, reason = wg.should_run(event, _digest_today(hours_ago=2))
    assert run is False
    assert "already published" in reason


def test_the_primary_cron_is_guarded_too():
    # CHANGED 2026-08-31, and the reason matters. The 14:00 cron used to run
    # unconditionally on the grounds that it *is* the review. With a Cloudflare
    # cron firing repository_dispatch at 14:05, that became a way to pay twice:
    # a GitHub cron delivered late (30 minutes is routine, 45 has happened)
    # would run a second full pipeline over a digest published minutes earlier.
    run, _ = wg.should_run(CRON, _digest_today(hours_ago=1))
    assert run is False, "a late primary cron must not duplicate a landed review"


@pytest.mark.parametrize("event", [CRON, DISPATCH])
def test_an_automated_trigger_runs_when_the_review_failed(event):
    # THE REGRESSION TEST: nothing published today, so covering it is exactly
    # what should happen — whichever scheduler got through.
    run, _ = wg.should_run(event, _digest(hours_ago=168))
    assert run is True


def test_a_trigger_runs_when_the_digest_is_from_an_incomplete_run():
    # A digest written by a run that crashed partway is not this week's update.
    run, _ = wg.should_run(CRON, _digest(hours_ago=1, status="failed"))
    assert run is True


@pytest.mark.parametrize("digest", [None, {}, {"generated": "sometime"},
                                    {"generated": None, "status": "completed"}])
def test_a_trigger_runs_when_the_digest_is_missing_or_unreadable(digest):
    # When in doubt, run: a redundant review costs money, a skipped one costs
    # the week.
    run, _ = wg.should_run(CRON, digest)
    assert run is True


def test_an_unknown_event_is_guarded_rather_than_waved_through():
    # A trigger nobody thought about must land on the safe side of the fence:
    # asked, not exempt. Exemption is an allowlist of exactly one.
    run, _ = wg.should_run("pull_request", _digest_today(hours_ago=1))
    assert run is False


def test_yesterdays_digest_does_not_count_as_today(monkeypatch):
    # Same-day is the test, not "recent" — a Sunday digest must not satisfy
    # Monday's run.
    now = datetime(2026, 8, 17, 16, 0, tzinfo=timezone.utc)
    digest = {"generated": "2026-08-16T23:59:00Z", "status": "completed"}
    run, _ = wg.should_run(CRON, digest, now=now)
    assert run is True


# ── the workflow contract ────────────────────────────────────────────────

def test_guard_writes_the_workflow_output(monkeypatch, tmp_path, capsys):
    digest_path = tmp_path / "digest.json"
    digest_path.write_text(json.dumps(_digest_today(hours_ago=2)), encoding="utf-8")
    output = tmp_path / "github_output"
    monkeypatch.setattr(wg, "DIGEST_PATH", str(digest_path))
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("FIRED_BY_EVENT", CRON)

    assert wg.main() == 0, "the guard must never be what fails the workflow"
    assert output.read_text(encoding="utf-8").strip() == "should_run=false"
    assert "skipping" in capsys.readouterr().out


def test_guard_survives_a_missing_digest_file(monkeypatch, tmp_path):
    output = tmp_path / "github_output"
    monkeypatch.setattr(wg, "DIGEST_PATH", str(tmp_path / "nope.json"))
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("FIRED_BY_EVENT", CRON)

    assert wg.main() == 0
    assert output.read_text(encoding="utf-8").strip() == "should_run=true"


WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"


def test_the_workflow_passes_the_event_name_to_the_guard():
    # The guard keys on github.event_name. Pass it the wrong thing — the old
    # github.event.schedule, say, which is empty for repository_dispatch — and
    # every dispatch reads as "a human asked", running unguarded. That is a
    # duplicate pipeline on any Monday where both schedulers get through.
    workflow = (WORKFLOWS / "weekly-review.yml").read_text(encoding="utf-8")
    assert "FIRED_BY_EVENT: ${{ github.event_name }}" in workflow
    assert "FIRED_BY_SCHEDULE" not in workflow, "the guard no longer reads a cron string"


def test_only_a_human_is_exempt_from_the_guard():
    # The exemption is an allowlist of one. Widening it is how the 14:00 cron
    # got to run unguarded, which cost a duplicate review once two schedulers
    # pointed at the same Monday.
    assert wg.UNGUARDED_EVENTS == {"workflow_dispatch"}


def test_both_publishers_retry_a_rejected_push():
    # Both workflows commit docs/ to main, so either can have the remote move
    # under it — the hourly ledger refresh writes at :20 and on every PR close,
    # and the review holds its checkout for ~4½ minutes. For the review the
    # stakes are higher than a missed file: "Send push notification" is the step
    # after the publish, so a lost race costs the notification too.
    for name in ("weekly-review.yml", "ledger-refresh.yml"):
        text = (WORKFLOWS / name).read_text(encoding="utf-8")
        assert "for i in 1 2 3" in text, f"{name} does not retry a rejected push"

    # The review rebases, because it holds a complete set of freshly written
    # files and can legitimately replay them over whatever landed (the test
    # below pins that). The refresh cannot: see the next test.
    assert "git pull --rebase" in (WORKFLOWS / "weekly-review.yml").read_text(encoding="utf-8")


def test_the_refresh_rebuilds_a_lost_race_instead_of_rebasing_it():
    # 2026-09-14, run #448: the refresh rebased into the review's publish,
    # conflicted in all three generated files, and its two remaining retries
    # then died on "you have unmerged files" without attempting a push. A
    # generated file has no mergeable half, so the retry recomputes on top of
    # whatever landed instead of replaying a commit over it.
    text = (WORKFLOWS / "ledger-refresh.yml").read_text(encoding="utf-8")
    assert "git reset --hard origin/main" in text
    assert "scripts/rebuild_docs.sh" in text

    # And NOT the review's resolution. `-X theirs` there means "keep the copy
    # being replayed"; here that copy holds a digest rebuilt from the PREVIOUS
    # one, so forcing it over the review's would roll `generated` back a week
    # and tell the dead-man's switch a review had run (invariant 13).
    assert "-X theirs" not in text, "the refresh must not force its digest over the review's"


def test_the_review_wins_a_collision_on_the_files_it_regenerates():
    # digest/history/shipped are written whole by a run that just read GitHub,
    # so a collision is resolved by taking the fresher complete file rather than
    # merging two. During a rebase that is `-X theirs` — "theirs" being the
    # commit under replay, i.e. ours. Drop it and a same-minute collision fails
    # the rebase instead of resolving it.
    text = (WORKFLOWS / "weekly-review.yml").read_text(encoding="utf-8")
    assert "git pull --rebase -X theirs" in text


# ── the dated kill switch (2026-09-20) ───────────────────────────────────
#
# September's API spend ran high and the ask was "skip tomorrow". The only lever
# was disabling this workflow in the Actions tab, which never expires — overseer
# stays dark until someone remembers to flip it back. pause.py makes it a date.
# These pin that the guard honours it, and that it cannot become permanent.

import pause  # noqa: E402

PAUSE_NOW = datetime(2026, 9, 22, 16, 0, tzinfo=timezone.utc)


def _in_days(days):
    return (PAUSE_NOW + timedelta(days=days)).date().isoformat()


def _stale_digest():
    """A digest from a week ago — the state where the guard would otherwise run."""
    stamp = (PAUSE_NOW - timedelta(hours=168)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"generated": stamp, "status": "completed"}


@pytest.mark.parametrize("event", [CRON, DISPATCH])
def test_a_pause_stands_an_automated_trigger_down(event):
    # Note the digest is stale, so without the pause this run is exactly the one
    # the guard exists to let through. The pause outranks that.
    run, reason = wg.should_run(event, _stale_digest(), now=PAUSE_NOW,
                                paused_until=_in_days(7))
    assert run is False
    assert "paused until" in reason


def test_a_human_can_still_force_a_run_while_paused():
    # Same exemption as everywhere else here: a pause is a standing instruction
    # to the schedulers, not a lock against the person who set it.
    run, reason = wg.should_run(MANUAL, _stale_digest(), now=PAUSE_NOW,
                                paused_until=_in_days(7))
    assert run is True
    assert "a human asked" in reason


def test_an_expired_pause_lets_the_review_resume_by_itself():
    # The property the Actions-tab toggle does not have.
    run, _ = wg.should_run(CRON, _stale_digest(), now=PAUSE_NOW,
                           paused_until=_in_days(-1))
    assert run is True


@pytest.mark.parametrize("junk", ["true", "next monday", "2026-13-01", "2126-09-29"])
def test_a_pause_the_guard_cannot_read_does_not_stop_the_review(junk):
    # When in doubt it RUNS. A switch that failed closed would be one typo in a
    # settings field away from silently retiring the whole pipeline.
    run, _ = wg.should_run(CRON, _stale_digest(), now=PAUSE_NOW, paused_until=junk)
    assert run is True


def test_a_pause_does_not_override_an_already_published_digest():
    # Ordering check: both say "skip", but the reason in the log should be the
    # pause, because that is the one a human set and will want confirmed.
    today = PAUSE_NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
    run, reason = wg.should_run(CRON, {"generated": today, "status": "completed"},
                                now=PAUSE_NOW, paused_until=_in_days(3))
    assert run is False
    assert "paused until" in reason


def test_an_undecodable_pause_file_still_leaves_a_decision(monkeypatch, tmp_path):
    # The contract this broke is the module docstring's: "Always exits 0 — this
    # is a decision, not a verdict, and it must never be the thing that fails
    # the workflow." A crash here writes no should_run, so every downstream
    # `if:` is skipped and the review does not run.
    monkeypatch.setattr(wg, "DIGEST_PATH", str(tmp_path / "nope.json"))
    output = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("FIRED_BY_EVENT", CRON)
    bad = tmp_path / "pause"
    bad.write_bytes(b"# caf\xe9\n2026-09-30\n")
    monkeypatch.setattr(pause, "PAUSE_FILE", str(bad))

    assert wg.main() == 0, "the guard must never be what fails the workflow"
    assert output.read_text(encoding="utf-8").strip() == "should_run=true"


def test_the_guard_reads_the_pause_file(monkeypatch, tmp_path):
    # The wiring, not the rule: main() must actually read the file through.
    monkeypatch.setattr(wg, "DIGEST_PATH", str(tmp_path / "nope.json"))
    output = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("FIRED_BY_EVENT", CRON)
    # Relative to the real clock, since main() reads it: far enough out to stay
    # future however long this repo lives, inside MAX_PAUSE_DAYS so it is
    # honoured rather than refused as a mistyped year.
    soon = (datetime.now(timezone.utc) + timedelta(days=30)).date().isoformat()
    pause_file = tmp_path / "pause"
    pause_file.write_text(f"# test\n{soon}\n", encoding="utf-8")
    monkeypatch.setattr(pause, "PAUSE_FILE", str(pause_file))

    assert wg.main() == 0
    assert output.read_text(encoding="utf-8").strip() == "should_run=false"


@pytest.fixture(autouse=True)
def _no_ambient_pause(monkeypatch, tmp_path):
    """Keep the repo's own .overseer-pause out of these tests.

    The pause file is committed when a pause is in force, and main() reads it
    from the working directory — so without this every test that exercises the
    guard end to end silently becomes a test of whatever pause happens to be
    live that week. It broke test_guard_survives_a_missing_digest_file the hour
    the first real pause landed. Tests that DO want a pause point PAUSE_FILE at
    their own fixture and override this.
    """
    monkeypatch.setattr(pause, "PAUSE_FILE", str(tmp_path / "no-pause-file"))
