"""
Config loader — reads `.env` next to the project root.

Centralizes every external knob so the rest of the codebase never
touches `os.environ` directly. Validation is deliberately strict:
missing required credentials abort startup instead of failing
later inside an order-placement call.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from dotenv import load_dotenv


# Project root (the directory containing this package's parent).
PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ENV_FILE = PROJECT_ROOT / ".env"


# Tradovate REST base URLs by environment (matches the JS adapter).
_TRADOVATE_BASE = {
    "demo": "https://demo.tradovateapi.com/v1",
    "live": "https://live.tradovateapi.com/v1",
}


@dataclass
class Config:
    # ── Tradovate credentials ────────────────────────────────────── #
    tradovate_username:  str
    tradovate_password:  str
    tradovate_app_id:    str
    tradovate_app_ver:   str
    tradovate_cid:       str
    tradovate_sec:       str
    tradovate_env:       str     # 'demo' | 'live'
    tradovate_acct_id:   int | None   # optional pin

    # ── Proxy ────────────────────────────────────────────────────── #
    proxy_host:  str
    proxy_port:  int

    # ── Replication policy ───────────────────────────────────────── #
    replication_mode:       str   # 'mirror' | 'market'
    skip_protective_stops:  bool
    ibkr_watched_accounts:  List[str] = field(default_factory=list)

    # ── Logging ──────────────────────────────────────────────────── #
    log_level: str = "INFO"
    log_file:  str = "/tmp/tradesync.log"

    # ── Derived ──────────────────────────────────────────────────── #
    @property
    def tradovate_api_url(self) -> str:
        return _TRADOVATE_BASE[self.tradovate_env]

    @classmethod
    def load(cls) -> "Config":
        """Read .env, validate required fields, return a Config."""
        load_dotenv(_ENV_FILE)

        required = {
            "TRADOVATE_USERNAME":  os.getenv("TRADOVATE_USERNAME"),
            "TRADOVATE_PASSWORD":  os.getenv("TRADOVATE_PASSWORD"),
            "TRADOVATE_CID":       os.getenv("TRADOVATE_CID"),
            "TRADOVATE_SEC":       os.getenv("TRADOVATE_SEC"),
        }
        missing = [k for k, v in required.items() if not v]
        if missing:
            raise RuntimeError(
                "Missing required env var(s): " + ", ".join(missing) +
                f". Copy .env.example to .env (at {_ENV_FILE}) and fill them in."
            )

        env = (os.getenv("TRADOVATE_ENVIRONMENT") or "demo").lower()
        if env not in _TRADOVATE_BASE:
            raise RuntimeError(
                f"TRADOVATE_ENVIRONMENT must be 'demo' or 'live', got '{env}'"
            )

        acct_id_raw = (os.getenv("TRADOVATE_ACCOUNT_ID") or "").strip()
        acct_id = int(acct_id_raw) if acct_id_raw else None

        replication_mode = (os.getenv("REPLICATION_MODE") or "mirror").lower()
        if replication_mode not in ("mirror", "market"):
            raise RuntimeError(
                f"REPLICATION_MODE must be 'mirror' or 'market', got '{replication_mode}'"
            )

        skip_stops_raw = (os.getenv("SKIP_PROTECTIVE_STOPS") or "true").lower()
        skip_stops = skip_stops_raw in ("1", "true", "yes", "on")

        watched_raw = (os.getenv("IBKR_WATCHED_ACCOUNTS") or "").strip()
        watched = [a.strip() for a in watched_raw.split(",") if a.strip()]

        return cls(
            tradovate_username=required["TRADOVATE_USERNAME"],
            tradovate_password=required["TRADOVATE_PASSWORD"],
            tradovate_app_id=os.getenv("TRADOVATE_APP_ID") or "TradeSynchronizer",
            tradovate_app_ver=os.getenv("TRADOVATE_APP_VERSION") or "1.0",
            tradovate_cid=required["TRADOVATE_CID"],
            tradovate_sec=required["TRADOVATE_SEC"],
            tradovate_env=env,
            tradovate_acct_id=acct_id,
            proxy_host=os.getenv("PROXY_LISTEN_HOST") or "127.0.0.1",
            proxy_port=int(os.getenv("PROXY_LISTEN_PORT") or "8080"),
            replication_mode=replication_mode,
            skip_protective_stops=skip_stops,
            ibkr_watched_accounts=watched,
            log_level=(os.getenv("LOG_LEVEL") or "INFO").upper(),
            log_file=os.getenv("LOG_FILE") or "/tmp/tradesync.log",
        )
