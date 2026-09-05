"""The duplicate check that stops the pipeline re-filing its own ideas (#33).

Twelve of the issues this pipeline has filed were later closed as duplicates,
despite every one of those runs being handed a list of what was already on
record. That list is a request to remember; these tests pin the check that runs
instead.
"""

import pytest

import dedupe
import tools


LEDGER = {"entries": [
    {"repo": "o/overseer", "number": 26, "kind": "enhancement", "status": "shipped",
     "title": "Publish an aging-backlog block in the weekly digest"},
    {"repo": "o/overseer", "number": 41, "kind": "enhancement", "status": "open",
     "title": "Send a push notification when the weekly review finishes"},
    {"repo": "o/crypto", "number": 44, "kind": "enhancement", "status": "shipped",
     "title": "Add a transaction-cost-aware signal gate"},
    {"repo": "o/overseer", "number": 12, "kind": "bug", "status": "open",
     "title": "Ledger refresh races its own published commit"},
]}


@pytest.fixture
def index():
    return dedupe.index_from_ledger(LEDGER)


def test_a_reworded_proposal_scores_as_a_duplicate(index):
    matches = index.query("Publish an aging backlog section in the weekly digest",
                          repo="o/overseer")
    assert matches and matches[0]["number"] == 26
    assert dedupe.verdict(matches[0]["score"]) == "duplicate"


def test_an_unrelated_proposal_is_clear(index):
    matches = index.query("Render the fight card as a printable one-pager",
                          repo="o/overseer")
    assert dedupe.verdict(matches[0]["score"] if matches else None) == "clear"


def test_scoring_never_crosses_repos(index):
    # The same words in two projects are two pieces of work. Scoring across
    # repos would manufacture duplicates out of a shared vocabulary.
    matches = index.query("Add a transaction-cost-aware signal gate", repo="o/overseer")
    assert not [m for m in matches if m["number"] == 44]


def test_bugs_are_indexed_alongside_enhancements(index):
    # The same defect filed as a bug and proposed as an enhancement is one piece
    # of work arriving twice.
    matches = index.query("Ledger refresh races its own published commit",
                          repo="o/overseer")
    assert matches[0]["kind"] == "bug"


def test_the_rationale_cannot_outweigh_the_title(index):
    # Every proposal's rationale shares the same boilerplate ("the pipeline",
    # "this would let us"). Weighting it equally scored unrelated overseer ideas
    # at 0.6 on that alone.
    matches = index.query(
        "Render the fight card as a printable one-pager",
        rationale="This would let the weekly digest publish an aging backlog "
                  "block so open ideas do not sit untriaged in the pipeline.",
        repo="o/overseer")
    assert all(m["score"] < dedupe.DUPLICATE_THRESHOLD for m in matches)


def test_an_empty_index_matches_nothing():
    assert dedupe.DuplicateIndex([]).query("anything") == []
    assert dedupe.index_from_ledger(None).query("anything") == []


# ── the tool + the gate ──────────────────────────────────────────────────


@pytest.fixture
def indexed(monkeypatch):
    monkeypatch.setattr(tools, "_DUP_INDEX", dedupe.index_from_ledger(LEDGER))


def test_check_duplicate_reports_the_gates_own_verdict(indexed):
    result = tools.check_duplicate(
        title="Publish an aging backlog section in the weekly digest",
        repo="o/overseer")
    assert result["verdict"] == "duplicate"
    assert result["matches"][0]["number"] == 26
    assert result["threshold"] == dedupe.DUPLICATE_THRESHOLD


def test_check_duplicate_without_an_index_says_so_and_does_not_block(monkeypatch):
    # A ledger that failed to load must not cost a week of ideas.
    monkeypatch.setattr(tools, "_DUP_INDEX", None)
    result = tools.check_duplicate(title="anything", repo="o/overseer")
    assert result["status"] == "unavailable" and result["verdict"] == "clear"


