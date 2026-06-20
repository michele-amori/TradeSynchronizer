"""
Unit tests for TradovatePushParser — the stateful translator from
Tradovate WS push entity frames to broker-neutral OrderEvents,
calibrated against real frames captured 2026-06-14.

Frames are fed in the REAL shape and arrival order observed live. A
fake resolver stands in for contractId → symbol so no network is
needed.
"""

import json
import unittest
from pathlib import Path

from tradesync.brokers.tradovate_push_parser import TradovatePushParser
from tradesync.order_event import BracketRole, EventKind, OrderType, Side


CAPTURE = (Path(__file__).resolve().parent.parent /
           "captures" / "tradovate_frames_2026-06-14.json")


def _parser(account="50000001"):
    return TradovatePushParser(
        account, resolve_symbol=lambda cid: "MNQM6" if cid == 4327110
        else f"C{cid}")


def _h(parser, et, ev, entity):
    return parser.handle(et, ev, entity)


class TestSingleOrder(unittest.TestCase):

    def test_new_single_limit(self):
        p = _parser()
        _h(p, "order", "Created", {
            "id": 1, "accountId": 50000001, "contractId": 4327110,
            "action": "Buy", "ordStatus": "Working"})
        _h(p, "orderVersion", "Created", {
            "id": 1, "orderId": 1, "orderQty": 3, "orderType": "Limit",
            "price": 101.25, "timeInForce": "Day"})
        out = _h(p, "executionReport", "Created", {
            "orderId": 1, "accountId": 50000001, "execType": "New"})
        self.assertEqual(len(out), 1)
        e = out[0]
        self.assertIs(e.kind, EventKind.NEW)
        self.assertIsNotNone(e.order)
        self.assertIs(e.order.side, Side.BUY)
        self.assertIs(e.order.order_type, OrderType.LIMIT)
        self.assertEqual(e.order.quantity, 3)
        self.assertEqual(e.order.limit_price, 101.25)
        self.assertEqual(e.symbol, "MNQM6")

    def test_new_single_stop_uses_stop_price(self):
        p = _parser()
        _h(p, "order", "Created", {
            "id": 5, "accountId": 50000001, "contractId": 4327110,
            "action": "Sell", "ordStatus": "Working"})
        _h(p, "orderVersion", "Created", {
            "id": 5, "orderId": 5, "orderQty": 1, "orderType": "Stop",
            "stopPrice": 98.0, "timeInForce": "Day"})
        out = _h(p, "executionReport", "Created", {
            "orderId": 5, "accountId": 50000001, "execType": "New"})
        self.assertEqual(out[0].order.order_type, OrderType.STOP)
        self.assertEqual(out[0].order.stop_price, 98.0)
        self.assertIsNone(out[0].order.limit_price)

    def test_new_single_market_has_no_prices(self):
        # A Market order carries no price/stopPrice on the wire; the
        # neutral spec must come through as MARKET with both None.
        p = _parser()
        _h(p, "order", "Created", {
            "id": 7, "accountId": 50000001, "contractId": 4327110,
            "action": "Buy", "ordStatus": "Working"})
        _h(p, "orderVersion", "Created", {
            "id": 7, "orderId": 7, "orderQty": 2, "orderType": "Market",
            "timeInForce": "Day"})
        out = _h(p, "executionReport", "Created", {
            "orderId": 7, "accountId": 50000001, "execType": "New"})
        self.assertEqual(len(out), 1)
        self.assertIs(out[0].order.order_type, OrderType.MARKET)
        self.assertEqual(out[0].order.quantity, 2)
        self.assertIsNone(out[0].order.limit_price)
        self.assertIsNone(out[0].order.stop_price)
        self.assertEqual(out[0].symbol, "MNQM6")

    def test_market_new_then_fill(self):
        # A Market order fills immediately: New then Filled. The NEW
        # must be emitted (so it replicates) and the Filled surfaces a
        # FILL (informational), in that order.
        p = _parser()
        _h(p, "order", "Created", {
            "id": 8, "accountId": 50000001, "contractId": 4327110,
            "action": "Buy", "ordStatus": "Working"})
        _h(p, "orderVersion", "Created", {
            "id": 8, "orderId": 8, "orderQty": 1, "orderType": "Market"})
        out_new = _h(p, "executionReport", "Created", {
            "orderId": 8, "accountId": 50000001, "execType": "New"})
        out_fill = _h(p, "executionReport", "Created", {
            "orderId": 8, "accountId": 50000001, "execType": "Filled"})
        self.assertEqual(len(out_new), 1)
        self.assertIs(out_new[0].kind, EventKind.NEW)
        self.assertEqual(len(out_fill), 1)
        self.assertIs(out_fill[0].kind, EventKind.FILL)

    def test_new_emitted_once(self):
        p = _parser()
        _h(p, "order", "Created", {
            "id": 1, "accountId": 50000001, "contractId": 4327110,
            "action": "Buy", "ordStatus": "Working"})
        _h(p, "orderVersion", "Created", {
            "id": 1, "orderId": 1, "orderQty": 1, "orderType": "Market"})
        first = _h(p, "executionReport", "Created", {
            "orderId": 1, "accountId": 50000001, "execType": "New"})
        second = _h(p, "executionReport", "Created", {
            "orderId": 1, "accountId": 50000001, "execType": "New"})
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])

    def test_modify_single(self):
        p = _parser()
        _h(p, "order", "Created", {
            "id": 1, "accountId": 50000001, "contractId": 4327110,
            "action": "Buy", "ordStatus": "Working"})
        _h(p, "orderVersion", "Created", {
            "id": 1, "orderId": 1, "orderQty": 1, "orderType": "Limit",
            "price": 100.0})
        _h(p, "executionReport", "Created", {
            "orderId": 1, "accountId": 50000001, "execType": "New"})
        _h(p, "orderVersion", "Created", {
            "id": 2, "orderId": 1, "orderQty": 1, "orderType": "Limit",
            "price": 105.0})
        out = _h(p, "executionReport", "Created", {
            "orderId": 1, "accountId": 50000001, "execType": "Replaced"})
        self.assertEqual(len(out), 1)
        self.assertIs(out[0].kind, EventKind.MODIFY)
        self.assertEqual(out[0].modify.new_limit_price, 105.0)

    def test_cancel(self):
        p = _parser()
        _h(p, "order", "Created", {
            "id": 1, "accountId": 50000001, "contractId": 4327110,
            "action": "Buy", "ordStatus": "Working"})
        _h(p, "orderVersion", "Created", {
            "id": 1, "orderId": 1, "orderQty": 1, "orderType": "Market"})
        out = _h(p, "executionReport", "Created", {
            "orderId": 1, "accountId": 50000001, "execType": "Canceled"})
        self.assertEqual(len(out), 1)
        self.assertIs(out[0].kind, EventKind.CANCEL)
        self.assertEqual(out[0].source_order_id, "1")

    def test_other_account_ignored(self):
        p = _parser()
        out = _h(p, "executionReport", "Created", {
            "orderId": 1, "accountId": 88888888, "execType": "New"})
        self.assertEqual(out, [])


