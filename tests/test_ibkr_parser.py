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
    IbkrBracket,
    IbkrOrder,
    UnsupportedOrderError,
    is_cancel_order_request,
    is_ibkr_order_request,
    is_modify_order_request,
    is_new_order_request,
    parse_ibkr_cancel,
    parse_ibkr_modify,
    parse_ibkr_order,
    parse_new_order_response_id,
    parse_new_order_response_ids,
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
        # /order/{orderId} (SINGULAR) is for cancel/modify — the
        # new-order matcher must not pick it up.
        flow = _make_flow(
            {"orders": [{}]},
            path="/v1/tv/iserver/account/U1234567/order/1134596995",
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

    def test_market_order(self):
        flow = _make_flow(self._full_body(orderType="MKT", price=None))
        o = parse_ibkr_order(flow)
        self.assertEqual(o.order_type, "MKT")
        self.assertIsNone(o.price)

    def test_stop_order(self):
        flow = _make_flow(self._full_body(orderType="STP", auxPrice=21000.0))
        o = parse_ibkr_order(flow)
        self.assertEqual(o.order_type, "STP")
        self.assertEqual(o.aux_price, 21000.0)

    def test_stop_limit_canonical_form(self):
        # IBKR sometimes emits "STPLMT" without space
        flow = _make_flow(self._full_body(
            orderType="STPLMT", price=21500.0, auxPrice=21490.0))
        o = parse_ibkr_order(flow)
        self.assertEqual(o.order_type, "STP LMT")   # canonical spaced form

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
              path="/v1/tv/iserver/account/U1234567/order/1134596995",
              host="api.ibkr.com"):
        return _make_empty_flow(method=method, path=path, host=host)

    def test_matches_delete_on_single_order_path(self):
        self.assertTrue(is_cancel_order_request(self._flow()))

    def test_rejects_delete_on_plural_orders_path(self):
        # Plural without an id is the new-order endpoint; cancel
        # must NOT pick it up.
        self.assertFalse(is_cancel_order_request(self._flow(
            path="/v1/tv/iserver/account/U1234567/orders")))

    def test_rejects_delete_on_non_ibkr_host(self):
        self.assertFalse(is_cancel_order_request(
            self._flow(host="api.tradovate.com")))

    def test_rejects_post(self):
        self.assertFalse(is_cancel_order_request(self._flow(method="POST")))

    def test_rejects_whatif_endpoint(self):
        # /order/whatif is the preview endpoint — never a cancel.
        self.assertFalse(is_cancel_order_request(self._flow(
            path="/v1/tv/iserver/account/U1234567/order/whatif")))

    def test_matches_delete_with_query_string(self):
        # TradingView Desktop appends `?manualIndicator=true` to the
        # DELETE URL when the user cancels an order from the chart.
        # Empirically confirmed against live captured traffic during
        # calibration; pre-fix, the regex's trailing `$` rejected the
        # query string and the cancel was silently dropped.
        self.assertTrue(is_cancel_order_request(self._flow(
            path="/v1/tv/iserver/account/U0000001/order/"
                 "1398750350?manualIndicator=true")))

    def test_parses_account_and_order_id(self):
        flow = self._flow(
            path="/v1/tv/iserver/account/U0000001/order/1134596995",
        )
        c = parse_ibkr_cancel(flow)
        self.assertEqual(c.account_id, "U0000001")
        self.assertEqual(c.ibkr_order_id, "1134596995")

    def test_parses_order_id_from_path_with_query_string(self):
        # Same as test_matches_delete_with_query_string, but
        # asserting that the order_id is extracted cleanly without
        # the query suffix being smuggled into it.
        flow = self._flow(
            path="/v1/tv/iserver/account/U0000001/order/"
                 "1398750350?manualIndicator=true",
        )
        c = parse_ibkr_cancel(flow)
        self.assertEqual(c.account_id, "U0000001")
        self.assertEqual(c.ibkr_order_id, "1398750350")


# ── Modify ─────────────────────────────────────────────────────────────── #

