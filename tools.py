"""
Shared tools, config, and agent runtime for the Project Overseer pipeline.

The overseer is split into four sequential agents (see orchestrator.py):

  1. Bug-Hunter  — investigates and files confirmed bugs only
  2. Fixer       — clones the repo, reproduces + fixes filed bugs, opens PRs
  3. Idea Agent  — brainstorms ranked enhancement ideas only
  4. Reviewer    — dedupes all outputs and sends one Telegram digest

Every agent script imports its tool implementations from this module so the
tool logic lives in exactly one place. This file also hosts:

  - the tool JSON schemas (per-agent subsets via `tool_specs`)
  - the `TOOL_FUNCTIONS` dispatch table reused by each agent loop
  - `run_agent`, the shared client.messages.create tool-use loop
  - the `--dry-run` switch (`set_dry_run`) that intercepts the mutating tools
    (file_issue, propose_enhancement, send_telegram_summary, plus the fixer's
    push / open_pull_request / comment_on_issue and the janitor's close_issue)
    so a run can be tested without anything hitting GitHub or Telegram

Configuration is via environment variables (see README.md). Anything not
configured degrades gracefully: the matching tool returns a "not_configured"
status the agent notes and works around, so the pipeline always runs end to end.
"""

import json
import os
import secrets
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from tracer import RunTracer, activity_idle

# Two model tiers to keep the weekly cost down. The Fixer gets the heavy model
# (writing correct code + repro tests is the judgment-heavy stage); the
# investigate/summarize agents (Bug-Hunter, Idea, Reviewer, Janitor) run on the
# light model, which is roughly 5x cheaper per token.
MODEL = os.getenv("OVERSEER_MODEL", "claude-opus-4-8")
LIGHT_MODEL = os.getenv("OVERSEER_LIGHT_MODEL", "claude-sonnet-5")

# Per-response output budget. Adaptive thinking spends from this too, so it
# must be generous: at 4096 a long think could swallow the whole budget and
# truncate the response (see the max_tokens handling in run_agent).
MAX_TOKENS = int(os.getenv("OVERSEER_MAX_TOKENS", "16384"))

# Safety bound on each agent's tool-use loop. Without this, a model that keeps
# calling tools would never terminate. On the final iteration we drop the tools
# so the model is forced to produce a closing summary instead of more tool calls.
MAX_ITERATIONS = 25

# The overseer runs weekly; if its own last completed run is older than this, the
# schedule likely lapsed — a skipped run must not read as healthy (overseer #5).
SCHEDULE_STALE_HOURS = 192  # 8 days


def _schedule_stale(age_hours):
    return age_hours is not None and age_hours > SCHEDULE_STALE_HOURS


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
def _env(name, default=None):
    """Read an env var, trimming stray whitespace/newlines — e.g. a repo slug
    pasted into a GitHub Variable with a trailing CRLF (see overseer #3)."""
    v = os.getenv(name, default)
    return v.strip() if isinstance(v, str) else v


# ── PER-PROJECT FRESHNESS SLA (overseer #1) ──────────────────────────────
# How old a project's published status file may get before it's flagged STALE.
# A silently-halted feed must trip an alert, not read as healthy: issue #34 was
# the crypto status file sitting ~153h stale while the digest stayed quiet.
# Each project sets its OWN SLA (a daily bot's data goes stale far sooner than a
# weekly pipeline's) via env, defaulting to 48h — two missed daily runs. The
# shared default is itself tunable via FRESHNESS_SLA_HOURS.
FRESHNESS_SLA_DEFAULT_HOURS = int(os.getenv("FRESHNESS_SLA_HOURS", "48"))


def _sla_hours(env_name):
    """A project's freshness SLA in hours, from its own env var, falling back to
    the shared default. A blank or non-numeric value falls back rather than
    erroring, so a fat-fingered GitHub Variable can't break the whole run."""
    raw = _env(env_name)
    try:
        return int(raw) if raw else FRESHNESS_SLA_DEFAULT_HOURS
    except ValueError:
        return FRESHNESS_SLA_DEFAULT_HOURS


