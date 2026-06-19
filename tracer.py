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
import time
from datetime import datetime, timezone

# Maps each tool to a visual category: a label + colour used in the timeline.
# This is what turns a flat log into something you can read at a glance —
# "investigate" steps look different from a filed bug or a proposed idea.
TOOL_CATEGORY = {
    "read_trading_bot_log": ("investigate", "#2563eb"),
    "read_volleyball_results": ("investigate", "#2563eb"),
    "read_ufc_scraper_status": ("investigate", "#2563eb"),
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

    # ── recording ────────────────────────────────────────────────────────

    def _record(self, kind: str, **data) -> None:
        ev = {"ts": _now(), "elapsed_s": round(time.monotonic() - self._t0, 2), "kind": kind, **data}
        self.events.append(ev)

    def start(self) -> None:
        self._record("run_start")
        print(f"\n[{_now()}] ── overseer run started ──")

    def thinking(self, iteration: int, text: str) -> None:
        self._record("thinking", iteration=iteration, text=text)
        print(f"[{_now()}] [THINK · turn {iteration}] {_oneline(text)}")

    def assistant_text(self, iteration: int, text: str) -> None:
        self._record("assistant_text", iteration=iteration, text=text)
        print(f"[{_now()}] [SAY   · turn {iteration}] {_oneline(text)}")

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
        print(f"[{_now()}] [{tag:9}] {name}({_oneline(json.dumps(tool_input))}) -> {_oneline(result)}")

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

    def write_digest(self, path: str) -> None:
        """Emit docs/digest.json — what the installable web app reads."""
        timeline = []
        for ev in self.events:
            if ev["kind"] == "tool_call":
                label = "error" if ev["is_error"] else ev["category"]
                timeline.append({"ts": ev["ts"], "label": f"{ev['name']} ({label})",
                                 "text": _tool_summary(ev)})
            elif ev["kind"] == "thinking":
                timeline.append({"ts": ev["ts"], "label": "reasoning",
                                 "text": _oneline(ev["text"], 240)})
        payload = {
            "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status": self.status,
            "summary": self.digest_text or "(no digest produced this run)",
            "counts": dict(self.counts),
            "timeline": timeline,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        print(f"[{_now()}] wrote {path}")

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
    if name == "publish_digest":
        return "digest published"
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
