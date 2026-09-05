"""Per-project attention scoring (overseer #25) — where is an hour worth spending?

THE PROBLEM THIS SOLVES. The dashboard's project panel is binary: a feed is
fresh or it is stale. All four projects can sit green on freshness while hiding
completely different situations — a negative 90-day Sharpe on a bot that is
barely trading, a CV pipeline that has never ingested footage, an odds scraper
burning through its API budget. "Everything is fresh" is a true answer to a
question nobody asked.

So this composes ONE number per project out of four normalised sub-signals,
sorts by it, and — the part that makes it usable — says in one line which signal
dominated. A ranking you cannot interrogate is a ranking you stop trusting the
first time it surprises you.

NO NEW DATA SOURCES. Everything here is read off what the run already has: the
per-project health the tracer derives from the shared telemetry read, the status
payload each project publishes, and the delivery ledger. That is issue #25's own
approach, and it is what keeps this free.

PURE, AND DELIBERATELY NOT IMPORTING `tools`. `tools` imports `tracer`, and
`tracer` calls into here, so a `tools` import would close a cycle. Everything
this needs — the repo a read tool reports on, the ledger — is passed in.
"""

from __future__ import annotations

import os

# Weights sum to 1.0. They encode a judgement, so it is written down: a project
# we cannot see at all outranks one that is merely producing bad numbers,
# because a blind spot hides both. The backlog term is smallest on purpose — a
# long idea list is a reason to spend an hour, not evidence anything is wrong,
# and letting it outweigh a dead feed is how you get a ranking that sends you to
# the healthiest project.
WEIGHTS = {
    "staleness": 0.40,   # can we see it, and is what we see current
    "errors": 0.25,      # is it failing when it runs
    "kpi": 0.20,         # is the thing it exists to do going badly
    "backlog": 0.15,     # how much untriaged work is queued against it
}

# Open enhancement ideas at which the backlog signal saturates. Ten untriaged
# ideas and thirty are the same message: nobody has looked at this list.
BACKLOG_FULL = int(os.getenv("OVERSEER_ATTENTION_BACKLOG_FULL", "10") or 10)

# Status → the floor its staleness signal cannot go below. A read that failed is
# a 1.0 whatever else the project reports, because everything else we know about
# it is out of date by definition.
_STATUS_FLOOR = {"error": 1.0, "blind": 1.0, "stale": 0.6, "idle": 0.4, "ok": 0.0}

# ── DOMAIN KPIs ──────────────────────────────────────────────────────────
# The signal issue #25 is actually about: crypto's Sharpe, coachvision's footage
# count, UFC's odds budget. Read from whatever the project publishes rather than
# hardcoded per project, so a new project that publishes a `sharpe` gets scored
# without a code change here — and one that publishes nothing scores 0.0 rather
# than being penalised for a field it never promised.

# Numbers where BELOW ZERO is the bad news. Scaled so a small loss reads as a
# small problem: -0.1 Sharpe is not the same as -2.0.
_NEGATIVE_IS_BAD = ("sharpe", "sharpe_90d", "sharpe_ratio", "pnl", "pnl_7d",
                    "roi", "return_pct", "net_pnl")

# Numbers where APPROACHING THE CEILING is the bad news — a quota about to run
# out. Published as a percentage or a 0..1 fraction; both are handled.
_EXHAUSTION = ("budget_used_pct", "quota_used_pct", "api_budget_used_pct",
               "odds_budget_used_pct", "rate_limit_used_pct")

# Rates where LOW is the bad news, published 0..1 or 0..100.
_SUCCESS_RATES = ("success_rate", "success_rate_7d", "win_rate", "detection_accuracy",
                  "accuracy")