class TestModifyRequest(unittest.TestCase):

    def _flow(self, body=None, *, method="POST",
              path="/v1/tv/iserver/account/U1234567/order/1134596995"):
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
        self.assertEqual(m.ibkr_order_id, "1134596995")
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

    def test_parses_order_type_LMT(self):
        # The bug this fixes: pre-fix, the modify parser dropped the
        # orderType field on the floor, the replicator's modifyorder
        # call to Tradovate omitted it, and Tradovate rejected with
        # HTTP 400 "missing required field orderType".
        # Real body captured live during calibration:
        #   {"orderId": 424625077, "conid": 770561201,
        #    "orderType": "LMT", "price": 30180.5, ...}
        flow = self._flow(body={
            "orderId": 424625077, "conid": 770561201,
            "orderType": "LMT", "price": 30180.5, "auxPrice": 0,
            "quantity": 5, "side": "BUY", "tif": "DAY",
        })
        m = parse_ibkr_modify(flow)
        self.assertEqual(m.order_type, "LMT")

    def test_parses_order_type_STPLMT_canonicalised(self):
        # IBKR's body uses STPLMT (no space) on some payloads; the
        # parser must canonicalise to STP LMT so the order_type maps
        # cleanly via _ORDER_TYPE_MAP in the replicator.
        flow = self._flow(body={"orderType": "STPLMT", "price": 100.0})
        m = parse_ibkr_modify(flow)
        self.assertEqual(m.order_type, "STP LMT")

    def test_order_type_missing_returns_none(self):
        # Defensive: if a future TV build ever omits orderType from
        # the modify body, the parser must not crash. The replicator
        # has a separate guard that turns this into a divergence.
        flow = self._flow(body={"price": 100.0})
        m = parse_ibkr_modify(flow)
        self.assertIsNone(m.order_type)

    def test_order_type_uppercased(self):
        # Tolerate lowercase / mixed case input from any client.
        flow = self._flow(body={"orderType": "lmt", "price": 100.0})
        m = parse_ibkr_modify(flow)
        self.assertEqual(m.order_type, "LMT")


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


# ── Bracket / OCO parsing ──────────────────────────────────────────────── #