PROJECTS = {
    "trading_bot": {
        "label": "Crypto trading bot (Coinbase Advanced Trade via CCXT, daily cloud runs)",
        "repo": _env("TRADING_REPO"),
        "db_path": _env("TRADING_DB_PATH"),              # local deployments
        "status_path": _env("TRADING_STATUS_PATH", "overseer-status.json"),  # cloud: file the bot publishes
        "sla_hours": _sla_hours("TRADING_SLA_HOURS"),    # daily bot → 48h default
    },
    # Internal key + env vars stay "volleyball"/VOLLEYBALL_* (the deployment's
    # GitHub Variables are wired to them); only the human-facing name changed
    # after the repo rebranded from Volleyball to coachvision (martial arts).
    "volleyball": {
        "label": "coachvision — martial-arts CV pipeline (technique tracking + coaching feedback)",
        "repo": _env("VOLLEYBALL_REPO"),
        "results_path": _env("VOLLEYBALL_RESULTS_PATH"),                       # local
        "status_path": _env("VOLLEYBALL_STATUS_PATH", "overseer-status.json"),  # cloud
        "sla_hours": _sla_hours("VOLLEYBALL_SLA_HOURS"),
    },
    "ufc": {
        "label": "UFC fight card dashboard (scraper + odds tracking)",
        "repo": _env("UFC_REPO"),  # repo whose Actions runs + status file we read
        "status_path": _env("UFC_STATUS_PATH", "overseer-status.json"),
        "sla_hours": _sla_hours("UFC_SLA_HOURS"),
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
    """
    if not (os.getenv("OVERSEER_GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN")):
        return {"status": "not_configured",
                "detail": "No OVERSEER_GITHUB_TOKEN — every GitHub-backed tool "
                          "will report not_configured.",
                "fatal": False}

    try:
        login = _github().get_user().login
    except Exception as exc:  # noqa: BLE001 — auth failure surfaces as 401
        status = _http_status(exc)
        hint = ("token is expired or revoked — regenerate it and update the "
                "OVERSEER_GITHUB_TOKEN repo secret") if status == 401 else str(exc)
        return {"status": "error", "detail": f"GitHub auth failed ({status}): {hint}",
                "fatal": True}

    # Per-repo reach. A fine-grained PAT scoped to only some of the projects
    # authenticates fine but 404s on the ones it was not granted.
    repos, unreachable = {}, []
    for key in REVIEW_PROJECTS:
        slug = PROJECTS[key].get("repo")
        if not slug:
            continue
        try:
            _github().get_repo(slug)
            repos[slug] = "ok"
        except Exception as exc:  # noqa: BLE001
            code = _http_status(exc)
            repos[slug] = f"unreachable ({code}): the token may not include this repo"
            unreachable.append(slug)

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
    "list_open_issues": {
        "name": "list_open_issues",
        "description": "List the open GitHub issues in a project repo (number, title, body, labels). Use this to pick which filed bugs to attempt a fix for.",
        "input_schema": {
            "type": "object",
            "properties": {"repo": {"type": "string"}},
            "required": ["repo"],
        },
    },
    "setup_fix_workspace": {
        "name": "setup_fix_workspace",
        "description": (
            "Clone a project repo into a scratch workspace and create a fresh fix "
            "branch (overseer/fix-<issue>) off the default branch. Must be called "
            "before any other workspace tool for that repo. Re-calling replaces the "
            "workspace, discarding uncommitted work."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "issue_number": {"type": "integer"},
            },
            "required": ["repo", "issue_number"],
        },
    },
    "run_in_workspace": {
        "name": "run_in_workspace",
        "description": (
            "Run a shell command inside the repo's workspace clone (tests, grep, "
            "git log/blame, pip install, reproduction scripts). Returns exit code "
            "and combined stdout+stderr, truncated if long."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "command": {"type": "string"},
                "timeout": {"type": "integer",
                            "description": "Seconds before the command is killed (default 180, max 600)."},
            },
            "required": ["repo", "command"],
        },
    },
    "read_workspace_file": {
        "name": "read_workspace_file",
        "description": "Read a file from the repo's workspace clone (path relative to the repo root).",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "path": {"type": "string"},
            },
            "required": ["repo", "path"],
        },
    },
    "write_workspace_file": {
        "name": "write_workspace_file",
        "description": "Write (create or fully overwrite) a file in the repo's workspace clone. Path is relative to the repo root.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["repo", "path", "content"],
        },
    },
    "commit_and_push": {
        "name": "commit_and_push",
        "description": (
            "Commit all workspace changes and push the fix branch to origin. "
            "Refuses to commit on the default branch — only the overseer/fix-* "
            "branch created by setup_fix_workspace can be pushed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "message": {"type": "string", "description": "Commit message."},
            },
            "required": ["repo", "message"],
        },
    },
    "open_pull_request": {
        "name": "open_pull_request",
        "description": (
            "Open a pull request from the pushed fix branch into the default "
            "branch. The body should state the root cause, the fix, and the test "
            "evidence; 'Fixes #<issue>' is appended automatically so the issue "
            "closes on merge."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "title": {"type": "string"},
                "body": {"type": "string"},
                "issue_number": {"type": "integer"},
            },
            "required": ["repo", "title", "body", "issue_number"],
        },
    },
    "comment_on_issue": {
        "name": "comment_on_issue",
        "description": (
            "Post a comment on an existing GitHub issue. Use this when escalating: "
            "record what you investigated, the root cause evidence, and the "
            "decision the owner needs to make — so the issue is actionable even "
            "without a PR."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "issue_number": {"type": "integer"},
                "comment": {"type": "string"},
            },
            "required": ["repo", "issue_number", "comment"],
        },
    },
    "close_issue": {
        "name": "close_issue",
        "description": (
            "Close a GitHub issue as completed, posting an explanatory comment "
            "first. Only close an issue when you can cite the specific commit "
            "or merged PR that resolved it — the comment must contain that "
            "evidence so the owner can spot-check the close."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "issue_number": {"type": "integer"},
                "comment": {"type": "string",
                            "description": "Why this is being closed, citing the commit SHA or PR that implemented it."},
            },
            "required": ["repo", "issue_number", "comment"],
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


# ── FIXER WORKSPACE TOOLS ────────────────────────────────────────────────
# The Fixer agent works in a real clone of the target repo: it investigates,
# writes a reproducing test, fixes, runs the tests, then pushes a fix branch and
# opens a PR. Two hard guarantees are enforced HERE, not just in the prompt:
#   - only repos configured in PROJECTS can be touched (setup_fix_workspace)
#   - the default branch can never be committed to or pushed (commit_and_push)

FIX_BRANCH_PREFIX = "overseer/fix-"
FIXER_COMMAND_TIMEOUT = int(os.getenv("FIXER_COMMAND_TIMEOUT", "180"))
_FIXER_OUTPUT_LIMIT = 10_000   # chars of command output returned to the agent
_WORKSPACE_FILE_LIMIT = 50_000

# How many PRs the Fixer may open per run. Enforced HERE (open_pull_request
# refuses past the budget), not just in the agent prompt.
FIXER_MAX_FIXES = int(os.getenv("FIXER_MAX_FIXES", "2"))
_fix_prs_opened = 0


def reset_fix_run():
    """Reset the per-run PR budget. Called at the start of each pipeline run."""
    global _fix_prs_opened
    _fix_prs_opened = 0

# repo slug -> {"dir", "branch", "default_branch", "committed", "pushed"}
_workspaces = {}


def configured_repos():
    """Repo slugs the fixer is allowed to touch — exactly the configured projects."""
    return {cfg["repo"] for cfg in PROJECTS.values() if cfg.get("repo")}


def _clone_url(repo_slug):
    """Token-authenticated HTTPS clone URL. Tests monkeypatch this to a local path."""
    token = os.getenv("OVERSEER_GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN")
    if not token:
        raise RuntimeError(
            "No GitHub token for cloning. OVERSEER_GITHUB_TOKEN needs Contents "
            "and Pull requests read/write on the project repos."
        )
    return f"https://x-access-token:{token}@github.com/{repo_slug}.git"


def _scrub_secrets(text):
    """Git prints the remote URL (token included) in clone/push errors, and the
    agent may run `git remote -v`. Never let the token reach the model or logs."""
    for var in ("OVERSEER_GITHUB_TOKEN", "GITHUB_TOKEN"):
        token = os.getenv(var)
        if token:
            text = text.replace(token, "***")
    return text


def _git(workdir, *args, timeout=60):
    proc = subprocess.run(["git", *args], cwd=workdir,
                          capture_output=True, text=True, timeout=timeout)
    return proc.returncode, _scrub_secrets((proc.stdout + proc.stderr).strip())


def _workspace(repo):
    ws = _workspaces.get(repo)
    if ws is None:
        raise RuntimeError(f"No workspace for {repo} — call setup_fix_workspace first.")
    return ws


def _ws_file(ws, path):
    """Resolve a repo-relative path, refusing anything that escapes the clone."""
    root = os.path.realpath(ws["dir"])
    full = os.path.realpath(os.path.join(root, path))
    if full != root and not full.startswith(root + os.sep):
        raise ValueError(f"Path escapes the workspace: {path}")
    return full


def list_open_issues(repo):
    if repo not in configured_repos():
        return {"status": "error",
                "detail": f"'{repo}' is not a configured project repo — refusing."}
    gh_repo = _github().get_repo(repo)
    issues = []
    for issue in gh_repo.get_issues(state="open"):
        if issue.pull_request is not None:
            continue  # the Issues API also returns PRs; the fixer wants issues only
        issues.append({
            "number": issue.number,
            "title": issue.title,
            "body": _oneline(issue.body or "", 2000),
            "labels": [l.name for l in issue.labels],
            "url": issue.html_url,
            "created_at": issue.created_at.isoformat(),
        })
        if len(issues) >= 20:
            break
    # Open overseer/fix-* PRs from previous runs, so the fixer can skip issues
    # that already have a fix in flight (the branch name carries the issue
    # number: overseer/fix-<issue>-<suffix>). Without this, cross-run dedupe
    # would rely on data the agent can't see.
    fix_prs = []
    for pr in gh_repo.get_pulls(state="open"):
        if pr.head.ref.startswith(FIX_BRANCH_PREFIX):
            fix_prs.append({"number": pr.number, "branch": pr.head.ref,
                            "title": pr.title, "url": pr.html_url})
    return {"status": "ok", "repo": repo, "open_issues": issues,
            "open_fix_prs": fix_prs}


# A transport git cannot resolve, so `git push origin ...` fails on the URL
# before any credential lookup happens. Deliberately not an https:// URL: those
# can be rewritten by insteadOf rules or authenticated by a helper.
BLOCKED_PUSH_URL = "overseer-push-blocked://refusing-shell-push"


def setup_fix_workspace(repo, issue_number):
    if repo not in configured_repos():
        return {"status": "error",
                "detail": f"'{repo}' is not a configured project repo — refusing to clone it."}
    old = _workspaces.pop(repo, None)
    if old:
        shutil.rmtree(old["dir"], ignore_errors=True)
    workdir = tempfile.mkdtemp(prefix="overseer-fix-" + repo.replace("/", "-") + "-")
    code, out = _git(workdir, "clone", _clone_url(repo), ".", timeout=300)
    if code != 0:
        shutil.rmtree(workdir, ignore_errors=True)
        return {"status": "error", "detail": f"git clone failed: {out}"}
    _, default_branch = _git(workdir, "rev-parse", "--abbrev-ref", "HEAD")
    # Random suffix so a retry (or a stale branch from a crashed run) never
    # collides with an existing remote branch.
    branch = f"{FIX_BRANCH_PREFIX}{issue_number}-{secrets.token_hex(3)}"
    code, out = _git(workdir, "checkout", "-b", branch)
    if code != 0:
        shutil.rmtree(workdir, ignore_errors=True)
        return {"status": "error", "detail": f"branch creation failed: {out}"}
    _git(workdir, "config", "user.name", "overseer-bot")
    _git(workdir, "config", "user.email", "overseer-bot@users.noreply.github.com")
    # Block shell pushes POSITIVELY, not by assuming the environment is
    # credential-free.
    #
    # run_in_workspace lets the agent run arbitrary git, so a shell
    # `git push origin main` must not be able to bypass commit_and_push's
    # default-branch guard. Stripping the token from the remote URL is necessary
    # but NOT sufficient: it only works if nothing else supplies credentials. Any
    # ambient credential helper, an `http.extraheader`, or a proxy with
    # `url.insteadOf` rewriting puts them back — and then the push succeeds.
    #
    # That is not hypothetical. Running this repo's own test suite inside a
    # sandbox whose git proxy authenticates github.com pushed a live branch to
    # AndyRBrett/ufc-dashboard, while the test asserting "the shell cannot push"
    # reported failure only because the push had worked.
    #
    # So: point pushurl at a transport that cannot exist, and clear inherited
    # credential helpers. `git push origin ...` now fails on the URL itself,
    # before any credential is consulted. commit_and_push sets a real pushurl for
    # the duration of its own guarded push and restores this afterwards.
    _git(workdir, "remote", "set-url", "origin", f"https://github.com/{repo}.git")
    _git(workdir, "remote", "set-url", "--push", "origin", BLOCKED_PUSH_URL)
    _git(workdir, "config", "--local", "credential.helper", "")
    _git(workdir, "config", "--local", "http.https://github.com/.extraheader", "")
    _, files = _git(workdir, "ls-files")
    file_list = files.splitlines()
    _workspaces[repo] = {"dir": workdir, "branch": branch,
                         "default_branch": default_branch,
                         "committed": False, "pushed": False}
    return {"status": "ok", "repo": repo, "branch": branch,
            "default_branch": default_branch,
            "files": file_list[:200],
            "file_count": len(file_list)}


def run_in_workspace(repo, command, timeout=None):
    ws = _workspace(repo)
    timeout = min(int(timeout or FIXER_COMMAND_TIMEOUT), 600)
    # No .pyc files in the clone: stale bytecode can mask a just-written fix
    # (same size + same mtime second reuses the old pyc), and __pycache__ would
    # otherwise be swept into the fix commit by `git add -A`. And never let git
    # prompt for credentials — there is no terminal, it would hang until timeout.
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "GIT_TERMINAL_PROMPT": "0"}
    try:
        proc = subprocess.run(command, shell=True, cwd=ws["dir"], env=env,
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"status": "timeout",
                "detail": f"Command killed after {timeout}s: {_oneline(command)}"}
    out = _scrub_secrets((proc.stdout or "") + (proc.stderr or ""))
    truncated = len(out) > _FIXER_OUTPUT_LIMIT
    if truncated:
        out = out[:_FIXER_OUTPUT_LIMIT] + "\n… (output truncated)"
    return {"status": "ok", "exit_code": proc.returncode,
            "output": out, "truncated": truncated}


def read_workspace_file(repo, path):
    ws = _workspace(repo)
    try:
        full = _ws_file(ws, path)
    except ValueError as exc:
        return {"status": "error", "detail": str(exc)}
    if not os.path.isfile(full):
        return {"status": "error", "detail": f"No such file in workspace: {path}"}
    with open(full, encoding="utf-8", errors="replace") as f:
        content = f.read(_WORKSPACE_FILE_LIMIT + 1)
    truncated = len(content) > _WORKSPACE_FILE_LIMIT
    if truncated:
        content = content[:_WORKSPACE_FILE_LIMIT]
    return {"status": "ok", "path": path, "content": content, "truncated": truncated}


def write_workspace_file(repo, path, content):
    ws = _workspace(repo)
    try:
        full = _ws_file(ws, path)
    except ValueError as exc:
        return {"status": "error", "detail": str(exc)}
    os.makedirs(os.path.dirname(full) or ws["dir"], exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(content)
    return {"status": "ok", "path": path, "bytes": len(content.encode("utf-8"))}


def commit_and_push(repo, message):
    ws = _workspace(repo)
    code, current = _git(ws["dir"], "rev-parse", "--abbrev-ref", "HEAD")
    if code != 0:
        return {"status": "error", "detail": f"could not read current branch: {current}"}
    # Hard guarantee: never commit to (let alone push) the default branch. Even
    # if the agent checked out another branch via run_in_workspace, only the
    # overseer/fix-* branch this workspace was created with is accepted.
    if current == ws["default_branch"] or not current.startswith(FIX_BRANCH_PREFIX):
        return {"status": "refused",
                "detail": f"On branch '{current}' — commits are only allowed on the "
                          f"'{ws['branch']}' fix branch, never '{ws['default_branch']}'."}
    _git(ws["dir"], "add", "-A")
    code, out = _git(ws["dir"], "commit", "-m", message)
    if code != 0:
        return {"status": "error", "detail": f"git commit failed: {out}"}
    ws["committed"] = True
    if DRY_RUN:
        print("\n[DRY-RUN] commit_and_push committed locally but would push:")
        print(f"          repo   : {repo}")
        print(f"          branch : {current}")
        print(f"          message: {_oneline(message, 200)}\n")
        return {"status": "dry_run", "branch": current,
                "detail": "Committed locally; push to origin skipped (dry run)."}
    # Pushes are blocked at the pushurl (see setup_fix_workspace). Open the gate
    # for this one guarded push only, and close it again whatever happens — a
    # failed push must not leave the workspace able to push from the shell.
    _git(ws["dir"], "remote", "set-url", "--push", "origin", _clone_url(repo))
    try:
        code, out = _git(ws["dir"], "push", "-u", "origin", current, timeout=120)
    finally:
        _git(ws["dir"], "remote", "set-url", "--push", "origin", BLOCKED_PUSH_URL)
    if code != 0:
        return {"status": "error", "detail": f"git push failed: {out}"}
    ws["pushed"] = True
    return {"status": "pushed", "repo": repo, "branch": current}


def open_pull_request(repo, title, body, issue_number):
    global _fix_prs_opened
    ws = _workspace(repo)
    # The per-run budget is enforced here, not just in the prompt — a run with
    # FIXER_MAX_FIXES=1 opens at most one PR no matter what the agent decides.
    if _fix_prs_opened >= FIXER_MAX_FIXES:
        return {"status": "refused",
                "detail": f"Fix budget spent: {FIXER_MAX_FIXES} PR(s) already opened "
                          "this run (FIXER_MAX_FIXES). Escalate remaining issues "
                          "with comment_on_issue instead."}
    body = f"{body}\n\nFixes #{issue_number}\n\n_PR opened by Project Overseer._"
    if DRY_RUN:
        if not ws["committed"]:
            return {"status": "error",
                    "detail": "Nothing committed yet — call commit_and_push first."}
        print("\n[DRY-RUN] open_pull_request would open a PR:")
        print(f"          repo : {repo}")
        print(f"          head : {ws['branch']} -> {ws['default_branch']}")
        print(f"          title: {title}")
        print(f"          body : {_oneline(body, 300)}\n")
        _fix_prs_opened += 1
        return {"status": "dry_run", "repo": repo, "title": title,
                "branch": ws["branch"], "issue_number": issue_number}
    if not ws["pushed"]:
        return {"status": "error",
                "detail": "Branch not pushed yet — call commit_and_push first."}
    pr = _github().get_repo(repo).create_pull(
        title=title, body=body, head=ws["branch"], base=ws["default_branch"])
    _fix_prs_opened += 1
    return {"status": "opened", "number": pr.number, "url": pr.html_url,
            "branch": ws["branch"]}


def comment_on_issue(repo, issue_number, comment):
    if repo not in configured_repos():
        return {"status": "error",
                "detail": f"'{repo}' is not a configured project repo — refusing."}
    if DRY_RUN:
        print("\n[DRY-RUN] comment_on_issue would comment:")
        print(f"          repo : {repo} issue #{issue_number}")
        print(f"          text : {_oneline(comment, 300)}\n")
        return {"status": "dry_run", "repo": repo, "issue_number": issue_number}
    issue = _github().get_repo(repo).get_issue(issue_number)
    c = issue.create_comment(comment)
    return {"status": "commented", "issue_number": issue_number, "url": c.html_url}


def close_issue(repo, issue_number, comment):
    """Comment-then-close, atomically from the agent's point of view: every
    close carries its evidence. Reopening is one click, so this is the mildest
    mutating tool — but it still respects the repo allowlist and dry-run."""
    if repo not in configured_repos():
        return {"status": "error",
                "detail": f"'{repo}' is not a configured project repo — refusing."}
    if DRY_RUN:
        print("\n[DRY-RUN] close_issue would close an issue as completed:")
        print(f"          repo : {repo} issue #{issue_number}")
        print(f"          why  : {_oneline(comment, 300)}\n")
        return {"status": "dry_run", "repo": repo, "issue_number": issue_number}
    issue = _github().get_repo(repo).get_issue(issue_number)
    issue.create_comment(comment)
    issue.edit(state="closed", state_reason="completed")
    return {"status": "closed", "issue_number": issue_number, "url": issue.html_url}


def cleanup_workspaces():
    """Remove all fixer clones and reset the per-run PR budget. Called by the
    orchestrator after the run (and by tests between cases)."""
    for repo in list(_workspaces):
        shutil.rmtree(_workspaces.pop(repo)["dir"], ignore_errors=True)
    reset_fix_run()


TOOL_FUNCTIONS = {
    "read_trading_bot_log": read_trading_bot_log,
    "read_volleyball_results": read_volleyball_results,
    "read_ufc_scraper_status": read_ufc_scraper_status,
    "read_overseer_status": read_overseer_status,
    "search_existing_issues": search_existing_issues,
    "file_issue": file_issue,
    "propose_enhancement": propose_enhancement,
    "list_open_issues": list_open_issues,
    "setup_fix_workspace": setup_fix_workspace,
    "run_in_workspace": run_in_workspace,
    "read_workspace_file": read_workspace_file,
    "write_workspace_file": write_workspace_file,
    "commit_and_push": commit_and_push,
    "open_pull_request": open_pull_request,
    "comment_on_issue": comment_on_issue,
    "close_issue": close_issue,
    "send_telegram_summary": send_telegram_summary,
}

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


def run_agent(client, *, agent, system, tool_names, user_message, tracer,
              max_iterations=MAX_ITERATIONS, model=None):
    """Run one agent's client.messages.create tool-use loop to completion.

    Reuses the TOOL_FUNCTIONS dispatch pattern: the model may only call the
    tools whose schemas we pass (tool_specs(tool_names)), and each call is
    dispatched through the shared TOOL_FUNCTIONS table. Every thought, message,
    and tool call is streamed to the terminal + recorded by the tracer, tagged
    with this agent's name.

    Returns the agent's final text output (its structured summary) so the
    orchestrator can pass it on to the next agent.
    """
    tracer.set_agent(agent)
    model = model or MODEL
    specs = tool_specs(tool_names)
    messages = [{"role": "user", "content": user_message}]
    final_text = ""

    for iteration in range(max_iterations):
        last_iteration = iteration == max_iterations - 1

        response = client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
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

        tool_uses = [b for b in response.content if b.type == "tool_use"]

        if response.stop_reason == "max_tokens" and not tool_uses:
            # Truncated mid-thought with nothing runnable. Breaking here would
            # silently end the agent mid-task — live validation caught the
            # Janitor verifying issues and then "completing" without acting.
            # Nudge it to pick up where it left off instead.
            tracer.assistant_text(iteration, "(response hit the output token limit — asking the agent to continue)")
            messages.append({"role": "user", "content": (
                "Your previous response was cut off by the output token limit. "
                "Continue exactly where you left off; if you were about to call "
                "a tool, issue that tool call now.")})
            continue

        if not tool_uses:
            break

        tool_results = []
        for block in tool_uses:
            # Isolate tool failures: a raising tool becomes an error result the
            # agent can route around, not a crash that aborts the whole run.
            try:
                if block.name == "send_telegram_summary":
                    # Lead the digest with any deterministic staleness alert so a
                    # halted feed can't hide behind a quiet LLM summary (overseer
                    # #1 / issue #34). Prepending BEFORE the send means Telegram
                    # and the dashboard summary both carry it.
                    banner = tracer.freshness_banner()
                    if banner:
                        base = block.input.get("text", "")
                        block.input["text"] = f"{banner}\n\n{base}" if base else banner
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
        tracer.assistant_text(max_iterations, f"(agent '{agent}' stopped: max iterations)")

    return final_text


def load_prev_projects():
    """Per-project health from the last run, for blind-spot continuity."""
    try:
        with open(DIGEST_PATH, encoding="utf-8") as f:
            return json.load(f).get("projects", {})
    except (FileNotFoundError, ValueError):
        return {}
