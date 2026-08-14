"""Tests for the delivery ledger (what the pipeline shipped, not just proposed).

The ledger does two jobs and both are load-bearing:
  * it is the dashboard's record of DELIVERED work, so "shipped" has to mean a
    merged fix rather than merely a closed issue;
  * it is the agents' dedupe context, so a re-proposed idea has to be visible
    as a duplicate rather than quietly inflating the delivery rate.
"""

from datetime import datetime, timezone

import tools as o


class _Label:
    def __init__(self, name):
        self.name = name


class _Repo:
    def __init__(self, full_name):
        self.full_name = full_name


class _Issue:
    """Minimal stand-in for a PyGithub Issue."""

    def __init__(self, number, title, *, body="", labels=(), state="open",
                 state_reason=None, repo="A/overseer", merged=False,
                 created=None, closed=None):
        self.number = number
        self.title = title
        self.body = body
        self.labels = [_Label(l) for l in labels]
        self.state = state
        self.state_reason = state_reason
        self.repository = _Repo(repo)
        self.html_url = f"https://github.com/{repo}/issues/{number}"
        self.created_at = created or datetime(2026, 7, 1, tzinfo=timezone.utc)
        self.closed_at = closed or (datetime(2026, 8, 1, tzinfo=timezone.utc)
                                    if state == "closed" else None)
        self.pull_request = None
        self._merged = merged


def _entry(issue, merged_only=True, monkeypatch=None):
    return o._ledger_entry(issue, merged_only=merged_only)


class _PR:
    """Stand-in for a linked pull request."""

    def __init__(self, merged, state="closed", number=99):
        self.merged, self.state, self.number = merged, state, number
        self.html_url = f"https://github.com/A/overseer/pull/{number}"
        self.merge_commit_sha = "abc1234def" if merged else None


def _stub_merge(monkeypatch, merged):
    """Point _ledger_entry's PR lookup at a fake linked PR."""
    monkeypatch.setattr(o, "_linked_prs",
                        lambda issue: [_PR(merged, "closed" if merged else "open")])


def test_overseer_filed_issues_are_recognised_by_marker():
    issue = _Issue(1, "Something broke", body=f"evidence\n\n---\n{o.OVERSEER_MARKER}")
    assert _entry(issue)["number"] == 1


def test_hand_written_issues_are_ignored():
    # Not ours: no marker, no enhancement prefix, no effort label. Counting these
    # would inflate the delivery rate with work the pipeline never proposed.
    assert _entry(_Issue(2, "my own todo", body="just a note")) is None


def test_legacy_enhancements_without_the_marker_still_count():
    # Enhancements filed before file_issue stamped the marker are still ours,
    # recognisable by the title prefix and effort/impact labels.
    issue = _Issue(3, "[enhancement] Add a thing", labels=["enhancement", "effort:low"])
    entry = _entry(issue)
    assert entry["kind"] == "enhancement"
    assert entry["effort"] == "low"


def test_closed_issue_with_a_merged_fix_is_shipped(monkeypatch):
    _stub_merge(monkeypatch, True)
    issue = _Issue(4, "Fix it", body=o.OVERSEER_MARKER, state="closed",
                   state_reason="completed")
    assert _entry(issue)["status"] == "shipped"


def test_closed_issue_with_an_OPEN_pr_is_in_flight(monkeypatch):
    # THE HONESTY TEST. A fix sitting in review is not delivered — this is what
    # stops the panel taking credit for unmerged code.
    monkeypatch.setattr(o, "_linked_prs", lambda i: [_PR(False, "open", 8)])
    issue = _Issue(5, "Fix it", body=o.OVERSEER_MARKER, state="closed",
                   state_reason="completed")
    entry = _entry(issue)
    assert entry["status"] == "in_flight"
    assert entry["fix_ref"] == "PR #8 (open)"


def test_closed_issue_with_no_pr_at_all_is_shipped(monkeypatch):
    # THE BUG THE FIRST REFRESH EXPOSED. These repos land most work by direct
    # commit to main, so "no linked PR" is the normal case for delivered work —
    # not proof it is pending. Treating absence of a PR as non-delivery reported
    # six coachvision features from June, long live in production, as in flight.
    monkeypatch.setattr(o, "_linked_prs", lambda i: [])
    monkeypatch.setattr(o, "_closing_commit", lambda i: None)
    issue = _Issue(5, "Fix it", body=o.OVERSEER_MARKER, state="closed",
                   state_reason="completed")
    assert _entry(issue)["status"] == "shipped"


def test_a_direct_commit_closure_records_where_it_landed(monkeypatch):
    monkeypatch.setattr(o, "_linked_prs", lambda i: [])
    monkeypatch.setattr(o, "_closing_commit", lambda i: "deadbeefcafe")
    issue = _Issue(5, "Fix it", body=o.OVERSEER_MARKER, state="closed",
                   state_reason="completed")
    entry = _entry(issue)
    assert entry["status"] == "shipped"
    assert entry["fix_ref"] == "deadbee"
    assert entry["fix_url"].endswith("/commit/deadbeefcafe")


