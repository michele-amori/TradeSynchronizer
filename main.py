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
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Optional


# The single hostname pattern we want mitmproxy to MITM. Exposed at
# module scope so tests can probe it against mitmproxy's "candidate
# hostnames per CONNECT" model without booting an actual proxy.
# See the comment in main() above the Options(...) call for the
# full rationale.
ALLOW_HOSTS_PATTERN = r"^api\.ibkr\.com(?::\d+)?$"


def _watch_parent_process(initial_ppid: int, log: logging.Logger) -> None:
    """Background watcher: self-terminate if our parent dies.

    Prevents orphan engine subprocesses from holding port 8080 after
    the GUI is force-quit (Activity Monitor's red "X", or any path
    that doesn't go through the GUI's _on_close handler). Without
    this watcher the next ▶ Start fails with errno 48 (address
    already in use) and the user has to manually kill us.

    Mechanism: poll os.getppid() every 2 s. If it differs from the
    initial parent PID — typically becoming 1 (launchd on macOS,
    init elsewhere) after we're reparented — the parent died, so we
    force-exit with os._exit (skipping atexit handlers, which is
    fine: the OS will close our listening socket and we don't have
    any persistent state to flush that the next engine instance
    won't redo anyway).
    """
    while True:
        time.sleep(2)
        current = os.getppid()
        if current != initial_ppid:
            log.warning(
                "Parent process %d died (now reparented to %d) — "
                "engine self-terminating to free the proxy port.",
                initial_ppid, current,
            )
            os._exit(0)

