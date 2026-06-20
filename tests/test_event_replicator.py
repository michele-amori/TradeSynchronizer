"""
Tests for EventReplicator — the broker-neutral replication engine.

Drive it with a fake FollowerEndpoint (records calls, returns canned
PlacedRef/PlacedBracketRef) and a real on-disk OrderMap, asserting:
NEW single + bracket placement and map registration (incl. synthetic
child labels), CANCEL/MODIFY resolution through the map, FILL skip,
account filter, follower-error containment (never raises), and the
conid-only event surfacing a clean failure.
"""

import tempfile
import unittest
from pathlib import Path

from tradesync.brokers.endpoint import PlacedBracketRef, PlacedRef
from tradesync.event_replicator import EventReplicator, scale_quantity
from tradesync.order_event import (
    BracketRole,
    BracketSpec,
    EventKind,
    ModifySpec,
    OrderEvent,
    OrderSpec,
    OrderType,
    Side,
)
from tradesync.order_map import OrderMap


class FakeFollower:
    def __init__(self, native_oco=False):
        self.placed = []
        self.brackets = []
        self.cancelled = []
        self.modified = []
        self.fail_place = False
        self.fail_cancel = False
        self._native_oco = native_oco
        self._next = 5000
        # Per-id status overrides for reconciliation tests. id -> status
        # string, or id -> Exception instance to simulate a query error.
        self.status_by_id = {}
        self.status_calls = []

    @property
    def identity(self):
        return "fake_follower"

    @property
    def native_oco(self):
        return self._native_oco

    def connect(self):
        pass

    def disconnect(self):
        pass

    def place_order(self, spec, *, symbol):
        if self.fail_place:
            raise RuntimeError("boom")
        self.placed.append((spec, symbol))
        oid = self._next
        self._next += 1
        return PlacedRef(follower_order_id=str(oid))

    def place_bracket(self, spec, *, symbol):
        self.brackets.append((spec, symbol))
        entry = self._next
        self._next += 1
        children = []
        for _ in spec.children:
            children.append(str(self._next))
            self._next += 1
        return PlacedBracketRef(entry_order_id=str(entry),
                                child_order_ids=children)

    def cancel_order(self, follower_order_id):
        if self.fail_cancel:
            raise RuntimeError("cancel boom")
        self.cancelled.append(follower_order_id)

    def modify_order(self, follower_order_id, changes):
        self.modified.append((follower_order_id, changes))

    def order_status(self, follower_order_id):
        self.status_calls.append(follower_order_id)
        val = self.status_by_id.get(follower_order_id, "Working")
        if isinstance(val, Exception):
            raise val
        return val


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.order_map = OrderMap(Path(self._tmp.name) / "orders.json")
        self.follower = FakeFollower()
        self.er = EventReplicator(
            follower=self.follower, order_map=self.order_map)

    def tearDown(self):
        self._tmp.cleanup()


class TestNewSingle(_Base):

    def test_places_and_registers(self):
        ev = OrderEvent(
            kind=EventKind.NEW, source_broker="tradovate",
            source_account_id="50000001", source_order_id="111",
            source_label="111", symbol="MNQM6",
            order=OrderSpec(side=Side.BUY, quantity=2,
                            order_type=OrderType.MARKET))
        res = self.er.apply(ev)
        self.assertTrue(res.success)
        self.assertEqual(len(self.follower.placed), 1)
        # follower id registered + source id bound for later cancel
        self.assertEqual(
            self.order_map.follower_id_for_source_id("111"), "5000")

    def test_follower_error_contained(self):
        self.follower.fail_place = True
        ev = OrderEvent(
            kind=EventKind.NEW, source_broker="tradovate",
            source_account_id="50000001", source_order_id="111",
            symbol="MNQM6",
            order=OrderSpec(side=Side.BUY, quantity=1,
                            order_type=OrderType.MARKET))
        res = self.er.apply(ev)   # must not raise
        self.assertFalse(res.success)
        self.assertFalse(res.skipped)
        self.assertIn("place_order failed", res.reason)

    def test_conid_only_event_fails_cleanly(self):
        # IBKR-source event carrying only a conid, no symbol → not yet
        # supported; must fail clearly rather than crash.
        ev = OrderEvent(
            kind=EventKind.NEW, source_broker="ibkr",
            source_account_id="U0000001", source_order_id="1",
            conid=770561201,
            order=OrderSpec(side=Side.BUY, quantity=1,
                            order_type=OrderType.MARKET))
        res = self.er.apply(ev)
        self.assertFalse(res.success)
        self.assertIn("conid", res.reason)


