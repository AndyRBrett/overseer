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
# NOTE ON SQUASH MERGES. A squash-merged branch is NOT an ancestor of main — its
# commits were replaced by one new commit — so the ancestry guard reports it as
# unmerged and refuses to touch it. That is the guard working, not a bug, but it
# means squash-merged branches never clear automatically. They go in the
# delete_explicit list at the bottom, each with a reason, so dropping one is a
# recorded decision rather than a loosened safety check.
#
# Usage:  bash delete-merged-branches.sh          # dry run, prints only
#         bash delete-merged-branches.sh --go     # actually delete
#
# Env:
#   GH_TOKEN  a token with Contents: write on the repos below. Optional for a
#             dry run (the clones are public), REQUIRED for --go. This is how
#             the "Clean up merged branches" Action runs the script; from a
#             terminal your ambient git credentials are used instead.
#   GH_OWNER  repo owner (default AndyRBrett).
#
# Exit status is the number of failed deletions, so a CI job fails loudly rather
# than reporting success over a pile of 403s.
set -uo pipefail
GO="${1:-}"
GH_TOKEN="${GH_TOKEN:-}"
GH_OWNER="${GH_OWNER:-AndyRBrett}"

DELETED=0; FAILED=0; SKIPPED=0; PENDING=0

# Built in a function rather than held in a variable so the token never lands in
# a log line or an error message.
clone_url () {
  if [ -n "$GH_TOKEN" ]; then
    echo "https://x-access-token:${GH_TOKEN}@github.com/${GH_OWNER}/$1"
  else
    echo "https://github.com/${GH_OWNER}/$1"
  fi
}

delete_merged () {
  local repo="$1"; shift
  echo "=== $repo ==="
  local dir; dir=$(mktemp -d)
  if ! git clone -q "$(clone_url "$repo")" "$dir"; then
    echo "  clone failed (is GH_TOKEN scoped to $GH_OWNER/$repo?)"
    FAILED=$((FAILED + 1)); rm -rf "$dir"; return
  fi
  # The loop runs in this shell rather than a ( subshell ) so the counters
  # survive it — a summary assembled from lost counters would always read zero.
  local prev; prev=$(pwd)
  cd "$dir" || { rm -rf "$dir"; return; }
  for b in "$@"; do
    if ! git rev-parse --verify -q "origin/$b" >/dev/null; then
      echo "  skip (gone):     $b"; SKIPPED=$((SKIPPED + 1)); continue
    fi
    if git merge-base --is-ancestor "origin/$b" origin/main; then
      if [ "$GO" = "--go" ]; then
        if git push -q origin --delete "$b"; then
          echo "  deleted:         $b"; DELETED=$((DELETED + 1))
        else
          echo "  FAILED:          $b"; FAILED=$((FAILED + 1))
        fi
      else
        echo "  would delete:    $b"; PENDING=$((PENDING + 1))
      fi
    else
      echo "  SKIP (unmerged): $b"   # gained commits since the list was made
      SKIPPED=$((SKIPPED + 1))
    fi
  done
  cd "$prev" || return
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

# --- branches deleted on a stated reason, not on ancestry ---------------------
# Everything above is provably recoverable from main. Everything here is not, so
# each entry carries the reason it is safe to drop. The ancestry guard does not
# apply — that is the whole point of the list — which is why it stays short and
# annotated rather than becoming a convenient bypass.
delete_explicit () {
  local repo="$1" branch="$2" reason="$3"
  echo "=== $repo: $branch ==="
  echo "  reason: $reason"
  if [ "$GO" != "--go" ]; then
    echo "  would delete:    $branch"; PENDING=$((PENDING + 1)); return
  fi
  local dir; dir=$(mktemp -d)
  if git clone -q "$(clone_url "$repo")" "$dir" \
     && (cd "$dir" && git push -q origin --delete "$branch"); then
    echo "  deleted:         $branch"; DELETED=$((DELETED + 1))
  else
    echo "  FAILED (or already gone): $branch"; FAILED=$((FAILED + 1))
  fi
  rm -rf "$dir"
}

# tests/test_fixer_tools.py pushed this to the real repo while asserting that it
# could NOT push (see overseer PR #10). It holds an unrelated two-commit fixture
# history (calc.py) and touches nothing on main.
delete_explicit ufc-dashboard overseer/fix-7-49981b \
  "stray fixture branch pushed by a sandbox test; unrelated to main's history"

# Squash-merged as overseer PR #22, so its commits are not ancestors of main.
delete_explicit overseer claude/light-model \
  "squash-merged to main as PR #22 (model tiering)"

echo
if [ "$GO" = "--go" ]; then
  echo "SUMMARY: $DELETED deleted, $FAILED failed, $SKIPPED skipped"
  if [ "$FAILED" -gt 0 ]; then
    echo "A failure here is usually token scope: deleting a branch needs" \
         "Contents: write on that repo."
  fi
else
  echo "SUMMARY: $PENDING would be deleted, $SKIPPED skipped (dry run — nothing changed)"
  echo "Re-run with --go to apply."
fi
exit "$FAILED"
