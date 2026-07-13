"""
RunTracer — captures the overseer agent's decision process and renders it
three ways:

  1. A live, readable trace to stdout (shows up in `python` runs and CI logs)
  2. A JSONL event log (overseer_run.jsonl) for machine replay / debugging
  3. A self-contained HTML timeline (overseer_report.html) you can open in a
     browser to see, step by step, what the agent thought, which tools it
     chose, what came back, and where anything failed.

No third-party dependencies — pure stdlib so it runs anywhere.
"""

from __future__ import annotations

import html
import json
import os
import time
from datetime import datetime, timezone

# How many consecutive idle/blind cycles a project may sit at before the rollup
# "nudges" it — promotes it from a quiet badge to an explicit call-out at the top
# of the dashboard (overseer self-review: idle-detection + rollup). Configurable
# so a noisier/quieter cadence can tune it without code changes; the threshold is
# also emitted into the digest so the dashboard highlights the same projects.
NUDGE_CYCLES = max(1, int(os.getenv("OVERSEER_NUDGE_CYCLES", "2")))

# Maps each tool to a visual category: a label + colour used in the timeline.
# This is what turns a flat log into something you can read at a glance —
# "investigate" steps look different from a filed bug or a proposed idea.
TOOL_CATEGORY = {
    "read_trading_bot_log": ("investigate", "#2563eb"),
    "read_volleyball_results": ("investigate", "#2563eb"),
    "read_ufc_scraper_status": ("investigate", "#2563eb"),
    "read_overseer_status": ("investigate", "#2563eb"),
    "search_existing_issues": ("search", "#6b7280"),
    "file_issue": ("bug", "#dc2626"),
    "propose_enhancement": ("idea", "#d97706"),
    "send_telegram_summary": ("digest", "#059669"),
}
DEFAULT_CATEGORY = ("tool", "#6b7280")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


