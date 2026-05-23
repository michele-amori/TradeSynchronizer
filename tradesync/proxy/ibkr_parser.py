"""
IBKR order traffic parser for the TradingView Desktop integration.

We observe three flavours of request against:

    https://api.ibkr.com/v1/tv/iserver/account/{accountId}/orders[/{orderId}]

  • POST   .../orders                  →  new order (existing flow)
  • POST   .../orders/{ibkr_order_id}  →  modify   existing order
  • DELETE .../orders/{ibkr_order_id}  →  cancel   existing order

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
          "acctId":          "Uxxxxxxx"
        }
      ]
    }

For modifications, the same body is sent but only the changed fields
matter (price / quantity / auxPrice / tif). Cancels have no body.

Multi-leg orders (`orders` array of length > 1) are not supported by
this MVP and raise UnsupportedOrderError. They're rare for futures
day-trading workflows and lifting that limit later is straightforward.

The POST response from /orders is observed separately by the addon
so we can capture the IBKR-assigned `order_id` and bind it to the
client-side `cOID`. Subsequent cancels / modifies arrive with the
IBKR order_id in the URL, so the cOID is our hinge: the order map
is keyed by cOID and indexed by IBKR id.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Optional

from mitmproxy import http


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

def parse_ibkr_order(flow: http.HTTPFlow) -> IbkrOrder:
    """
    Decode the new-order POST body into an `IbkrOrder`. Raises
    UnsupportedOrderError on malformed or multi-leg payloads.
    """
    body = _decode_json_body(flow.request.content)
    orders = body.get("orders") if isinstance(body, dict) else None
    if not isinstance(orders, list) or not orders:
        raise UnsupportedOrderError(f"Missing 'orders' array in body: {body}")
    if len(orders) > 1:
        raise UnsupportedOrderError(
            f"Multi-leg order ({len(orders)} legs) is not yet supported"
        )

    o = orders[0]
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

def parse_new_order_response_id(flow: http.HTTPFlow) -> Optional[str]:
    """
    Extract the IBKR-assigned order_id from a new-order POST response.
    Returns None on any parse failure — the addon downgrades that to
    "subsequent cancel/modify by IBKR id won't have a mapping, will
    log a warning, and we move on" rather than crashing.

    IBKR's TV-flavour response shape isn't fully documented; we accept
    a few common dialects:

        {"order_id": "...", ...}
        {"orderId":  "...", ...}
        [{"order_id": "...", ...}, ...]                # array form
        {"orders": [{"order_id": "...", ...}, ...]}    # nested-orders form
    """
    if flow.response is None or flow.response.status_code not in (200, 201):
        return None
    try:
        body = _decode_json_body(flow.response.content)
    except UnsupportedOrderError:
        return None
    if isinstance(body, list) and body:
        return _extract_order_id(body[0])
    if isinstance(body, dict):
        if "orders" in body and isinstance(body["orders"], list) and body["orders"]:
            return _extract_order_id(body["orders"][0])
        return _extract_order_id(body)
    return None


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
