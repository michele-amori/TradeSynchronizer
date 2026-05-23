"""
Unit tests for tradesync.proxy.ibkr_parser.

Uses a tiny in-memory stub for mitmproxy's HTTPFlow so the parser
can be exercised without booting the proxy.

Run from the repo root:

    python3 -m unittest tests.test_ibkr_parser
"""

import json
import unittest
from dataclasses import dataclass
from typing import Optional

from tradesync.proxy.ibkr_parser import (
    UnsupportedOrderError,
    is_ibkr_order_request,
    parse_ibkr_order,
)


# ── Tiny stand-ins for mitmproxy types ─────────────────────────────────── #

@dataclass
class _Req:
    method: str
    pretty_host: str
    path: str
    content: bytes = b""
    headers: Optional[dict] = None


@dataclass
class _Flow:
    request: _Req


def _make_flow(body: dict, *, method="POST", host="api.ibkr.com",
               path="/v1/tv/iserver/account/U1234567/orders") -> _Flow:
    return _Flow(request=_Req(
        method=method, pretty_host=host, path=path,
        content=json.dumps(body).encode("utf-8"),
    ))


# ── Tests ─────────────────────────────────────────────────────────────── #

class TestIsIbkrOrderRequest(unittest.TestCase):

    def test_matches_canonical_post(self):
        flow = _make_flow({"orders": [{}]})
        self.assertTrue(is_ibkr_order_request(flow))

    def test_rejects_non_ibkr_host(self):
        flow = _make_flow({"orders": [{}]}, host="api.tradovate.com")
        self.assertFalse(is_ibkr_order_request(flow))

    def test_rejects_get(self):
        flow = _make_flow({"orders": [{}]}, method="GET")
        self.assertFalse(is_ibkr_order_request(flow))

    def test_rejects_single_order_path(self):
        # /orders/{orderId} is for cancels — must not be matched
        flow = _make_flow(
            {"orders": [{}]},
            path="/v1/tv/iserver/account/U1234567/orders/abc-123",
        )
        self.assertFalse(is_ibkr_order_request(flow))


class TestParseIbkrOrder(unittest.TestCase):

    def _full_body(self, **overrides):
        base = {
            "cOID":            "tv-12345",
            "conid":           845307883,
            "orderType":       "LMT",
            "price":           21500.0,
            "quantity":        2,
            "side":            "BUY",
            "tif":             "DAY",
            "outsideRTH":      False,
            "manualIndicator": False,
            "acctId":          "U1234567",
        }
        base.update(overrides)
        return {"orders": [base]}

    def test_happy_path_limit(self):
        flow = _make_flow(self._full_body())
        o = parse_ibkr_order(flow)
        self.assertEqual(o.account_id, "U1234567")
        self.assertEqual(o.conid, 845307883)
        self.assertEqual(o.side, "BUY")
        self.assertEqual(o.quantity, 2)
        self.assertEqual(o.order_type, "LMT")
        self.assertEqual(o.price, 21500.0)
        self.assertIsNone(o.aux_price)
        self.assertEqual(o.tif, "DAY")
        self.assertEqual(o.cOID, "tv-12345")
        self.assertFalse(o.is_protective_stop)

    def test_market_order(self):
        flow = _make_flow(self._full_body(orderType="MKT", price=None))
        o = parse_ibkr_order(flow)
        self.assertEqual(o.order_type, "MKT")
        self.assertIsNone(o.price)

    def test_stop_order_is_protective(self):
        flow = _make_flow(self._full_body(orderType="STP", auxPrice=21000.0))
        o = parse_ibkr_order(flow)
        self.assertEqual(o.order_type, "STP")
        self.assertEqual(o.aux_price, 21000.0)
        self.assertTrue(o.is_protective_stop)

    def test_stop_limit_canonical_form(self):
        # IBKR sometimes emits "STPLMT" without space
        flow = _make_flow(self._full_body(
            orderType="STPLMT", price=21500.0, auxPrice=21490.0))
        o = parse_ibkr_order(flow)
        self.assertEqual(o.order_type, "STP LMT")   # canonical spaced form
        self.assertTrue(o.is_protective_stop)

    def test_rejects_unknown_side(self):
        flow = _make_flow(self._full_body(side="HEDGE"))
        with self.assertRaises(UnsupportedOrderError):
            parse_ibkr_order(flow)

    def test_rejects_zero_quantity(self):
        flow = _make_flow(self._full_body(quantity=0))
        with self.assertRaises(UnsupportedOrderError):
            parse_ibkr_order(flow)

    def test_rejects_missing_conid(self):
        body = self._full_body()
        del body["orders"][0]["conid"]
        flow = _make_flow(body)
        with self.assertRaises(UnsupportedOrderError):
            parse_ibkr_order(flow)

    def test_rejects_multi_leg(self):
        body = self._full_body()
        body["orders"].append(dict(body["orders"][0]))
        flow = _make_flow(body)
        with self.assertRaises(UnsupportedOrderError):
            parse_ibkr_order(flow)

    def test_rejects_unknown_order_type(self):
        flow = _make_flow(self._full_body(orderType="TRAIL"))
        with self.assertRaises(UnsupportedOrderError):
            parse_ibkr_order(flow)

    def test_rejects_bad_json(self):
        flow = _make_flow({"orders": []})  # empty
        with self.assertRaises(UnsupportedOrderError):
            parse_ibkr_order(flow)


if __name__ == "__main__":
    unittest.main()
