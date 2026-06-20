"""
Integration test: REAL captured Tradovate push frames driven through
the WHOLE Tradovate-source pipeline — TradovateWSObserver._handle_array
→ stateful TradovatePushParser → on_event → EventReplicator → fake
FollowerEndpoint — proving the entire chain end to end against the
actual wire shape captured live 2026-06-14 (MNQM2026 bracket on DEMO
account 50000001, captures/tradovate_frames_2026-06-14.json).

This is the post-calibration version: it no longer injects a provisional
parser. It feeds frames in the exact shape Tradovate really sends and
asserts the follower receives the correctly-translated bracket / cancel.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from tradesync.brokers.endpoint import PlacedRef, PlacedBracketRef
from tradesync.brokers.tradovate_ws_observer import TradovateWSObserver
from tradesync.event_replicator import EventReplicator
from tradesync.order_event import BracketRole, OrderType, Side
from tradesync.order_map import OrderMap


CAPTURE = (Path(__file__).resolve().parent.parent /
           "captures" / "tradovate_frames_2026-06-14.json")


class FakeFollower:
    def __init__(self):
        self.placed = []
        self.brackets = []
        self.cancelled = []
        self._next = 9000

    @property
    def identity(self):
        return "fake_ibkr_follower"

    def connect(self):
        pass

    def disconnect(self):
        pass

    def place_order(self, spec, *, symbol):
        self.placed.append((spec, symbol))
        oid = self._next
        self._next += 1
        return PlacedRef(follower_order_id=str(oid))

    def place_bracket(self, spec, *, symbol):
        self.brackets.append((spec, symbol))
        eid = self._next
        self._next += 1
        return PlacedBracketRef(
            entry_order_id=str(eid),
            child_order_ids=[str(eid + 1), str(eid + 2)])

    def cancel_order(self, follower_order_id):
        self.cancelled.append(follower_order_id)

    def modify_order(self, follower_order_id, changes):
        pass

    def order_status(self, follower_order_id):
        return "Working"


def _build_observer(account_id="50000001", symbol="MNQM6"):
    client = MagicMock()
    client.connected = True
    client.user_id = 3701228
    client._access_token = "tok"
    # contractId → symbol resolution the parser will call.
    client.get_contract_name.return_value = symbol
    return TradovateWSObserver(client, env="demo", account_id=account_id)


class TestRealCaptureThroughPipeline(unittest.TestCase):
    """Drive the real captured frames through the full chain."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.order_map = OrderMap(Path(self._tmp.name) / "orders.json")
        self.follower = FakeFollower()
        self.replicator = EventReplicator(
            follower=self.follower, order_map=self.order_map,
            watched_source_accounts=["50000001"])
        self.observer = _build_observer()
        self._results = []
        self.observer.start_observing(
            lambda ev: self._results.append(self.replicator.apply(ev)))

    def tearDown(self):
        self._tmp.cleanup()

    def _feed_capture(self):
        data = json.loads(CAPTURE.read_text())
        for p in data["pushes"]:
            # Re-wrap each captured message as the 'a[...]' frame body
            # the observer's _handle_array consumes.
            self.observer._handle_array(json.dumps([p["msg"]]))

    def test_real_bracket_reaches_follower(self):
        self._feed_capture()

        # The captured session placed exactly one bracket (entry +
        # TP + SL), then modified and cancelled it.
        self.assertEqual(len(self.follower.brackets), 1,
                         "expected exactly one bracket placed on follower")
        spec, symbol = self.follower.brackets[0]
        self.assertEqual(symbol, "MNQM6")

        # Entry: Buy Limit @ 29506.25
        self.assertIs(spec.entry.side, Side.BUY)
        self.assertIs(spec.entry.order_type, OrderType.LIMIT)
        self.assertEqual(spec.entry.limit_price, 29506.25)

        # Two OCO children: a take-profit Limit and a stop-loss Stop.
        self.assertEqual(len(spec.children), 2)
        roles = {c.role for c in spec.children}
        self.assertEqual(roles, {BracketRole.TAKE_PROFIT,
                                 BracketRole.STOP_LOSS})
        tp = next(c for c in spec.children
                  if c.role is BracketRole.TAKE_PROFIT)
        sl = next(c for c in spec.children
                  if c.role is BracketRole.STOP_LOSS)
        self.assertIs(tp.order_type, OrderType.LIMIT)
        self.assertEqual(tp.limit_price, 29563.0)
        self.assertIs(sl.order_type, OrderType.STOP)
        self.assertEqual(sl.stop_price, 29458.5)

    def test_real_cancel_reaches_follower(self):
        self._feed_capture()
        # The session ended by cancelling the bracket; the entry's
        # cancel must reach the follower (mapped via the bracket entry).
        self.assertGreaterEqual(len(self.follower.cancelled), 1)

    def test_other_account_filtered(self):
        # Same frames, but observe a different account → nothing placed.
        other = _build_observer(account_id="99999999")
        results = []
        rep = EventReplicator(
            follower=self.follower, order_map=self.order_map,
            watched_source_accounts=["99999999"])
        other.start_observing(lambda ev: results.append(rep.apply(ev)))
        data = json.loads(CAPTURE.read_text())
        for p in data["pushes"]:
            other._handle_array(json.dumps([p["msg"]]))
        # The captured frames are all for 50000001, so an observer of
        # 99999999 sees nothing actionable.
        self.assertEqual(self.follower.brackets, [])
        self.assertEqual(self.follower.placed, [])


class TestParserUnit(unittest.TestCase):
    """Direct unit checks on the parser via the observer's push path,
    using small synthetic frames in the REAL shape."""

    def setUp(self):
        self.observer = _build_observer()
        self.events = []
        self.observer.start_observing(self.events.append)

    def _push(self, entity_type, event_type, entity):
        self.observer._handle_array(json.dumps([{
            "e": "props",
            "d": {"entityType": entity_type, "eventType": event_type,
                  "entity": entity}}]))

    def test_single_order_new(self):
        # A non-strategy order: order + orderVersion + executionReport New
        self._push("order", "Created", {
            "id": 1, "accountId": 50000001, "contractId": 4327110,
            "action": "Buy", "ordStatus": "Working"})
        self._push("orderVersion", "Created", {
            "id": 1, "orderId": 1, "orderQty": 2, "orderType": "Limit",
            "price": 100.5, "timeInForce": "Day"})
        self._push("executionReport", "Created", {
            "orderId": 1, "accountId": 50000001, "execType": "New"})
        news = [e for e in self.events if e.kind.value == "NEW"]
        self.assertEqual(len(news), 1)
        self.assertIsNotNone(news[0].order)
        self.assertEqual(news[0].order.limit_price, 100.5)
        self.assertEqual(news[0].symbol, "MNQM6")

    def test_single_order_cancel(self):
        self._push("order", "Created", {
            "id": 2, "accountId": 50000001, "contractId": 4327110,
            "action": "Buy", "ordStatus": "Working"})
        self._push("orderVersion", "Created", {
            "id": 2, "orderId": 2, "orderQty": 1, "orderType": "Market"})
        self._push("executionReport", "Created", {
            "orderId": 2, "accountId": 50000001, "execType": "New"})
        self._push("executionReport", "Created", {
            "orderId": 2, "accountId": 50000001, "execType": "Canceled"})
        kinds = [e.kind.value for e in self.events]
        self.assertIn("CANCEL", kinds)


if __name__ == "__main__":
    unittest.main()
