"""The context pack the voice assistant answers from — built HERE, in Python.

WHY THIS FILE EXISTS AT ALL. The voice front end is a Cloudflare Worker, which
means JavaScript, which means the same trap the dashboard fell into: a second
copy of the rules, drifting from the first. Invariant 4 already says the gate
lives in `tools.implementable` and that `app.js` must never re-derive it. A
Worker that built its own prompt and worked out its own answers would be that
mistake again, one platform further away and harder to test.

So the Worker is deliberately stupid. Everything that requires judgment — which
facts matter, how they are phrased, what the model is told to do with them —
is computed here, published as `docs/ask-context.json`, and fetched verbatim.
The Worker's whole job is: fetch this file, send `system` and the question to
the API, return the text. There is nothing in it to keep in sync.

That also makes the assistant TESTABLE, which a Worker answering from live model
calls would not be. `tests/test_ask.py` pins the pack's shape, its prompt
budget, and — the reason the thing is trustworthy — that the gate verdicts it
publishes come from `tools.implementable` rather than a paraphrase of it.

The pack is served from GitHub Pages alongside digest.json, so it is PUBLIC. It
must never carry anything that isn't already public. A test asserts that.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

import tools

# The pack rides in every single request as the cached prefix, so its size is a
# per-question cost, not a one-off. These caps are what keep a question at
# fractions of a cent: the assembled prompt runs ~3.5k tokens, and the parts
# that grow without bound (the ledger, the backlog, the run history) are exactly
# the parts capped. scripts/build_ask_context.py refuses to publish past 20KB.
MAX_SHIPPED = 12      # recent deliveries — enough to answer "what shipped lately?"
MAX_BACKLOG = 20      # open items, with the gate's verdict on each
MAX_RUNS = 8          # ~2 months of weekly spend, enough to see a trend
SUMMARY_CHARS = 2600  # the last digest, whole — it is the highest-signal block

ASK_CONTEXT_PATH = os.getenv("ASK_CONTEXT_PATH", "docs/ask-context.json")


def _now():
    return datetime.now(timezone.utc)


def _parse(stamp):
    """Lenient ISO parse — the three source files stamp times three ways.

    digest.json uses a trailing Z, the ledger writes +00:00 offsets, and an
    entry that predates a schema change may carry neither. A pack that raised on
    one odd stamp would take the whole assistant down over a date, so anything
    unparseable is simply absent.
    """
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None


def _stamp(value):
    """Normalise a timestamp to one UTC ISO shape, or drop it.

    The three source files stamp times three ways and the model is asked to
    compare them against the current time, so they arrive in one format rather
    than three. Anything unparseable is absent — a date the assistant cannot
    read is better missing than quietly wrong by a timezone.
    """
    parsed = _parse(value)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _short(text, limit):
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _repo_name(slug):
    return (slug or "").split("/")[-1]


# ── THE FACTS ────────────────────────────────────────────────────────────

# The heartbeat script owns this number; importing it keeps the published copy
# from drifting out of step with the alarm that GitHub-side runs use.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))
from heartbeat import MAX_AGE_HOURS as HEARTBEAT_MAX_AGE_HOURS  # noqa: E402


def build_facts(digest, history, ledger):
    """Assemble the deterministic fact block. Pure — no I/O, no model calls.

    Everything here is read off files the pipeline already publishes. Nothing is
    recomputed: the queue block arrives exactly as `tools.queue_state` wrote it,
    and each backlog verdict is whatever `tools.implementable` actually returns,
    so "why did crypto-trading get nothing this week?" is answered by the code
    that made the decision rather than by a model inferring it from labels.

    NOTHING IN HERE DEPENDS ON THE CURRENT TIME, and that is deliberate twice
    over. It means the pack changes only when the underlying data changes, so
    an hourly rebuild does not commit a new file every hour into a history that
    is already mostly ledger refreshes. And it removes the trap that costs more:
    ages baked in at build time read as fresh forever, so a pack rebuilt on
    Monday would still be saying "two hours old" on Thursday — out loud, with
    total confidence. The current time rides in the user turn instead, where it
    is also outside the cached prefix and therefore free.
    """
    digest = digest or {}
    ledger = ledger or {}
    runs = (history or {}).get("runs", [])

    queue = ledger.get("queue") or {}
    efforts = queue.get("efforts") or None

    shipped, backlog = [], []
    for entry in ledger.get("entries", []):
        status = entry.get("status")
        if status == "shipped":
            shipped.append({
                "repo": _repo_name(entry.get("repo")),
                "number": entry.get("number"),
                "title": _short(entry.get("title"), 110),
                "kind": entry.get("kind"),
                "fix": entry.get("fix_ref"),
                "merged_at": _stamp(entry.get("closed_at")),
            })
        elif status == "open":
            ok, why = tools.implementable(entry, efforts=efforts)
            backlog.append({
                "repo": _repo_name(entry.get("repo")),
                "number": entry.get("number"),
                "title": _short(entry.get("title"), 110),
                "kind": entry.get("kind"),
                "effort": entry.get("effort"),
                "impact": entry.get("impact"),
                "filed_at": _stamp(entry.get("created_at")),
                # The gate's own words. This is the single most useful field in
                # the pack and the one that must never be paraphrased.
                "eligible": ok,
                "gate": why,
            })

    shipped.sort(key=lambda e: (e["merged_at"] is None, e["merged_at"]), reverse=True)
    backlog.sort(key=lambda e: (e["eligible"] is False, e["filed_at"] or "9999"))

    spend_runs = [{
        "date": run.get("date"),
        "usd": (run.get("spend") or {}).get("total_usd"),
    } for run in runs[-MAX_RUNS:]]

    return {
        # The staleness THRESHOLD the off-GitHub watchdog applies (issue #59) — a
        # limit, not a computed age: the pack may never carry an age (invariant 9). It is
        # PUBLISHED rather than written into worker/overseer-ask.js for the same
        # reason the gate is (invariant 8): a second copy of the threshold in
        # JavaScript would be deployed separately, drift the first time this one
        # moved, and page — or fail to page — on a rule nobody could see from
        # here. The Worker compares two timestamps against this number; the
        # number itself is Python's.
        "heartbeat": {
            "stale_after_hours": HEARTBEAT_MAX_AGE_HOURS,
        },
        "digest": {
            "generated": _stamp(digest.get("generated")),
            "status": digest.get("status"),
            "counts": digest.get("counts"),
            "rollup": digest.get("rollup"),
            "projects": digest.get("projects"),
            "summary": _short(digest.get("summary"), SUMMARY_CHARS),
        },
        "spend": {
            "last_run": digest.get("spend"),
            "recent_runs": spend_runs,
            # Measured, not estimated — see CLAUDE.md. Quoting these keeps the
            # assistant from inventing a number when asked what things cost.
            "reference_costs_usd": {
                "one_review": 0.34,
                "one_implementation": 1.49,
                "typical_week": 4.80,
            },
        },
        "delivery": {
            "generated": _stamp(ledger.get("generated")),
            "totals": ledger.get("totals"),
            "recent_shipped": shipped[:MAX_SHIPPED],
        },
        # Published by tools.queue_state. Passed through untouched: the Worker
        # must render this, never re-derive it (invariant 4).
        "queue": queue,
        "backlog": backlog[:MAX_BACKLOG],
        "backlog_total": len(backlog),
    }


# ── THE PROMPT ───────────────────────────────────────────────────────────
# Authored here and published in the pack so the Worker carries no prompt text
# of its own. A test greps the Worker for these sentences: the moment a second
# copy appears, tuning the assistant means editing two files on two platforms,
# which is invariant 3's failure mode with a deployment step in the middle.

SYSTEM = """You are the Overseer — a weekly review pipeline that watches four \
projects (crypto-trading, coachvision, ufc-dashboard, and overseer itself), \
files issues for what it finds, and implements a few of them.

