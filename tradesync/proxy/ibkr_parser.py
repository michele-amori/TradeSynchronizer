"""
IBKR order traffic parser for the TradingView Desktop integration.

We observe three flavours of request against:

    https://api.ibkr.com/v1/tv/iserver/account/{accountId}/orders[/{orderId}]

  • POST   .../orders                  →  new order(s)  — single leg or bracket
  • POST   .../orders/{ibkr_order_id}  →  modify existing order
  • DELETE .../orders/{ibkr_order_id}  →  cancel existing order

(Some TradingView builds may emit PUT instead of POST for modify;
the parser accepts both for resilience.)

The body shape for new orders was verified against
myTradingGuardMacOs's traffic_spy_ibkr capture (May 2026):

    {
      "orders": [
        {
          "cOID":            "<client-side id>",
          "conid":           <int>,
          "orderType":       "MKT"|"LMT"|"STP"|"STP LMT",
          "price":           <float>,
          "auxPrice":        <float>,
          "quantity":        <float>,
          "side":            "BUY"|"SELL",
          "tif":             "DAY"|"GTC"|"IOC"|"FOK",
          "outsideRTH":      <bool>,
          "manualIndicator": <bool>,
          "acctId":          "Uxxxxxxx",
          "parentId":        "<cOID of entry, brackets only>"   # opt
        }
      ]
    }

Bracket / OCO orders arrive as a multi-leg `orders` array where
exit legs carry `parentId` referencing the entry leg's `cOID`. The
parser identifies that structure and returns an `IbkrBracket`
instead of a single `IbkrOrder`. Multi-leg arrays that don't fit
the bracket pattern (e.g. two identical orders with no parent
linkage) are still rejected as unsupported.

For modifications, the same body is sent but only the changed fields
matter (price / quantity / auxPrice / tif). Cancels have no body.

The POST response from /orders is observed separately by the addon
so we can capture the IBKR-assigned `order_id` and bind it to the
client-side `cOID`. Subsequent cancels / modifies arrive with the
IBKR order_id in the URL, so the cOID is our hinge: the order map
is keyed by cOID and indexed by IBKR id.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

from mitmproxy import http


logger = logging.getLogger("tradesync.ibkr_parser")


# ── URL patterns ─────────────────────────────────────────────────────── #

# New-order placement (no trailing order id).
_NEW_ORDER_PATH_RE = re.compile(r"^/v1/tv/iserver/account/(?P<acct>[\w]+)/orders$")

# Per-order management: cancel (DELETE) and modify (POST / PUT) live
# at /orders/{order_id}.
_SINGLE_ORDER_PATH_RE = re.compile(
    r"^/v1/tv/iserver/account/(?P<acct>[\w]+)/orders/(?P<order_id>[\w\-]+)$"
)


_IBKR_HOST = "api.ibkr.com"


class UnsupportedOrderError(RuntimeError):
    """Raised when the IBKR order can't be safely replicated."""


# ── Parsed-request dataclasses ───────────────────────────────────────── #

@dataclass
class IbkrOrder:
    """Normalised IBKR new order, ready for translation to Tradovate."""

    account_id: str          # IBKR account, e.g. "U1234567"
    conid:      int          # IBKR numeric contract id
    side:       str          # "BUY" | "SELL"
    quantity:   int          # contracts (futures are integer-only)
    order_type: str          # "MKT" | "LMT" | "STP" | "STP LMT"
    price:      Optional[float]      # limit price (LMT / STP LMT)
    aux_price:  Optional[float]      # stop price  (STP / STP LMT)
    tif:        str                  # "DAY" | "GTC" | "IOC" | "FOK"
    cOID:       Optional[str]        # client-side dedup id, used for log correlation
    raw:        dict                 # original JSON for diagnostic dumps

    @property
    def is_protective_stop(self) -> bool:
        """
        Heuristic: stop and stop-limit orders are typically protective
        (stop-loss on an existing position) rather than entry orders.
        The mitmproxy addon uses this flag together with the
        SKIP_PROTECTIVE_STOPS config to decide whether to replicate.
        """
        return self.order_type in ("STP", "STP LMT")


@dataclass
class IbkrOrderCancel:
    """Cancellation request — DELETE on /orders/{order_id}."""
    account_id: str
    ibkr_order_id: str       # IBKR-assigned id taken from the URL


