#!/usr/bin/env bash
set -euo pipefail

# Restore dev plugin state on main after a release tag.
#
# Usage:
#   scripts/restore-dev-plugin.sh [release-prep-commit]
#
# If no argument is given, auto-detects the last "prepare plugin for release"
# commit and restores from its parent.

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PLUGIN_JSON="${REPO_ROOT}/.claude-plugin/plugin.json"

# Preflight: abort if repo has uncommitted changes
if [[ -n "$(git -C "$REPO_ROOT" status --porcelain -uno)" ]]; then
  echo "Error: repository has uncommitted changes. Commit or stash before running $(basename "$0")." >&2
  exit 1
fi

# Determine the release-prep commit to restore from
RELEASE_PREP_COMMIT="${1:-}"
if [[ -z "$RELEASE_PREP_COMMIT" ]]; then
  RELEASE_PREP_COMMIT="$(git -C "$REPO_ROOT" log -n 1 --grep='prepare plugin for release' --pretty=format:%H || true)"
  if [[ -z "$RELEASE_PREP_COMMIT" ]]; then
    echo "Error: could not find a 'prepare plugin for release' commit. Pass a commit or tag as the first argument." >&2
    exit 1
  fi
fi

echo "Restoring dev state from parent of ${RELEASE_PREP_COMMIT:0:12}"
git -C "$REPO_ROOT" checkout "${RELEASE_PREP_COMMIT}^" -- "$PLUGIN_JSON"

# Restore dev commands if the parent commit had a commands/ directory
if git -C "$REPO_ROOT" ls-tree "${RELEASE_PREP_COMMIT}^" -- commands/ | grep -q .; then
  git -C "$REPO_ROOT" checkout "${RELEASE_PREP_COMMIT}^" -- commands/
fi

git -C "$REPO_ROOT" add "$PLUGIN_JSON"
git -C "$REPO_ROOT" add commands/ 2>/dev/null || true

# Skip commit if nothing changed (already in dev state)
if git -C "$REPO_ROOT" diff --cached --quiet; then
  echo "No changes to restore; working tree already matches dev state."
  exit 0
fi

git -C "$REPO_ROOT" commit --no-verify -m "chore: restore dev plugin state"
