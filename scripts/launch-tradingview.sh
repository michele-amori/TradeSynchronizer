#!/usr/bin/env bash
#
# launch-tradingview.sh — quit any running TradingView Desktop
# instance and relaunch it with --proxy-server pointing at the
# requested TradeSynchronizer engine.
#
# Usage:
#     ./scripts/launch-tradingview.sh demo      # → :8081 (default)
#     ./scripts/launch-tradingview.sh live      # → :8080
#     ./scripts/launch-tradingview.sh --check   # diagnostics only
#
# Why a wrapper?
# --------------
# `open -a TradingView --args --proxy-server=...` silently ignores
# the flag if TradingView is already running (LaunchServices brings
# the existing instance to the front instead of relaunching it).
# This wrapper quits the running instance first, then re-opens
# with the flag — so the proxy actually takes effect.
#
set -euo pipefail

TV_APP="/Applications/TradingView.app"
TV_PROC_NAME="TradingView"
HOST="127.0.0.1"

# ── argument parsing ───────────────────────────────────────────────
TARGET="${1:-demo}"
case "$TARGET" in
  live)    PORT=8080 ;;
  demo)    PORT=8081 ;;
  --check) PORT="" ;;
  *)
    echo "Usage: $0 [live|demo|--check]" >&2
    exit 2
    ;;
esac

# ── diagnostics-only mode ──────────────────────────────────────────
if [[ "$TARGET" == "--check" ]]; then
  echo "TradingView app:   $([[ -d "$TV_APP" ]] && echo "✓ $TV_APP" || echo "❌ NOT FOUND")"
  if pgrep -lf "$TV_PROC_NAME" >/dev/null 2>&1; then
    echo "TV process:        ✓ running (would be re-launched on next invocation)"
  else
    echo "TV process:        — not running (would start fresh)"
  fi
  echo -n "mitmproxy CA:      "
  if security find-certificate -c mitmproxy /Library/Keychains/System.keychain >/dev/null 2>&1; then
    echo "✓ trusted (TV will accept proxy TLS)"
  else
    echo "❌ NOT installed — run ./scripts/install_ca_cert.sh FIRST,"
    echo "                   or TV will refuse TLS for every site."
  fi
  exit 0
fi

# ── pre-flight: app exists ─────────────────────────────────────────
if [[ ! -d "$TV_APP" ]]; then
  echo "❌ TradingView is not installed at $TV_APP." >&2
  echo "   Install it from https://www.tradingview.com/desktop/ first." >&2
  exit 1
fi

# ── pre-flight: CA trust ───────────────────────────────────────────
# Without the CA trusted system-wide, TradingView will refuse TLS for
# every site once it routes through mitmproxy. Refuse to launch
# rather than leave the user staring at a broken TV with no clue why.
if ! security find-certificate -c mitmproxy /Library/Keychains/System.keychain \
        >/dev/null 2>&1; then
  cat >&2 <<EOF
❌ mitmproxy CA is NOT installed in the system keychain.
   Without it, TradingView will refuse the proxy's TLS certificates
   and you'll see a blank screen or 'connection error' inside TV.

   Fix this ONCE before relaunching:
       ./scripts/install_ca_cert.sh

   Then re-run this script.
EOF
  exit 1
fi

# ── pre-flight: warn if the target engine isn't running ────────────
# Best-effort: we just try to connect to the port. If nothing listens,
# the user gets a heads-up — they can still proceed (TV will run
# unproxied for the IBKR endpoint, which means: no replication).
if ! nc -z -G 1 "$HOST" "$PORT" >/dev/null 2>&1; then
  echo "⚠  Warning: nothing seems to be listening on $HOST:$PORT."
  echo "   Launch the $(echo "$TARGET" | tr '[:lower:]' '[:upper:]') engine in TradeSynchronizer first,"
  echo "   otherwise IBKR orders won't be intercepted."
  echo
  read -p "   Proceed anyway? [y/N] " ans
  [[ "$ans" == "y" || "$ans" == "Y" ]] || { echo "Aborted."; exit 1; }
fi

# ── quit existing TV instance ──────────────────────────────────────
if pgrep -lf "$TV_PROC_NAME" >/dev/null 2>&1; then
  echo "ℹ️  TradingView is already running — quitting it so the new"
  echo "   --proxy-server flag actually takes effect…"
  osascript -e 'quit app "TradingView"' || true
  # Wait up to 6 s for the process to actually go away.
  for _ in 1 2 3 4 5 6; do
    pgrep -lf "$TV_PROC_NAME" >/dev/null 2>&1 || break
    sleep 1
  done
  if pgrep -lf "$TV_PROC_NAME" >/dev/null 2>&1; then
    echo "⚠  TradingView didn't quit cleanly. Force-killing…"
    pkill -f "$TV_PROC_NAME" || true
    sleep 1
  fi
fi

# ── relaunch ───────────────────────────────────────────────────────
echo "🚀  Launching TradingView → $(echo "$TARGET" | tr '[:lower:]' '[:upper:]') engine ($HOST:$PORT)…"
open -a "TradingView" --args --proxy-server="${HOST}:${PORT}"
echo "✓  Done. TradingView will route ALL traffic through the proxy."
echo "   Watch the TradeSynchronizer Log tab for incoming orders."
