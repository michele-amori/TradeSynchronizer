#!/usr/bin/env bash
#
# install-hooks.sh — wire the versioned pre-commit hook into .git/hooks/.
#
# Run ONCE per clone of this repo:
#   ./scripts/install-hooks.sh
#
# The hook itself lives at scripts/pre-commit.sh (versioned, peer-
# reviewed) and gets symlinked into .git/hooks/pre-commit. That way
# any change to the hook is committable and visible in the repo
# history, while git still picks it up automatically.
#
# Idempotent: re-running the installer just refreshes the symlink.
#
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

HOOK_SRC="scripts/pre-commit.sh"
HOOK_DST=".git/hooks/pre-commit"

if [[ ! -f "$HOOK_SRC" ]]; then
    echo "❌ $HOOK_SRC not found. Run this script from the repo root." >&2
    exit 1
fi

if [[ ! -d ".git/hooks" ]]; then
    echo "❌ .git/hooks is missing — is this a git checkout?" >&2
    exit 1
fi

# Make sure the hook itself is executable (file permissions don't
# always survive a fresh checkout).
chmod +x "$HOOK_SRC"

# Remove any existing hook (file or symlink) to avoid the
# "already-exists" failure mode of ln -s.
rm -f "$HOOK_DST"

# Relative symlink so the hook keeps working if the repo is moved.
ln -s "../../$HOOK_SRC" "$HOOK_DST"

echo "✓ Pre-commit hook installed:"
echo "    $HOOK_DST  →  $HOOK_SRC"
echo ""
echo "Every 'git commit' will now run:"
echo "  1. Test suite (blocks on failure)"
echo "  2. .app rebuild (blocks on failure)"
echo "  3. README freshness check (warns if code changed without README)"
echo ""
echo "Bypass for a single commit with:  git commit --no-verify"
