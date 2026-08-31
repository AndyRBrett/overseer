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
    """The worker's cron -> repository_dispatch type table, parsed out of the JS."""
    block = re.search(r"const DISPATCH_EVENTS = \{(.*?)\};", worker_source(), re.S)
    assert block, "the worker no longer declares a cron -> event map"
    return dict(re.findall(r'"([^"]+)"\s*:\s*"([^"]+)"', block.group(1)))


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
    for cron, event_type in dispatch_map().items():
        assert event_type in heard, f"{cron} fires {event_type!r} and nothing listens"


def test_both_jobs_are_covered():
    # The review and the alarm that watches it. An alarm on the scheduler it
    # watches shares a failure mode with it, which is what happened on 08-31 —
    # the review never ran and the heartbeat never ran to say so.
    assert set(dispatch_map().values()) == {"weekly-review", "heartbeat"}
    heard = listeners()
    assert heard["weekly-review"] == "weekly-review.yml"
    assert heard["heartbeat"] == "heartbeat.yml"


def test_every_cron_maps_to_an_event():
    # A cron in wrangler.toml with no entry in DISPATCH_EVENTS fires into
    # nothing, on a schedule, forever.
    assert set(wrangler()["triggers"]["crons"]) == set(dispatch_map())


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


def test_the_review_cron_fires_on_monday():
    # Day-of-week 1 = Monday, matching the review's own crons. A trigger that
    # fires on the wrong day is a trigger that never covers the run it exists
    # for.
    for cron, event_type in dispatch_map().items():
        if event_type == "weekly-review":
            assert cron.split()[4] == "1", f"{cron} does not fire on Monday"


def test_the_heartbeat_cron_fires_daily():
    # Daily is the point: it is what bounds how long a dropped weekly event can
    # go unnoticed to about a day.
    daily = [c for c, e in dispatch_map().items() if e == "heartbeat"]
    assert daily, "nothing fires the heartbeat"
    for cron in daily:
        assert cron.split()[4] == "*", f"{cron} does not fire every day"
        assert cron.split()[2] == "*", f"{cron} does not fire every day"


@pytest.mark.parametrize("event_type,wf", [("weekly-review", "weekly-review.yml"),
                                           ("heartbeat", "heartbeat.yml")])
def test_each_cron_lands_after_githubs_own(event_type, wf):
    # 14:05 vs GitHub's 14:00; 15:20 vs GitHub's 15:00. Ordering is what makes a
    # healthy day free: GitHub's on-time cron goes first, the dispatch arrives
    # second and no-ops. Fire first and every healthy Monday pays twice.
    primary = min(cron_minutes(c) for c in github_crons(wf))
    ours = min(cron_minutes(c) for c, e in dispatch_map().items() if e == event_type)
    assert ours > primary, f"the worker's {event_type} cron beats GitHub's own"


# ── the worker still holds no rules (invariant 8) ────────────────────────

def test_the_worker_does_not_decide_whether_to_run():
    # Invariant 8, one platform further away. Whether today's review is owed is
    # weekly_guard.py's judgement, on the GitHub side, for every automated
    # trigger. A copy of it here would be deployed separately and drift out of
    # sight of the Python — and would be deciding, unwatched, to skip a review.
    src = worker_source().lower()
    for leak in ("digest.json", "generated", "should_run", "status === \"completed\""):
        assert leak not in src, f"the worker is re-deriving the guard: {leak!r}"


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