class RunTracer:
    def __init__(self, jsonl_path="overseer_run.jsonl", html_path="overseer_report.html"):
        self.events: list[dict] = []
        self.jsonl_path = jsonl_path
        self.html_path = html_path
        self._t0 = time.monotonic()
        self.counts = {"tools": 0, "errors": 0, "issues": 0, "enhancements": 0}
        self.digest_text = None
        self.status = "running"
        self.read_tools = {}          # {tool_name: project_label} — set by caller
        self.prev_projects = {}       # last run's per-project health, for continuity
        self.agent = None             # current pipeline agent, for labelling output

    # ── recording ────────────────────────────────────────────────────────

    def _record(self, kind: str, **data) -> None:
        ev = {"ts": _now(), "elapsed_s": round(time.monotonic() - self._t0, 2), "kind": kind, **data}
        if self.agent:
            ev["agent"] = self.agent
        self.events.append(ev)

    def _prefix(self) -> str:
        return f"({self.agent}) " if self.agent else ""

    def start(self) -> None:
        self._record("run_start")
        print(f"\n[{_now()}] ── overseer run started ──")

    def set_agent(self, name: str) -> None:
        """Switch the active pipeline agent. Prints a banner so the terminal
        output (and CI log) clearly shows which agent is reasoning/acting."""
        self.agent = name
        self._record("agent_start", agent=name)
        print(f"\n[{_now()}] ━━━━━━ AGENT: {name} ━━━━━━")

    def thinking(self, iteration: int, text: str) -> None:
        self._record("thinking", iteration=iteration, text=text)
        print(f"[{_now()}] [THINK · turn {iteration}] {self._prefix()}{_oneline(text)}")

    def assistant_text(self, iteration: int, text: str) -> None:
        self._record("assistant_text", iteration=iteration, text=text)
        print(f"[{_now()}] [SAY   · turn {iteration}] {self._prefix()}{_oneline(text)}")

    def tool_call(self, iteration: int, name: str, tool_input: dict, result: str, is_error: bool) -> None:
        category, _ = TOOL_CATEGORY.get(name, DEFAULT_CATEGORY)
        self.counts["tools"] += 1
        if is_error:
            self.counts["errors"] += 1
        if name == "file_issue" and not is_error:
            self.counts["issues"] += 1
        if name == "propose_enhancement" and not is_error:
            self.counts["enhancements"] += 1
        self._record(
            "tool_call", iteration=iteration, name=name, category=category,
            input=tool_input, result=result, is_error=is_error,
        )
        tag = "ERROR" if is_error else category.upper()
        print(f"[{_now()}] [{tag:9}] {self._prefix()}{name}({_oneline(json.dumps(tool_input))}) -> {_oneline(result)}")

    def set_digest(self, text: str) -> None:
        """The final digest the agent publishes — surfaced on the dashboard."""
        self.digest_text = text

    def finish(self, status: str) -> None:
        self.status = status
        self._record("run_end", status=status, counts=dict(self.counts))
        print(
            f"[{_now()}] ── run {status} · "
            f"{self.counts['tools']} tool calls, {self.counts['issues']} issue(s), "
            f"{self.counts['enhancements']} enhancement(s), {self.counts['errors']} error(s) ──"
        )

    # ── output ───────────────────────────────────────────────────────────

    def write(self) -> None:
        with open(self.jsonl_path, "w", encoding="utf-8") as f:
            for ev in self.events:
                f.write(json.dumps(ev) + "\n")
        with open(self.html_path, "w", encoding="utf-8") as f:
            f.write(self._render_html())
        print(f"[{_now()}] wrote {self.html_path} and {self.jsonl_path}")

    def project_health(self) -> dict:
        """Per-project read health with blind-spot continuity (self-review #1).

        A project read returning "ok" resets it; "not_configured"/"error"/a raised
        tool marks it BLIND and increments a cross-run blind_cycles counter so the
        dashboard and notification can flag a project that's been dark >1 cycle —
        instead of a green run hiding the fact that we couldn't see it.
        """
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        projects = {}
        for ev in self.events:
            if ev["kind"] != "tool_call" or ev["name"] not in self.read_tools:
                continue
            label = self.read_tools[ev["name"]]
            obj = {}
            if ev["is_error"]:
                status, reason = "error", _oneline(ev["result"], 120)
            else:
                try:
                    obj = json.loads(ev["result"])
                except (ValueError, TypeError):
                    obj = {}
                rs = obj.get("status", "ok")
                if rs != "ok":
                    status, reason = "blind", ("not configured" if rs == "not_configured" else rs)
                elif _is_stale(obj):
                    status, reason = "stale", "read OK but the project's data is past-due / stale"
                elif _is_idle(obj):
                    status, reason = "idle", "read OK but no recent activity"
                else:
                    status, reason = "ok", None
            # Prefer the project's self-reported `app` name from its status file;
            # fall back to the static READ_TOOLS label when a read fails or the
            # file omits `app`, so the name stays stable across healthy/blind runs.
            name = _app_name(obj) or label
            prev = self.prev_projects.get(name, {})
            if status == "ok":
                projects[name] = {"status": "ok", "last_ok": now_iso, "blind_cycles": 0}
            elif status == "stale":
                # Data past-due, but the read itself worked so last_ok updates;
                # track how many cycles it's been stale so a chronically dark feed
                # escalates week over week instead of resetting.
                projects[name] = {"status": "stale", "reason": reason, "last_ok": now_iso,
                                  "blind_cycles": 0, "stale_cycles": prev.get("stale_cycles", 0) + 1}
            elif status == "idle":
                # Idle read fine, so last_ok updates; track how long it's been quiet.
                projects[name] = {"status": "idle", "reason": reason, "last_ok": now_iso,
                                  "blind_cycles": 0, "idle_cycles": prev.get("idle_cycles", 0) + 1}
            else:
                projects[name] = {"status": status, "reason": reason,
                                  "last_ok": prev.get("last_ok"),
                                  "blind_cycles": prev.get("blind_cycles", 0) + 1}
        return projects

    def rollup(self) -> dict:
        """A scannable, top-of-dashboard summary of this run (idle-detection +
        rollup). Reuses the per-project health flags (status / idle_cycles /
        blind_cycles) rather than inventing new state: every project that isn't
        "ok" becomes an attention row, and any that's been idle/blind for
        >= NUDGE_CYCLES is flagged `nudge` so a project that's quietly gone dark
        (e.g. volleyball idle 3 cycles) is impossible to miss instead of being
        buried in the timeline.
        """
        health = self.project_health()
        attention = []
        for name, p in health.items():
            status = p.get("status")
            if status == "ok":
                continue
            if status == "stale":
                cycles = p.get("stale_cycles", 0)
            elif status == "idle":
                cycles = p.get("idle_cycles", 0)
            else:
                cycles = p.get("blind_cycles", 0)
            # A stale feed is a freshness violation the moment we see it — a
            # scheduled job has stopped — so it nudges immediately rather than
            # waiting out the idle/blind cycle threshold (overseer #1).
            nudge = True if status == "stale" else cycles >= NUDGE_CYCLES
            attention.append({
                "name": name,
                "status": status,
                "detail": _attention_detail(status, p),
                "cycles": cycles,
                "nudge": nudge,
            })
        # Nudged projects first; within each group most-severe status first
        # (can't-see-it before past-due before merely-quiet), then alphabetical —
        # so a dead daily feed never sits below a harmlessly-idle project.
        attention.sort(key=lambda a: (not a["nudge"], _ATTENTION_SEVERITY.get(a["status"], 0), a["name"]))
        return {
            "ok": sum(1 for p in health.values() if p.get("status") == "ok"),
            "total": len(health),
            "issues": self.counts["issues"],
            "enhancements": self.counts["enhancements"],
            "errors": self.counts["errors"],
            "nudge_threshold": NUDGE_CYCLES,
            "attention": attention,
        }

    def write_digest(self, path: str) -> None:
        """Emit docs/digest.json — what the installable web app reads."""
        timeline = []
        for ev in self.events:
            # Tag each row with the agent that produced it so the dashboard can
            # group the timeline by agent (Bug-Hunter / Idea / Reviewer).
            if ev["kind"] == "tool_call":
                label = "error" if ev["is_error"] else ev["category"]
                timeline.append({"ts": ev["ts"], "agent": ev.get("agent"),
                                 "label": f"{ev['name']} ({label})",
                                 "text": _tool_summary(ev)})
            elif ev["kind"] == "thinking":
                entry = {"ts": ev["ts"], "agent": ev.get("agent"),
                         "label": "reasoning",
                         "text": _oneline(ev["text"], 240)}
                # Ship the full reasoning (capped) alongside the truncated line
                # so the dashboard can expand it in place instead of dead-ending
                # at "…" — the full text otherwise lives only in the CI artifact.
                full = _oneline(ev["text"], 2000)
                if full != entry["text"]:
                    entry["text_full"] = full
                timeline.append(entry)
        payload = {
            "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status": self.status,
            "summary": self.digest_text or "(no digest produced this run)",
            "counts": dict(self.counts),
            "rollup": self.rollup(),
            "projects": self.project_health(),
            "timeline": timeline,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        print(f"[{_now()}] wrote {path}")

    def write_history(self, path: str, max_runs: int = 26) -> None:
        """Append this run's per-project health + counts to an append-only history
        file the dashboard turns into trend sparklines (overseer #6).

        Each run contributes one record keyed by date; a same-day re-run replaces
        that day's record rather than double-counting. The file is capped to the
        last `max_runs` records so it (and the sparklines) stay small. Per project
        we store a 0..1 health score (ok=1, idle=0.5, error/blind=0) so a
        regression shows up as the line dropping week over week. The run's digest
        `summary` is stored too, so the dashboard can offer an expandable log of
        past digests, not just the latest one.
        """
        try:
            with open(path, encoding="utf-8") as f:
                history = json.load(f)
            runs = history.get("runs", []) if isinstance(history, dict) else []
        except (FileNotFoundError, ValueError):
            runs = []

        now = datetime.now(timezone.utc)
        record = {
            "date": now.strftime("%Y-%m-%d"),
            "generated": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status": self.status,
            "summary": self.digest_text or "",
            "counts": dict(self.counts),
            "projects": {name: {"status": p.get("status"), "score": _status_score(p.get("status"))}
                         for name, p in self.project_health().items()},
        }
        # Replace a record from the same day (re-run) instead of appending a dup.
        if runs and runs[-1].get("date") == record["date"]:
            runs[-1] = record
        else:
            runs.append(record)
        runs = runs[-max_runs:]

        with open(path, "w", encoding="utf-8") as f:
            json.dump({"runs": runs}, f, indent=2)
        print(f"[{_now()}] wrote {path} ({len(runs)} run(s) of history)")

    def _render_html(self) -> str:
        rows = []
        for ev in self.events:
            rows.append(_render_event(ev))
        generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        c = self.counts
        return _HTML_TEMPLATE.format(
            generated=generated,
            tools=c["tools"], issues=c["issues"],
            enhancements=c["enhancements"], errors=c["errors"],
            timeline="\n".join(rows),
        )


_ACTIVITY_KEYS = ("trades", "signals_evaluated", "footage_processed", "frames_processed",
                  "clips_processed", "events_tracked", "runs_7d")


# Attention-row ordering: lower sorts first (more urgent). "Can't see it"
# (blind/error) outranks a past-due feed (stale), which outranks a quiet-but-fresh
# project (idle). Keeps the dead-daily-feed inversion from ever recurring.
_ATTENTION_SEVERITY = {"error": 0, "blind": 0, "stale": 1, "idle": 2}


# Per-project health collapsed to a 0..1 score the dashboard plots as a sparkline
# (overseer #6): a drop from 1.0 to 0.5/0.25/0.0 is a visible week-over-week
# regression. `stale` (a past-due feed) sits below `idle` (quiet but fresh)
# because a scheduled job that has stopped is a real problem, not just a quiet week.
_STATUS_SCORE = {"ok": 1.0, "idle": 0.5, "stale": 0.25, "error": 0.0, "blind": 0.0}


def _status_score(status) -> float:
    return _STATUS_SCORE.get(status, 0.0)


def activity_idle(data) -> bool:
    """No-activity heuristic on a project's published data: an explicit idle flag,
    or every known activity counter present and at zero/null. Read tools call this
    so the agent gets an explicit signal instead of inferring (overseer #5)."""
    if not isinstance(data, dict):
        return False
    if data.get("idle") is True:
        return True
    present = [data[k] for k in _ACTIVITY_KEYS if k in data]
    return bool(present) and all(v in (0, None) for v in present)


def _app_name(obj: dict):
    """A project's self-reported display name: the `app` field of the status file
    it publishes, surfaced through the read tool's `data`. Preferring it lets a
    repo rename itself on the dashboard (e.g. Volleyball → coachvision) without a
    code change. Returns None when there's no readable `app` so the caller can
    fall back to the static READ_TOOLS label."""
    data = obj.get("data") if isinstance(obj, dict) else None
    if isinstance(data, dict):
        app = data.get("app")
        if isinstance(app, str) and app.strip():
            return app.strip()
    return None


def _is_stale(obj: dict) -> bool:
    """A read succeeded but the project's own status file declares its data
    past-due — an explicit `stale`/`data_stale` flag. This is a freshness
    violation (a scheduled job that has stopped producing), distinct from a
    genuinely quiet-but-fresh project: a daily bot dark for days is a problem
    *now*, not merely 'no recent activity'. Callers check this before `_is_idle`
    so staleness never gets flattened into the softer idle bucket (overseer #1).
    """
    return bool(obj.get("stale") or obj.get("data_stale"))


def _is_idle(obj: dict) -> bool:
    """A read succeeded and the data is fresh but the project shows no recent
    activity (overseer #4): an explicit `idle` flag, or every known activity
    counter at zero. Distinct from `_is_stale` — this is 'quiet', not 'dead'.
    Lets us render zero-activity as amber instead of green without conflating it
    with a past-due feed.
    """
    if obj.get("idle") is True:
        return True
    data = obj.get("data") if isinstance(obj.get("data"), dict) else obj
    return activity_idle(data)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _attention_detail(status: str, p: dict) -> str:
    """One-line reason a project needs attention, built from its health flags."""
    if status == "stale":
        return "data past-due · stale " + _plural(p.get("stale_cycles", 0), "cycle")
    if status == "idle":
        return "no recent activity · idle " + _plural(p.get("idle_cycles", 0), "cycle")
    if status == "blind":
        reason = p.get("reason") or "no data"
        return f"{reason} · blind " + _plural(p.get("blind_cycles", 0), "cycle")
    return p.get("reason") or status


def _tool_summary(ev: dict) -> str:
    """A human-readable one-liner for a tool call, instead of raw JSON."""
    name, inp = ev["name"], ev.get("input", {})
    if ev["is_error"]:
        return _oneline(ev["result"], 200)
    try:
        result = json.loads(ev["result"])
    except (ValueError, TypeError):
        result = {}
    if name == "propose_enhancement":
        return (f"{inp.get('repo', '')} — {inp.get('title', '')} "
                f"(effort {inp.get('effort', '?')}, impact {inp.get('impact', '?')})")
    if name == "file_issue":
        n = result.get("number")
        return f"{inp.get('repo', '')} — {inp.get('title', '')}" + (f" (#{n})" if n else "")
    if name == "search_existing_issues":
        return f"searched {inp.get('repo', '')}: {len(result.get('matches', []))} match(es)"
    if name.startswith("read_"):
        status = result.get("status", "ok")
        extra = result.get("detail") or result.get("last_error") or ""
        return _oneline(f"{status}" + (f" — {extra}" if extra else ""), 160)
    if name in ("send_telegram_summary", "publish_digest"):
        status = result.get("status", "sent")
        return f"digest {status}"
    return _oneline(ev["result"], 160)


def _oneline(text: str, limit: int = 160) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _render_event(ev: dict) -> str:
    kind = ev["kind"]
    ts = ev["ts"]
    if kind == "thinking":
        return _card("#7c3aed", "thinking", ts, "Reasoning", _pre(ev["text"]))
    if kind == "assistant_text":
        return _card("#334155", "note", ts, "Agent note", _pre(ev["text"]))
    if kind == "tool_call":
        _, colour = TOOL_CATEGORY.get(ev["name"], DEFAULT_CATEGORY)
        if ev["is_error"]:
            colour = "#dc2626"
        badges = ""
        inp = ev.get("input", {})
        if ev["name"] == "propose_enhancement":
            badges = (
                f"<span class='badge'>effort: {html.escape(str(inp.get('effort', '?')))}</span>"
                f"<span class='badge'>impact: {html.escape(str(inp.get('impact', '?')))}</span>"
            )
        label = "ERROR" if ev["is_error"] else ev["category"]
        title = f"{html.escape(ev['name'])} <span class='tag'>{html.escape(label)}</span>{badges}"
        body = (
            f"<div class='kv'>input</div>{_pre(json.dumps(inp, indent=2))}"
            f"<div class='kv'>result</div>{_pre(ev['result'])}"
        )
        return _card(colour, ev["category"], ts, title, body)
    if kind == "agent_start":
        return _card("#0ea5e9", "agent", ts, f"Agent: {html.escape(ev['agent'])}", "")
    if kind == "run_start":
        return _card("#059669", "start", ts, "Run started", "")
    if kind == "run_end":
        return _card("#059669", "end", ts, f"Run {html.escape(ev['status'])}", "")
    return ""


def _card(colour: str, klass: str, ts: str, title: str, body: str) -> str:
    return (
        f"<div class='item {klass}' style='border-left-color:{colour}'>"
        f"<div class='head'><span class='ts'>{html.escape(ts)}</span>"
        f"<span class='title'>{title}</span></div>"
        f"{body}</div>"
    )


def _pre(text: str) -> str:
    if not text:
        return ""
    return f"<pre>{html.escape(str(text))}</pre>"


_HTML_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Overseer run report</title>
<style>
  body {{ font: 14px/1.5 -apple-system, system-ui, sans-serif; margin: 0; background: #f8fafc; color: #0f172a; }}
  header {{ background: #0f172a; color: #fff; padding: 20px 28px; }}
  header h1 {{ margin: 0 0 4px; font-size: 18px; }}
  header .sub {{ color: #94a3b8; font-size: 13px; }}
  .stats {{ display: flex; gap: 12px; padding: 16px 28px; flex-wrap: wrap; }}
  .stat {{ background: #fff; border: 1px solid #e2e8f0; border-radius: 8px; padding: 10px 16px; }}
  .stat .n {{ font-size: 22px; font-weight: 600; }}
  .stat .l {{ font-size: 12px; color: #64748b; }}
  main {{ padding: 8px 28px 40px; max-width: 900px; }}
  .item {{ background: #fff; border: 1px solid #e2e8f0; border-left-width: 4px;
           border-radius: 8px; padding: 12px 16px; margin: 10px 0; }}
  .head {{ display: flex; align-items: baseline; gap: 10px; }}
  .ts {{ color: #94a3b8; font-variant-numeric: tabular-nums; font-size: 12px; }}
  .title {{ font-weight: 600; }}
  .tag {{ font-size: 11px; text-transform: uppercase; letter-spacing: .04em;
          color: #64748b; font-weight: 600; }}
  .badge {{ font-size: 11px; background: #fef3c7; color: #92400e; border-radius: 999px;
            padding: 2px 8px; margin-left: 6px; }}
  .kv {{ font-size: 11px; text-transform: uppercase; color: #94a3b8; margin: 8px 0 2px; }}
  pre {{ background: #f1f5f9; border-radius: 6px; padding: 8px 10px; margin: 0;
         white-space: pre-wrap; word-break: break-word; font-size: 12.5px; }}
</style></head>
<body>
<header><h1>Project Overseer — run report</h1>
<div class="sub">Generated {generated}</div></header>
<div class="stats">
  <div class="stat"><div class="n">{tools}</div><div class="l">tool calls</div></div>
  <div class="stat"><div class="n">{issues}</div><div class="l">issues filed</div></div>
  <div class="stat"><div class="n">{enhancements}</div><div class="l">enhancements</div></div>
  <div class="stat"><div class="n">{errors}</div><div class="l">errors</div></div>
</div>
<main>{timeline}</main>
</body></html>
"""