class TestBracketParsing(unittest.TestCase):
    """`parse_ibkr_order` returns IbkrBracket for a multi-leg POST
    whose legs form a valid entry+children structure."""

    def _entry(self, **overrides):
        base = {
            "cOID":      "tv-entry",
            "conid":     845307883,
            "orderType": "LMT",
            "price":     21500.0,
            "quantity":  2,
            "side":      "BUY",
            "tif":       "DAY",
            "acctId":    "U0000001",
        }
        base.update(overrides)
        return base

    def _child(self, **overrides):
        base = {
            "cOID":      "tv-tp",
            "parentId":  "tv-entry",
            "orderType": "LMT",
            "price":     21550.0,
            "quantity":  2,
            "side":      "SELL",
            "tif":       "DAY",
            "acctId":    "U0000001",
        }
        base.update(overrides)
        return base

    def _flow(self, *legs):
        return _make_flow({"orders": list(legs)})

    # ── happy paths ───────────────────────────────────────────────── #

    def test_entry_with_tp_and_sl_parses_as_bracket(self):
        tp = self._child(cOID="tv-tp", orderType="LMT", price=21550.0)
        sl = self._child(cOID="tv-sl", orderType="STP", auxPrice=21450.0,
                         price=None)
        result = parse_ibkr_order(self._flow(self._entry(), tp, sl))
        self.assertIsInstance(result, IbkrBracket)
        self.assertEqual(result.entry.cOID, "tv-entry")
        self.assertEqual(result.entry.side, "BUY")
        self.assertEqual(result.entry.order_type, "LMT")
        self.assertEqual(len(result.children), 2)
        # Children kept in body order
        self.assertEqual(result.children[0].cOID, "tv-tp")
        self.assertEqual(result.children[0].order_type, "LMT")
        self.assertEqual(result.children[0].price, 21550.0)
        self.assertEqual(result.children[1].cOID, "tv-sl")
        self.assertEqual(result.children[1].order_type, "STP")
        self.assertEqual(result.children[1].aux_price, 21450.0)
        # Both children inherit the opposite side
        for c in result.children:
            self.assertEqual(c.side, "SELL")

    def test_entry_with_single_child_is_accepted(self):
        # A 2-leg bracket is also valid (e.g. entry + just a stop)
        result = parse_ibkr_order(self._flow(
            self._entry(),
            self._child(cOID="tv-sl-only", orderType="STP",
                        auxPrice=21450.0, price=None),
        ))
        self.assertIsInstance(result, IbkrBracket)
        self.assertEqual(len(result.children), 1)
        self.assertEqual(result.children[0].cOID, "tv-sl-only")

    def test_stplmt_canonical_form_in_child(self):
        result = parse_ibkr_order(self._flow(
            self._entry(),
            self._child(cOID="tv-sl", orderType="STPLMT",
                        price=21450.0, auxPrice=21455.0),
        ))
        self.assertEqual(result.children[0].order_type, "STP LMT")

    def test_stp_child_trigger_in_price_field_normalises_to_aux(self):
        # TV's bracket-child convention: STP trigger comes in `price`,
        # NOT in `auxPrice`. This was the cause of a real Tradovate
        # rejection during calibration (HTTP 400 "Stop Price should
        # be specified"). The parser must normalise so the downstream
        # replicator sees the canonical IBKR shape:
        #   aux_price = trigger, price = None (no limit on pure STP)
        # Replays the exact body captured in the failing flow:
        #   {orderType: 'STP', price: 30126.5, quantity: 5, ...}
        result = parse_ibkr_order(self._flow(
            self._entry(orderType="MKT", price=None,
                        cOID="BQzcSmiEhRca"),
            self._child(parentId="BQzcSmiEhRca", cOID=None,
                        orderType="LMT", price=30321.5),
            # Note auxPrice INTENTIONALLY OMITTED — matches real TV body
            {"parentId": "BQzcSmiEhRca", "conid": 770561201,
             "quantity": 5, "side": "SELL", "tif": "GTC",
             "orderType": "STP", "price": 30126.5,
             "outsideRTH": False, "manualIndicator": True,
             "acctId": "U0000001"},
        ))
        self.assertIsInstance(result, IbkrBracket)
        sl_child = result.children[1]
        self.assertEqual(sl_child.order_type, "STP")
        # The fix: trigger moved from price → aux_price
        self.assertEqual(sl_child.aux_price, 30126.5,
            "STP child trigger price must end up in aux_price after "
            "normalisation — otherwise the Tradovate placeoso payload "
            "carries stop_price=None and gets HTTP 400.")
        self.assertIsNone(sl_child.price,
            "Pure STP has no limit price — price field should be None "
            "after we move the trigger to aux_price.")

    def test_stp_child_with_both_price_and_aux_unchanged(self):
        # Defensive: if TV ever DOES send both fields (or another
        # client / a future TV version), don't clobber them — the
        # normalisation only kicks in when aux_price is None.
        result = parse_ibkr_order(self._flow(
            self._entry(),
            self._child(cOID="tv-sl", orderType="STP",
                        price=21455.0, auxPrice=21450.0),
        ))
        sl = result.children[0]
        self.assertEqual(sl.aux_price, 21450.0)
        self.assertEqual(sl.price, 21455.0)

    def test_lmt_child_with_only_price_unchanged(self):
        # The normalisation must only target STP/STP LMT children —
        # plain LMT (take-profit) keeps its price exactly as is.
        result = parse_ibkr_order(self._flow(
            self._entry(),
            self._child(cOID="tv-tp", orderType="LMT", price=21550.0),
        ))
        tp = result.children[0]
        self.assertEqual(tp.price, 21550.0)
        self.assertIsNone(tp.aux_price)

    def test_stplmt_child_without_aux_uses_price_as_trigger(self):
        # Degenerate STP LMT body — TV sends only price, no auxPrice.
        # We mirror price → aux so Tradovate gets a usable trigger;
        # the resulting StopLimit will have limit == trigger, which
        # the user can refine on their side. Better than HTTP 400.
        result = parse_ibkr_order(self._flow(
            self._entry(),
            self._child(cOID="tv-sl", orderType="STPLMT",
                        price=21450.0),
        ))
        sl = result.children[0]
        self.assertEqual(sl.order_type, "STP LMT")
        self.assertEqual(sl.price, 21450.0)
        self.assertEqual(sl.aux_price, 21450.0)

    # ── single-leg still returns IbkrOrder ────────────────────────── #

    def test_single_leg_still_returns_ibkr_order(self):
        result = parse_ibkr_order(_make_flow({
            "orders": [self._entry()]
        }))
        self.assertIsInstance(result, IbkrOrder)
        self.assertNotIsInstance(result, IbkrBracket)

    # ── rejections ────────────────────────────────────────────────── #

    def test_rejects_bracket_with_no_entry(self):
        # All legs have parentId — no entry
        with self.assertRaises(UnsupportedOrderError):
            parse_ibkr_order(self._flow(
                self._child(cOID="tv-a", parentId="tv-x"),
                self._child(cOID="tv-b", parentId="tv-x"),
            ))

    def test_rejects_bracket_with_two_entries(self):
        # Two legs with no parentId — not a recognisable bracket.
        # This is also what the historical `test_rejects_multi_leg`
        # asserted, preserved here.
        body = {"orders": [self._entry(cOID="tv-a"),
                           self._entry(cOID="tv-b")]}
        with self.assertRaises(UnsupportedOrderError):
            parse_ibkr_order(_make_flow(body))

    def test_rejects_three_children(self):
        # Tradovate placeoso supports at most 2 brackets.
        with self.assertRaises(UnsupportedOrderError):
            parse_ibkr_order(self._flow(
                self._entry(),
                self._child(cOID="tv-c1"),
                self._child(cOID="tv-c2"),
                self._child(cOID="tv-c3"),
            ))

    def test_rejects_child_with_wrong_parent_id(self):
        with self.assertRaises(UnsupportedOrderError):
            parse_ibkr_order(self._flow(
                self._entry(cOID="tv-entry"),
                self._child(cOID="tv-tp", parentId="WRONG-PARENT"),
            ))

    def test_rejects_child_on_same_side_as_entry(self):
        # Entry BUY, child also BUY → not an exit, refuse to replicate
        # to avoid accidentally doubling the position.
        with self.assertRaises(UnsupportedOrderError):
            parse_ibkr_order(self._flow(
                self._entry(side="BUY"),
                self._child(side="BUY", orderType="LMT", price=21550.0),
            ))

    def test_rejects_child_with_market_order_type(self):
        # Brackets must be limit or stop — a market child is meaningless.
        with self.assertRaises(UnsupportedOrderError):
            parse_ibkr_order(self._flow(
                self._entry(),
                self._child(orderType="MKT"),
            ))

    def test_rejects_bracket_when_entry_has_no_coid(self):
        body = self._entry()
        del body["cOID"]
        with self.assertRaises(UnsupportedOrderError):
            parse_ibkr_order(self._flow(body, self._child()))


