"""
TradeSyncAddon — mitmproxy addon that observes IBKR traffic from
TradingView Desktop and replicates new orders onto a Tradovate LEADER
account.

Hooks
-----
request(flow)
    1. Capture the IBKR Bearer token from every request to api.ibkr.com
       so the contract resolver can fall back to active lookups.
    2. Detect order POSTs to /v1/tv/iserver/account/<id>/orders and
       schedule them for replication in a worker thread (the
       replication itself must NOT block mitmproxy's main loop —
       Tradovate's /contract/find + /order/placeorder can together
       take 500-2000ms).

response(flow)
    Observe /contract/{conid}/info responses to populate the
    conid→symbol cache passively. No flow modification.

The addon NEVER modifies, blocks, or delays the original IBKR
request. Replication runs in parallel in a background thread.

Mirrors the host/path discovery work done in myTradingGuardMacOs
(`mytradingguard/proxy/addon.py` + `traffic_spy_ibkr.py`) — the
IBKR endpoint set was verified empirically there in May 2026.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

from mitmproxy import http

from ..brokers.ibkr import IbkrContractResolver
from ..brokers.tradovate import TradovateClient
from ..config import Config
from ..replicator import Replicator
from .ibkr_parser import (
    IbkrOrder,
    UnsupportedOrderError,
    is_ibkr_order_request,
    parse_ibkr_order,
)


logger = logging.getLogger("tradesync.addon")


_IBKR_HOST = "api.ibkr.com"


class TradeSyncAddon:

    def __init__(
        self,
        *,
        cfg: Config,
        tradovate: TradovateClient,
        resolver: IbkrContractResolver,
        replicator: Replicator,
    ):
        self._cfg = cfg
        self._tradovate = tradovate
        self._resolver = resolver
        self._replicator = replicator

        logger.info("TradeSyncAddon active — listening for IBKR orders on %s",
                    _IBKR_HOST)
        logger.info("  Replication mode      : %s", cfg.replication_mode)
        logger.info("  Skip protective stops : %s", cfg.skip_protective_stops)
        if cfg.ibkr_watched_accounts:
            logger.info("  Watched IBKR accounts : %s",
                        ", ".join(cfg.ibkr_watched_accounts))

    # ------------------------------------------------------------------ #
    #  mitmproxy hooks                                                    #
    # ------------------------------------------------------------------ #

    def request(self, flow: http.HTTPFlow) -> None:
        if _IBKR_HOST not in flow.request.pretty_host:
            return

        # Passive token capture — works on every IBKR request, not
        # only orders. Cheaper than parsing the URL.
        auth = flow.request.headers.get("Authorization", "")
        if auth:
            self._resolver.capture_token(auth)

        # Order placement?
        if not is_ibkr_order_request(flow):
            return

        try:
            order = parse_ibkr_order(flow)
        except UnsupportedOrderError as e:
            logger.warning("Skipping unparseable IBKR order: %s", e)
            return

        logger.info(
            "📥 IBKR order intercepted: %s %d %s @ conid=%d type=%s "
            "price=%s aux=%s tif=%s cOID=%s",
            order.side, order.quantity, order.account_id,
            order.conid, order.order_type,
            order.price, order.aux_price, order.tif, order.cOID,
        )

        # Replicate off the proxy thread so we don't add latency to
        # the original IBKR order.
        t = threading.Thread(
            target=self._replicate_async,
            args=(order,),
            name=f"replicate-cOID-{order.cOID}",
            daemon=True,
        )
        t.start()

    def response(self, flow: http.HTTPFlow) -> None:
        # Only IBKR responses interest us — for the passive
        # conid→symbol cache.
        if _IBKR_HOST not in flow.request.pretty_host:
            return
        if flow.response is None or flow.response.status_code != 200:
            return
        if flow.request.method != "GET":
            return

        self._resolver.observe_contract_info(
            flow.request.path,
            flow.response.content,
        )

    # ------------------------------------------------------------------ #
    #  Background replication                                             #
    # ------------------------------------------------------------------ #

    def _replicate_async(self, order: IbkrOrder) -> None:
        try:
            result = self._replicator.replicate(order)
        except Exception:
            logger.exception("Unhandled exception in replicator")
            return

        if result.success:
            logger.info("✅ %s", result.reason)
        elif result.skipped:
            logger.info("⏭️  Skipped: %s", result.reason)
        else:
            logger.error("❌ Replication failed: %s", result.reason)