class TestNewBracket(_Base):

    def _bracket_event(self):
        entry = OrderSpec(side=Side.BUY, quantity=2,
                          order_type=OrderType.MARKET, role=BracketRole.ENTRY)
        tp = OrderSpec(side=Side.SELL, quantity=2, order_type=OrderType.LIMIT,
                       limit_price=29292.0, role=BracketRole.TAKE_PROFIT)
        sl = OrderSpec(side=Side.SELL, quantity=2, order_type=OrderType.STOP,
                       stop_price=28942.0, role=BracketRole.STOP_LOSS)
        return OrderEvent(
            kind=EventKind.NEW, source_broker="tradovate",
            source_account_id="50000001", source_order_id="200",
            source_label="200", symbol="MNQM6",
            bracket=BracketSpec(entry=entry, children=[tp, sl]))

    def test_bracket_registers_entry_and_synthetic_children(self):
        res = self.er.apply(self._bracket_event())
        self.assertTrue(res.success)
        self.assertEqual(len(self.follower.brackets), 1)
        # entry under its label, children under synthetic #LMT / #STP
        self.assertEqual(self.order_map.get_by_coid("200").follower_order_id,
                         "5000")
        self.assertEqual(
            self.order_map.get_by_coid("200#LMT").follower_order_id, "5001")
        self.assertEqual(
            self.order_map.get_by_coid("200#STP").follower_order_id, "5002")


