#!/usr/bin/env bash
set -euo pipefail

# Sync local branch with upstream/main (BSkando/GoogleFindMy-HA)
# and restore files that existed only in the previous local state
# but were deleted by upstream.
#
# Usage:
#   ./sync_gfm_upstream.sh [--push] [--force] [branch]
#
# Examples:
#   ./sync_gfm_upstream.sh                   # sync main, kein push, kein auto-commit
#   ./sync_gfm_upstream.sh --push            # sync main, mit push
#   ./sync_gfm_upstream.sh --force           # sync main, vorher WIP-Commit bei Änderungen
#   ./sync_gfm_upstream.sh --push --force    # sync main, WIP-Commit + push
#   ./sync_gfm_upstream.sh dev-branch        # sync dev-branch
#   ./sync_gfm_upstream.sh --force dev-branch

UPSTREAM_REMOTE="upstream"
UPSTREAM_URL="https://github.com/BSkando/GoogleFindMy-HA.git"

DO_PUSH=0
DO_FORCE=0
BRANCH="main"

# Simple argument parsing
while [[ $# -gt 0 ]]; do
  case "$1" in
    --push|-p)
      DO_PUSH=1
      shift
      ;;
    --force|-f)
      DO_FORCE=1
      shift
      ;;
    *)
      BRANCH="$1"
      shift
      ;;
  esac
done

echo "Branch to sync          : $BRANCH"
echo "Upstream remote         : $UPSTREAM_REMOTE ($UPSTREAM_URL)"
echo "Auto-commit (WIP)       : $([[ $DO_FORCE -eq 1 ]] && echo 'YES (--force)' || echo 'NO')"
echo "Auto-push after sync    : $([[ $DO_PUSH -eq 1 ]] && echo 'YES (--push)' || echo 'NO')"
echo

# 1. Ensure we are inside a git repo
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "Error: not inside a git repository."
  exit 1
fi

# 2. Working tree check (with optional --force auto-commit)
STATUS_OUTPUT="$(git status --short)"

if [[ -n "$STATUS_OUTPUT" ]]; then
  echo "=================================================================="
  echo "[sync_gfm_upstream.sh] Working tree is not clean. Current status:"
  echo
  echo "$STATUS_OUTPUT"
  echo "=================================================================="
  echo "By default, the script aborts to avoid losing work."
  echo "If you want the script to automatically commit these changes as"
  echo "  \"WIP before upstream sync\""
  echo "and then continue, run it with the --force (or -f) option."
  echo

  if [[ $DO_FORCE -eq 0 ]]; then
    echo "Aborting. Re-run as:"
    echo "  ./sync_gfm_upstream.sh --force [--push] [$BRANCH]"
    exit 1
  fi

  echo "[sync_gfm_upstream.sh] --force specified: creating WIP commit before sync..."
  git add .
  git commit -m "WIP before upstream sync"

  # Re-check: now it must be clean
  STATUS_AFTER="$(git status --short)"
  if [[ -n "$STATUS_AFTER" ]]; then
    echo "Error: working tree is still not clean after forced WIP commit."
    echo "Please inspect the repository manually."
    exit 1
  fi

  echo "[sync_gfm_upstream.sh] WIP commit created. Proceeding with sync."
  echo
fi

# 3. Ensure branch exists and check it out
if ! git rev-parse --verify "$BRANCH" >/dev/null 2>&1; then
  echo "Error: local branch '$BRANCH' does not exist."
  exit 1
fi

git checkout "$BRANCH" >/dev/null 2>&1
echo "Checked out branch '$BRANCH'."

# 4. Create backup tag
BACKUP_TAG="before-upstream-sync-$(date +%Y%m%d-%H%M%S)"
echo "Creating backup tag: $BACKUP_TAG"
git tag "$BACKUP_TAG"

# 5. Ensure upstream remote exists and points to the expected URL
if git remote get-url "$UPSTREAM_REMOTE" >/dev/null 2>&1; then
  CURRENT_URL="$(git remote get-url "$UPSTREAM_REMOTE")"
  if [[ "$CURRENT_URL" != "$UPSTREAM_URL" ]]; then
    echo "Warning: remote '$UPSTREAM_REMOTE' points to:"
    echo "  $CURRENT_URL"
    echo "Expected:"
    echo "  $UPSTREAM_URL"
    echo "Abort to avoid syncing against the wrong upstream."
    exit 1
  fi
else
  echo "Adding upstream remote '$UPSTREAM_REMOTE' -> $UPSTREAM_URL"
  git remote add "$UPSTREAM_REMOTE" "$UPSTREAM_URL"
fi

# 6. Fetch upstream
echo "Fetching from '$UPSTREAM_REMOTE'..."
git fetch "$UPSTREAM_REMOTE"

# 7. Ensure branch exists on upstream
if ! git rev-parse --verify "$UPSTREAM_REMOTE/$BRANCH" >/dev/null 2>&1; then
  echo "Error: branch '$BRANCH' does not exist on remote '$UPSTREAM_REMOTE'."
  exit 1
fi

# 8. Reset local branch to upstream branch
echo "Resetting local '$BRANCH' to '$UPSTREAM_REMOTE/$BRANCH'..."
git reset --hard "$UPSTREAM_REMOTE/$BRANCH"

# 9. Determine files that existed only in backup (A in diff HEAD..BACKUP_TAG)
TMPFILE="$(mktemp /tmp/gfm_local_only_files.XXXXXX)"

git diff --name-status HEAD "$BACKUP_TAG" \
  | awk '$1=="A" {print $2}' > "$TMPFILE"

if [[ ! -s "$TMPFILE" ]]; then
  echo "No local-only deleted files to restore."
  rm -f "$TMPFILE"
  echo
  echo "Local '$BRANCH' is now identical to '$UPSTREAM_REMOTE/$BRANCH'."
  if [[ $DO_PUSH -eq 1 ]]; then
    echo "Pushing to origin..."
    git push origin "$BRANCH"
    echo "Push completed."
  else
    echo "If you want to publish this state, run:"
    echo "  git push origin $BRANCH"
  fi
  exit 0
fi

echo "Restoring the following local-only deleted files from backup tag '$BACKUP_TAG':"
cat "$TMPFILE"
echo

# 10. Restore files from backup tag
xargs git checkout "$BACKUP_TAG" -- < "$TMPFILE"
rm -f "$TMPFILE"

# 11. Commit restored files
git add -A
git commit -m "Restore local-only files after syncing with upstream ($BACKUP_TAG)"

echo
echo "Sync completed."
echo "Your branch '$BRANCH' now contains:"
echo "  - all commits from '$UPSTREAM_REMOTE/$BRANCH'"
echo "  - plus one extra commit restoring files that upstream deleted but existed in your previous state."
echo

if [[ $DO_PUSH -eq 1 ]]; then
  echo "Pushing to origin..."
  git push origin "$BRANCH"
  echo "Push completed."
else
  echo "Review the changes with:"
  echo "  git status"
  echo "  git diff $UPSTREAM_REMOTE/$BRANCH"
  echo
  echo "If everything looks good, publish with:"
  echo "  git push origin $BRANCH"
fi
