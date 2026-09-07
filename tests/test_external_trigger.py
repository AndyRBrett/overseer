"""Tests for the weekly review's off-GitHub trigger (overseer, 2026-08-31).

THE INCIDENT. That Monday GitHub delivered none of the weekly review's three
scheduled events. Not a failed run — no run at all: 14:00, 16:00 and 18:00 came
and went with nothing created, nothing red, nothing queued, while push- and
PR-triggered runs in the same repo fired normally. The dashboard sat on a
seven-day-old digest and nothing alerted, because every alarm in this system is
downstream of a job starting. Measured the same day, ledger-refresh's HOURLY
cron was delivered twice in eighteen hours.

The 2026-08-17 hardening could not help: in-job retries and the catch-up crons
both live inside a job that has to start first, and the catch-ups are schedule:
entries in the same workflow, queued through the same deprioritised scheduler
that dropped the primary. So redundancy now comes from a different vendor — a
Cloudflare cron in worker/overseer-ask.js that fires repository_dispatch.

The heartbeat rides the same path, and for a sharper reason: it is the
dead-man's switch for the review, and on 08-31 it was dropped by the same
scheduler in the same outage. An alarm sharing a failure mode with the thing it
watches is not an alarm. Off GitHub, it also becomes the detector for a dropped
event — a review that never runs stops docs/digest.json advancing, and the
heartbeat notices within about a day.

There is no JS test runner here (see CLAUDE.md), so these grep the Worker and
its config for the seams whose absence would make the trigger silently useless:
a dispatch nothing listens for, a cron mapped to no event, a schedule that never
fires or fires too early, a rule quietly copied out of Python.
"""

import os
import re

import pytest

try:
    import tomllib
except ImportError:  # pragma: no cover - Python < 3.11
    import tomli as tomllib

import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKER = os.path.join(REPO_ROOT, "worker", "overseer-ask.js")
WRANGLER = os.path.join(REPO_ROOT, "worker", "wrangler.toml")
WORKFLOWS = os.path.join(REPO_ROOT, ".github", "workflows")
WORKFLOW = os.path.join(WORKFLOWS, "weekly-review.yml")


def worker_source():
    with open(WORKER, encoding="utf-8") as f:
        return f.read()


def wrangler():
    with open(WRANGLER, "rb") as f:
        return tomllib.load(f)


def workflow(name="weekly-review.yml"):
    with open(os.path.join(WORKFLOWS, name), encoding="utf-8") as f:
        # `on:` parses as the boolean True in YAML 1.1, which is why this reads
        # d[True] rather than d["on"].
        return yaml.safe_load(f)


def dispatch_map():
    """The worker's cron -> repository_dispatch types table, parsed out of the JS.

    The values are LISTS since 2026-09-07: the implementer's backup rides the
    crons that already exist rather than adding two more, because Cloudflare's
    free-plan cron cap is documented two different ways and three are already
    declared.
    """
    block = re.search(r"const DISPATCH_EVENTS = \{(.*?)\n\};", worker_source(), re.S)
    assert block, "the worker no longer declares a cron -> event map"
    pairs = re.findall(r'"([^"]+)"\s*:\s*\[([^\]]*)\]', block.group(1))
    assert pairs, "DISPATCH_EVENTS no longer maps each cron to a list of events"
    return {cron: re.findall(r'"([^"]+)"', events) for cron, events in pairs}


def crons_firing(event_type):
    return [cron for cron, events in dispatch_map().items() if event_type in events]


def monday_events():
    """The event types the worker will only ask for on a Monday."""
    block = re.search(r"const MONDAY_EVENTS = new Set\(\[(.*?)\]\);", worker_source(), re.S)
    assert block, "the worker no longer declares which events are Monday-only"
    return set(re.findall(r'"([^"]+)"', block.group(1)))


def listeners():
    """Every repository_dispatch type any workflow here listens for -> its file."""
    found = {}
    for name in os.listdir(WORKFLOWS):
        if not name.endswith((".yml", ".yaml")):
            continue
        triggers = (workflow(name) or {}).get(True) or {}
        for event_type in ((triggers.get("repository_dispatch") or {}).get("types") or []):
            found[event_type] = name
    return found


def cron_minutes(expr):
    """Minutes past midnight UTC for a 5-field cron, for ordering comparisons."""
    minute, hour = expr.split()[0], expr.split()[1]
    return int(hour) * 60 + int(minute)


def github_crons(name):
    return [c["cron"] if isinstance(c, dict) else c
            for c in workflow(name)[True]["schedule"]]


# ── the two ends of the dispatch have to agree ───────────────────────────

def test_the_workflows_listen_for_what_the_worker_sends():
    # A mismatch here is the worst kind of failure: GitHub accepts the dispatch
    # with 204, the Worker logs success, and no workflow ever reacts. It would
    # read as healthy right up until someone noticed a stale digest — which is
    # exactly how the original incident was found.
    heard = listeners()
    for cron, event_types in dispatch_map().items():
        for event_type in event_types:
            assert event_type in heard, f"{cron} fires {event_type!r} and nothing listens"