class TestOcoCascade(unittest.TestCase):
    """When a bracket exit leg is cancelled and the follower does NOT
    enforce OCO natively (e.g. Tradovate), the replicator must cancel the
    sibling exit leg too. When the follower DOES enforce OCO natively
    (e.g. IBKR), it must NOT — the broker already handles it, and a
    second cancel would be redundant/erroneous."""

    def _setup(self, native_oco):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.order_map = OrderMap(Path(self._tmp.name) / "orders.json")
        self.follower = FakeFollower(native_oco=native_oco)
        self.er = EventReplicator(
            follower=self.follower, order_map=self.order_map)
        # Place a bracket whose children carry their OWN source ids, as
        # the live parser emits (entry s-200, tp s-tp, sl s-sl).
        entry = OrderSpec(side=Side.BUY, quantity=1,
                          order_type=OrderType.LIMIT, limit_price=100.0,
                          source_order_id="s-200", source_label="200",
                          role=BracketRole.ENTRY)
        tp = OrderSpec(side=Side.SELL, quantity=1, order_type=OrderType.LIMIT,
                       limit_price=120.0, source_order_id="s-tp",
                       source_label="200#LMT", role=BracketRole.TAKE_PROFIT)
        sl = OrderSpec(side=Side.SELL, quantity=1, order_type=OrderType.STOP,
                       stop_price=90.0, source_order_id="s-sl",
                       source_label="200#STP", role=BracketRole.STOP_LOSS)
        self.er.apply(OrderEvent(
            kind=EventKind.NEW, source_broker="tradovate",
            source_account_id="50000001", source_order_id="s-200",
            source_label="200", symbol="MNQM6",
            bracket=BracketSpec(entry=entry, children=[tp, sl])))
        # entry→5000, tp(200#LMT)→5001, sl(200#STP)→5002

    def test_cancel_tp_cascades_to_sl_when_no_native_oco(self):
        self._setup(native_oco=False)
        res = self.er.apply(OrderEvent(
            kind=EventKind.CANCEL, source_broker="tradovate",
            source_account_id="50000001", source_order_id="s-tp"))
        self.assertTrue(res.success, res.reason)
        # both the TP (primary) and the SL (sibling) were cancelled
        self.assertIn("5001", self.follower.cancelled)
        self.assertIn("5002", self.follower.cancelled)
        self.assertIn("OCO sibling", res.reason)

    def test_cancel_sl_cascades_to_tp_when_no_native_oco(self):
        self._setup(native_oco=False)
        res = self.er.apply(OrderEvent(
            kind=EventKind.CANCEL, source_broker="tradovate",
            source_account_id="50000001", source_order_id="s-sl"))
        self.assertTrue(res.success, res.reason)
        self.assertIn("5002", self.follower.cancelled)  # SL primary
        self.assertIn("5001", self.follower.cancelled)  # TP sibling

    def test_cancel_leg_does_NOT_cascade_when_native_oco(self):
        # IBKR follower: the broker auto-cancels the sibling, so the
        # replicator must cancel ONLY the primary leg.
        self._setup(native_oco=True)
        res = self.er.apply(OrderEvent(
            kind=EventKind.CANCEL, source_broker="tradovate",
            source_account_id="50000001", source_order_id="s-tp"))
        self.assertTrue(res.success, res.reason)
        self.assertEqual(self.follower.cancelled, ["5001"])  # ONLY the TP
        self.assertNotIn("OCO sibling", res.reason)

    def test_cancel_single_order_does_not_cascade(self):
        # A non-bracket order (no #LMT/#STP label) must never cascade.
        self._setup(native_oco=False)
        self.er.apply(OrderEvent(
            kind=EventKind.NEW, source_broker="tradovate",
            source_account_id="50000001", source_order_id="solo",
            source_label="solo", symbol="MNQM6",
            order=OrderSpec(side=Side.BUY, quantity=1,
                            order_type=OrderType.LIMIT, limit_price=50.0)))
        before = list(self.follower.cancelled)
        res = self.er.apply(OrderEvent(
            kind=EventKind.CANCEL, source_broker="tradovate",
            source_account_id="50000001", source_order_id="solo"))
        self.assertTrue(res.success, res.reason)
        # exactly one new cancel (the solo order), no sibling
        self.assertEqual(len(self.follower.cancelled), len(before) + 1)
        self.assertNotIn("OCO sibling", res.reason)

    def test_fill_of_exit_leg_cascades_to_sibling_when_no_native_oco(self):
        # The take-profit FILLS → position closed → stop-loss sibling
        # must be cancelled (Tradovate doesn't do it natively).
        self._setup(native_oco=False)
        res = self.er.apply(OrderEvent(
            kind=EventKind.FILL, source_broker="tradovate",
            source_account_id="50000001", source_order_id="s-tp"))
        self.assertTrue(res.success, res.reason)
        self.assertEqual(self.follower.cancelled, ["5002"])  # SL sibling
        self.assertIn("cascaded OCO", res.reason)

    def test_fill_of_exit_leg_does_NOT_cascade_when_native_oco(self):
        # IBKR cancels the sibling itself on a fill → replicator stays out.
        self._setup(native_oco=True)
        res = self.er.apply(OrderEvent(
            kind=EventKind.FILL, source_broker="tradovate",
            source_account_id="50000001", source_order_id="s-tp"))
        self.assertTrue(res.skipped, res.reason)
        self.assertEqual(self.follower.cancelled, [])

    def test_fill_of_ENTRY_does_not_cascade(self):
        # CRITICAL: the entry filling OPENS the position; the exits must
        # stay live. An entry fill must cancel NOTHING.
        self._setup(native_oco=False)
        res = self.er.apply(OrderEvent(
            kind=EventKind.FILL, source_broker="tradovate",
            source_account_id="50000001", source_order_id="s-200"))
        self.assertTrue(res.skipped, res.reason)
        self.assertEqual(self.follower.cancelled, [])  # exits untouched

    def test_cascade_failure_does_not_fail_primary(self):
        # If the sibling cancel raises, the primary cancel still counts
        # as success — the source side already shows both legs gone.
        self._setup(native_oco=False)
        # Make the SECOND cancel (the sibling) blow up: fail_cancel makes
        # ALL cancels raise, so we instead remove the sibling's follower
        # id resolution by failing only via a flag after the primary.
        # Simpler: set fail_cancel and assert primary still reports ok is
        # NOT valid (primary would raise). So we target the sibling by
        # making cancel raise only for 5002.
        orig = self.follower.cancel_order
        def selective(fid):
            if fid == "5002":
                raise RuntimeError("sibling boom")
            return orig(fid)
        self.follower.cancel_order = selective
        res = self.er.apply(OrderEvent(
            kind=EventKind.CANCEL, source_broker="tradovate",
            source_account_id="50000001", source_order_id="s-tp"))
        self.assertTrue(res.success, res.reason)      # primary still ok
        self.assertIn("5001", self.follower.cancelled)  # TP went through
        self.assertNotIn("OCO sibling", res.reason)     # sibling not noted


