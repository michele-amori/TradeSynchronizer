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
from ..notify import notify
from ..replicator import Replicator
from .ibkr_parser import (
    IbkrBracket,
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
    parse_new_order_response_ids,
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
        # Stash the list of cOIDs (in body order) per flow so that
        # when the response comes back we can bind each cOID → IBKR
        # order_id. Brackets produce multiple cOIDs per flow.
        # Mitmproxy guarantees one request/response pair per HTTPFlow,
        # so a regular dict keyed by id(flow) is fine; we still clean
        # up after response to avoid unbounded growth.
        self._coids_by_flow: dict[int, list] = {}
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
            logger.debug("captured Bearer token, len=%d", len(auth))

        if is_new_order_request(flow):
            logger.debug("➜ NEW ORDER request → %s %s",
                         flow.request.method, flow.request.path)
            self._handle_new_order_request(flow)
        elif is_cancel_order_request(flow):
            logger.debug("➜ CANCEL request → %s %s",
                         flow.request.method, flow.request.path)
            self._handle_cancel_request(flow)
        elif is_modify_order_request(flow):
            logger.debug("➜ MODIFY request → %s %s",
                         flow.request.method, flow.request.path)
            self._handle_modify_request(flow)
        else:
            logger.debug("(unmatched IBKR request: %s %s — passive only)",
                         flow.request.method, flow.request.path)

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
            logger.debug("(observed contract info from %s — status %d, %d bytes)",
                         flow.request.path,
                         flow.response.status_code,
                         len(flow.response.content or b""))

        # New-order POST response: bind cOID ↔ IBKR order_id.
        if is_new_order_request(flow):
            logger.debug("⇠ NEW ORDER response → status=%d, body=%s",
                         flow.response.status_code,
                         (flow.response.content or b"")[:600])
            self._handle_new_order_response(flow)

    # ------------------------------------------------------------------ #
    #  Request handlers                                                   #
    # ------------------------------------------------------------------ #

    def _handle_new_order_request(self, flow: http.HTTPFlow) -> None:
        try:
            parsed = parse_ibkr_order(flow)
        except UnsupportedOrderError as e:
            logger.warning("Skipping unparseable IBKR order: %s", e)
            return

        # Collect the cOIDs in body order so the response hook can
        # bind each one to its IBKR-assigned order_id.
        if isinstance(parsed, IbkrBracket):
            coids = [parsed.entry.cOID] + [c.cOID for c in parsed.children]
            child_summary = ", ".join(
                f"{c.order_type}@{c.price or c.aux_price}"
                for c in parsed.children
            )
            logger.info(
                "📥 IBKR new bracket: %s %d %s @ conid=%d entry=%s%s "
                "+ %d exit(s) [%s] cOIDs=%s",
                parsed.entry.side, parsed.entry.quantity,
                parsed.entry.account_id, parsed.entry.conid,
                parsed.entry.order_type,
                f"@{parsed.entry.price}" if parsed.entry.price else "",
                len(parsed.children), child_summary, coids,
            )
            label = f"replicate-bracket-cOID-{parsed.entry.cOID}"
        else:
            coids = [parsed.cOID]
            logger.info(
                "📥 IBKR new order: %s %d %s @ conid=%d type=%s "
                "price=%s aux=%s tif=%s cOID=%s",
                parsed.side, parsed.quantity, parsed.account_id,
                parsed.conid, parsed.order_type,
                parsed.price, parsed.aux_price, parsed.tif, parsed.cOID,
            )
            label = f"replicate-new-cOID-{parsed.cOID}"

        coids = [c for c in coids if c]
        if coids:
            with self._coid_lock:
                self._coids_by_flow[id(flow)] = coids

        self._spawn(self._replicator.replicate_new, parsed, label=label)

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
        # Recover the list of cOIDs we stashed on the request side.
        with self._coid_lock:
            request_coids = self._coids_by_flow.pop(id(flow), None) or []
        pairs = parse_new_order_response_ids(flow)
        if not pairs:
            if request_coids:
                logger.warning(
                    "IBKR new-order response had no order_id(s) we "
                    "recognise — future cancel/modify on cOID(s) %s "
                    "won't be replicated. Status=%s Body[:300]=%r",
                    request_coids,
                    flow.response.status_code if flow.response else None,
                    (flow.response.content or b"")[:300] if flow.response else b"",
                )
            return
        for idx, (resp_coid, ibkr_id) in enumerate(pairs):
            # Prefer cOID echoed in the response; fall back to
            # positional matching against the stashed request order.
            coid = resp_coid or (request_coids[idx]
                                 if idx < len(request_coids) else None)
            if not coid:
                logger.warning(
                    "IBKR response leg %d has IBKR id=%s but we can't "
                    "resolve which cOID it belongs to — cancel/modify "
                    "on that leg won't replicate.", idx, ibkr_id,
                )
                continue
            self._replicator.register_ibkr_id(coid, ibkr_id)
            logger.info("🔗 Bound cOID=%s ↔ IBKR id=%s", coid, ibkr_id)

    # ------------------------------------------------------------------ #
    #  Background dispatch                                                #
    # ------------------------------------------------------------------ #

    def _spawn(self, fn, payload, *, label: str) -> None:
        """Run `fn(payload)` on a daemon thread and log its result."""
        env_label = self._cfg.tradovate_env.upper()

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
                # Fire-and-forget desktop notification so the trader
                # sees the rejection even if they're not looking at
                # the GUI. The replicator has already emitted a
                # structured DIVERGENCE event for the Sync-health
                # panel; this notification is the "tap on the
                # shoulder" complementary surface.
                notify(
                    title=f"TradeSynchronizer {env_label}: order rejected",
                    message=result.reason,
                )
        threading.Thread(target=runner, name=label, daemon=True).start()
