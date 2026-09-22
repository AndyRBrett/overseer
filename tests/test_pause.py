"""
The dated kill switch (pause.py).

On 2026-09-20 the only way to skip an expensive Monday was to disable
weekly-review.yml and implement.yml in the Actions tab. That works and covers
both schedulers at once, but it never expires: overseer stays dark until a human
remembers two toggles. The replacement was a repository Variable for one
afternoon and is now a committed file, because a Variable meant a human in
Settings for every pause and an agent that could not set it at all. These pin the replacement, and most of them exist for one
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
        assert "no pause file" in why


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


def test_a_space_padded_day_does_not_pause():
    # CODEX, PR #92, and the only finding on it. The first draft gated on
    # len(text) == 10 and handed the rest to strptime — which accepts a
    # SPACE-PADDED day, so `2026-09- 9` measured ten characters, parsed as the
    # 9th, and PAUSED the pipeline. Fail-closed, on an undocumented value, in
    # the module whose whole argument is that it fails open.
    for padded in ("2026-09- 9", "2026-09-1 ", " 026-09-19"):
        assert pause.is_paused(padded, NOW) is False, f"{padded!r} must not pause"


def test_only_canonical_iso_round_trips():
    # The general form of the bug above: the gate is that the parsed date
    # renders back to exactly the text it came from, so every near-miss is
    # caught by one rule rather than a list of the ones somebody enumerated.
    good = _date(7)
    assert pause.parse_resume_date(good).isoformat() == good
    for variant in ("2026-9-9", "2026-09- 9", "2026-1-01"):
        assert pause.parse_resume_date(variant) is None, variant


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


def test_the_switch_no_longer_rides_in_the_environment():
    # It was `vars.OVERSEER_PAUSED_UNTIL` for one afternoon. A leftover env line
    # would be a second, invisible source for the date — and the one nobody can
    # edit from here, which is the whole reason it moved into the repo.
    workflows = Path(__file__).resolve().parent.parent / ".github" / "workflows"
    for name in ("weekly-review.yml", "implement.yml"):
        text = (workflows / name).read_text(encoding="utf-8")
        assert "OVERSEER_PAUSED_UNTIL" not in text, (
            f"{name} still passes the retired pause Variable")


def test_both_guards_read_the_file():
    scripts = Path(__file__).resolve().parent.parent / "scripts"
    for name in ("weekly_guard.py", "implement_guard.py"):
        src = (scripts / name).read_text(encoding="utf-8")
        assert "pause.read_pause_file(" in src, f"{name} must read the pause file"


# --- reading the file -----------------------------------------------------

def test_a_missing_file_is_the_quiet_normal_state(tmp_path):
    assert pause.read_pause_file(str(tmp_path / "nope")) is None
    paused, why = pause.current(NOW, path=str(tmp_path / "nope"))
    assert paused is False
    assert "no pause file" in why


def test_comments_and_blank_lines_are_skipped(tmp_path):
    # The comment line is the point: it is where the WHY lives, which a
    # settings field had nowhere to put.
    f = tmp_path / "pause"
    f.write_text(f"# spend was high in September\n\n{_date(5)}\n", encoding="utf-8")
    assert pause.read_pause_file(str(f)) == _date(5)
    assert pause.current(NOW, path=str(f))[0] is True


def test_only_the_first_value_line_counts(tmp_path):
    # Two dates in one file is a question this must not have to answer.
    f = tmp_path / "pause"
    f.write_text(f"{_date(5)}\n{_date(60)}\n", encoding="utf-8")
    assert pause.read_pause_file(str(f)) == _date(5)


def test_an_empty_or_comment_only_file_does_not_pause(tmp_path):
    for body in ("", "\n\n", "# paused? no, just a note\n"):
        f = tmp_path / "pause"
        f.write_text(body, encoding="utf-8")
        assert pause.read_pause_file(str(f)) is None
        assert pause.current(NOW, path=str(f))[0] is False


def test_an_unreadable_file_does_not_pause(tmp_path):
    # A permissions problem on this file must not be what takes the review down.
    d = tmp_path / "a-directory"
    d.mkdir()
    assert pause.read_pause_file(str(d)) is None


def test_a_committed_pause_file_must_actually_parse():
    # THE EARLY WARNING. A malformed value fails OPEN by design, so "I paused it"
    # and "I typed the date wrong" look identical until the bill arrives. If the
    # repo carries a pause file at all, CI says so here rather than on Monday.
    committed = Path(__file__).resolve().parent.parent / pause.PAUSE_FILE
    if not committed.exists():
        return
    raw = pause.read_pause_file(str(committed))
    assert raw is not None, f"{pause.PAUSE_FILE} exists but has no value line"
    assert pause.parse_resume_date(raw) is not None, (
        f"{pause.PAUSE_FILE} says {raw!r}, which will NOT pause anything")


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
