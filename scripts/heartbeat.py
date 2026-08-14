"""
Dead-man's switch for the overseer itself (overseer #13 / #17).

The overseer watches four projects, so when the overseer stops, all four go
dark at once — and nothing is left to notice. This script is the independent
watcher of the watcher. It runs on its own daily schedule and answers two
questions the weekly pipeline structurally cannot answer about itself:

  1. DID IT RUN?    docs/digest.json carries the timestamp of the last completed
                    review. If that stops advancing, the weekly run is dead —
                    a lapsed cron, a disabled workflow, a crash on startup.
  2. DID IT SEE?    A run that completes but reads nothing is worth no more than
                    a run that never happened. Between 2026-07-20 and 08-10 the
                    weekly Action reported SUCCESS four times while an expired
                    PAT 401'd every tool call inside it; the agents degraded
                    gracefully, wrote a digest, and the workflow stayed green.
                    A high error ratio in the last digest is that failure's
                    fingerprint, so it alerts even when the digest is fresh.

DELIBERATELY DEPENDENCY-FREE. Standard library only: no PyGithub, no anthropic,
no GitHub API call, and no use of OVERSEER_GITHUB_TOKEN. The failure this alarm
exists to catch is *a broken credential*, so it must not need one. Everything it
checks comes from the digest file already committed in the repo checkout, and
the only network call is the optional Telegram alert (its own separate secret).

Exit status is the primary signal — 1 means unhealthy, which turns the Action
red and gets a failure email from GitHub even if Telegram is unconfigured.

Environment:
  HEARTBEAT_MAX_AGE_HOURS   staleness threshold (default 192h = 8 days, so a
                            single missed weekly run trips it)
  HEARTBEAT_ERROR_RATIO     blind-run threshold (default 0.5 of tool calls)
  TELEGRAM_BOT_TOKEN        optional — same secrets the weekly digest uses
  TELEGRAM_CHAT_ID          optional
"""

import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

DIGEST_PATH = os.getenv("DIGEST_PATH", "docs/digest.json")

# One missed weekly run is the alarm point. The weekly cron fires every 168h, so
# 192h leaves ~24h of slack for a late or retried run before we cry wolf.
MAX_AGE_HOURS = float(os.getenv("HEARTBEAT_MAX_AGE_HOURS", "192"))

# Fraction of tool calls that must fail before a "successful" run is treated as
# blind. The four silent-failure runs scored 0.91–0.95 against this.
ERROR_RATIO = float(os.getenv("HEARTBEAT_ERROR_RATIO", "0.5"))


def _age_hours(timestamp, now=None):
    """Hours since an ISO-8601 timestamp, or None if it can't be parsed.

    Unparseable is not 'fresh': the caller treats None as a failure, because a
    digest we cannot date is a digest we cannot vouch for.
    """
    if not timestamp:
        return None
    try:
        ts = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ((now or datetime.now(timezone.utc)) - ts).total_seconds() / 3600


def check_digest(digest, now=None):
    """Assess a digest payload. Returns (healthy, list_of_alert_strings).

    Pure function over already-loaded JSON so it is directly testable without
    touching the filesystem, the clock, or the network.
    """
    alerts = []

    age = _age_hours(digest.get("generated"), now)
    if age is None:
        alerts.append(
            f"Digest has no readable 'generated' timestamp ({digest.get('generated')!r}) "
            "— cannot confirm the overseer ran at all."
        )
    elif age > MAX_AGE_HOURS:
        days = age / 24
        alerts.append(
            f"No completed review in {age:.0f}h ({days:.1f} days) — the weekly run "
            f"has missed its schedule (threshold {MAX_AGE_HOURS:.0f}h)."
        )

    counts = digest.get("counts") or {}
    calls, errors = counts.get("tools") or 0, counts.get("errors") or 0
    if calls and errors / calls >= ERROR_RATIO:
        alerts.append(
            f"Last review completed but was BLIND: {errors}/{calls} tool calls failed. "
            "Projects went unreviewed — check the GitHub token first (an expired "
            "OVERSEER_GITHUB_TOKEN 401s every call while the run still reports success)."
        )

    status = str(digest.get("status") or "")
    if status and status != "completed":
        alerts.append(f"Last run did not complete cleanly: status={status!r}.")

    return (not alerts), alerts


def send_telegram(text):
    """Best-effort Telegram alert. Never raises — a failed alert must not mask
    the exit code, which is the signal that actually matters."""
    token, chat_id = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        print("[heartbeat] Telegram not configured — relying on the non-zero exit status.")
        return
    payload = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    try:
        with urllib.request.urlopen(
            f"https://api.telegram.org/bot{token}/sendMessage", data=payload, timeout=15
        ) as resp:
            print(f"[heartbeat] Telegram alert sent ({resp.status}).")
    except Exception as exc:  # noqa: BLE001 — alerting must never crash the check
        print(f"[heartbeat] Telegram alert failed: {exc}")


def main():
    try:
        with open(DIGEST_PATH, encoding="utf-8") as f:
            digest = json.load(f)
    except FileNotFoundError:
        print(f"[heartbeat] FAIL: {DIGEST_PATH} not found — the overseer has never "
              "published a digest.")
        send_telegram(f"🚨 Overseer heartbeat: {DIGEST_PATH} is missing — no review has ever run.")
        return 1
    except json.JSONDecodeError as exc:
        print(f"[heartbeat] FAIL: {DIGEST_PATH} is not valid JSON: {exc}")
        send_telegram(f"🚨 Overseer heartbeat: {DIGEST_PATH} is corrupt ({exc}).")
        return 1

    healthy, alerts = check_digest(digest)
    if healthy:
        age = _age_hours(digest.get("generated"))
        print(f"[heartbeat] OK — last review {age:.0f}h ago, "
              f"{(digest.get('counts') or {}).get('errors', 0)} tool errors.")
        return 0

    body = "🚨 Overseer heartbeat FAILED\n\n" + "\n\n".join(f"• {a}" for a in alerts)
    print("[heartbeat] FAIL:")
    for alert in alerts:
        print(f"[heartbeat]   - {alert}")
    send_telegram(body)
    return 1


if __name__ == "__main__":
    sys.exit(main())
