"""
Project Overseer — agentic weekly review of personal automation projects.

Claude is given tools and decides on its own:
  - what to investigate
  - whether something is a bug worth filing
  - what enhancements to propose, ranked by effort vs impact
  - what to summarize back to you via Telegram

Run this on a weekly cron (GitHub Actions or local crontab). Every run also
writes a visual report (overseer_report.html) of the agent's decisions — see
tracer.py.

Configuration is via environment variables (see README.md). Anything not
configured degrades gracefully: the matching tool returns a "not_configured"
status the agent notes and works around, so the script always runs end to end.
"""

import json
import os
from datetime import datetime, timedelta, timezone

import anthropic

from tracer import RunTracer

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

# Safety bound on the agentic loop. Without this, a model that keeps calling
# tools would never terminate. On the final iteration we drop the tools so the
# model is forced to produce a closing summary instead of more tool calls.
MAX_ITERATIONS = 25

# The dashboard (docs/, served by GitHub Pages) reads this file. The weekly
# Action commits it after each run so the web app shows the latest digest.
DIGEST_PATH = os.getenv("DIGEST_PATH", "docs/digest.json")

# ── PROJECT CONFIG ───────────────────────────────────────────────────────
# Repo slug ("owner/name") + data-source location per project, from env.
# The repo slugs are injected into the system prompt so the agent files issues
# and enhancements against the correct repositories.
PROJECTS = {
    "trading_bot": {
        "label": "Paper trading bot (crypto, Coinbase Advanced Trade via CCXT)",
        "repo": os.getenv("TRADING_REPO"),
        "db_path": os.getenv("TRADING_DB_PATH"),
    },
    "volleyball": {
        "label": "Volleyball CV pipeline (ball + player tracking, coaching feedback)",
        "repo": os.getenv("VOLLEYBALL_REPO"),
        "results_path": os.getenv("VOLLEYBALL_RESULTS_PATH"),
    },
    "ufc": {
        "label": "UFC fight card dashboard (scraper + odds tracking)",
        "repo": os.getenv("UFC_REPO"),  # also the repo whose Actions runs we read
    },
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

# ── GITHUB / TELEGRAM CLIENTS ────────────────────────────────────────────

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
        from github import Github  # PyGithub
        _gh = Github(token)
    return _gh

# ── TOOL DEFINITIONS ─────────────────────────────────────────────────────

tools = [
    {
        "name": "read_trading_bot_log",
        "description": "Read paper trading bot performance for the last N days: P&L, win rate, signal accuracy, errors.",
        "input_schema": {
            "type": "object",
            # `days` has a default, so it is intentionally NOT required.
            "properties": {"days": {"type": "integer", "default": 7}},
        },
    },
    {
        "name": "read_volleyball_results",
        "description": "Read volleyball CV pipeline results: ball detection accuracy, failed frames, footage processed this period.",
        "input_schema": {
            "type": "object",
            "properties": {"days": {"type": "integer", "default": 7}},
        },
    },
    {
        "name": "read_ufc_scraper_status",
        "description": "Read UFC dashboard scraper run history: success rate, last error, data freshness.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
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
    {
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
    {
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
    {
        "name": "publish_digest",
        "description": "Publish the final weekly digest to the dashboard. Call this LAST, after all investigation is done.",
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
]

# ── TOOL IMPLEMENTATIONS ─────────────────────────────────────────────────

def read_trading_bot_log(days=7):
    db_path = PROJECTS["trading_bot"]["db_path"]
    if not db_path:
        return {"status": "not_configured", "detail": "Set TRADING_DB_PATH to your SQLite trade log."}
    if not os.path.exists(db_path):
        return {"status": "error", "detail": f"TRADING_DB_PATH does not exist: {db_path}"}
    import sqlite3
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(TRADING_QUERY, {"since": since}).fetchone()
    finally:
        con.close()
    return {
        "status": "ok",
        "days": days,
        "trades": row["trades"],
        "pnl": round(row["pnl"], 2),
        "win_rate": round(row["win_rate"], 3),
    }

def read_volleyball_results(days=7):
    path = PROJECTS["volleyball"]["results_path"]
    if not path:
        return {"status": "not_configured", "detail": "Set VOLLEYBALL_RESULTS_PATH to your pipeline's output JSON."}
    if not os.path.exists(path):
        return {"status": "error", "detail": f"VOLLEYBALL_RESULTS_PATH does not exist: {path}"}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return {"status": "ok", "days": days, "results": data}

def read_ufc_scraper_status():
    repo_slug = PROJECTS["ufc"]["repo"]
    if not repo_slug:
        return {"status": "not_configured", "detail": "Set UFC_REPO (owner/name) to read its GitHub Actions runs."}
    repo = _github().get_repo(repo_slug)
    since = datetime.now(timezone.utc) - timedelta(days=7)
    total = success = 0
    last_error = None
    for run in repo.get_workflow_runs()[:50]:
        created = run.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if created < since:
            break
        total += 1
        if run.conclusion == "success":
            success += 1
        elif last_error is None and run.conclusion in ("failure", "timed_out"):
            last_error = {"workflow": run.name, "url": run.html_url, "at": created.isoformat()}
    return {
        "status": "ok",
        "runs_7d": total,
        "success_rate_7d": round(success / total, 3) if total else None,
        "last_error": last_error,
    }

def search_existing_issues(repo, query):
    results = _github().search_issues(f"{query} repo:{repo} in:title,body")
    matches = [
        {"number": i.number, "title": i.title, "state": i.state, "url": i.html_url}
        for i in results[:10]
    ]
    return {"status": "ok", "matches": matches}

def file_issue(repo, title, body):
    issue = _github().get_repo(repo).create_issue(title=title, body=body)
    return {"status": "filed", "number": issue.number, "url": issue.html_url}

def propose_enhancement(repo, title, rationale, effort, impact):
    body = f"{rationale}\n\n---\n**Effort:** {effort}  **Impact:** {impact}\n_Filed by Project Overseer._"
    issue = _github().get_repo(repo).create_issue(title=f"[enhancement] {title}", body=body)
    # Labels may not exist in the repo; best-effort, don't fail the call over it.
    try:
        issue.add_to_labels("enhancement", f"effort:{effort}", f"impact:{impact}")
    except Exception:  # noqa: BLE001
        pass
    return {"status": "logged", "number": issue.number, "url": issue.html_url,
            "effort": effort, "impact": impact}

def publish_digest(text):
    # The text is captured by the tracer and written to docs/digest.json after
    # the loop; the GitHub Action then commits it (updating the web app) and
    # sends the push notification. Nothing to do here but acknowledge.
    return {"status": "published"}

TOOL_FUNCTIONS = {
    "read_trading_bot_log": read_trading_bot_log,
    "read_volleyball_results": read_volleyball_results,
    "read_ufc_scraper_status": read_ufc_scraper_status,
    "search_existing_issues": search_existing_issues,
    "file_issue": file_issue,
    "propose_enhancement": propose_enhancement,
    "publish_digest": publish_digest,
}

# ── SYSTEM PROMPT ────────────────────────────────────────────────────────

def build_system_prompt():
    lines = []
    for key, cfg in PROJECTS.items():
        repo = cfg["repo"] or "(repo not configured — do not file issues for this project)"
        lines.append(f"- {cfg['label']} — repo: {repo}")
    project_block = "\n".join(lines)
    return f"""You oversee three personal automation projects:
{project_block}

Use the exact repo slugs above when calling file_issue or propose_enhancement.

Each week, investigate all three. For each project:
- Check its recent logs/results using the read tools
- If something is genuinely broken, search existing issues first to avoid
  duplicates, then file_issue
- ALWAYS propose at least one enhancement per project, even if nothing is
  broken — rank it by effort vs impact honestly, don't inflate impact
- Prioritize enhancements that are low effort / high impact

If a read tool returns status "not_configured" or "error", note it briefly in
the digest and move on — don't let one project block the others, and don't file
issues for a project whose repo isn't configured.

When investigation is complete, call publish_digest with a concise
digest organized as:
  ISSUES FOUND (if any)
  ENHANCEMENT IDEAS (always at least 3, one per project minimum)

Be specific and technical. No vague suggestions like "improve accuracy" —
say what to change and why."""

# ── AGENTIC LOOP ─────────────────────────────────────────────────────────

def run_overseer():
    tracer = RunTracer()
    tracer.start()
    system_prompt = build_system_prompt()
    messages = [{"role": "user", "content": "Run this week's review."}]
    status = "completed"

    try:
        for iteration in range(MAX_ITERATIONS):
            last_iteration = iteration == MAX_ITERATIONS - 1

            response = client.messages.create(
                model="claude-opus-4-8",
                max_tokens=4096,
                # Adaptive thinking with summarized display: judgment-heavy task
                # (bug vs. enhancement, effort/impact ranking), and the summaries
                # are what the visual trace shows.
                thinking={"type": "adaptive", "display": "summarized"},
                # Cache the static prefix (tools + system + earlier turns). Pays
                # off once the cumulative prefix passes Opus 4.8's 4096-token
                # minimum, which happens after a couple of tool calls.
                cache_control={"type": "ephemeral"},
                system=system_prompt,
                tools=[] if last_iteration else tools,
                messages=messages,
            )

            # Record the agent's reasoning + any interim text for the trace.
            for block in response.content:
                if block.type == "thinking" and block.thinking:
                    tracer.thinking(iteration, block.thinking)
                elif block.type == "text" and block.text.strip():
                    tracer.assistant_text(iteration, block.text)

            # Preserve full content (incl. thinking + tool_use) for the next turn.
            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason != "tool_use":
                break

            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                func = TOOL_FUNCTIONS[block.name]
                # Isolate tool failures: a raising tool becomes an error result
                # the agent can route around, not a crash that aborts the run.
                try:
                    result = func(**block.input)
                    content = json.dumps(result)
                    is_error = False
                    if block.name == "publish_digest":
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
            status = "stopped (max iterations)"
    except Exception as exc:  # noqa: BLE001 — record, render, then re-raise
        status = f"crashed: {exc}"
        tracer.finish(status)
        tracer.write()
        tracer.write_digest(DIGEST_PATH)
        raise

    tracer.finish(status)
    tracer.write()
    tracer.write_digest(DIGEST_PATH)

if __name__ == "__main__":
    run_overseer()
