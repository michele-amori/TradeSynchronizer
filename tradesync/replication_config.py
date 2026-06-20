"""
replication_config — load / save / validate config/replication.json,
the file that declares which source→follower replication pairs the
engine should run.

Why a separate config from .env
-------------------------------
The .env.* files hold per-broker CREDENTIALS (one Tradovate login, one
IBKR watchlist). The bidirectional work adds a separate question: given
those credentials, WHICH direction(s) do we replicate? A user might run
IBKR→Tradovate (today's live path), or Tradovate→IBKR, or — in
principle — several pairs at once. That's a structured list, not flat
key=value, so it lives in its own JSON file rather than being crammed
into dotenv.

NO auto-detection (design decision). The engine never guesses who is
master and who is follower. The user declares explicit source+follower
pairs here (each side: broker + env + account id). This cut ~half the
original design's complexity — no master/follower state machine, no
loop-prevention, no marker-based dedup — because a pair is one-way by
construction and the user owns the choice.

This module is pure load/save/validate over a dataclass tree. No I/O
beyond reading/writing the one JSON file; no broker imports; safe for
the GUI and the engine bootstrap to share.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional


logger = logging.getLogger("tradesync.replication_config")


_VALID_BROKERS = ("ibkr", "tradovate")
_VALID_ENVS = ("demo", "live")

_SCHEMA_VERSION = 1


class ReplicationConfigError(ValueError):
    """Raised when replication.json is structurally invalid. The
    message is user-facing (surfaced in the GUI), so it names the
    offending field and the allowed values."""


@dataclass
class EndpointRef:
    """One side of a replication pair: which broker, which environment,
    which account."""
    broker:     str           # "ibkr" | "tradovate"
    env:        str           # "demo" | "live"
    account_id: str

    def validate(self, where: str) -> None:
        if self.broker not in _VALID_BROKERS:
            raise ReplicationConfigError(
                f"{where}: broker must be one of {_VALID_BROKERS}, got "
                f"{self.broker!r}")
        if self.env not in _VALID_ENVS:
            raise ReplicationConfigError(
                f"{where}: env must be one of {_VALID_ENVS}, got "
                f"{self.env!r}")
        if not str(self.account_id).strip():
            raise ReplicationConfigError(f"{where}: account_id is empty")

    @property
    def identity(self) -> str:
        return f"{self.broker}_{self.env}_{self.account_id}"

    @classmethod
    def from_dict(cls, d: dict, where: str) -> "EndpointRef":
        if not isinstance(d, dict):
            raise ReplicationConfigError(f"{where}: expected an object")
        try:
            return cls(
                broker=str(d["broker"]).lower().strip(),
                env=str(d["env"]).lower().strip(),
                account_id=str(d["account_id"]).strip(),
            )
        except KeyError as e:
            raise ReplicationConfigError(
                f"{where}: missing required field {e}") from e


# The follower's size scaling factor. follower_qty = round(master_qty *
# ratio), floored to 1 (never 0 — a replicated trade always opens at
# least the 1-contract minimum). 1.0 = mirror exactly. Capped to keep a
# typo (3.3 vs 0.33) from multiplying real exposure unboundedly.
_RATIO_MAX = 100.0


@dataclass
class ReplicationPair:
    """A one-way replication: observe `source`, mirror onto `follower`."""
    name:     str
    source:   EndpointRef
    follower: EndpointRef
    enabled:  bool = True
    # Follower size = master size × ratio (rounded, min 1). 1.0 = exact
    # mirror. Belongs to the follower side: it scales what the follower
    # trades relative to the master.
    ratio:    float = 1.0
    # Optional per-follower IBKR Gateway override. When set, THIS pair's
    # IBKR follower connects through this Gateway (its own host / port /
    # client_id) instead of the config-level ibkr_gateway, so several
    # Tradovate->IBKR pairs can target DIFFERENT IBKR logins (separate
    # accounts) at once. None = use the global ibkr_gateway (the
    # single-follower default). Ignored for non-IBKR followers.
    ibkr_gateway: Optional["IbkrGatewayConfig"] = None

    def validate(self) -> None:
        where = f"pair {self.name!r}"
        self.source.validate(f"{where} source")
        self.follower.validate(f"{where} follower")
        if self.ibkr_gateway is not None:
            self.ibkr_gateway.validate()
        # A pair must move orders BETWEEN two distinct endpoints.
        # Same broker+env+account on both sides would be a loop.
        if self.source.identity == self.follower.identity:
            raise ReplicationConfigError(
                f"{where}: source and follower are the same endpoint "
                f"({self.source.identity}) — a pair must connect two "
                f"different endpoints")
        # Ratio scales REAL order sizes, so guard it hard: strictly
        # positive (0 or negative would mean "don't trade" / "trade the
        # wrong way", neither of which a size factor should express) and
        # capped, so a mistyped 33 instead of 0.33 can't silently
        # 33× the follower's exposure.
        if not (self.ratio > 0):
            raise ReplicationConfigError(
                f"{where}: ratio must be > 0, got {self.ratio!r}")
        if self.ratio > _RATIO_MAX:
            raise ReplicationConfigError(
                f"{where}: ratio must be <= {_RATIO_MAX}, got {self.ratio!r}")

    @classmethod
    def from_dict(cls, d: dict) -> "ReplicationPair":
        if not isinstance(d, dict):
            raise ReplicationConfigError("each pair must be an object")
        name = str(d.get("name") or "unnamed")
        ratio_raw = d.get("ratio", 1.0)
        try:
            ratio = float(ratio_raw)
        except (TypeError, ValueError):
            raise ReplicationConfigError(
                f"pair {name!r}: ratio must be a number, got {ratio_raw!r}")
        gw_raw = d.get("ibkr_gateway")
        gateway = (IbkrGatewayConfig.from_dict(gw_raw)
                   if gw_raw is not None else None)
        return cls(
            name=name,
            source=EndpointRef.from_dict(d.get("source"), f"pair {name!r} source"),
            follower=EndpointRef.from_dict(d.get("follower"),
                                           f"pair {name!r} follower"),
            enabled=bool(d.get("enabled", True)),
            ratio=ratio,
            ibkr_gateway=gateway,
        )

    def resolve_ibkr_gateway(
        self, default: "IbkrGatewayConfig"
    ) -> "IbkrGatewayConfig":
        """The Gateway this pair's IBKR follower should connect through:
        the pair's own override if set, else the config-level default."""
        return self.ibkr_gateway if self.ibkr_gateway is not None else default


