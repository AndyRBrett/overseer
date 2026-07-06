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
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from tracer import RunTracer, activity_idle

# All three agents run on the same model unless overridden.
MODEL = os.getenv("OVERSEER_MODEL", "claude-opus-4-8")

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


PROJECTS = {
    "trading_bot": {
        "label": "Crypto trading bot (Coinbase Advanced Trade via CCXT, daily cloud runs)",
        "repo": _env("TRADING_REPO"),
        "db_path": _env("TRADING_DB_PATH"),              # local deployments
        "status_path": _env("TRADING_STATUS_PATH", "overseer-status.json"),  # cloud: file the bot publishes
    },
    # Internal key + env vars stay "volleyball"/VOLLEYBALL_* (the deployment's
    # GitHub Variables are wired to them); only the human-facing name changed
    # after the repo rebranded from Volleyball to coachvision (martial arts).
    "volleyball": {
        "label": "coachvision — martial-arts CV pipeline (technique tracking + coaching feedback)",
        "repo": _env("VOLLEYBALL_REPO"),
        "results_path": _env("VOLLEYBALL_RESULTS_PATH"),                       # local
        "status_path": _env("VOLLEYBALL_STATUS_PATH", "overseer-status.json"),  # cloud
    },
    "ufc": {
        "label": "UFC fight card dashboard (scraper + odds tracking)",
        "repo": _env("UFC_REPO"),  # repo whose Actions runs + status file we read
        "status_path": _env("UFC_STATUS_PATH", "overseer-status.json"),
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


def _read_status_file(repo_slug, path):
    """Read a JSON status file the project publishes to its own repo.
    Cloud-native: the overseer runs anywhere and just reads the file via the API.
    Flags staleness from the file's own 'generated_at' if present."""
    repo = _github().get_repo(repo_slug)
    try:
        content = repo.get_contents(path)
    except Exception as exc:  # noqa: BLE001 — UnknownObjectException (404) etc.
        return {"status": "error",
                "detail": f"No '{path}' in {repo_slug} yet (has the bot published it?): {exc}"}
    data = json.loads(content.decoded_content.decode("utf-8"))
    result = {"status": "ok", "source": f"{repo_slug}/{path}", "data": data,
              # Explicit idle signal so the agent doesn't have to infer it (overseer #5).
              "idle": activity_idle(data)}
    generated = data.get("generated_at")
    if generated:
        try:
            ts = datetime.fromisoformat(generated.replace("Z", "+00:00"))
            age_h = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
            result["age_hours"] = round(age_h, 1)
            if age_h > 48:  # daily bot → anything older than 2 days is stale
                result["stale"] = True
        except ValueError:
            pass
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
        return _read_status_file(cfg["repo"], cfg["status_path"])
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
        return _read_status_file(cfg["repo"], cfg["status_path"])
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
    status = _read_status_file(repo_slug, cfg["status_path"])
    if status.get("status") == "ok":
        health["data"] = status["data"]
        if "age_hours" in status:
            health["data_age_hours"] = status["age_hours"]
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
    issue = _github().get_repo(repo).create_issue(title=title, body=body)
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
    body = f"{rationale}\n\n---\n**Effort:** {effort}  **Impact:** {impact}\n_Filed by Project Overseer._"
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
    """
    tracer.set_agent(agent)
    specs = tool_specs(tool_names)
    messages = [{"role": "user", "content": user_message}]
    final_text = ""

    for iteration in range(MAX_ITERATIONS):
        last_iteration = iteration == MAX_ITERATIONS - 1

        response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            # Adaptive thinking with summarized display: judgment-heavy work
            # (bug vs. enhancement, effort/impact ranking, dedupe), and the
            # summaries are what the visual trace shows.
            thinking={"type": "adaptive", "display": "summarized"},
            # Cache the static prefix (tools + system + earlier turns). 1h TTL
            # (vs the 5m default) so the cache survives slow tool round-trips
            # within an agent's loop; it can't span the weekly run gap regardless.
            cache_control={"type": "ephemeral", "ttl": "1h"},
            system=system,
            tools=[] if last_iteration else specs,
            messages=messages,
        )

        # Record token usage (incl. cache write/read) for this call so cache
        # behaviour can be reviewed per agent per iteration — see tracer.usage.
        tracer.usage(iteration, response.usage)

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
                func = TOOL_FUNCTIONS[block.name]
                result = func(**block.input)
                content = json.dumps(result)
                is_error = False
                if block.name == "send_telegram_summary":
                    # Capture the digest text for the dashboard / push notification.
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
