"""
ibkr_endpoint — translate the IBKR parser dataclasses (IbkrOrder /
IbkrBracket / IbkrOrderCancel / IbkrOrderModify) into the broker-neutral
OrderEvent vocabulary.

This is the IBKR realisation of "observed orders → neutral events".
Where the TradovateEndpoint (follower) translates neutral specs OUT to a
broker, the `order_event_from_new` / `order_event_from_cancel` /
`order_event_from_modify` functions here translate a broker's observed
orders IN to neutral events.

These functions are consumed by IbkrEventSourceObserver (the Step-A
neutral source observer the addon can route through behind the
TRADESYNC_NEUTRAL_IBKR_SOURCE flag); the addon-facing thread/dispatch
shell lives there, not here. Translation is kept pure and separate so it
can be unit-tested on its own and reused by any caller.
"""

from __future__ import annotations

import logging
from typing import Optional

from ..order_event import (
    BracketRole,
    BracketSpec,
    EventKind,
    ModifySpec,
    OrderEvent,
    OrderSpec,
    OrderType,
    Side,
    TimeInForce,
)
from ..proxy.ibkr_parser import (
    IbkrBracket,
    IbkrBracketChild,
    IbkrOrder,
    IbkrOrderCancel,
    IbkrOrderModify,
)


logger = logging.getLogger("tradesync.ibkr_endpoint")


_IBKR_BROKER = "ibkr"


# ── IBKR wire-value → neutral maps ───────────────────────────────────── #

_ORDER_TYPE_FROM_IBKR = {
    "MKT":     OrderType.MARKET,
    "LMT":     OrderType.LIMIT,
    "STP":     OrderType.STOP,
    "STP LMT": OrderType.STOP_LIMIT,
}

_SIDE_FROM_IBKR = {
    "BUY":  Side.BUY,
    "SELL": Side.SELL,
}

_TIF_FROM_IBKR = {
    "DAY": TimeInForce.DAY,
    "GTC": TimeInForce.GTC,
    "IOC": TimeInForce.IOC,
    "FOK": TimeInForce.FOK,
}


class IbkrTranslationError(ValueError):
    """Raised when an IBKR dataclass can't be expressed as a neutral
    OrderEvent — e.g. an order type we don't have a neutral token for.
    Surfacing this as an explicit error (rather than guessing) keeps a
    mistranslation from silently reaching the follower broker."""


def _side(ibkr_side: str) -> Side:
    try:
        return _SIDE_FROM_IBKR[ibkr_side]
    except KeyError:
        raise IbkrTranslationError(f"unknown IBKR side {ibkr_side!r}")


def _order_type(ibkr_type: Optional[str]) -> OrderType:
    if ibkr_type is None:
        raise IbkrTranslationError("missing IBKR order_type")
    try:
        return _ORDER_TYPE_FROM_IBKR[ibkr_type]
    except KeyError:
        raise IbkrTranslationError(f"unknown IBKR order_type {ibkr_type!r}")


def _tif(ibkr_tif: Optional[str]) -> TimeInForce:
    # IBKR/TradingView occasionally omit or use an unmapped tif; the
    # historical replicator defaulted to Day, so we keep that.
    if not ibkr_tif:
        return TimeInForce.DAY
    return _TIF_FROM_IBKR.get(ibkr_tif, TimeInForce.DAY)


def _child_role(ibkr_child: IbkrBracketChild) -> BracketRole:
    """Classify a bracket exit leg as take-profit or stop-loss by its
    order type — the same rule the replicator's _bracket_child_role
    uses (a LIMIT exit is the take-profit; a STOP / STOP LIMIT exit is
    the stop-loss)."""
    t = (ibkr_child.order_type or "").upper().strip()
    if t in ("LMT", "LIMIT"):
        return BracketRole.TAKE_PROFIT
    if t in ("STP", "STOP", "STP LMT", "STOPLIMIT", "STP_LMT"):
        return BracketRole.STOP_LOSS
    raise IbkrTranslationError(
        f"can't classify bracket child of type {ibkr_child.order_type!r} "
        f"as take-profit or stop-loss"
    )


