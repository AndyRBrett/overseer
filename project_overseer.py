"""
Project Overseer — agentic weekly review of personal automation projects.

Claude is given tools and decides on its own:
  - what to investigate
  - whether something is a bug worth filing
  - what enhancements to propose, ranked by effort vs impact
  - what to summarize back to you via Telegram

Run this on a weekly cron (GitHub Actions or local crontab).
"""

import anthropic
import json
import os

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

# Safety bound on the agentic loop. Without this, a model that keeps calling
# tools would never terminate. If the limit is hit, we force a final summary.
MAX_ITERATIONS = 25

# ── TOOL DEFINITIONS ─────────────────────────────────────────────────────
# These are the only actions Claude is allowed to take. It chooses the
# order and which ones to call based on what it finds.

tools = [
    {
        "name": "read_trading_bot_log",
        "description": "Read paper trading bot performance for the last N days: P&L, win rate, signal accuracy, errors.",
        "input_schema": {
            "type": "object",
            # `days` has a default, so it is intentionally NOT required — Claude
            # may omit it to accept the 7-day default.
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
        "name": "send_telegram_summary",
        "description": "Send the final weekly digest. Call this LAST, after all investigation is done.",
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
]

# ── TOOL IMPLEMENTATIONS (stubs — wire these to your real data) ─────────

def read_trading_bot_log(days=7):
    # TODO: query your SQLite trade log
    return {"pnl": 0, "win_rate": 0, "errors": [], "days": days}

def read_volleyball_results(days=7):
    # TODO: read your CV pipeline's output JSON/CSV
    return {"detection_accuracy": None, "failed_frames": 0, "clips_processed": 0, "days": days}

def read_ufc_scraper_status():
    # TODO: read your GitHub Actions run history or local log
    return {"success_rate_7d": None, "last_error": None}

def search_existing_issues(repo, query):
    # TODO: use PyGithub or requests against GitHub's REST API
    return {"matches": []}

def file_issue(repo, title, body):
    # TODO: actually create the issue via GitHub API
    print(f"[BUG FILED] {repo}: {title}")
    return {"status": "filed"}

def propose_enhancement(repo, title, rationale, effort, impact):
    # TODO: file as a labeled GitHub issue, e.g. label="enhancement"
    print(f"[ENHANCEMENT] {repo}: {title} (effort={effort}, impact={impact})")
    return {"status": "logged"}

def send_telegram_summary(text):
    # TODO: POST to Telegram Bot API
    print("\n── WEEKLY DIGEST ──\n" + text)
    return {"status": "sent"}

TOOL_FUNCTIONS = {
    "read_trading_bot_log": read_trading_bot_log,
    "read_volleyball_results": read_volleyball_results,
    "read_ufc_scraper_status": read_ufc_scraper_status,
    "search_existing_issues": search_existing_issues,
    "file_issue": file_issue,
    "propose_enhancement": propose_enhancement,
    "send_telegram_summary": send_telegram_summary,
}

# ── SYSTEM PROMPT ────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You oversee three personal automation projects:
1. A paper trading bot (crypto, Coinbase Advanced Trade via CCXT)
2. A volleyball computer vision pipeline (ball + player tracking, coaching feedback)
3. A UFC fight card dashboard (scraper + odds tracking)

Each week, investigate all three. For each project:
- Check its recent logs/results using the read tools
- If something is genuinely broken, search existing issues first to avoid
  duplicates, then file_issue
- ALWAYS propose at least one enhancement per project, even if nothing is
  broken — rank it by effort vs impact honestly, don't inflate impact
- Prioritize enhancements that are low effort / high impact

If a read tool returns an error, note it in the digest and move on — don't let
one failed project block the review of the others.

When investigation is complete, call send_telegram_summary with a concise
digest organized as:
  ISSUES FOUND (if any)
  ENHANCEMENT IDEAS (always at least 3, one per project minimum)

Be specific and technical. No vague suggestions like "improve accuracy" —
say what to change and why."""

# ── AGENTIC LOOP ─────────────────────────────────────────────────────────

def run_overseer():
    messages = [{"role": "user", "content": "Run this week's review."}]

    for iteration in range(MAX_ITERATIONS):
        # On the final allowed iteration, drop the tools so the model is forced
        # to produce a closing text response instead of requesting more work.
        last_iteration = iteration == MAX_ITERATIONS - 1

        response = client.messages.create(
            model="claude-opus-4-8",
            max_tokens=4096,
            # Adaptive thinking: this is a judgment-heavy task (bug vs.
            # enhancement, effort/impact ranking), so let Claude decide how
            # much to reason per step.
            thinking={"type": "adaptive"},
            # Cache the static prefix (tools + system + earlier turns). The
            # benefit kicks in once the cumulative prefix exceeds Opus 4.8's
            # 4096-token minimum, which it will after a couple of tool calls.
            cache_control={"type": "ephemeral"},
            system=SYSTEM_PROMPT,
            tools=[] if last_iteration else tools,
            messages=messages,
        )

        # Append the full response.content — preserves thinking + tool_use
        # blocks, which the API requires on subsequent turns.
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            break  # Claude is done — no more tools to call

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            func = TOOL_FUNCTIONS[block.name]
            # Isolate tool failures: a raising tool becomes an error result
            # Claude can route around, not a crash that aborts the whole run.
            try:
                result = func(**block.input)
                content = json.dumps(result)
                is_error = False
            except Exception as exc:  # noqa: BLE001 — surface any failure to the model
                content = f"Tool '{block.name}' failed: {exc}"
                is_error = True
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": content,
                "is_error": is_error,
            })

        messages.append({"role": "user", "content": tool_results})
    else:
        # Loop exhausted MAX_ITERATIONS without a natural stop.
        print(f"[WARN] Stopped after {MAX_ITERATIONS} iterations without completion.")

if __name__ == "__main__":
    run_overseer()
