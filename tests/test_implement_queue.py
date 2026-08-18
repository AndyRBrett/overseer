"""Tests for the implementation gate — what gets handed to the coding agent.

This gate is the difference between an implementer that helps and one that
buries you. The pipeline files several issues a week across four repos; handing
all of them over produces a PR queue nobody reviews, and since the ledger only
counts MERGED work as shipped, unreviewed PRs park in `in_flight` forever while
the spend climbs. So the rules that keep the queue small are load-bearing, and
they are what these tests pin down:

  * only OPEN issues with no fix already in flight,
  * confirmed bugs plus enhancements the Idea Agent itself sized as effort:low,
  * a hard cap per run, shared round-robin so one busy repo can't take it all,
  * a dispatch that fails must leave the issue UNLABELLED so it is retried.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import tools as o
from tracer import RunTracer


def _entry(number, *, repo="A/overseer", kind="bug", status="open", effort=None,
           impact=None, labels=(), created="2026-08-01T00:00:00+00:00",
           closed=None, title="Something", fix_ref=None):
    entry = {"repo": repo, "number": number, "title": title, "kind": kind,
             "status": status, "created_at": created,
             "url": f"https://github.com/{repo}/issues/{number}"}
    if effort:
        entry["effort"] = effort
    if impact:
        entry["impact"] = impact
    if labels:
        entry["labels"] = list(labels)
    if closed:
        entry["closed_at"] = closed
    if fix_ref:
        entry["fix_ref"] = fix_ref
    return entry


# ── THE GATE ─────────────────────────────────────────────────────────────

def test_an_open_confirmed_bug_is_eligible():
    ok, why = o.implementable(_entry(1))
    assert ok and "bug" in why


def test_a_low_effort_enhancement_is_eligible():
    ok, _ = o.implementable(_entry(2, kind="enhancement", effort="low"))
    assert ok


@pytest.mark.parametrize("effort", ["medium", "high"])
def test_bigger_enhancements_are_not_attempted(effort):
    # The Idea Agent sizes its own ideas; the gate takes it at its word. A
    # medium-effort idea is a conversation, not something to wake up to a PR for.
    ok, why = o.implementable(_entry(3, kind="enhancement", effort=effort))
    assert not ok and effort in why


def test_an_unsized_enhancement_is_not_attempted():
    ok, why = o.implementable(_entry(4, kind="enhancement"))
    assert not ok and "effort label" in why


def test_the_effort_gate_is_configurable():
    ok, _ = o.implementable(_entry(5, kind="enhancement", effort="medium"),
                            efforts=("low", "medium"))
    assert ok


@pytest.mark.parametrize("status", ["in_flight", "shipped", "duplicate", "not_planned"])
def test_only_open_issues_are_attempted(status):
    # in_flight is the important one: it means a PR already exists against this
    # issue, so attempting it again would produce a second branch fixing the
    # same thing. It is also the backstop for a failed label — see below.
    ok, why = o.implementable(_entry(6, status=status))
    assert not ok and status in why


def test_an_issue_already_handed_over_is_not_handed_over_again():
    ok, why = o.implementable(_entry(7, labels=[o.IMPLEMENTING_LABEL]))
    assert not ok and "already handed" in why


def test_a_burned_issue_is_not_retried_but_says_how_to_re_queue():
    # A failed attempt swaps overseer:implementing for this label. Both halves
    # matter: without the swap the issue reads as in progress forever (filed and
    # silently dropped), and without the exclusion Monday's run would retry an
    # attempt that already burned its turn budget once, at full price.
    ok, why = o.implementable(_entry(9, labels=[o.FAILED_LABEL]))
    assert not ok
    assert "previous attempt failed" in why and o.FAILED_LABEL in why


def test_the_opt_out_label_is_honoured():
    ok, why = o.implementable(_entry(8, labels=[o.NO_IMPLEMENT_LABEL]))
    assert not ok and o.NO_IMPLEMENT_LABEL in why


# ── THE QUEUE ────────────────────────────────────────────────────────────

def test_the_cap_is_enforced():
    ledger = {"entries": [_entry(n) for n in range(1, 11)]}
    assert len(o.implementation_queue(ledger, limit=3)["picks"]) == 3


def test_one_busy_repo_cannot_take_every_slot():
    # The overseer files against itself more than any other project, so without
    # round-robin it would win every slot every week and the three projects the
    # pipeline exists to watch would never see a PR.
    ledger = {"entries": [_entry(n, repo="A/overseer") for n in range(1, 9)]
                         + [_entry(50, repo="A/ufc"), _entry(60, repo="A/trading")]}
    picks = o.implementation_queue(ledger, limit=3)["picks"]
    assert len({p["repo"] for p in picks}) == 3


def test_bugs_outrank_enhancements_and_high_impact_outranks_low():
    ledger = {"entries": [
        _entry(1, kind="enhancement", effort="low", impact="low"),
        _entry(2, kind="enhancement", effort="low", impact="high"),
        _entry(3, kind="bug"),
    ]}
    assert [p["number"] for p in o.implementation_queue(ledger, limit=3)["picks"]] == [3, 2, 1]


def test_the_oldest_eligible_item_is_not_starved():
    # Age is the last tiebreak, but it is a tiebreak: a steady drip of newer
    # items of equal rank must not push an old one down the list forever.
    ledger = {"entries": [
        _entry(9, created="2026-08-10T00:00:00+00:00"),
        _entry(2, created="2026-06-01T00:00:00+00:00"),
    ]}
    assert o.implementation_queue(ledger, limit=1)["picks"][0]["number"] == 2


def test_every_rejection_carries_a_reason():
    # "Why was my issue skipped?" is the first question anyone asks of a gate,
    # and a dispatcher that cannot answer it gets switched off.
    ledger = {"entries": [_entry(1, status="shipped"),
                          _entry(2, kind="enhancement", effort="high")]}
    skipped = o.implementation_queue(ledger)["skipped"]
    assert len(skipped) == 2
    assert all(s["reason"] for s in skipped)


def test_a_queue_with_nothing_eligible_is_empty_not_an_error():
    ledger = {"entries": [_entry(1, status="shipped")]}
    assert o.implementation_queue(ledger)["picks"] == []


# ── THE HAND-OVER ────────────────────────────────────────────────────────

class _FakeIssue:
    def __init__(self, fail_label=False):
        self.labels_added, self._fail = [], fail_label

    def add_to_labels(self, *names):
        if self._fail:
            raise RuntimeError("no Issues: write on this repo")
        self.labels_added.extend(names)


class _FakeRepo:
    def __init__(self, fail_dispatch=False, fail_label=False):
        self.dispatches, self._fail = [], fail_dispatch
        self.issue = _FakeIssue(fail_label)

    def create_repository_dispatch(self, event_type, client_payload=None):
        if self._fail:
            raise RuntimeError("403 Resource not accessible by personal access token")
        self.dispatches.append((event_type, client_payload))

    def get_issue(self, number):
        return self.issue


def _stub_github(monkeypatch, repo):
    monkeypatch.setattr(o, "_github", lambda: type("_GH", (), {"get_repo": lambda self, slug: repo})())


def test_dispatch_fires_the_event_then_labels_the_issue(monkeypatch):
    repo = _FakeRepo()
    _stub_github(monkeypatch, repo)
    result = o.dispatch_implementation(_entry(11, title="Fix the thing"), dry_run=False)

    assert result["status"] == "dispatched"
    event, payload = repo.dispatches[0]
    assert event == o.IMPLEMENT_EVENT
    assert payload["issue"] == 11 and payload["title"] == "Fix the thing"
    assert repo.issue.labels_added == [o.IMPLEMENTING_LABEL]


def test_a_failed_dispatch_leaves_the_issue_unlabelled(monkeypatch):
    # THE ORDERING TEST. Labelling first would mark the issue as handed over
    # when nothing was, and the gate would skip it forever after — a filed bug
    # silently dropped, which is the exact failure shape this repo keeps hitting.
    repo = _FakeRepo(fail_dispatch=True)
    _stub_github(monkeypatch, repo)
    with pytest.raises(RuntimeError):
        o.dispatch_implementation(_entry(12), dry_run=False)
    assert repo.issue.labels_added == []


def test_a_failed_label_is_reported_but_not_fatal(monkeypatch):
    # The work is already under way; raising here would report a failure for a
    # dispatch that succeeded. The PR it opens links the issue, which moves the
    # entry to in_flight and keeps it out of the next queue regardless.
    repo = _FakeRepo(fail_label=True)
    _stub_github(monkeypatch, repo)
    result = o.dispatch_implementation(_entry(13), dry_run=False)
    assert result["status"] == "dispatched_unlabelled"
    assert "Issues: write" in result["label_error"]


def test_the_tier_rides_along_in_the_payload(monkeypatch):
    repo = _FakeRepo()
    _stub_github(monkeypatch, repo)
    result = o.dispatch_implementation(_entry(15), dry_run=False, tier="heavy")

    assert result["tier"] == "heavy"
    assert repo.dispatches[0][1]["tier"] == "heavy"


def test_an_unknown_tier_falls_back_instead_of_travelling(monkeypatch):
    # THE INJECTION TEST. The tier crosses a repository_dispatch — i.e. arrives
    # over the API — and the workflow turns it into the --model argument the
    # coding agent runs on. A name that isn't a known tier must never reach that
    # argument, so it is replaced here and refused again in the workflow.
    repo = _FakeRepo()
    _stub_github(monkeypatch, repo)
    result = o.dispatch_implementation(
        _entry(16), dry_run=False, tier="opus --dangerously-skip-permissions")

    assert result["tier"] in o.IMPLEMENT_TIERS
    assert repo.dispatches[0][1]["tier"] in o.IMPLEMENT_TIERS


def test_no_tier_means_the_configured_default():
    assert o.resolve_tier(None) == o.IMPLEMENT_TIER
    assert o.resolve_tier("HEAVY") == "heavy"   # case and padding are forgiven
    assert o.resolve_tier("  light ") == "light"


def test_dry_run_touches_nothing(monkeypatch):
    repo = _FakeRepo()
    _stub_github(monkeypatch, repo)
    assert o.dispatch_implementation(_entry(14), dry_run=True)["status"] == "dry_run"
    assert repo.dispatches == [] and repo.issue.labels_added == []


# ── THE SUMMARY ──────────────────────────────────────────────────────────

_NOW = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)


def _shipped(number, days_ago, **kw):
    stamp = (_NOW - timedelta(days=days_ago)).isoformat()
    return _entry(number, status="shipped", closed=stamp, **kw)


def test_the_digest_reports_what_merged_this_week():
    ledger = {"entries": [_shipped(1, 2, title="Retry the push", fix_ref="PR #34")]}
    banner = o.delivery_banner(ledger, now=_NOW)
    assert banner.startswith("IMPLEMENTED (LAST 7 DAYS)")
    assert "overseer #1 — Retry the push (PR #34)" in banner


def test_older_work_is_not_re_reported_every_week():
    ledger = {"entries": [_shipped(1, 30)]}
    assert o.delivery_banner(ledger, now=_NOW) == ""


def test_work_awaiting_review_is_named_not_just_counted():
    # An implementer whose PRs are never reviewed looks exactly like one that
    # never ran. This line is what tells the two apart.
    ledger = {"entries": [_entry(7, status="in_flight", repo="A/ufc")]}
    banner = o.delivery_banner(ledger, now=_NOW)
    assert "1 fix(es) awaiting review: ufc#7" in banner


def test_a_quiet_week_leaves_the_digest_untouched():
    ledger = {"entries": [_entry(1, status="open"), _shipped(2, 40)]}
    assert o.delivery_banner(ledger, now=_NOW) == ""


def test_the_banner_header_renders_as_a_heading_on_the_dashboard():
    # The dashboard's formatDigest treats an ALL-CAPS line as a section heading
    # (docs/app.js) — that is how STALENESS ALERTS gets its own header. If the
    # block's first line ever stops matching, the section quietly degrades to a
    # paragraph of body text in the middle of the digest.
    import re
    from pathlib import Path

    header = o.delivery_banner(
        {"entries": [_shipped(1, 1)]}, now=_NOW).splitlines()[0]
    app_js = Path(__file__).resolve().parent.parent / "docs" / "app.js"
    pattern = re.search(r"\^\[A-Z\]\[(.+?)\]\*\$", app_js.read_text(encoding="utf-8"))
    assert pattern, "docs/app.js no longer has the heading regex this test pins"
    assert re.match(rf"^[A-Z][{pattern.group(1)}]*$", header)


def test_the_banner_survives_a_missing_ledger():
    # The ledger fetch is allowed to fail without stopping a review, so every
    # consumer of it has to tolerate None.
    assert o.delivery_banner(None) == ""
    assert o.delivery_banner({"entries": []}) == ""


# ── THE AGING BACKLOG (overseer #26) ────────────────────────────────────

def _open_enhancement(number, days_old, **kw):
    stamp = (_NOW - timedelta(days=days_old)).isoformat()
    return _entry(number, kind="enhancement", status="open", created=stamp, **kw)


def test_an_old_open_enhancement_is_reported():
    ledger = {"entries": [_open_enhancement(42, 95, title="Dead capital warning")]}
    banner = o.aging_backlog_banner(ledger, now=_NOW)
    assert banner.startswith("AGING BACKLOG (OPEN OVER 60 DAYS)")
    assert "overseer #42 — Dead capital warning (95d open)" in banner


def test_a_recent_open_enhancement_is_not_reported():
    ledger = {"entries": [_open_enhancement(1, 10)]}
    assert o.aging_backlog_banner(ledger, now=_NOW) == ""


def test_only_open_enhancements_count_toward_the_backlog():
    # Bugs and non-open issues are tracked elsewhere (implementable / delivery
    # banner); this section is specifically about untriaged IDEAS piling up.
    ledger = {"entries": [
        _entry(1, kind="bug", status="open",
               created=(_NOW - timedelta(days=90)).isoformat()),
        _entry(2, kind="enhancement", status="shipped",
               created=(_NOW - timedelta(days=90)).isoformat(),
               closed=(_NOW - timedelta(days=1)).isoformat()),
    ]}
    assert o.aging_backlog_banner(ledger, now=_NOW) == ""


def test_the_banner_counts_and_caps_the_list():
    ledger = {"entries": [_open_enhancement(n, 61 + n) for n in range(1, 9)]}
    banner = o.aging_backlog_banner(ledger, now=_NOW, limit=6)
    assert "- 8 item(s) untriaged." in banner
    assert banner.count(" open)") == 6
    assert "…and 2 more." in banner


def test_the_aging_banner_heading_renders_as_a_heading_on_the_dashboard():
    # Same contract as delivery_banner's heading: the dashboard's formatDigest
    # (docs/app.js) treats an ALL-CAPS line as a section header.
    import re
    from pathlib import Path

    header = o.aging_backlog_banner(
        {"entries": [_open_enhancement(1, 61)]}, now=_NOW).splitlines()[0]
    app_js = Path(__file__).resolve().parent.parent / "docs" / "app.js"
    pattern = re.search(r"\^\[A-Z\]\[(.+?)\]\*\$", app_js.read_text(encoding="utf-8"))
    assert pattern, "docs/app.js no longer has the heading regex this test pins"
    assert re.match(rf"^[A-Z][{pattern.group(1)}]*$", header)


def test_the_aging_banner_survives_a_missing_ledger():
    assert o.aging_backlog_banner(None) == ""
    assert o.aging_backlog_banner({"entries": []}) == ""


# ── THE WIRING ───────────────────────────────────────────────────────────

class _StubMessages:
    """Two turns: call send_telegram_summary, then stop."""

    def __init__(self):
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        usage = SimpleNamespace(input_tokens=10, output_tokens=10,
                                cache_creation_input_tokens=0,
                                cache_read_input_tokens=0)
        if self.calls == 1:
            block = SimpleNamespace(type="tool_use", id="t1",
                                    name="send_telegram_summary",
                                    input={"text": "Issues Found\n- none"})
            return SimpleNamespace(content=[block], stop_reason="tool_use", usage=usage)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="sent")],
                               stop_reason="end_turn", usage=usage)


class _StubClient:
    def __init__(self):
        self.messages = _StubMessages()


def _capture(into):
    """Stand in for send_telegram_summary, recording exactly what was sent."""
    def _send(text):
        into["text"] = text
        return {"status": "ok"}
    return _send


def test_the_digest_the_reviewer_sends_carries_the_implemented_block(tmp_path, monkeypatch):
    # THE SEAM. delivery_banner existing proves nothing — run_agent has to
    # actually append it on the way out, the same way the staleness banner is
    # prepended, so Telegram and the dashboard both carry it. The Reviewer is
    # never told to write this section and must not have to be: a summary that
    # depends on an agent remembering it is one that eventually goes quiet.
    sent = {}
    monkeypatch.setitem(o.TOOL_FUNCTIONS, "send_telegram_summary", _capture(sent))

    t = RunTracer(jsonl_path=str(tmp_path / "x.jsonl"), html_path=str(tmp_path / "x.html"))
    t.heavy_model = o.MODEL
    t.ledger = {"entries": [_shipped(1, 1, title="Retry the push", fix_ref="PR #34"),
                            _open_enhancement(2, 61, title="Dead capital warning")]}

    o.run_agent(_StubClient(), agent="Reviewer", system="s",
                tool_names=["send_telegram_summary"], user_message="go", tracer=t)

    assert "Issues Found" in sent["text"]
    assert "IMPLEMENTED (LAST 7 DAYS)" in sent["text"]
    assert "AGING BACKLOG (OPEN OVER 60 DAYS)" in sent["text"]
    # The Reviewer's own words come first, then what shipped, then what's aged.
    assert (sent["text"].index("Issues Found") < sent["text"].index("IMPLEMENTED")
            < sent["text"].index("AGING BACKLOG"))
    assert t.digest_text == sent["text"]   # what the dashboard and push notification read


def test_a_run_without_a_ledger_sends_the_digest_unchanged(tmp_path, monkeypatch):
    sent = {}
    monkeypatch.setitem(o.TOOL_FUNCTIONS, "send_telegram_summary", _capture(sent))
    t = RunTracer(jsonl_path=str(tmp_path / "y.jsonl"), html_path=str(tmp_path / "y.html"))
    t.heavy_model = o.MODEL   # no .ledger at all — the fetch failed this run

    o.run_agent(_StubClient(), agent="Reviewer", system="s",
                tool_names=["send_telegram_summary"], user_message="go", tracer=t)

    assert sent["text"] == "Issues Found\n- none"


# ── THE DASHBOARD PANEL ──────────────────────────────────────────────────

def test_the_queue_panel_reports_what_the_next_run_will_attempt():
    ledger = {"entries": [_entry(1), _entry(2, repo="A/ufc"),
                          _entry(3, kind="enhancement", effort="high")]}
    q = o.queue_state(ledger, limit=3)
    assert [e["number"] for e in q["next"]] == [1, 2]
    assert q["cap"] == 3 and q["tier"] == o.IMPLEMENT_TIER
    assert q["eligible"] == 2


def test_work_already_handed_over_shows_as_under_way_before_a_pr_exists():
    # The dispatcher labels an issue the moment it hands it over, and the agent
    # takes ten minutes to push. Keying "under way" on the linked PR alone would
    # make a just-dispatched issue vanish from the panel for that whole window.
    ledger = {"entries": [_entry(4, labels=[o.IMPLEMENTING_LABEL]),
                          _entry(5, status="in_flight", fix_ref="PR #9")]}
    q = o.queue_state(ledger)
    assert {e["number"] for e in q["in_flight"]} == {4, 5}


def test_a_failed_label_on_settled_work_is_not_reported_as_stalled():
    # THE ONE THAT BIT ME. These labels are never cleaned off a closed issue, so
    # keying on the label alone kept reporting overseer#26 as needing attention
    # after it had failed once on a dry API key and then shipped.
    ledger = {"entries": [_shipped(26, 1, labels=[o.FAILED_LABEL]),
                          _entry(27, labels=[o.FAILED_LABEL])]}
    q = o.queue_state(ledger)
    assert [e["number"] for e in q["benched"]] == [27]


def test_the_panel_is_hidden_rather_than_empty_without_a_ledger():
    assert o.queue_state(None) is None
    assert o.queue_state({"entries": []}) is None


def test_the_published_ledger_carries_the_queue_for_the_dashboard(tmp_path):
    # The panel refreshes on the hourly ledger job precisely because the queue
    # rides along in the same file — no second workflow, no model calls.
    import json
    path = tmp_path / "shipped.json"
    o.write_ledger({"entries": [_entry(1)], "totals": {}}, str(path))
    assert json.loads(path.read_text())["queue"]["next"][0]["number"] == 1


def test_the_dashboard_renders_the_queue_it_is_given():
    # No JS test runner here, so pin the seam instead: app.js must render into an
    # element index.html actually has, and must read the published queue block
    # rather than re-deriving the gate (a second copy would drift from
    # tools.implementable and describe a queue that never runs).
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    app = (root / "docs" / "app.js").read_text(encoding="utf-8")
    page = (root / "docs" / "index.html").read_text(encoding="utf-8")

    assert "renderImplementer(ledger && ledger.queue)" in app
    for element in ("implementer-card", "implementer"):
        assert f'id="{element}"' in page, f"app.js writes to #{element}; index.html lacks it"