from tradesync.brokers.ibkr import IbkrContractResolver
from tradesync.brokers.tradovate import TradovateAuthError, TradovateClient
from tradesync.config import Config, PROJECT_ROOT
from tradesync.order_map import OrderMap, default_store_path
from tradesync import preflight
from tradesync.proxy.addon import TradeSyncAddon
from tradesync.proxy.traffic_logger import TrafficLoggerAddon
from tradesync.replication_config import (
    ReplicationConfig,
    ReplicationConfigError,
    default_replication_config_path,
)


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

    Two-tier verbosity:
      * Root logger level = cfg.log_level (INFO by default), so noisy
        third-party loggers (mitmproxy.*, urllib3, asyncio) stay
        quiet.
      * tradesync.* loggers run at DEBUG when
        cfg.verbose_troubleshooting is True, so OUR diagnostic
        output is fully captured during calibration; flip the flag
        off in the GUI later to drop back to INFO.

    The log file path supports `~`, its parent dir is created on
    demand, and rotation uses RotatingFileHandler. Both engine
    subprocesses share the same file; worst case during a
    concurrent rotation is a few seconds of one process writing to
    the just-rotated `.log.1`, harmless at personal-use scale.
    """
    env_tag = f"[{cfg.tradovate_env.upper()}]"
    base_level = getattr(logging, cfg.log_level, logging.INFO)
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
        # Read-only fs or permissions: keep the stream handler so the
        # GUI's Log tab keeps working in adverse conditions.
        pass

    logging.basicConfig(
        level=base_level,
        format=f"%(asctime)s %(levelname)-7s {env_tag:<6} %(name)-22s %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
        force=True,
    )

    if cfg.verbose_troubleshooting:
        # Crank up only tradesync.* — don't drown the log under
        # mitmproxy/urllib3/asyncio DEBUG chatter.
        logging.getLogger("tradesync").setLevel(logging.DEBUG)
        logging.getLogger("tradesync.bootstrap").info(
            "🔍 VERBOSE_TROUBLESHOOTING is ON — tradesync.* at DEBUG, "
            "every IBKR request/response will be dumped in full. "
            "Turn this OFF in the GUI's General tab once the system "
            "is verified to be replicating cleanly."
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
             cfg.tradovate_env, cfg.tradovate_username or "(not set)",
             cfg.tradovate_acct_id)
    if cfg.is_shadow_mode:
        log.warning(
            "🔮 SHADOW MODE — Tradovate credentials are not yet "
            "configured. The proxy will intercept and log every IBKR "
            "order, but no real Tradovate orders will be placed. "
            "Useful for validating the IBKR-side interception before "
            "registering an app at trader.tradovate.com → API Access. "
            "Fill in _app_credentials.py + .env.%s to switch to live "
            "replication.", cfg.tradovate_env,
        )
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
        device_id=cfg.tradovate_device_id or None,
        is_automated=cfg.tradovate_is_automated,
    )
    # Surface this in the boot log so the user can see immediately
    # what's being sent to Tradovate — handy when debugging
    # trade-copier filters that key off the isAutomated field.
    log.info(
        "Tradovate orders will carry isAutomated=%s (set via "
        "TRADOVATE_IS_AUTOMATED env var or the GUI's Tradovate tab). "
        "Set this to false if you use a trade-copier that filters "
        "out algorithmic orders.",
        cfg.tradovate_is_automated,
    )
    try:
        tradovate.connect()
    except TradovateAuthError as e:
        log.error("Tradovate authentication failed: %s", e)
        raise

    resolver = IbkrContractResolver()

    # The persistent OrderMap is created HERE (the bootstrap) and shared
    # with the neutral source pipeline below.
    order_map = OrderMap(default_store_path(PROJECT_ROOT, cfg.tradovate_env))

    # The IBKR→Tradovate hot path runs through the broker-neutral
    # EventReplicator (Step C/D unification complete; the historical
    # Replicator has been removed). The neutral source observer is built
    # over the OrderMap and injected into the addon; the mitmproxy hooks
    # are unchanged. The builder also performs the startup OrderMap
    # reconciliation (EventReplicator.reconcile_with_follower) so stale
    # entries from orders filled/cancelled while the engine was down get
    # pruned.
    source = _build_neutral_ibkr_source(cfg, tradovate, resolver,
                                        order_map, log)

    return TradeSyncAddon(
        cfg=cfg, tradovate=tradovate,
        resolver=resolver, source=source,
    )


def _neutral_ibkr_source_pair(log):
    """Find the enabled IBKR-source pair in replication.json, if any.

    The neutral IBKR-source path (Step A, and the IBKR→IBKR flow) is
    driven by the addon, not the WS pipelines, so it reads its follower
    side and ratio from the matching pair here. Returns the first enabled
    pair whose source broker is IBKR, or None. Never raises — a config
    problem yields None and the caller falls back to legacy behaviour.
    """
    try:
        rep = ReplicationConfig.load(
            default_replication_config_path(PROJECT_ROOT))
    except (ReplicationConfigError, FileNotFoundError, OSError):
        return None
    except Exception:  # noqa: BLE001 — never let config parsing break boot
        log.exception("Reading replication.json for IBKR source pair failed")
        return None
    for p in rep.pairs:
        if p.enabled and p.source.broker == "ibkr":
            return p
    return None


def _build_neutral_ibkr_source(cfg, tradovate, resolver, order_map, log):
    """Assemble the neutral IBKR-source observer and inject it into the
    addon. The SOURCE is always the IBKR orders observed via mitmproxy;
    the FOLLOWER is chosen from the matching replication.json pair:

      * follower broker 'tradovate' (or no config) → TradovateEndpoint,
        the original Step-A IBKR→Tradovate neutral path. The
        EventReplicator needs a conid→symbol resolver because Tradovate
        wants a symbol while the IBKR-source event carries a conid.

      * follower broker 'ibkr' → IbkrFollowerEndpoint on a SECOND IBKR
        account (the IBKR→IBKR flow). STILL needs the conid→symbol
        resolver: the follower endpoint places by symbol
        (resolve_contract), not by conid, so the conid-only source event
        must be mapped to a symbol just as in the Tradovate case. The
        follower Gateway is the one described by replication.json's
        ibkr_gateway block (point it at the follower account's Gateway).

    The follower size ratio is taken from the matching pair in BOTH
    cases (default 1.0 when there's no pair), so every pair type honours
    the ratio. Imports are local so the default path never loads them.
    """
    from tradesync.event_replicator import EventReplicator
    from tradesync.proxy.ibkr_event_source_observer import (
        IbkrEventSourceObserver,
    )

    pair = _neutral_ibkr_source_pair(log)
    ratio = pair.ratio if pair is not None else 1.0
    follower_broker = pair.follower.broker if pair is not None else "tradovate"

    if follower_broker == "ibkr":
        # IBKR→IBKR: follower is a SECOND IBKR account via its own
        # Gateway (replication.json ibkr_gateway block). A conid_resolver
        # IS still required: although the source conid is a valid IBKR
        # instrument id on the follower too, the follower endpoint places
        # by SYMBOL (IbkrFollowerEndpoint.place_* → resolve_contract(sym)),
        # not by conid. The IBKR-source event carries only a conid and no
        # symbol, so without a resolver EventReplicator can't derive the
        # follower symbol and every order fails as "no conid_resolver is
        # configured". resolver.resolve_symbol maps conid → the Tradovate-
        # style short symbol (e.g. "MNQU6"), which resolve_contract then
        # resolves to the follower's contract. Same resolver the
        # Tradovate-follower branch uses.
        from tradesync.brokers.ibkr_api_client import IbkrApiClient
        from tradesync.brokers.ibkr_follower_endpoint import (
            IbkrFollowerEndpoint,
        )
        rep = ReplicationConfig.load(
            default_replication_config_path(PROJECT_ROOT))
        gw = rep.ibkr_gateway
        client = IbkrApiClient(host=gw.host, port=gw.port,
                               client_id=gw.client_id)
        follower = IbkrFollowerEndpoint(
            client, env=pair.follower.env,
            account_id=str(pair.follower.account_id))
        conid_resolver = resolver.resolve_symbol
        log.warning(
            "🧪 IBKR→IBKR: master IBKR orders are mirrored onto IBKR "
            "account %s via Gateway %s:%d (clientId %d), ratio=%g. This "
            "places orders on a SECOND real account — validate on paper "
            "first.",
            pair.follower.account_id, gw.host, gw.port, gw.client_id, ratio)
        # Connect the follower's Gateway NOW. Unlike the Tradovate
        # follower (which wraps the already-connected `tradovate` client)
        # and the Tradovate→IBKR WS pipeline (which connects in
        # SourcePipeline.start), nothing else connects this second IBKR
        # account — so without this the first mirrored order fails with
        # "resolve_contract called while disconnected" and the follower
        # receives nothing. Guarded: a Gateway that's down must NOT crash
        # engine startup; we log a clear, actionable error and leave the
        # follower disconnected (orders fail until the Gateway is up and
        # the engine restarted) rather than abort the whole addon.
        try:
            follower.connect()
        except Exception as e:  # noqa: BLE001 - startup must not be blocked
            log.error(
                "IBKR→IBKR follower Gateway %s:%d (account %s) is not "
                "reachable (%s) — mirrored orders will FAIL until the "
                "Gateway is logged in and the engine is restarted.",
                gw.host, gw.port, pair.follower.account_id, e)
    else:
        # IBKR→Tradovate: the live hot path. Runs on the broker-neutral
        # EventReplicator (the only engine now that the historical
        # Replicator has been removed).
        from tradesync.brokers.tradovate_endpoint import TradovateEndpoint
        follower = TradovateEndpoint(
            tradovate, env=cfg.tradovate_env,
            account_id=str(cfg.tradovate_acct_id or ""),
        )
        conid_resolver = resolver.resolve_symbol
        if pair is not None:
            # An IBKR-source pair is enabled → this observer is the live
            # IBKR→Tradovate replication, with the pair's ratio.
            log.info(
                "IBKR→Tradovate replication active (neutral "
                "EventReplicator, ratio=%g).", ratio)
        else:
            # No enabled IBKR-source pair: the observer is built but
            # dormant (the live direction here is the Tradovate→IBKR WS
            # pipeline, which applies its OWN ratio). Don't log a
            # default 'ratio=1' that looks like the active replication.
            log.info(
                "IBKR→Tradovate neutral observer ready (no enabled "
                "IBKR-source pair; dormant unless IBKR orders arrive).")

    event_replicator = EventReplicator(
        follower=follower,
        order_map=order_map,
        conid_resolver=conid_resolver,
        ratio=ratio,
    )

    # Surface async follower rejections (e.g. IBKR size/liquidity reject
    # after placeOrder returned) on the structured alert channel. No-op
    # for Tradovate followers (they have no set_rejection_handler).
    from tradesync.wiring import _attach_rejection_alert
    _attach_rejection_alert(
        follower, env=cfg.tradovate_env,
        pair_name=(pair.name if pair is not None else "IBKR→Tradovate"))

    # Startup OrderMap reconciliation for THIS (neutral) path — the
    # broker-neutral equivalent of Replicator.reconcile_with_tradovate.
    # The Tradovate follower wraps the already-connected `tradovate`
    # client, so order_status works here. Never block startup on it:
    # reconcile_with_follower already swallows per-entry errors, and we
    # guard the whole call too.
    try:
        event_replicator.reconcile_with_follower()
    except Exception:
        log.exception(
            "Neutral-path OrderMap reconciliation failed — starting with the "
            "existing map intact. Stale entries (if any) resolve on first "
            "cancel/modify."
        )

    return IbkrEventSourceObserver(event_replicator, order_map)


def _build_all_addons(cfg: Optional[Config] = None) -> list:
    """
    Build the list of mitmproxy addons to register: always the core
    TradeSyncAddon, plus the TrafficLoggerAddon when verbose
    troubleshooting is enabled. The TrafficLoggerAddon must come
    FIRST so its request/response hooks fire before TradeSyncAddon's
    — that way we capture even the requests TradeSyncAddon decides
    not to act on (account filter, etc.).
    """
    cfg = cfg or Config.load()
    core_addon = _build_addon(cfg)
    addons: list = []
    if cfg.verbose_troubleshooting:
        addons.append(TrafficLoggerAddon(env_label=cfg.tradovate_env))
    addons.append(core_addon)
    return addons


# ── mitmdump entry point ───────────────────────────────────────────────── #
# `mitmdump -s main.py` looks for a top-level `addons` list or a
# `load_addon()` function. Both are supported here; we instantiate
# lazily so that `python main.py` (which uses asyncio + DumpMaster)
# doesn't double-instantiate the addon when mitmproxy's importer
# touches the module.

addons: list = []


def load_addon():
    if not addons:
        addons.extend(_build_all_addons())
    # mitmproxy accepts either a single addon or a list; we return
    # the list so multiple addons get registered together.
    return addons


# ── Tradovate-source WS pipelines (opt-in, OFF by default) ─────────────── #
#
# The bidirectional work adds a SECOND replication direction —
# Tradovate→IBKR — driven not by mitmproxy but by a WebSocket observer
# (see tradesync/wiring.py). This is wired here behind an explicit,
# default-OFF env flag so the live IBKR→Tradovate hot path is utterly
# unchanged unless the user opts in.
#
# The push-frame parser is calibrated and the Tradovate→IBKR direction
# has been validated end to end against real DEMO orders (native OCO
# bracket + MKT + MODIFY + CANCEL). It stays OFF by default purely so the
# live IBKR→Tradovate hot path is never affected unless the user opts in;
# turning it on is a supervised, market-open step.
#
# Enable with TRADESYNC_ENABLE_WS_PIPELINES=1, which reads
# config/replication.json and starts a pipeline per enabled
# Tradovate-source pair.

def _ws_pipelines_enabled() -> bool:
    return (os.getenv("TRADESYNC_ENABLE_WS_PIPELINES") or "").strip().lower() \
        in ("1", "true", "yes", "on")


def _build_source_pipelines_or_empty(cfg: Config, log: logging.Logger):
    """Load replication.json and assemble Tradovate-source pipelines.
    Returns a list of startable SourcePipelines (possibly empty). Never
    raises — a misconfig logs and yields no pipelines rather than
    taking down the proxy."""
    if not _ws_pipelines_enabled():
        log.info("Tradovate-source WS pipelines are OFF (default). Set "
                 "TRADESYNC_ENABLE_WS_PIPELINES=1 to enable the "
                 "Tradovate→IBKR direction (push-frame parser calibrated "
                 "and validated live).")
        return []

    # Imports are local so the default path never even loads the WS /
    # ibapi machinery.
    from tradesync.wiring import build_source_pipelines
    from tradesync.brokers.ibkr_api_client import IbkrApiClient

    cfg_path = default_replication_config_path(PROJECT_ROOT)
    try:
        rep_cfg = ReplicationConfig.load(cfg_path)
    except ReplicationConfigError as e:
        log.error("replication.json is invalid (%s) — WS pipelines "
                  "disabled this run.", e)
        return []

    if not rep_cfg.enabled_pairs:
        log.info("WS pipelines enabled but replication.json has no enabled "
                 "pairs — nothing to start.")
        return []

    # If any enabled pair has IBKR as its follower, the Tradovate→IBKR
    # direction needs a local IB Gateway. Open it for the user if it
    # isn't already running — but NEVER restart a running one (that
    # would drop the authenticated 2FA session). Mirrors what the
    # TradingView launcher does, with the opposite running-instance
    # rule. Opening only lands the user on the login screen; the API
    # becomes ready when they finish logging in, which the engine's
    # connect step waits for.
    if rep_cfg.needs_ibkr_gateway():
        from tradesync.ibc_gateway_orchestrator import (
            ensure_ports_listening,
            load_gateway_map,
            default_gateway_map_path,
            required_ports_for,
            PortStartResult,
        )
        ports = required_ports_for(rep_cfg, rep_cfg.ibkr_gateway)
        try:
            gw_map = load_gateway_map(default_gateway_map_path(PROJECT_ROOT))
        except ValueError as e:
            log.warning("IBC gateway map invalid (%s) — auto-launch off", e)
            gw_map = {}
        log.info("IB Gateways: enabled pairs need port(s) %s",
                 ", ".join(str(p) for p in ports) or "(none)")
        for outcome in ensure_ports_listening(
                ports, gw_map, host=rep_cfg.ibkr_gateway.host):
            level = (logging.WARNING
                     if outcome.result in (PortStartResult.LAUNCHED_TIMEOUT,
                                           PortStartResult.NO_MAPPING,
                                           PortStartResult.LAUNCH_FAILED)
                     else logging.INFO)
            log.log(level, "IB Gateway port %d: %s",
                    outcome.port, outcome.message)

    def tradovate_factory(env: str, account_id: str) -> TradovateClient:
        c = TradovateClient(
            api_url=cfg.tradovate_api_url,
            username=cfg.tradovate_username,
            password=cfg.tradovate_password,
            app_id=cfg.tradovate_app_id,
            app_version=cfg.tradovate_app_ver,
            cid=cfg.tradovate_cid,
            sec=cfg.tradovate_sec,
            pinned_account_id=int(account_id) if account_id.isdigit() else None,
            device_id=cfg.tradovate_device_id or None,
            is_automated=cfg.tradovate_is_automated,
        )
        return c

    def ibkr_factory(gateway) -> "IbkrApiClient":
        # `gateway` is resolved per-follower by wiring (the pair's own
        # override or the config-level default), so separate-login
        # followers connect through their own host/port/client_id.
        return IbkrApiClient(host=gateway.host, port=gateway.port,
                             client_id=gateway.client_id)

    def order_map_factory(env: str, account_id: str) -> OrderMap:
        # Per-FOLLOWER map file (orders-<env>-<account>.json), so several
        # followers in the same env keep separate id maps. NOTE: this
        # changes the filename vs the old per-env orders-<env>.json — the
        # first restart after this should be from a known/flat state.
        return OrderMap(default_store_path(PROJECT_ROOT,
                                           f"{env}-{account_id}"))

    result = build_source_pipelines(
        rep_cfg,
        tradovate_client_factory=tradovate_factory,
        ibkr_client_factory=ibkr_factory,
        order_map_factory=order_map_factory,
    )
    for note in result.notes:
        log.info("wiring: %s", note)
    return result.source_pipelines


# ── Standalone mode: programmatic mitmproxy ────────────────────────────── #

def main() -> None:
    from mitmproxy.tools.dump import DumpMaster
    from mitmproxy.options import Options

    cfg = Config.load()
    addon_list = _build_all_addons(cfg)
    log = logging.getLogger("tradesync.bootstrap")

    # Auto-suicide if the GUI parent ever dies — see
    # _watch_parent_process docstring above.
    threading.Thread(
        target=_watch_parent_process,
        args=(os.getppid(), log),
        daemon=True,
        name="parent-watcher",
    ).start()

    # We need TLS interception only for api.ibkr.com (IBKR order
    # placements / modifications / cancellations sent by TradingView
    # Desktop). For every other hostname — TradingView's WebSocket
    # data feeds, charts-storage, CDN assets, telemetry, etc. — we
    # want mitmproxy to get out of the way as much as possible so
    # the TV UI stays responsive.
    #
    # We use `allow_hosts` (positive list) rather than `ignore_hosts`
    # (negative list). The intuitive choice is `ignore_hosts` with a
    # negative-lookahead regex like `^(?!api\.ibkr\.com…).+$` — but
    # that's a TRAP. mitmproxy tests the regex against multiple
    # candidate strings per CONNECT, including the upstream server's
    # peername IP (e.g. "95.101.235.232:443" — Akamai for IBKR), and
    # returns True if ANY of those strings matches. The IP obviously
    # doesn't match "api.ibkr.com", so the negative lookahead passes,
    # the regex matches, and the connection ends up ignored anyway —
    # silently raw-forwarded with NO MITM. Symptom: every IBKR order
    # placed by TV is correctly received by IBKR (the TCP relay
    # works) but our addon never sees the HTTP request, so nothing
    # gets replicated to Tradovate. This was the actual root cause
    # of the "I placed an order but nothing happened on Tradovate"
    # report on the night of 5 Jun.
    #
    # `allow_hosts` has the right semantics here: a flow is allowed
    # iff AT LEAST ONE of its candidate hostnames matches the regex.
    # Both the api.ibkr.com server-address string and the SNI match,
    # so the flow is correctly intercepted regardless of what the
    # peername IP happens to be.
    #
    # Performance trade-off: `allow_hosts` does parse the TLS
    # handshake (it needs SNI to decide) while `ignore_hosts` skips
    # even that. In practice this is negligible — the heavy lifting
    # for "keep TV responsive" is done by the Chromium-side
    # --proxy-bypass-list flag in tradingview_launcher.py, which
    # makes TV connect DIRECTLY to its CDN/data hosts without ever
    # routing those flows through our proxy in the first place.
    opts = Options(
        listen_host=cfg.proxy_host,
        listen_port=cfg.proxy_port,
        ssl_insecure=True,
        allow_hosts=[ALLOW_HOSTS_PATTERN],
    )

    # Tradovate-source WS pipelines (OFF unless explicitly enabled).
    # These run on their own observer threads, independent of the
    # mitmproxy event loop, so they coexist with the proxy. Built and
    # started before the proxy loop; stopped in the finally below.
    source_pipelines = _build_source_pipelines_or_empty(cfg, log)
    for p in source_pipelines:
        try:
            p.start()
        except Exception:  # noqa: BLE001 - one bad pipeline mustn't sink the rest
            log.exception("Failed to start pipeline %s — continuing without it",
                          p.identity)

    async def _run():
        master = DumpMaster(opts, with_termlog=False, with_dumper=False)
        for a in addon_list:
            master.addons.add(a)

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
    finally:
        for p in source_pipelines:
            try:
                p.stop()
            except Exception:  # noqa: BLE001 - teardown must not raise
                log.exception("Failed to stop pipeline %s", p.identity)

    log.info("TradeSynchronizer stopped.")


if __name__ == "__main__":
    main()