class TestCancel(_Base):

    def test_cancel_known_order(self):
        # First place so the map knows it.
        self.order_map.set_follower_id("111", "5000")
        self.order_map.bind_source_id("111", "src-111")
        ev = OrderEvent(kind=EventKind.CANCEL, source_broker="tradovate",
                        source_account_id="50000001", source_order_id="src-111")
        res = self.er.apply(ev)
        self.assertTrue(res.success)
        self.assertEqual(self.follower.cancelled, ["5000"])

    def test_cancel_unknown_is_skip(self):
        ev = OrderEvent(kind=EventKind.CANCEL, source_broker="tradovate",
                        source_account_id="50000001", source_order_id="nope")
        res = self.er.apply(ev)
        self.assertFalse(res.success)
        self.assertTrue(res.skipped)


class TestModify(_Base):

    def test_modify_known_order(self):
        self.order_map.set_follower_id("111", "5000")
        self.order_map.bind_source_id("111", "src-111")
        ev = OrderEvent(kind=EventKind.MODIFY, source_broker="tradovate",
                        source_account_id="50000001", source_order_id="src-111",
                        modify=ModifySpec(new_limit_price=105.0,
                                          order_type=OrderType.LIMIT))
        res = self.er.apply(ev)
        self.assertTrue(res.success)
        fid, changes = self.follower.modified[0]
        self.assertEqual(fid, "5000")
        self.assertEqual(changes.new_limit_price, 105.0)

    def test_modify_unknown_is_skip(self):
        ev = OrderEvent(kind=EventKind.MODIFY, source_broker="tradovate",
                        source_account_id="50000001", source_order_id="nope",
                        modify=ModifySpec(new_quantity=3))
        res = self.er.apply(ev)
        self.assertTrue(res.skipped)

    def test_modify_bracket_child_leg_reaches_follower(self):
        # Regression: a MODIFY targeting a bracket CHILD leg (e.g. moving
        # the stop-loss) must resolve to the follower order. Previously
        # _apply_new_bracket bound only the ENTRY's source id, so a child
        # leg modify resolved to None and was silently skipped — the leg
        # change never reached the follower. The child legs here carry
        # their OWN source_order_id, exactly as the live parser emits.
        entry = OrderSpec(side=Side.BUY, quantity=1,
                          order_type=OrderType.LIMIT, limit_price=100.0,
                          source_order_id="entry-1", source_label="entry-1",
                          role=BracketRole.ENTRY)
        tp = OrderSpec(side=Side.SELL, quantity=1, order_type=OrderType.LIMIT,
                       limit_price=120.0, source_order_id="tp-1",
                       source_label="tp-1", role=BracketRole.TAKE_PROFIT)
        sl = OrderSpec(side=Side.SELL, quantity=1, order_type=OrderType.STOP,
                       stop_price=90.0, source_order_id="sl-1",
                       source_label="sl-1", role=BracketRole.STOP_LOSS)
        place = OrderEvent(
            kind=EventKind.NEW, source_broker="tradovate",
            source_account_id="50000001", source_order_id="entry-1",
            source_label="entry-1", symbol="MNQM6",
            bracket=BracketSpec(entry=entry, children=[tp, sl]))
        self.assertTrue(self.er.apply(place).success)

        modify_sl = OrderEvent(
            kind=EventKind.MODIFY, source_broker="tradovate",
            source_account_id="50000001", source_order_id="sl-1",
            modify=ModifySpec(new_stop_price=85.0,
                              order_type=OrderType.STOP))
        res = self.er.apply(modify_sl)
        self.assertTrue(res.success, res.reason)
        self.assertFalse(res.skipped, res.reason)
        fid, changes = self.follower.modified[-1]
        self.assertEqual(fid, "5002")
        self.assertEqual(changes.new_stop_price, 85.0)

    def test_modify_bracket_child_tp_leg_reaches_follower(self):
        entry = OrderSpec(side=Side.BUY, quantity=1,
                          order_type=OrderType.LIMIT, limit_price=100.0,
                          source_order_id="e2", source_label="e2",
                          role=BracketRole.ENTRY)
        tp = OrderSpec(side=Side.SELL, quantity=1, order_type=OrderType.LIMIT,
                       limit_price=120.0, source_order_id="tp2",
                       source_label="tp2", role=BracketRole.TAKE_PROFIT)
        sl = OrderSpec(side=Side.SELL, quantity=1, order_type=OrderType.STOP,
                       stop_price=90.0, source_order_id="sl2",
                       source_label="sl2", role=BracketRole.STOP_LOSS)
        self.er.apply(OrderEvent(
            kind=EventKind.NEW, source_broker="tradovate",
            source_account_id="50000001", source_order_id="e2",
            source_label="e2", symbol="MNQM6",
            bracket=BracketSpec(entry=entry, children=[tp, sl])))
        res = self.er.apply(OrderEvent(
            kind=EventKind.MODIFY, source_broker="tradovate",
            source_account_id="50000001", source_order_id="tp2",
            modify=ModifySpec(new_limit_price=125.0,
                              order_type=OrderType.LIMIT)))
        self.assertTrue(res.success, res.reason)
        fid, changes = self.follower.modified[-1]
        self.assertEqual(fid, "5001")
        self.assertEqual(changes.new_limit_price, 125.0)


