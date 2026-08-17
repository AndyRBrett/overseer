"""
Shared tools, config, and agent runtime for the Project Overseer pipeline.

The overseer is split into three sequential agents (see orchestrator.py):

  1. Bug-Hunter  — investigates and files confirmed bugs only
  2. Idea Agent  — brainstorms ranked enhancement ideas only
  3. Reviewer    — dedupes both outputs and sends one Telegram digest

Every agent script imports its tool implementations from this module so the
tool logic lives in exactly one place. This file also hosts:

  - the tool JSON schemas (per-agent subsets via `tool_specs`)
  - the `TOOL_FUNCTIONS` dispatch table reused by each agent loop
  - `run_agent`, the shared client.messages.create tool-use loop
  - the `--dry-run` switch (`set_dry_run`) that intercepts the mutating tools
    (file_issue, propose_enhancement, send_telegram_summary) so a run can be
    tested without anything hitting GitHub or Telegram

Configuration is via environment variables (see README.md). Anything not
configured degrades gracefully: the matching tool returns a "not_configured"
status the agent notes and works around, so the pipeline always runs end to end.
"""

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from tracer import RAISED_KEY, RunTracer, activity_idle

# ── MODEL TIERS ──────────────────────────────────────────────────────────
# The three agents do not all need the same class of model, and the two that
# run long tool loops dominate the bill.
#
#   Bug-Hunter  — HEAVY. Its calls are the consequential ones: telling a feed
#                 that is legitimately quiet apart from one that has died, and
#                 deciding whether to file a bug on a real repo. Getting that
#                 wrong means either a false alarm or another silent four-week
#                 outage, which is the failure this pipeline exists to catch.
#   Idea-Agent  — light. Enhancement ideation and effort/impact ranking. Its
#                 output is filtered by the Reviewer and then by a human, so a
#                 weak idea costs one line in a digest.
#   Reviewer    — light. Dedupes and summarizes two reports it is handed; it
#                 reads no data and files nothing.
#
# Both models are configurable. Set OVERSEER_LIGHT_MODEL to the same value as
# OVERSEER_MODEL — or list every agent in OVERSEER_HEAVY_AGENTS — to put the
# whole pipeline back on one model. The tiering has an off switch that needs no
# code change, and run_agent records the actual token spend per agent either
# way, so the saving is measured rather than assumed.
AGENT_NAMES = ("Bug-Hunter", "Idea-Agent", "Reviewer")


def _env(name, default=None):
    """Read an env var, trimming whitespace and treating a BLANK value as unset.

    Two things bite here. A repo slug pasted into a GitHub Variable can carry a
    trailing CRLF (overseer #3) — hence the strip. And an *unset* GitHub Actions
    variable is interpolated into the workflow as an empty string rather than
    omitted, so a plain `os.getenv(name, default)` returns "" and never reaches
    the default — which for a model name means handing the API model="".
    """
    value = os.getenv(name)
    if isinstance(value, str):
        value = value.strip()
    return value or default


MODEL = _env("OVERSEER_MODEL", "claude-opus-4-8")
LIGHT_MODEL = _env("OVERSEER_LIGHT_MODEL", "claude-sonnet-5")


def _parse_heavy_agents(raw):
    """Parse OVERSEER_HEAVY_AGENTS, warning loudly about names that match no agent.

    A typo here would silently demote the Bug-Hunter to the light model — the
    exact class of quiet misconfiguration this project keeps getting bitten by —
    so unknown names are reported rather than ignored.
    """
    names = tuple(n.strip() for n in raw.split(",") if n.strip())
    unknown = [n for n in names if n not in AGENT_NAMES]
    if unknown:
        print(f"[model] WARNING: OVERSEER_HEAVY_AGENTS names no such agent: "
              f"{', '.join(unknown)} (known: {', '.join(AGENT_NAMES)})")
    return names


HEAVY_AGENTS = _parse_heavy_agents(_env("OVERSEER_HEAVY_AGENTS", "Bug-Hunter"))


def model_for(agent):
    """The model this agent runs on: MODEL if it's judgment-heavy, else LIGHT_MODEL."""
    if not LIGHT_MODEL or LIGHT_MODEL == MODEL:
        return MODEL
    return MODEL if agent in HEAVY_AGENTS else LIGHT_MODEL


def tier_for(agent):
    return "heavy" if model_for(agent) == MODEL else "light"

# Safety bound on each agent's tool-use loop. Without this, a model that keeps
# calling tools would never terminate. On the final iteration we drop the tools
# so the model is forced to produce a closing summary instead of more tool calls.
MAX_ITERATIONS = 25

# The overseer's own schedule check lives with the freshness SLAs below, since
# it is the same rule applied to this project: see SCHEDULE_STALE_HOURS.


# The dashboard (docs/, served by GitHub Pages) reads this file. The weekly
# Action commits it after each run so the web app shows the latest digest.
DIGEST_PATH = os.getenv("DIGEST_PATH", "docs/digest.json")

# Append-only week-over-week history the dashboard turns into trend sparklines,
# so the overseer is a trend monitor and not just a point-in-time board
# (overseer #6). Capped so the file (and the sparklines) stay small.
HISTORY_PATH = os.getenv("HISTORY_PATH", "docs/history.json")

# What the pipeline has actually DELIVERED, not just proposed — read back from
# the issues it filed. Feeds the dashboard's "Shipped" panel and the agents'
# dedupe context.
LEDGER_PATH = os.getenv("LEDGER_PATH", "docs/shipped.json")
HISTORY_MAX_RUNS = int(os.getenv("HISTORY_MAX_RUNS", "26"))  # ~6 months of weekly runs

# ── DRY-RUN SWITCH ───────────────────────────────────────────────────────
# When enabled, the mutating tools print what they WOULD do and return a
# "dry_run" status instead of touching GitHub or Telegram. Toggled by the
# orchestrator's --dry-run flag via set_dry_run().
DRY_RUN = False


def set_dry_run(value: bool) -> None:
    global DRY_RUN
    DRY_RUN = bool(value)


# ── PROJECT CONFIG ───────────────────────────────────────────────────────
# Repo slug ("owner/name") + data-source location per project, from env.
# The repo slugs are injected into the system prompts so the agents file issues
# and enhancements against the correct repositories.
# (_env, which reads these, is defined with the model config near the top.)


# ── PER-PROJECT FRESHNESS SLA (overseer #1) ──────────────────────────────
# How old a project's published status file may get before it's flagged STALE.
# A silently-halted feed must trip an alert, not read as healthy: issue #34 was
# the crypto status file sitting ~153h stale while the digest stayed quiet.
#
# THE RULE: an SLA is derived from how often that project actually PUBLISHES,
# never picked by feel.
#
#     sla_hours = publish_interval_hours + SLA_GRACE_HOURS
#
# One full missed publish cycle, plus a day of slack because GitHub's scheduled
# crons are best-effort and routinely drift. A daily publisher lands on 48h and
# a weekly one on 192h, which is where both conventions in this repo already sat
# — the rule reproduces them rather than reinventing them.
#
# Why it matters that this is derived: coachvision publishes weekly (Mondays
# 06:00 UTC) and was being graded against the shared 48h default, so from every
# Wednesday onward it was flagged STALE for behaving exactly as designed. Five
# consecutive runs raised a correct-looking alarm about a healthy project. An
# alert that fires on normal operation is worse than no alert, because it
# teaches you to skim past the panel where the real one will appear.
#
# Both halves are overridable per project: <PROJECT>_PUBLISH_INTERVAL_HOURS to
# state the cadence, or <PROJECT>_SLA_HOURS to set the deadline outright.
FRESHNESS_SLA_DEFAULT_HOURS = int(os.getenv("FRESHNESS_SLA_HOURS", "48"))
SLA_GRACE_HOURS = int(os.getenv("SLA_GRACE_HOURS", "24"))

HOURLY, DAILY, WEEKLY = 1, 24, 168


