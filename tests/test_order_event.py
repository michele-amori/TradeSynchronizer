"""
Tests for the broker-neutral order vocabulary (tradesync.order_event).

These guard the invariants enforced at the construction boundary —
the point where a source endpoint builds an OrderEvent. Getting a
malformed event rejected here, rather than three layers deep in the
replicator, is the whole reason __post_init__ does validation.
"""

import unittest

from tradesync.order_event import (
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


class TestEnums(unittest.TestCase):

    def test_side_opposite(self):
        self.assertIs(Side.BUY.opposite, Side.SELL)
        self.assertIs(Side.SELL.opposite, Side.BUY)

    def test_enums_are_str_valued(self):
        # str-Enum so they serialise cleanly into JSON / log lines
        # and compare equal to their wire token.
        self.assertEqual(Side.BUY, "BUY")
        self.assertEqual(OrderType.STOP_LIMIT, "STOP_LIMIT")
        self.assertEqual(TimeInForce.GTC, "GTC")


class TestOrderSpecDefaults(unittest.TestCase):

    def test_minimal_market_order(self):
        spec = OrderSpec(
            side=Side.BUY, quantity=1, order_type=OrderType.MARKET,
        )
        self.assertIsNone(spec.limit_price)
        self.assertIsNone(spec.stop_price)
        self.assertIs(spec.tif, TimeInForce.DAY)
        self.assertIs(spec.role, BracketRole.ENTRY)
        self.assertIsNone(spec.source_order_id)


class TestNewEventInvariants(unittest.TestCase):

    def _order(self):
        return OrderSpec(side=Side.BUY, quantity=2,
                         order_type=OrderType.LIMIT, limit_price=100.0)

    def test_new_with_single_order_ok(self):
        ev = OrderEvent(
            kind=EventKind.NEW, source_broker="tradovate",
            source_account_id="19000001", source_order_id="111",
            symbol="MNQM6", order=self._order(),
        )
        self.assertIs(ev.kind, EventKind.NEW)
        self.assertIsNotNone(ev.order)
        self.assertIsNone(ev.bracket)

    def test_new_with_bracket_ok(self):
        entry = self._order()
        tp = OrderSpec(side=Side.SELL, quantity=2,
                       order_type=OrderType.LIMIT, limit_price=110.0,
                       role=BracketRole.TAKE_PROFIT)
        sl = OrderSpec(side=Side.SELL, quantity=2,
                       order_type=OrderType.STOP, stop_price=90.0,
                       role=BracketRole.STOP_LOSS)
        ev = OrderEvent(
            kind=EventKind.NEW, source_broker="ibkr",
            source_account_id="U0000001", source_order_id="319073567",
            conid=770561201,
            bracket=BracketSpec(entry=entry, children=[tp, sl]),
        )
        self.assertIsNotNone(ev.bracket)
        self.assertEqual(len(ev.bracket.children), 2)

    def test_new_without_order_or_bracket_raises(self):
        with self.assertRaises(ValueError):
            OrderEvent(
                kind=EventKind.NEW, source_broker="tradovate",
                source_account_id="19000001", source_order_id="111",
            )

    def test_new_with_both_order_and_bracket_raises(self):
        entry = self._order()
        with self.assertRaises(ValueError):
            OrderEvent(
                kind=EventKind.NEW, source_broker="ibkr",
                source_account_id="U0000001", source_order_id="1",
                order=self._order(),
                bracket=BracketSpec(entry=entry, children=[]),
            )


class TestModifyEventInvariants(unittest.TestCase):

    def test_modify_ok(self):
        ev = OrderEvent(
            kind=EventKind.MODIFY, source_broker="tradovate",
            source_account_id="19000001", source_order_id="111",
            modify=ModifySpec(new_limit_price=105.0,
                              order_type=OrderType.LIMIT),
        )
        self.assertEqual(ev.modify.new_limit_price, 105.0)

    def test_modify_without_modifyspec_raises(self):
        with self.assertRaises(ValueError):
            OrderEvent(
                kind=EventKind.MODIFY, source_broker="tradovate",
                source_account_id="19000001", source_order_id="111",
            )

    def test_modify_without_source_order_id_raises(self):
        with self.assertRaises(ValueError):
            OrderEvent(
                kind=EventKind.MODIFY, source_broker="tradovate",
                source_account_id="19000001",
                modify=ModifySpec(new_quantity=3),
            )


class TestCancelFillInvariants(unittest.TestCase):

    def test_cancel_ok(self):
        ev = OrderEvent(
            kind=EventKind.CANCEL, source_broker="ibkr",
            source_account_id="U0000001", source_order_id="319073569",
        )
        self.assertIs(ev.kind, EventKind.CANCEL)

    def test_cancel_without_source_order_id_raises(self):
        with self.assertRaises(ValueError):
            OrderEvent(
                kind=EventKind.CANCEL, source_broker="ibkr",
                source_account_id="U0000001",
            )

    def test_fill_without_source_order_id_raises(self):
        with self.assertRaises(ValueError):
            OrderEvent(
                kind=EventKind.FILL, source_broker="tradovate",
                source_account_id="19000001",
            )


if __name__ == "__main__":
    unittest.main()
