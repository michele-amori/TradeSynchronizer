"""
account_book — load / save / validate config/accounts.json, a reusable
address book of broker accounts for the GUI.

Why this exists
---------------
A replication pair names two endpoints, each broker+env+account_id. The
same account often appears in several pairs (one source fanned out to
many followers, say). Re-typing its three fields every time is tedious
and error-prone. This module lets the user register each account ONCE,
under a friendly label, so the pair form can offer them in a dropdown.

Relationship to replication.json (important)
--------------------------------------------
This is a UI convenience ONLY. It does NOT change the replication.json
schema or what the engine reads. When the user picks an account in the
pair form, the GUI copies its broker/env/account_id into the pair, just
as if they had typed them. accounts.json can be deleted at any time and
the engine is entirely unaffected — pairs already carry their own copy
of the fields. So this module is pure load/save/validate over a small
dataclass list, with no broker imports and no engine coupling.

The label is the identity: labels are unique within the book and are
what the dropdown shows. (broker, env, account_id) may legitimately
repeat under different labels — we don't forbid it — but the common
case is one label per real account.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional

from .replication_config import (
    EndpointRef,
    ReplicationConfigError,
    _VALID_BROKERS,
    _VALID_ENVS,
)

logger = logging.getLogger("tradesync.account_book")

_SCHEMA_VERSION = 1


@dataclass
class Account:
    """One reusable broker account: a friendly label plus the three
    fields a pair endpoint needs."""
    label:      str
    broker:     str   # "ibkr" | "tradovate"
    env:        str   # "demo" | "live"
    account_id: str

    def validate(self) -> None:
        if not self.label.strip():
            raise ReplicationConfigError("account label is empty")
        if self.broker not in _VALID_BROKERS:
            raise ReplicationConfigError(
                f"account {self.label!r}: broker must be one of "
                f"{_VALID_BROKERS}, got {self.broker!r}")
        if self.env not in _VALID_ENVS:
            raise ReplicationConfigError(
                f"account {self.label!r}: env must be one of {_VALID_ENVS}, "
                f"got {self.env!r}")
        if not str(self.account_id).strip():
            raise ReplicationConfigError(
                f"account {self.label!r}: account_id is empty")

    @property
    def identity(self) -> str:
        """The broker_env_account triple, matching EndpointRef.identity —
        used to tell whether a pair endpoint refers to this account."""
        return f"{self.broker}_{self.env}_{self.account_id}"

    def to_endpoint(self) -> EndpointRef:
        return EndpointRef(broker=self.broker, env=self.env,
                           account_id=self.account_id)

    @classmethod
    def from_dict(cls, d: dict) -> "Account":
        if not isinstance(d, dict):
            raise ReplicationConfigError("each account must be an object")
        try:
            return cls(
                label=str(d["label"]).strip(),
                broker=str(d["broker"]).lower().strip(),
                env=str(d["env"]).lower().strip(),
                account_id=str(d["account_id"]).strip(),
            )
        except KeyError as e:
            raise ReplicationConfigError(
                f"account missing required field {e}") from e


@dataclass
class AccountBook:
    """The whole accounts.json: a list of saved accounts."""
    accounts: List[Account] = field(default_factory=list)

    def validate(self) -> None:
        seen = set()
        for a in self.accounts:
            a.validate()
            key = a.label.strip().lower()
            if key in seen:
                raise ReplicationConfigError(
                    f"duplicate account label {a.label!r} — labels must be "
                    f"unique")
            seen.add(key)

    # ── persistence ──────────────────────────────────────────────── #

    @classmethod
    def load(cls, path: Path) -> "AccountBook":
        """Load + validate from JSON. A missing file yields an empty book
        rather than an error — a fresh install simply has no accounts."""
        if not path.exists():
            logger.info("No account book at %s — starting empty", path)
            return cls()
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            raise ReplicationConfigError(
                f"could not read {path}: {e}") from e
        if not isinstance(data, dict):
            raise ReplicationConfigError(f"{path}: top level must be an object")
        accts_raw = data.get("accounts", [])
        if not isinstance(accts_raw, list):
            raise ReplicationConfigError(f"{path}: 'accounts' must be a list")
        book = cls(accounts=[Account.from_dict(a) for a in accts_raw])
        book.validate()
        return book

    def save(self, path: Path) -> None:
        """Validate then atomically write to JSON."""
        self.validate()
        payload = {
            "schema": _SCHEMA_VERSION,
            "accounts": [asdict(a) for a in self.accounts],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(path)
        logger.info("Saved account book (%d account(s)) to %s",
                    len(self.accounts), path)


def default_account_book_path(project_root: Path) -> Path:
    return project_root / "config" / "accounts.json"
