#!/usr/bin/env bash
set -euo pipefail

# Restore dev plugin state on main after a release tag.
#
# Usage:
#   scripts/restore-dev-plugin.sh [release-prep-commit]
#
# If no argument is given, auto-detects the last "prepare plugin for release"
# commit and restores from its parent.
#
# CONTRACT (pkit-hsyi, see punt-kit commit 462c65d): this script stages the
# reverted files; it does NOT commit. The caller (_phase9_post_release in
# punt-kit's release engine) re-stamps the version and commits with hooks
# running.

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# The shippable plugin surface lives under plugin/ so a git-subdir marketplace
# install fetches only that subtree; the plugin root is ${REPO_ROOT}/plugin.
PLUGIN_JSON="${REPO_ROOT}/plugin/.claude-plugin/plugin.json"
COMMANDS_PATHSPEC="plugin/commands/"

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

git -C "$REPO_ROOT" add "$PLUGIN_JSON"

# Restore dev commands only if the parent commit carried that directory. The add
# is inside the guard because outside it there is nothing to stage: an
# unconditional `git add … || true` would swallow a real failure (a checkout that
# silently restored nothing) and report success.
if git -C "$REPO_ROOT" ls-tree "${RELEASE_PREP_COMMIT}^" -- "$COMMANDS_PATHSPEC" | grep -q .; then
  git -C "$REPO_ROOT" checkout "${RELEASE_PREP_COMMIT}^" -- "$COMMANDS_PATHSPEC"
  git -C "$REPO_ROOT" add "$COMMANDS_PATHSPEC"
fi

# Nothing further to do if nothing changed (already in dev state). Otherwise
# leave the restored files staged — see CONTRACT above.
if git -C "$REPO_ROOT" diff --cached --quiet; then
  echo "No changes to restore; working tree already matches dev state."
fi