# ── Multi-id response parsing ──────────────────────────────────────────── #

class TestOrdersListPollResponse(unittest.TestCase):
    """Coverage for the /v1/tv/iserver/account/orders parser used to
    bind bracket-child IBKR ids back to the synthetic cOIDs the
    replicator registered at placeoso time. The TV-side fix for
    bracket cancel/modify (which silently no-op'd before because the
    child IBKR ids were never bound to anything in the order map)
    depends on this path being right."""

    @staticmethod
    def _orders_list_flow(body: dict, *, status: int = 200,
                          method: str = "GET",
                          path: str = "/v1/tv/iserver/account/orders") -> _Flow:
        raw = json.dumps(body).encode("utf-8")
        return _Flow(
            request=_Req(method=method, pretty_host="api.ibkr.com",
                         path=path, content=b""),
            response=_Resp(status_code=status, content=raw),
        )

    def test_is_orders_list_response_matches_canonical_path(self):
        from tradesync.proxy.ibkr_parser import is_orders_list_response
        flow = self._orders_list_flow({"orders": []})
        self.assertTrue(is_orders_list_response(flow))

    def test_is_orders_list_response_accepts_query_string(self):
        from tradesync.proxy.ibkr_parser import is_orders_list_response
        flow = self._orders_list_flow(
            {"orders": []},
            path="/v1/tv/iserver/account/orders?force=false&accountId=U0000001",
        )
        self.assertTrue(is_orders_list_response(flow))

    def test_is_orders_list_response_rejects_other_methods(self):
        from tradesync.proxy.ibkr_parser import is_orders_list_response
        for method in ("POST", "DELETE", "PUT", "OPTIONS"):
            flow = self._orders_list_flow({"orders": []}, method=method)
            self.assertFalse(is_orders_list_response(flow),
                             f"{method} should NOT count as orders-list")

    def test_is_orders_list_response_rejects_non_200(self):
        from tradesync.proxy.ibkr_parser import is_orders_list_response
        flow = self._orders_list_flow({"orders": []}, status=500)
        self.assertFalse(is_orders_list_response(flow))

    def test_is_orders_list_response_rejects_account_scoped_path(self):
        """The account-scoped path /v1/tv/iserver/account/<id>/orders
        is the NEW-ORDER POST endpoint (with method=POST). It must NOT
        match the orders-list detector even on GET — the body shape is
        different and we'd parse it incorrectly."""
        from tradesync.proxy.ibkr_parser import is_orders_list_response
        flow = self._orders_list_flow(
            {"orders": []},
            path="/v1/tv/iserver/account/U0000001/orders",
        )
        self.assertFalse(is_orders_list_response(flow))

    def test_parse_extracts_one_lmt_child(self):
        """`parentId` in the IBKR /orders payload is the entry's
        IBKR ORDER ID, not its cOID — confirmed by live capture on
        2026-06-07. The parser surfaces it verbatim; resolving it
        to the entry's cOID is the caller's job."""
        from tradesync.proxy.ibkr_parser import parse_orders_list_bracket_children
        body = json.dumps({
            "orders": [
                {  # the entry — has a cOID, no parentId → skipped
                    "orderId": 319073567, "cOID": "ENTRY1",
                    "orderType": "MKT",
                },
                {  # the take-profit child
                    "orderId": 319073568, "parentId": 319073567,
                    "orderType": "LMT",
                },
            ],
        }).encode()
        result = parse_orders_list_bracket_children(body)
        self.assertEqual(result, [("319073567", "319073568", "LMT")])

    def test_parse_extracts_lmt_and_stp_children(self):
        from tradesync.proxy.ibkr_parser import parse_orders_list_bracket_children
        body = json.dumps({
            "orders": [
                {"orderId": 319073567, "cOID": "ENTRY1",
                 "orderType": "MKT"},
                {"orderId": 319073568, "parentId": 319073567,
                 "orderType": "LMT"},
                {"orderId": 319073569, "parentId": 319073567,
                 "orderType": "STP"},
            ],
        }).encode()
        result = parse_orders_list_bracket_children(body)
        self.assertEqual(sorted(result), sorted([
            ("319073567", "319073568", "LMT"),
            ("319073567", "319073569", "STP"),
        ]))

    def test_parse_handles_snake_case_field_names(self):
        """Some IBKR API revisions use snake_case (parent_id /
        order_id / order_type) instead of the camelCase variant.
        Tolerate both — the production-truth source is the live
        traffic dump, not the docs."""
        from tradesync.proxy.ibkr_parser import parse_orders_list_bracket_children
        body = json.dumps({
            "orders": [
                {"order_id": 200, "parent_id": 100,
                 "order_type": "STP"},
            ],
        }).encode()
        result = parse_orders_list_bracket_children(body)
        self.assertEqual(result, [("100", "200", "STP")])

    def test_parse_normalises_order_type_to_canonical_role(self):
        """The role token embedded in the synthetic cOID must match
        EXACTLY what the replicator emits — otherwise the binding
        never connects. STOP / Limit / STP_LMT all reach the
        same canonical roles {STP, LMT, STPLMT}."""
        from tradesync.proxy.ibkr_parser import parse_orders_list_bracket_children
        body = json.dumps({
            "orders": [
                {"orderId": 1, "parentId": 99, "orderType": "Limit"},
                {"orderId": 2, "parentId": 99, "orderType": "Stop"},
                {"orderId": 3, "parentId": 99, "orderType": "STP_LMT"},
            ],
        }).encode()
        result = parse_orders_list_bracket_children(body)
        roles = [r for (_, _, r) in result]
        self.assertEqual(sorted(roles), ["LMT", "STP", "STPLMT"])

    def test_parse_skips_non_bracket_orders(self):
        """A standalone (non-bracket) entry has no parentId — must be
        excluded. We're only here to bind CHILDREN."""
        from tradesync.proxy.ibkr_parser import parse_orders_list_bracket_children
        body = json.dumps({
            "orders": [
                {"orderId": 100, "cOID": "STANDALONE", "orderType": "LMT"},
            ],
        }).encode()
        self.assertEqual(parse_orders_list_bracket_children(body), [])

    def test_parse_skips_unrecognised_order_types(self):
        """If IBKR introduces a new bracket leg type we don't know
        about, skip it rather than guess the role. Better to leave
        that leg unbound (cancel/modify won't replicate on it) than
        to bind it under a wrong synth cOID and corrupt the chain."""
        from tradesync.proxy.ibkr_parser import parse_orders_list_bracket_children
        body = json.dumps({
            "orders": [
                {"orderId": 999, "parentId": 100, "orderType": "TRAIL"},
            ],
        }).encode()
        self.assertEqual(parse_orders_list_bracket_children(body), [])

    def test_parse_handles_gzipped_body(self):
        """In production we observe gzip-compressed bodies from IBKR
        (the passive contract cache fix 2baca5d already had to deal
        with this). The orders-list parser must transparently decode."""
        import gzip
        from tradesync.proxy.ibkr_parser import parse_orders_list_bracket_children
        raw = json.dumps({
            "orders": [
                {"orderId": 77, "parentId": 33, "orderType": "STP"},
            ],
        }).encode()
        compressed = gzip.compress(raw)
        self.assertEqual(compressed[:2], b"\x1f\x8b")  # gzip magic
        self.assertEqual(
            parse_orders_list_bracket_children(compressed),
            [("33", "77", "STP")],
        )

    def test_parse_is_silent_on_malformed_input(self):
        """Any kind of corrupt payload returns [] — we'd rather miss
        a binding window than crash the response hook."""
        from tradesync.proxy.ibkr_parser import parse_orders_list_bracket_children
        for body in (b"", b"not-json", b"{}", b"[]",
                     json.dumps({"orders": "not-a-list"}).encode(),
                     json.dumps({"orders": [None, 42, "string"]}).encode()):
            self.assertEqual(parse_orders_list_bracket_children(body), [],
                             f"failed for body={body!r}")


