"""
IbkrFollowerEndpoint — adapts IbkrApiClient to the FollowerEndpoint
protocol, translating the neutral OrderSpec / BracketSpec / ModifySpec
vocabulary into ibapi Contract + Order objects.

This is the IBKR realisation of "a place orders go". Symmetric to
TradovateEndpoint (the Tradovate follower); where that maps neutral →
Tradovate wire strings, this maps neutral → ibapi Order fields and
drives IbkrApiClient.

Kept as a SEPARATE class from IbkrEndpoint (the source observer)
because the two roles have nothing in common at the code level — the
source is mitmproxy-driven and emits events, the follower holds a
Gateway socket and places orders. Sharing a class would force each to
carry the other's irrelevant surface (the whole reason endpoint.py
splits SourceEndpoint from FollowerEndpoint).

Status: validated live. Unit-tested against a fake IbkrApiClient and
exercised against the paper account — it placed real paper orders end
to end (native OCO bracket + MKT + MODIFY + CANCEL) as the follower in
the Tradovate→IBKR direction.

Order-id model note
-------------------
IBKR identifies orders by an integer order id the client allocates.
place_order / place_bracket return those ids (stringified) as the
neutral follower_order_id(s), so the OrderMap keys uniformly across
brokers. cancel_order / modify_order take that string id back.
modify_order needs the instrument too (IBKR modifies by re-placing the
full order), so this endpoint remembers each placed order's contract +
last Order, keyed by id, to rebuild the modify payload.
"""

from __future__ import annotations

import logging
from typing import Dict

from ibapi.contract import Contract
from ibapi.order import Order

from ..order_event import (
    BracketSpec,
    ModifySpec,
    OrderSpec,
    OrderType,
    Side,
    TimeInForce,
)
from .endpoint import PlacedBracketRef, PlacedRef
from .ibkr_api_client import IbkrApiClient, IbkrApiError


logger = logging.getLogger("tradesync.ibkr_follower")


def _wrap_rejection_handler(handler):
    """Adapt a neutral handler(order_id:str, code:int, msg:str) to the
    client's on_order_rejected(order_id:int, code, msg). Returns None for
    a None handler (clears the client callback)."""
    if handler is None:
        return None

    def _adapt(order_id, code, msg):
        handler(str(order_id), int(code), msg)

    return _adapt


# ── Neutral → IBKR wire-value maps ───────────────────────────────────── #

_ORDER_TYPE_TO_IBKR = {
    OrderType.MARKET:     "MKT",
    OrderType.LIMIT:      "LMT",
    OrderType.STOP:       "STP",
    OrderType.STOP_LIMIT: "STP LMT",   # IBKR uses a space
}

_SIDE_TO_IBKR = {
    Side.BUY:  "BUY",
    Side.SELL: "SELL",
}

_TIF_TO_IBKR = {
    TimeInForce.DAY: "DAY",
    TimeInForce.GTC: "GTC",
    TimeInForce.IOC: "IOC",
    TimeInForce.FOK: "FOK",
}


def _wants_limit(t: OrderType) -> bool:
    return t in (OrderType.LIMIT, OrderType.STOP_LIMIT)


def _wants_stop(t: OrderType) -> bool:
    return t in (OrderType.STOP, OrderType.STOP_LIMIT)


def _build_order(spec: OrderSpec) -> Order:
    """Translate a neutral OrderSpec into an ibapi Order. Price fields
    are gated by order type, mirroring the Tradovate follower's rules.
    transmit is left at IBKR's default True for single orders; the
    bracket path overrides it per-leg."""
    o = Order()
    o.action = _SIDE_TO_IBKR[spec.side]
    o.orderType = _ORDER_TYPE_TO_IBKR[spec.order_type]
    o.totalQuantity = spec.quantity
    o.tif = _TIF_TO_IBKR.get(spec.tif, "DAY")
    if _wants_limit(spec.order_type) and spec.limit_price is not None:
        o.lmtPrice = spec.limit_price
    if _wants_stop(spec.order_type) and spec.stop_price is not None:
        o.auxPrice = spec.stop_price
    # We never want IBKR to apply its own "percentage of NLV" or
    # adaptive sizing; eDecMax/etc. are left default. Crucially set
    # these off so paper/live behave predictably:
    o.eTradeOnly = False
    o.firmQuoteOnly = False
    return o


