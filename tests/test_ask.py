"""Tests for the voice assistant — the pack, the prompt, and the Worker.

The assistant answers questions out loud, which makes it the most confident
surface this project has ever had: nobody cross-checks a sentence they heard in
the car against docs/shipped.json. So the things pinned here are the ones that
would let it sound right while being wrong.

  * the gate's verdicts are QUOTED from tools.implementable, never paraphrased,
  * the queue block is passed through exactly as tools.queue_state wrote it,
  * the Worker carries no prompt text and no rules of its own (invariants 3, 4),
  * the Worker never re-serializes the facts, which would silently cost money,
  * the prompt stays inside its size budget, because every question pays for it,
  * the pack, which is published PUBLICLY, carries nothing that isn't already.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone

import pytest

import ask
import ask_context
import tools
from scripts import build_ask_context

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKER = os.path.join(REPO_ROOT, "worker", "overseer-ask.js")

NOW = datetime(2026, 8, 30, 12, 0, 0, tzinfo=timezone.utc)


def _entry(number, **kw):
    entry = {"repo": "A/crypto-trading", "number": number, "title": f"Thing {number}",
             "kind": "bug", "status": "open",
             "created_at": "2026-08-01T00:00:00+00:00"}
    entry.update(kw)
    return entry


def _ledger(entries, queue=None):
    return {"generated": "2026-08-30T09:00:00+00:00", "entries": entries,
            "totals": {"proposed": len(entries)},
            "queue": queue if queue is not None else {"cap": 3, "efforts": ["low"],
                                                      "next": [], "eligible": 0}}


def _digest(**kw):
    base = {"generated": "2026-08-24T14:35:53Z", "status": "completed",
            "summary": "Issues Found\n- nothing", "counts": {"issues": 0}}
    base.update(kw)
    return base


# ── THE GATE'S OWN WORDS ─────────────────────────────────────────────────

@pytest.mark.parametrize("entry", [
    _entry(1, kind="enhancement", effort="medium"),
    _entry(2, kind="enhancement", effort="high"),
    _entry(3, kind="enhancement"),
    _entry(4),
    _entry(5, kind="enhancement", effort="low"),
    _entry(6, labels=["overseer:implement-failed"]),
])
def test_backlog_quotes_the_gate_verbatim(entry):
    # "Why has this not been implemented?" is the question this assistant will
    # be asked most, and the only trustworthy answer is the one the dispatcher
    # would give. A paraphrase here is a plausible-sounding wrong answer at the
    # exact moment nobody can check it.
    facts = ask_context.build_facts(_digest(), None, _ledger([entry]))
    published = facts["backlog"][0]
    ok, why = tools.implementable(entry, efforts=["low"])
    assert published["gate"] == why
    assert published["eligible"] is ok


def test_the_gate_verdict_uses_the_published_efforts_not_the_default():
    # The pack must describe the queue the dispatcher will actually produce. If
    # OVERSEER_IMPLEMENT_EFFORT is widened, the published queue block says so,
    # and re-running the verdict against the module default would have the
    # assistant explaining a rule that is no longer in force.
    entry = _entry(1, kind="enhancement", effort="medium")
    ledger = _ledger([entry], queue={"efforts": ["low", "medium"], "next": []})
    facts = ask_context.build_facts(_digest(), None, ledger)
    assert facts["backlog"][0]["eligible"] is True


def test_the_queue_block_is_passed_through_untouched():
    # Invariant 4: queue_state publishes it, nothing downstream re-derives it.
    queue = {"cap": 3, "efforts": ["low"], "eligible": 5, "tier": "light",
             "next": [{"repo": "A/overseer", "number": 32, "kind": "bug"}],
             "in_flight": [], "benched": []}
    facts = ask_context.build_facts(_digest(), None, _ledger([], queue))
    assert facts["queue"] == queue


def test_only_merged_work_is_reported_as_shipped():
    # Invariant 1. An issue closed with a fix on an unreviewed branch is
    # in_flight, and an assistant that called that "shipped" would flatter the
    # delivery record out loud.
    entries = [
        _entry(1, status="shipped", closed_at="2026-08-28T00:00:00+00:00", fix_ref="PR #9"),
        _entry(2, status="in_flight", closed_at="2026-08-28T00:00:00+00:00"),
    ]
    facts = ask_context.build_facts(_digest(), None, _ledger(entries))
    numbers = [item["number"] for item in facts["delivery"]["recent_shipped"]]
    assert numbers == [1]
    assert [item["number"] for item in facts["backlog"]] == []


# ── THE PACK ─────────────────────────────────────────────────────────────

def test_facts_json_is_the_facts():
    context = ask_context.build_context(_digest(), None, _ledger([_entry(1)]))
    assert json.loads(context["facts_json"]) == context["facts"]


def test_the_prompt_carries_the_facts_and_the_format_rule():
    context = ask_context.build_context(_digest(), None, _ledger([_entry(1)]))
    voice = ask_context.system_for(context, "voice")
    text = ask_context.system_for(context, "text")
    assert context["facts_json"] in voice
    assert ask_context.VOICE_FORMAT in voice
    assert ask_context.TEXT_FORMAT in text
    assert voice != text


def test_an_unknown_format_falls_back_to_voice_rather_than_dropping_the_rule():
    # A phone sending format=spoken must not get an answer with no length rule
    # and read four paragraphs at you.
    context = ask_context.build_context(_digest(), None, _ledger([_entry(1)]))
    assert ask_context.VOICE_FORMAT in ask_context.system_for(context, "nonsense")


def test_the_caps_hold():
    entries = ([_entry(n, status="shipped",
                       closed_at=f"2026-08-{(n % 28) + 1:02d}T00:00:00+00:00")
                for n in range(60)]
               + [_entry(1000 + n) for n in range(60)])
    facts = ask_context.build_facts(_digest(), None, _ledger(entries))
    assert len(facts["delivery"]["recent_shipped"]) == ask_context.MAX_SHIPPED
    assert len(facts["backlog"]) == ask_context.MAX_BACKLOG
    # Capped for size, but the assistant must still know how much it cannot see.
    assert facts["backlog_total"] == 60


def test_eligible_items_sort_ahead_of_blocked_ones_then_oldest_first():
    entries = [
        _entry(1, kind="enhancement", effort="medium"),
        _entry(2, created_at="2026-08-20T00:00:00+00:00"),
        _entry(3, created_at="2026-08-02T00:00:00+00:00"),
    ]
    facts = ask_context.build_facts(_digest(), None, _ledger(entries))
    assert [item["number"] for item in facts["backlog"]] == [3, 2, 1]


def test_a_missing_source_file_does_not_take_the_assistant_down():
    # Losing history.json should cost the spend trend, not every answer.
    facts = ask_context.build_facts(_digest(), None, _ledger([_entry(1)]))
    assert facts["spend"]["recent_runs"] == []
    assert facts["digest"]["summary"]


def test_an_unparseable_timestamp_is_absent_rather_than_fatal():
    entry = _entry(1, created_at="last Tuesday")
    facts = ask_context.build_facts(_digest(), None, _ledger([entry]))
    assert facts["backlog"][0]["filed_at"] is None


def test_timestamps_are_normalised_to_one_utc_shape():
    # The three source files stamp times three ways, and the model is asked to
    # compare all of them against one clock in the user turn.
    entry = _entry(1, status="shipped", closed_at="2026-08-28T10:00:00+02:00")
    digest = _digest(generated="2026-08-24T14:35:53Z")
    facts = ask_context.build_facts(digest, None, _ledger([entry]))
    assert facts["digest"]["generated"] == "2026-08-24T14:35:53Z"
    assert facts["delivery"]["recent_shipped"][0]["merged_at"] == "2026-08-28T08:00:00Z"


# ── THE CLOCK STAYS OUT OF THE CACHED PREFIX ─────────────────────────────

def test_the_pack_does_not_depend_on_the_current_time():
    # This is what lets the hourly rebuild stay silent unless data moved, and
    # what stops a pack built on Monday from still claiming to be fresh on
    # Thursday. If a clock creeps back into the facts, both break at once.
    first = ask_context.build_facts(_digest(), None, _ledger([_entry(1)]))
    second = ask_context.build_facts(_digest(), None, _ledger([_entry(1)]))
    assert first == second
    assert not re.search(r"age_(days|hours)", json.dumps(first))


def test_the_build_stamp_is_outside_the_prompt():
    context = ask_context.build_context(_digest(), None, _ledger([_entry(1)]))
    assert context["generated"] not in ask_context.system_for(context, "voice")


def test_the_user_turn_carries_the_clock():
    turn = ask_context.user_turn("what is queued?", now=NOW)
    assert turn.startswith("[current time: 2026-08-30T12:00:00Z]")
    assert turn.endswith("what is queued?")


def test_the_worker_puts_the_clock_in_the_user_turn_too():
    code = _worker_code()
    assert "userTurn(question)" in code
    assert "current time:" in code
    # The trap this guards: one line higher and every question pays full input
    # price for a 3.5k-token prefix, with nothing visibly wrong.
    system_block = code[code.index("system: ["):code.index("messages: [")]
    assert "Date()" not in system_block


# ── PUBLISHED PUBLICLY ───────────────────────────────────────────────────

SECRET_SHAPED = re.compile(
    r"(sk-ant-|ghp_|github_pat_|BEGIN [A-Z ]*PRIVATE KEY|bot\d{6,}:[A-Za-z0-9_-]{30,})")


def test_the_published_pack_carries_nothing_private():
    # It is served from GitHub Pages next to digest.json, so it is world
    # readable. Everything in it is derived from files that already are.
    path = os.path.join(REPO_ROOT, ask_context.ASK_CONTEXT_PATH)
    if not os.path.exists(path):
        pytest.skip("pack not built in this checkout")
    with open(path, encoding="utf-8") as f:
        blob = f.read()
    assert not SECRET_SHAPED.search(blob)
    for name in ("ANTHROPIC_API_KEY", "OVERSEER_GITHUB_TOKEN", "TELEGRAM_BOT_TOKEN",
                 "VAPID_PRIVATE_KEY", "ASK_SHARED_SECRET"):
        assert name not in blob


def test_the_published_prompt_is_inside_its_budget():
    path = os.path.join(REPO_ROOT, ask_context.ASK_CONTEXT_PATH)
    if not os.path.exists(path):
        pytest.skip("pack not built in this checkout")
    context = ask_context.load_published(path)
    size = len(ask_context.system_for(context, "voice").encode("utf-8"))
    assert size <= build_ask_context.MAX_PROMPT_BYTES


def test_the_builder_refuses_to_publish_an_empty_pack(tmp_path, monkeypatch):
    # A pack with nothing in it produces an assistant that answers every
    # question with equal confidence and no information.
    monkeypatch.setattr(tools, "DIGEST_PATH", str(tmp_path / "nope.json"))
    monkeypatch.setattr(tools, "HISTORY_PATH", str(tmp_path / "nope2.json"))
    monkeypatch.setattr(tools, "LEDGER_PATH", str(tmp_path / "nope3.json"))
    assert build_ask_context.main(["--out", str(tmp_path / "out.json")]) == 1
    assert not (tmp_path / "out.json").exists()


def test_a_partial_read_still_publishes_and_says_what_was_missing(tmp_path, monkeypatch):
    digest = tmp_path / "digest.json"
    digest.write_text(json.dumps(_digest()), encoding="utf-8")
    monkeypatch.setattr(tools, "DIGEST_PATH", str(digest))
    monkeypatch.setattr(tools, "HISTORY_PATH", str(tmp_path / "gone.json"))
    monkeypatch.setattr(tools, "LEDGER_PATH", str(tmp_path / "gone2.json"))
    out = tmp_path / "out.json"
    assert build_ask_context.main(["--out", str(out)]) == 0
    context = json.loads(out.read_text(encoding="utf-8"))
    assert any("gone.json" in note for note in context["facts"]["unavailable"])


# ── THE WORKER STAYS DUMB ────────────────────────────────────────────────

def _worker_code():
    with open(WORKER, encoding="utf-8") as f:
        source = f.read()
    # Strip comments: the reasoning in this file legitimately talks about the
    # gate and the prompt, and only executable code can actually drift.
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return "\n".join(line for line in source.splitlines()
                     if not line.strip().startswith("//"))


def test_the_worker_carries_no_prompt_of_its_own():
    # Invariant 3's shape: the prompt lived in five files once, and a two-line
    # fix meant five edits. Here it would be two files on two platforms, one of
    # which needs a deploy to change.
    code = _worker_code()
    for sentence in ("You are the Overseer", "read aloud", "Never invent an issue"):
        assert sentence not in code


def test_the_worker_does_not_re_derive_the_gate():
    # Invariant 4. Every rule-shaped string belongs to tools.implementable.
    code = _worker_code()
    for rule in ("effort", "confirmed bug", "in_flight", "implement-failed",
                 "not open", "shipped"):
        assert rule not in code


def test_the_watchdog_threshold_is_published_not_hardcoded():
    # Invariant 8, one platform further out. A staleness limit written into the
    # Worker would be deployed separately from the Python that owns it, drift
    # the first time one moved, and then page — or stay silent — on a rule
    # nobody could see from this repo.
    import ask_context
    from heartbeat import MAX_AGE_HOURS

    facts = ask_context.build_facts(
        {"generated": "2026-08-31T19:48:15Z"}, {"runs": []}, {"entries": [], "totals": {}})
    assert facts["heartbeat"]["stale_after_hours"] == MAX_AGE_HOURS

    worker = open(WORKER, encoding="utf-8").read()
    assert "facts?.heartbeat?.stale_after_hours" in worker
    assert str(int(MAX_AGE_HOURS)) not in worker, "the Worker hardcodes the threshold"


def test_the_watchdog_alerts_a_human_not_the_console():
    # The bug it exists for: every other failure path in the Worker ends at
    # console.error, which is only visible to `wrangler tail`. An alarm nobody
    # is watching is the same as no alarm.
    worker = open(WORKER, encoding="utf-8").read()
    assert "api.telegram.org" in worker
    assert "checkDigestFreshness" in worker


def test_the_watchdog_runs_even_when_the_dispatch_fails():
    # Poking GitHub and noticing that the poking stopped working are
    # independent. Chaining the check to a successful dispatch would make it
    # silent in exactly the case it is for — an expired DISPATCH_TOKEN.
    worker = open(WORKER, encoding="utf-8").read()
    scheduled = worker.split("async scheduled(")[1].split("async fetch(")[0]
    assert "ctx.waitUntil(fireDispatch(env, eventType));" in scheduled
    assert "ctx.waitUntil(checkDigestFreshness(env));" in scheduled
    # Two separate waitUntil calls, not one chained off the other.
    assert ".then(" not in scheduled


def test_the_watchdog_stays_quiet_without_a_threshold_or_credentials():
    # A watchdog that guesses its own limit pages at the wrong time forever,
    # and one that assumes credentials throws inside a scheduled handler where
    # nothing surfaces it.
    worker = open(WORKER, encoding="utf-8").read()
    fn = worker.split("async function checkDigestFreshness(")[1].split("\nexport default")[0]
    assert "if (!env.TELEGRAM_BOT_TOKEN || !env.TELEGRAM_CHAT_ID) return;" in fn
    assert "if (!staleAfterHours || !generated)" in fn


def test_the_worker_never_re_serializes_the_facts():
    # JSON.stringify(pack.facts) would render different bytes from Python's
    # dump — different key order, different spacing, \u-escaped em dashes — and
    # miss the prompt cache on every question while looking perfectly fine.
    code = _worker_code()
    assert "JSON.stringify(pack" not in code
    assert "facts_json" in code


def test_the_worker_checks_the_shared_secret_before_spending_anything():
    code = _worker_code()
    auth = code.index("ASK_SHARED_SECRET")
    assert auth < code.index("ANTHROPIC_API_KEY")
    assert "secretMatches" in code


def test_the_worker_logs_why_a_failure_happened():
    """A voice endpoint that swallows its errors cannot be debugged.

    The friendly sentence is right for something read aloud and wrong for
    everything else: the first real failure here was "Something went wrong
    reaching the model", with no way to tell a bad key from a bad request from
    an outage. So the reason goes to the log on every failure, and comes back
    in the response when the caller asks — which only an authenticated caller
    can do, so it reveals nothing they do not already own.
    """
    code = _worker_code()
    assert code.count("console.error") >= 2
    assert "function explain(" in code
    # And the friendly default survives: a phone must not read an error object.
    assert "debug ?" in code


def test_the_worker_asks_for_no_tools_and_no_thinking():
    # One call, no loop. This is the difference between a question costing a
    # fraction of a cent and costing what an agent run costs.
    code = _worker_code()
    assert '"disabled"' in code or "'disabled'" in code
    assert "tools:" not in code


def test_python_and_the_worker_build_the_same_prefix_byte_for_byte():
    """The claim the whole caching design rests on, actually executed.

    Everything else here greps the Worker; this one runs its `systemFor` against
    the published pack and diffs the result against Python's. If the two ever
    render one byte differently — a stray space, an escaped em dash — every
    question silently pays full input price for 3.5k tokens instead of a tenth,
    and nothing looks wrong from either side.

    Skipped rather than failed without node: this repo has no JS toolchain by
    design, and a test that demands one would just get deleted.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    path = os.path.join(REPO_ROOT, ask_context.ASK_CONTEXT_PATH)
    if not os.path.exists(path):
        pytest.skip("pack not built in this checkout")

    with open(WORKER, encoding="utf-8") as f:
        src = f.read()
    body = src[src.index("function systemFor"):src.index("export default")]

    with tempfile.TemporaryDirectory() as tmp:
        script = os.path.join(tmp, "check.mjs")
        out = os.path.join(tmp, "prompt.txt")
        with open(script, "w", encoding="utf-8") as f:
            f.write(
                'import { readFileSync, writeFileSync } from "node:fs";\n'
                f"const systemFor = new Function({json.dumps(body)} + "
                '"; return systemFor;")();\n'
                f"const pack = JSON.parse(readFileSync({json.dumps(path)}, \"utf8\"));\n"
                f"writeFileSync({json.dumps(out)}, systemFor(pack, \"voice\"));\n")
        subprocess.run([node, script], check=True, capture_output=True)
        with open(out, encoding="utf-8") as f:
            from_worker = f.read()

    context = ask_context.load_published(path)
    assert from_worker == ask_context.system_for(context, "voice")


