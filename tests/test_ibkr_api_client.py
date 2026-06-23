"""
Tests for IbkrApiClient — the synchronous ibapi wrapper (IBKR follower).

ibapi's EClient methods talk to a socket, so these tests subclass the
client to stub the network-facing EClient methods (placeOrder /
reqContractDetails / cancelOrder / isConnected) and drive the EWrapper
callbacks directly. That exercises all the bridging logic — order-id
allocation, bracket parentId/OCA wiring, status tracking, contract
resolution + caching, not-connected guards — with no Gateway.

A separate live test (guarded by TRADESYNC_IBKR_LIVE=1) connects to a
real paper Gateway; it's skipped by default so the suite runs anywhere.
"""

import os
import threading
import unittest
from unittest.mock import MagicMock

from ibapi.contract import Contract
from ibapi.order import Order

from tradesync.brokers.ibkr_api_client import (
    IbkrApiClient,
    IbkrApiError,
    IbkrNotConnected,
    IbkrOrderNotFound,
    _MONTH_CODE_TO_NUM,
)


class _FakeIbkrClient(IbkrApiClient):
    """IbkrApiClient with the network-facing EClient methods stubbed so
    we can drive it without a socket."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.placed = []          # (orderId, contract, order)
        self.cancelled = []       # orderId
        self.contract_requests = []  # (reqId, contract)
        self._fake_connected = True
        # Auto-answer reqContractDetails with this conId unless a test
        # overrides _auto_resolve.
        self._auto_resolve = True
        self._auto_con_id = 770561201

    # — stub EClient network methods —
    def isConnected(self):
        return self._fake_connected

    def placeOrder(self, orderId, contract, order):
        self.placed.append((orderId, contract, order))

    def cancelOrder(self, orderId, *args):
        self.cancelled.append(orderId)

    def reqContractDetails(self, reqId, contract):
        self.contract_requests.append((reqId, contract))
        if self._auto_resolve:
            # Simulate IBKR answering on the reader thread: fire a
            # contractDetails then contractDetailsEnd.
            resolved = Contract()
            resolved.symbol = contract.symbol
            resolved.secType = "FUT"
            resolved.exchange = "CME"
            resolved.conId = self._auto_con_id
            resolved.lastTradeDateOrContractMonth = (
                contract.lastTradeDateOrContractMonth)

            class _Details:
                pass
            d = _Details()
            d.contract = resolved
            self._on_contract_details(reqId, d)
            self._on_contract_details_end(reqId)

    def serverVersion(self):
        return 157

    # — stub positions: a test sets self._fake_positions to a list of
    #   (account, conId, qty) or (account, conId, qty, secType);
    #   reqPositions replays them then ends. Default secType is FUT. —
    def reqPositions(self):
        for row in getattr(self, "_fake_positions", []):
            if len(row) == 4:
                acct, con_id, qty, sec = row
            else:
                acct, con_id, qty = row
                sec = "FUT"
            c = Contract()
            c.conId = con_id
            c.secType = sec
            self._on_position(acct, c, qty)
        self._on_position_end()

    def cancelPositions(self):
        pass


def _connected_client(**kw):
    c = _FakeIbkrClient(**kw)
    # Simulate the handshake: nextValidId arrives.
    c._on_next_valid_id(1000)
    return c


class TestOrderIdAllocation(unittest.TestCase):

    def test_alloc_is_monotonic_from_next_valid_id(self):
        c = _connected_client()
        self.assertEqual(c._alloc_order_id(), 1000)
        self.assertEqual(c._alloc_order_id(), 1001)
        self.assertEqual(c._alloc_order_id(), 1002)

    def test_alloc_before_connect_raises(self):
        c = _FakeIbkrClient()
        with self.assertRaises(IbkrNotConnected):
            c._alloc_order_id()


class TestErrorRouting(unittest.TestCase):

    def test_benign_codes_are_ignored(self):
        c = _connected_client()
        # Should not raise or set anything.
        for code in (2104, 2106, 2158):
            c._on_error(-1, code, "farm OK")

    def test_real_error_routes_to_pending_resolve(self):
        c = _connected_client()
        from tradesync.brokers.ibkr_api_client import _PendingResolve
        pend = _PendingResolve()
        c._pending_resolves[42] = pend
        c._on_error(42, 200, "No security definition found")
        self.assertTrue(pend.event.is_set())
        self.assertIn("200", pend.error)


class TestOrderRejection(unittest.TestCase):
    """Async order rejections route to the injected on_order_rejected."""

    def test_known_reject_code_invokes_callback(self):
        c = _connected_client()
        cb = MagicMock()
        c.on_order_rejected = cb
        c._on_error(777, 201, "Order rejected - reason: size")
        cb.assert_called_once_with(777, 201, "Order rejected - reason: size")

    def test_error_on_tracked_order_id_invokes_callback(self):
        c = _connected_client()
        c._order_status[888] = "Submitted"
        cb = MagicMock()
        c.on_order_rejected = cb
        c._on_error(888, 12345, "some order problem")
        cb.assert_called_once()

    def test_pending_resolve_error_is_not_a_rejection(self):
        c = _connected_client()
        from tradesync.brokers.ibkr_api_client import _PendingResolve
        c._pending_resolves[42] = _PendingResolve()
        cb = MagicMock()
        c.on_order_rejected = cb
        c._on_error(42, 201, "resolve error, not an order reject")
        cb.assert_not_called()

    def test_no_callback_set_does_not_raise(self):
        c = _connected_client()
        c.on_order_rejected = None
        c._on_error(777, 201, "rejected")

    def test_callback_error_is_swallowed(self):
        c = _connected_client()
        c.on_order_rejected = MagicMock(side_effect=RuntimeError("boom"))
        c._on_error(777, 201, "rejected")


class TestPlaceOrder(unittest.TestCase):

    def _order(self):
        o = Order()
        o.action = "BUY"
        o.orderType = "MKT"
        o.totalQuantity = 2
        return o

    def test_place_order_allocates_id_and_transmits(self):
        c = _connected_client()
        contract = Contract()
        oid = c.place_order(contract=contract, order=self._order())
        self.assertEqual(oid, 1000)
        self.assertEqual(len(c.placed), 1)
        placed_id, _, _ = c.placed[0]
        self.assertEqual(placed_id, 1000)

    def test_place_order_disconnected_raises(self):
        c = _connected_client()
        c._fake_connected = False
        with self.assertRaises(IbkrNotConnected):
            c.place_order(contract=Contract(), order=self._order())


class TestPlaceBracket(unittest.TestCase):

    def _o(self, action, otype, qty, **kw):
        o = Order()
        o.action = action
        o.orderType = otype
        o.totalQuantity = qty
        for k, v in kw.items():
            setattr(o, k, v)
        return o

    def test_bracket_wiring(self):
        c = _connected_client()
        contract = Contract()
        entry = self._o("BUY", "MKT", 2)
        tp = self._o("SELL", "LMT", 2, lmtPrice=29292.0)
        sl = self._o("SELL", "STP", 2, auxPrice=28942.0)
        entry_id, child_ids = c.place_bracket(
            contract=contract, parent=entry, children=[tp, sl])

        # ids are monotonic: entry 1000, children 1001/1002
        self.assertEqual(entry_id, 1000)
        self.assertEqual(child_ids, [1001, 1002])

        # entry held (transmit False), children carry parentId + OCA,
        # only the LAST transmits.
        self.assertFalse(entry.transmit)
        self.assertEqual(tp.parentId, 1000)
        self.assertEqual(sl.parentId, 1000)
        self.assertEqual(tp.ocaGroup, sl.ocaGroup)
        self.assertTrue(tp.ocaGroup.startswith("oca_1000"))
        self.assertFalse(tp.transmit)   # not last
        self.assertTrue(sl.transmit)    # last transmits the group

        # all three were placed
        self.assertEqual(len(c.placed), 3)

    def test_oca_group_seed_makes_name_unique_per_follower(self):
        # Regression: with several followers, each is a separate client
        # whose ids restart from nextValidId, so without a seed the first
        # bracket on every follower is "oca_<entry>" with the same entry
        # id → collision → modify rejected with 10326. Seeding with the
        # account id keeps the group name unique per follower.
        c = _connected_client()
        tp = self._o("SELL", "LMT", 2, lmtPrice=29292.0)
        sl = self._o("SELL", "STP", 2, auxPrice=28942.0)
        c.place_bracket(contract=Contract(), parent=self._o("BUY", "MKT", 2),
                        children=[tp, sl], oca_group_seed="DU2967357")
        self.assertEqual(tp.ocaGroup, sl.ocaGroup)
        self.assertEqual(tp.ocaGroup, "oca_DU2967357_1000")

    def test_two_followers_same_entry_id_get_distinct_oca_groups(self):
        # Two fresh clients (followers) both start at nextValidId=1000;
        # different seeds must yield different group names so IBKR sees
        # them as unrelated OCA groups.
        a = _connected_client()
        b = _connected_client()
        ta = self._o("SELL", "LMT", 1, lmtPrice=1.0)
        sa = self._o("SELL", "STP", 1, auxPrice=2.0)
        tb = self._o("SELL", "LMT", 1, lmtPrice=1.0)
        sb = self._o("SELL", "STP", 1, auxPrice=2.0)
        a.place_bracket(contract=Contract(), parent=self._o("BUY", "MKT", 1),
                        children=[ta, sa], oca_group_seed="DUQ752730")
        b.place_bracket(contract=Contract(), parent=self._o("BUY", "MKT", 1),
                        children=[tb, sb], oca_group_seed="DU5915979")
        self.assertNotEqual(ta.ocaGroup, tb.ocaGroup)

    def test_seed_is_sanitised(self):
        # A seed with spaces/odd chars is reduced to alnum/-/_ so the
        # group name stays a clean token.
        c = _connected_client()
        tp = self._o("SELL", "LMT", 1, lmtPrice=1.0)
        sl = self._o("SELL", "STP", 1, auxPrice=2.0)
        c.place_bracket(contract=Contract(), parent=self._o("BUY", "MKT", 1),
                        children=[tp, sl], oca_group_seed="A B/C*D")
        self.assertEqual(tp.ocaGroup, "oca_ABCD_1000")


class TestCancelModify(unittest.TestCase):

    def test_cancel(self):
        c = _connected_client()
        c.cancel_order(1234)
        self.assertEqual(c.cancelled, [1234])

    def test_modify_replaces_same_id_and_transmits(self):
        c = _connected_client()
        o = Order()
        o.action = "BUY"
        o.orderType = "LMT"
        o.totalQuantity = 2
        o.lmtPrice = 100.0
        c.modify_order(order_id=999, contract=Contract(), order=o)
        self.assertEqual(o.orderId, 999)
        self.assertTrue(o.transmit)
        self.assertEqual(c.placed[0][0], 999)


class TestOrderStatus(unittest.TestCase):

    def test_status_tracked_from_callback(self):
        c = _connected_client()
        c._on_order_status(555, "Submitted")
        self.assertEqual(c.order_status(555), "Submitted")
        c._on_order_status(555, "Filled")
        self.assertEqual(c.order_status(555), "Filled")

    def test_status_from_open_order_callback(self):
        c = _connected_client()

        class _State:
            status = "PreSubmitted"
        c._on_open_order(777, None, None, _State())
        self.assertEqual(c.order_status(777), "PreSubmitted")

    def test_open_order_echo_captured(self):
        # The order EXACTLY as IBKR echoed it must be stashed so a later
        # OCA-safe modify can re-place IBKR's own stored order.
        c = _connected_client()

        class _State:
            status = "Submitted"
        contract = Contract()
        order = Order()
        order.ocaGroup = "oca_x_5"
        c._on_open_order(888, contract, order, _State())
        echo = c.open_order_echo(888)
        self.assertIsNotNone(echo)
        self.assertIs(echo[0], contract)
        self.assertIs(echo[1], order)
        # Unknown id → None (no echo yet).
        self.assertIsNone(c.open_order_echo(999))

    def test_unknown_order_raises(self):
        c = _connected_client()
        with self.assertRaises(IbkrOrderNotFound):
            c.order_status(424242)


class TestContractResolution(unittest.TestCase):

    def test_resolve_and_cache(self):
        c = _connected_client()
        r = c.resolve_contract("MNQM6")
        self.assertEqual(r.con_id, 770561201)
        self.assertEqual(r.contract.symbol, "MNQ")
        # Second call must hit cache (no new request).
        n_before = len(c.contract_requests)
        r2 = c.resolve_contract("MNQM6")
        self.assertIs(r2, r)
        self.assertEqual(len(c.contract_requests), n_before)

    def test_resolve_disconnected_raises(self):
        c = _connected_client()
        c._fake_connected = False
        with self.assertRaises(IbkrNotConnected):
            c.resolve_contract("MNQM6")

    def test_unparseable_symbol_raises(self):
        c = _connected_client()
        with self.assertRaises(IbkrApiError):
            c.resolve_contract("NOT_A_FUTURE")


class TestBuildQueryContract(unittest.TestCase):
    """Pure symbol-parsing + year disambiguation, no client needed."""

    def test_month_and_base_parsed(self):
        c = IbkrApiClient._build_query_contract("MNQM6")
        self.assertEqual(c.symbol, "MNQ")
        self.assertEqual(c.secType, "FUT")
        # June = month 06; year ends in 6 → 2026 (>= 2026 now).
        self.assertTrue(c.lastTradeDateOrContractMonth.endswith("06"))
        self.assertTrue(c.lastTradeDateOrContractMonth.startswith("202"))

    def test_all_month_codes_known(self):
        # Sanity: every CME month code maps to 1..12.
        self.assertEqual(set(_MONTH_CODE_TO_NUM.values()), set(range(1, 13)))

    def test_bad_symbol_raises(self):
        with self.assertRaises(IbkrApiError):
            IbkrApiClient._build_query_contract("XYZ")


class TestGetPositions(unittest.TestCase):
    """Regression: reqPositions streams EVERY account the Gateway login
    can see; get_positions must filter to the followed account, or other
    accounts' holdings leak in as phantom positions (a false MISMATCH was
    seen live while the followed account was actually flat)."""

    def _client(self, managed="U0000001"):
        c = _connected_client()
        c._managed_accounts = managed
        return c

    def test_filters_to_single_managed_account(self):
        c = self._client(managed="U0000001")
        c._fake_positions = [
            ("U0000001", 770561201, -1),    # ours
            ("U9999999", 129386728, 60),    # another account — must be ignored
            ("U9999999", 613022092, 20),
        ]
        pos = c.get_positions(timeout=1.0)
        self.assertEqual(pos, {770561201: -1})

    def test_flat_account_returns_empty(self):
        c = self._client(managed="U0000001")
        c._fake_positions = [("U9999999", 129386728, 60)]  # only other acct
        self.assertEqual(c.get_positions(timeout=1.0), {})

    def test_drops_zero_net_positions(self):
        c = self._client(managed="U0000001")
        c._fake_positions = [
            ("U0000001", 770561201, 0),     # flat row — dropped
            ("U0000001", 770561202, 2),
        ]
        self.assertEqual(c.get_positions(timeout=1.0), {770561202: 2})

    def test_explicit_account_overrides_managed(self):
        c = self._client(managed="U0000001")
        c._fake_positions = [
            ("U0000001", 770561201, -1),
            ("U5555555", 770561202, 3),
        ]
        self.assertEqual(c.get_positions(timeout=1.0, account="U5555555"),
                         {770561202: 3})

    def test_multi_account_login_without_account_raises(self):
        c = self._client(managed="U0000001,U5555555")
        c._fake_positions = []
        with self.assertRaises(IbkrApiError):
            c.get_positions(timeout=1.0)

    def test_non_future_instruments_are_filtered_out(self):
        # Real-world case: an IBKR account holds sovereign BONDs (which
        # Tradovate knows nothing about) plus a flat MNQ future. Only
        # futures should be considered; the bonds must NOT appear or
        # they read as permanent phantom mismatches against Tradovate.
        c = self._client(managed="U0000001")
        c._fake_positions = [
            ("U0000001", 613022092, 20, "BOND"),   # POLAND govt bond
            ("U0000001", 129386728, 60, "BOND"),   # FRTR govt bond
            ("U0000001", 770561201, 0,  "FUT"),    # MNQ — flat
        ]
        self.assertEqual(c.get_positions(timeout=1.0), {})

    def test_future_kept_alongside_bonds(self):
        c = self._client(managed="U0000001")
        c._fake_positions = [
            ("U0000001", 613022092, 20, "BOND"),
            ("U0000001", 770561201, -1, "FUT"),
        ]
        self.assertEqual(c.get_positions(timeout=1.0), {770561201: -1})

    def test_sec_types_none_disables_type_filter(self):
        c = self._client(managed="U0000001")
        c._fake_positions = [
            ("U0000001", 613022092, 20, "BOND"),
            ("U0000001", 770561201, -1, "FUT"),
        ]
        got = c.get_positions(timeout=1.0, sec_types=None)
        self.assertEqual(got, {613022092: 20, 770561201: -1})


@unittest.skipUnless(
    os.environ.get("TRADESYNC_IBKR_LIVE") == "1",
    "live IBKR Gateway test — set TRADESYNC_IBKR_LIVE=1 with a paper "
    "Gateway running on 127.0.0.1:4002 to enable")
class TestLiveGateway(unittest.TestCase):
    """Optional end-to-end against a real paper Gateway. Never places
    orders — only connects and resolves a contract."""

    def test_connect_and_resolve(self):
        c = IbkrApiClient(host="127.0.0.1", port=4002, client_id=79)
        c.connect_and_wait(timeout=10)
        try:
            self.assertTrue(c.is_connected)
            r = c.resolve_contract("MNQM6")
            self.assertGreater(r.con_id, 0)
            self.assertEqual(r.contract.symbol, "MNQ")
        finally:
            c.disconnect_and_wait()


if __name__ == "__main__":
    unittest.main()
