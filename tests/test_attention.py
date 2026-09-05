"""The per-project attention score and the ranking it drives (#25).

A binary fresh/stale panel says all four projects are fine while one has never
ingested footage, one is trading through a negative Sharpe and one is about to
exhaust its odds budget. These pin the composition — which is the part that can
quietly stop meaning anything.
"""

import json

import attention
import tools
from tracer import RunTracer


def _tracer(tmp_path):
    t = RunTracer(jsonl_path=str(tmp_path / "x.jsonl"), html_path=str(tmp_path / "x.html"))
    t.read_tools = {"read_trading_bot_log": "Trading bot",
                    "read_ufc_scraper_status": "UFC dashboard"}
    t.read_repos = {"read_trading_bot_log": "o/crypto",
                    "read_ufc_scraper_status": "o/ufc"}
    return t


# ── the signals ──────────────────────────────────────────────────────────


def test_a_project_we_cannot_read_outranks_one_merely_reporting_bad_numbers():
    blind = attention.score_project({"status": "blind", "blind_cycles": 3})
    bad = attention.score_project({"status": "ok"}, data={"sharpe_90d": -2.0})
    assert blind["score"] > bad["score"]


def test_staleness_scales_with_how_far_past_the_sla_a_feed_is():
    # coachvision 12h past a weekly deadline is not crypto 153h past a daily one.
    near = attention.score_project({"status": "stale", "age_hours": 200, "sla_hours": 192})
    far = attention.score_project({"status": "stale", "age_hours": 153, "sla_hours": 48})
    assert far["signals"]["staleness"] > near["signals"]["staleness"]


def test_a_healthy_project_with_no_backlog_scores_zero():
    row = attention.score_project({"status": "ok"}, data={"trades": 5})
    assert row["score"] == 0.0
    assert "healthy" in row["why"]


def test_publishing_nothing_recognised_is_not_a_problem_signal():
    # Otherwise the projects that publish least rank as the healthiest.
    assert attention.kpi_signal({"app": "x", "notes": "hello"}) == (0.0, None)


def test_the_domain_kpis_issue_25_names_are_all_read():
    sharpe, _ = attention.kpi_signal({"sharpe_90d": -0.8})
    budget, _ = attention.kpi_signal({"odds_budget_used_pct": 94})
    accuracy, _ = attention.kpi_signal({"data": {"detection_accuracy": 0.4}})
    assert sharpe > 0 and budget > 0 and accuracy > 0


def test_a_positive_sharpe_is_not_a_problem():
    assert attention.kpi_signal({"sharpe_90d": 1.4}) == (0.0, None)


def test_a_budget_only_counts_once_it_is_nearly_gone():
    assert attention.kpi_signal({"quota_used_pct": 40}) == (0.0, None)
    assert attention.kpi_signal({"quota_used_pct": 99})[0] > 0.9


def test_a_junk_kpi_value_does_not_crash_the_digest():
    # One of four projects will one day publish "sharpe": "n/a".
    assert attention.kpi_signal({"sharpe": "n/a", "failures": None}) == (0.0, None)
    assert attention.error_signal({"failures": "lots"}) == (0.0, None)


def test_the_backlog_signal_saturates():
    assert attention.backlog_signal(30)[0] == attention.backlog_signal(
        attention.BACKLOG_FULL)[0] == 1.0
    assert attention.backlog_signal(0) == (0.0, None)


def test_a_full_backlog_cannot_outrank_a_dead_feed():
    # The failure mode this weighting exists to prevent: a ranking that sends you
    # to the healthiest project because its idea list is longest.
    busy = attention.score_project({"status": "ok"}, open_ideas=50)
    dark = attention.score_project({"status": "error"})
    assert dark["score"] > busy["score"]
    assert "cannot see" in dark["why"] or "unreadable" in dark["why"]


# ── the ranking ──────────────────────────────────────────────────────────


def test_rank_orders_by_score_and_breaks_ties_alphabetically():
    ranked = attention.rank({"Zeta": {"status": "ok"}, "Alpha": {"status": "ok"},
                             "Broken": {"status": "error"}})
    assert [r["name"] for r in ranked] == ["Broken", "Alpha", "Zeta"]


