"""
Tests for ibkr_endpoint — the IBKR-parser-dataclass → broker-neutral
OrderEvent translation functions.

These pin the IBKR→neutral translation: side, order type, tif, price
gating, bracket-child role classification, and the per-kind event
shape. They're the mirror image of test_tradovate_endpoint (which
pins neutral→Tradovate).
"""

import unittest

from tradesync.brokers.ibkr_endpoint import (
    IbkrTranslationError,
    order_event_from_cancel,
    order_event_from_modify,
    order_event_from_new,
)
from tradesync.order_event import (
    BracketRole,
    EventKind,
    OrderType,
    Side,
    TimeInForce,
)
from tradesync.proxy.ibkr_parser import (
    IbkrBracket,
    IbkrBracketChild,
    IbkrOrder,
    IbkrOrderCancel,
    IbkrOrderModify,
)


def _ibkr_order(**overrides) -> IbkrOrder:
    base = dict(
        account_id="U0000001", conid=770561201, side="BUY", quantity=2,
        order_type="LMT", price=21500.0, aux_price=None, tif="DAY",
        cOID="tv-1", raw={},
    )
    base.update(overrides)
    return IbkrOrder(**base)


def _ibkr_child(**overrides) -> IbkrBracketChild:
    base = dict(
        side="SELL", quantity=2, order_type="LMT", price=21550.0,
        aux_price=None, tif="DAY", cOID=None, raw={},
    )
    base.update(overrides)
    return IbkrBracketChild(**base)


class TestNewSingleOrderTranslation(unittest.TestCase):

    def test_limit_buy(self):
        ev = order_event_from_new(_ibkr_order())
        self.assertIs(ev.kind, EventKind.NEW)
        self.assertEqual(ev.source_broker, "ibkr")
        self.assertEqual(ev.source_account_id, "U0000001")
        self.assertEqual(ev.conid, 770561201)
        self.assertEqual(ev.source_label, "tv-1")
        self.assertIsNone(ev.bracket)
        o = ev.order
        self.assertIs(o.side, Side.BUY)
        self.assertIs(o.order_type, OrderType.LIMIT)
        self.assertEqual(o.limit_price, 21500.0)
        self.assertIsNone(o.stop_price)
        self.assertIs(o.tif, TimeInForce.DAY)
        self.assertIs(o.role, BracketRole.ENTRY)

    def test_market_has_no_prices(self):
        ev = order_event_from_new(_ibkr_order(order_type="MKT", price=None))
        self.assertIs(ev.order.order_type, OrderType.MARKET)
        self.assertIsNone(ev.order.limit_price)
        self.assertIsNone(ev.order.stop_price)

    def test_stop_gates_to_stop_price(self):
        ev = order_event_from_new(_ibkr_order(
            order_type="STP", price=None, aux_price=28942.0))
        self.assertIs(ev.order.order_type, OrderType.STOP)
        self.assertEqual(ev.order.stop_price, 28942.0)
        self.assertIsNone(ev.order.limit_price)

    def test_stop_limit_carries_both(self):
        ev = order_event_from_new(_ibkr_order(
            order_type="STP LMT", price=100.0, aux_price=99.0))
        self.assertIs(ev.order.order_type, OrderType.STOP_LIMIT)
        self.assertEqual(ev.order.limit_price, 100.0)
        self.assertEqual(ev.order.stop_price, 99.0)

    def test_sell_gtc(self):
        ev = order_event_from_new(_ibkr_order(side="SELL", tif="GTC"))
        self.assertIs(ev.order.side, Side.SELL)
        self.assertIs(ev.order.tif, TimeInForce.GTC)

    def test_unknown_order_type_raises(self):
        with self.assertRaises(IbkrTranslationError):
            order_event_from_new(_ibkr_order(order_type="TRAIL"))

    def test_unmapped_tif_defaults_to_day(self):
        ev = order_event_from_new(_ibkr_order(tif="WEIRD"))
        self.assertIs(ev.order.tif, TimeInForce.DAY)


