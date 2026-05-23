"""
TradeSyncAddon — mitmproxy addon that observes IBKR traffic from
TradingView Desktop and keeps a Tradovate LEADER account in sync.

Hooks
-----
request(flow)
    1. Capture the IBKR Bearer token from every request to api.ibkr.com
       so the contract resolver can fall back to active lookups.
    2. Detect order-management requests on
       /v1/tv/iserver/account/<id>/orders[/<order_id>]:
         - POST   .../orders                  → new order
         - DELETE .../orders/{ibkr_order_id}  → cancel
         - POST/PUT .../orders/{ibkr_order_id} → modify
       Each is dispatched to the replicator in a worker thread (the
       replication itself must NOT block mitmproxy's main loop —
       Tradovate's contract/find + placeorder can together take
       500-2000 ms).

response(flow)
    1. Observe /contract/{conid}/info responses to populate the
       conid → symbol cache passively.
    2. Observe new-order POST responses to capture the IBKR-assigned
       order_id and bind it to the cOID in the replicator's
       OrderMap — so a future cancel/modify URL (which references
       IBKR's id) can be translated back into the right Tradovate id.

The addon NEVER modifies, blocks, or delays the original IBKR
request — every replication call happens in parallel on a daemon
thread.

Mirrors the host/path discovery work done in myTradingGuardMacOs
(`mytradingguard/proxy/addon.py` + `traffic_spy_ibkr.py`) — the
IBKR endpoint set was verified empirically there in May 2026.
"""

from __future__ import annotations

import logging
import threading

from mitmproxy import http

from ..brokers.ibkr import IbkrContractResolver
from ..brokers.tradovate import TradovateClient
from ..config import Config
from ..replicator import Replicator
from .ibkr_parser import (
    IbkrOrder,
    IbkrOrderCancel,
    IbkrOrderModify,
    UnsupportedOrderError,
    is_cancel_order_request,
    is_modify_order_request,
    is_new_order_request,
    parse_ibkr_cancel,
    parse_ibkr_modify,
    parse_ibkr_order,
    parse_new_order_response_id,
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
        # Stash cOID per flow so that when the response comes back we
        # can bind cOID → IBKR order_id. Mitmproxy guarantees one
        # request/response pair per HTTPFlow, so a regular dict keyed
        # by id(flow) is fine; we still clean up after response to
        # avoid unbounded growth.
        self._coid_by_flow: dict[int, str] = {}
        self._coid_lock = threading.Lock()

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

        if is_new_order_request(flow):
            self._handle_new_order_request(flow)
        elif is_cancel_order_request(flow):
            self._handle_cancel_request(flow)
        elif is_modify_order_request(flow):
            self._handle_modify_request(flow)

    def response(self, flow: http.HTTPFlow) -> None:
        if _IBKR_HOST not in flow.request.pretty_host:
            return
        if flow.response is None:
            return

        # Conid → symbol passive cache (existing behaviour).
        if (flow.response.status_code == 200
                and flow.request.method == "GET"):
            self._resolver.observe_contract_info(
                flow.request.path,
                flow.response.content,
            )

        # New-order POST response: bind cOID ↔ IBKR order_id.
        if is_new_order_request(flow):
            self._handle_new_order_response(flow)

    # ------------------------------------------------------------------ #
    #  Request handlers                                                   #
    # ------------------------------------------------------------------ #

    def _handle_new_order_request(self, flow: http.HTTPFlow) -> None:
        try:
            order = parse_ibkr_order(flow)
        except UnsupportedOrderError as e:
            logger.warning("Skipping unparseable IBKR order: %s", e)
            return

        logger.info(
            "📥 IBKR new order: %s %d %s @ conid=%d type=%s "
            "price=%s aux=%s tif=%s cOID=%s",
            order.side, order.quantity, order.account_id,
            order.conid, order.order_type,
            order.price, order.aux_price, order.tif, order.cOID,
        )

        # Stash cOID so the response hook can finish the mapping.
        if order.cOID:
            with self._coid_lock:
                self._coid_by_flow[id(flow)] = order.cOID

        self._spawn(
            self._replicator.replicate_new, order,
            label=f"replicate-new-cOID-{order.cOID}",
        )

    def _handle_cancel_request(self, flow: http.HTTPFlow) -> None:
        try:
            cancel = parse_ibkr_cancel(flow)
        except UnsupportedOrderError as e:
            logger.warning("Skipping unparseable cancel: %s", e)
            return
        logger.info("📥 IBKR cancel request: account=%s ibkr_id=%s",
                    cancel.account_id, cancel.ibkr_order_id)
        self._spawn(
            self._replicator.replicate_cancel, cancel,
            label=f"replicate-cancel-{cancel.ibkr_order_id}",
        )

    def _handle_modify_request(self, flow: http.HTTPFlow) -> None:
        try:
            modify = parse_ibkr_modify(flow)
        except UnsupportedOrderError as e:
            logger.warning("Skipping unparseable modify: %s", e)
            return
        changed = ", ".join(
            f"{k}={v}" for k, v in (
                ("qty",   modify.quantity),
                ("price", modify.price),
                ("aux",   modify.aux_price),
                ("tif",   modify.tif),
            ) if v is not None
        ) or "(no fields)"
        logger.info("📥 IBKR modify request: account=%s ibkr_id=%s [%s]",
                    modify.account_id, modify.ibkr_order_id, changed)
        self._spawn(
            self._replicator.replicate_modify, modify,
            label=f"replicate-modify-{modify.ibkr_order_id}",
        )

    # ------------------------------------------------------------------ #
    #  Response handler                                                   #
    # ------------------------------------------------------------------ #

    def _handle_new_order_response(self, flow: http.HTTPFlow) -> None:
        # Recover the cOID we stashed on the request side.
        with self._coid_lock:
            coid = self._coid_by_flow.pop(id(flow), None)
        if not coid:
            return
        ibkr_order_id = parse_new_order_response_id(flow)
        if not ibkr_order_id:
            logger.warning(
                "IBKR new-order response had no order_id we recognise — "
                "future cancel/modify on cOID=%s won't be replicated. "
                "Status=%s Body[:200]=%r",
                coid,
                flow.response.status_code if flow.response else None,
                (flow.response.content or b"")[:200] if flow.response else b"",
            )
            return
        self._replicator.register_ibkr_id(coid, ibkr_order_id)
        logger.info("🔗 Bound cOID=%s ↔ IBKR id=%s", coid, ibkr_order_id)

    # ------------------------------------------------------------------ #
    #  Background dispatch                                                #
    # ------------------------------------------------------------------ #

    def _spawn(self, fn, payload, *, label: str) -> None:
        """Run `fn(payload)` on a daemon thread and log its result."""
        def runner():
            try:
                result = fn(payload)
            except Exception:
                logger.exception("Unhandled exception in replicator")
                return
            if result.success:
                logger.info("✅ %s", result.reason)
            elif result.skipped:
                logger.info("⏭️  Skipped: %s", result.reason)
            else:
                logger.error("❌ Replication failed: %s", result.reason)
        threading.Thread(target=runner, name=label, daemon=True).start()

    # Back-compat: a couple of older tests called this directly.
    def _replicate_async(self, order: IbkrOrder) -> None:
        self._spawn(
            self._replicator.replicate_new, order,
            label=f"replicate-new-cOID-{order.cOID}",
        )
