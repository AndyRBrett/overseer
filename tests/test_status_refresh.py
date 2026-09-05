"""The between-reviews status refresh (docs/digest.json's top half).

The dashboard carried two clocks: a delivery panel current to the hour sitting
above project health that could be five days old — 0.3h against 117.8h on
2026-09-05 — and the stale half was the half that says whether anything is
broken. Nothing about that was necessary; the stale parts are four GitHub reads
and some arithmetic.

Most of this file is about what the refresh must NOT do.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import refresh_status  # noqa: E402
import tools  # noqa: E402

# A digest as the weekly review leaves it.
PUBLISHED = {
    "generated": "2026-08-31T19:48:15Z",
    "status": "completed",
    "summary": "STALENESS ALERTS\n- nothing\n\nWEEKLY REVIEW\n- the model wrote this",
    "counts": {"tools": 9, "issues": 0, "enhancements": 4},
    "spend": {"total_usd": 0.34},
    "timeline": [{"ts": "14:00", "label": "read_x (investigate)", "text": "ok"}],
    "projects": {"coachvision": {"status": "idle", "idle_cycles": 3, "last_ok": "x"}},
    "output_alerts": [],
}

READINGS = {
    "read_trading_bot_log": {"status": "ok", "data": {"trades": 3}},
    "read_volleyball_results": {"status": "ok", "stale": True, "age_hours": 400,
                                "sla_hours": 192,
                                "data": {"app": "coachvision", "footage_processed": 0}},
    "read_ufc_scraper_status": {"status": "ok", "runs_7d": 9},
    "read_overseer_status": {"status": "ok", "runs_7d": 2},
}

LEDGER = {"entries": [
    {"repo": "A/overseer", "number": 1, "kind": "enhancement", "status": "open",
     "title": "an idea"},
]}


@pytest.fixture
def stub(monkeypatch):
    monkeypatch.setattr(tools, "preflight_github", lambda: {"status": "ok", "detail": "ok"})
    monkeypatch.setattr(tools, "read_all_projects", lambda: dict(READINGS))


def _run(tmp_path, published=None, argv=()):
    path = tmp_path / "digest.json"
    path.write_text(json.dumps(published or PUBLISHED), encoding="utf-8")
    rc = refresh_status.main(["--path", str(path), *argv])
    return rc, json.loads(path.read_text(encoding="utf-8"))


# ── what it must never touch ─────────────────────────────────────────────


def test_the_review_timestamp_is_never_moved(tmp_path, stub):
    # THE ONE THAT MATTERS. scripts/heartbeat.py trips when digest.json's
    # `generated` stops moving, and CLAUDE.md's note is that a review GitHub
    # silently dropped is only detectable BECAUSE of that. A refresh that bumped
    # this field would tell the dead-man's switch the review had run — six times
    # a day, invisibly, forever.
    rc, after = _run(tmp_path)
    assert rc == 0
    assert after["generated"] == PUBLISHED["generated"]


def test_the_heartbeat_still_trips_after_a_refresh(tmp_path, stub):
    # The rule above, proven through the alarm itself rather than through the
    # field it reads. A review old enough to page, refreshed six times since,
    # must still page.
    #
    # The timestamp is RELATIVE, resolved now: pinned to a date it would drift
    # past the threshold on its own and start passing for the wrong reason —
    # the trap CLAUDE.md's "beware time-of-day tests" bullet describes.
    import heartbeat
    stale = datetime.now(timezone.utc) - timedelta(hours=heartbeat.MAX_AGE_HOURS + 24)
    published = dict(PUBLISHED, generated=stale.strftime("%Y-%m-%dT%H:%M:%SZ"))
    assert heartbeat._age_hours(published["generated"]) > heartbeat.MAX_AGE_HOURS

    _, after = _run(tmp_path, published)
    assert heartbeat._age_hours(after["generated"]) > heartbeat.MAX_AGE_HOURS


def test_the_models_own_account_of_its_run_is_preserved(tmp_path, stub):
    # Re-reading a status feed says nothing new about a model run that happened
    # on Monday. A refresh that recomputed these would be inventing a run.
    _, after = _run(tmp_path)
    for key in ("summary", "counts", "spend", "timeline", "status"):
        assert after[key] == PUBLISHED[key], key


def test_an_unknown_key_from_a_future_review_survives(tmp_path, stub):
    published = dict(PUBLISHED, some_new_block={"a": 1})
    _, after = _run(tmp_path, published)
    assert after["some_new_block"] == {"a": 1}


# ── what it does ─────────────────────────────────────────────────────────


def test_it_republishes_health_and_the_ranking(tmp_path, stub, monkeypatch):
    ledger = tmp_path / "shipped.json"
    ledger.write_text(json.dumps(LEDGER), encoding="utf-8")
    monkeypatch.setattr(tools, "LEDGER_PATH", str(ledger))
    _, after = _run(tmp_path)
    assert after["projects"]["coachvision"]["status"] == "stale"
    assert after["attention"][0]["name"] == "coachvision"
    assert after["refreshed"]
    # The plain sentence the page leads with, published rather than composed in
    # JavaScript (invariant 12).
    assert "17 days" in after["headline"]


def test_a_refresh_is_not_a_cycle(tmp_path, stub):
    # blind/stale/idle_cycles mean "consecutive weekly REVIEWS in this state",
    # which is what the nudge threshold of 2 was chosen against. Counting a
    # six-times-a-day refresh would read "stale 42 cycles" by Friday and nudge on
    # the first afternoon.
    published = dict(PUBLISHED,
                     projects={"coachvision": {"status": "stale", "stale_cycles": 2}})
    _, first = _run(tmp_path, published)
    assert first["projects"]["coachvision"]["stale_cycles"] == 2
    # And again, feeding its own output back in, as the cron does.
    _, second = _run(tmp_path, first)
    assert second["projects"]["coachvision"]["stale_cycles"] == 2


def test_a_newly_seen_state_counts_as_one_not_zero(tmp_path, stub):
    # "stale 0 cycles" reads as a bug in the panel.
    _, after = _run(tmp_path)   # published says idle, the read says stale
    assert after["projects"]["coachvision"]["stale_cycles"] == 1


# ── when it declines ─────────────────────────────────────────────────────


def test_a_github_wobble_leaves_the_panel_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "preflight_github",
                        lambda: {"status": "unavailable", "transient": True,
                                 "fatal": True, "detail": "503"})
    monkeypatch.setattr(tools, "read_all_projects",
                        lambda: pytest.fail("read attempted after a failed preflight"))
    rc, after = _run(tmp_path)
    assert rc == 0                      # a wobble is not a red workflow
    assert after == PUBLISHED           # byte-for-byte untouched


def test_every_read_failing_does_not_blank_the_panel(tmp_path, monkeypatch):
    # Four dead projects at once is a broken credential or an unconfigured
    # environment, not four dead projects. Publishing that would replace a
    # healthy panel with a wall of BLIND.
    monkeypatch.setattr(tools, "preflight_github", lambda: {"status": "ok", "detail": "ok"})
    monkeypatch.setattr(tools, "read_all_projects",
                        lambda: {k: {"status": "not_configured"} for k in tools.READ_TOOLS})
    rc, after = _run(tmp_path)
    assert rc == 0 and after == PUBLISHED


def test_a_missing_digest_fails_rather_than_inventing_one(tmp_path, stub):
    assert refresh_status.main(["--path", str(tmp_path / "nope.json")]) == 1


def test_an_unchanged_run_writes_nothing(tmp_path, stub):
    # This runs on the ledger's cron. A file that rewrote itself every few hours
    # would put six commits a day into a history already mostly "Refresh
    # delivery ledger".
    path = tmp_path / "digest.json"
    path.write_text(json.dumps(PUBLISHED), encoding="utf-8")
    refresh_status.main(["--path", str(path)])
    first = path.read_text(encoding="utf-8")
    refresh_status.main(["--path", str(path)])
    after = json.loads(path.read_text(encoding="utf-8"))
    # Only the stamp may differ, and only if something else did too.
    assert json.loads(first)["projects"] == after["projects"]
    assert json.loads(first)["refreshed"] == after["refreshed"]


def test_dry_run_writes_nothing(tmp_path, stub):
    _, after = _run(tmp_path, argv=["--dry-run"])
    assert after == PUBLISHED


def test_the_cron_runs_it_and_publishes_the_digest():
    import yaml
    wf = yaml.safe_load(Path(".github/workflows/ledger-refresh.yml").read_text(encoding="utf-8"))
    steps = wf["jobs"]["refresh"]["steps"]
    runs = " ".join(str(s.get("run", "")) for s in steps)
    assert "scripts/refresh_status.py" in runs
    # Publishing it too, or the refresh runs and the page never sees it.
    assert "docs/digest.json" in runs
    # After the ledger: the attention ranking counts open ideas off it.
    order = [i for i, s in enumerate(steps)
             if "refresh_ledger.py" in str(s.get("run", ""))
             or "refresh_status.py" in str(s.get("run", ""))]
    assert order == sorted(order) and len(order) == 2
