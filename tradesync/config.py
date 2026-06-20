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

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from dotenv import dotenv_values


logger = logging.getLogger("tradesync.config")


def _ibkr_source_accounts_from_pairs() -> List[str]:
    """Derive the IBKR source-account watch list from the replication
    pair model (config/replication.json). Returns the account ids of
    every ENABLED pair whose SOURCE is IBKR — i.e. the IBKR accounts we
    mirror FROM. Empty if there's no config, no IBKR-source pairs, or
    anything goes wrong (callers fall back to IBKR_WATCHED_ACCOUNTS).

    Lazy import keeps config.py free of a hard dependency on the
    replication-config module and avoids any import cycle.
    """
    try:
        from tradesync.replication_config import (
            ReplicationConfig,
            default_replication_config_path,
        )
        path = default_replication_config_path(PROJECT_ROOT)
        if not path.exists():
            return []
        cfg = ReplicationConfig.load(path)
        accounts = []
        for pair in cfg.enabled_pairs:
            if pair.source.broker == "ibkr":
                acct = str(pair.source.account_id).strip()
                if acct and acct not in accounts:
                    accounts.append(acct)
        return accounts
    except Exception as e:  # noqa: BLE001 - never break config load
        logger.warning("Could not derive IBKR watch list from replication "
                       "pairs (%s); falling back to IBKR_WATCHED_ACCOUNTS", e)
        return []


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


