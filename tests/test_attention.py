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