def test_merged_only_false_counts_any_closed_issue(monkeypatch):
    monkeypatch.setattr(o, "_linked_prs", lambda i: [_PR(False, "open", 9)])
    issue = _Issue(6, "Fix it", body=o.OVERSEER_MARKER, state="closed",
                   state_reason="completed")
    assert _entry(issue, merged_only=False)["status"] == "shipped"


def test_duplicates_are_kept_and_never_read_as_shipped():
    # Duplicates are evidence the dedupe is failing, which is half the reason
    # the ledger exists — so they're retained, not discarded.
    issue = _Issue(7, "[enhancement] Same idea again", body=o.OVERSEER_MARKER,
                   state="closed", state_reason="duplicate")
    assert _entry(issue)["status"] == "duplicate"


def test_not_planned_is_distinct_from_shipped():
    issue = _Issue(8, "[enhancement] Nope", body=o.OVERSEER_MARKER,
                   state="closed", state_reason="not_planned")
    assert _entry(issue)["status"] == "not_planned"


def test_open_issues_are_tracked_as_open():
    issue = _Issue(9, "[enhancement] Pending", body=o.OVERSEER_MARKER)
    assert _entry(issue)["status"] == "open"


def test_shipped_entries_record_where_the_fix_landed(monkeypatch):
    # "shipped" without a link means "go dig through commit history" — the
    # panel has to say WHERE, not just whether.
    _stub_merge(monkeypatch, True)
    issue = _Issue(11, "Fix", body=o.OVERSEER_MARKER, state="closed",
                   state_reason="completed")
    entry = _entry(issue)
    assert entry["fix_url"].endswith("/pull/99")
    assert entry["fix_ref"] == "PR #99"
    assert entry["fix_sha"] == "abc1234"


def test_open_issue_with_a_pending_pr_reads_as_in_flight(monkeypatch):
    monkeypatch.setattr(o, "_linked_prs", lambda issue: [_PR(False, "open", 42)])
    entry = _entry(_Issue(12, "[enhancement] Pending", body=o.OVERSEER_MARKER))
    assert entry["status"] == "in_flight"
    assert entry["fix_ref"] == "PR #42"


def test_unreadable_timeline_does_not_crash_the_run():
    # Missing permission on the timeline API must degrade, not raise. With no PR
    # evidence either way, a completed closure still counts as delivered — the
    # enrichment is for the "where", not for deciding the "whether".
    class _Boom(_Issue):
        def get_timeline(self):
            raise RuntimeError("no permission")

    issue = _Boom(10, "Fix", body=o.OVERSEER_MARKER, state="closed",
                  state_reason="completed")
    entry = o._ledger_entry(issue)
    assert entry["status"] == "shipped"
    assert "fix_url" not in entry


# --- aggregate maths --------------------------------------------------------

def _ledger(*statuses):
    entries = [{"repo": "A/x", "number": i, "title": f"t{i}", "status": s,
                "kind": "enhancement", "url": "", "closed_at": None,
                "created_at": "2026-07-01"}
               for i, s in enumerate(statuses, 1)]
    totals = {"proposed": len(entries)}
    for s in ("shipped", "in_flight", "open", "duplicate", "not_planned"):
        totals[s] = sum(1 for e in entries if e["status"] == s)
    considered = totals["proposed"] - totals["duplicate"]
    totals["delivery_rate"] = round(totals["shipped"] / considered, 3) if considered else 0.0
    totals["duplicate_rate"] = round(totals["duplicate"] / totals["proposed"], 3) if entries else 0.0
    return {"entries": entries, "totals": totals, "errors": {}}


def test_duplicates_are_excluded_from_the_delivery_denominator():
    # Re-proposing the same idea shouldn't drag down the delivery rate — that
    # would punish the pipeline twice for one dedupe failure. It's reported
    # separately instead.
    led = _ledger("shipped", "open", "duplicate", "duplicate")
    assert led["totals"]["delivery_rate"] == 0.5    # 1 shipped of 2 non-duplicates
    assert led["totals"]["duplicate_rate"] == 0.5   # but 2 of 4 filings were dupes


def test_known_work_block_marks_each_state_for_the_agents():
    text = o.known_work_block(_ledger("shipped", "open", "duplicate"))
    assert "SHIPPED" in text and "STILL OPEN" in text and "duplicate" in text
    assert "#1" in text


def test_known_work_block_is_explicit_when_there_is_no_record():
    # An empty string would leave the agent unable to tell "nothing filed yet"
    # from "the ledger failed to load".
    assert "no previously filed issues" in o.known_work_block({"entries": []})


