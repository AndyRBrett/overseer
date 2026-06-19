#!/usr/bin/env python3
"""Publish a small status file that Project Overseer reads each week.

DROP THIS INTO THE crypto-trading REPO. Run it at the end of the bot's daily
run (or as a separate scheduled step), then commit `overseer-status.json`. The
overseer reads that file from the repo via the GitHub API — no shared disk, no
local path, fully cloud-side.

Only `collect_metrics()` is bot-specific — adapt it to your real data source.
Everything the overseer needs is just: a JSON file with a `generated_at`
timestamp (for staleness) plus whatever metrics you want surfaced.
"""

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

WINDOW_DAYS = int(os.getenv("OVERSEER_WINDOW_DAYS", "7"))
OUT_PATH = os.getenv("OVERSEER_STATUS_PATH", "overseer-status.json")
DB_PATH = os.getenv("TRADING_DB_PATH", "trades.db")


def collect_metrics() -> dict:
    """Return the metrics dict. >>> ADAPT THIS to your schema. <<<

    Assumes a `trades(ts TEXT ISO-8601, pnl REAL)` table — change the query to
    match your bot. Keep the returned keys stable so the digest reads cleanly.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)).isoformat()
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            "SELECT COUNT(*) AS trades, "
            "       COALESCE(SUM(pnl), 0) AS pnl, "
            "       COALESCE(AVG(CASE WHEN pnl > 0 THEN 1.0 ELSE 0.0 END), 0) AS win_rate, "
            "       MAX(ts) AS last_fill_at "
            "FROM trades WHERE ts >= :since",
            {"since": since},
        ).fetchone()
    finally:
        con.close()
    return {
        "trades": row["trades"],
        "pnl": round(row["pnl"], 2),
        "win_rate": round(row["win_rate"], 3),
        "last_fill_at": row["last_fill_at"],
    }


def main() -> None:
    status = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window_days": WINDOW_DAYS,
        "errors": [],
    }
    try:
        status.update(collect_metrics())
    except Exception as exc:  # noqa: BLE001 — record so the overseer sees the failure
        status["errors"].append(f"metrics collection failed: {exc}")
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(status, f, indent=2)
    print(f"wrote {OUT_PATH}: {json.dumps(status)}")


if __name__ == "__main__":
    main()