class TestNewBracketTranslation(unittest.TestCase):

    def _bracket(self):
        entry = _ibkr_order(order_type="MKT", price=None, cOID="ENTRY1")
        tp = _ibkr_child(order_type="LMT", price=29292.0)
        sl = _ibkr_child(order_type="STP", price=None, aux_price=28942.0)
        return IbkrBracket(entry=entry, children=[tp, sl])

    def test_bracket_shape(self):
        ev = order_event_from_new(self._bracket())
        self.assertIs(ev.kind, EventKind.NEW)
        self.assertIsNone(ev.order)
        self.assertIsNotNone(ev.bracket)
        self.assertEqual(ev.source_label, "ENTRY1")
        self.assertIs(ev.bracket.entry.role, BracketRole.ENTRY)
        self.assertIs(ev.bracket.entry.order_type, OrderType.MARKET)

    def test_bracket_children_roles_classified(self):
        ev = order_event_from_new(self._bracket())
        roles = [c.role for c in ev.bracket.children]
        self.assertIn(BracketRole.TAKE_PROFIT, roles)
        self.assertIn(BracketRole.STOP_LOSS, roles)
        tp = next(c for c in ev.bracket.children
                  if c.role is BracketRole.TAKE_PROFIT)
        sl = next(c for c in ev.bracket.children
                  if c.role is BracketRole.STOP_LOSS)
        self.assertIs(tp.order_type, OrderType.LIMIT)
        self.assertEqual(tp.limit_price, 29292.0)
        self.assertIs(sl.order_type, OrderType.STOP)
        self.assertEqual(sl.stop_price, 28942.0)

    def test_stop_limit_child_classified_as_stop_loss(self):
        entry = _ibkr_order(order_type="MKT", price=None)
        sl = _ibkr_child(order_type="STP LMT", price=100.0, aux_price=99.0)
        ev = order_event_from_new(IbkrBracket(entry=entry, children=[sl]))
        self.assertIs(ev.bracket.children[0].role, BracketRole.STOP_LOSS)


class TestCancelModifyTranslation(unittest.TestCase):

    def test_cancel(self):
        ev = order_event_from_cancel(
            IbkrOrderCancel(account_id="U0000001", ibkr_order_id="319073569"))
        self.assertIs(ev.kind, EventKind.CANCEL)
        self.assertEqual(ev.source_order_id, "319073569")
        self.assertEqual(ev.source_account_id, "U0000001")

    def test_modify_limit(self):
        ev = order_event_from_modify(IbkrOrderModify(
            account_id="U0000001", ibkr_order_id="319073568",
            quantity=None, price=29135.5, aux_price=None, tif=None,
            order_type="LMT", raw={}))
        self.assertIs(ev.kind, EventKind.MODIFY)
        self.assertEqual(ev.source_order_id, "319073568")
        self.assertEqual(ev.modify.new_limit_price, 29135.5)
        self.assertIs(ev.modify.order_type, OrderType.LIMIT)

    def test_modify_without_order_type_tolerated(self):
        # A qty-only modify need not carry an order type.
        ev = order_event_from_modify(IbkrOrderModify(
            account_id="U0000001", ibkr_order_id="1",
            quantity=3, price=None, aux_price=None, tif=None,
            order_type=None, raw={}))
        self.assertEqual(ev.modify.new_quantity, 3)
        self.assertIsNone(ev.modify.order_type)

    def test_modify_unknown_order_type_tolerated_as_none(self):
        ev = order_event_from_modify(IbkrOrderModify(
            account_id="U0000001", ibkr_order_id="1",
            quantity=None, price=None, aux_price=None, tif=None,
            order_type="TRAIL", raw={}))
        self.assertIsNone(ev.modify.order_type)


if __name__ == "__main__":
    unittest.main()