class TestBracket(unittest.TestCase):
    """Bracket assembly in the real arrival order: entry executionReport
    New arrives BEFORE the child legs link in; the strategy params give
    the expected leg count so we hold until all legs are present."""

    def _place_bracket(self, p):
        # entry order + version
        _h(p, "order", "Created", {
            "id": 15, "accountId": 50000001, "contractId": 4327110,
            "action": "Buy", "ordStatus": "Working"})
        _h(p, "orderVersion", "Created", {
            "id": 15, "orderId": 15, "orderQty": 1, "orderType": "Limit",
            "price": 29506.25})
        _h(p, "orderStrategyLink", "Created", {
            "id": 15, "orderId": 15, "orderStrategyId": 14})
        _h(p, "orderStrategy", "Created", {
            "id": 14, "accountId": 50000001, "contractId": 4327110,
            "action": "Buy", "params": json.dumps({
                "entryVersion": {"orderType": "Limit", "price": 29506.25},
                "brackets": [{"qty": 1, "profitTarget": 29563.0,
                              "stopLoss": 29458.5}]})})
        # entry's New arrives now — bracket not complete yet (children
        # haven't linked) so nothing emitted.
        out_entry_new = _h(p, "executionReport", "Created", {
            "orderId": 15, "accountId": 50000001, "execType": "New"})
        # TP leg
        _h(p, "order", "Created", {
            "id": 18, "accountId": 50000001, "contractId": 4327110,
            "action": "Sell", "ordStatus": "Suspended"})
        _h(p, "orderVersion", "Created", {
            "id": 18, "orderId": 18, "orderQty": 1, "orderType": "Limit",
            "price": 29563.0})
        _h(p, "orderStrategyLink", "Created", {
            "id": 18, "orderId": 18, "orderStrategyId": 14})
        # SL leg — the LAST one; emission should happen here.
        _h(p, "order", "Created", {
            "id": 20, "accountId": 50000001, "contractId": 4327110,
            "action": "Sell", "ordStatus": "Suspended"})
        _h(p, "orderVersion", "Created", {
            "id": 20, "orderId": 20, "orderQty": 1, "orderType": "Stop",
            "stopPrice": 29458.5})
        out_last = _h(p, "orderStrategyLink", "Created", {
            "id": 20, "orderId": 20, "orderStrategyId": 14})
        return out_entry_new, out_last

    def test_bracket_emitted_once_all_legs_present(self):
        p = _parser()
        out_entry_new, out_last = self._place_bracket(p)
        # Entry's New alone does NOT emit (children not yet linked).
        self.assertEqual(out_entry_new, [])
        # The final leg linking in completes + emits the bracket.
        self.assertEqual(len(out_last), 1)
        e = out_last[0]
        self.assertIs(e.kind, EventKind.NEW)
        self.assertIsNotNone(e.bracket)
        self.assertEqual(e.symbol, "MNQM6")
        self.assertIs(e.bracket.entry.side, Side.BUY)
        self.assertEqual(e.bracket.entry.limit_price, 29506.25)
        self.assertEqual(len(e.bracket.children), 2)
        roles = {c.role for c in e.bracket.children}
        self.assertEqual(roles, {BracketRole.TAKE_PROFIT,
                                 BracketRole.STOP_LOSS})

    def test_bracket_not_re_emitted(self):
        p = _parser()
        self._place_bracket(p)
        # A further leg update must not re-emit the bracket.
        again = _h(p, "orderVersion", "Created", {
            "id": 20, "orderId": 20, "orderQty": 1, "orderType": "Stop",
            "stopPrice": 29400.0})
        self.assertEqual(again, [])

    def test_bracket_leg_modify_routed_to_correct_leg(self):
        # Modifying the take-profit leg: Tradovate re-sends the entry's
        # UNCHANGED orderVersion, then the TP's CHANGED one, then fires
        # the Replaced against the ENTRY's id. The MODIFY must be routed
        # to the TP (id 18), not the entry, and the unchanged entry must
        # NOT produce a redundant MODIFY.
        p = _parser()
        self._place_bracket(p)
        _h(p, "orderVersion", "Created", {  # entry re-sent, unchanged
            "id": 15, "orderId": 15, "orderQty": 1, "orderType": "Limit",
            "price": 29506.25})
        _h(p, "orderVersion", "Created", {  # TP actually changed
            "id": 18, "orderId": 18, "orderQty": 1, "orderType": "Limit",
            "price": 29599.75})
        out = _h(p, "executionReport", "Created", {
            "orderId": 15, "accountId": 50000001, "execType": "Replaced"})
        self.assertEqual(len(out), 1)
        self.assertIs(out[0].kind, EventKind.MODIFY)
        self.assertEqual(out[0].source_order_id, "18")
        self.assertEqual(out[0].modify.new_limit_price, 29599.75)

    def test_bracket_entry_modify_routed_to_entry(self):
        # Modifying the entry itself: the Replaced (against the entry's
        # id) must route the MODIFY to the entry, carrying its new price.
        p = _parser()
        self._place_bracket(p)
        _h(p, "orderVersion", "Created", {
            "id": 15, "orderId": 15, "orderQty": 1, "orderType": "Limit",
            "price": 29513.25})
        out = _h(p, "executionReport", "Created", {
            "orderId": 15, "accountId": 50000001, "execType": "Replaced"})
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].source_order_id, "15")
        self.assertEqual(out[0].modify.new_limit_price, 29513.25)

    def test_bracket_stop_leg_modify_routed_to_stop(self):
        p = _parser()
        self._place_bracket(p)
        _h(p, "orderVersion", "Created", {  # entry re-sent, unchanged
            "id": 15, "orderId": 15, "orderQty": 1, "orderType": "Limit",
            "price": 29506.25})
        _h(p, "orderVersion", "Created", {  # SL actually changed
            "id": 20, "orderId": 20, "orderQty": 1, "orderType": "Stop",
            "stopPrice": 29395.75})
        out = _h(p, "executionReport", "Created", {
            "orderId": 15, "accountId": 50000001, "execType": "Replaced"})
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].source_order_id, "20")
        self.assertEqual(out[0].modify.new_stop_price, 29395.75)
        self.assertIs(out[0].modify.order_type, OrderType.STOP)


