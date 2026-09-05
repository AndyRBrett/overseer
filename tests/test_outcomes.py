"""Per-project proposal outcomes, and the feedback loop they close (#60).

The dashboard's delivery rate is one number across four projects. It is the
right headline and the wrong feedback signal: it cannot tell the Idea Agent that
its coachvision ideas ship and its crypto risk ideas get closed as not-planned,
which is the only thing that would make next week's batch better.
"""

import json
import pathlib

import agent_idea
import tools

LEDGER = {"entries": [
    # coachvision: 3 shipped, 1 not planned, 1 open  → settled 4, ships at 75%
    *[{"repo": "o/coach", "kind": "enhancement", "status": "shipped"} for _ in range(3)],
    {"repo": "o/coach", "kind": "enhancement", "status": "not_planned"},
    {"repo": "o/coach", "kind": "enhancement", "status": "open"},
    # crypto: 1 shipped, 1 duplicate → settled 2, below the sample floor
    {"repo": "o/crypto", "kind": "enhancement", "status": "shipped"},
    {"repo": "o/crypto", "kind": "enhancement", "status": "duplicate"},
    # a bug, which is the Bug-Hunter's hit rate and not an idea's outcome
    {"repo": "o/coach", "kind": "bug", "status": "shipped"},
]}


def test_ship_rate_counts_only_proposals_that_got_an_answer():
    row = tools.proposal_outcomes(LEDGER)["by_repo"]["o/coach"]
    # 3 shipped of 4 settled — the open one is untriaged, not rejected. Counting
    # it would make every project's rate fall simply because the agent kept filing.
    assert row["settled"] == 4 and row["ship_rate"] == 0.75
    assert row["open"] == 1


def test_bugs_are_not_counted_as_proposals():
    row = tools.proposal_outcomes(LEDGER)["by_repo"]["o/coach"]
    assert row["proposed"] == 5      # the shipped bug is excluded


def test_a_rate_is_withheld_below_the_sample_floor():
    # One shipped and one duplicate is not "50% — favour other categories".
    row = tools.proposal_outcomes(LEDGER)["by_repo"]["o/crypto"]
    assert row["ship_rate"] is None and row["sample"] == "too small"


def test_the_overall_row_aggregates_every_repo():
    overall = tools.proposal_outcomes(LEDGER)["overall"]
    assert overall["shipped"] == 4 and overall["proposed"] == 7


def test_an_empty_ledger_does_not_crash():
    out = tools.proposal_outcomes({"entries": []})
    assert out["by_repo"] == {} and out["overall"]["ship_rate"] is None


def test_the_prompt_block_says_why_a_rate_is_missing():
    block = tools.outcomes_block(tools.proposal_outcomes(LEDGER))
    assert "75%" in block
    # A silent "—" reads as a bug in the panel; say it out loud.
    assert "too small a sample" in block


def test_the_idea_agent_is_told_to_calibrate_what_it_proposes_not_whether():
    prompt = agent_idea.build_system_prompt(outcomes="o/coach: 75% ...")
    assert "o/coach: 75%" in prompt
    # The failure mode: an agent that reads a low rate and stops filing for that
    # project entirely.
    assert "never\nwhether" in prompt or "never whether" in prompt.replace("\n", " ")


def test_the_ledger_carries_its_own_outcomes_so_consumers_cannot_disagree():
    # The dashboard, the ask pack and the agent prompt all read this one block.
    ledger = dict(LEDGER)
    ledger["outcomes"] = tools.proposal_outcomes(ledger)
    assert ledger["outcomes"]["by_repo"]["o/coach"]["ship_rate"] == 0.75


def test_write_ledger_backfills_outcomes_for_a_ledger_that_predates_them(tmp_path):
    path = tmp_path / "shipped.json"
    tools.write_ledger(dict(LEDGER), str(path))
    published = json.loads(path.read_text(encoding="utf-8"))
    assert published["outcomes"]["by_repo"]["o/coach"]["ship_rate"] == 0.75


def test_the_dashboard_renders_the_published_block_and_does_not_recompute_it():
    # Invariant 4, one panel further: a second copy of this arithmetic in app.js
    # would drift the first time the definition of "settled" moved.
    source = pathlib.Path("docs/app.js").read_text(encoding="utf-8")
    assert "ledger.outcomes" in source and "ship_rate" in source
    # It must read the published rate, never derive one from the entry list.
    assert "not_planned + " not in source