@dataclass
class IbkrGatewayConfig:
    """How to reach the local IB Gateway / TWS, used only when a pair
    has IBKR as its follower. Port 4001 live / 4002 paper (TWS uses
    7496 / 7497)."""
    host:      str = "127.0.0.1"
    port:      int = 4002
    client_id: int = 11

    def validate(self) -> None:
        if not self.host.strip():
            raise ReplicationConfigError("ibkr_gateway.host is empty")
        if not (1 <= self.port <= 65535):
            raise ReplicationConfigError(
                f"ibkr_gateway.port out of range: {self.port}")
        if self.client_id < 0:
            raise ReplicationConfigError(
                f"ibkr_gateway.client_id must be >= 0, got {self.client_id}")

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "IbkrGatewayConfig":
        if not d:
            return cls()
        if not isinstance(d, dict):
            raise ReplicationConfigError("ibkr_gateway must be an object")
        out = cls()
        if "host" in d:
            out.host = str(d["host"]).strip()
        if "port" in d:
            try:
                out.port = int(d["port"])
            except (TypeError, ValueError):
                raise ReplicationConfigError(
                    f"ibkr_gateway.port must be an integer, got {d['port']!r}")
        if "client_id" in d:
            try:
                out.client_id = int(d["client_id"])
            except (TypeError, ValueError):
                raise ReplicationConfigError(
                    f"ibkr_gateway.client_id must be an integer, got "
                    f"{d['client_id']!r}")
        return out


@dataclass
class ReplicationConfig:
    """The whole replication.json: a list of pairs + gateway settings."""
    pairs:        List[ReplicationPair] = field(default_factory=list)
    ibkr_gateway: IbkrGatewayConfig = field(default_factory=IbkrGatewayConfig)

    def validate(self) -> None:
        self.ibkr_gateway.validate()
        seen_names = set()
        for p in self.pairs:
            p.validate()
            if p.name in seen_names:
                raise ReplicationConfigError(
                    f"duplicate pair name {p.name!r} — names must be unique")
            seen_names.add(p.name)
        # Multi-follower safety: no two ENABLED pairs may target the same
        # follower endpoint. Two pipelines mirroring onto one account
        # would double every replicated order on it: almost always a
        # config mistake, and a dangerous one on real money. (Disabled
        # pairs are ignored, so alternatives can stay on file.)
        seen_followers: dict = {}
        for p in self.pairs:
            if not p.enabled:
                continue
            fid = p.follower.identity
            if fid in seen_followers:
                raise ReplicationConfigError(
                    f"pairs {seen_followers[fid]!r} and {p.name!r} both "
                    f"replicate onto the same follower {fid}: one account "
                    f"would receive every order twice. Disable one.")
            seen_followers[fid] = p.name

    @property
    def enabled_pairs(self) -> List[ReplicationPair]:
        return [p for p in self.pairs if p.enabled]

    def needs_ibkr_gateway(self) -> bool:
        """True if any ENABLED pair has IBKR as its follower — i.e. the
        daily Gateway login is actually required this run."""
        return any(p.follower.broker == "ibkr" for p in self.enabled_pairs)

    # ── persistence ──────────────────────────────────────────────── #

    @classmethod
    def load(cls, path: Path) -> "ReplicationConfig":
        """Load + validate from JSON. A missing file yields an empty
        config (no pairs) rather than an error — a fresh install simply
        has nothing configured yet."""
        if not path.exists():
            logger.info("No replication config at %s — starting empty", path)
            return cls()
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            raise ReplicationConfigError(
                f"could not read {path}: {e}") from e
        if not isinstance(data, dict):
            raise ReplicationConfigError(
                f"{path}: top level must be an object")
        pairs_raw = data.get("pairs", [])
        if not isinstance(pairs_raw, list):
            raise ReplicationConfigError(f"{path}: 'pairs' must be a list")
        cfg = cls(
            pairs=[ReplicationPair.from_dict(p) for p in pairs_raw],
            ibkr_gateway=IbkrGatewayConfig.from_dict(data.get("ibkr_gateway")),
        )
        cfg.validate()
        return cfg

    def save(self, path: Path) -> None:
        """Validate then atomically write to JSON."""
        self.validate()
        payload = {
            "schema": _SCHEMA_VERSION,
            "pairs": [
                {
                    "name": p.name,
                    "source": asdict(p.source),
                    "follower": asdict(p.follower),
                    "enabled": p.enabled,
                    "ratio": p.ratio,
                    **({"ibkr_gateway": asdict(p.ibkr_gateway)}
                       if p.ibkr_gateway is not None else {}),
                }
                for p in self.pairs
            ],
            "ibkr_gateway": asdict(self.ibkr_gateway),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(path)
        logger.info("Saved replication config (%d pair(s)) to %s",
                    len(self.pairs), path)


def default_replication_config_path(project_root: Path) -> Path:
    return project_root / "config" / "replication.json"