# ── THE CLI ──────────────────────────────────────────────────────────────

class _FakeClient:
    def __init__(self):
        self.kwargs = None
        self.calls = 0

    class _Messages:
        def __init__(self, outer):
            self.outer = outer

        def create(self, **kwargs):
            self.outer.kwargs = kwargs
            self.outer.calls += 1
            block = type("B", (), {"type": "text", "text": "Two are queued."})()
            usage = type("U", (), {"input_tokens": 5, "output_tokens": 9,
                                   "cache_creation_input_tokens": 0,
                                   "cache_read_input_tokens": 3300})()
            return type("R", (), {"content": [block], "usage": usage})()

    @property
    def messages(self):
        return self._Messages(self)


def test_a_question_is_exactly_one_call_with_no_tools():
    client = _FakeClient()
    answer, usage = ask.ask("what is queued?", client=client)
    assert answer == "Two are queued."
    assert client.calls == 1
    assert "tools" not in client.kwargs
    assert client.kwargs["thinking"] == {"type": "disabled"}
    assert usage["cache_read"] == 3300


def test_the_question_goes_after_the_cached_prefix_not_inside_it():
    # Anything volatile inside the cached block invalidates it, which turns a
    # tenth-price cache read into a full-price write on every question.
    client = _FakeClient()
    ask.ask("what shipped?", client=client)
    system = client.kwargs["system"]
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert "what shipped?" not in system[0]["text"]
    turn = client.kwargs["messages"][0]
    assert turn["role"] == "user"
    assert turn["content"].endswith("what shipped?")
    assert "current time:" in turn["content"]
    assert "current time:" not in system[0]["text"]
