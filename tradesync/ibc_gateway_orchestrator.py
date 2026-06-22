"""
ibc_gateway_orchestrator — start exactly the IB Gateway instances an
environment's ENABLED pairs need, on the right ports, one at a time.

The problem this solves
-----------------------
A multi-follower setup needs ONE IB Gateway per distinct follower port
(e.g. 4002 for one paper account, 4003 for another), each logged into a
DIFFERENT IBKR user so it exposes the right account. The old
ensure_gateway_running only ever opened the single config-level default
gateway, so a second follower's port was never brought up — its pipeline
failed to connect.

This module instead:
  1. takes the set of ports the enabled pairs actually require,
  2. looks each port up in a port→launch-command map
     (config/ibc_gateways.json), and
  3. for every required port whose API socket isn't already listening,
     runs its launch command and WAITS for the port to come up before
     moving to the next one — so any login/2FA prompt is unambiguous and
     two Gateways never race each other on startup.

A port that's already listening is left completely untouched — we never
restart a logged-in Gateway (that would drop its authenticated session),
exactly like ensure_gateway_running.

What it does NOT do
-------------------
It does not know which IBKR account a login exposes — only which command
serves which port. If a launched Gateway logs into a user that exposes a
DIFFERENT account than the pair expects, the follower's own connect-time
guardrail (ibkr_follower_endpoint) is what catches it and refuses. This
module's job is purely "make the right ports listen".

The map and the socket probe are injectable so this is unit-testable
without launching anything or opening real sockets.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .ibkr_gateway_launcher import is_api_port_open

logger = logging.getLogger("tradesync.ibc_orchestrator")


class PortStartResult(Enum):
    ALREADY_UP = "already_up"        # port was already listening — untouched
    LAUNCHED_UP = "launched_up"      # we launched it and it came up
    LAUNCHED_TIMEOUT = "launched_timeout"  # launched but port never opened
    NO_MAPPING = "no_mapping"        # no launch command for this port
    LAUNCH_FAILED = "launch_failed"  # the launch command itself errored


@dataclass
class PortOutcome:
    port: int
    result: PortStartResult
    message: str


@dataclass
class GatewaySpec:
    """How to launch the Gateway that serves one API port."""
    port: int
    command: List[str]         # argv, e.g. ["/Users/x/ibc-demo/start-gateway.sh", "A"]
    login: str = ""            # informational only (for log messages)

    @classmethod
    def from_dict(cls, port: int, d: dict) -> "GatewaySpec":
        if not isinstance(d, dict):
            raise ValueError(f"gateway spec for port {port} must be an object")
        # Accept either an explicit argv list ("command": [...]) or the
        # convenience script+arg form ("script": "...", "arg": "A").
        if "command" in d:
            cmd = list(d["command"])
        elif "script" in d:
            script = os.path.expanduser(str(d["script"]))
            cmd = [script]
            if d.get("arg"):
                cmd.append(str(d["arg"]))
        else:
            raise ValueError(
                f"gateway spec for port {port} needs 'command' or 'script'")
        return cls(port=port, command=cmd, login=str(d.get("login", "")))


def load_gateway_map(path: Path) -> Dict[int, GatewaySpec]:
    """Load config/ibc_gateways.json → {port: GatewaySpec}. A missing
    file yields an empty map (no auto-launch — the user opens Gateways
    by hand, as before). Keys in the file are port strings."""
    if not path.exists():
        logger.info("No IBC gateway map at %s — auto-launch disabled", path)
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"could not read {path}: {e}") from e
    raw = data.get("gateways", data)  # allow either {"gateways": {...}} or {...}
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected an object of port→spec")
    out: Dict[int, GatewaySpec] = {}
    for port_str, spec in raw.items():
        if port_str == "schema":
            continue
        try:
            port = int(port_str)
        except (TypeError, ValueError):
            raise ValueError(f"{path}: {port_str!r} is not a valid port")
        out[port] = GatewaySpec.from_dict(port, spec)
    return out


def default_gateway_map_path(project_root: Path) -> Path:
    return project_root / "config" / "ibc_gateways.json"


def _default_launch(spec: GatewaySpec) -> None:
    """Launch a Gateway detached so it keeps running after the engine
    process that started it. stdout/stderr go to the Gateway's own IBC
    logs, not ours."""
    subprocess.Popen(  # noqa: S603 - command comes from the user's own map
        spec.command,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def ensure_ports_listening(
    required_ports: List[int],
    gateway_map: Dict[int, GatewaySpec],
    *,
    host: str = "127.0.0.1",
    port_open_check: Optional[Callable[[str, int], bool]] = None,
    launcher: Optional[Callable[[GatewaySpec], None]] = None,
    wait_timeout: float = 90.0,
    poll_interval: float = 2.0,
    sleeper: Callable[[float], None] = time.sleep,
) -> List[PortOutcome]:
    """Bring up every required port that isn't already listening, ONE AT
    A TIME (launch, then wait for that port before the next).

    Dependencies (port_open_check / launcher / sleeper) are injectable so
    this is fully unit-testable without sockets or subprocesses.

    Returns one PortOutcome per required port, in the order processed.
    """
    is_open = port_open_check or (lambda h, p: is_api_port_open(h, p))
    launch = launcher or _default_launch

    outcomes: List[PortOutcome] = []
    # De-dup while preserving order: two followers may share a port.
    seen = set()
    ports = [p for p in required_ports if not (p in seen or seen.add(p))]

    for port in ports:
        if is_open(host, port):
            outcomes.append(PortOutcome(
                port, PortStartResult.ALREADY_UP,
                f"Gateway API on {host}:{port} already listening — left "
                f"untouched."))
            continue

        spec = gateway_map.get(port)
        if spec is None:
            outcomes.append(PortOutcome(
                port, PortStartResult.NO_MAPPING,
                f"No launch command mapped for port {port}. Add it to "
                f"config/ibc_gateways.json, or open that Gateway by hand."))
            continue

        who = f" (login {spec.login})" if spec.login else ""
        logger.info("Launching IB Gateway for port %d%s: %s",
                    port, who, " ".join(spec.command))
        try:
            launch(spec)
        except Exception as e:  # noqa: BLE001 - report, don't crash startup
            logger.exception("Failed to launch Gateway for port %d", port)
            outcomes.append(PortOutcome(
                port, PortStartResult.LAUNCH_FAILED,
                f"Tried to launch the Gateway for port {port} but the "
                f"command failed: {e}"))
            continue

        # Wait for THIS port before moving on, so logins don't overlap.
        deadline = time.monotonic() + wait_timeout
        came_up = False
        while time.monotonic() < deadline:
            if is_open(host, port):
                came_up = True
                break
            sleeper(poll_interval)

        if came_up:
            outcomes.append(PortOutcome(
                port, PortStartResult.LAUNCHED_UP,
                f"Gateway for port {port}{who} is up and its API is "
                f"listening."))
        else:
            outcomes.append(PortOutcome(
                port, PortStartResult.LAUNCHED_TIMEOUT,
                f"Launched the Gateway for port {port}{who} but its API "
                f"didn't start listening within {wait_timeout:.0f}s. If it "
                f"needs a manual login/2FA, finish it; the engine will "
                f"connect once the port is up."))
    return outcomes


def required_ports_for(replication_config, default_gateway) -> List[int]:
    """The distinct API ports the ENABLED IBKR-follower pairs need, in
    first-seen order. Each pair resolves to its own gateway (its override
    or the config default)."""
    ports: List[int] = []
    for p in replication_config.enabled_pairs:
        if p.follower.broker != "ibkr":
            continue
        gw = p.resolve_ibkr_gateway(default_gateway)
        if gw.port not in ports:
            ports.append(gw.port)
    return ports