def test_propose_enhancement_refuses_a_refiling_without_calling_github(indexed, monkeypatch):
    monkeypatch.setattr(tools, "DRY_RUN", False)
    monkeypatch.setattr(tools, "_github", lambda: pytest.fail("GitHub was called"))
    result = tools.propose_enhancement(
        repo="o/overseer",
        title="Publish an aging backlog section in the weekly digest",
        rationale="Show how long open ideas have sat.", effort="low", impact="medium")
    assert result["status"] == "duplicate"
    assert result["match"]["number"] == 26
    # The refusal has to say what to do next, or the agent re-tries the same idea.
    assert "extends=26" in result["detail"]


def test_the_refusal_is_a_result_not_an_exception(indexed, monkeypatch):
    # A raised tool error reads to the agent as a broken pipeline to route
    # around; this is a decision it should act on by proposing something else.
    monkeypatch.setattr(tools, "DRY_RUN", False)
    monkeypatch.setattr(tools, "_github", lambda: pytest.fail("GitHub was called"))
    tools.propose_enhancement(repo="o/overseer",
                              title="Publish an aging backlog section in the weekly digest",
                              rationale="x", effort="low", impact="medium")


def test_extends_is_the_only_override_and_it_is_recorded(indexed, monkeypatch):
    filed = {}

    class _Issue:
        number, html_url = 99, "https://example/99"

        def add_to_labels(self, *names):
            pass

    class _Repo:
        def create_issue(self, title, body):
            filed["title"], filed["body"] = title, body
            return _Issue()

    monkeypatch.setattr(tools, "DRY_RUN", False)
    monkeypatch.setattr(tools, "_github", lambda: type("G", (), {"get_repo": lambda s, r: _Repo()})())
    result = tools.propose_enhancement(
        repo="o/overseer", title="Publish an aging backlog section in the weekly digest",
        rationale="Now with per-repo ageing.", effort="low", impact="medium", extends=26)
    assert result["status"] == "logged"
    # On the issue, where a human triaging it can weigh the claim.
    assert "Extends #26." in filed["body"]


def test_a_genuinely_new_idea_still_files(indexed, monkeypatch):
    class _Issue:
        number, html_url = 100, "https://example/100"

        def add_to_labels(self, *names):
            pass

    monkeypatch.setattr(tools, "DRY_RUN", False)
    monkeypatch.setattr(
        tools, "_github",
        lambda: type("G", (), {"get_repo": lambda s, r: type(
            "R", (), {"create_issue": lambda s, title, body: _Issue()})()})())
    result = tools.propose_enhancement(
        repo="o/overseer", title="Render the fight card as a printable one-pager",
        rationale="Nothing like this exists.", effort="low", impact="low")
    assert result["status"] == "logged"


def test_the_idea_agent_is_told_to_check_first():
    import agent_idea
    # Order matters: the check is only useful before the filing.
    assert agent_idea.TOOL_NAMES.index("check_duplicate") < \
           agent_idea.TOOL_NAMES.index("propose_enhancement")
    prompt = agent_idea.build_system_prompt()
    assert "check_duplicate" in prompt and "extends" in prompt


def test_the_schema_documents_the_override():
    schema = tools.TOOL_SCHEMAS["propose_enhancement"]["input_schema"]
    assert "extends" in schema["properties"]
    # Never required — the normal path must not ask the agent to justify itself.
    assert "extends" not in schema["required"]


def test_a_dry_run_shows_the_refusal_a_real_run_would_make(indexed, monkeypatch):
    # A --dry-run that files what production would reject is a rehearsal of a
    # different pipeline.
    monkeypatch.setattr(tools, "DRY_RUN", True)
    result = tools.propose_enhancement(
        repo="o/overseer", title="Publish an aging backlog section in the weekly digest",
        rationale="x", effort="low", impact="medium")
    assert result["status"] == "duplicate"


def test_the_dashboard_shows_that_the_gate_did_something():
    # A gate whose effect is invisible looks identical to a week with no
    # re-proposals, which is how a working guard gets removed as pointless.
    import pathlib
    app = pathlib.Path("docs/app.js").read_text(encoding="utf-8")
    assert "duplicates_blocked" in app
