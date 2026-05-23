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
import signal
import sys
from typing import Optional

from tradesync.brokers.ibkr import IbkrContractResolver
from tradesync.brokers.tradovate import TradovateAuthError, TradovateClient
from tradesync.config import Config
from tradesync.proxy.addon import TradeSyncAddon
from tradesync.replicator import Replicator


def _setup_logging(cfg: Config) -> None:
    """
    Configure root logging. The env-tag (`[LIVE]` / `[DEMO]`) is baked
    into the format so that, when the GUI runs both engines side by
    side and their stdout streams interleave in the Log tab and in
    /tmp/tradesync.log, every line is unambiguously attributable.
    """
    env_tag = f"[{cfg.tradovate_env.upper()}]"
    level = getattr(logging, cfg.log_level, logging.INFO)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        handlers.append(logging.FileHandler(cfg.log_file))
    except OSError:
        # If we can't write the log file, keep the stream handler only.
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
