"""
TradeSynchronizer — main entry point.

Two modes:

  1. `python main.py`
        Bootstraps the full stack: connects to Tradovate, then starts
        mitmproxy programmatically and runs the addon. Convenient for
        development and for the future macOS app wrapper.

  2. `mitmdump -s main.py --listen-host 127.0.0.1 --listen-port 8080 \
                --ssl-insecure`
        mitmproxy loads this file as an addon script and calls the
        module-level `addons` list. Use this if you prefer the
        canonical mitmdump CLI (e.g. to add custom mitmproxy flags).

Either way, TradingView Desktop must be launched with
`--proxy-server=127.0.0.1:8080` (or whatever PROXY_LISTEN_PORT you
set in .env), and the mitmproxy CA certificate must be trusted by
the system Keychain — see README.md for the one-time setup.
"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import signal
import sys
from pathlib import Path
from typing import Optional

from tradesync.brokers.ibkr import IbkrContractResolver
from tradesync.brokers.tradovate import TradovateAuthError, TradovateClient
from tradesync.config import Config
from tradesync import preflight
from tradesync.proxy.addon import TradeSyncAddon
from tradesync.replicator import Replicator


# Rotating-file-handler tuning. Both engines write to the same file
# (disambiguated by the [LIVE] / [DEMO] tag) so total on-disk usage is
# ≈ LOG_FILE_MAX_BYTES * (LOG_FILE_BACKUP_COUNT + 1). 5 MB × 6 = 30 MB
# is plenty for a personal-use day-trading workflow.
LOG_FILE_MAX_BYTES = 5 * 1024 * 1024
LOG_FILE_BACKUP_COUNT = 5


def _setup_logging(cfg: Config) -> None:
    """
    Configure root logging. The env-tag (`[LIVE]` / `[DEMO]`) is baked
    into the format so that, when the GUI runs both engines side by
    side and their stdout streams interleave in the Log tab and in
    cfg.log_file, every line is unambiguously attributable.

    The log file path supports `~` (expanded to the user's home) and
    its parent directory is created on demand. Rotation uses
    RotatingFileHandler so the file never grows unboundedly. Both
    engine subprocesses share the same file: the worst case during
    a concurrent rotation is that one process keeps writing to the
    just-rotated `.log.1` for a few seconds, which is harmless for
    personal-use day-trading scale.
    """
    env_tag = f"[{cfg.tradovate_env.upper()}]"
    level = getattr(logging, cfg.log_level, logging.INFO)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]

    log_path = Path(cfg.log_file).expanduser()
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(
            str(log_path),
            maxBytes=LOG_FILE_MAX_BYTES,
            backupCount=LOG_FILE_BACKUP_COUNT,
            encoding="utf-8",
        ))
    except OSError:
        # If we can't write the log file (read-only fs, permissions,
        # etc.) we keep the stream handler so the GUI's Log tab still
        # gets the merged stdout stream.
        pass

    logging.basicConfig(
        level=level,
        format=f"%(asctime)s %(levelname)-7s {env_tag:<6} %(name)-22s %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
        force=True,
    )


def _build_addon(cfg: Optional[Config] = None) -> TradeSyncAddon:
    """
    Wire up every collaborator and return the addon. Shared between
    the standalone `main()` and the `mitmdump -s` entry point.
    """
    cfg = cfg or Config.load()
    _setup_logging(cfg)
    log = logging.getLogger("tradesync.bootstrap")

    log.info("TradeSynchronizer starting up")
    log.info("Tradovate env=%s, user=%s, pinned_account=%s",
             cfg.tradovate_env, cfg.tradovate_username, cfg.tradovate_acct_id)
    preflight.run_all()

    tradovate = TradovateClient(
        api_url=cfg.tradovate_api_url,
        username=cfg.tradovate_username,
        password=cfg.tradovate_password,
        app_id=cfg.tradovate_app_id,
        app_version=cfg.tradovate_app_ver,
        cid=cfg.tradovate_cid,
        sec=cfg.tradovate_sec,
        pinned_account_id=cfg.tradovate_acct_id,
    )
    try:
        tradovate.connect()
    except TradovateAuthError as e:
        log.error("Tradovate authentication failed: %s", e)
        raise

    resolver = IbkrContractResolver()
    replicator = Replicator(cfg=cfg, tradovate=tradovate, resolver=resolver)

    # Reconcile the persistent OrderMap with Tradovate's current
    # state: orders that filled or were cancelled out-of-band while
    # the engine was down get pruned, so they don't sit forever in
    # the map waiting for a DELETE/PUT that will never come.
    try:
        replicator.reconcile_with_tradovate()
    except Exception:
        log.exception(
            "OrderMap reconciliation failed — starting with the existing map "
            "intact. Stale entries (if any) will resolve on first cancel/modify."
        )

    return TradeSyncAddon(
        cfg=cfg, tradovate=tradovate,
        resolver=resolver, replicator=replicator,
    )


# ── mitmdump entry point ───────────────────────────────────────────────── #
# `mitmdump -s main.py` looks for a top-level `addons` list or a
# `load_addon()` function. Both are supported here; we instantiate
# lazily so that `python main.py` (which uses asyncio + DumpMaster)
# doesn't double-instantiate the addon when mitmproxy's importer
# touches the module.

addons: list = []


def load_addon():
    if not addons:
        addons.append(_build_addon())
    return addons[0]


# ── Standalone mode: programmatic mitmproxy ────────────────────────────── #

def main() -> None:
    from mitmproxy.tools.dump import DumpMaster
    from mitmproxy.options import Options

    cfg = Config.load()
    addon = _build_addon(cfg)
    log = logging.getLogger("tradesync.bootstrap")

    opts = Options(
        listen_host=cfg.proxy_host,
        listen_port=cfg.proxy_port,
        ssl_insecure=True,
    )

    async def _run():
        master = DumpMaster(opts, with_termlog=False, with_dumper=False)
        master.addons.add(addon)

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, master.shutdown)

        log.info("mitmproxy listening on %s:%d — TradingView Desktop must use "
                 "--proxy-server=%s:%d",
                 cfg.proxy_host, cfg.proxy_port,
                 cfg.proxy_host, cfg.proxy_port)
        await master.run()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass

    log.info("TradeSynchronizer stopped.")


if __name__ == "__main__":
    main()
