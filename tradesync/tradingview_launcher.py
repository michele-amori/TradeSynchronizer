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

from .platform_util import is_apple_silicon_hardware

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


# Domains TV should reach DIRECTLY, never through our mitmproxy.
# This is critical for performance: forcing TV chart-data WebSocket
# frames and CDN asset downloads through mitmproxy's asyncio event
# loop is a hard bottleneck — TV becomes essentially unusable even
# when mitmproxy is doing raw TCP forwarding (ignore_hosts) for
# those flows. By passing this list to TV's --proxy-bypass-list
# flag (Chromium feature; TV Desktop is a Chromium shell), those
# connections never touch the proxy at all — TV opens direct TCP
# sockets to the upstream server like there's no proxy configured.
#
# Pattern syntax (Chromium proxy bypass rules):
#   "*tradingview.com" matches both the apex (tradingview.com) and
#   every subdomain (www.tradingview.com, prodata.tradingview.com,
#   charts-storage.tradingview.com, etc.) without us having to
#   maintain a list of TV's internal CDN hostnames.
#
# Anything NOT in this list still goes through --proxy-server,
# which means api.ibkr.com — the one hostname we actually need to
# intercept for IBKR order replication — keeps going through the
# proxy and getting MITM'd as before.
PROXY_BYPASS_DOMAINS = ";".join([
    "*tradingview.com",
    "*google-analytics.com",
    "*googleapis.com",
    "*gstatic.com",
    "*google.com",
    "*doubleclick.net",
    "*cloudflare.com",
    "*amazonaws.com",
    "*cloudfront.net",
])


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
    # Note: previously we passed `--ignore-certificate-errors` as a
    # safety net in case the mitmproxy CA wasn't installed in the
    # system keychain. Two problems with that:
    #   (1) Recent Chromium versions ignore that flag in stable
    #       builds unless --test-type is also set, so it wasn't
    #       even doing what we hoped on most users' machines.
    #   (2) When the flag IS honoured, it appears to interact badly
    #       with --proxy-bypass-list: TradingView ends up in a
    #       perpetual "loading" state with a black UI, even though
    #       all the network calls (visible via lsof -i) succeed
    #       and the proxy never sees the TV traffic.
    # We rely on the system keychain install of the mitmproxy CA
    # (preflight check at engine startup verifies it) and drop the
    # certificate-error override entirely.
    argv: list[str] = [
        TRADINGVIEW_BINARY,
        _expected_arg(port),
        f"--proxy-bypass-list={PROXY_BYPASS_DOMAINS}",
    ]
    # Force arm64 on Apple Silicon. TradingView Desktop is a
    # universal binary; when macOS picks the x86_64 slice (which it
    # often does when TV is spawned by a subprocess of a process
    # itself running under Rosetta — i.e. our GUI), the whole
    # Chromium engine runs under Rosetta translation and chart-data
    # rendering becomes ~3x slower. Verified empirically: TV under
    # Rosetta = chart almost frozen; same TV, same flags, same
    # mitmproxy config but launched via `arch -arm64` = fluid.
    if is_apple_silicon_hardware():
        argv = ["/usr/bin/arch", "-arm64"] + argv
    subprocess.Popen(
        argv,
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
                                 wait_for_proxy: bool = True,
                                 force_restart: bool = True) -> str:
    """
    Reconcile TradingView's state with the desired proxy port.

    Returns one of:
        "not_installed"     — TV isn't at /Applications, we give up
        "proxy_not_ready"   — wait_for_proxy was True and the port
                              never came up; we did NOT touch TV
        "launched"          — TV wasn't running, we started it
        "already_proxied"   — TV already running with the right port
                              (only possible when force_restart=False)
        "restarted"         — TV was running with the wrong port (or
                              no port, or force_restart=True); we
                              quit & relaunched it

    The function is idempotent and safe to call multiple times.

    Args:
        port: the loopback port to attach TV to (LIVE=8080,
              DEMO=8081 in TradeSynchronizer's defaults).
        wait_for_proxy: if True, block up to PORT_WAIT_TIMEOUT_SEC
                        for `127.0.0.1:<port>` to accept connections
                        BEFORE launching TV. Avoids the race where
                        TV starts faster than the mitmproxy
                        subprocess and its first request fails.
        force_restart: if True (default), always quit and relaunch
                        TradingView even when it's "already correctly
                        proxied". Observed in production: when the
                        previous engine session terminated abruptly
                        (Stop button → mitmproxy SIGTERM → all live
                        TV↔proxy TLS sockets get RST'd mid-stream),
                        TV's internal retry/reconnect machinery ends
                        up wedged, and a fresh engine listener on
                        the same port is not enough to recover — the
                        TV UI behaves as if everything is hung.
                        Restarting TV reliably clears that state.
                        Set False if a caller wants the older lenient
                        "leave TV alone if it's already wired up"
                        behaviour (e.g. tests, or a hot-reload of
                        engine settings that doesn't touch the port).
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

    if current_port == port and not force_restart:
        logger.info("TradingView already running with --proxy-server=127.0.0.1:%d ✓",
                    port)
        return "already_proxied"

    # Either no proxy, wrong port, or force_restart=True → restart.
    if current_port is None:
        logger.info("TradingView running without proxy — restarting "
                    "with --proxy-server=127.0.0.1:%d", port)
    elif current_port != port:
        logger.info("TradingView running with wrong proxy port "
                    "(:%d instead of :%d) — restarting",
                    current_port, port)
    else:
        # current_port == port and force_restart is True
        logger.info("TradingView running with the right proxy port "
                    "but forcing a restart to clear any stale "
                    "post-disconnect retry state from a previous "
                    "engine session.")
    _quit_tradingview()
    _launch(port)
    return "restarted"