def _num(value):
    """A float, or None for anything that isn't a plain number.

    Status files are written by four different projects and one of them will one
    day publish `"sharpe": "n/a"`. That must read as "no signal", not crash the
    digest.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _walk(data, depth=2):
    """Yield (key, value) pairs from a status payload, one nesting level deep.

    UFC nests its numbers under `data`, the trading bot publishes some at the
    top level. Rather than teach this module each project's shape, look in both
    places — the key names are the contract, not the path to them.
    """
    if not isinstance(data, dict):
        return
    for key, value in data.items():
        yield key, value
        if depth > 0 and isinstance(value, dict):
            yield from _walk(value, depth - 1)


def _fraction(value):
    """A 0..1 rate from a field published either as 0..1 or as 0..100."""
    return value / 100.0 if value > 1.0 else value


def kpi_signal(data) -> tuple[float, str | None]:
    """The worst domain KPI in this status payload -> (0..1, why) .

    Returns (0.0, None) when the project publishes nothing recognised. Silence is
    not a problem signal: inventing one would rank the projects that publish
    least as the healthiest, which is precisely backwards.
    """
    worst, why = 0.0, None

    def _consider(score, reason):
        nonlocal worst, why
        if score > worst:
            worst, why = score, reason

    for key, raw in _walk(data):
        value = _num(raw)
        if value is None:
            continue
        name = key.lower()
        if name in _NEGATIVE_IS_BAD and value < 0:
            # -1.0 and worse is as bad as this signal gets; a hair below zero
            # is a nudge, not an alarm.
            _consider(min(1.0, abs(value)), f"{key} is {value:g}")
        elif name in _EXHAUSTION:
            used = _fraction(value)
            if used >= 0.75:
                # 75% of a budget is where it starts mattering; 100% is a 1.0.
                _consider(min(1.0, (used - 0.75) / 0.25), f"{key} at {used * 100:.0f}%")
        elif name in _SUCCESS_RATES:
            rate = _fraction(value)
            if rate < 0.9:
                _consider(min(1.0, (0.9 - rate) / 0.9), f"{key} is {rate:.0%}")
    return worst, why


def error_signal(data) -> tuple[float, str | None]:
    """How badly the project is failing when it does run -> (0..1, why)."""
    for key, raw in _walk(data):
        name = key.lower()
        if name in ("last_error", "error") and isinstance(raw, str) and raw.strip():
            return 0.7, f"last error: {raw.strip()[:60]}"
        if name in ("failures", "failed_runs", "errors_7d", "error_count"):
            count = _num(raw)
            if count:
                # Five failures in a week is a fully-lit signal; one is a fifth
                # of one. Anything above five is already as loud as it gets.
                return min(1.0, count / 5.0), f"{int(count)} recent failure(s)"
    return 0.0, None


def staleness_signal(health) -> tuple[float, str | None]:
    """How out-of-date our picture of this project is -> (0..1, why).

    Scaled past the floor by how far the feed has overshot its own SLA, so a
    coachvision feed 12h past a weekly deadline does not rank alongside a crypto
    feed 153h past a daily one — which is the pair that motivated the scaling.
    """
    status = health.get("status") or "ok"
    score = _STATUS_FLOOR.get(status, 0.5)
    why = None
    if status in ("error", "blind"):
        cycles = health.get("blind_cycles") or 0
        why = f"unreadable{f' for {cycles} cycles' if cycles > 1 else ''}"
    elif status == "stale":
        age, sla = _num(health.get("age_hours")), _num(health.get("sla_hours"))
        if age and sla:
            overshoot = max(0.0, (age - sla) / sla)
            score = min(1.0, score + 0.4 * min(1.0, overshoot))
            why = f"data {age:g}h old against a {sla:g}h SLA"
        else:
            why = "data past-due"
    elif status == "idle":
        cycles = health.get("idle_cycles") or 0
        score = min(1.0, score + 0.1 * max(0, cycles - 1))
        why = f"no activity for {cycles} cycle(s)" if cycles > 1 else "no recent activity"
    return score, why


def backlog_signal(open_ideas) -> tuple[float, str | None]:
    """Untriaged open enhancement ideas -> (0..1, why)."""
    if not open_ideas:
        return 0.0, None
    return (min(1.0, open_ideas / BACKLOG_FULL),
            f"{open_ideas} open idea{'s' if open_ideas != 1 else ''}")


def open_ideas_by_repo(ledger) -> dict:
    """{repo slug: count of open enhancement proposals} from the ledger."""
    counts = {}
    for entry in (ledger or {}).get("entries", []):
        if entry.get("status") == "open" and entry.get("kind") == "enhancement":
            counts[entry.get("repo")] = counts.get(entry.get("repo"), 0) + 1
    return counts


# The phrase each signal contributes to the one-line "why". Written as the
# fallback: a signal that produced its own detail (an SLA overshoot, a named
# KPI) says that instead, because "past-due" is worth less than "153h old
# against a 48h SLA".
_SIGNAL_PHRASE = {
    "staleness": "we cannot see current data",
    "errors": "it is failing when it runs",
    "kpi": "its headline numbers are bad",
    "backlog": "ideas are piling up untriaged",
}


# ── PLAIN ENGLISH ────────────────────────────────────────────────────────
# The same finding as `why`, in words that need no context.
#
# `why` is written for someone who knows what an SLA is: "data 400h old against
# a 192h SLA" is precise, and it is the right thing to show beside a score. It
# is the wrong thing to lead a dashboard with, because the first question the
# page has to answer — is anything wrong, and with what — should be readable
# without knowing anything about this project at all.
#
# It is generated HERE rather than phrased in app.js for the same reason the
# score is (invariant 12): two sentences describing one finding, written in two
# languages, drift the first time the scoring changes, and the plain one is the
# one people would actually be reading.

# Below this a project is not worth mentioning on the plain summary at all.
# Every project scores something — an idea in the backlog is a non-zero signal —
# and a page that reports four "concerns" every week trains you to ignore it.
NOTABLE = 0.15


def _days(hours):
    """Hours as a whole number of days, for a sentence rather than a gauge."""
    try:
        return max(1, int(round(float(hours) / 24.0)))
    except (TypeError, ValueError):
        return None


def plain_predicate(health, signals, open_ideas=0) -> str:
    """What is going on with this project, as a phrase with no subject.

    Split from the full sentence so one wording serves both places the page
    needs it: the headline says "coachvision has not sent anything new in 17
    days", and the per-project list says "has not sent anything new in 17 days"
    beside the name it already printed. Deriving the second by trimming the
    first in JavaScript would break the first time a name appeared mid-sentence.

    Ordered by the same weights the score uses, so the phrase always describes
    the thing that put the project where it is in the ranking.
    """
    health = health or {}
    status = health.get("status")
    if status in ("error", "blind"):
        return "cannot be seen at all right now"
    if status == "stale":
        days = _days(health.get("age_hours"))
        return (f"has not sent anything new in {days} days"
                if days else "has stopped sending new information")
    if status == "idle":
        return "is running, but nothing has happened there lately"
    # Reads fine, so the concern (if any) is in what it reports.
    if signals.get("errors", 0) > 0:
        return "is working, but some of its jobs keep failing"
    if signals.get("kpi", 0) > 0:
        return "is working, but the numbers it reports look bad"
    if open_ideas:
        return (f"has {open_ideas} idea{'s' if open_ideas != 1 else ''} "
                f"waiting for someone to look at")
    return "looks fine"


def plain_reason(name, health, signals, open_ideas=0) -> str:
    """The predicate above as a whole sentence about one project."""
    if (health or {}).get("status") in ("error", "blind"):
        # Capitalised at source, unlike every other branch: those begin with the
        # project's own name, and "coachvision" is spelled that way. Upper-casing
        # a sentence's first letter would silently rename a project on the one
        # line of the page most people read.
        return f"We cannot see {name} at all right now"
    return f"{name} {plain_predicate(health, signals, open_ideas)}"


# What to actually do about it, keyed on the same dominant signal. Short enough
# to read at a glance and deliberately not a diagnosis — the page's job is to
# send you to the right project, not to fix it from the sofa.
_ACTIONS = {
    "unreadable": "Check whether it is still running.",
    "stale": "Check whether it is still running.",
    "idle": "Check whether it has anything to do.",
    "errors": "Have a look at what keeps failing.",
    "kpi": "Have a look at its numbers.",
    "backlog": "Some ideas are waiting for you to look at them.",
    "fine": "Nothing to do.",
}


def plain_action(health, signals, open_ideas=0) -> str:
    """The one thing worth doing about this project, in plain words."""
    status = (health or {}).get("status")
    if status in ("error", "blind"):
        return _ACTIONS["unreadable"]
    if status in ("stale", "idle"):
        return _ACTIONS[status]
    if signals.get("errors", 0) > 0:
        return _ACTIONS["errors"]
    if signals.get("kpi", 0) > 0:
        return _ACTIONS["kpi"]
    if open_ideas:
        return _ACTIONS["backlog"]
    return _ACTIONS["fine"]


def headline(ranked) -> str:
    """The one sentence the whole dashboard leads with.

    Deliberately singular. A list of four things needing attention is a list
    nobody reads to the end of; the ranking exists precisely so there is a first
    one, and the rest are a count.
    """
    notable = [r for r in (ranked or []) if r.get("score", 0) >= NOTABLE]
    if not notable:
        return "Everything looks fine."
    sentence = notable[0]["plain"] + "."
    others = len(notable) - 1
    if others:
        sentence += f" {others} other{'s' if others != 1 else ''} could use a look too."
    return sentence


def score_project(health, data=None, open_ideas=0) -> dict:
    """One project's attention score and the reason for it.

    Returns {score, signals, why}. `score` is 0..1 — not a percentage of
    anything, just a common scale that makes four projects comparable.
    """
    signals, reasons = {}, {}
    for name, (value, why) in {
        "staleness": staleness_signal(health or {}),
        "errors": error_signal(data),
        "kpi": kpi_signal(data),
        "backlog": backlog_signal(open_ideas),
    }.items():
        signals[name] = round(value, 3)
        if why:
            reasons[name] = why

    score = sum(WEIGHTS[name] * value for name, value in signals.items())

    # The "why" names the biggest CONTRIBUTOR (weight × signal), not the biggest
    # raw signal. A saturated backlog term is 0.15 of the score and must not be
    # the headline while a blind feed contributes 0.40.
    ranked = sorted(signals, key=lambda n: -WEIGHTS[n] * signals[n])
    top = [n for n in ranked if signals[n] > 0][:2]
    if not top:
        why = "healthy — nothing is asking for attention"
    else:
        why = "; ".join(reasons.get(n) or _SIGNAL_PHRASE[n] for n in top)
    return {"score": round(score, 3), "signals": signals, "why": why}


def rank(projects, readings=None, repos=None, ledger=None) -> list[dict]:
    """Every project, most-in-need-of-attention first.

    `projects`  — the tracer's per-project health, keyed by display name.
    `readings`  — {display name: the project's parsed status payload}.
    `repos`     — {display name: repo slug}, so the backlog term can be joined.
    `ledger`    — the delivery ledger, for the open-idea counts.

    Ties break alphabetically so the order is stable run to run: a ranking that
    reshuffles two equal projects every week reads as movement when nothing
    moved.
    """
    readings, repos = readings or {}, repos or {}
    open_ideas = open_ideas_by_repo(ledger)
    ranked = []
    for name, health in (projects or {}).items():
        repo = repos.get(name)
        # The whole read envelope, not just its `data` block: `_walk` looks one
        # level down, and the projects disagree about which level their numbers
        # live on (UFC nests under `data`, the trading bot does not).
        row = score_project(health or {},
                            data=readings.get(name),
                            open_ideas=open_ideas.get(repo, 0))
        row["name"] = name
        row["status"] = (health or {}).get("status")
        row["plain"] = plain_reason(name, health, row["signals"],
                                    open_ideas.get(repo, 0))
        row["short"] = plain_predicate(health, row["signals"], open_ideas.get(repo, 0))
        row["action"] = plain_action(health, row["signals"], open_ideas.get(repo, 0))
        # Whether this one is worth the reader's attention at all, decided by
        # the same floor `headline` uses — so the list and the headline can
        # never disagree about which projects are a concern.
        row["notable"] = row["score"] >= NOTABLE
        if repo:
            row["repo"] = repo
        ranked.append(row)
    ranked.sort(key=lambda r: (-r["score"], r["name"]))
    return ranked


def banner(ranked, limit=4) -> str:
    """An ATTENTION RANKING block for the digest, or "" when nothing scores.

    Deterministic and stitched in by `run_agent` alongside the staleness and
    delivery blocks (invariant 7), for the same reason those are: a prioritised
    list that depends on an agent remembering to write it is one that will
    eventually go missing on the week it mattered.

    No ':' or '>' in the heading — the dashboard's formatDigest only treats an
    ALL-CAPS line without them as a section heading (docs/app.js), and a heading
    that fails that regex silently degrades to body text.
    """
    rows = [r for r in (ranked or []) if r.get("score", 0) > 0]
    if not rows:
        return ""
    lines = ["ATTENTION RANKING (WHERE AN HOUR IS WORTH MOST)"]
    for position, row in enumerate(rows[:limit], start=1):
        lines.append(f"{position}. {row['name']} ({row['score']:.2f}) — {row['why']}")
    return "\n".join(lines)
