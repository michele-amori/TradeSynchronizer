"""
Tests for TradovateEndpoint — the FollowerEndpoint adapter over
TradovateClient.

These pin the neutral→Tradovate translation: given OrderSpec /
BracketSpec / ModifySpec in the broker-neutral vocabulary, the adapter
must call the underlying TradovateClient with exactly the right
Tradovate wire fields (Buy/Sell, Market/Limit/Stop/StopLimit, Day/GTC,
price gating by order type), and map the results back into the neutral
PlacedRef / PlacedBracketRef.
"""

import unittest

from tradesync.brokers.endpoint import (
    FollowerEndpoint,
    PlacedBracketRef,
    PlacedRef,
)
from tradesync.brokers.tradovate import PlacedBracket, PlacedOrder
from tradesync.brokers.tradovate_endpoint import TradovateEndpoint
from tradesync.order_event import (
    BracketRole,
    BracketSpec,
    ModifySpec,
    OrderSpec,
    OrderType,
    Side,
    TimeInForce,
)


class FakeTradovateClient:
    """Records calls and returns canned placement results."""

    def __init__(self):
        self.place_order_calls = []
        self.place_bracket_calls = []
        self.cancel_calls = []
        self.modify_calls = []
        self.status_calls = []
        self.contract_calls = []
        self.connected_count = 0
        self.next_id = 1000

    def connect(self):
        self.connected_count += 1

    def get_contract_id(self, symbol):
        self.contract_calls.append(symbol)
        return 4327110

    def place_order(self, **kwargs):
        self.place_order_calls.append(kwargs)
        oid = self.next_id
        self.next_id += 1
        return PlacedOrder(order_id=oid, raw={"orderId": oid})

    def place_bracket(self, **kwargs):
        self.place_bracket_calls.append(kwargs)
        entry = self.next_id
        self.next_id += 1
        n = len(kwargs.get("brackets") or [])
        children = []
        for _ in range(n):
            children.append(self.next_id)
            self.next_id += 1
        return PlacedBracket(
            entry_order_id=entry, bracket_ids=children,
            oco_id=None, raw={"orderId": entry},
        )

    def cancel_order(self, order_id):
        self.cancel_calls.append(order_id)
        return {"ok": True}

    def modify_order(self, order_id, **kwargs):
        self.modify_calls.append((order_id, kwargs))
        return {"ok": True}

    def get_order_status(self, order_id):
        self.status_calls.append(order_id)
        return "Working"


def _endpoint():
    client = FakeTradovateClient()
    ep = TradovateEndpoint(client, env="live", account_id="19000001")
    return ep, client


class TestIdentityAndProtocol(unittest.TestCase):

    def test_identity(self):
        ep, _ = _endpoint()
        self.assertEqual(ep.identity, "tradovate_live_19000001")

    def test_satisfies_follower_protocol(self):
        ep, _ = _endpoint()
        self.assertIsInstance(ep, FollowerEndpoint)

    def test_connect_delegates(self):
        ep, client = _endpoint()
        ep.connect()
        self.assertEqual(client.connected_count, 1)


class TestPlaceOrderTranslation(unittest.TestCase):

    def test_market_buy(self):
        ep, client = _endpoint()
        ref = ep.place_order(
            OrderSpec(side=Side.BUY, quantity=2, order_type=OrderType.MARKET),
            symbol="MNQM6",
        )
        self.assertIsInstance(ref, PlacedRef)
        self.assertEqual(client.contract_calls, ["MNQM6"])
        call = client.place_order_calls[0]
        self.assertEqual(call["action"], "Buy")
        self.assertEqual(call["order_type"], "Market")
        self.assertEqual(call["qty"], 2)
        self.assertEqual(call["contract_id"], 4327110)
        # Market: no prices
        self.assertIsNone(call["limit_price"])
        self.assertIsNone(call["stop_price"])
        self.assertEqual(call["tif"], "Day")
        self.assertEqual(ref.follower_order_id, "1000")

    def test_limit_sell_gtc(self):
        ep, client = _endpoint()
        ep.place_order(
            OrderSpec(side=Side.SELL, quantity=1, order_type=OrderType.LIMIT,
                      limit_price=29292.0, stop_price=None,
                      tif=TimeInForce.GTC),
            symbol="MNQM6",
        )
        call = client.place_order_calls[0]
        self.assertEqual(call["action"], "Sell")
        self.assertEqual(call["order_type"], "Limit")
        self.assertEqual(call["limit_price"], 29292.0)
        # Limit must NOT carry a stop price
        self.assertIsNone(call["stop_price"])
        self.assertEqual(call["tif"], "GTC")

    def test_stop_gates_prices(self):
        ep, client = _endpoint()
        ep.place_order(
            OrderSpec(side=Side.SELL, quantity=1, order_type=OrderType.STOP,
                      limit_price=None, stop_price=28942.0),
            symbol="MNQM6",
        )
        call = client.place_order_calls[0]
        self.assertEqual(call["order_type"], "Stop")
        self.assertEqual(call["stop_price"], 28942.0)
        # Stop must NOT carry a limit price
        self.assertIsNone(call["limit_price"])

    def test_stop_limit_carries_both(self):
        ep, client = _endpoint()
        ep.place_order(
            OrderSpec(side=Side.BUY, quantity=1,
                      order_type=OrderType.STOP_LIMIT,
                      limit_price=100.0, stop_price=99.0),
            symbol="MNQM6",
        )
        call = client.place_order_calls[0]
        self.assertEqual(call["order_type"], "StopLimit")
        self.assertEqual(call["limit_price"], 100.0)
        self.assertEqual(call["stop_price"], 99.0)


