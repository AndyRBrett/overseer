"""Tests for two-tier model selection and the per-run spend estimate.

The tiering exists to cut cost without weakening the one agent whose judgment
is consequential. Two things therefore have to hold, and both are asserted
here: the Bug-Hunter must stay on the heavy model under every configuration
that isn't an explicit override, and the reported saving must be arithmetic on
real token counts rather than a claim.
"""

import json
from types import SimpleNamespace

import pytest

import tools
import tracer as tracer_mod
from tracer import RunTracer, price_usd


# ── tier selection ───────────────────────────────────────────────────────


@pytest.fixture
def tiers(monkeypatch):
    """Set MODEL / LIGHT_MODEL / HEAVY_AGENTS without touching the environment."""
    def _set(model="claude-opus-4-8", light="claude-sonnet-5", heavy=("Bug-Hunter",)):
        monkeypatch.setattr(tools, "MODEL", model)
        monkeypatch.setattr(tools, "LIGHT_MODEL", light)
        monkeypatch.setattr(tools, "HEAVY_AGENTS", tuple(heavy))
    return _set


def test_default_split_keeps_the_bug_hunter_heavy(tiers):
    # The Bug-Hunter decides whether a quiet feed is dead and whether to file a
    # bug on a real repo. That call is what the heavy tier is being paid for.
    tiers()
    assert tools.model_for("Bug-Hunter") == "claude-opus-4-8"
    assert tools.tier_for("Bug-Hunter") == "heavy"


@pytest.mark.parametrize("agent", ["Idea-Agent", "Reviewer"])
def test_default_split_moves_the_other_agents_to_the_light_model(tiers, agent):
    tiers()
    assert tools.model_for(agent) == "claude-sonnet-5"
    assert tools.tier_for(agent) == "light"


def test_every_pipeline_agent_is_covered_by_the_tier_map(tiers):
    # AGENT_NAMES must stay in sync with the strings run_agent is called with,
    # or a misconfiguration warning would be silently unreachable.
    tiers()
    assert set(tools.AGENT_NAMES) == {"Bug-Hunter", "Idea-Agent", "Reviewer"}
    for name in tools.AGENT_NAMES:
        assert tools.model_for(name) in ("claude-opus-4-8", "claude-sonnet-5")


def test_tiering_has_an_off_switch(tiers):
    # Pointing OVERSEER_LIGHT_MODEL at the heavy model must put the whole
    # pipeline back on one model — a single revert, no code change.
    tiers(light="claude-opus-4-8")
    for name in tools.AGENT_NAMES:
        assert tools.model_for(name) == "claude-opus-4-8"
        assert tools.tier_for(name) == "heavy"


@pytest.mark.parametrize("light", ["", None])
def test_a_missing_light_model_falls_back_to_the_heavy_one(tiers, light):
    # Defensive: whatever the config layer does, an empty light model must never
    # reach the API as model="".
    tiers(light=light)
    for name in tools.AGENT_NAMES:
        assert tools.model_for(name) == "claude-opus-4-8"


@pytest.mark.parametrize("var,default", [
    ("OVERSEER_MODEL", "claude-opus-4-8"),
    ("OVERSEER_LIGHT_MODEL", "claude-sonnet-5"),
    ("OVERSEER_HEAVY_AGENTS", "Bug-Hunter"),
])
def test_a_blank_actions_variable_is_treated_as_unset(monkeypatch, var, default):
    # An unset GitHub Actions variable interpolates as "" rather than being
    # omitted, so `os.getenv(var, default)` would hand the API model="".
    for value in ("", "   "):
        monkeypatch.setenv(var, value)
        assert tools._env(var, default) == default
    monkeypatch.delenv(var, raising=False)
    assert tools._env(var, default) == default
    monkeypatch.setenv(var, " claude-haiku-4-5 ")
    assert tools._env(var, default) == "claude-haiku-4-5"


def test_heavy_agents_is_configurable_in_both_directions(tiers):
    tiers(heavy=("Bug-Hunter", "Idea-Agent"))
    assert tools.model_for("Idea-Agent") == "claude-opus-4-8"
    assert tools.model_for("Reviewer") == "claude-sonnet-5"
    tiers(heavy=())
    assert tools.model_for("Bug-Hunter") == "claude-sonnet-5"


def test_unknown_heavy_agent_name_is_reported_not_swallowed(capsys):
    # A typo here would quietly demote the Bug-Hunter — the exact class of silent
    # misconfiguration this pipeline was rebuilt to stop shipping.
    assert tools._parse_heavy_agents("Bug-Hunter, BugHunter") == ("Bug-Hunter", "BugHunter")
    out = capsys.readouterr().out
    assert "WARNING" in out and "BugHunter" in out


