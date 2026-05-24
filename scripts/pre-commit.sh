#!/usr/bin/env bash
#
# pre-commit.sh — versioned pre-commit hook for TradeSynchronizer.
#
# Install once with:
#   ./scripts/install-hooks.sh
# (it symlinks this file into .git/hooks/pre-commit)
#
# Bypass for a single commit, e.g. when doing a WIP push:
#   git commit --no-verify -m "..."
#
# What this hook enforces, in order:
#
#   1. Tests pass.           BLOCKS on failure.
#   2. .app rebuilds.        BLOCKS on failure.
#   3. README freshness.     WARNS + prompts when code changed but
#                            README.md didn't. Answer 'n' to abort.
#
# All output goes to stderr so commit-message editors don't eat it.
#
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

# Colours for output, but only if stderr is a tty (so CI logs stay clean).
if [[ -t 2 ]]; then
    BOLD='\033[1m'  RED='\033[31m'  GREEN='\033[32m'
    YELLOW='\033[33m'  DIM='\033[2m'  RESET='\033[0m'
else
    BOLD=''  RED=''  GREEN=''  YELLOW=''  DIM=''  RESET=''
fi

log()  { printf '%b%s%b\n' "$BOLD"   "$1" "$RESET" >&2; }
ok()   { printf '%b✓ %s%b\n' "$GREEN" "$1" "$RESET" >&2; }
warn() { printf '%b⚠ %s%b\n' "$YELLOW" "$1" "$RESET" >&2; }
err()  { printf '%b✗ %s%b\n' "$RED"   "$1" "$RESET" >&2; }
dim()  { printf '%b%s%b\n'  "$DIM"   "$1" "$RESET" >&2; }

# Pick the right Python: prefer the project venv so we get the
# packages that match requirements.txt; fall back to PATH python3.
if [[ -x ".venv/bin/python" ]]; then
    PY=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PY="$(command -v python3)"
else
    err "No usable Python found. Activate .venv or install python3."
    exit 1
fi

# ── Step 1: tests ──────────────────────────────────────────────────────── #
log "pre-commit ▸ Running test suite…"
TEST_OUTPUT=$(mktemp)
if "$PY" -m unittest discover tests > "$TEST_OUTPUT" 2>&1; then
    summary=$(tail -3 "$TEST_OUTPUT" | head -1)
    ok "Tests passed — $summary"
    rm -f "$TEST_OUTPUT"
else
    err "Tests FAILED — commit blocked."
    echo "" >&2
    tail -25 "$TEST_OUTPUT" >&2
    rm -f "$TEST_OUTPUT"
    echo "" >&2
    dim "Bypass (NOT recommended) with: git commit --no-verify"
    exit 1
fi

# ── Step 2: rebuild the .app ──────────────────────────────────────────── #
log "pre-commit ▸ Rebuilding TradeSynchronizer.app…"
BUILD_OUTPUT=$(mktemp)
if ./build_app.sh > "$BUILD_OUTPUT" 2>&1; then
    ok "TradeSynchronizer.app rebuild OK"
    rm -f "$BUILD_OUTPUT"
else
    err ".app build FAILED — commit blocked."
    echo "" >&2
    tail -20 "$BUILD_OUTPUT" >&2
    rm -f "$BUILD_OUTPUT"
    exit 1
fi

# ── Step 3: README freshness check ────────────────────────────────────── #
# If the staged diff touches code under tradesync/, scripts/, main.py,
# gui.py, build_app.sh, requirements.txt — but doesn't touch README.md
# — that's *probably* a docs-drift situation. Warn and ask.
STAGED=$(git diff --cached --name-only --diff-filter=ACMR)

code_touched=false
readme_touched=false
while IFS= read -r f; do
    case "$f" in
        README.md)                                readme_touched=true ;;
        tradesync/*|main.py|gui.py|build_app.sh|requirements.txt|scripts/*) \
                                                  code_touched=true ;;
    esac
done <<< "$STAGED"

if $code_touched && ! $readme_touched; then
    warn "Code changed but README.md is unmodified."
    dim "Files touched in this commit:"
    while IFS= read -r f; do dim "    $f" ; done <<< "$STAGED"
    echo "" >&2
    dim "If the change affects user-visible behaviour, update README.md."
    echo "" >&2
    # Read from /dev/tty because stdin is the hook's pipe.
    if [[ -e /dev/tty ]]; then
        printf "%bProceed with commit anyway?%b [y/N] " "$BOLD" "$RESET" >&2
        # Read from /dev/tty because stdin is the hook's pipe.
        read -r ans < /dev/tty || ans=""
        # NOTE: macOS ships bash 3.2 — portable `case` rather than
        # bash-4 ${var,,} lowercase expansion.
        case "$ans" in y|Y|yes|YES) ans=yes ;; *) ans=no ;; esac
        if [[ "$ans" == "no" ]]; then
            err "Aborted by pre-commit (README not updated)."
            dim "Either update README.md or re-commit with: --no-verify"
            exit 1
        fi
        warn "Proceeding without README update — remember to circle back."
    else
        # Non-interactive context (CI, batch). Don't block — just warn.
        warn "Non-interactive shell — proceeding without prompt."
    fi
else
    ok "README freshness check OK"
fi

# ── done ───────────────────────────────────────────────────────────────── #
log "pre-commit ▸ All checks passed."