def test_write_ledger_skips_a_failed_fetch(tmp_path):
    # A transient GitHub error must not blank the published panel.
    target = tmp_path / "shipped.json"
    target.write_text('{"entries": [{"keep": true}]}', encoding="utf-8")
    assert o.write_ledger(None, str(target)) is None
    assert "keep" in target.read_text(encoding="utf-8")


def test_write_ledger_persists_and_timestamps(tmp_path):
    import json
    target = tmp_path / "shipped.json"
    o.write_ledger(_ledger("shipped"), str(target))
    written = json.loads(target.read_text(encoding="utf-8"))
    assert written["totals"]["shipped"] == 1
    assert written["generated"].endswith("Z")


# --- incremental refresh (keeping the panel live between weekly runs) --------

class _ListRepo:
    def __init__(self, full_name, issues):
        self.full_name, self._issues = full_name, issues

    def get_issues(self, state="all"):
        return self._issues


def _single_repo(monkeypatch, issues, slug="A/overseer"):
    """Configure exactly one reviewed project, served from `issues`."""
    for key in ("trading_bot", "volleyball", "ufc"):
        monkeypatch.setitem(o.PROJECTS[key], "repo", None)
    monkeypatch.setitem(o.PROJECTS["overseer"], "repo", slug)
    for i in issues:
        i.repository = _Repo(slug)

    class _GH:
        def get_repo(self, s):
            return _ListRepo(s, issues)
    monkeypatch.setattr(o, "_github", lambda: _GH())


def test_settled_outcomes_are_carried_forward_without_a_timeline_call(monkeypatch):
    # The optimisation that makes an hourly refresh cheap: the per-issue PR
    # lookup is the expensive call, and a shipped fix cannot un-ship.
    issue = _Issue(1, "Done", body=o.OVERSEER_MARKER, state="closed",
                   state_reason="completed")
    _single_repo(monkeypatch, [issue])
    calls = []
    monkeypatch.setattr(o, "_linked_prs", lambda i: calls.append(i) or [])

    known = {"entries": [{"repo": "A/overseer", "number": 1, "title": "Done",
                          "status": "shipped", "kind": "bug", "url": "",
                          "created_at": "2026-07-01", "closed_at": "2026-08-01"}]}
    led = o.delivery_ledger(known=known)
    assert led["totals"]["shipped"] == 1
    assert calls == [], "a settled entry must not trigger a timeline lookup"


def test_live_entries_are_always_rechecked(monkeypatch):
    # in_flight is NOT terminal — it is precisely the state that should flip to
    # shipped the moment a PR merges, which is the whole point of refreshing.
    issue = _Issue(2, "Pending", body=o.OVERSEER_MARKER, state="closed",
                   state_reason="completed")
    _single_repo(monkeypatch, [issue])
    monkeypatch.setattr(o, "_linked_prs", lambda i: [_PR(True, "closed", 7)])

    known = {"entries": [{"repo": "A/overseer", "number": 2, "title": "Pending",
                          "status": "in_flight", "kind": "bug", "url": "",
                          "created_at": "2026-07-01", "closed_at": None}]}
    led = o.delivery_ledger(known=known)
    assert led["entries"][0]["status"] == "shipped"
    assert led["entries"][0]["fix_ref"] == "PR #7"


def test_a_reopened_issue_loses_its_settled_status(monkeypatch):
    # Carrying a terminal status forward on a reopened issue would leave the
    # panel asserting an outcome that no longer holds.
    issue = _Issue(3, "[enhancement] Back again", body=o.OVERSEER_MARKER, state="open")
    _single_repo(monkeypatch, [issue])
    monkeypatch.setattr(o, "_linked_prs", lambda i: [])

    known = {"entries": [{"repo": "A/overseer", "number": 3, "title": "Back again",
                          "status": "shipped", "kind": "enhancement", "url": "",
                          "created_at": "2026-07-01", "closed_at": "2026-08-01"}]}
    led = o.delivery_ledger(known=known)
    assert led["entries"][0]["status"] == "open"


def test_full_rebuild_ignores_the_published_ledger(monkeypatch):
    issue = _Issue(4, "Done", body=o.OVERSEER_MARKER, state="closed",
                   state_reason="completed")
    _single_repo(monkeypatch, [issue])
    monkeypatch.setattr(o, "_linked_prs", lambda i: [])
    known = {"entries": [{"repo": "A/overseer", "number": 4, "title": "Done",
                          "status": "shipped", "kind": "bug", "url": "",
                          "created_at": "2026-07-01", "closed_at": "2026-08-01"}]}
    # known=None is what --full passes: no carry-forward, recomputed from live
    # state (no pending PR -> delivered).
    assert o.delivery_ledger(known=None)["entries"][0]["status"] == "shipped"
    assert o.delivery_ledger(known=known)["entries"][0]["status"] == "shipped"