class TestPlaceBracketTranslation(unittest.TestCase):

    def _bracket(self):
        entry = OrderSpec(side=Side.BUY, quantity=2,
                          order_type=OrderType.MARKET,
                          role=BracketRole.ENTRY)
        tp = OrderSpec(side=Side.SELL, quantity=2,
                       order_type=OrderType.LIMIT, limit_price=29292.0,
                       role=BracketRole.TAKE_PROFIT)
        sl = OrderSpec(side=Side.SELL, quantity=2,
                       order_type=OrderType.STOP, stop_price=28942.0,
                       role=BracketRole.STOP_LOSS)
        return BracketSpec(entry=entry, children=[tp, sl])

    def test_bracket_entry_and_children_translated(self):
        ep, client = _endpoint()
        ref = ep.place_bracket(self._bracket(), symbol="MNQM6")
        self.assertIsInstance(ref, PlacedBracketRef)
        call = client.place_bracket_calls[0]
        self.assertEqual(call["entry_action"], "Buy")
        self.assertEqual(call["entry_order_type"], "Market")
        self.assertEqual(call["entry_qty"], 2)
        # children payloads
        brackets = call["brackets"]
        self.assertEqual(len(brackets), 2)
        tp, sl = brackets
        self.assertEqual(tp["order_type"], "Limit")
        self.assertEqual(tp["limit_price"], 29292.0)
        self.assertIsNone(tp["stop_price"])
        self.assertEqual(sl["order_type"], "Stop")
        self.assertEqual(sl["stop_price"], 28942.0)
        self.assertIsNone(sl["limit_price"])

    def test_bracket_result_ids_stringified(self):
        ep, client = _endpoint()
        ref = ep.place_bracket(self._bracket(), symbol="MNQM6")
        # FakeClient assigns entry=1000, children=1001,1002
        self.assertEqual(ref.entry_order_id, "1000")
        self.assertEqual(ref.child_order_ids, ["1001", "1002"])
        self.assertIsNone(ref.oco_id)


class TestCancelModifyStatus(unittest.TestCase):

    def test_cancel_stringid_to_int(self):
        ep, client = _endpoint()
        ep.cancel_order("11978727757")
        self.assertEqual(client.cancel_calls, [11978727757])

    def test_modify_translates_limit(self):
        ep, client = _endpoint()
        ep.modify_order("123", ModifySpec(
            new_limit_price=105.0, order_type=OrderType.LIMIT))
        oid, kwargs = client.modify_calls[0]
        self.assertEqual(oid, 123)
        self.assertEqual(kwargs["order_type"], "Limit")
        self.assertEqual(kwargs["limit_price"], 105.0)
        self.assertIsNone(kwargs["stop_price"])

    def test_modify_translates_stop(self):
        ep, client = _endpoint()
        ep.modify_order("123", ModifySpec(
            new_stop_price=99.0, order_type=OrderType.STOP))
        _oid, kwargs = client.modify_calls[0]
        self.assertEqual(kwargs["order_type"], "Stop")
        self.assertEqual(kwargs["stop_price"], 99.0)
        self.assertIsNone(kwargs["limit_price"])

    def test_modify_requires_order_type(self):
        ep, _ = _endpoint()
        with self.assertRaises(ValueError):
            ep.modify_order("123", ModifySpec(new_quantity=3))

    def test_order_status_delegates(self):
        ep, client = _endpoint()
        self.assertEqual(ep.order_status("555"), "Working")
        self.assertEqual(client.status_calls, [555])


if __name__ == "__main__":
    unittest.main()