def _order_spec_from_ibkr_order(o: IbkrOrder) -> OrderSpec:
    ot = _order_type(o.order_type)
    return OrderSpec(
        side=_side(o.side),
        quantity=o.quantity,
        order_type=ot,
        limit_price=o.price if ot in (OrderType.LIMIT, OrderType.STOP_LIMIT) else None,
        stop_price=o.aux_price if ot in (OrderType.STOP, OrderType.STOP_LIMIT) else None,
        tif=_tif(o.tif),
        source_order_id=None,         # IBKR id is learned later, on the response hook
        source_label=o.cOID,
        role=BracketRole.ENTRY,
    )


def _order_spec_from_ibkr_child(c: IbkrBracketChild) -> OrderSpec:
    ot = _order_type(c.order_type)
    return OrderSpec(
        side=_side(c.side),
        quantity=c.quantity,
        order_type=ot,
        limit_price=c.price if ot in (OrderType.LIMIT, OrderType.STOP_LIMIT) else None,
        stop_price=c.aux_price if ot in (OrderType.STOP, OrderType.STOP_LIMIT) else None,
        tif=_tif(c.tif),
        source_order_id=None,
        source_label=c.cOID,
        role=_child_role(c),
    )


# ── Public translation entry points ──────────────────────────────────── #

def order_event_from_new(parsed) -> OrderEvent:
    """Translate an IbkrOrder or IbkrBracket into a NEW OrderEvent."""
    if isinstance(parsed, IbkrBracket):
        entry = parsed.entry
        return OrderEvent(
            kind=EventKind.NEW,
            source_broker=_IBKR_BROKER,
            source_account_id=entry.account_id,
            source_order_id=None,           # learned on the response hook
            source_label=entry.cOID,
            conid=entry.conid,
            bracket=BracketSpec(
                entry=_order_spec_from_ibkr_order(entry),
                children=[_order_spec_from_ibkr_child(c)
                          for c in parsed.children],
            ),
        )
    if isinstance(parsed, IbkrOrder):
        return OrderEvent(
            kind=EventKind.NEW,
            source_broker=_IBKR_BROKER,
            source_account_id=parsed.account_id,
            source_order_id=None,
            source_label=parsed.cOID,
            conid=parsed.conid,
            order=_order_spec_from_ibkr_order(parsed),
        )
    raise IbkrTranslationError(
        f"order_event_from_new expects IbkrOrder or IbkrBracket, got "
        f"{type(parsed).__name__}"
    )


def order_event_from_cancel(cancel: IbkrOrderCancel) -> OrderEvent:
    return OrderEvent(
        kind=EventKind.CANCEL,
        source_broker=_IBKR_BROKER,
        source_account_id=cancel.account_id,
        source_order_id=cancel.ibkr_order_id,
    )


def order_event_from_modify(modify: IbkrOrderModify) -> OrderEvent:
    # order_type may legitimately be present on the IBKR modify body;
    # translate it when we can, but tolerate absence here (the
    # follower endpoint enforces its own order_type requirement). We
    # do NOT raise on a missing/unknown type at translation time
    # because a modify might change only qty or tif.
    ot: Optional[OrderType] = None
    if modify.order_type:
        try:
            ot = _order_type(modify.order_type)
        except IbkrTranslationError:
            ot = None
    new_tif = _TIF_FROM_IBKR.get(modify.tif) if modify.tif else None
    return OrderEvent(
        kind=EventKind.MODIFY,
        source_broker=_IBKR_BROKER,
        source_account_id=modify.account_id,
        source_order_id=modify.ibkr_order_id,
        modify=ModifySpec(
            new_quantity=modify.quantity,
            new_limit_price=modify.price,
            new_stop_price=modify.aux_price,
            new_tif=new_tif,
            order_type=ot,
        ),
    )


