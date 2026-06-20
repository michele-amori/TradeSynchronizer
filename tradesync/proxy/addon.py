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
from ..brokers.tradovate import TradovateClient, TradovateOrderError
from ..symbols.converter import convert_to_tradovate_format
from ..config import Config
from ..replication_alert import emit_replication_failure
from .ibkr_parser import (
    IbkrBracket,
    IbkrOrder,
    IbkrOrderCancel,
    IbkrOrderModify,
    UnsupportedOrderError,
    is_cancel_order_request,
    is_modify_order_request,
    is_new_order_request,
    is_orders_list_response,
    parse_ibkr_cancel,
    parse_ibkr_modify,
    parse_ibkr_order,
    parse_new_order_response_ids,
    parse_orders_list_bracket_children,
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
        source,
    ):
        self._cfg = cfg
        self._tradovate = tradovate
        self._resolver = resolver
        # The source observer is the seam between "observing IBKR
        # traffic" and "replicating it". The addon emits order events
        # through it rather than driving an engine directly, so the
        # mitmproxy hooks only depend on this object's surface (emit_* /
        # coid_for_ibkr_id / register_ibkr_id). The bootstrap injects the
        # broker-neutral observer (IbkrEventSourceObserver →
        # EventReplicator); the hooks never change.
        self._source = source
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
        # only orders. The resolver only stores Bearer tokens (see
        # IbkrContractResolver.capture_token's docstring for the
        # OAuth-vs-Bearer story); we still log every Authorization
        # header we see so the log isn't deceptive about which auth
        # scheme TV is actually using.
        auth = flow.request.headers.get("Authorization", "")
        if auth:
            kind = self._resolver.capture_token(auth)
            if kind == "bearer":
                logger.debug("IBKR auth: new Bearer token captured (len=%d)",
                             len(auth))
            elif kind == "oauth":
                # The common case for TradingView Desktop. Log at TRACE-
                # level frequency would be ideal but stdlib logging
                # doesn't have TRACE — DEBUG is fine since it only
                # fires when verbose troubleshooting is on.
                logger.debug("IBKR auth: OAuth 1.0a header seen "
                             "(not replayable; passive resolve only)")
            elif kind == "other":
                logger.debug("IBKR auth: unrecognised scheme: %.30s…",
                             auth)
            # "bearer-same" and "none" don't deserve a log line.

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

        # Conid → symbol passive cache (existing behaviour). On a
        # NEW conid (first observation), kick off a background
        # /contract/find at Tradovate so the contract_id is in the
        # cache by the time the user actually places the first
        # order on this symbol — typically minutes later, since TV
        # observes /info when the chart opens. Saves ~50-150ms off
        # the first-order critical path.
        if (flow.response.status_code == 200
                and flow.request.method == "GET"):
            self._resolver.observe_contract_info(
                flow.request.path,
                flow.response.content,
                on_new_symbol=self._pre_resolve_tradovate_contract,
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

        # Orders-list poll: TradingView fetches this right after
        # placing a bracket (and periodically thereafter) to learn
        # the IBKR order ids of the bracket children. We use that
        # payload to bind each child's IBKR id to the synthetic cOID
        # the replicator registered at placeoso time. Without this
        # binding, any subsequent DELETE/POST on a child's IBKR id
        # can't be resolved back to the Tradovate child orderId and
        # the cancel/modify silently fails to replicate. See
        # ibkr_parser.parse_orders_list_bracket_children + replicator's
        # _bracket_child_role for the synthetic-cOID convention.
        if is_orders_list_response(flow):
            self._handle_orders_list_response(flow)

    # ------------------------------------------------------------------ #
    #  Background contract_id pre-resolution                              #
    # ------------------------------------------------------------------ #

    def _pre_resolve_tradovate_contract(self, ibkr_symbol: str) -> None:
        """Spawn a daemon thread that pre-fetches the Tradovate
        contract_id for this symbol so it lands in TradovateClient's
        cache before any order arrives. Best-effort and silent on
        failure — the first real order will simply re-resolve.

        Called from the response hook the moment a NEW conid maps
        to a symbol. That's almost always when TV first paints the
        chart, minutes ahead of the user clicking BUY. By the time
        the order POST hits this addon, `get_contract_id` is a
        cache hit and the critical path drops by one round-trip
        (50-150 ms in practice).

        In shadow mode pre-resolve is a no-op: there's no real
        Tradovate cache to warm up, and we'd just pollute the local
        cache with fake-shadow ids that the first real order would
        have to invalidate later anyway."""
        if getattr(self._tradovate, "_shadow_mode", False):
            logger.debug("shadow mode — skipping pre-resolve for %s",
                         ibkr_symbol)
            return

        def warm():
            try:
                tv_symbol = convert_to_tradovate_format(ibkr_symbol)
                self._tradovate.get_contract_id(tv_symbol)
                logger.debug("pre-resolved Tradovate contract_id for %s",
                             tv_symbol)
            except (TradovateOrderError, Exception) as e:
                # Pre-resolution is opportunistic; a failure here
                # just means the first real order eats the lookup.
                logger.debug("pre-resolve for %s failed (non-fatal): %s",
                             ibkr_symbol, e)
        threading.Thread(
            target=warm,
            name=f"pre-resolve-{ibkr_symbol}",
            daemon=True,
        ).start()

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

        self._spawn(self._source.emit_new, parsed, label=label)

    def _handle_cancel_request(self, flow: http.HTTPFlow) -> None:
        try:
            cancel = parse_ibkr_cancel(flow)
        except UnsupportedOrderError as e:
            logger.warning("Skipping unparseable cancel: %s", e)
            return
        logger.info("📥 IBKR cancel request: account=%s ibkr_id=%s",
                    cancel.account_id, cancel.ibkr_order_id)
        self._spawn(
            self._source.emit_cancel, cancel,
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
            self._source.emit_modify, modify,
            label=f"replicate-modify-{modify.ibkr_order_id}",
        )

    # ------------------------------------------------------------------ #
    #  Response handler                                                   #
    # ------------------------------------------------------------------ #

    def _handle_orders_list_response(self, flow: http.HTTPFlow) -> None:
        """Bind bracket-child IBKR ids to synthetic cOIDs.

        TradingView's POST /orders response for a bracket only
        echoes the entry's IBKR id — the children get their ids
        later, via the GET /v1/tv/iserver/account/orders poll TV
        fires shortly after placement (and on every position-panel
        refresh thereafter). Each child entry in that payload has a
        `parentId` field, plus the child's own `orderId` and
        `orderType`. CRITICAL detail uncovered in production on
        2026-06-07: that `parentId` is the entry's IBKR order id,
        NOT the entry's cOID — the docstring on
        parse_orders_list_bracket_children documented it wrong, and
        empirical traffic from a live bracket-MKT placement showed
        children with parentId=319073567 (the IBKR order id of the
        entry, whose cOID was 'xsPj1DgsevTF'). So we have to resolve
        parent IBKR id → entry cOID via the order map before we can
        form the synthetic key the replicator stored Tradovate child
        ids under.

        The chain we're trying to close:

            replicator @ placeoso time:
              entry.cOID = "xsPj1DgsevTF"
              order_map["xsPj1DgsevTF#LMT"]   ← tp Tradovate id
              order_map["xsPj1DgsevTF#STP"]   ← sl Tradovate id

            new-order response hook:
              order_map.set_ibkr_id(
                  "xsPj1DgsevTF", 319073567 )  ← entry IBKR id

            this handler (called on each /orders poll):
              parser yields (319073567, 319073568, "LMT")
              parent_coid = order_map.coid_for_ibkr_id(319073567)
                          → "xsPj1DgsevTF"
              synth_coid  = "xsPj1DgsevTF#LMT"
              register_ibkr_id(synth_coid, 319073568)
                          → now order_map["xsPj1DgsevTF#LMT"]
                            has BOTH a Tradovate id (set earlier)
                            AND an IBKR id (set now).

        After this completes, a future DELETE/POST on
        IBKR id 319073568 resolves via _coid_by_ibkr["319073568"]
        → "xsPj1DgsevTF#LMT" → Tradovate tp id, and the cancel /
        modify replicates correctly.

        Polls of /orders are idempotent: register_ibkr_id is a
        setter and gets called once per (synth_coid, ibkr_id) pair
        per poll; repeats are no-ops at the order-map level.

        If the parent IBKR id can't be resolved to a cOID — most
        commonly because the /orders poll arrived BEFORE the
        new-order response hook bound the entry — we skip that
        child for now. The next poll will retry; the order map
        gains the entry binding within milliseconds of placeoso
        returning, so at most one or two polls miss the window.
        """
        body = flow.response.content if flow.response else b""
        tuples = parse_orders_list_bracket_children(body or b"")
        if not tuples:
            return
        bound = 0
        deferred = 0
        for parent_ref, ibkr_child_id, role in tuples:
            parent_coid = self._source.coid_for_ibkr_id(parent_ref)
            if not parent_coid:
                deferred += 1
                logger.debug(
                    "  /orders poll: parent IBKR id=%s not yet bound to a "
                    "cOID — deferring child IBKR id=%s (role=%s) to next "
                    "poll.",
                    parent_ref, ibkr_child_id, role,
                )
                continue
            synth_coid = f"{parent_coid}#{role}"
            self._source.register_ibkr_id(synth_coid, ibkr_child_id)
            logger.debug(
                "🔗 Bound bracket child cOID=%s ↔ IBKR id=%s (role=%s)",
                synth_coid, ibkr_child_id, role,
            )
            bound += 1
        if bound or deferred:
            logger.info(
                "Bound %d bracket child IBKR id(s) from /orders poll "
                "(%d deferred to next poll).",
                bound, deferred,
            )

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
            self._source.register_ibkr_id(coid, ibkr_id)
            logger.info("🔗 Bound cOID=%s ↔ IBKR id=%s", coid, ibkr_id)

    # ------------------------------------------------------------------ #
    #  Background dispatch                                                #
    # ------------------------------------------------------------------ #

    def _spawn(self, fn, payload, *, label: str) -> None:
        """Run `fn(payload)` on a daemon thread and log its result."""
        env = self._cfg.tradovate_env

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
                # Surface on every channel at once: the structured
                # DIVERGENCE line lights the GUI Sync-health panel, and
                # the desktop notification is the "tap on the shoulder"
                # for an AFK trader. (label is the event kind, e.g.
                # "emit_new".)
                emit_replication_failure(
                    env=env, kind=label, summary=label,
                    reason=result.reason)
        threading.Thread(target=runner, name=label, daemon=True).start()