class TestFillAndFilter(_Base):

    def test_fill_is_informational_skip(self):
        ev = OrderEvent(kind=EventKind.FILL, source_broker="tradovate",
                        source_account_id="50000001", source_order_id="111")
        res = self.er.apply(ev)
        self.assertTrue(res.skipped)
        self.assertEqual(self.follower.placed, [])

    def test_account_filter_skips_unwatched(self):
        er = EventReplicator(follower=self.follower, order_map=self.order_map,
                             watched_source_accounts=["50000001"])
        ev = OrderEvent(kind=EventKind.NEW, source_broker="tradovate",
                        source_account_id="99999999", source_order_id="1",
                        symbol="MNQM6",
                        order=OrderSpec(side=Side.BUY, quantity=1,
                                        order_type=OrderType.MARKET))
        res = er.apply(ev)
        self.assertTrue(res.skipped)
        self.assertEqual(self.follower.placed, [])

    def test_account_filter_allows_watched(self):
        er = EventReplicator(follower=self.follower, order_map=self.order_map,
                             watched_source_accounts=["50000001"])
        ev = OrderEvent(kind=EventKind.NEW, source_broker="tradovate",
                        source_account_id="50000001", source_order_id="1",
                        symbol="MNQM6",
                        order=OrderSpec(side=Side.BUY, quantity=1,
                                        order_type=OrderType.MARKET))
        res = er.apply(ev)
        self.assertTrue(res.success)


class TestScaleQuantity(unittest.TestCase):
    """The pure size-scaling rule. Touches real order sizes, so every
    edge is pinned."""

    def test_exact_mirror(self):
        self.assertEqual(scale_quantity(90, 1.0), 90)
        self.assertEqual(scale_quantity(1, 1.0), 1)

    def test_users_example(self):
        # 90 × 0.33 = 29.7 → 30
        self.assertEqual(scale_quantity(90, 0.33), 30)

    def test_round_half_up_not_bankers(self):
        # Python's round(2.5) is 2 (banker's); we want 3.
        self.assertEqual(scale_quantity(5, 0.5), 3)     # 2.5 → 3
        self.assertEqual(scale_quantity(3, 0.5), 2)     # 1.5 → 2
        self.assertEqual(scale_quantity(7, 0.5), 4)     # 3.5 → 4

    def test_floor_to_one_never_zero(self):
        # 1 × 0.33 = 0.33 → would round to 0 → forced to 1
        self.assertEqual(scale_quantity(1, 0.33), 1)
        self.assertEqual(scale_quantity(2, 0.1), 1)     # 0.2 → 0 → 1
        self.assertEqual(scale_quantity(1, 0.01), 1)

    def test_scaling_up(self):
        self.assertEqual(scale_quantity(10, 2.5), 25)
        self.assertEqual(scale_quantity(4, 100.0), 400)

    def test_non_positive_master_unchanged(self):
        self.assertEqual(scale_quantity(0, 0.5), 0)