@dataclass
class IbkrOrderModify:
    """
    Modification request — POST/PUT on /orders/{order_id}.

    Only fields the user actually changed will be populated; the rest
    are None. The replicator translates the non-None fields to a
    Tradovate modifyorder call.
    """
    account_id:    str
    ibkr_order_id: str
    quantity:      Optional[int]
    price:         Optional[float]
    aux_price:     Optional[float]
    tif:           Optional[str]
    raw:           dict


@dataclass
class IbkrBracketChild:
    """One of the OCO-linked exit legs in a bracket order."""
    side:       str                  # opposite of the parent's side
    quantity:   int
    order_type: str                  # "LMT" (take-profit) or "STP"/"STP LMT" (stop-loss)
    price:      Optional[float]      # limit price
    aux_price:  Optional[float]      # stop price
    tif:        str
    cOID:       Optional[str]
    raw:        dict


@dataclass
class IbkrBracket:
    """
    A bracket / OCO group: one parent entry order plus 1..2 child
    exit orders that are OCO-linked (when one fills, the other is
    cancelled). Tradovate's /order/placeoso handles this natively
    via a `bracket1` / `bracket2` payload.
    """
    entry:     IbkrOrder
    children:  list   # type: list[IbkrBracketChild]


# ── Request classification ───────────────────────────────────────────── #

def _is_ibkr_host(flow: http.HTTPFlow) -> bool:
    return _IBKR_HOST in flow.request.pretty_host


def is_new_order_request(flow: http.HTTPFlow) -> bool:
    """True when this is a POST that places a NEW order (no trailing id)."""
    if flow.request.method != "POST":
        return False
    if not _is_ibkr_host(flow):
        return False
    return bool(_NEW_ORDER_PATH_RE.match(flow.request.path))


# Keep the old name as an alias so existing code/tests don't break.
is_ibkr_order_request = is_new_order_request


def is_cancel_order_request(flow: http.HTTPFlow) -> bool:
    """True when this is a DELETE on /orders/{order_id}."""
    if flow.request.method != "DELETE":
        return False
    if not _is_ibkr_host(flow):
        return False
    return bool(_SINGLE_ORDER_PATH_RE.match(flow.request.path))


def is_modify_order_request(flow: http.HTTPFlow) -> bool:
    """True when this is a POST/PUT on /orders/{order_id}."""
    if flow.request.method not in ("POST", "PUT"):
        return False
    if not _is_ibkr_host(flow):
        return False
    return bool(_SINGLE_ORDER_PATH_RE.match(flow.request.path))


# ── Parsers ──────────────────────────────────────────────────────────── #

def parse_ibkr_order(flow: http.HTTPFlow):
    """
    Decode the new-order POST body. Returns either:
      * `IbkrOrder`   — for a single-leg POST
      * `IbkrBracket` — for a multi-leg POST that fits the bracket /
                        OCO pattern (entry + children with `parentId`)

    Raises `UnsupportedOrderError` for malformed or multi-leg arrays
    that don't match the bracket pattern (e.g. duplicates with no
    parent linkage).
    """
    body = _decode_json_body(flow.request.content)
    logger.debug("parse_ibkr_order: raw body keys=%s, full=%s",
                 list(body.keys()) if isinstance(body, dict) else type(body).__name__,
                 body)
    orders = body.get("orders") if isinstance(body, dict) else None
    if not isinstance(orders, list) or not orders:
        raise UnsupportedOrderError(f"Missing 'orders' array in body: {body}")

    if len(orders) == 1:
        parsed = _parse_single_leg(orders[0])
        logger.debug("parse_ibkr_order: single-leg result = %s", parsed)
        return parsed

    parsed = _parse_bracket(orders)
    logger.debug("parse_ibkr_order: bracket result entry=%s children=%d",
                 parsed.entry, len(parsed.children))
    return parsed