def test_valid_heavy_agents_list_is_quiet(capsys):
    assert tools._parse_heavy_agents("Bug-Hunter,Reviewer") == ("Bug-Hunter", "Reviewer")
    assert capsys.readouterr().out == ""


# ── spend accounting ─────────────────────────────────────────────────────


def _usage(inp=0, out=0, cw=0, cr=0):
    return SimpleNamespace(input_tokens=inp, output_tokens=out,
                           cache_creation_input_tokens=cw, cache_read_input_tokens=cr)


def _tracer(tmp_path, heavy="claude-opus-4-8"):
    t = RunTracer(jsonl_path=str(tmp_path / "x.jsonl"), html_path=str(tmp_path / "x.html"))
    t.heavy_model = heavy
    return t


def test_price_matches_the_published_rate_card():
    # 1M input + 1M output on Opus is $5 + $25.
    assert price_usd("claude-opus-4-8", {"input": 1_000_000, "output": 1_000_000}) == 30.0
    assert price_usd("claude-sonnet-5", {"input": 1_000_000, "output": 1_000_000}) == 18.0


def test_cache_reads_are_charged_at_a_tenth_of_input():
    # The agents cache a large static prefix every turn; costing cache reads at
    # the full input rate would overstate the bill several-fold.
    full = price_usd("claude-opus-4-8", {"input": 1_000_000})
    cached = price_usd("claude-opus-4-8", {"cache_read": 1_000_000})
    written = price_usd("claude-opus-4-8", {"cache_write": 1_000_000})
    assert cached == pytest.approx(full * 0.10)
    assert written == pytest.approx(full * 1.25)


def test_unpriced_model_reports_none_rather_than_zero(tmp_path):
    # A model we have no rate for must not be summarized as free.
    assert price_usd("claude-something-unreleased", {"input": 10_000}) is None
    t = _tracer(tmp_path)
    t.set_agent_model("Reviewer", "claude-something-unreleased", "light")
    t.record_usage("Reviewer", "claude-something-unreleased", _usage(inp=10_000, out=1_000))
    spend = t.spend()
    assert spend["agents"][0]["usd"] is None
    assert spend["priced"] is False
    assert spend["total_usd"] is None and spend["saved_usd"] is None
    assert spend["agents"][0]["input"] == 10_000  # tokens still counted


def test_saving_is_measured_against_the_same_tokens_at_the_heavy_rate(tmp_path):
    t = _tracer(tmp_path)
    t.set_agent_model("Bug-Hunter", "claude-opus-4-8", "heavy")
    t.record_usage("Bug-Hunter", "claude-opus-4-8", _usage(inp=100_000, out=10_000))
    t.set_agent_model("Idea-Agent", "claude-sonnet-5", "light")
    t.record_usage("Idea-Agent", "claude-sonnet-5", _usage(inp=100_000, out=10_000))

    spend = t.spend()
    # Bug-Hunter: 0.1M*$5 + 0.01M*$25 = $0.75. Idea: 0.1M*$3 + 0.01M*$15 = $0.45.
    assert spend["total_usd"] == pytest.approx(1.20)
    # All-heavy baseline reprices the identical tokens: $0.75 + $0.75.
    assert spend["baseline_usd"] == pytest.approx(1.50)
    assert spend["saved_usd"] == pytest.approx(0.30)
    assert spend["saved_pct"] == pytest.approx(20.0)


def test_no_saving_is_claimed_when_everything_ran_heavy(tmp_path):
    t = _tracer(tmp_path)
    for agent in tools.AGENT_NAMES:
        t.set_agent_model(agent, "claude-opus-4-8", "heavy")
        t.record_usage(agent, "claude-opus-4-8", _usage(inp=50_000, out=5_000))
    spend = t.spend()
    assert spend["saved_usd"] == pytest.approx(0.0)
    assert spend["total_usd"] == spend["baseline_usd"]


def test_usage_accumulates_across_a_multi_turn_tool_loop(tmp_path):
    t = _tracer(tmp_path)
    t.set_agent_model("Bug-Hunter", "claude-opus-4-8", "heavy")
    for _ in range(4):
        t.record_usage("Bug-Hunter", "claude-opus-4-8", _usage(inp=1_000, out=200, cw=500, cr=9_000))
    row = t.spend()["agents"][0]
    assert row["calls"] == 4
    assert (row["input"], row["output"], row["cache_write"], row["cache_read"]) == \
        (4_000, 800, 2_000, 36_000)


