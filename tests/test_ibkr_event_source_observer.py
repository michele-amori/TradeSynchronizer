"""
Tests for IbkrEventSourceObserver — the NEUTRAL source-side façade that
translates observed IBKR orders into OrderEvents and replicates them via
the EventReplicator (Step A of unifying the two replication paths).

Unlike the historical IbkrSourceObserver (pure passthrough to Replicator),
this one must:
  * translate IbkrOrder/Bracket/Cancel/Modify → OrderEvent and feed the
    EventReplicator,
  * expose the SAME addon-facing surface (emit_* + the two id-binding
    helpers) so the addon needs no changes,
  * resolve a child/single MODIFY back to the follower via the IBKR id
    bound through register_ibkr_id — the IBKR-source analogue of the
    bracket-child binding.

We use a REAL EventReplicator with a fake follower and a real (scratch)
OrderMap so the id-resolution path is exercised end to end, not mocked.
"""

import tempfile
import unittest
from pathlib import Path

from tradesync.proxy.ibkr_event_source_observer import IbkrEventSourceObserver
from tradesync.event_replicator import EventReplicator
from tradesync.order_map import OrderMap
from tradesync.brokers.endpoint import PlacedRef, PlacedBracketRef
from tradesync.proxy.ibkr_parser import (
    IbkrBracket,
    IbkrBracketChild,
    IbkrOrder,
    IbkrOrderCancel,
    IbkrOrderModify,
)


class FakeFollower:
    def __init__(self):
        self.placed, self.brackets = [], []
        self.cancelled, self.modified = [], []
        self._next = 9000

    @property
    def identity(self): return "fake_tradovate"
    def connect(self): pass
    def disconnect(self): pass

    def place_order(self, spec, *, symbol):
        self.placed.append((spec, symbol))
        oid = self._next; self._next += 1
        return PlacedRef(follower_order_id=str(oid))

    def place_bracket(self, spec, *, symbol):
        self.brackets.append((spec, symbol))
        entry = self._next; self._next += 1
        kids = []
        for _ in spec.children:
            kids.append(str(self._next)); self._next += 1
        return PlacedBracketRef(entry_order_id=str(entry),
                                child_order_ids=kids)

    def cancel_order(self, fid): self.cancelled.append(fid)
    def modify_order(self, fid, changes): self.modified.append((fid, changes))
    def order_status(self, fid): return "Working"


def _order(**ov):
    base = dict(account_id="U0000001", conid=770561201, side="BUY",
                quantity=2, order_type="LMT", price=21500.0, aux_price=None,
                tif="DAY", cOID="tv-1", raw={})
    base.update(ov)
    return IbkrOrder(**base)


def _child(**ov):
    base = dict(side="SELL", quantity=2, order_type="LMT", price=21550.0,
                aux_price=None, tif="DAY", cOID=None, raw={})
    base.update(ov)
    return IbkrBracketChild(**base)


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.order_map = OrderMap(Path(self._tmp.name) / "orders.json")
        self.follower = FakeFollower()
        # conid resolver maps the test conid → a Tradovate-ish symbol,
        # since IBKR-source events carry a conid not a symbol.
        self.er = EventReplicator(
            follower=self.follower, order_map=self.order_map,
            conid_resolver=lambda cid: "MNQM6" if cid == 770561201 else None)
        self.obs = IbkrEventSourceObserver(self.er, self.order_map)

    def tearDown(self):
        self._tmp.cleanup()


class TestEmitTranslatesAndReplicates(_Base):

    def test_new_single_places_on_follower(self):
        res = self.obs.emit_new(_order(cOID="tv-1"))
        self.assertTrue(res.success, res.reason)
        self.assertEqual(len(self.follower.placed), 1)
        spec, symbol = self.follower.placed[0]
        self.assertEqual(symbol, "MNQM6")          # resolved from conid

    def test_new_bracket_places_on_follower(self):
        br = IbkrBracket(
            entry=_order(order_type="MKT", price=None, cOID="ENTRY1"),
            children=[_child(order_type="LMT", cOID="ENTRY1-tp"),
                      _child(order_type="STP", price=None, aux_price=21000.0,
                             cOID="ENTRY1-sl")])
        res = self.obs.emit_new(br)
        self.assertTrue(res.success, res.reason)
        self.assertEqual(len(self.follower.brackets), 1)

    def test_result_has_addon_surface(self):
        # The addon's _spawn runner only reads .success/.skipped/.reason.
        res = self.obs.emit_new(_order())
        for attr in ("success", "skipped", "reason"):
            self.assertTrue(hasattr(res, attr))


class TestIdBindingAndModify(_Base):
    """The IBKR-source binding: register_ibkr_id binds the IBKR id as the
    source id, so a later modify/cancel carrying that IBKR id resolves to
    the follower order."""

    def test_modify_after_binding_reaches_follower(self):
        # 1. place a single → follower id 9000, label "tv-1"
        self.obs.emit_new(_order(cOID="tv-1"))
        # 2. addon's response hook binds the IBKR id to the label
        self.obs.register_ibkr_id("tv-1", "319073567")
        # 3. a modify referencing that IBKR id must reach the follower
        res = self.obs.emit_modify(IbkrOrderModify(
            account_id="U0000001", ibkr_order_id="319073567",
            quantity=None, price=21490.0, aux_price=None, tif=None,
            order_type="LMT", raw={}))
        self.assertTrue(res.success, res.reason)
        self.assertEqual(len(self.follower.modified), 1)
        fid, _changes = self.follower.modified[0]
        self.assertEqual(fid, "9000")

    def test_cancel_after_binding_reaches_follower(self):
        self.obs.emit_new(_order(cOID="tv-1"))
        self.obs.register_ibkr_id("tv-1", "319073567")
        res = self.obs.emit_cancel(IbkrOrderCancel(
            account_id="U0000001", ibkr_order_id="319073567"))
        self.assertTrue(res.success, res.reason)
        self.assertEqual(self.follower.cancelled, ["9000"])

    def test_coid_for_ibkr_id_resolves_bound_id(self):
        self.obs.emit_new(_order(cOID="tv-1"))
        self.obs.register_ibkr_id("tv-1", "319073567")
        self.assertEqual(self.obs.coid_for_ibkr_id("319073567"), "tv-1")

    def test_coid_for_ibkr_id_unknown_is_none(self):
        self.assertIsNone(self.obs.coid_for_ibkr_id("nope"))


class TestAddonInjection(unittest.TestCase):
    """The addon uses the injected source observer (the neutral one).
    The historical Replicator/IbkrSourceObserver fallback was removed in
    Step D, so `source` is now required and used as-is."""

    def _cfg(self):
        from unittest.mock import MagicMock
        cfg = MagicMock()
        cfg.ibkr_watched_accounts = []
        cfg.tradovate_env = "demo"
        return cfg

    def test_injected_source_is_used(self):
        from unittest.mock import MagicMock, sentinel
        from tradesync.proxy.addon import TradeSyncAddon
        addon = TradeSyncAddon(
            cfg=self._cfg(), tradovate=MagicMock(),
            resolver=MagicMock(),
            source=sentinel.neutral_observer)
        self.assertIs(addon._source, sentinel.neutral_observer)


if __name__ == "__main__":
    unittest.main()
