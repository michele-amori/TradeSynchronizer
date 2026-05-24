"""
Pre-flight checks run at engine startup.

These are non-fatal: they warn loudly when something is misconfigured
in the host environment so the user gets actionable feedback in the
log instead of mysterious downstream errors (e.g. obscure TLS
verification failures from TradingView when the mitmproxy CA isn't
trusted).

Each check function returns True on success and logs a warning on
failure; main.py wires them to run after _setup_logging so the
warnings land in both stdout and the rotating log file.
"""

from __future__ import annotations

import logging
import platform
import subprocess
from pathlib import Path


logger = logging.getLogger("tradesync.preflight")


def check_mitmproxy_ca_trusted() -> bool:
    """
    On macOS, verify that mitmproxy's CA cert is installed in the
    System keychain. If it's not, TradingView Desktop will refuse
    TLS to api.ibkr.com when routed through the proxy, and the user
    will see only a generic SSL error inside TradingView with no
    obvious cause.

    Returns True if the cert is present (or if we're not on macOS
    so the check doesn't apply); False with a logged warning
    otherwise.
    """
    if platform.system() != "Darwin":
        # On non-macOS platforms each distro has its own trust store
        # workflow; we don't second-guess it.
        return True

    # `security find-certificate -c mitmproxy <keychain>` exits 0 iff
    # a cert with that common name is in the keychain.
    try:
        result = subprocess.run(
            ["security", "find-certificate",
             "-c", "mitmproxy",
             "/Library/Keychains/System.keychain"],
            capture_output=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        # `security` should always be on macOS. If it isn't or it
        # hangs, log a soft note and don't block startup.
        logger.warning(
            "Could not query system keychain for the mitmproxy CA (%s) "
            "— skipping the TLS-trust pre-flight check.", e,
        )
        return True

    if result.returncode == 0:
        logger.info("Pre-flight: mitmproxy CA is trusted in the system keychain.")
        return True

    # Help the user understand what to do, exactly.
    ca_path = Path("~/.mitmproxy/mitmproxy-ca-cert.pem").expanduser()
    logger.warning(
        "⚠ Pre-flight: mitmproxy CA is NOT installed in the system keychain. "
        "TradingView Desktop will refuse TLS to api.ibkr.com when routed "
        "through the proxy, and no orders will be intercepted.\n"
        "   Fix this once, from the project root:\n"
        "       ./scripts/install_ca_cert.sh\n"
        "   The CA file lives at: %s",
        ca_path,
    )
    return False


def run_all() -> None:
    """Run every pre-flight check. Failures only warn; nothing aborts."""
    check_mitmproxy_ca_trusted()