def sla_for_interval(publish_interval_hours):
    """The staleness deadline for a feed that publishes every N hours."""
    return publish_interval_hours + SLA_GRACE_HOURS


# The overseer runs weekly (weekly-review.yml: `0 14 * * 1`), so its own missed
# schedule is the same rule applied to this project — a skipped run must not read
# as healthy (overseer #5). Derived rather than hardcoded to 192 so that if the
# grace period is ever retuned, the overseer is held to the same bar it holds
# everything else to.
SCHEDULE_STALE_HOURS = sla_for_interval(WEEKLY)


def _schedule_stale(age_hours):
    return age_hours is not None and age_hours > SCHEDULE_STALE_HOURS


def _int_env(env_name, default=None):
    """An int from env, falling back rather than erroring on junk — a
    fat-fingered GitHub Variable must not break the whole run."""
    raw = _env(env_name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _sla_hours(env_name, publish_interval_hours=None):
    """A project's freshness SLA, in precedence order:

    1. <PROJECT>_SLA_HOURS — an explicit deadline, when the cadence rule doesn't
       fit (a feed with no schedule at all, say).
    2. <PROJECT>_PUBLISH_INTERVAL_HOURS — state the cadence, derive the deadline.
    3. The project's known publish interval, declared in PROJECTS below.
    4. The shared default, for a project whose cadence nobody has recorded yet.
    """
    explicit = _int_env(env_name)
    if explicit is not None:
        return explicit
    interval = _int_env(env_name.replace("_SLA_HOURS", "_PUBLISH_INTERVAL_HOURS"),
                        publish_interval_hours)
    if interval is not None:
        return sla_for_interval(interval)
    return FRESHNESS_SLA_DEFAULT_HOURS


PROJECTS = {
    "trading_bot": {
        "label": "Crypto trading bot (Coinbase Advanced Trade via CCXT, daily cloud runs)",
        "repo": _env("TRADING_REPO"),
        "db_path": _env("TRADING_DB_PATH"),              # local deployments
        "status_path": _env("TRADING_STATUS_PATH", "overseer-status.json"),  # cloud: file the bot publishes
        # The bot ticks hourly, but run-bot.yml gates the status publish behind
        # MIN_AGE_HOURS=20, so the file itself refreshes about once a day. The
        # SLA tracks the PUBLISH cadence, not the tick cadence. → 48h
        "sla_hours": _sla_hours("TRADING_SLA_HOURS", DAILY),
    },
    # Internal key + env vars stay "volleyball"/VOLLEYBALL_* (the deployment's
    # GitHub Variables are wired to them); only the human-facing name changed
    # after the repo rebranded from Volleyball to coachvision (martial arts).
    "volleyball": {
        "label": "coachvision — martial-arts CV pipeline (technique tracking + coaching feedback)",
        "repo": _env("VOLLEYBALL_REPO"),
        "results_path": _env("VOLLEYBALL_RESULTS_PATH"),                       # local
        "status_path": _env("VOLLEYBALL_STATUS_PATH", "overseer-status.json"),  # cloud
        # overseer-status.yml runs `schedule: 0 6 * * 1` — weekly, Mondays 06:00
        # UTC — and publishes whether or not footage arrived, precisely so the
        # overseer can tell idle from broken. Under the old 48h default it read
        # STALE from every Wednesday on, which is why five consecutive runs
        # raised an alarm about a project that was working as designed. → 192h
        "sla_hours": _sla_hours("VOLLEYBALL_SLA_HOURS", WEEKLY),
    },
    "ufc": {
        "label": "UFC fight card dashboard (scraper + odds tracking)",
        "repo": _env("UFC_REPO"),  # repo whose Actions runs + status file we read
        "status_path": _env("UFC_STATUS_PATH", "overseer-status.json"),
        # update.yml runs far more often around a card (every 4h Thu–Sat, every
        # 5 min during fight windows), but the only GUARANTEED publish is the
        # daily 09:00 UTC cron. An SLA is a floor, so it tracks the slowest
        # guaranteed cadence — sizing it on fight-night frequency would page us
        # every ordinary Tuesday. → 48h
        "sla_hours": _sla_hours("UFC_SLA_HOURS", DAILY),
    },
    "overseer": {
        "label": "Project Overseer itself — this agent: the weekly-review runner, "
                 "tools, tracer, and the GitHub Pages dashboard",
        # Defaults to the repo the Action runs in (GITHUB_REPOSITORY); override with OVERSEER_REPO.
        "repo": _env("OVERSEER_REPO") or _env("GITHUB_REPOSITORY"),
    },
}

# The three external projects the pipeline reviews.
CORE_PROJECTS = ("trading_bot", "volleyball", "ufc")

# What the Bug-Hunter and Idea agents actually review: the three external
# projects PLUS Project Overseer itself. The overseer is held to the same bar as
# any other project — it gets its own read tool (read_overseer_status) and the
# agents file bugs / propose enhancements against the overseer repo too.
REVIEW_PROJECTS = CORE_PROJECTS + ("overseer",)

# Maps each read tool to the project it reports on — used to track per-project
# read health (blind-spot detection) across runs. (overseer self-review #1)
# The value is only a FALLBACK display name: when a project publishes an `app`
# field in its overseer-status.json, that self-reported name wins on the
# dashboard (see tracer.project_health / _app_name). This label is what shows
# when the read fails or the status file omits `app`.
READ_TOOLS = {
    "read_trading_bot_log": "Trading bot",
    "read_volleyball_results": "coachvision",
    "read_ufc_scraper_status": "UFC dashboard",
    "read_overseer_status": "Overseer",
}

# SQL used by read_trading_bot_log. Adjust the table/column names to match your
# trade log. It must return one row of aggregates. `:since` is bound to the
# start of the window.
TRADING_QUERY = """
    SELECT
        COUNT(*)                                   AS trades,
        COALESCE(SUM(pnl), 0)                      AS pnl,
        COALESCE(AVG(CASE WHEN pnl > 0 THEN 1.0 ELSE 0.0 END), 0) AS win_rate
    FROM trades
    WHERE ts >= :since
"""

# ── GITHUB CLIENT ────────────────────────────────────────────────────────

_gh = None


def _github():
    """Lazy GitHub client. Raises a clear error if no token is configured."""
    global _gh
    if _gh is None:
        token = os.getenv("OVERSEER_GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN")
        if not token:
            raise RuntimeError(
                "No GitHub token. Set OVERSEER_GITHUB_TOKEN (a PAT with Issues "
                "read/write on your project repos)."
            )
        from github import Auth, Github  # PyGithub
        _gh = Github(auth=Auth.Token(token))
    return _gh


# ── DELIVERY LEDGER ──────────────────────────────────────────────────────
# Closing the loop: the overseer proposes work every week and never learns what
# came of it. Two costs follow from that, and this fixes both from one source.
#
#   1. Nothing shows what the pipeline has actually DELIVERED — only what it
#      suggested. A month of digests reads as a pile of ideas with no evidence
#      any of them mattered.
#   2. With no memory of what it already proposed, the Idea agent re-proposes it.
#      That is not hypothetical: #13/#17 are the same dead-man's switch five
#      weeks apart, #11/#15 the same staleness alerting, #9/#12/#14/#16 the same
#      schema validation four times over seven weeks, and ufc-dashboard#68
#      proposed a per-bout CLV tracker that was already fully built and running.
#
# Every issue the agents file is stamped with OVERSEER_MARKER, so the ledger is
# just "read back our own issues and see what happened to them".

# Stamped into every filed issue body. Changing this orphans older issues from
# the ledger, so treat it as a stable identifier, not a message.
OVERSEER_MARKER = "_Filed by Project Overseer._"


def _ledger_entry(issue, merged_only=True):
    """One ledger row from a GitHub issue, or None if it isn't ours.

    `merged_only` decides what counts as SHIPPED. A closed issue whose fix sits
    on an unmerged branch is 'in flight', not delivered — claiming otherwise
    would let the dashboard take credit for unreviewed code. GitHub only reports
    a linked PR as merged once it lands on the default branch, so that flag is
    the honest signal and we key off it rather than off the close alone.
    """
    body = issue.body or ""
    labels = {l.name for l in issue.labels}
    # Legacy fallback: bugs filed before file_issue stamped the marker, and
    # enhancements are still recognisable by their title prefix + effort labels.
    ours = (OVERSEER_MARKER in body
            or issue.title.startswith("[enhancement]")
            or any(l.startswith("effort:") for l in labels))
    if not ours:
        return None

    kind = "enhancement" if ("enhancement" in labels
                             or issue.title.startswith("[enhancement]")) else "bug"
    entry = {
        "repo": issue.repository.full_name,
        "number": issue.number,
        "title": issue.title.removeprefix("[enhancement]").strip(),
        "kind": kind,
        "url": issue.html_url,
        "state": issue.state,
        "created_at": issue.created_at.isoformat() if issue.created_at else None,
        "closed_at": issue.closed_at.isoformat() if issue.closed_at else None,
    }
    for axis in ("effort", "impact"):
        match = next((l.split(":", 1)[1] for l in labels if l.startswith(f"{axis}:")), None)
        if match:
            entry[axis] = match
    # Carried so the implementation gate can read control labels
    # (overseer:implementing / overseer:no-implement) off the ledger instead of
    # re-fetching every issue to ask.
    if labels:
        entry["labels"] = sorted(labels)

    if issue.state != "closed":
        # An open issue with work already on a branch is IN FLIGHT, not merely
        # open — otherwise the panel shows nothing happening right up until the
        # moment a PR merges, which is the least useful time to learn about it.
        pending = [pr for pr in _linked_prs(issue) if not pr.merged and pr.state == "open"]
        if pending:
            entry["status"] = "in_flight"
            entry["fix_url"] = pending[0].html_url
            entry["fix_ref"] = f"PR #{pending[0].number}"
        else:
            entry["status"] = "open"
        return entry

    reason = getattr(issue, "state_reason", None)
    if reason in ("duplicate", "not_planned"):
        # Explicitly NOT shipped, and worth keeping: a duplicate is evidence the
        # dedupe is failing, which is half the reason this ledger exists.
        entry["status"] = reason
        return entry

    # A closed-as-completed issue is DELIVERED unless there is positive evidence
    # the work is still pending. The first cut had this backwards: it treated the
    # absence of a merged PR as proof of non-delivery, so anything shipped by a
    # direct commit to main could never read as shipped. These repos land most
    # work that way — the first refresh duly reported six coachvision features
    # from June, long live in production, as "in flight". Absence of a PR link is
    # absence of evidence, not evidence of absence.
    prs = _linked_prs(issue)
    merged = [pr for pr in prs if pr.merged]
    pending = [pr for pr in prs if not pr.merged and pr.state == "open"]

    # WHERE it landed, best source first. Without this the panel can say
    # "shipped" and still leave you hunting through commit history for it.
    if merged:
        entry["fix_url"] = merged[0].html_url
        entry["fix_ref"] = f"PR #{merged[0].number}"
        if merged[0].merge_commit_sha:
            entry["fix_sha"] = merged[0].merge_commit_sha[:7]
    elif pending:
        entry["fix_url"] = pending[0].html_url
        entry["fix_ref"] = f"PR #{pending[0].number} (open)"
    else:
        sha = _closing_commit(issue)
        if sha:
            entry["fix_url"] = f"https://github.com/{entry['repo']}/commit/{sha}"
            entry["fix_ref"] = sha[:7]
            entry["fix_sha"] = sha[:7]

    if not merged_only:
        entry["status"] = "shipped"
    elif merged:
        entry["status"] = "shipped"
    elif pending:
        # The one case that genuinely is not delivered yet: a fix exists but is
        # sitting in review. This is what keeps the panel from taking credit for
        # unmerged code.
        entry["status"] = "in_flight"
    else:
        entry["status"] = "shipped"
    return entry


def _closing_commit(issue):
    """SHA of the commit that closed this issue, if it was closed by one.

    Covers the direct-commit workflow these repos actually use, so "where did
    this land" has an answer even when no pull request was involved.
    """
    try:
        for event in issue.get_timeline():
            if event.event == "closed" and getattr(event, "commit_id", None):
                return event.commit_id
    except Exception:  # noqa: BLE001 — enrichment must never break a run
        return None
    return None


def _linked_prs(issue):
    """Pull requests cross-referenced from this issue's timeline.

    Best-effort by design: the timeline API is the only place the link is
    recorded, and a repo where we lack permission to read it yields nothing
    rather than failing the run.
    """
    out = []
    try:
        for event in issue.get_timeline():
            if event.event not in ("cross-referenced", "connected", "closed"):
                continue
            source = getattr(event, "source", None)
            pr = getattr(source, "issue", None) if source else None
            if pr is not None and getattr(pr, "pull_request", None):
                out.append(pr.as_pull_request())
    except Exception:  # noqa: BLE001 — ledger enrichment must never break a run
        return []
    return out


def _has_open_fix(issue):
    """True when an unmerged PR references this still-open issue."""
    return any(not pr.merged and pr.state == "open" for pr in _linked_prs(issue))


def _has_merged_fix(issue):
    """True when a merged PR closed this issue — the signal that work landed."""
    return any(pr.merged for pr in _linked_prs(issue))


# Outcomes that cannot change again. A shipped fix does not un-ship, and a
# duplicate does not stop being one — so a refresh can carry these forward
# instead of re-walking each issue's timeline. That is what makes a frequent
# refresh cheap: the per-issue PR lookup is the expensive call, and it only has
# to run for the handful of entries still in motion.
TERMINAL_STATUSES = ("shipped", "duplicate", "not_planned")


def delivery_ledger(merged_only=True, limit_per_repo=100, known=None):
    """Every overseer-filed issue across the reviewed repos, with its outcome.

    `known`: a previously published ledger (as returned by this function or read
    back from LEDGER_PATH). Entries in it with a terminal status are reused
    as-is, skipping their timeline lookup. Pass None for a full rebuild.

    Returns {"entries": [...], "totals": {...}, "errors": {...}}. Never raises:
    an unreachable repo contributes an error note instead of killing the run,
    because a partial ledger is still worth showing.
    """
    settled = {}
    for entry in (known or {}).get("entries", []):
        if entry.get("status") in TERMINAL_STATUSES:
            settled[(entry.get("repo"), entry.get("number"))] = entry

    entries, repo_errors = [], {}
    for key in REVIEW_PROJECTS:
        slug = PROJECTS[key].get("repo")
        if not slug:
            continue
        try:
            repo = _github().get_repo(slug)
            for issue in repo.get_issues(state="all")[:limit_per_repo]:
                if issue.pull_request is not None:
                    continue  # get_issues returns PRs too
                prior = settled.get((slug, issue.number))
                # Reuse only while the issue is still closed. If it was reopened
                # its outcome is live again and has to be recomputed, or the
                # ledger would keep reporting a settled state that no longer holds.
                if prior and issue.state == "closed":
                    entries.append(prior)
                    continue
                entry = _ledger_entry(issue, merged_only=merged_only)
                if entry:
                    entries.append(entry)
        except Exception as exc:  # noqa: BLE001
            repo_errors[slug] = str(exc)[:120]

    entries.sort(key=lambda e: e.get("closed_at") or e.get("created_at") or "", reverse=True)
    totals = {"proposed": len(entries)}
    for status in ("shipped", "in_flight", "open", "duplicate", "not_planned"):
        totals[status] = sum(1 for e in entries if e["status"] == status)
    # The number the dashboard leads with. Duplicates are excluded from the
    # denominator: re-proposing the same idea shouldn't dilute the delivery rate,
    # it's tracked separately as a dedupe failure.
    considered = totals["proposed"] - totals["duplicate"]
    totals["delivery_rate"] = round(totals["shipped"] / considered, 3) if considered else 0.0
    totals["duplicate_rate"] = round(totals["duplicate"] / totals["proposed"], 3) if entries else 0.0
    return {"entries": entries, "totals": totals, "errors": repo_errors}


def write_ledger(ledger, path=None):
    """Persist the ledger for the dashboard. No-op when there's nothing to write.

    A None ledger means the fetch failed this run; the previously published file
    is left alone rather than being replaced with an empty one, so a transient
    GitHub error doesn't blank the panel.
    """
    if not ledger:
        return None
    path = path or LEDGER_PATH
    payload = dict(ledger)
    payload["generated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.write("\n")
    return path


def known_work_block(ledger, per_repo=12):
    """Compact 'already proposed / already shipped' list for the agent prompts.

    This is the half of the ledger that changes agent BEHAVIOUR rather than the
    dashboard. Given the pipeline re-proposed the same idea four times over seven
    weeks, showing each agent what it has already asked for is the cheapest
    available fix — far cheaper than hoping it remembers to search first.
    """
    by_repo = {}
    for entry in ledger.get("entries", []):
        by_repo.setdefault(entry["repo"], []).append(entry)
    if not by_repo:
        return "(no previously filed issues on record)"

    lines = []
    for repo, items in sorted(by_repo.items()):
        lines.append(f"{repo}:")
        for e in items[:per_repo]:
            state = {"shipped": "SHIPPED", "in_flight": "IN FLIGHT",
                     "open": "STILL OPEN", "duplicate": "closed as duplicate",
                     "not_planned": "closed, not planned"}.get(e["status"], e["status"])
            lines.append(f"  - #{e['number']} [{state}] {e['title']}")
    return "\n".join(lines)


# ── IMPLEMENTATION QUEUE ─────────────────────────────────────────────────
# The pipeline proposes work every week and a human implements it — or, judging
# by the ledger's own delivery rate, mostly doesn't. What closes that gap is not
# a fourth reviewing agent but a DISPATCHER: pick a few already-filed issues and
# hand each one to a coding agent running in the PROJECT'S OWN repo, where that
# project's tests, dependencies and CI already live. Nothing in this module
# writes code. It decides what is worth attempting, and refuses to attempt more
# than a person can review.
#
# THE GATE IS THE DESIGN. "Implement everything filed" produces a PR queue
# bigger than the digest it was meant to replace, and since shipped means MERGED
# (see _ledger_entry), unreviewed PRs would park in `in_flight` forever while the
# spend climbed — the delivery rate would get worse, not better. So:
#
#   - confirmed bugs, plus enhancements the Idea Agent itself labelled effort:low
#   - only issues that are OPEN with no fix already in flight
#   - at most OVERSEER_IMPLEMENT_MAX per run (default 3), round-robined across
#     repos so one busy project cannot take every slot every week
#   - a PR is where this stops. Nothing here merges anything.

# Cap per dispatch run. Three is "a evening's review", not a throughput target;
# raise it once the PRs are landing rather than piling up.
IMPLEMENT_MAX = _int_env("OVERSEER_IMPLEMENT_MAX", 3) or 3

# Which enhancement efforts may be attempted. Bugs are always eligible — a
# confirmed bug is a defect with evidence attached, which is the easiest thing
# to hand a coding agent and the thing you most want fixed. Set
# OVERSEER_IMPLEMENT_EFFORT="low,medium" to widen it.
IMPLEMENT_EFFORTS = tuple(
    e.strip().lower() for e in _env("OVERSEER_IMPLEMENT_EFFORT", "low").split(",") if e.strip()
)

# repository_dispatch event the per-repo implementer workflow listens for
# (examples/implementer/implement.yml).
IMPLEMENT_EVENT = _env("OVERSEER_IMPLEMENT_EVENT", "overseer-implement")

# Applied to an issue once it has been handed over, so the next run doesn't hand
# it over again. The linked PR is the second line of defence: once one exists the
# entry reads `in_flight` and the gate excludes it regardless of labels.
IMPLEMENTING_LABEL = "overseer:implementing"

# Your opt-out. Put this on anything you want to decide yourself.
NO_IMPLEMENT_LABEL = "overseer:no-implement"

# Swapped in for IMPLEMENTING_LABEL when an attempt fails (the implementer
# workflow does this in an `if: failure()` step). Without it a failed run left
# the issue marked as handed-over forever: no PR, no retry, no signal beyond a
# red workflow — a filed issue silently burned, which is precisely the failure
# shape the rest of this project exists to prevent. It excludes the issue from
# the queue rather than re-queueing it, because an attempt that ran out of turns
# or couldn't get the suite passing will usually do the same thing again on
# Monday, at full price. Remove the label to put it back in the queue.
FAILED_LABEL = "overseer:implement-failed"

_IMPACT_RANK = {"high": 0, "medium": 1, "low": 2}


def implementable(entry, efforts=None):
    """May the implementer attempt this ledger entry? -> (bool, reason).

    The reason is returned even on success paths' counterparts because "why was
    my issue skipped?" is the first question anyone asks of a gate, and a
    dispatcher that can't answer it gets switched off.
    """
    efforts = IMPLEMENT_EFFORTS if efforts is None else efforts
    labels = set(entry.get("labels") or ())

    status = entry.get("status")
    if status != "open":
        # in_flight already has a PR open against it; shipped / duplicate /
        # not_planned are finished. Only `open` is unclaimed work.
        return False, f"status is {status}, not open"
    if NO_IMPLEMENT_LABEL in labels:
        return False, f"labelled {NO_IMPLEMENT_LABEL}"
    if IMPLEMENTING_LABEL in labels:
        return False, "already handed to the implementer"
    if FAILED_LABEL in labels:
        return False, f"a previous attempt failed (remove {FAILED_LABEL} to re-queue)"

    if entry.get("kind") == "bug":
        return True, "confirmed bug"

    effort = (entry.get("effort") or "").lower()
    if not effort:
        # An enhancement with no effort label predates the labelling or failed to
        # apply. Unsized work is exactly what this gate exists to keep out.
        return False, "enhancement with no effort label"
    if effort not in efforts:
        return False, f"effort:{effort} (gate allows {', '.join(efforts) or 'nothing'})"
    return True, f"enhancement, effort:{effort}"


def _queue_sort_key(entry):
    """Bugs before enhancements, higher impact first, then oldest first.

    Age last but not never: without it a long-lived low-impact item is starved
    forever by a steady drip of newer ones.
    """
    return (
        0 if entry.get("kind") == "bug" else 1,
        _IMPACT_RANK.get((entry.get("impact") or "").lower(), 3),
        entry.get("created_at") or "",
        entry.get("number") or 0,
    )


def implementation_queue(ledger, limit=None, efforts=None):
    """What to attempt this run: {"picks": [...], "skipped": [...]}.

    Pure function of a ledger — it makes no API calls, so the gate can be tested
    and dry-run without touching GitHub.
    """
    limit = IMPLEMENT_MAX if limit is None else limit
    by_repo, skipped = {}, []
    for entry in (ledger or {}).get("entries", []):
        ok, why = implementable(entry, efforts)
        if ok:
            by_repo.setdefault(entry["repo"], []).append(entry)
        else:
            skipped.append({"entry": entry, "reason": why})

    for items in by_repo.values():
        items.sort(key=_queue_sort_key)

    # Round-robin across repos, best-first within each round. A project with
    # twenty open bugs would otherwise take every slot every week and the other
    # three would never see a PR — and the overseer, which files against itself
    # most often, is exactly that project.
    picks = []
    while len(picks) < limit:
        live = [r for r, items in by_repo.items() if items]
        if not live:
            break
        for repo in sorted(live, key=lambda r: _queue_sort_key(by_repo[r][0])):
            picks.append(by_repo[repo].pop(0))
            if len(picks) >= limit:
                break
    return {"picks": picks, "skipped": skipped,
            "eligible": sum(len(v) for v in by_repo.values()) + len(picks)}


def dispatch_implementation(entry, event_type=None, dry_run=None):
    """Hand ONE filed issue to the implementer workflow in its own repo.

    Two steps, in this order and no other: fire the repository_dispatch, then
    label the issue. A dispatch that fails — a token without Actions: write is
    the likely cause — must leave the issue unlabelled so the next run retries
    it. Labelling first would drop the issue on the floor silently, which is the
    failure mode this whole project keeps being bitten by.

    A label that fails to apply after a successful dispatch is reported rather
    than raised: the work IS under way, and the PR it opens links the issue,
    which moves the entry to `in_flight` and excludes it from the next queue
    anyway. Belt and braces, in that order.
    """
    event_type = event_type or IMPLEMENT_EVENT
    dry_run = DRY_RUN if dry_run is None else dry_run
    slug, number = entry["repo"], entry["number"]
    payload = {
        "issue": number,
        "title": entry.get("title", ""),
        "url": entry.get("url", ""),
        "kind": entry.get("kind", "bug"),
        "effort": entry.get("effort", ""),
        "impact": entry.get("impact", ""),
    }
    if dry_run:
        print(f"\n[DRY-RUN] would dispatch '{event_type}' to {slug}:")
        print(f"          issue: #{number} {payload['title']}")
        print(f"          kind : {payload['kind']} "
              f"(effort {payload['effort'] or '—'}, impact {payload['impact'] or '—'})")
        print(f"          then label it {IMPLEMENTING_LABEL}\n")
        return {"status": "dry_run", "repo": slug, "number": number}

    repo = _github().get_repo(slug)
    repo.create_repository_dispatch(event_type=event_type, client_payload=payload)

    result = {"status": "dispatched", "repo": slug, "number": number,
              "event": event_type}
    try:
        repo.get_issue(number).add_to_labels(IMPLEMENTING_LABEL)
    except Exception as exc:  # noqa: BLE001 — the work is already under way
        result["status"] = "dispatched_unlabelled"
        result["label_error"] = str(exc)[:120]
    return result


def _parse_stamp(value):
    """ISO timestamp -> aware datetime, or None. Ledger stamps only; never raises."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def delivery_banner(ledger, days=7, now=None, limit=6):
    """A plain-text IMPLEMENTED block for the foot of the weekly digest.

    The answer to "so what did it actually build this week". Derived from the
    ledger rather than from anything an agent said, for the same reason the
    staleness banner is: a summary that depends on an LLM remembering to mention
    something is a summary that will one day not mention it.

    Returns "" when nothing shipped and nothing is in review, so a quiet week's
    digest is left exactly as the Reviewer wrote it.
    """
    entries = (ledger or {}).get("entries", [])
    if not entries:
        return ""
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)

    shipped, in_flight = [], []
    for e in entries:
        if e.get("status") == "shipped":
            closed = _parse_stamp(e.get("closed_at"))
            if closed and closed >= cutoff:
                shipped.append(e)
        elif e.get("status") == "in_flight":
            in_flight.append(e)
    if not shipped and not in_flight:
        return ""

    def _line(e):
        where = f" ({e['fix_ref']})" if e.get("fix_ref") else ""
        return f"- {e['repo'].split('/')[-1]} #{e['number']} — {e['title']}{where}"

    shipped.sort(key=lambda e: e.get("closed_at") or "", reverse=True)
    lines = [f"IMPLEMENTED (LAST {days} DAYS)"]
    if shipped:
        lines += [_line(e) for e in shipped[:limit]]
        if len(shipped) > limit:
            lines.append(f"- …and {len(shipped) - limit} more merged.")
    else:
        lines.append("- Nothing merged this week.")
    if in_flight:
        # Named, not just counted: an implementer whose PRs never get reviewed
        # looks identical to one that never ran, and this is the line that tells
        # the two apart.
        lines.append(f"- {len(in_flight)} fix(es) awaiting review: "
                     + ", ".join(f"{e['repo'].split('/')[-1]}#{e['number']}"
                                 for e in in_flight[:limit]))
    return "\n".join(lines)


# ── CREDENTIAL PREFLIGHT ─────────────────────────────────────────────────
# Why this exists: four consecutive weekly runs (2026-07-20 → 2026-08-10)
# reported SUCCESS in the Actions tab while every GitHub tool call inside them
# failed with 401 "Bad credentials" on an expired PAT. Each agent handled its
# tool errors gracefully and still produced a digest, so the workflow never went
# red and the outage stayed invisible for four weeks — the projects simply went
# unreviewed. Checking the credential up front turns that silent failure into a
# loud one, before an agent burns a run (and API spend) on a dead token.

def _http_status(exc):
    """HTTP status carried by a PyGithub exception, if any."""
    return getattr(exc, "status", None)


# How hard the preflight tries before calling GitHub unreachable. A 5xx from
# api.github.com is usually seconds long, and PyGithub's own retries give up
# inside ten seconds — too fast to ride out even a brief wobble.
PREFLIGHT_ATTEMPTS = int(os.getenv("OVERSEER_PREFLIGHT_ATTEMPTS", "3"))
PREFLIGHT_BACKOFF_SECONDS = float(os.getenv("OVERSEER_PREFLIGHT_BACKOFF", "5"))

# Network-level failures arrive with no HTTP status at all — requests wraps the
# exhausted urllib3 retry as a ConnectionError whose text names the real cause.
_TRANSIENT_MARKERS = (
    "max retries exceeded", "too many 5", "connection aborted",
    "connection reset", "connection refused", "connection error",
    "timed out", "timeout", "temporarily unavailable", "bad gateway",
    "service unavailable", "server error", "remote end closed",
)
_TRANSIENT_EXCEPTIONS = {
    "connectionerror", "connectiontimeout", "connectionresetterror",
    "maxretryerror", "newconnectionerror", "readtimeout", "readtimeouterror",
    "retryerror", "timeout", "timeouterror",
}


def _is_transient(exc):
    """True when a call failed because GitHub was briefly unavailable.

    The distinction that matters everywhere below: a 401 means the credential is
    dead and a human has to fix it, while a 503 means api.github.com had a bad
    minute and the next run will be fine. Treating the second like the first is
    how a nine-second outage turns into a red workflow and a failure email.
    """
    status = _http_status(exc)
    if status is not None:
        return int(status) in (429, 502, 503, 504) or 500 <= int(status) < 600
    names = {cls.__name__.lower() for cls in type(exc).__mro__}
    if names & _TRANSIENT_EXCEPTIONS:
        return True
    text = str(exc).lower()
    return any(marker in text for marker in _TRANSIENT_MARKERS)


def _authenticated_login():
    """Log in, riding out transient GitHub failures.

    Returns (login, error). `error` is None on success; otherwise it is the
    preflight report for a credential that is genuinely broken, or for a GitHub
    that stayed unreachable across every attempt.
    """
    for attempt in range(1, max(PREFLIGHT_ATTEMPTS, 1) + 1):
        try:
            return _github().get_user().login, None
        except Exception as exc:  # noqa: BLE001 — auth failure surfaces as 401
            status = _http_status(exc)
            if not _is_transient(exc):
                hint = ("token is expired or revoked — regenerate it and update "
                        "the OVERSEER_GITHUB_TOKEN repo secret") if status == 401 else str(exc)
                return None, {"status": "error", "fatal": True,
                              "detail": f"GitHub auth failed ({status}): {hint}"}
            if attempt >= max(PREFLIGHT_ATTEMPTS, 1):
                return None, {
                    "status": "unavailable", "fatal": True, "transient": True,
                    "detail": f"GitHub API unreachable after {attempt} attempt(s) "
                              f"({status}): {exc}. The credential was never "
                              f"rejected — this is GitHub, not the token.",
                }
            time.sleep(PREFLIGHT_BACKOFF_SECONDS * attempt)


def preflight_github():
    """Validate the GitHub credential and per-repo reach before the agents run.

    Checks three things, cheapest first:
      1. a token is configured at all
      2. the token authenticates (401 ⇒ expired/revoked — the July failure)
      3. each configured project repo is actually reachable with it (404/403 ⇒
         the repo was left out when the token was regenerated, which fails only
         for that project and is easy to miss)

    Returns a structured report and never raises — the caller decides what is
    fatal. `status` is one of:
      "ok"             every configured repo is reachable
      "not_configured" no token at all (local dev / partial setup)
      "error"          the token is present but rejected, or some repo is
                       unreachable; `fatal` is True when NO repo is reachable,
                       which is the case where a run is worthless.
      "unavailable"    GitHub itself could not be reached (5xx / network) on
                       every attempt. Also fatal — nothing can be read — but
                       carries `transient: True`, because the fix is to wait
                       rather than to touch the credential.
    """
    if not (os.getenv("OVERSEER_GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN")):
        return {"status": "not_configured",
                "detail": "No OVERSEER_GITHUB_TOKEN — every GitHub-backed tool "
                          "will report not_configured.",
                "fatal": False}

    login, error = _authenticated_login()
    if error:
        return error

    # Per-repo reach. A fine-grained PAT scoped to only some of the projects
    # authenticates fine but 404s on the ones it was not granted.
    repos, unreachable, transient = {}, [], []
    for key in REVIEW_PROJECTS:
        slug = PROJECTS[key].get("repo")
        if not slug:
            continue
        try:
            _github().get_repo(slug)
            repos[slug] = "ok"
        except Exception as exc:  # noqa: BLE001
            code = _http_status(exc)
            if _is_transient(exc):
                repos[slug] = f"unreachable ({code}): GitHub is not answering"
                transient.append(slug)
            else:
                repos[slug] = f"unreachable ({code}): the token may not include this repo"
            unreachable.append(slug)

    # An outage looks exactly like a badly scoped token from here — same failed
    # reads, opposite remedy. Only call it a scope problem when at least one
    # repo failed for a reason that is not GitHub being down.
    if unreachable and len(unreachable) == len(repos) and len(transient) == len(unreachable):
        return {"status": "unavailable", "login": login, "repos": repos,
                "transient": True, "fatal": True,
                "detail": f"Authenticated as {login}, but GitHub answered none of "
                          f"the {len(repos)} configured repos. Treating this as an "
                          f"outage, not a credential problem."}
    if unreachable and len(unreachable) == len(repos):
        return {"status": "error", "login": login, "repos": repos,
                "detail": f"Token authenticates as {login} but reaches none of "
                          f"its {len(repos)} configured repos: {', '.join(unreachable)}.",
                "fatal": True}
    if unreachable:
        return {"status": "error", "login": login, "repos": repos,
                "detail": f"Token cannot reach: {', '.join(unreachable)}. Those "
                          "projects will go unreviewed; the rest still run.",
                "fatal": False}
    return {"status": "ok", "login": login, "repos": repos,
            "detail": f"Authenticated as {login}; all {len(repos)} configured repos reachable.",
            "fatal": False}

# ── TOOL SCHEMAS ─────────────────────────────────────────────────────────
# Keyed by name so each agent can request just the subset it's allowed to use
# via tool_specs([...]). This enforces separation of concerns at the API level:
# the Bug-Hunter never sees propose_enhancement, the Idea agent never sees
# file_issue, and the Reviewer only ever sees send_telegram_summary.

TOOL_SCHEMAS = {
    "read_trading_bot_log": {
        "name": "read_trading_bot_log",
        "description": "Read paper trading bot performance for the last N days: P&L, win rate, signal accuracy, errors.",
        "input_schema": {
            "type": "object",
            # `days` has a default, so it is intentionally NOT required.
            "properties": {"days": {"type": "integer", "default": 7}},
        },
    },
    "read_volleyball_results": {
        "name": "read_volleyball_results",
        "description": "Read volleyball CV pipeline results: ball detection accuracy, failed frames, footage processed this period.",
        "input_schema": {
            "type": "object",
            "properties": {"days": {"type": "integer", "default": 7}},
        },
    },
    "read_ufc_scraper_status": {
        "name": "read_ufc_scraper_status",
        "description": "Read UFC dashboard scraper run history: success rate, last error, data freshness.",
        "input_schema": {"type": "object", "properties": {}},
    },
    "read_overseer_status": {
        "name": "read_overseer_status",
        "description": "Read Project Overseer's OWN weekly-run health (this agent): success rate, last error, freshness. Use it to self-review and propose fixes/improvements for the overseer itself.",
        "input_schema": {"type": "object", "properties": {}},
    },
    "search_existing_issues": {
        "name": "search_existing_issues",
        "description": "Search GitHub issues in a repo to avoid filing duplicates.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "query": {"type": "string"},
            },
            "required": ["repo", "query"],
        },
    },
    "file_issue": {
        "name": "file_issue",
        "description": "File a GitHub issue for a genuine bug or failure. Only use for confirmed problems, not ideas.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "title": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["repo", "title", "body"],
        },
    },
    "propose_enhancement": {
        "name": "propose_enhancement",
        "description": (
            "Log an improvement idea for a project, even if nothing is broken. "
            "Always include effort (low/medium/high) and impact (low/medium/high) so it can be triaged later."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "title": {"type": "string"},
                "rationale": {"type": "string"},
                "effort": {"type": "string", "enum": ["low", "medium", "high"]},
                "impact": {"type": "string", "enum": ["low", "medium", "high"]},
            },
            "required": ["repo", "title", "rationale", "effort", "impact"],
        },
    },
    "send_telegram_summary": {
        "name": "send_telegram_summary",
        "description": (
            "Send the final weekly digest to Telegram. Call this exactly once, LAST, "
            "after reviewing the Bug-Hunter and Idea agent outputs. The text should be "
            "the complete digest split into 'Issues Found' and 'Top Enhancement Ideas (ranked)'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
}


def tool_specs(names):
    """The schema list for a given set of tool names — what an agent is allowed
    to call. Raises on an unknown name so a typo fails loudly at startup."""
    return [TOOL_SCHEMAS[name] for name in names]

# ── TOOL IMPLEMENTATIONS ─────────────────────────────────────────────────


def _freshness(generated_at, sla_hours):
    """Age of a status file (hours) and whether it breaches its freshness SLA.

    Returns (age_hours, is_stale). A missing or unparseable 'generated_at' yields
    (None, False): we can't prove the feed is stale, so we don't assert it — the
    project just reads as fresh/idle rather than falsely past-due (overseer #1)."""
    if not generated_at:
        return None, False
    try:
        ts = datetime.fromisoformat(str(generated_at).replace("Z", "+00:00"))
    except ValueError:
        return None, False
    age_h = round((datetime.now(timezone.utc) - ts).total_seconds() / 3600, 1)
    return age_h, age_h > sla_hours


def _read_status_file(repo_slug, path, sla_hours=FRESHNESS_SLA_DEFAULT_HOURS):
    """Read a JSON status file the project publishes to its own repo.
    Cloud-native: the overseer runs anywhere and just reads the file via the API.
    Flags staleness when the file's own 'generated_at' is older than the
    project's freshness SLA, and echoes both the age and the SLA so the digest
    can say *how* past-due it is (overseer #1)."""
    repo = _github().get_repo(repo_slug)
    try:
        content = repo.get_contents(path)
    except Exception as exc:  # noqa: BLE001 — UnknownObjectException (404) etc.
        return {"status": "error",
                "detail": f"No '{path}' in {repo_slug} yet (has the bot published it?): {exc}"}
    data = json.loads(content.decoded_content.decode("utf-8"))
    result = {"status": "ok", "source": f"{repo_slug}/{path}", "data": data,
              # Explicit idle signal so the agent doesn't have to infer it (overseer #5).
              "idle": activity_idle(data),
              # The SLA this feed is held to, echoed so the digest can cite it.
              "sla_hours": sla_hours}
    age_h, stale = _freshness(data.get("generated_at"), sla_hours)
    if age_h is not None:
        result["age_hours"] = age_h
        if stale:
            result["stale"] = True
    return result


def read_trading_bot_log(days=7):
    cfg = PROJECTS["trading_bot"]
    # Local deployment: read the SQLite trade log directly.
    if cfg["db_path"]:
        if not os.path.exists(cfg["db_path"]):
            return {"status": "error", "detail": f"TRADING_DB_PATH does not exist: {cfg['db_path']}"}
        import sqlite3
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        con = sqlite3.connect(cfg["db_path"])
        con.row_factory = sqlite3.Row
        try:
            row = con.execute(TRADING_QUERY, {"since": since}).fetchone()
        finally:
            con.close()
        return {"status": "ok", "days": days, "trades": row["trades"],
                "pnl": round(row["pnl"], 2), "win_rate": round(row["win_rate"], 3)}
    # Cloud deployment: read the status file the bot publishes to its repo.
    if cfg["repo"]:
        return _read_status_file(cfg["repo"], cfg["status_path"], cfg["sla_hours"])
    return {"status": "not_configured",
            "detail": "Set TRADING_DB_PATH (local) or have the bot publish "
                      f"{cfg['status_path']} to TRADING_REPO (cloud)."}


def read_volleyball_results(days=7):
    cfg = PROJECTS["volleyball"]
    # Local: read the pipeline's output JSON directly.
    if cfg["results_path"]:
        if not os.path.exists(cfg["results_path"]):
            return {"status": "error", "detail": f"VOLLEYBALL_RESULTS_PATH does not exist: {cfg['results_path']}"}
        with open(cfg["results_path"], encoding="utf-8") as f:
            return {"status": "ok", "days": days, "results": json.load(f)}
    # Cloud: read the status file the pipeline publishes to its repo.
    if cfg["repo"]:
        return _read_status_file(cfg["repo"], cfg["status_path"], cfg["sla_hours"])
    return {"status": "not_configured",
            "detail": "Set VOLLEYBALL_RESULTS_PATH (local) or have the pipeline publish "
                      f"{cfg['status_path']} to VOLLEYBALL_REPO (cloud)."}


def _workflow_health(repo_slug, workflow_file=None, days=7):
    """Success rate + last failure over the window, from a repo's Actions runs.
    Pass workflow_file (e.g. 'weekly-review.yml') to scope to one workflow."""
    repo = _github().get_repo(repo_slug)
    runs = repo.get_workflow(workflow_file).get_runs() if workflow_file else repo.get_workflow_runs()
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    total = success = 0
    last_error = None
    last_run_at = None  # most recent COMPLETED run, regardless of window
    for run in runs[:50]:
        if run.status != "completed":
            continue  # skip in-progress runs (e.g. this very run)
        created = run.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if last_run_at is None:
            last_run_at = created
        if created < since:
            break
        total += 1
        if run.conclusion == "success":
            success += 1
        elif last_error is None and run.conclusion in ("failure", "timed_out"):
            last_error = {"workflow": run.name, "url": run.html_url, "at": created.isoformat()}
    result = {
        "status": "ok",
        "runs_7d": total,
        "success_rate_7d": round(success / total, 3) if total else None,
        "last_error": last_error,
    }
    if last_run_at is not None:
        result["last_run_at"] = last_run_at.isoformat()
        result["last_run_age_hours"] = round((now - last_run_at).total_seconds() / 3600, 1)
    return result


def read_ufc_scraper_status():
    cfg = PROJECTS["ufc"]
    repo_slug = cfg["repo"]
    if not repo_slug:
        return {"status": "not_configured", "detail": "Set UFC_REPO (owner/name) to read its GitHub Actions runs."}
    health = _workflow_health(repo_slug)  # scrape RUN success
    # Data freshness — distinct from run success (ufc-dashboard #10): if the
    # scraper publishes a status file with a data timestamp, surface its age so
    # silently-frozen upstream data is caught even when runs keep "succeeding".
    status = _read_status_file(repo_slug, cfg["status_path"], cfg["sla_hours"])
    if status.get("status") == "ok":
        health["data"] = status["data"]
        if "age_hours" in status:
            health["data_age_hours"] = status["age_hours"]
        if "sla_hours" in status:
            health["data_sla_hours"] = status["sla_hours"]
        if status.get("stale"):
            health["data_stale"] = True
    return health


def read_overseer_status():
    """Overseer reviewing itself: health of its own weekly-review workflow."""
    repo_slug = PROJECTS["overseer"]["repo"]
    if not repo_slug:
        return {"status": "not_configured", "detail": "Set OVERSEER_REPO (owner/name) to read the overseer's own run health."}
    health = _workflow_health(repo_slug, workflow_file="weekly-review.yml")
    # A skipped weekly run must not read as healthy: if the last completed run is
    # too old, the schedule lapsed — flag it (surfaces as IDLE/yellow). (overseer #5)
    if _schedule_stale(health.get("last_run_age_hours")):
        health["schedule_stale"] = True
        health["stale"] = True
    return health


def search_existing_issues(repo, query):
    # GitHub's search API requires an `is:issue`/`is:pull-request` qualifier
    # (omitting it 422s). Iterate-and-break instead of slicing the lazy
    # PaginatedList, which can IndexError on empty results.
    q = f"repo:{repo} is:issue in:title,body {query}"
    matches = []
    for issue in _github().search_issues(q):
        matches.append({"number": issue.number, "title": issue.title,
                        "state": issue.state, "url": issue.html_url})
        if len(matches) >= 10:
            break
    return {"status": "ok", "matches": matches}


def file_issue(repo, title, body):
    if DRY_RUN:
        print("\n[DRY-RUN] file_issue would file a GitHub issue:")
        print(f"          repo : {repo}")
        print(f"          title: {title}")
        print(f"          body : {_oneline(body, 200)}\n")
        return {"status": "dry_run", "repo": repo, "title": title}
    # Stamp bugs with the same marker enhancements carry, so the delivery ledger
    # can attribute BOTH kinds back to the overseer. Without it a filed bug was
    # indistinguishable from a hand-written issue and never counted as shipped.
    issue = _github().get_repo(repo).create_issue(
        title=title, body=f"{body}\n\n---\n{OVERSEER_MARKER}")
    return {"status": "filed", "number": issue.number, "url": issue.html_url}


def propose_enhancement(repo, title, rationale, effort, impact):
    if DRY_RUN:
        print("\n[DRY-RUN] propose_enhancement would file a labelled GitHub issue:")
        print(f"          repo  : {repo}")
        print(f"          title : [enhancement] {title}")
        print(f"          effort: {effort}   impact: {impact}")
        print(f"          why   : {_oneline(rationale, 200)}\n")
        return {"status": "dry_run", "repo": repo, "title": title,
                "effort": effort, "impact": impact}
    body = f"{rationale}\n\n---\n**Effort:** {effort}  **Impact:** {impact}\n{OVERSEER_MARKER}"
    issue = _github().get_repo(repo).create_issue(title=f"[enhancement] {title}", body=body)
    # Labels may not exist in the repo; best-effort, don't fail the call over it.
    try:
        issue.add_to_labels("enhancement", f"effort:{effort}", f"impact:{impact}")
    except Exception:  # noqa: BLE001
        pass
    return {"status": "logged", "number": issue.number, "url": issue.html_url,
            "effort": effort, "impact": impact}


# Telegram caps a single message at 4096 characters.
_TELEGRAM_LIMIT = 4096


def send_telegram_summary(text):
    """Send the Reviewer's weekly digest to Telegram (Bot API). Degrades to a
    "not_configured" status when the bot token / chat id aren't set, so a run
    never fails just because Telegram isn't wired up yet."""
    if len(text) > _TELEGRAM_LIMIT:
        text = text[: _TELEGRAM_LIMIT - 1] + "…"
    if DRY_RUN:
        print("\n[DRY-RUN] send_telegram_summary would send this digest:")
        print("─" * 64)
        print(text)
        print("─" * 64 + "\n")
        return {"status": "dry_run", "chars": len(text)}
    token = _env("TELEGRAM_BOT_TOKEN")
    chat_id = _env("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        return {"status": "not_configured",
                "detail": "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to deliver the digest to Telegram."}
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text,
                          "disable_web_page_preview": True}).encode("utf-8")
    req = urllib.request.Request(url, data=payload,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        return {"status": "error", "detail": f"Telegram API {exc.code}: {_oneline(detail, 200)}"}
    except Exception as exc:  # noqa: BLE001 — network/JSON issues
        return {"status": "error", "detail": f"Telegram send failed: {exc}"}
    return {"status": "sent", "message_id": body.get("result", {}).get("message_id")}


TOOL_FUNCTIONS = {
    "read_trading_bot_log": read_trading_bot_log,
    "read_volleyball_results": read_volleyball_results,
    "read_ufc_scraper_status": read_ufc_scraper_status,
    "read_overseer_status": read_overseer_status,
    "search_existing_issues": search_existing_issues,
    "file_issue": file_issue,
    "propose_enhancement": propose_enhancement,
    "send_telegram_summary": send_telegram_summary,
}

# ── SHARED TELEMETRY READ ────────────────────────────────────────────────

def read_all_projects():
    """Read every project's status ONCE per run, for all agents to share.

    The Bug-Hunter and the Idea Agent were each opening the same four status
    files in the same run — eight reads for four files, and a whole API turn per
    agent spent doing it. Reading once and handing both agents the result cuts
    that turn out of the loop entirely.

    It also removes a subtler problem: the two agents read minutes apart, so a
    feed that published in between gave them different pictures of the same run.
    Now they reason over one snapshot.

    A tool that raises is captured as an error entry rather than propagating —
    one unreadable project must not take the review down, which is the same
    contract the tools have when an agent calls them directly.

    That capture carries RAISED_KEY. Project health distinguishes a tool that
    threw ("error") from one that returned a handled error status ("blind"), and
    when the agents called the tools themselves that difference was carried by
    the tool-result envelope. Flattening both into a dict would have silently
    reclassified every missing status file.
    """
    readings = {}
    for name in READ_TOOLS:
        try:
            readings[name] = TOOL_FUNCTIONS[name]()
        except Exception as exc:  # noqa: BLE001 — degrade, never abort
            readings[name] = {"status": "error", "detail": f"{name} failed: {exc}",
                              RAISED_KEY: True}
    return readings


def telemetry_block(readings):
    """Format the shared readings for injection into an agent's system prompt.

    Emitted as JSON per project because that is exactly what the agents used to
    receive as tool results — same shape, same field names, so their existing
    reasoning about `stale`, `idle` and `status` carries over unchanged.
    """
    if not readings:
        return "(no telemetry could be read this run)"
    lines = []
    for name, data in readings.items():
        lines.append(f"{READ_TOOLS.get(name, name)} (via {name}):")
        lines.append(f"  {json.dumps(data, default=str)}")
    return "\n".join(lines)


# ── SHARED PROMPT HELPERS ────────────────────────────────────────────────


def project_block(keys=REVIEW_PROJECTS):
    """Bulleted 'label — repo' lines for the given projects, injected into each
    agent's system prompt so it uses the correct repo slugs."""
    lines = []
    for key in keys:
        cfg = PROJECTS[key]
        repo = cfg["repo"] or "(repo not configured — do not file issues for this project)"
        lines.append(f"- {cfg['label']} — repo: {repo}")
    return "\n".join(lines)


# ── SHARED AGENT RUNTIME ─────────────────────────────────────────────────


def _oneline(text, limit=160):
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def run_agent(client, *, agent, system, tool_names, user_message, tracer):
    """Run one agent's client.messages.create tool-use loop to completion.

    Reuses the TOOL_FUNCTIONS dispatch pattern: the model may only call the
    tools whose schemas we pass (tool_specs(tool_names)), and each call is
    dispatched through the shared TOOL_FUNCTIONS table. Every thought, message,
    and tool call is streamed to the terminal + recorded by the tracer, tagged
    with this agent's name.

    Returns the agent's final text output (its structured summary) so the
    orchestrator can pass it on to the next agent.

    The model comes from model_for(agent) — see MODEL / LIGHT_MODEL above — and
    every response's token usage is reported to the tracer, so each run's
    dashboard shows what the tiering actually cost and actually saved.
    """
    tracer.set_agent(agent)
    model = model_for(agent)
    tracer.set_agent_model(agent, model, tier_for(agent))
    specs = tool_specs(tool_names)
    messages = [{"role": "user", "content": user_message}]
    final_text = ""

    for iteration in range(MAX_ITERATIONS):
        last_iteration = iteration == MAX_ITERATIONS - 1

        response = client.messages.create(
            model=model,
            max_tokens=4096,
            # Adaptive thinking with summarized display: judgment-heavy work
            # (bug vs. enhancement, effort/impact ranking, dedupe), and the
            # summaries are what the visual trace shows.
            thinking={"type": "adaptive", "display": "summarized"},
            # Cache the static prefix (tools + system + earlier turns).
            cache_control={"type": "ephemeral"},
            system=system,
            tools=[] if last_iteration else specs,
            messages=messages,
        )

        tracer.record_usage(agent, model, getattr(response, "usage", None))

        texts = []
        for block in response.content:
            if block.type == "thinking" and block.thinking:
                tracer.thinking(iteration, block.thinking)
            elif block.type == "text" and block.text.strip():
                tracer.assistant_text(iteration, block.text)
                texts.append(block.text)
        if texts:
            final_text = "\n".join(texts)

        # Preserve full content (incl. thinking + tool_use) for the next turn.
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            break

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            # Isolate tool failures: a raising tool becomes an error result the
            # agent can route around, not a crash that aborts the whole run.
            try:
                if block.name == "send_telegram_summary":
                    # Lead the digest with any deterministic staleness alert so a
                    # halted feed can't hide behind a quiet LLM summary (overseer
                    # #1 / issue #34). Prepending BEFORE the send means Telegram
                    # and the dashboard summary both carry it.
                    #
                    # The foot of the digest gets the other deterministic block:
                    # what the implementer actually landed since last week, read
                    # off the ledger. Same reasoning — a "what shipped" section
                    # that depends on an agent remembering to write it is one
                    # that will eventually go quiet without anything failing.
                    head = tracer.freshness_banner()
                    tail = delivery_banner(getattr(tracer, "ledger", None))
                    base = block.input.get("text", "")
                    parts = [p for p in (head, base, tail) if p]
                    if parts:
                        block.input["text"] = "\n\n".join(parts)
                func = TOOL_FUNCTIONS[block.name]
                result = func(**block.input)
                content = json.dumps(result)
                is_error = False
                if block.name == "send_telegram_summary":
                    # Capture the (banner-prefixed) digest for the dashboard / push.
                    tracer.set_digest(block.input.get("text", ""))
            except Exception as exc:  # noqa: BLE001
                content = f"Tool '{block.name}' failed: {exc}"
                is_error = True
            tracer.tool_call(iteration, block.name, block.input, content, is_error)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": content,
                "is_error": is_error,
            })

        messages.append({"role": "user", "content": tool_results})
    else:
        tracer.assistant_text(MAX_ITERATIONS, f"(agent '{agent}' stopped: max iterations)")

    return final_text


def load_prev_projects():
    """Per-project health from the last run, for blind-spot continuity."""
    try:
        with open(DIGEST_PATH, encoding="utf-8") as f:
            return json.load(f).get("projects", {})
    except (FileNotFoundError, ValueError):
        return {}