def load_app_credentials_or_empty() -> tuple[str, str]:
    """Non-raising variant of load_app_credentials(). Returns
    ("", "") when the credentials are missing, so the engine can
    keep booting in shadow mode (intercept + log IBKR orders
    without ever talking to Tradovate). Used by Config.load()."""
    try:
        return load_app_credentials()
    except MissingAppCredentialsError:
        return "", ""


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
    ibkr_watched_accounts:  List[str] = field(default_factory=list)

    # ── Logging ──────────────────────────────────────────────────── #
    log_level: str = "INFO"
    # macOS-standard persistent location (~/Library/Logs survives
    # reboots; main.py expands the `~` and creates the dir on demand).
    log_file:  str = "~/Library/Logs/TradeSynchronizer/tradesync.log"

    # ── Troubleshooting ──────────────────────────────────────────── #
    # When True (the calibration default), tradesync.* loggers run at
    # DEBUG and the TrafficLoggerAddon is registered so we capture
    # every HTTP transaction TradingView routes through the proxy.
    # Turn OFF (via the GUI's General tab) once the system is verified
    # to be replicating cleanly, to keep log volume sane.
    verbose_troubleshooting: bool = True

    # Stable device id for Tradovate's anti-fraud heuristics —
    # changing the device id on every restart can trigger account
    # holds or extra MFA prompts. Empty string means "no preference,
    # let TradovateClient generate a fresh uuid4 per session".
    tradovate_device_id: str = ""

    # Value to put in every Tradovate order payload's `isAutomated`
    # field. Tradovate exposes this so the broker — and any third
    # party watching the account's order stream — can tell
    # algorithmic orders apart from manual ones.
    #
    # Trade-copier services like TradeSyncer.com that fan out from
    # the leader account to followers typically filter on
    # isAutomated=true and skip those orders by default (to avoid
    # copier loops between accounts that all run algos, and so
    # leaders' internal strategies aren't auto-shared). If our
    # mirror is sending isAutomated=true the leader account
    # accepts the order fine but the copier never broadcasts it.
    #
    # Semantically the right answer is False here: the order
    # originated from a manual click on TradingView, not from an
    # autonomous strategy on our side — we're a mirror, not a
    # generator. The default below reflects that. Users who run
    # genuinely automated strategies upstream of TradeSynchronizer
    # can flip this to True via the GUI's Tradovate tab or by
    # setting TRADOVATE_IS_AUTOMATED=true in .env.
    tradovate_is_automated: bool = False

    # ── Derived ──────────────────────────────────────────────────── #
    @property
    def tradovate_api_url(self) -> str:
        return _TRADOVATE_BASE[self.tradovate_env]

    @property
    def is_shadow_mode(self) -> bool:
        """True when any required Tradovate credential is missing.
        In shadow mode TradovateClient skips all HTTP calls and
        logs what it WOULD have sent, so the proxy can be validated
        end-to-end against real TV+IBKR traffic without needing a
        registered Tradovate app yet. Set automatically by
        Config.load() when _app_credentials.py is absent/empty OR
        when username/password aren't filled in."""
        return not all((
            self.tradovate_cid,
            self.tradovate_sec,
            self.tradovate_username,
            self.tradovate_password,
        ))

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

        # User credentials (per-env, in .env.<env>): username +
        # password. ALLOWED to be empty — Config.is_shadow_mode will
        # report True and the engine will skip Tradovate HTTP calls
        # while still intercepting and logging every IBKR order.
        username = os.getenv("TRADOVATE_USERNAME") or ""
        password = os.getenv("TRADOVATE_PASSWORD") or ""

        # App credentials (shared across LIVE and DEMO, app-level):
        # cid + sec from the gitignored _app_credentials.py module.
        # Also allowed to be empty — same shadow-mode story.
        cid, sec = load_app_credentials_or_empty()

        # TRADOVATE_ACCOUNT_ID is expected to be the NUMERIC Tradovate
        # account id (e.g. 12345678) obtained from
        # "Sign in & pick account" in the GUI — NOT the alphanumeric
        # nickname some prop firms (Apex / TopStep / BlueGuardian etc.)
        # expose as the public-facing account name. If a non-integer
        # value is found here:
        #   * In shadow mode: warn and treat it as unset. The shadow
        #     TradovateClient never uses the real account id anyway
        #     (returns 999_999 as a placeholder), so the user can
        #     still boot and validate the IBKR-side interception
        #     while they sort out the right value.
        #   * Out of shadow mode: raise a clear error pointing at the
        #     misconfiguration, so the user understands the
        #     "Sign in & pick account" workflow.
        acct_id_raw = (os.getenv("TRADOVATE_ACCOUNT_ID") or "").strip()
        acct_id: Optional[int]
        if not acct_id_raw:
            acct_id = None
        else:
            try:
                acct_id = int(acct_id_raw)
            except ValueError:
                shadow_now = not all((
                    cid, sec,
                    os.getenv("TRADOVATE_USERNAME") or "",
                    os.getenv("TRADOVATE_PASSWORD") or "",
                ))
                if shadow_now:
                    logger.warning(
                        "TRADOVATE_ACCOUNT_ID=%r is not a numeric id "
                        "— ignoring (shadow mode). The numeric id is "
                        "the one returned by Tradovate's /account/list "
                        "endpoint, e.g. 12345678. Prop-firm nicknames "
                        "like 'APEX-12345' or 'BGF46274' go in "
                        "TRADOVATE_USERNAME instead, NOT here. Once "
                        "credentials are configured, use the GUI's "
                        "'Sign in & pick account' to populate this "
                        "field with the right value.", acct_id_raw)
                    acct_id = None
                else:
                    raise RuntimeError(
                        f"TRADOVATE_ACCOUNT_ID={acct_id_raw!r} is not a "
                        f"valid integer. This field expects the NUMERIC "
                        f"Tradovate account id (e.g. 12345678), not the "
                        f"alphanumeric nickname your prop firm shows you. "
                        f"Open the GUI, go to the {env.capitalize()} "
                        f"tab, click 'Sign in & pick account' and let "
                        f"it auto-fill the right value."
                    )

        verbose_raw = (os.getenv("VERBOSE_TROUBLESHOOTING") or "true").lower()
        verbose = verbose_raw in ("1", "true", "yes", "on")

        # isAutomated flag — default false (see field docstring for
        # the trade-copier-compatibility rationale).
        is_automated_raw = (os.getenv("TRADOVATE_IS_AUTOMATED") or "false").lower()
        is_automated = is_automated_raw in ("1", "true", "yes", "on")

        # IBKR source-account filter (which IBKR accounts to mirror FROM).
        # The pair model in config/replication.json is now the source of
        # truth: any enabled IBKR-source pair's account contributes to
        # the watch list. IBKR_WATCHED_ACCOUNTS remains a backward-compat
        # FALLBACK for setups that haven't migrated to pairs yet, so the
        # production filter is never left ungoverned. Pairs win when
        # present; otherwise the env var is used.
        watched = _ibkr_source_accounts_from_pairs()
        if not watched:
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
            tradovate_username=username,
            tradovate_password=password,
            tradovate_app_id=os.getenv("TRADOVATE_APP_ID") or "TradeSynchronizer",
            tradovate_app_ver=os.getenv("TRADOVATE_APP_VERSION") or "1.0",
            tradovate_cid=cid,
            tradovate_sec=sec,
            tradovate_env=env,
            tradovate_acct_id=acct_id,
            tradovate_device_id=os.getenv("TRADOVATE_DEVICE_ID") or "",
            tradovate_is_automated=is_automated,
            proxy_host=os.getenv("PROXY_LISTEN_HOST") or "127.0.0.1",
            proxy_port=proxy_port,
            ibkr_watched_accounts=watched,
            log_level=(os.getenv("LOG_LEVEL") or "INFO").upper(),
            log_file=(os.getenv("LOG_FILE")
                      or "~/Library/Logs/TradeSynchronizer/tradesync.log"),
            verbose_troubleshooting=verbose,
        )