def test_every_scheduled_stage_is_covered():
    # The review, the alarm that watches it, and the dispatcher. An alarm on the
    # scheduler it watches shares a failure mode with it, which is what happened
    # on 08-31 — the review never ran and the heartbeat never ran to say so.
    #
    # implement joined them on 2026-09-07, when GitHub created no run for either
    # its 15:00 cron or its 17:00 catch-up and the week's implementation stage
    # was skipped until a human fired it by hand. It was the last stage here
    # still trusting a single scheduler; a stage off this list is one outage away
    # from being skipped with nothing red.
    fired = {e for events in dispatch_map().values() for e in events}
    assert fired == {"weekly-review", "heartbeat", "implement"}
    heard = listeners()
    assert heard["weekly-review"] == "weekly-review.yml"
    assert heard["heartbeat"] == "heartbeat.yml"
    assert heard["implement"] == "implement.yml"


def test_the_implementer_backup_adds_no_cron():
    # Cloudflare's docs disagree with themselves about the free-plan cap — "per
    # Worker" on one page, 5 per ACCOUNT on the Limits page, 3 per Worker in
    # third-party references — and three crons were already declared. The backup
    # therefore rides existing crons; a fourth would sit on the cap under the
    # generous reading and over it under the strict one, and the deploy would
    # start failing at the worst possible moment.
    assert len(wrangler()["triggers"]["crons"]) <= 3
    assert crons_firing("implement"), "nothing fires the implementer"
    for cron in crons_firing("implement"):
        assert len(dispatch_map()[cron]) > 1, (
            f"{cron} carries only implement — it should be riding an existing cron")


def test_every_cron_maps_to_an_event():
    # A cron in wrangler.toml with no entry in DISPATCH_EVENTS fires into
    # nothing, on a schedule, forever.
    assert set(wrangler()["triggers"]["crons"]) == set(dispatch_map())
    for cron, events in dispatch_map().items():
        assert events, f"{cron} maps to an empty event list"


def test_the_worker_posts_to_the_dispatches_endpoint():
    src = worker_source()
    assert "/dispatches" in src
    assert "event_type" in src


def test_the_dispatch_carries_a_user_agent():
    # GitHub rejects an API call with no User-Agent, and the 403 it returns
    # reads like a permissions problem — hours of chasing the wrong thing.
    assert re.search(r'"user-agent"\s*:', worker_source())


# ── the schedule has to actually fire, and not collide ───────────────────

def test_the_worker_has_a_cron_schedule():
    crons = wrangler().get("triggers", {}).get("crons", [])
    assert crons, "no Cloudflare cron means the whole trigger is decorative"
    for cron in crons:
        assert len(cron.split()) == 5, f"not a 5-field cron: {cron}"


# Cloudflare's cron parser counts the week from 1, so Sunday is 1 and Monday is
# 2. GitHub runs POSIX cron, counting from 0, so Monday is 1 there. The same
# five characters mean different days on the two schedulers, and this file is
# the only place that says so.
CLOUDFLARE_MONDAY = "2"
GITHUB_MONDAY = "1"


def test_the_review_cron_fires_on_monday():
    # 2026-09-06, and this assertion is the bug. It used to read `== "1"`,
    # copied from weekly-review.yml's own crons on the reasonable assumption
    # that a cron is a cron. On Cloudflare `5 14 * * 1` is SUNDAY: the Worker
    # fired weekly-review at 14:05 on Sunday 09-06, weekly_guard.py correctly
    # saw no digest for that day and let a full $0.43 review run, and Monday's
    # GitHub cron then owed a second one. Nothing was red; the test agreed with
    # the mistake because it shared the mistake's premise.
    #
    # A trigger that fires on the wrong day is not covering the run it exists
    # for — it is buying an extra one.
    for cron in crons_firing("weekly-review"):
        assert cron.split()[4] == CLOUDFLARE_MONDAY, (
            f"{cron} does not fire on Cloudflare's Monday")


def test_the_two_schedulers_disagree_deliberately():
    # The halves of the incident, pinned facing each other: if these two ever
    # read the same day-of-week field, one of them is firing on the wrong day.
    # Written as an inequality on purpose — the next person to touch either file
    # should have to come here and read why.
    github = {c.split()[4] for c in github_crons("weekly-review.yml")}
    assert github == {GITHUB_MONDAY}, f"GitHub's review crons moved off Monday: {github}"
    for cron in crons_firing("weekly-review"):
        assert cron.split()[4] != GITHUB_MONDAY, (
            f"{cron} mirrors GitHub's day field, which is Sunday on Cloudflare")


def test_the_worker_checks_the_day_itself():
    # The belt to the cron's braces. A cron field is a claim about a parser this
    # repo does not own; getUTCDay() is not. The check exists so the next drift
    # — a re-edit, a parser change, a fourth vendor — costs a skipped redundant
    # poke rather than a review nobody asked for.
    src = worker_source()
    assert "MONDAY_UTC_DAY = 1" in src, "the worker does not know which day Monday is"
    scheduled = src.split("async scheduled(")[1].split("async fetch(")[0]
    guard = scheduled.split("ctx.waitUntil(fireDispatch(")[0]
    assert "getUTCDay()" in guard, "the day is not checked before the review is asked for"
    assert "MONDAY_UTC_DAY" in guard