class TestRealCaptureReplay(unittest.TestCase):
    """Replay the entire real capture and assert the event sequence."""

    def test_replay(self):
        data = json.loads(CAPTURE.read_text())
        p = _parser()
        events = []
        for push in data["pushes"]:
            d = push["msg"].get("d", {})
            events.extend(p.handle(
                d.get("entityType"), d.get("eventType"),
                d.get("entity", {})))
        kinds = [e.kind for e in events]
        # Exactly one NEW (the bracket), at least one CANCEL.
        news = [e for e in events if e.kind is EventKind.NEW]
        self.assertEqual(len(news), 1)
        self.assertIsNotNone(news[0].bracket)
        self.assertEqual(len(news[0].bracket.children), 2)
        self.assertIn(EventKind.CANCEL, kinds)

        # The captured session modified the entry, then the TP, then the
        # SL — exactly three MODIFYs, each routed to the correct leg with
        # the value it really changed to (no redundant entry MODIFYs).
        mods = [e for e in events if e.kind is EventKind.MODIFY]
        self.assertEqual(len(mods), 3)
        by_id = {m.source_order_id: m for m in mods}
        self.assertEqual(by_id["516124640015"].modify.new_limit_price,
                         29513.25)
        self.assertEqual(by_id["516124640018"].modify.new_limit_price,
                         29599.75)
        self.assertEqual(by_id["516124640020"].modify.new_stop_price,
                         29395.75)


if __name__ == "__main__":
    unittest.main()
