"""
Config loader — reads the project's three dotenv files:

    .env        — settings shared by every engine
    .env.live   — LIVE-only credentials, port, IBKR watchlist
    .env.demo   — DEMO-only credentials, port, IBKR watchlist

Centralizes every external knob so the rest of the codebase never
touches `os.environ` directly. Validation is deliberately strict:
missing required credentials abort startup instead of failing later
inside an order-placement call.

Loading order at runtime is fixed: shared first, then env-specific.
If a key happens to appear in both files, the env-specific value
wins. Any value already present in os.environ (e.g. an override
passed by the GUI when spawning the subprocess) takes precedence
over everything in the files.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from dotenv import dotenv_values


class MissingAppCredentialsError(RuntimeError):
    """Raised when `tradesync/_app_credentials.py` is missing,
    unparseable, or still contains placeholder values. The error
    message points the user at the one-time app-registration
    workflow at trader.tradovate.com → API Access."""


def load_app_credentials() -> tuple[str, str]:
    """
    Load the Tradovate APPLICATION cid/sec from the gitignored
    module `tradesync/_app_credentials.py`.

    Environment-variable overrides (TRADOVATE_CID / TRADOVATE_SEC)
    take precedence — useful for CI, ephemeral container runs, or
    anyone who'd rather not commit a file at all (even gitignored).

    Returns:
        (cid, sec)

    Raises:
        MissingAppCredentialsError with a one-screen explanation of
        how to register the app and where to put the values.
    """
    env_cid = (os.getenv("TRADOVATE_CID") or "").strip()
    env_sec = (os.getenv("TRADOVATE_SEC") or "").strip()
    if env_cid and env_sec:
        return env_cid, env_sec

    try:
        from tradesync import _app_credentials as creds
    except ImportError as e:
        raise MissingAppCredentialsError(
            "Tradovate application credentials are not configured.\n\n"
            "Create tradesync/_app_credentials.py from the .example "
            "template:\n"
            "    cp tradesync/_app_credentials.py.example "
            "tradesync/_app_credentials.py\n"
            "Then register TradeSynchronizer at trader.tradovate.com "
            "→ API Access → Register an App (one-time, free; works "
            "with any Tradovate account) and paste the cid + sec "
            "Tradovate returns into the file.\n\n"
            f"(original ImportError: {e})"
        ) from e

    cid = (getattr(creds, "APP_CID", "") or env_cid or "").strip()
    sec = (getattr(creds, "APP_SEC", "") or env_sec or "").strip()
    if not cid or not sec:
        raise MissingAppCredentialsError(
            "tradesync/_app_credentials.py exists but APP_CID and/or "
            "APP_SEC are empty. Register TradeSynchronizer at "
            "trader.tradovate.com → API Access → Register an App "
            "(one-time, free) and paste the values Tradovate returns. "
            "See the .example file for the full walkthrough."
        )
    return cid, sec


def has_app_credentials() -> bool:
    """Non-raising probe used by the GUI to decide whether to show
    the 'app not configured' banner."""
    try:
        load_app_credentials()
    except MissingAppCredentialsError:
        return False
    return True


# Project root (the directory containing this package's parent).
PROJECT_ROOT = Path(__file__).resolve().parent.parent

SHARED_ENV_FILE = PROJECT_ROOT / ".env"


# Tradovate REST base URLs by environment (matches the JS adapter).
_TRADOVATE_BASE = {
    "demo": "https://demo.tradovateapi.com/v1",
    "live": "https://live.tradovateapi.com/v1",
}


def env_file_for(env: str) -> Path:
    """Path to the env-specific dotenv file."""
    if env not in _TRADOVATE_BASE:
        raise ValueError(
            f"TRADOVATE_ENVIRONMENT must be 'demo' or 'live', got '{env}'"
        )
    return PROJECT_ROOT / f".env.{env}"


def _merge_into_environ(env: str) -> None:
    """
    Read .env (shared) + .env.<env> (env-specific) and merge them
    into os.environ with the following precedence:

        os.environ (existing — GUI overrides win)
          >  .env.<env>
          >  .env

    We use dotenv_values to read the files into dicts WITHOUT
    touching os.environ, then write them in with setdefault so any
    pre-existing value (e.g. PROXY_LISTEN_PORT passed by the GUI
    when spawning this subprocess) survives unchanged.
    """
    layers: List[dict] = []
    if SHARED_ENV_FILE.exists():
        layers.append({k: v for k, v in dotenv_values(SHARED_ENV_FILE).items()
                       if v is not None})
    env_file = env_file_for(env)
    if env_file.exists():
        layers.append({k: v for k, v in dotenv_values(env_file).items()
                       if v is not None})

    merged: dict = {}
    for layer in layers:                 # later layers override earlier
        merged.update(layer)
    for k, v in merged.items():
        os.environ.setdefault(k, v)      # never override existing env vars


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
    # macOS-standard persistent location (~/Library/Logs survives
    # reboots; main.py expands the `~` and creates the dir on demand).
    log_file:  str = "~/Library/Logs/TradeSynchronizer/tradesync.log"

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
                f"Open TradeSynchronizer.app and use the "
                f"'{env.capitalize()}' tab to create it."
            )
        _merge_into_environ(env)

        # User credentials (per-env, in .env.<env>): username + password.
        required = {
            "TRADOVATE_USERNAME": os.getenv("TRADOVATE_USERNAME") or "",
            "TRADOVATE_PASSWORD": os.getenv("TRADOVATE_PASSWORD") or "",
        }
        missing = [k for k, v in required.items() if not v]
        if missing:
            raise RuntimeError(
                f"Missing required {env.upper()} user credential(s): "
                + ", ".join(missing) +
                f". Open TradeSynchronizer.app or edit {env_file} and "
                f"fill them in."
            )

        # App credentials (shared across LIVE and DEMO, app-level):
        # cid + sec from the gitignored _app_credentials.py module.
        cid, sec = load_app_credentials()

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
            tradovate_cid=cid,
            tradovate_sec=sec,
            tradovate_env=env,
            tradovate_acct_id=acct_id,
            proxy_host=os.getenv("PROXY_LISTEN_HOST") or "127.0.0.1",
            proxy_port=proxy_port,
            replication_mode=replication_mode,
            skip_protective_stops=skip_stops,
            ibkr_watched_accounts=watched,
            log_level=(os.getenv("LOG_LEVEL") or "INFO").upper(),
            log_file=(os.getenv("LOG_FILE")
                      or "~/Library/Logs/TradeSynchronizer/tradesync.log"),
        )