def test_the_monday_only_jobs_are_day_checked():
    # Both weekly jobs ride Cloudflare crons now, and the implementer's primary
    # backup rides the DAILY heartbeat cron — so without this it would ask for a
    # $4.50 implementation run every morning of the week.
    assert monday_events() == {"weekly-review", "implement"}


def test_the_day_check_does_not_gate_the_heartbeat():
    # The heartbeat is daily and is the dead-man's switch. Gating it on a
    # weekday would silence the alarm six days out of seven, which is the one
    # thing this Worker may never do.
    assert "heartbeat" not in monday_events(), (
        "the day check gates the heartbeat, silencing the alarm six days in seven")
    scheduled = worker_source().split("async scheduled(")[1].split("async fetch(")[0]
    guard = scheduled.split("ctx.waitUntil(fireDispatch(")[0]
    assert "MONDAY_EVENTS.has" in guard, (
        "the day check is not scoped by event type, so it also gates the heartbeat")
    # `continue`, not `return`: the heartbeat shares its cron with the
    # implementer's Monday backup, so skipping the wrong-day job must not take
    # the alarm down with it.
    skip = guard.split("MONDAY_EVENTS.has")[1]
    assert "continue;" in skip and "return;" not in skip


def test_the_heartbeat_cron_fires_daily():
    # Daily is the point: it is what bounds how long a dropped weekly event can
    # go unnoticed to about a day.
    daily = crons_firing("heartbeat")
    assert daily, "nothing fires the heartbeat"
    for cron in daily:
        assert cron.split()[4] == "*", f"{cron} does not fire every day"
        assert cron.split()[2] == "*", f"{cron} does not fire every day"


@pytest.mark.parametrize("event_type,wf", [("weekly-review", "weekly-review.yml"),
                                           ("heartbeat", "heartbeat.yml"),
                                           ("implement", "implement.yml")])
def test_each_cron_lands_after_githubs_own(event_type, wf):
    # 14:05 vs GitHub's 14:00; 15:20 vs GitHub's 15:00. Ordering is what makes a
    # healthy day free: GitHub's on-time cron goes first, the dispatch arrives
    # second and no-ops. Fire first and every healthy Monday pays twice — and for
    # the implementer that is the expensive stage, three attempts at ~$1.50.
    primary = min(cron_minutes(c) for c in github_crons(wf))
    ours = min(cron_minutes(c) for c in crons_firing(event_type))
    assert ours > primary, f"the worker's {event_type} cron beats GitHub's own"


# ── the worker still holds no rules (invariant 8) ────────────────────────

def test_the_worker_does_not_decide_whether_to_run():
    # Invariant 8, one platform further away. Whether today's review is owed is
    # weekly_guard.py's judgement, on the GitHub side, for every automated
    # trigger. A copy of it here would be deployed separately and drift out of
    # sight of the Python — and would be deciding, unwatched, to skip a review.
    #
    # Scoped to the DISPATCH path rather than the whole file since #59: the
    # watchdog reads the published digest stamp to decide whether to wake a
    # HUMAN, which is the opposite failure mode — it can only ever add an alert,
    # never withhold a run. The assertion below is what actually protects the
    # invariant, and it is stronger than a substring ban: the dispatch is
    # unconditional.
    src = worker_source()
    dispatch = src.split("async function checkDigestFreshness(")[0]
    for leak in ("digest.json", "generated", "should_run", 'status === "completed"'):
        assert leak.lower() not in dispatch.lower(), (
            f"the dispatch path is re-deriving the guard: {leak!r}")

    scheduled = src.split("async scheduled(")[1].split("async fetch(")[0]
    fire = scheduled.split("ctx.waitUntil(fireDispatch(")[0]
    # Nothing between entering the handler and firing may consult freshness.
    for leak in ("checkDigestFreshness", "generated", "stale"):
        assert leak not in fire, f"the dispatch is gated on {leak!r}"


def test_the_worker_does_not_hold_the_token_in_source():
    # The token is a wrangler secret. wrangler.toml is committed; [vars] is not
    # a place for a credential, and DISPATCH_TOKEN must never appear there.
    assert "DISPATCH_TOKEN" not in str(wrangler().get("vars", {}))
    src = worker_source()
    assert "env.DISPATCH_TOKEN" in src, "the token must come from the environment"
    assert not re.search(r"gh[ps]_[A-Za-z0-9]{20,}", src), "a PAT is hardcoded in the worker"


def test_the_dispatch_target_is_configured_not_hardcoded():
    assert "env.DISPATCH_REPO" in worker_source()
    assert "/" in wrangler()["vars"]["DISPATCH_REPO"], "DISPATCH_REPO is owner/repo"


def test_a_missing_credential_is_logged_rather_than_silent():
    # A trigger that stops working quietly recreates the original incident with
    # extra steps.
    src = worker_source()
    assert "[dispatch] not configured" in src
    assert "[dispatch] failed" in src
