"""
ibkr_gateway_launcher — open IB Gateway automatically when an IBKR
follower needs it, WITHOUT ever disturbing an already-running session.

Why this exists
---------------
When a replication pair has IBKR as its FOLLOWER (the Tradovate→IBKR
direction), the engine needs a local IB Gateway running and logged in
so IbkrApiClient can place orders. This mirrors what
scripts/launch-tradingview.sh does for TradingView — open the
dependency for the user instead of making them hunt for it.

The CRITICAL difference from the TradingView launcher
-----------------------------------------------------
TradingView is *quit and relaunched* so it picks up the --proxy-server
flag. IB Gateway must NEVER be restarted that way: a running Gateway
holds an authenticated session (the daily 2FA login). Restarting it
would drop that session, force a fresh login, and kill any live API
connection. So the rule here is the opposite of the TV launcher:

    if Gateway is already running → DO NOTHING. Leave it untouched.
    if Gateway is NOT running     → open the app (which lands the user
                                     on the login screen) and tell them
                                     they must finish the 2FA login
                                     before the API is ready.

Opening the app is therefore only a convenience that saves the user a
trip to Finder; it does NOT make the API ready on its own — only the
user completing the login does. The return status makes that explicit
so the caller can show the right message.

Detection vs action are separated so the detection helpers are unit-
testable without actually launching anything.
"""

from __future__ import annotations

import logging
import socket
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import List, Optional


logger = logging.getLogger("tradesync.ibkr_gateway")


# Directories where IB Gateway installs on macOS. The bundle name
# carries the version (e.g. "IB Gateway 10.45.app"), so we glob rather
# than hard-code — an update to 10.46 must not break discovery.
#
# IBKR's installer nests the .app one level deep inside a versioned
# install FOLDER, e.g.:
#     ~/Applications/IB Gateway 10.45/IB Gateway 10.45.app
# so we search both the dir itself and one level down. We also EXCLUDE
# the "… Uninstaller.app" that ships alongside it.
_SEARCH_DIRS = (
    Path.home() / "Applications",
    Path("/Applications"),
)
_BUNDLE_GLOBS = ("IB Gateway*.app", "IB Gateway*/IB Gateway*.app")
_EXCLUDE_SUBSTR = "Uninstaller"

# Substring that identifies the Gateway process in `pgrep -lf`. The
# JavaApplicationStub command line contains the bundle path, which
# includes "IB Gateway".
_PROCESS_PATTERN = "IB Gateway"


class GatewayLaunchStatus(Enum):
    ALREADY_RUNNING = "already_running"   # left untouched — nothing to do
    LAUNCHED = "launched"                 # we opened it; user must log in
    NOT_FOUND = "not_found"               # app isn't installed
    LAUNCH_FAILED = "launch_failed"       # `open` returned an error


@dataclass
class GatewayStatus:
    status: GatewayLaunchStatus
    app_path: Optional[Path] = None
    message: str = ""


def find_gateway_app(search_dirs: Optional[List[Path]] = None) -> Optional[Path]:
    """Locate the IB Gateway .app bundle, newest version first. Returns
    the Path or None if not installed."""
    dirs = search_dirs if search_dirs is not None else _SEARCH_DIRS
    matches: List[Path] = []
    for d in dirs:
        for pattern in _BUNDLE_GLOBS:
            try:
                matches.extend(d.glob(pattern))
            except OSError:
                continue
    # Drop the uninstaller bundle that ships next to the real app.
    matches = [m for m in matches if _EXCLUDE_SUBSTR not in m.name]
    if not matches:
        return None
    # Prefer the highest version. Version strings from IBKR are
    # zero-aligned in practice (10.45, 10.46…), so a lexical sort on the
    # bundle name orders them correctly; take the last.
    return sorted(set(matches), key=lambda p: p.name)[-1]


def is_gateway_running(
    pgrep_runner: Optional[callable] = None,
) -> bool:
    """True if an IB Gateway process is currently running. `pgrep_runner`
    is injectable for testing; by default shells out to pgrep."""
    runner = pgrep_runner or _default_pgrep
    try:
        return runner(_PROCESS_PATTERN)
    except Exception:  # noqa: BLE001 - detection must never raise
        logger.debug("pgrep for Gateway failed", exc_info=True)
        return False


def _default_pgrep(pattern: str) -> bool:
    result = subprocess.run(
        ["pgrep", "-f", pattern],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def is_api_port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    """True if something is listening on the Gateway API host:port. This
    is the strongest 'ready' signal — it means the Gateway is up AND the
    API socket is accepting connections (i.e. the user has logged in and
    API access is enabled)."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def ensure_gateway_running(
    *,
    api_host: str = "127.0.0.1",
    api_port: int = 4002,
    find_app: Optional[callable] = None,
    running_check: Optional[callable] = None,
    opener: Optional[callable] = None,
) -> GatewayStatus:
    """Make sure IB Gateway is at least OPEN, without ever restarting a
    running instance.

    The dependencies (find_app / running_check / opener) are injectable
    so this is fully unit-testable without touching the real system.

    Returns a GatewayStatus describing what happened and a message
    suitable for surfacing to the user.
    """
    running = (running_check or is_gateway_running)
    # 1. If the API port is already open, the Gateway is up AND logged
    #    in — the ideal state. Never touch it.
    if is_api_port_open(api_host, api_port):
        return GatewayStatus(
            GatewayLaunchStatus.ALREADY_RUNNING,
            message=(f"IB Gateway is already running and its API is "
                     f"listening on {api_host}:{api_port} — left untouched."))

    # 2. The process may be running but not yet logged in (API port
    #    closed). Still must NOT restart it — the user is mid-login or
    #    needs to finish it. Treat as already-running, but tell them the
    #    API isn't ready yet.
    if running():
        return GatewayStatus(
            GatewayLaunchStatus.ALREADY_RUNNING,
            message=(f"IB Gateway is already open but its API isn't "
                     f"listening on {api_host}:{api_port} yet. Finish "
                     f"logging in (and make sure API access is enabled) — "
                     f"the engine will connect once the port is up."))

    # 3. Not running at all → open it for the user.
    app = (find_app or find_gateway_app)()
    if app is None:
        return GatewayStatus(
            GatewayLaunchStatus.NOT_FOUND,
            message=("IB Gateway isn't installed where expected "
                     "(~/Applications or /Applications). Install it from "
                     "interactivebrokers.com, or open it manually before "
                     "starting a Tradovate→IBKR pipeline."))

    open_fn = opener or _default_open
    try:
        open_fn(app)
    except Exception as e:  # noqa: BLE001
        logger.exception("Failed to open IB Gateway")
        return GatewayStatus(
            GatewayLaunchStatus.LAUNCH_FAILED, app_path=app,
            message=f"Tried to open IB Gateway but the open command "
                    f"failed: {e}")

    return GatewayStatus(
        GatewayLaunchStatus.LAUNCHED, app_path=app,
        message=(f"Opened {app.name}. Finish the daily login (2FA) and "
                 f"make sure API access is enabled on "
                 f"{api_host}:{api_port}; the engine will connect to the "
                 f"Gateway once you're logged in. Your session is NOT "
                 f"restarted on subsequent runs — a logged-in Gateway is "
                 f"always left untouched."))


def _default_open(app: Path) -> None:
    subprocess.run(["open", str(app)], check=True)
