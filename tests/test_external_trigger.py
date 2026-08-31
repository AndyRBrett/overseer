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

There is no JS test runner here (see CLAUDE.md), so these grep the Worker and
its config for the seams whose absence would make the trigger silently useless:
a dispatch nothing listens for, a schedule that never fires, a rule quietly
copied out of Python.
"""

import os
import re

try:
    import tomllib
except ImportError:  # pragma: no cover - Python < 3.11
    import tomli as tomllib

import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKER = os.path.join(REPO_ROOT, "worker", "overseer-ask.js")
WRANGLER = os.path.join(REPO_ROOT, "worker", "wrangler.toml")
WORKFLOW = os.path.join(REPO_ROOT, ".github", "workflows", "weekly-review.yml")


def worker_source():
    with open(WORKER, encoding="utf-8") as f:
        return f.read()


def wrangler():
    with open(WRANGLER, "rb") as f:
        return tomllib.load(f)


def workflow():
    with open(WORKFLOW, encoding="utf-8") as f:
        # `on:` parses as the boolean True in YAML 1.1, which is why this reads
        # d[True] rather than d["on"].
        return yaml.safe_load(f)


# ── the two ends of the dispatch have to agree ───────────────────────────

def test_the_workflow_listens_for_the_dispatch():
    types = workflow()[True]["repository_dispatch"]["types"]
    assert "weekly-review" in types


def test_the_worker_sends_the_type_the_workflow_listens_for():
    # A mismatch here is the worst kind of failure: GitHub accepts the dispatch
    # with 204, the Worker logs success, and no workflow ever reacts. It would
    # read as healthy right up until someone noticed a stale digest — which is
    # exactly how the original incident was found.
    match = re.search(r'const DISPATCH_EVENT = "([^"]+)"', worker_source())
    assert match, "the worker no longer declares a dispatch event type"
    assert match.group(1) in workflow()[True]["repository_dispatch"]["types"]


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


def test_the_cron_fires_on_monday():
    # Day-of-week 1 = Monday, matching the review's own crons. A trigger that
    # fires on the wrong day is a trigger that never covers the run it exists
    # for.
    for cron in wrangler()["triggers"]["crons"]:
        assert cron.split()[4] == "1", f"{cron} does not fire on Monday"


def test_the_cron_lands_after_githubs_own():
    # 14:05 vs GitHub's 14:00. Ordering is what makes a healthy Monday free:
    # GitHub's on-time cron publishes first, the dispatch arrives second, and
    # the guard no-ops it. Fire this first and every healthy Monday pays twice.
    github_crons = [c["cron"] if isinstance(c, dict) else c
                    for c in workflow()[True]["schedule"]]
    primary = min(int(c.split()[1]) * 60 + int(c.split()[0]) for c in github_crons)
    worker_first = min(int(c.split()[1]) * 60 + int(c.split()[0])
                       for c in wrangler()["triggers"]["crons"])
    assert worker_first > primary, "the worker's cron must not beat GitHub's own"


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