def test_the_why_names_the_biggest_contributor_not_the_biggest_signal():
    # A saturated backlog is 0.15 of the score; it must not headline over a feed
    # that is 400h past its SLA.
    row = attention.score_project({"status": "stale", "age_hours": 400, "sla_hours": 192},
                                  open_ideas=50)
    assert "400" in row["why"]


def test_the_banner_is_a_heading_the_dashboard_will_render():
    ranked = attention.rank({"Trading bot": {"status": "error"}})
    banner = attention.banner(ranked)
    heading = banner.splitlines()[0]
    # formatDigest treats an ALL-CAPS line WITHOUT ':' or '>' as a section
    # heading; one using either silently degrades to body text.
    assert heading == heading.upper() and ":" not in heading and ">" not in heading


def test_the_banner_is_empty_when_nothing_scores():
    assert attention.banner(attention.rank({"Trading bot": {"status": "ok"}})) == ""


# ── the wiring ───────────────────────────────────────────────────────────


def test_the_tracer_joins_telemetry_to_the_ledgers_open_ideas(tmp_path):
    t = _tracer(tmp_path)
    t.tool_call(0, "read_trading_bot_log", {},
                json.dumps({"status": "ok", "trades": 4, "sharpe_90d": -0.5}), False)
    t.tool_call(0, "read_ufc_scraper_status", {}, json.dumps({"status": "ok", "runs_7d": 9}), False)
    t.ledger = {"entries": [
        {"repo": "o/ufc", "status": "open", "kind": "enhancement"},
        {"repo": "o/ufc", "status": "open", "kind": "enhancement"},
        {"repo": "o/ufc", "status": "shipped", "kind": "enhancement"},
    ]}
    ranked = {r["name"]: r for r in t.attention()}
    assert ranked["UFC dashboard"]["signals"]["backlog"] > 0
    # Shipped work is not backlog.
    assert ranked["UFC dashboard"]["signals"]["backlog"] == round(2 / attention.BACKLOG_FULL, 3)
    assert ranked["Trading bot"]["signals"]["backlog"] == 0


def test_the_digest_publishes_the_ranking_for_the_dashboard(tmp_path):
    t = _tracer(tmp_path)
    t.tool_call(0, "read_trading_bot_log", {}, json.dumps({"status": "not_configured"}), False)
    path = tmp_path / "digest.json"
    t.write_digest(str(path))
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["attention"][0]["name"] == "Trading bot"
    # The reason travels with the score: an ordering nobody can interrogate is
    # one that stops being trusted the first time it surprises you.
    assert payload["attention"][0]["why"]


def test_read_tool_repos_covers_every_read_tool():
    # The join is keyed on the tool, not the display name — a project can rename
    # itself from its own status file, and coachvision has.
    assert set(tools.READ_TOOL_PROJECT) == set(tools.READ_TOOLS)
    for key in tools.READ_TOOL_PROJECT.values():
        assert key in tools.PROJECTS


def test_the_ranking_is_stitched_into_the_digest_deterministically():
    # Invariant 7: a section that depends on an agent remembering to write it
    # will one day go quiet with nothing failing. Pin that run_agent — not a
    # prompt — is what puts it there.
    import inspect
    source = inspect.getsource(tools.run_agent)
    assert "attention_banner" in source


# ── plain English ────────────────────────────────────────────────────────
# The verdict a stranger reads first. Generated here, next to the scorer,
# because two sentences describing one finding in two languages drift — and the
# plain one is the one people actually read.


def test_the_plain_reason_describes_the_signal_that_ranked_it():
    stale = attention.rank({"coachvision": {"status": "stale", "age_hours": 400,
                                            "sla_hours": 192}})[0]
    assert stale["plain"] == "coachvision has not sent anything new in 17 days"
    # Days, not hours: nobody acts differently on 400h than on 17 days.
    assert "400" not in stale["plain"]


def test_a_project_name_is_never_recapitalised():
    # "coachvision" is spelled that way. Upper-casing a sentence's first letter
    # would silently rename a project on the most-read line of the page.
    row = attention.rank({"coachvision": {"status": "stale", "age_hours": 400,
                                          "sla_hours": 192}})[0]
    assert attention.headline([row]).startswith("coachvision ")


