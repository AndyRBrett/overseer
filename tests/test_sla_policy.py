"""Tests for how a project's staleness deadline is chosen.

coachvision publishes weekly (overseer-status.yml, `0 6 * * 1`) and publishes
whether or not footage arrived — precisely so the overseer can tell idle from
broken. It was graded against the shared 48h default, so from every Wednesday
onward it read STALE for behaving exactly as designed, and five consecutive runs
raised a correct-looking alarm about a healthy project.

An alert that fires on normal operation is worse than no alert: it trains you to
skim past the panel where the real one will appear. So the deadline is derived
from the publish cadence rather than picked by feel, and these tests pin both
the rule and the cadences it was applied to.
"""

import pytest

import tools


# ── the rule ─────────────────────────────────────────────────────────────


def test_the_rule_is_one_missed_cycle_plus_grace():
    assert tools.sla_for_interval(tools.DAILY) == tools.DAILY + tools.SLA_GRACE_HOURS
    assert tools.sla_for_interval(tools.WEEKLY) == tools.WEEKLY + tools.SLA_GRACE_HOURS


def test_the_rule_reproduces_the_conventions_already_in_the_repo():
    # 48h for a daily feed and 192h for a weekly one were both already in use.
    # A rule that contradicted them would be a redesign, not a formalisation.
    assert tools.sla_for_interval(tools.DAILY) == 48
    assert tools.sla_for_interval(tools.WEEKLY) == 192


def test_a_faster_publisher_gets_a_tighter_deadline():
    assert tools.sla_for_interval(tools.HOURLY) < tools.sla_for_interval(tools.DAILY)
    assert tools.sla_for_interval(tools.DAILY) < tools.sla_for_interval(tools.WEEKLY)


def test_the_overseer_is_held_to_the_rule_it_applies_to_others():
    # It runs weekly (weekly-review.yml `0 14 * * 1`), so its own missed-schedule
    # threshold must be the weekly deadline, not an independently-chosen number.
    assert tools.SCHEDULE_STALE_HOURS == tools.sla_for_interval(tools.WEEKLY)


# ── the cadences, as configured ──────────────────────────────────────────


@pytest.mark.parametrize("key,expected,why", [
    ("trading_bot", 48, "status publish gated to ~daily by MIN_AGE_HOURS=20"),
    ("ufc", 48, "slowest guaranteed publish is the daily 09:00 cron"),
    ("volleyball", 192, "overseer-status.yml publishes weekly on Mondays"),
])
def test_each_project_sla_matches_its_real_publish_cadence(key, expected, why):
    assert tools.PROJECTS[key]["sla_hours"] == expected, why


def test_coachvision_is_not_stale_five_days_after_its_weekly_publish():
    # The exact case from the 2026-08-15 run: 122.8h since the Monday publish.
    # Under the old 48h default this was flagged STALE.
    assert 122.8 < tools.PROJECTS["volleyball"]["sla_hours"]


def test_coachvision_still_alarms_once_it_misses_a_whole_cycle():
    # Relaxing the deadline must not mean never alarming. A genuinely stopped
    # weekly publisher is caught after one missed Monday plus the grace day.
    sla = tools.PROJECTS["volleyball"]["sla_hours"]
    assert tools.WEEKLY < sla, "must tolerate one on-time weekly gap"
    assert sla < 2 * tools.WEEKLY, "must not silently tolerate two missed weeks"


# ── overrides ────────────────────────────────────────────────────────────


def test_an_explicit_sla_env_var_wins(monkeypatch):
    monkeypatch.setenv("UFC_SLA_HOURS", "12")
    assert tools._sla_hours("UFC_SLA_HOURS", tools.DAILY) == 12


def test_stating_the_cadence_derives_the_deadline(monkeypatch):
    # The preferred override: say how often it publishes, not what the deadline
    # should be — the rule then keeps the two consistent.
    monkeypatch.setenv("UFC_PUBLISH_INTERVAL_HOURS", "6")
    assert tools._sla_hours("UFC_SLA_HOURS", tools.DAILY) == 6 + tools.SLA_GRACE_HOURS


def test_an_explicit_deadline_beats_a_stated_cadence(monkeypatch):
    monkeypatch.setenv("UFC_PUBLISH_INTERVAL_HOURS", "6")
    monkeypatch.setenv("UFC_SLA_HOURS", "99")
    assert tools._sla_hours("UFC_SLA_HOURS", tools.DAILY) == 99


@pytest.mark.parametrize("junk", ["", "   ", "soon", "48h"])
def test_a_fat_fingered_variable_falls_back_instead_of_breaking_the_run(monkeypatch, junk):
    monkeypatch.setenv("UFC_SLA_HOURS", junk)
    assert tools._sla_hours("UFC_SLA_HOURS", tools.DAILY) == 48


def test_a_project_with_no_recorded_cadence_uses_the_shared_default(monkeypatch):
    monkeypatch.delenv("NEW_PROJECT_SLA_HOURS", raising=False)
    assert tools._sla_hours("NEW_PROJECT_SLA_HOURS") == tools.FRESHNESS_SLA_DEFAULT_HOURS