def _parse_single_leg(o: dict) -> IbkrOrder:
    side = (o.get("side") or "").upper()
    if side not in ("BUY", "SELL"):
        raise UnsupportedOrderError(f"Unknown side '{side}'")

    qty_raw = o.get("quantity")
    if qty_raw is None:
        raise UnsupportedOrderError("Missing 'quantity'")
    try:
        qty = int(float(qty_raw))
    except (TypeError, ValueError) as e:
        raise UnsupportedOrderError(f"Bad quantity {qty_raw!r}") from e
    if qty <= 0:
        raise UnsupportedOrderError(f"quantity must be > 0, got {qty}")

    order_type = (o.get("orderType") or "").upper()
    if order_type not in ("MKT", "LMT", "STP", "STP LMT", "STPLMT"):
        raise UnsupportedOrderError(f"Unsupported orderType '{order_type}'")
    if order_type == "STPLMT":
        order_type = "STP LMT"     # canonical spaced form

    conid = o.get("conid")
    if not isinstance(conid, int):
        raise UnsupportedOrderError(f"Missing or non-int conid: {conid!r}")

    return IbkrOrder(
        account_id=str(o.get("acctId") or ""),
        conid=int(conid),
        side=side,
        quantity=qty,
        order_type=order_type,
        price=_to_float(o.get("price")),
        aux_price=_to_float(o.get("auxPrice")),
        tif=(o.get("tif") or "DAY").upper(),
        cOID=o.get("cOID"),
        raw=o,
    )


def _parse_bracket(orders: list) -> IbkrBracket:
    """
    Identify the bracket structure and return an `IbkrBracket`.

    Rules:
      • Exactly one leg has no `parentId` → that's the entry.
      • Every other leg's `parentId` must reference the entry leg's
        `cOID`. (We're strict here on purpose — a multi-leg array
        without proper parent linkage is suspicious and could
        misreplicate badly.)
      • Up to 2 children are accepted (Tradovate's placeoso has
        bracket1 + bracket2 slots). 3+ children are rejected
        rather than silently dropping legs.
      • Each child's side is the opposite of the entry's side
        (typical bracket: entry BUY → both exits SELL).
    """
    entry_leg = None
    child_legs = []
    for o in orders:
        parent_id = o.get("parentId")
        if not parent_id:
            if entry_leg is not None:
                raise UnsupportedOrderError(
                    "Multi-leg POST has more than one leg without "
                    "parentId — not a recognisable bracket structure."
                )
            entry_leg = o
        else:
            child_legs.append((parent_id, o))

    if entry_leg is None:
        raise UnsupportedOrderError(
            "Multi-leg POST has no leg without parentId — cannot "
            "identify the bracket entry."
        )
    if not child_legs:
        # Single entry, no children — shouldn't reach here since
        # len(orders) > 1, but defensive anyway.
        return IbkrBracket(entry=_parse_single_leg(entry_leg), children=[])
    if len(child_legs) > 2:
        raise UnsupportedOrderError(
            f"Bracket has {len(child_legs)} child legs; Tradovate's "
            f"placeoso supports at most 2. Cannot safely replicate."
        )

    entry = _parse_single_leg(entry_leg)
    if not entry.cOID:
        raise UnsupportedOrderError(
            "Bracket entry leg has no cOID — cannot validate "
            "parentId references."
        )

    children: list[IbkrBracketChild] = []
    opposite_side = "SELL" if entry.side == "BUY" else "BUY"
    for parent_id, child in child_legs:
        if parent_id != entry.cOID:
            raise UnsupportedOrderError(
                f"Bracket child's parentId={parent_id!r} doesn't match "
                f"entry cOID={entry.cOID!r}."
            )
        side = (child.get("side") or "").upper()
        if side not in ("BUY", "SELL"):
            raise UnsupportedOrderError(
                f"Bracket child has unknown side '{side}'"
            )
        if side != opposite_side:
            # Warn-loud rather than reject: some asymmetric bracket
            # styles exist (e.g. partial close), but they're rare and
            # we still want to be told.
            raise UnsupportedOrderError(
                f"Bracket child side {side} not opposite of entry "
                f"{entry.side} — refusing to replicate to avoid "
                f"misdirected hedging."
            )
        qty_raw = child.get("quantity")
        try:
            qty = int(float(qty_raw)) if qty_raw is not None else entry.quantity
        except (TypeError, ValueError):
            qty = entry.quantity
        order_type = (child.get("orderType") or "").upper()
        if order_type == "STPLMT":
            order_type = "STP LMT"
        if order_type not in ("LMT", "STP", "STP LMT"):
            raise UnsupportedOrderError(
                f"Bracket child orderType '{order_type}' unsupported — "
                f"must be LMT (take-profit) or STP/STP LMT (stop-loss)."
            )
        children.append(IbkrBracketChild(
            side=side,
            quantity=qty,
            order_type=order_type,
            price=_to_float(child.get("price")),
            aux_price=_to_float(child.get("auxPrice")),
            tif=(child.get("tif") or "DAY").upper(),
            cOID=child.get("cOID"),
            raw=child,
        ))

    return IbkrBracket(entry=entry, children=children)