class TestParseNewOrderResponseIds(unittest.TestCase):
    """parse_new_order_response_ids returns ONE pair per leg, in
    body order. Used by the addon to bind every cOID in a bracket
    to its IBKR-assigned order_id."""

    def test_returns_all_pairs_from_array_with_coids(self):
        body = [
            {"order_id": "i-1", "cOID": "c-1"},
            {"order_id": "i-2", "cOID": "c-2"},
            {"order_id": "i-3", "cOID": "c-3"},
        ]
        f = _make_response_flow(200, body)
        pairs = parse_new_order_response_ids(f)
        self.assertEqual(pairs, [
            ("c-1", "i-1"), ("c-2", "i-2"), ("c-3", "i-3"),
        ])

    def test_pairs_with_no_coid_in_response(self):
        # IBKR returns order_ids but doesn't echo the cOID — the
        # addon will fall back to positional matching with the
        # stashed request cOIDs.
        body = [{"order_id": "i-1"}, {"order_id": "i-2"}]
        pairs = parse_new_order_response_ids(_make_response_flow(200, body))
        self.assertEqual(pairs, [(None, "i-1"), (None, "i-2")])

    def test_single_dict_response_yields_one_pair(self):
        body = {"order_id": "i-1", "cOID": "c-1"}
        pairs = parse_new_order_response_ids(_make_response_flow(200, body))
        self.assertEqual(pairs, [("c-1", "i-1")])

    def test_nested_orders_array(self):
        body = {"orders": [
            {"orderId": "i-1", "cOID": "c-1"},
            {"orderId": "i-2", "cOID": "c-2"},
        ]}
        pairs = parse_new_order_response_ids(_make_response_flow(200, body))
        self.assertEqual(pairs, [("c-1", "i-1"), ("c-2", "i-2")])

    def test_skips_entries_without_an_order_id(self):
        body = [
            {"order_id": "i-1", "cOID": "c-1"},
            {"cOID": "c-2-no-id"},        # malformed, skipped
            {"order_id": "i-3", "cOID": "c-3"},
        ]
        pairs = parse_new_order_response_ids(_make_response_flow(200, body))
        self.assertEqual(pairs, [("c-1", "i-1"), ("c-3", "i-3")])

    def test_back_compat_singular_returns_first_id(self):
        body = [
            {"order_id": "i-1", "cOID": "c-1"},
            {"order_id": "i-2", "cOID": "c-2"},
        ]
        f = _make_response_flow(200, body)
        # The legacy singular helper just returns the first id.
        self.assertEqual(parse_new_order_response_id(f), "i-1")


if __name__ == "__main__":
    unittest.main()