def test_a_missing_usage_object_does_not_break_the_run(tmp_path):
    # An older SDK or a stubbed client must cost us a spend line, never a review.
    t = _tracer(tmp_path)
    t.set_agent_model("Reviewer", "claude-sonnet-5", "light")
    t.record_usage("Reviewer", "claude-sonnet-5", None)
    row = t.spend()["agents"][0]
    assert row["calls"] == 0 and row["usd"] == 0.0
    assert row["model"] == "claude-sonnet-5" and row["tier"] == "light"


def test_partial_usage_fields_are_tolerated(tmp_path):
    # Cache counters are absent on responses that neither wrote nor read cache.
    t = _tracer(tmp_path)
    t.set_agent_model("Reviewer", "claude-sonnet-5", "light")
    t.record_usage("Reviewer", "claude-sonnet-5",
                   SimpleNamespace(input_tokens=100, output_tokens=None))
    row = t.spend()["agents"][0]
    assert (row["input"], row["output"], row["cache_read"]) == (100, 0, 0)


def test_spend_reaches_the_dashboard_payload(tmp_path):
    t = _tracer(tmp_path)
    t.set_agent_model("Bug-Hunter", "claude-opus-4-8", "heavy")
    t.record_usage("Bug-Hunter", "claude-opus-4-8", _usage(inp=100_000, out=10_000))
    t.finish("completed")
    t.write_digest(str(tmp_path / "digest.json"))
    spend = json.load(open(tmp_path / "digest.json"))["spend"]
    assert spend["total_usd"] == pytest.approx(0.75)
    assert spend["agents"][0]["agent"] == "Bug-Hunter"
    assert spend["heavy_model"] == "claude-opus-4-8"


def test_spend_is_trended_in_history(tmp_path):
    t = _tracer(tmp_path)
    t.set_agent_model("Idea-Agent", "claude-sonnet-5", "light")
    t.record_usage("Idea-Agent", "claude-sonnet-5", _usage(inp=100_000, out=10_000))
    t.finish("completed")
    t.write_history(str(tmp_path / "history.json"))
    run = json.load(open(tmp_path / "history.json"))["runs"][-1]
    assert run["spend"]["total_usd"] == pytest.approx(0.45)
    assert run["spend"]["saved_usd"] == pytest.approx(0.30)
    assert "agents" not in run["spend"]  # history stays compact


def test_a_run_with_no_model_calls_reports_nothing_rather_than_free(tmp_path):
    # A run that aborts at preflight spent nothing, but it also measured
    # nothing — it must not publish a confident $0.00 saving.
    t = _tracer(tmp_path)
    spend = t.spend()
    assert spend["agents"] == [] and spend["priced"] is False
    assert spend["total_usd"] is None and spend["saved_usd"] is None


def test_every_model_the_pipeline_can_select_by_default_is_priced():
    # If a default model isn't in the rate card the dashboard silently loses its
    # cost panel, so pin the two defaults rather than trusting they stay listed.
    import os
    for env, default in (("OVERSEER_MODEL", "claude-opus-4-8"),
                         ("OVERSEER_LIGHT_MODEL", "claude-sonnet-5")):
        assert default in tracer_mod.MODEL_PRICES, f"{env} default {default} unpriced"


# ── the wiring, end to end ───────────────────────────────────────────────


class _StubResponse:
    """One non-tool-use response, shaped like the SDK's."""

    stop_reason = "end_turn"

    def __init__(self, model):
        self.content = [SimpleNamespace(type="text", text=f"done on {model}")]
        self.usage = _usage(inp=1_000, out=100)


class _StubMessages:
    def __init__(self):
        self.models = []

    def create(self, **kwargs):
        self.models.append(kwargs["model"])
        return _StubResponse(kwargs["model"])


class _StubClient:
    def __init__(self):
        self.messages = _StubMessages()


def test_run_agent_dispatches_each_agent_to_its_own_tier(tmp_path, tiers):
    # The seam that actually matters: model_for exists, but run_agent has to use
    # it. A regression that hardcodes MODEL again would pass every test above.
    tiers()
    t = _tracer(tmp_path)
    client = _StubClient()
    for agent in tools.AGENT_NAMES:
        tools.run_agent(client, agent=agent, system="s", tool_names=[],
                        user_message="go", tracer=t)
    assert client.messages.models == [
        "claude-opus-4-8", "claude-sonnet-5", "claude-sonnet-5"]


def test_run_agent_records_usage_against_the_right_agent(tmp_path, tiers):
    tiers()
    t = _tracer(tmp_path)
    tools.run_agent(_StubClient(), agent="Idea-Agent", system="s", tool_names=[],
                    user_message="go", tracer=t)
    row = t.spend()["agents"][0]
    assert row["agent"] == "Idea-Agent" and row["model"] == "claude-sonnet-5"
    assert row["tier"] == "light" and row["input"] == 1_000 and row["calls"] == 1