class IbkrFollowerEndpoint:
    """FollowerEndpoint adapter over IbkrApiClient.

    Structurally satisfies tradesync.brokers.endpoint.FollowerEndpoint.
    """

    def __init__(self, client: IbkrApiClient, *, env: str, account_id: str):
        self._client = client
        self._env = env
        self._account_id = str(account_id)
        # Remember contract + last order per placed id, so modify_order
        # (which re-places the whole order in IBKR) can rebuild the
        # payload. Keyed by the stringified IBKR order id.
        self._placed: Dict[str, tuple] = {}   # id -> (Contract, Order)

    # ── identity / lifecycle ─────────────────────────────────────── #

    @property
    def identity(self) -> str:
        return f"ibkr_{self._env}_{self._account_id}"

    @property
    def native_oco(self) -> bool:
        # IBKR groups bracket children via ocaGroup, so cancelling or
        # filling one leg auto-cancels the sibling at the broker.
        return True

    def connect(self) -> None:
        self._client.connect_and_wait()
        self._assert_account_reachable()

    def set_rejection_handler(self, handler) -> None:
        """Register a callback for ASYNC order rejections from IBKR.
        handler(order_id: str, code: int, msg: str) is invoked when IBKR
        rejects an order after placeOrder returned (e.g. size exceeds the
        account/instrument max). Adapts the client's int-id callback to a
        stringified neutral follower_order_id. handler=None clears it."""
        self._client.on_order_rejected = _wrap_rejection_handler(handler)

    def _assert_account_reachable(self) -> None:
        """Guardrail: refuse to operate if the connected Gateway does not
        manage the configured follower account. This is what stops orders
        from being placed on the WRONG IBKR account — critical for the
        IBKR→IBKR flow, where the follower is a *different* account on a
        *different* Gateway login: if the wrong Gateway is running, fail
        loudly instead of silently mirroring onto whatever is logged in.

        managed_accounts is a comma-separated list IBKR sends right after
        connect. If empty (some configs don't populate it) we can't
        verify, so we warn and proceed rather than block — a safety net,
        not a hard gate, so the existing Tradovate→IBKR direction keeps
        working where it may be unset.
        """
        managed = getattr(self._client, "managed_accounts", None)
        if not managed:
            logger.warning(
                "IBKR follower %s: Gateway did not report managed accounts; "
                "cannot verify the connected account matches the configured "
                "follower %s. Proceeding — double-check the right Gateway is "
                "logged in.", self.identity, self._account_id)
            return
        accounts = {a.strip() for a in managed.split(",") if a.strip()}
        if self._account_id not in accounts:
            raise IbkrApiError(
                f"IBKR follower account mismatch: configured follower is "
                f"{self._account_id!r} but the connected Gateway manages "
                f"{sorted(accounts)}. Refusing to place orders on the wrong "
                f"account — start the Gateway logged into {self._account_id!r}.")

    def disconnect(self) -> None:
        self._client.disconnect_and_wait()

    # ── placement ────────────────────────────────────────────────── #

    def _stamp_account(self, order) -> None:
        """Pin the order to THIS follower's IBKR account, so even a
        Gateway login that can see several accounts routes the order to
        the configured one (and never to the Gateway's default account).
        Critical once multiple followers exist. No-op when the account id
        is unset (some single-account configs leave it blank)."""
        if self._account_id:
            order.account = self._account_id

    def place_order(self, spec: OrderSpec, *, symbol: str) -> PlacedRef:
        resolved = self._client.resolve_contract(symbol)
        order = _build_order(spec)
        self._stamp_account(order)
        order_id = self._client.place_order(
            contract=resolved.contract, order=order)
        sid = str(order_id)
        self._placed[sid] = (resolved.contract, order)
        return PlacedRef(follower_order_id=sid,
                         raw={"conId": resolved.con_id})

    def place_bracket(self, spec: BracketSpec, *, symbol: str) -> PlacedBracketRef:
        resolved = self._client.resolve_contract(symbol)
        parent = _build_order(spec.entry)
        children = [_build_order(c) for c in spec.children]
        self._stamp_account(parent)
        for _c in children:
            self._stamp_account(_c)
        entry_id, child_ids = self._client.place_bracket(
            contract=resolved.contract, parent=parent, children=children)
        # Remember each leg for later modify.
        self._placed[str(entry_id)] = (resolved.contract, parent)
        for cid, child in zip(child_ids, children):
            self._placed[str(cid)] = (resolved.contract, child)
        return PlacedBracketRef(
            entry_order_id=str(entry_id),
            child_order_ids=[str(c) for c in child_ids],
            # IBKR groups the children via ocaGroup natively, so OCO is
            # the broker's job here (no manual cascade needed, unlike
            # Tradovate). We don't surface the oca group as an oco_id
            # because the replicator's cascade logic keys off None.
            oco_id=None,
            raw={"conId": resolved.con_id},
        )

    # ── cancel / modify ──────────────────────────────────────────── #

    def cancel_order(self, follower_order_id: str) -> None:
        self._client.cancel_order(int(follower_order_id))

    def modify_order(self, follower_order_id: str, changes: ModifySpec) -> None:
        # IBKR modifies by re-placing the full order under the same id.
        # Rebuild from the remembered order, applying only the changed
        # fields. order_type is needed to gate prices; the neutral
        # ModifySpec carries it (the replicator fills it from the
        # source order type even when only price changed).
        remembered = self._placed.get(follower_order_id)
        if remembered is None:
            raise IbkrApiError(
                f"modify_order: no remembered IBKR order for id "
                f"{follower_order_id!r} — can't rebuild the modify payload")
        contract, prev = remembered
        order = _clone_order(prev)
        self._stamp_account(order)
        if changes.new_quantity is not None:
            order.totalQuantity = changes.new_quantity
        ot = changes.order_type
        if changes.new_limit_price is not None and (
                ot is None or _wants_limit(ot)):
            order.lmtPrice = changes.new_limit_price
        if changes.new_stop_price is not None and (
                ot is None or _wants_stop(ot)):
            order.auxPrice = changes.new_stop_price
        if changes.new_tif is not None:
            order.tif = _TIF_TO_IBKR.get(changes.new_tif, order.tif)
        self._client.modify_order(
            order_id=int(follower_order_id), contract=contract, order=order)
        # Remember the updated order for any subsequent modify.
        self._placed[follower_order_id] = (contract, order)

    # ── status ───────────────────────────────────────────────────── #

    def order_status(self, follower_order_id: str) -> str:
        return self._client.order_status(int(follower_order_id))


