#!/usr/bin/env bash
#
# install_ca_cert.sh — install + trust the mitmproxy CA in the macOS
# System keychain so TradingView Desktop accepts the proxy's
# certificates for api.ibkr.com.
#
# Idempotent: safe to re-run. Usage:
#
#   ./scripts/install_ca_cert.sh            install (asks for sudo)
#   ./scripts/install_ca_cert.sh --check    diagnostic only (no changes,
#                                           exit 0 if trusted, 1 otherwise)
#
set -euo pipefail

MITMPROXY_DIR="${HOME}/.mitmproxy"
CA_CERT="${MITMPROXY_DIR}/mitmproxy-ca-cert.pem"
SYSTEM_KEYCHAIN="/Library/Keychains/System.keychain"
CERT_NAME="mitmproxy"

CHECK_ONLY=false
if [[ "${1:-}" == "--check" ]]; then
  CHECK_ONLY=true
elif [[ -n "${1:-}" ]]; then
  echo "Usage: $0 [--check]" >&2
  exit 2
fi

# Refuse to run anywhere but macOS — the `security` command is
# Darwin-only.
if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "❌ This script only runs on macOS. On other platforms, install"
  echo "   mitmproxy's CA via your distro's trust store mechanism."
  exit 1
fi

# ── Step 1: locate mitmdump ──────────────────────────────────────────
if ! command -v mitmdump >/dev/null 2>&1; then
  # Helpful hint when the project's venv is just not active.
  if [[ -x ".venv/bin/mitmdump" ]]; then
    echo "ℹ️  Using mitmdump from .venv/bin (your venv isn't activated)."
    export PATH="$(pwd)/.venv/bin:$PATH"
  else
    echo "❌ mitmdump not found in PATH."
    echo "   Activate the project's venv (source .venv/bin/activate) or"
    echo "   install dependencies (pip install -r requirements.txt)."
    exit 1
  fi
fi

# ── Step 2: ensure the CA file exists ────────────────────────────────
if [[ ! -f "$CA_CERT" ]]; then
  if $CHECK_ONLY; then
    echo "❌ mitmproxy CA not generated yet ($CA_CERT missing)."
    exit 1
  fi
  echo "ℹ️  Generating mitmproxy CA (one-shot, ~3s)..."
  # Boot mitmdump on a sacrificial port; it writes the CA on first run.
  mitmdump --listen-port 18080 -q >/dev/null 2>&1 &
  MITM_PID=$!
  cleanup() { kill "$MITM_PID" 2>/dev/null || true; }
  trap cleanup EXIT
  for _ in 1 2 3 4 5 6 7 8; do
    [[ -f "$CA_CERT" ]] && break
    sleep 0.5
  done
  cleanup
  trap - EXIT
  if [[ ! -f "$CA_CERT" ]]; then
    echo "❌ Failed to generate CA at $CA_CERT — is mitmproxy installed?"
    exit 1
  fi
  echo "   CA generated at $CA_CERT"
fi

# ── Step 3: check whether the CA is already in the system keychain ───
# `security find-certificate` exits 0 if a matching cert is present.
# We don't separately verify the trust bit because the only way the
# cert gets into the System keychain via this script is via
# `add-trusted-cert -r trustRoot`, which establishes the trust.
already_present=false
if security find-certificate -c "$CERT_NAME" "$SYSTEM_KEYCHAIN" \
        >/dev/null 2>&1; then
  already_present=true
fi

if $CHECK_ONLY; then
  if $already_present; then
    echo "✓ mitmproxy CA is installed in $SYSTEM_KEYCHAIN."
    exit 0
  fi
  echo "❌ mitmproxy CA is NOT in the system keychain."
  echo "   Run: $0   (without --check) to install."
  exit 1
fi

if $already_present; then
  echo "✓ mitmproxy CA already installed in $SYSTEM_KEYCHAIN — nothing to do."
  exit 0
fi

# ── Step 4: install + trust ──────────────────────────────────────────
echo "📥 Installing mitmproxy CA into $SYSTEM_KEYCHAIN ..."
echo "   You'll be asked for your macOS password (sudo)."
sudo security add-trusted-cert \
     -d -r trustRoot \
     -k "$SYSTEM_KEYCHAIN" \
     "$CA_CERT"

# ── Step 5: confirm ──────────────────────────────────────────────────
if security find-certificate -c "$CERT_NAME" "$SYSTEM_KEYCHAIN" \
        >/dev/null 2>&1; then
  echo "✓ Done. TradingView Desktop will now accept the proxy's TLS."
  echo "  (Restart TradingView if it was already running.)"
else
  echo "⚠ add-trusted-cert ran without error but the cert isn't visible"
  echo "  in the keychain. Open Keychain Access → System → look for"
  echo "  'mitmproxy' and verify it's there & marked 'Always Trust'."
  exit 2
fi
