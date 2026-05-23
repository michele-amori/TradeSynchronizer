"""
Config loader — reads the env-specific .env file (.env.live or
.env.demo) next to the project root.

Centralizes every external knob so the rest of the codebase never
touches `os.environ` directly. Validation is deliberately strict:
missing required credentials abort startup instead of failing
later inside an order-placement call.

Each environment has its own dotenv file with unsuffixed keys.
Shared settings (proxy host, replication policy, logging) appear
in BOTH files; the GUI keeps them in sync on Save.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from dotenv import load_dotenv


# Project root (the directory containing this package's parent).
PROJECT_ROOT = Path(__file__).resolve().parent.parent


# Tradovate REST base URLs by environment (matches the JS adapter).
_TRADOVATE_BASE = {
    "demo": "https://demo.tradovateapi.com/v1",
    "live": "https://live.tradovateapi.com/v1",
}


def env_file_for(env: str) -> Path:
    """Path to the dotenv file that holds `env`'s configuration."""
    if env not in _TRADOVATE_BASE:
        raise ValueError(
            f"TRADOVATE_ENVIRONMENT must be 'demo' or 'live', got '{env}'"
        )
    return PROJECT_ROOT / f".env.{env}"


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
        """
        Read the env-specific dotenv file, validate required fields,
        return a Config.

        Resolution order for TRADOVATE_ENVIRONMENT:
          1. If TRADOVATE_ENVIRONMENT is already set in os.environ
             (e.g. via subprocess env-var override from the GUI), use
             it — and DON'T let load_dotenv overwrite it.
          2. Otherwise, default to 'demo' for safety; the user can
             override per-invocation via the shell.

        Then `.env.<env>` is loaded with override=False semantics
        (the default for python-dotenv), so any env vars already set
        in os.environ (including PROXY_LISTEN_PORT, LOG_FILE, etc.
        passed by the GUI) keep their values from the caller.
        """
        env = (os.getenv("TRADOVATE_ENVIRONMENT") or "demo").lower()
        if env not in _TRADOVATE_BASE:
            raise RuntimeError(
                f"TRADOVATE_ENVIRONMENT must be 'demo' or 'live', got '{env}'"
            )

        env_file = env_file_for(env)
        if not env_file.exists():
            raise RuntimeError(
                f"Config file not found: {env_file}\n"
                f"Open TradeSynchronizer.app to create it, or copy the "
                f"layout from .env.live / .env.demo in the repo."
            )
        load_dotenv(env_file)

        required = {
            "TRADOVATE_USERNAME": os.getenv("TRADOVATE_USERNAME") or "",
            "TRADOVATE_PASSWORD": os.getenv("TRADOVATE_PASSWORD") or "",
            "TRADOVATE_CID":      os.getenv("TRADOVATE_CID") or "",
            "TRADOVATE_SEC":      os.getenv("TRADOVATE_SEC") or "",
        }
        missing = [k for k, v in required.items() if not v]
        if missing:
            raise RuntimeError(
                f"Missing required {env.upper()} credential(s): "
                + ", ".join(missing) +
                f". Open TradeSynchronizer.app or edit {env_file} and "
                f"fill them in."
            )

        acct_id_raw = (os.getenv("TRADOVATE_ACCOUNT_ID") or "").strip()
        acct_id = int(acct_id_raw) if acct_id_raw else None

        replication_mode = (os.getenv("REPLICATION_MODE") or "mirror").lower()
        if replication_mode not in ("mirror", "market"):
            raise RuntimeError(
                f"REPLICATION_MODE must be 'mirror' or 'market', got "
                f"'{replication_mode}'"
            )

        skip_stops_raw = (os.getenv("SKIP_PROTECTIVE_STOPS") or "true").lower()
        skip_stops = skip_stops_raw in ("1", "true", "yes", "on")

        watched_raw = (os.getenv("IBKR_WATCHED_ACCOUNTS") or "").strip()
        watched = [a.strip() for a in watched_raw.split(",") if a.strip()]

        port_raw = (os.getenv("PROXY_LISTEN_PORT")
                    or ("8080" if env == "live" else "8081"))
        try:
            proxy_port = int(port_raw)
        except ValueError:
            raise RuntimeError(
                f"PROXY_LISTEN_PORT for {env.upper()} must be an integer, "
                f"got '{port_raw}'"
            )

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
            proxy_port=proxy_port,
            replication_mode=replication_mode,
            skip_protective_stops=skip_stops,
            ibkr_watched_accounts=watched,
            log_level=(os.getenv("LOG_LEVEL") or "INFO").upper(),
            log_file=os.getenv("LOG_FILE") or "/tmp/tradesync.log",
        )