def _clone_order(src: Order) -> Order:
    """Shallow-copy the order fields we set, so a modify doesn't mutate
    the remembered original until it succeeds."""
    o = Order()
    o.action = src.action
    o.orderType = src.orderType
    o.totalQuantity = src.totalQuantity
    o.tif = getattr(src, "tif", "DAY")
    if getattr(src, "lmtPrice", None) is not None:
        o.lmtPrice = src.lmtPrice
    if getattr(src, "auxPrice", None) is not None:
        o.auxPrice = src.auxPrice
    # OCA fields are deliberately NOT carried onto a modify.
    #
    # History (both observed live on paper, with the stop-price proven
    # unchanged afterwards via reqAllOpenOrders):
    #   * Originally we sent no OCA fields → IBKR rejected the bracket-leg
    #     modify with code 10327 "OCA group type revision is not allowed".
    #   * So we then re-sent ocaGroup+ocaType on the modify → IBKR instead
    #     rejected it with code 10326 "OCA group revision is not allowed",
    #     and the stop did NOT move (verified: leg stayed at the old aux
    #     price on both followers).
    # The resolution: a modify must re-place ONLY the changed economic
    # fields and leave OCA grouping entirely out. IBKR already knows the
    # leg's group from its order id and keeps it; the group is a
    # place-time property, not something a modify may restate. parentId
    # is dropped for the same reason — restating the bracket parent on a
    # standalone re-place is what IBKR treats as a group revision.
    #
    # Net effect: a bracket-leg modify now carries action/type/qty/tif +
    # the new price only, which IBKR accepts, and the OCO grouping set at
    # placement remains intact at the broker.
    o.eTradeOnly = False
    o.firmQuoteOnly = False
    return o
