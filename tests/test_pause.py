"""
The dated kill switch (pause.py).

On 2026-09-20 the only way to skip an expensive Monday was to disable
weekly-review.yml and implement.yml in the Actions tab. That works and covers
both schedulers at once, but it never expires: overseer stays dark until a human
remembers two toggles. These pin the replacement, and most of them exist for one
half of it — that a value this cannot make sense of must NOT pause anything.
A switch that fails closed is a switch one typo away from being permanent, and
"a stage went quiet and nothing was red about it" is this system's whole
bestiary.

EVERY DATE HERE IS RELATIVE to an injected NOW. CLAUDE.md records why twice: a
fixture stamped `hours_ago=2` lands on *yesterday* after UTC midnight, and
test_guard_writes_the_workflow_output dated a fake run against a fixed Monday in
August while the code under test read the real clock — so it passed on the day
it was written and asserted nothing ever after.
"""

import sys

from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import pause  # noqa: E402

NOW = datetime(2026, 9, 22, 16, 0, tzinfo=timezone.utc)


def _date(days_from_now):
    """An ISO date `days_from_now` days after NOW — never a literal."""
    return (NOW + timedelta(days=days_from_now)).date().isoformat()


# --- the normal states ----------------------------------------------------

def test_unset_means_run():
    for empty in (None, "", "   ", "\n"):
        paused, why = pause.pause_state(empty, NOW)
        assert paused is False, f"{empty!r} must not pause anything"
        assert "no pause" in why


def test_a_future_date_pauses():
    paused, why = pause.pause_state(_date(7), NOW)
    assert paused is True
    assert "7 days" in why


def test_the_resume_date_itself_runs():
    # OVERSEER_PAUSED_UNTIL is the day it comes BACK, not the last paused day.
    # Off by one here skips an extra cycle, which is the expensive direction.
    assert pause.is_paused(_date(0), NOW) is False
    assert pause.is_paused(_date(1), NOW) is True


def test_tomorrow_reads_as_one_day_not_one_days():
    _, why = pause.pause_state(_date(1), NOW)
    assert "1 day " in why and "1 days" not in why


def test_a_past_date_is_inert():
    # The whole point of a dated switch: a value left behind in the settings
    # stops mattering on its own rather than becoming a trap.
    paused, why = pause.pause_state(_date(-30), NOW)
    assert paused is False
    assert "expired" in why


# --- the refusals, which all run ------------------------------------------

def test_junk_does_not_pause():
    # A guard that paused on anything it could not read would be one
    # fat-fingered settings field away from switching the system off forever.
    for junk in ("true", "yes", "soon", "next week", "0", "-1", "2026-13-01",
                 "2026-02-30", "not-a-date"):
        paused, why = pause.pause_state(junk, NOW)
        assert paused is False, f"{junk!r} must not pause anything"
        assert repr(junk) in why or "not a YYYY-MM-DD" in why


def test_near_miss_date_formats_do_not_pause():
    # date.fromisoformat accepts some of these on newer Pythons, and a value in
    # a format nobody documented is a value somebody guessed at.
    for near in ("2026-9-29", "20260929", "09-29-2026", "2026/09/29",
                 f"{_date(7)}T00:00:00Z", f" {_date(7)} extra"):
        assert pause.is_paused(near, NOW) is False, f"{near!r} must not pause"


def test_surrounding_whitespace_is_forgiven():
    # A settings field with a stray newline is a typo, not a different intent.
    assert pause.is_paused(f"  {_date(7)}  ", NOW) is True


def test_a_mistyped_year_does_not_pause_for_a_century():
    # The single most likely typo in the one field whose job is to expire.
    paused, why = pause.pause_state("2126-09-29", NOW)
    assert paused is False
    assert str(pause.MAX_PAUSE_DAYS) in why


def test_the_long_pause_boundary():
    assert pause.is_paused(_date(pause.MAX_PAUSE_DAYS), NOW) is True
    assert pause.is_paused(_date(pause.MAX_PAUSE_DAYS + 1), NOW) is False


def test_the_reason_always_names_the_date_it_acted_on():
    # These strings are read in a run log by someone asking why Monday was quiet.
    _, why = pause.pause_state(_date(3), NOW)
    assert _date(3) in why


def test_env_var_name_is_what_the_workflows_pass():
    # Both guard steps set this by name; renaming it in one place only would
    # silently disarm the switch.
    assert pause.ENV_VAR == "OVERSEER_PAUSED_UNTIL"
    workflows = Path(__file__).resolve().parent.parent / ".github" / "workflows"
    for name in ("weekly-review.yml", "implement.yml"):
        text = (workflows / name).read_text(encoding="utf-8")
        assert f"{pause.ENV_VAR}: ${{{{ vars.{pause.ENV_VAR} }}}}" in text, (
            f"{name} must pass the pause variable into its guard step")


# --- the refusal has to be audible ----------------------------------------

def test_nothing_configured_says_nothing():
    for empty in (None, "", "  "):
        assert pause.announcement(empty, NOW) is None


def test_an_honoured_pause_is_announced():
    note = pause.announcement(_date(5), NOW)
    assert note.startswith("pause —")
    assert _date(5) in note


def test_a_refused_pause_is_announced_loudly():
    # The whole safety argument for "in doubt it runs" depends on this. Someone
    # set that field expecting a quiet Monday; silently ignoring the value would
    # let them find out it never took when the bill arrived.
    for junk in ("true", "2126-09-29", "2020-01-01"):
        note = pause.announcement(junk, NOW)
        assert note is not None and note.startswith("PAUSE NOT APPLIED"), junk


def test_both_guards_announce_the_pause_state():
    # Pinned because the announcement lives in main(), which no unit test of
    # should_run would ever reach.
    scripts = Path(__file__).resolve().parent.parent / "scripts"
    for name in ("weekly_guard.py", "implement_guard.py"):
        src = (scripts / name).read_text(encoding="utf-8")
        assert "pause.announcement(" in src, f"{name} must log the pause state"
