"""
tradingview_launcher.py — ensures TradingView Desktop is running
and routed through one of TradeSynchronizer's mitmproxy engines.

Three input states (× the target port we want):
  1. TradingView not running             → launch with --proxy-server
  2. TradingView running with right port → nothing to do
  3. TradingView running with wrong port → quit, wait, relaunch

Patterned after myTradingGuardMacOs/tradingview_launcher.py, with
two twists specific to TradeSynchronizer:

  * We have TWO engines (LIVE on 8080, DEMO on 8081). `_has_proxy()`
    must distinguish "TV is on the proxy I asked for" from "TV is
    on the OTHER engine's proxy" — the latter is a mismatch we
    actively want to fix.

  * The proxy subprocess takes ~1 s to bind its port after the
    engine's STATE_RUNNING transition. We wait for the port to be
    accepting connections before launching TV — otherwise TV's
    first request fails-with-no-retry and the user sees a blank
    chart with no obvious reason.

`--ignore-certificate-errors` is passed alongside `--proxy-server`
as a safety net: even if the mitmproxy CA isn't installed in the
system keychain (the recommended path; see
scripts/install_ca_cert.sh), TV will still accept the proxy's
self-signed certs. Less secure in theory (TV will accept ANY cert,
not just mitmproxy's), but on a localhost-only proxy used by one
trader on their own Mac, the practical risk is nil and the UX
recovery is huge.
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import time
from typing import Optional


logger = logging.getLogger("tradesync.tv_launcher")


TRADINGVIEW_APP_BUNDLE = "/Applications/TradingView.app"
TRADINGVIEW_BINARY     = f"{TRADINGVIEW_APP_BUNDLE}/Contents/MacOS/TradingView"
PROCESS_NAME           = "TradingView"

# Substring we look for in the running TV's argv to confirm it's
# attached to OUR proxy. We grep for the full host:port so two
# engines can coexist (TV bound to :8080 is NOT "ok" for the DEMO
# engine on :8081).
def _expected_arg(port: int) -> str:
    return f"--proxy-server=127.0.0.1:{port}"


QUIT_TIMEOUT_SEC = 8        # graceful quit budget before SIGKILL
PORT_WAIT_TIMEOUT_SEC = 8   # wait for proxy port before launching TV


# ── installation / process queries ─────────────────────────────────── #

def is_installed() -> bool:
    """Returns True iff TradingView Desktop is installed at the
    canonical /Applications path."""
    return os.path.isfile(TRADINGVIEW_BINARY)


def is_running() -> bool:
    """True if there's at least one TradingView process running."""
    return subprocess.run(
        ["pgrep", "-x", PROCESS_NAME],
        capture_output=True,
    ).returncode == 0


def running_proxy_port() -> Optional[int]:
    """
    If TradingView is currently running with a --proxy-server flag
    that resolves to 127.0.0.1:<port>, return <port>. Otherwise None
    (TV not running, or running without a proxy flag, or with a
    non-loopback proxy we don't manage).
    """
    if not is_running():
        return None
    result = subprocess.run(
        ["ps", "-axww", "-o", "command"],
        capture_output=True, text=True,
    )
    for line in result.stdout.splitlines():
        # Skip helper processes: only the main "TradingView" binary
        # carries the --proxy-server flag in its argv. The helpers
        # inherit nothing of the sort.
        if "/Contents/MacOS/TradingView" not in line:
            continue
        if "--proxy-server=" not in line:
            continue
        # Extract the host:port that follows --proxy-server=
        for token in line.split():
            if token.startswith("--proxy-server="):
                value = token.split("=", 1)[1]
                # Strip optional protocol prefixes ("http=", "https=")
                # if present — we accept both `--proxy-server=h:p`
                # and `--proxy-server=https=h:p` for resilience.
                if "=" in value:
                    value = value.rsplit("=", 1)[1]
                if value.startswith("127.0.0.1:"):
                    try:
                        return int(value.split(":", 1)[1])
                    except ValueError:
                        return None
    return None


