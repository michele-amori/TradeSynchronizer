"""
TradovateEndpoint — adapts the existing TradovateClient to the
FollowerEndpoint protocol.

This is where the neutral OrderSpec / BracketSpec / ModifySpec
vocabulary gets translated into Tradovate's wire fields (order types,
actions, TIF, and the payload building). The EventReplicator hands
neutral specs to whichever FollowerEndpoint is configured and never
knows about Tradovate field names itself; this adapter is the Tradovate
realisation of that protocol. It drives the live IBKR→Tradovate hot path
(as a follower) and the Tradovate-side of every other flow.

Symbol handling
--------------
place_order / place_bracket take the follower-side `symbol` (already
resolved to Tradovate's short form, e.g. "MNQM6") and resolve the
numeric contract id internally via TradovateClient.get_contract_id,
which is cached. This keeps contract resolution a follower-side
concern rather than leaking it into the replicator.
"""

from __future__ import annotations

import logging
from typing import Optional

from ..order_event import (
    BracketSpec,
    ModifySpec,
    OrderSpec,
    OrderType,
    Side,
    TimeInForce,
)
from .endpoint import PlacedBracketRef, PlacedRef
from .tradovate import (
    PlacedBracket,
    PlacedOrder,
    TradovateClient,
)


logger = logging.getLogger("tradesync.tradovate_endpoint")


# ── Neutral → Tradovate wire-value maps ──────────────────────────────── #

_ORDER_TYPE_TO_TRADOVATE = {
    OrderType.MARKET:     "Market",
    OrderType.LIMIT:      "Limit",
    OrderType.STOP:       "Stop",
    OrderType.STOP_LIMIT: "StopLimit",
}

_SIDE_TO_TRADOVATE = {
    Side.BUY:  "Buy",
    Side.SELL: "Sell",
}

_TIF_TO_TRADOVATE = {
    TimeInForce.DAY: "Day",
    TimeInForce.GTC: "GTC",
    TimeInForce.IOC: "IOC",
    TimeInForce.FOK: "FOK",
}


def _wants_limit(order_type: OrderType) -> bool:
    return order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT)


def _wants_stop(order_type: OrderType) -> bool:
    return order_type in (OrderType.STOP, OrderType.STOP_LIMIT)


class TradovateEndpoint:
    """FollowerEndpoint adapter over TradovateClient.

    Structurally satisfies tradesync.brokers.endpoint.FollowerEndpoint.
    """

    def __init__(self, client: TradovateClient, *, env: str, account_id: str):
        self._client = client
        self._env = env
        self._account_id = account_id

    # ── identity / lifecycle ─────────────────────────────────────── #

    @property
    def identity(self) -> str:
        return f"tradovate_{self._env}_{self._account_id}"

    @property
    def native_oco(self) -> bool:
        # Tradovate's /order/placeoso does NOT group bracket children
        # into an OCO set (ocoId comes back null — observed live), so
        # the replicator must cancel the sibling leg explicitly.
        return False

    def connect(self) -> None:
        # TradovateClient.connect is idempotent enough for our use —
        # it (re)authenticates and caches the account id. The bootstrap
        # currently calls it directly; exposing it here lets a future
        # endpoint-driven bootstrap treat all followers uniformly.
        self._client.connect()

    def disconnect(self) -> None:
        # TradovateClient holds only a requests.Session + token; there
        # is no explicit logout endpoint we rely on. Nothing to tear
        # down today, but the method exists so the protocol is
        # satisfied and a future client with a socket can hook in.
        pass

    # ── placement ────────────────────────────────────────────────── #

    def place_order(self, spec: OrderSpec, *, symbol: str) -> PlacedRef:
        contract_id = self._client.get_contract_id(symbol)
        placed: PlacedOrder = self._client.place_order(
            tradovate_symbol=symbol,
            contract_id=contract_id,
            action=_SIDE_TO_TRADOVATE[spec.side],
            qty=spec.quantity,
            order_type=_ORDER_TYPE_TO_TRADOVATE[spec.order_type],
            limit_price=spec.limit_price if _wants_limit(spec.order_type) else None,
            stop_price=spec.stop_price if _wants_stop(spec.order_type) else None,
            tif=_TIF_TO_TRADOVATE.get(spec.tif, "Day"),
        )
        return PlacedRef(
            follower_order_id=str(placed.order_id),
            raw=placed.raw,
        )

    def place_bracket(
        self, spec: BracketSpec, *, symbol: str
    ) -> PlacedBracketRef:
        contract_id = self._client.get_contract_id(symbol)
        entry = spec.entry
        bracket_payloads = []
        for child in spec.children:
            bracket_payloads.append({
                "action":      _SIDE_TO_TRADOVATE[child.side],
                "order_type":  _ORDER_TYPE_TO_TRADOVATE[child.order_type],
                "qty":         child.quantity,
                "limit_price": child.limit_price
                    if _wants_limit(child.order_type) else None,
                "stop_price":  child.stop_price
                    if _wants_stop(child.order_type) else None,
                "tif":         _TIF_TO_TRADOVATE.get(child.tif, "Day"),
            })
        placed: PlacedBracket = self._client.place_bracket(
            tradovate_symbol=symbol,
            contract_id=contract_id,
            entry_action=_SIDE_TO_TRADOVATE[entry.side],
            entry_qty=entry.quantity,
            entry_order_type=_ORDER_TYPE_TO_TRADOVATE[entry.order_type],
            entry_limit_price=entry.limit_price
                if _wants_limit(entry.order_type) else None,
            entry_stop_price=entry.stop_price
                if _wants_stop(entry.order_type) else None,
            entry_tif=_TIF_TO_TRADOVATE.get(entry.tif, "Day"),
            brackets=bracket_payloads,
        )
        return PlacedBracketRef(
            entry_order_id=str(placed.entry_order_id),
            child_order_ids=[str(cid) for cid in placed.bracket_ids],
            oco_id=str(placed.oco_id) if placed.oco_id is not None else None,
            raw=placed.raw,
        )

    # ── cancel / modify ──────────────────────────────────────────── #

    def cancel_order(self, follower_order_id: str) -> None:
        self._client.cancel_order(int(follower_order_id))

    def modify_order(self, follower_order_id: str, changes: ModifySpec) -> None:
        # order_type is required by Tradovate's modify endpoint. The
        # neutral ModifySpec carries it explicitly for exactly this
        # reason; translate it (it must be present).
        if changes.order_type is None:
            raise ValueError(
                "TradovateEndpoint.modify_order requires changes.order_type "
                "(Tradovate's /order/modifyorder rejects payloads without "
                "orderType)."
            )
        tv_order_type = _ORDER_TYPE_TO_TRADOVATE[changes.order_type]
        self._client.modify_order(
            int(follower_order_id),
            order_type=tv_order_type,
            qty=changes.new_quantity,
            limit_price=changes.new_limit_price
                if _wants_limit(changes.order_type) else None,
            stop_price=changes.new_stop_price
                if _wants_stop(changes.order_type) else None,
            tif=_TIF_TO_TRADOVATE.get(changes.new_tif)
                if changes.new_tif is not None else None,
        )

    # ── status ───────────────────────────────────────────────────── #

    def order_status(self, follower_order_id: str) -> str:
        return self._client.get_order_status(int(follower_order_id))