class TestRatioScalingEndToEnd(_Base):
    """The ratio threaded through EventReplicator onto real follower
    calls — single, bracket (entry + legs), and size-modify."""

    def _er(self, ratio):
        return EventReplicator(follower=self.follower,
                               order_map=self.order_map, ratio=ratio)

    def test_single_order_scaled(self):
        er = self._er(0.5)
        er.apply(OrderEvent(
            kind=EventKind.NEW, source_broker="tradovate",
            source_account_id="50000001", source_order_id="1",
            source_label="1", symbol="MNQM6",
            order=OrderSpec(side=Side.BUY, quantity=90,
                            order_type=OrderType.MARKET)))
        spec, _symbol = self.follower.placed[-1]
        self.assertEqual(spec.quantity, 45)

    def test_single_order_floored_to_one(self):
        er = self._er(0.33)
        er.apply(OrderEvent(
            kind=EventKind.NEW, source_broker="tradovate",
            source_account_id="50000001", source_order_id="1",
            source_label="1", symbol="MNQM6",
            order=OrderSpec(side=Side.BUY, quantity=1,
                            order_type=OrderType.MARKET)))
        spec, _symbol = self.follower.placed[-1]
        self.assertEqual(spec.quantity, 1)

    def test_bracket_entry_and_legs_scaled(self):
        er = self._er(0.5)
        entry = OrderSpec(side=Side.BUY, quantity=10,
                          order_type=OrderType.LIMIT, limit_price=100.0,
                          source_label="e", role=BracketRole.ENTRY)
        tp = OrderSpec(side=Side.SELL, quantity=10, order_type=OrderType.LIMIT,
                       limit_price=120.0, source_label="e#LMT",
                       role=BracketRole.TAKE_PROFIT)
        sl = OrderSpec(side=Side.SELL, quantity=10, order_type=OrderType.STOP,
                       stop_price=90.0, source_label="e#STP",
                       role=BracketRole.STOP_LOSS)
        er.apply(OrderEvent(
            kind=EventKind.NEW, source_broker="tradovate",
            source_account_id="50000001", source_order_id="e",
            source_label="e", symbol="MNQM6",
            bracket=BracketSpec(entry=entry, children=[tp, sl])))
        placed_bracket, _symbol = self.follower.brackets[-1]
        self.assertEqual(placed_bracket.entry.quantity, 5)
        self.assertEqual([c.quantity for c in placed_bracket.children], [5, 5])

    def test_source_spec_not_mutated(self):
        # Scaling must COPY, never mutate the source event's spec.
        er = self._er(0.5)
        original = OrderSpec(side=Side.BUY, quantity=90,
                             order_type=OrderType.MARKET, source_label="1")
        ev = OrderEvent(
            kind=EventKind.NEW, source_broker="tradovate",
            source_account_id="50000001", source_order_id="1",
            source_label="1", symbol="MNQM6", order=original)
        er.apply(ev)
        self.assertEqual(original.quantity, 90)        # untouched

    def test_modify_quantity_scaled(self):
        er = self._er(0.33)
        # place first so the modify resolves to a follower id
        er.apply(OrderEvent(
            kind=EventKind.NEW, source_broker="tradovate",
            source_account_id="50000001", source_order_id="1",
            source_label="1", symbol="MNQM6",
            order=OrderSpec(side=Side.BUY, quantity=90,
                            order_type=OrderType.MARKET)))
        er.apply(OrderEvent(
            kind=EventKind.MODIFY, source_broker="tradovate",
            source_account_id="50000001", source_order_id="1",
            modify=ModifySpec(new_quantity=60)))
        _fid, changes = self.follower.modified[-1]
        self.assertEqual(changes.new_quantity, 20)     # 60 × 0.33 = 19.8 → 20

    def test_price_only_modify_not_affected_by_ratio(self):
        er = self._er(0.5)
        er.apply(OrderEvent(
            kind=EventKind.NEW, source_broker="tradovate",
            source_account_id="50000001", source_order_id="1",
            source_label="1", symbol="MNQM6",
            order=OrderSpec(side=Side.BUY, quantity=10,
                            order_type=OrderType.LIMIT, limit_price=100.0)))
        er.apply(OrderEvent(
            kind=EventKind.MODIFY, source_broker="tradovate",
            source_account_id="50000001", source_order_id="1",
            modify=ModifySpec(new_limit_price=105.0,
                              order_type=OrderType.LIMIT)))
        _fid, changes = self.follower.modified[-1]
        self.assertEqual(changes.new_limit_price, 105.0)
        self.assertIsNone(changes.new_quantity)