def _wait_for_port(port: int, *, host="127.0.0.1",
                   timeout: float = PORT_WAIT_TIMEOUT_SEC) -> bool:
    """Block until something is accepting TCP on host:port, or
    timeout elapses. Returns True iff the port became reachable
    within the budget."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except (OSError, socket.timeout):
            time.sleep(0.2)
    return False


# ── lifecycle actions ──────────────────────────────────────────────── #

def _launch(port: int) -> None:
    """Spawn TradingView with the right --proxy-server flag. Caller
    must have already confirmed TV is NOT running (otherwise this
    creates two parallel instances)."""
    if not is_installed():
        raise FileNotFoundError(
            f"TradingView not found at {TRADINGVIEW_APP_BUNDLE}. Install "
            f"it from https://www.tradingview.com/desktop/ first."
        )
    subprocess.Popen(
        [
            TRADINGVIEW_BINARY,
            _expected_arg(port),
            "--ignore-certificate-errors",   # safety net if CA not trusted
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        # Detach from the Python parent so quitting TradeSynchronizer
        # doesn't take TradingView down with it.
        start_new_session=True,
    )


def _quit_tradingview() -> None:
    """Graceful AppleScript quit, then SIGKILL fallback after
    QUIT_TIMEOUT_SEC."""
    subprocess.run(
        ["osascript", "-e", 'quit app "TradingView"'],
        capture_output=True,
    )
    for _ in range(QUIT_TIMEOUT_SEC):
        time.sleep(1)
        if not is_running():
            return
    # Didn't quit gracefully — force.
    subprocess.run(["pkill", "-x", PROCESS_NAME], capture_output=True)
    time.sleep(1)


# ── public API ─────────────────────────────────────────────────────── #

def ensure_tradingview_via_proxy(port: int, *,
                                 wait_for_proxy: bool = True) -> str:
    """
    Reconcile TradingView's state with the desired proxy port.

    Returns one of:
        "not_installed"     — TV isn't at /Applications, we give up
        "proxy_not_ready"   — wait_for_proxy was True and the port
                              never came up; we did NOT touch TV
        "launched"          — TV wasn't running, we started it
        "already_proxied"   — TV already running with the right port
        "restarted"         — TV was running with the wrong port (or
                              no port); we quit & relaunched it

    The function is idempotent and safe to call multiple times.

    Args:
        port: the loopback port to attach TV to (LIVE=8080,
              DEMO=8081 in TradeSynchronizer's defaults).
        wait_for_proxy: if True, block up to PORT_WAIT_TIMEOUT_SEC
                        for `127.0.0.1:<port>` to accept connections
                        BEFORE launching TV. Avoids the race where
                        TV starts faster than the mitmproxy
                        subprocess and its first request fails.
    """
    if not is_installed():
        logger.warning(
            "TradingView Desktop is not installed at %s — skipping "
            "auto-launch. Install it from "
            "https://www.tradingview.com/desktop/ when ready.",
            TRADINGVIEW_APP_BUNDLE,
        )
        return "not_installed"

    if wait_for_proxy and not _wait_for_port(port):
        logger.warning(
            "Proxy on 127.0.0.1:%d isn't accepting connections after "
            "%ds — not launching TradingView. Start the engine first.",
            port, PORT_WAIT_TIMEOUT_SEC,
        )
        return "proxy_not_ready"

    current_port = running_proxy_port()

    if not is_running():
        logger.info("TradingView not running — launching with --proxy-server=127.0.0.1:%d",
                    port)
        _launch(port)
        return "launched"

    if current_port == port:
        logger.info("TradingView already running with --proxy-server=127.0.0.1:%d ✓",
                    port)
        return "already_proxied"

    # Either no proxy (current_port is None) or wrong port → restart.
    if current_port is None:
        logger.info("TradingView running without proxy — restarting "
                    "with --proxy-server=127.0.0.1:%d", port)
    else:
        logger.info("TradingView running with wrong proxy port "
                    "(:%d instead of :%d) — restarting",
                    current_port, port)
    _quit_tradingview()
    _launch(port)
    return "restarted"