def parse_ibkr_cancel(flow: http.HTTPFlow) -> IbkrOrderCancel:
    """Extract account_id and IBKR order_id from a DELETE URL."""
    m = _SINGLE_ORDER_PATH_RE.match(flow.request.path)
    if not m:
        raise UnsupportedOrderError(
            f"DELETE path doesn't match /orders/{{order_id}}: {flow.request.path}"
        )
    return IbkrOrderCancel(
        account_id=m.group("acct"),
        ibkr_order_id=m.group("order_id"),
    )


def parse_ibkr_modify(flow: http.HTTPFlow) -> IbkrOrderModify:
    """
    Decode a modification POST/PUT. Same body shape as new orders,
    but every field is optional — only what the user changed is sent
    (in practice TradingView often resends the full body, so we
    don't insist on having any specific field).
    """
    m = _SINGLE_ORDER_PATH_RE.match(flow.request.path)
    if not m:
        raise UnsupportedOrderError(
            f"Modify path doesn't match /orders/{{order_id}}: {flow.request.path}"
        )
    account_id = m.group("acct")
    ibkr_order_id = m.group("order_id")

    body = _decode_json_body(flow.request.content)
    orders = body.get("orders") if isinstance(body, dict) else None
    o = orders[0] if isinstance(orders, list) and orders else (
        body if isinstance(body, dict) else {}
    )

    qty_raw = o.get("quantity")
    qty: Optional[int]
    if qty_raw is None:
        qty = None
    else:
        try:
            qty = int(float(qty_raw))
        except (TypeError, ValueError):
            qty = None
        if qty is not None and qty <= 0:
            qty = None

    tif = o.get("tif")
    return IbkrOrderModify(
        account_id=account_id,
        ibkr_order_id=ibkr_order_id,
        quantity=qty,
        price=_to_float(o.get("price")),
        aux_price=_to_float(o.get("auxPrice")),
        tif=tif.upper() if isinstance(tif, str) else None,
        raw=o if isinstance(o, dict) else {},
    )


# ── Response parsing — capture the IBKR-assigned order_id ────────────── #

def parse_new_order_response_ids(flow: http.HTTPFlow) -> list:
    """
    Extract every (cOID, ibkr_order_id) pair from a new-order POST
    response. The returned list has the same order as legs in the
    request body, so the addon can fall back to positional matching
    if the response doesn't echo cOIDs back.

    Returns an empty list on any parse failure. Each tuple is
    (Optional[str] cOID, str ibkr_order_id).

    Shapes accepted (defensive against minor TV-flavour drift):
        {"order_id": "...", "cOID": "...", ...}
        {"orderId":  "...", ...}
        [{"order_id": "...", ...}, ...]                # array (brackets!)
        {"orders": [{"order_id": "...", ...}, ...]}    # nested
    """
    if flow.response is None or flow.response.status_code not in (200, 201):
        return []
    try:
        body = _decode_json_body(flow.response.content)
    except UnsupportedOrderError:
        return []

    items: list = []
    if isinstance(body, list):
        items = body
    elif isinstance(body, dict):
        if "orders" in body and isinstance(body["orders"], list):
            items = body["orders"]
        else:
            items = [body]

    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        ibkr_id = _extract_order_id(item)
        if not ibkr_id:
            continue
        coid = item.get("cOID") or item.get("coid")
        out.append((str(coid) if coid else None, ibkr_id))
    return out


def parse_new_order_response_id(flow: http.HTTPFlow) -> Optional[str]:
    """Back-compat wrapper around parse_new_order_response_ids that
    returns just the first IBKR order_id (or None)."""
    pairs = parse_new_order_response_ids(flow)
    return pairs[0][1] if pairs else None


def _extract_order_id(d) -> Optional[str]:
    if not isinstance(d, dict):
        return None
    for key in ("order_id", "orderId", "orderid", "id"):
        v = d.get(key)
        if v is not None and v != "":
            return str(v)
    return None


# ── Internal helpers ─────────────────────────────────────────────────── #

def _decode_json_body(raw: Optional[bytes]) -> dict:
    if not raw:
        return {}
    try:
        out = json.loads(raw)
    except json.JSONDecodeError as e:
        raise UnsupportedOrderError(f"Body is not valid JSON: {e}") from e
    return out if isinstance(out, (dict, list)) else {}


def _to_float(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
