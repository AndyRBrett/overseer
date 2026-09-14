#!/usr/bin/env bash
# Rebuild the three published files under docs/ from live GitHub state.
#
# One script rather than three workflow steps because ledger-refresh.yml needs
# to run this TWICE in a run: once normally, and again after losing a push race,
# where the only correct resolution is to recompute on top of whatever landed
# first (see the publish step's comment). Two copies of the build order would be
# two places to forget that the ask-context pack has to be rebuilt last.
#
# Everything here is pure GitHub reads and arithmetic — no Anthropic key, no
# agents, no model calls — so re-running it costs nothing but a few API calls.
#
# Set FULL=--full to re-walk every issue instead of carrying settled outcomes
# forward.
set -euo pipefail

cd "$(dirname "$0")/.."

# The delivery ledger: every filed issue, its PR, and what became of it.
python scripts/refresh_ledger.py ${FULL:-}

# The top half of the dashboard — per-project health, the freshness alerts, the
# attention ranking — was written ONLY by the weekly review, so the page carried
# two clocks: a delivery panel current to the hour above project health that
# could be five days old (0.3h against 117.8h on 2026-09-05), and the stale half
# was the half that says whether anything is broken. None of it needs a model:
# it is four GitHub reads and some arithmetic, which is exactly what this script
# already is.
#
# It runs BEFORE the ask-context rebuild so the voice assistant answers from the
# same freshly-read health the page shows.
python scripts/refresh_status.py

# The voice assistant answers from this pack, so it has to move with the ledger
# — an assistant describing last week's queue out loud is worse than one that
# says it doesn't know. Free to rebuild: pure file reads, no GitHub, no model
# calls, and it no-ops when nothing changed.
python scripts/build_ask_context.py
