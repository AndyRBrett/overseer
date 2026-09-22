"""A dated pause for the automated stages — the kill switch that expires itself.

THE PROBLEM THIS SOLVES. On 2026-09-20 the month's API spend was running high
and the ask was simply "skip tomorrow". There was no way to say that. The only
lever was disabling `weekly-review.yml` and `implement.yml` in the Actions tab,
which works — a disabled workflow ignores `schedule`, `repository_dispatch` and
`workflow_dispatch` alike, so it covers both schedulers at once with no merge
and no Worker deploy — but it has no expiry and leaves no trace in the repo.
Overseer stayed dark until a human remembered to flip two toggles back. That is
this system's characteristic failure mode wearing a different hat: a stage goes
quiet, nothing is red about it being quiet, and the only alarm left is the
heartbeat noticing the digest has stopped moving.

So the pause is a DATE, and the date is the thing that ends it. Set
`OVERSEER_PAUSED_UNTIL` and the automated triggers stand down until it passes;
forget to unset it and the system comes back on its own.

    OVERSEER_PAUSED_UNTIL=2026-09-29

reads as "resume on the 29th" — the runs on the 29th happen. It is the RESUME
date, not the last paused day, so pausing a single Monday means naming the
Tuesday. Stated the other way round it would be off by one in the direction
that skips an extra week, and a pause that overruns is the expensive mistake
here.

WHERE THE VALUE LIVES. A GitHub repository Variable, read by the guard steps of
both workflows. That is the half that has to be editable without a merge: the
RULE is here in code, reviewed and tested, while the DATE is a settings field
anyone can change in ten seconds. Invariant 4's split, applied to spend.

WHEN IN DOUBT IT RUNS, which is the whole reason this file is as fussy as it is
about what it refuses to honour. Both callers are guards whose docstrings say
the same thing for the same reason: a redundant review costs $0.34 and a
duplicate batch ~$4.50, but a skipped week costs the week, and a stage that
silently never comes back costs however long it takes someone to notice. So a
value this cannot make sense of does NOT pause anything — it says so loudly and
gets out of the way. Three refusals, each one a way a pause could otherwise
become permanent by accident:

  * unparseable — a typo, a half-edited value, `true`, an ISO timestamp with a
    time on it. A guard that paused on anything it could not read would be one
    fat-fingered settings field away from switching the system off forever.
  * already past — not a refusal so much as the design working. This is what
    makes the switch self-clearing, and it means a stale value left in the
    settings is inert rather than a trap.
  * absurdly far out — `2126-09-29` is a mistyped year, not a century-long
    pause, and it is the single most likely typo to make: a digit, in the field
    whose whole job is to eventually expire. Past MAX_PAUSE_DAYS this is
    treated as the typo it almost certainly is. Someone who genuinely wants a
    longer stand-down can say so twice, or disable the workflows the old way.

NOT A SECOND GATE ON SPEND. This decides whether a stage runs at all; it does
not touch `OVERSEER_IMPLEMENT_MAX`, the tier, or the queue. And note the trap
next door while you are here — `tools.IMPLEMENT_MAX` is `_int_env(...) or 3`,
so setting that variable to `0` quietly means three, not zero. It is not a
pause and never was; this is.

PURE, AND IMPORTING NOTHING OF OURS. `weekly-review.yml` runs its guard BEFORE
`pip install -r requirements.txt`, so anything that guard reaches has to be
standard library only — the same constraint `attention.py` and `dedupe.py`
work under.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

ENV_VAR = "OVERSEER_PAUSED_UNTIL"

# How far ahead a pause may legitimately reach. Ninety days is generous for
# "costs are high this month" and still short enough that a mistyped year lands
# outside it — which is the case this exists to catch, not a policy on how long
# anyone may pause.
MAX_PAUSE_DAYS = 90


def _today(now=None):
    return (now or datetime.now(timezone.utc)).date()


def parse_resume_date(raw):
    """The resume date in `raw`, or None if it is not a plain ISO date.

    Deliberately strict. `date.fromisoformat` on newer Pythons accepts forms
    like `20260929`, and a timestamp with a time on it would silently become a
    date — both are signs the field was filled in by someone guessing at the
    format, which is exactly when refusing to pause is the safe answer.

    THE GATE IS THE ROUND TRIP, not a length check (Codex, PR #92). This first
    read the value's length and handed the rest to strptime, which was wrong in
    the one direction this module may not be wrong in: strptime accepts a
    SPACE-PADDED day, so `2026-09- 9` is ten characters long, parses happily as
    the 9th, and PAUSED the pipeline on a malformed value the docs never
    describe — fail-closed, in the file whose entire argument is that it fails
    open. Requiring the parsed date to render back to exactly the text it came
    from is the same check for every such near-miss at once, this one and the
    `2026-9-9` family alike, rather than a list of the ones anybody thought of.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None
    return parsed if parsed.isoformat() == text else None


def pause_state(raw, now=None):
    """(paused?, reason) for the configured value `raw`.

    The reason is written to be read in a run log by someone wondering why the
    Monday was quiet, so it always names the value it acted on.
    """
    if raw is None or not str(raw).strip():
        return False, "no pause configured."

    text = str(raw).strip()
    resume = parse_resume_date(text)
    if resume is None:
        return False, (
            f"{ENV_VAR}={text!r} is not a YYYY-MM-DD date — ignoring it and "
            "running. A pause nobody can read is how a stage goes quiet for good."
        )

    today = _today(now)
    if resume <= today:
        return False, (
            f"the pause until {resume.isoformat()} has expired (today is "
            f"{today.isoformat()}) — running as normal."
        )

    days = (resume - today).days
    if days > MAX_PAUSE_DAYS:
        return False, (
            f"{ENV_VAR}={text!r} is {days} days out, past the {MAX_PAUSE_DAYS}-day "
            "limit — treating it as a mistyped year and running. Set a nearer "
            "date, or disable the workflow if you really mean indefinitely."
        )

    return True, (
        f"paused until {resume.isoformat()} ({days} day{'s' if days != 1 else ''} "
        f"from today) — standing down."
    )


def announcement(raw, now=None):
    """A line for the run log when a value is configured at all, else None.

    A REFUSED pause has to be loud. Someone set that field expecting a quiet
    Monday; if the value is junk the run proceeds, which is correct, but staying
    silent about it would let them believe the pause took and discover otherwise
    on the bill. The refusals above are the safe direction precisely BECAUSE
    they are announced — unannounced, "in doubt it runs" is just a switch that
    doesn't work.
    """
    if raw is None or not str(raw).strip():
        return None
    paused, why = pause_state(raw, now)
    return f"{'pause' if paused else 'PAUSE NOT APPLIED'} — {why}"


def is_paused(raw, now=None):
    """Just the boolean, for callers that do not log the reason."""
    return pause_state(raw, now)[0]