def test_the_unreadable_sentence_is_capitalised_at_source():
    row = attention.rank({"coachvision": {"status": "blind"}})[0]
    assert attention.headline([row]).startswith("We cannot see ")


def test_the_headline_names_one_project_and_counts_the_rest():
    ranked = attention.rank({"a": {"status": "blind"}, "b": {"status": "stale",
                                                             "age_hours": 100,
                                                             "sla_hours": 48}})
    line = attention.headline(ranked)
    assert line.startswith("We cannot see a")
    assert "1 other" in line


def test_a_healthy_week_says_so_plainly():
    assert attention.headline(attention.rank({"a": {"status": "ok"}})) == "Everything looks fine."


def test_a_single_waiting_idea_is_not_a_concern():
    # Every project scores something, and a page reporting four "concerns" every
    # week trains you to ignore it.
    ranked = attention.rank({"a": {"status": "ok"}},
                            repos={"a": "o/a"},
                            ledger={"entries": [{"repo": "o/a", "status": "open",
                                                 "kind": "enhancement"}]})
    assert ranked[0]["score"] < attention.NOTABLE
    assert attention.headline(ranked) == "Everything looks fine."


def test_every_row_carries_something_to_do():
    ranked = attention.rank({"a": {"status": "stale", "age_hours": 100, "sla_hours": 48},
                             "b": {"status": "ok"}})
    assert ranked[0]["action"] == "Check whether it is still running."
    assert ranked[1]["action"] == "Nothing to do."


def test_the_headline_is_published_not_left_to_the_dashboard(tmp_path):
    t = _tracer(tmp_path)
    t.tool_call(0, "read_trading_bot_log", {}, json.dumps({"status": "not_configured"}), False)
    path = tmp_path / "digest.json"
    t.write_digest(str(path))
    assert json.loads(path.read_text(encoding="utf-8"))["headline"].startswith("We cannot see")


def test_the_dashboard_renders_the_published_headline_and_does_not_compose_one():
    import pathlib
    app = pathlib.Path("docs/app.js").read_text(encoding="utf-8")
    assert "d.headline" in app
    # No second copy of the wording. If these phrases ever appear in JavaScript,
    # the page has started forming its own opinion.
    for phrase in ("has not sent anything new", "Everything looks fine",
                   "cannot see", "could use a look"):
        assert phrase not in app, f"app.js is composing its own verdict: {phrase!r}"


def test_the_headline_count_matches_the_projects_the_list_will_flag():
    # The page shows both: a headline saying "1 other could use a look too" and
    # a list marking which projects those are. They read the same `notable`
    # flag, and this pins that they cannot disagree — a headline counting two
    # while the list highlights three is the kind of thing nobody reports and
    # everybody stops trusting.
    ranked = attention.rank(
        {"a": {"status": "stale", "age_hours": 400, "sla_hours": 192},
         "b": {"status": "ok"}, "c": {"status": "ok"}, "d": {"status": "blind"}},
        readings={"b": {"odds_budget_used_pct": 94}})
    flagged = [r for r in ranked if r["notable"]]
    line = attention.headline(ranked)
    others = len(flagged) - 1
    assert (f"{others} other" in line) if others else ("other" not in line)


def test_the_short_form_drops_the_name_and_nothing_else():
    # The list prints the name in its own column, so the phrase beside it must
    # not repeat it. Derived from one wording, not by trimming the sentence in
    # JavaScript — that breaks the first time a name appears mid-sentence.
    row = attention.rank({"coachvision": {"status": "stale", "age_hours": 400,
                                          "sla_hours": 192}})[0]
    assert row["short"] == "has not sent anything new in 17 days"
    assert row["plain"] == f"coachvision {row['short']}"
    assert "coachvision" not in row["short"]


def test_an_unreadable_project_still_reads_as_a_phrase_in_the_list():
    row = attention.rank({"coachvision": {"status": "blind"}})[0]
    assert row["short"] == "cannot be seen at all right now"
    assert row["plain"].startswith("We cannot see")
