#!/usr/bin/env bash
# Delete branches that are FULLY MERGED into main — every commit already exists
# in main's history, so nothing is lost and each is recoverable from main.
#
# Generated 2026-08-14. Verified with `git merge-base --is-ancestor <branch> main`
# at that time; the guard below re-verifies before each delete, so a branch that
# has gained commits since is skipped rather than dropped.
#
# Excluded deliberately: main, gh-pages, and bot-state (crypto-trading's bot
# publishes state.json to bot-state — deleting it breaks the dashboard).
#
# Usage:  bash delete-merged-branches.sh          # dry run, prints only
#         bash delete-merged-branches.sh --go     # actually delete
set -uo pipefail
GO="${1:-}"

delete_merged () {
  local repo="$1"; shift
  echo "=== $repo ==="
  local dir; dir=$(mktemp -d)
  git clone -q "https://github.com/AndyRBrett/$repo" "$dir" || { echo "  clone failed"; return; }
  ( cd "$dir"
    for b in "$@"; do
      if ! git rev-parse --verify -q "origin/$b" >/dev/null; then
        echo "  skip (gone):     $b"; continue
      fi
      if git merge-base --is-ancestor "origin/$b" origin/main; then
        if [ "$GO" = "--go" ]; then
          git push -q origin --delete "$b" && echo "  deleted:         $b" || echo "  FAILED:          $b"
        else
          echo "  would delete:    $b"
        fi
      else
        echo "  SKIP (unmerged): $b"   # gained commits since the list was made
      fi
    done )
  rm -rf "$dir"
}

delete_merged overseer \
  claude/3-agent-pipeline-refactor-4fz372 \
  claude/coachvision-registry-rename-mh89ld \
  claude/json-logs-review-m2nqop \
  claude/ledger-live-refresh \
  claude/ledger-shipped-semantics \
  claude/mobile-copy-button-hsmu0j \
  claude/new-session-dk5m27 \
  claude/overseer-enhancements-u8xh5x \
  claude/overseer-feedback-review-7bjux3 \
  claude/overseer-github-token-issues-7ettay \
  claude/overseer-review-suggestions-lf31gb

delete_merged ufc-dashboard \
  claude/app-opening-issue-8sxssd \
  claude/challenges-toggle-delete-spacing-4th04k \
  claude/codebase-security-audit-y71uga \
  claude/dashboard-enhancement-ideas-ahu2kn \
  claude/dean-omalley-odds-qq28d0 \
  claude/fight-card-verification-lsmygw \
  claude/fn-mode-manage-card-gpvvnv \
  claude/leaderboard-badge-layout-taien0 \
  claude/main-page-blank-space-qp8ct0 \
  claude/notification-failures-card-picks-2qo1fa \
  claude/nudge-notifications-push-j7wzqn \
  claude/overseer-enhancements-bn8vx2 \
  claude/overseer-github-token-issues-7ettay \
  claude/picks-dropped-yesterday-card-ok2yb1 \
  claude/picks-share-themed-image-0g2bnd \
  claude/supabase-auto-push-notifications-buymuk \
  claude/trash-talk-persona-search-gl1ssh \
  claude/ufc-extraction-status-odds-ngld3a \
  claude/ufc-fight-card-validation-a3rms1 \
  claude/ufc-freedom-250-start-time-0uytqv \
  claude/ufc-odds-snapshots-lcux2q \
  claude/ui-color-themes-ph3nr0

delete_merged crypto-trading \
  claude/codebase-security-audit-8xl9oi \
  claude/crypto-bot-audit-enhance-50zx4n \
  claude/crypto-bot-shorting-explore-x2yehk \
  claude/crypto-risk-metrics-signals-iegu3e \
  claude/equity-jump-bug-audit-9no1j8 \
  claude/first-bot-trade-failure-hrb0gv \
  claude/multiple-paper-accounts-strategies-db46cl \
  claude/overseer-enhancements-qbhlvz \
  claude/overseer-feedback-review-j5j6wb \
  claude/overseer-github-token-issues-7ettay \
  claude/overseer-status-metrics-1b6qyh \
  claude/profit-portfolio-notifications-UHi3m \
  claude/sentiment-fees-calculation-nuyou3 \
  claude/trading-strategies-explanations-55z5vl

delete_merged coachvision \
  claude/coach-vision-cleanup-merge-wvkun6 \
  claude/coach-vision-e2e-slice-4xmoc6 \
  claude/overseer-enhancements-q8emif \
  claude/overseer-github-token-issues-7ettay \
  claude/overseer-status-heartbeat-pwpjok \
  claude/volleyball-cv-pipeline-coaching-34ydg0 \
  claude/volleyball-martial-arts-switch-0be6na

# --- stray branch created by a test run in a credentialed sandbox -------------
# tests/test_fixer_tools.py pushed this to the real repo while asserting that it
# could NOT push (see overseer PR #10). It holds an unrelated two-commit fixture
# history (calc.py), touches nothing on main, and is safe to delete outright.
# Not merged into main, so the guard above would skip it — handled separately.
echo "=== ufc-dashboard: stray test branch ==="
if [ "$GO" = "--go" ]; then
  d=$(mktemp -d); git clone -q https://github.com/AndyRBrett/ufc-dashboard "$d" \
    && (cd "$d" && git push -q origin --delete overseer/fix-7-49981b \
        && echo "  deleted:         overseer/fix-7-49981b" \
        || echo "  FAILED (may already be gone): overseer/fix-7-49981b")
  rm -rf "$d"
else
  echo "  would delete:    overseer/fix-7-49981b"
fi

echo "Done. Re-run with --go to apply." 