class TestReconcileWithFollower(_Base):
    """Startup OrderMap reconciliation: prune entries whose follower
    order went terminal while the engine was down. Conservative — prune
    ONLY on a recognised terminal status; active/unknown/error keeps the
    entry."""

    def _map_entry(self, label, follower_id):
        self.order_map.add_pending(label)
        if follower_id is not None:
            self.order_map.set_follower_id(label, follower_id)

    def test_empty_map_is_noop(self):
        stats = self.er.reconcile_with_follower()
        self.assertEqual(stats["checked"], 0)
        self.assertEqual(self.follower.status_calls, [])

    def test_prunes_terminal_keeps_active(self):
        self._map_entry("A", "5001")   # will be Filled → pruned
        self._map_entry("B", "5002")   # Working → kept
        self.follower.status_by_id = {"5001": "Filled", "5002": "Working"}
        stats = self.er.reconcile_with_follower()
        self.assertEqual(stats["pruned"], 1)
        self.assertEqual(stats["kept"], 1)
        self.assertIsNone(self.order_map.record_for_source_label("A"))
        self.assertIsNotNone(self.order_map.record_for_source_label("B"))

    def test_terminal_statuses_both_vocabularies(self):
        # Tradovate + IBKR terminal spellings, case-insensitive.
        for i, status in enumerate(
                ["Filled", "Cancelled", "Canceled", "Rejected",
                 "Expired", "ApiCancelled", "Inactive", "FILLED"]):
            self._map_entry(f"L{i}", str(6000 + i))
            self.follower.status_by_id[str(6000 + i)] = status
        stats = self.er.reconcile_with_follower()
        self.assertEqual(stats["pruned"], 8)
        self.assertEqual(stats["kept"], 0)

    def test_unknown_status_is_kept_not_pruned(self):
        # An unfamiliar status must NEVER prune (could be a valid order).
        self._map_entry("A", "5001")
        self.follower.status_by_id = {"5001": "SomeNewStatusWeDontKnow"}
        stats = self.er.reconcile_with_follower()
        self.assertEqual(stats["pruned"], 0)
        self.assertEqual(stats["kept"], 1)
        self.assertIsNotNone(self.order_map.record_for_source_label("A"))

    def test_query_error_is_kept_not_pruned(self):
        # A transient query error must not wipe a valid mapping.
        self._map_entry("A", "5001")
        self.follower.status_by_id = {"5001": RuntimeError("503")}
        stats = self.er.reconcile_with_follower()
        self.assertEqual(stats["errors"], 1)
        self.assertEqual(stats["pruned"], 0)
        self.assertIsNotNone(self.order_map.record_for_source_label("A"))

    def test_entry_without_follower_id_is_skipped(self):
        # Placement never completed — leave it, don't query.
        self._map_entry("A", None)
        stats = self.er.reconcile_with_follower()
        self.assertEqual(stats["skipped_no_follower_id"], 1)
        self.assertEqual(self.follower.status_calls, [])
        self.assertIsNotNone(self.order_map.record_for_source_label("A"))


if __name__ == "__main__":
    unittest.main()