You are being asked a question out loud by the person who built you. Answer \
from the FACTS block below and nothing else.

How to be useful here:
- The facts are a snapshot, not live. If asked about something the snapshot \
does not cover, say what the snapshot does say and that you cannot see further.
- Every timestamp in the facts is UTC ISO-8601, and the question you are asked \
begins with the current time on the same clock. Work out how old something is \
by comparing the two, and say it the way a person would — "about a week ago", \
not a number of hours. If the digest or the delivery ledger is more than a few \
days old, say so before you answer anything from it.
- Never invent an issue number, a repo, a dollar amount, or a date. Every one \
of those is in the facts if it is real.
- `backlog[].gate` is the implementation gate's own verdict on why an item is \
or is not queued. When asked why something has not been worked on, quote that \
reason rather than reasoning about labels yourself.
- The `queue` block is what the next implementation run will actually pick up.
- Costs: quote `spend`. A review is about a third of a dollar, one \
implementation about a dollar fifty — the implementer is the expensive part.
- Say "shipped" only for work that is MERGED. An issue closed with a fix on an \
unreviewed branch is in flight, and the facts distinguish the two.

Be direct and concrete. Lead with the answer. Do not include internal or \
system XML tags in your response."""

VOICE_FORMAT = """This answer will be READ ALOUD by a phone, so write it to be \
heard, not read: two or three sentences, no markdown, no bullet points, no \
URLs, no bare issue numbers without their repo. Say "issue fifty in \
crypto-trading", not "#50". Round money to the nearest cent and say it as \
words. Stop when the question is answered — there is no scrolling back."""

TEXT_FORMAT = """Answer in plain text, at most a short paragraph or a few \
lines. Include issue numbers and repo names. No markdown headings."""


def facts_json(facts):
    """The exact bytes of the FACTS block, as both front ends must send them."""
    return json.dumps(facts, sort_keys=True, separators=(",", ":"))


def build_context(digest, history, ledger):
    """The full published pack: prompt, output formats, and facts.

    `facts` and `facts_json` are the same data twice, on purpose. The object is
    what humans and tests read; the string is what actually goes on the wire.

    They are published rather than left for each front end to serialize because
    the prefix has to be byte-identical for the prompt cache to hit, and Python
    and JavaScript do not agree on how to render JSON — key order, separator
    spacing, and whether an em dash survives as itself or becomes \\u2014. Every
    one of those differences is invisible in review and silently turns a
    cache read into a full-price write. Serializing once here removes the
    question: the Worker concatenates strings and never parses the facts at all.
    """
    facts = build_facts(digest, history, ledger)
    return {
        # Outside the cached prefix on purpose: a build stamp that changed the
        # prefix would pay full input price for every question after a rebuild.
        "generated": _now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "model_hint": os.getenv("OVERSEER_ASK_MODEL", "claude-sonnet-5"),
        "system": SYSTEM,
        "formats": {"voice": VOICE_FORMAT, "text": TEXT_FORMAT},
        "facts": facts,
        "facts_json": facts_json(facts),
    }


def system_for(context, fmt="voice"):
    """The exact system string to send: prompt + format rule + facts.

    This is the one function both front doors implement — here in Python for the
    CLI, and as four lines of string concatenation in the Worker. Keep them the
    same shape or the cache stops hitting.
    """
    formats = context.get("formats") or {}
    rule = formats.get(fmt) or formats.get("voice") or ""
    blob = context.get("facts_json") or facts_json(context.get("facts", {}))
    return f"{context.get('system', '')}\n\n{rule}\n\nFACTS:\n{blob}"


def user_turn(question, now=None):
    """The user turn: the current time, then the question.

    The time goes HERE rather than in the facts because everything before the
    question is the cached prefix. A clock in the prefix would invalidate the
    cache on every request and quietly charge full input price for a 3k-token
    pack, question after question, with nothing visibly wrong.
    """
    stamp = (now or _now()).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"[current time: {stamp}]\n{question}"


def load_published(path=None):
    path = path or ASK_CONTEXT_PATH
    with open(path, encoding="utf-8") as f:
        return json.load(f)
