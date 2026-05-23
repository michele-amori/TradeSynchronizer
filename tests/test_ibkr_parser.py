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
    is_cancel_order_request,
    is_ibkr_order_request,
    is_modify_order_request,
    is_new_order_request,
    parse_ibkr_cancel,
    parse_ibkr_modify,
    parse_ibkr_order,
    parse_new_order_response_id,
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
class _Resp:
    status_code: int
    content: bytes = b""


@dataclass
class _Flow:
    request: _Req
    response: Optional[_Resp] = None


def _make_flow(body: dict, *, method="POST", host="api.ibkr.com",
               path="/v1/tv/iserver/account/U1234567/orders") -> _Flow:
    return _Flow(request=_Req(
        method=method, pretty_host=host, path=path,
        content=json.dumps(body).encode("utf-8"),
    ))


def _make_empty_flow(*, method, path, host="api.ibkr.com") -> _Flow:
    return _Flow(request=_Req(
        method=method, pretty_host=host, path=path, content=b"",
    ))


def _make_response_flow(status: int, body) -> _Flow:
    raw = json.dumps(body).encode("utf-8") if body is not None else b""
    return _Flow(
        request=_Req(method="POST", pretty_host="api.ibkr.com",
                     path="/v1/tv/iserver/account/U1234567/orders",
                     content=b"{}"),
        response=_Resp(status_code=status, content=raw),
    )


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


# ── Cancel ─────────────────────────────────────────────────────────────── #

class TestCancelRequest(unittest.TestCase):

    def _flow(self, *, method="DELETE",
              path="/v1/tv/iserver/account/U1234567/orders/ibkr-42",
              host="api.ibkr.com"):
        return _make_empty_flow(method=method, path=path, host=host)

    def test_matches_delete_on_single_order_path(self):
        self.assertTrue(is_cancel_order_request(self._flow()))

    def test_rejects_delete_on_plural_orders_path(self):
        self.assertFalse(is_cancel_order_request(self._flow(
            path="/v1/tv/iserver/account/U1234567/orders")))

    def test_rejects_delete_on_non_ibkr_host(self):
        self.assertFalse(is_cancel_order_request(
            self._flow(host="api.tradovate.com")))

    def test_rejects_post(self):
        self.assertFalse(is_cancel_order_request(self._flow(method="POST")))

    def test_parses_account_and_order_id(self):
        flow = self._flow(
            path="/v1/tv/iserver/account/U7713037/orders/abc-12345-xyz",
        )
        c = parse_ibkr_cancel(flow)
        self.assertEqual(c.account_id, "U7713037")
        self.assertEqual(c.ibkr_order_id, "abc-12345-xyz")


# ── Modify ─────────────────────────────────────────────────────────────── #

class TestModifyRequest(unittest.TestCase):

    def _flow(self, body=None, *, method="POST",
              path="/v1/tv/iserver/account/U1234567/orders/ibkr-42"):
        if body is None:
            body = {"orders": [{"price": 21505.5, "quantity": 3,
                                "tif": "GTC"}]}
        return _Flow(request=_Req(
            method=method, pretty_host="api.ibkr.com", path=path,
            content=json.dumps(body).encode("utf-8"),
        ))

    def test_matches_post_on_single_order_path(self):
        self.assertTrue(is_modify_order_request(self._flow()))

    def test_matches_put_on_single_order_path(self):
        self.assertTrue(is_modify_order_request(self._flow(method="PUT")))

    def test_does_not_overlap_with_new_order_path(self):
        flow = self._flow(path="/v1/tv/iserver/account/U1234567/orders")
        self.assertFalse(is_modify_order_request(flow))
        self.assertTrue(is_new_order_request(flow))

    def test_does_not_overlap_with_cancel(self):
        # Same path, but DELETE — should be cancel only
        flow = self._flow(method="DELETE")
        self.assertFalse(is_modify_order_request(flow))
        self.assertTrue(is_cancel_order_request(flow))

    def test_parses_changed_fields(self):
        m = parse_ibkr_modify(self._flow())
        self.assertEqual(m.account_id, "U1234567")
        self.assertEqual(m.ibkr_order_id, "ibkr-42")
        self.assertEqual(m.quantity, 3)
        self.assertEqual(m.price, 21505.5)
        self.assertEqual(m.tif, "GTC")
        self.assertIsNone(m.aux_price)

    def test_handles_partial_body(self):
        # Only the price changed; quantity / tif / aux are None.
        flow = self._flow(body={"orders": [{"price": 21600.0}]})
        m = parse_ibkr_modify(flow)
        self.assertEqual(m.price, 21600.0)
        self.assertIsNone(m.quantity)
        self.assertIsNone(m.tif)
        self.assertIsNone(m.aux_price)

    def test_handles_unwrapped_body_shape(self):
        # Some TV builds might send the order fields at top level
        # rather than under 'orders'. Be lenient.
        flow = self._flow(body={"price": 21300.0, "quantity": 1})
        m = parse_ibkr_modify(flow)
        self.assertEqual(m.price, 21300.0)
        self.assertEqual(m.quantity, 1)


# ── Response parsing — extract IBKR order_id ────────────────────────────── #

class TestParseNewOrderResponseId(unittest.TestCase):

    def test_extracts_from_flat_dict(self):
        f = _make_response_flow(200, {"order_id": "ibkr-42", "ok": True})
        self.assertEqual(parse_new_order_response_id(f), "ibkr-42")

    def test_extracts_camelcase(self):
        f = _make_response_flow(200, {"orderId": "ibkr-99"})
        self.assertEqual(parse_new_order_response_id(f), "ibkr-99")

    def test_extracts_from_array(self):
        f = _make_response_flow(200, [{"order_id": "ibkr-1"}, {"order_id": "ibkr-2"}])
        self.assertEqual(parse_new_order_response_id(f), "ibkr-1")

    def test_extracts_from_nested_orders(self):
        f = _make_response_flow(200, {"orders": [{"order_id": "ibkr-7"}]})
        self.assertEqual(parse_new_order_response_id(f), "ibkr-7")

    def test_returns_none_on_no_id(self):
        f = _make_response_flow(200, {"foo": "bar"})
        self.assertIsNone(parse_new_order_response_id(f))

    def test_returns_none_on_error_status(self):
        f = _make_response_flow(500, {"order_id": "ibkr-42"})
        self.assertIsNone(parse_new_order_response_id(f))

    def test_returns_none_on_bad_body(self):
        f = _Flow(
            request=_Req(method="POST", pretty_host="api.ibkr.com",
                         path="/v1/tv/iserver/account/U1234567/orders",
                         content=b"{}"),
            response=_Resp(status_code=200, content=b"<not json>"),
        )
        # Response parsing fails soft — we don't want to crash the
        # proxy hook just because IBKR returned an unexpected body.
        self.assertIsNone(parse_new_order_response_id(f))


if __name__ == "__main__":
    unittest.main()
