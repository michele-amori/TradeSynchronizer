"""
IBKR order payload parser — extracts the trade parameters from
the request body of a POST to:

    https://api.ibkr.com/v1/tv/iserver/account/{accountId}/orders

Body shape (verified against myTradingGuardMacOs traffic_spy_ibkr
capture, May 2026):

    {
      "orders": [
        {
          "cOID":            "<client-side id>",
          "conid":           <int>,            # IBKR contract id
          "orderType":       "MKT"|"LMT"|"STP"|"STP LMT",
          "price":           <float>,          # limit price (LMT/STP LMT)
          "auxPrice":        <float>,          # stop price (STP/STP LMT)
          "quantity":        <float>,
          "side":            "BUY"|"SELL",
          "tif":             "DAY"|"GTC"|"IOC"|"FOK",
          "outsideRTH":      <bool>,
          "manualIndicator": <bool>,
          "acctId":          "Uxxxxxxx"
        }
      ]
    }

Multi-leg orders (`orders` array of length > 1) are not supported by
this MVP and raise UnsupportedOrderError. They're rare for futures
day-trading workflows and lifting that limit later is straightforward.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Optional

from mitmproxy import http


# URL of the order-placement endpoint. The trailing `$` excludes the
# single-order management subroute /order/{id} which is used for
# cancels and other non-placement actions.
_ORDER_PATH_RE = re.compile(r"^/v1/tv/iserver/account/[\w]+/orders$")


class UnsupportedOrderError(RuntimeError):
    """Raised when the IBKR order can't be safely replicated."""


@dataclass
class IbkrOrder:
    """Normalised IBKR order, ready for translation to Tradovate."""

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


def is_ibkr_order_request(flow: http.HTTPFlow) -> bool:
    """
    True when the flow is the POST that places a new IBKR order via
    the TradingView integration. Modifications (PUT/PATCH) and the
    cancel route /order/{id} are excluded — we only want fresh
    placements.
    """
    if flow.request.method != "POST":
        return False
    if "api.ibkr.com" not in flow.request.pretty_host:
        return False
    return bool(_ORDER_PATH_RE.match(flow.request.path))


def parse_ibkr_order(flow: http.HTTPFlow) -> IbkrOrder:
    """
    Decode the order POST body into an `IbkrOrder`. Raises
    UnsupportedOrderError on malformed or multi-leg payloads.
    """
    try:
        body = json.loads(flow.request.content or b"{}")
    except json.JSONDecodeError as e:
        raise UnsupportedOrderError(f"Body is not valid JSON: {e}") from e

    orders = body.get("orders")
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


def _to_float(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
