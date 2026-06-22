"""
Tests for IbkrFollowerEndpoint — the FollowerEndpoint adapter over
IbkrApiClient.

Pin the neutral→IBKR translation: Side→BUY/SELL, OrderType→MKT/LMT/STP/
'STP LMT', TimeInForce→DAY/GTC/..., price gating by order type, the
bracket parentId/OCA wiring delegated to the client, and the modify
rebuild-from-remembered-order path. Uses a fake IbkrApiClient that
records calls and returns canned ids — no socket, no Gateway.
"""

import unittest
from unittest.mock import MagicMock

from ibapi.common import UNSET_DOUBLE
from ibapi.contract import Contract

from tradesync.brokers.endpoint import (
    FollowerEndpoint,
    PlacedBracketRef,
    PlacedRef,
)
from tradesync.brokers.ibkr_api_client import IbkrApiError
from tradesync.brokers.ibkr_follower_endpoint import (
    IbkrFollowerEndpoint,
    _build_order,
)
from tradesync.order_event import (
    BracketRole,
    BracketSpec,
    ModifySpec,
    OrderSpec,
    OrderType,
    Side,
    TimeInForce,
)


class _Resolved:
    def __init__(self, con_id=770561201):
        self.con_id = con_id
        c = Contract()
        c.symbol = "MNQ"
        c.secType = "FUT"
        c.conId = con_id
        self.contract = c


class FakeIbkrApiClient:
    def __init__(self):
        self.placed = []           # (contract, order)
        self.brackets = []         # (contract, parent, children)
        self.cancelled = []
        self.modified = []         # (id, contract, order)
        self.statuses = {}
        self.connected_count = 0
        self.disconnected_count = 0
        self._next = 1000
        # Comma-separated managed-account list IBKR reports after
        # connect. Defaults to the account _endpoint() configures so the
        # follower's connect() account-match guardrail passes in tests
        # that don't care about it; override per-test to exercise it.
        self.managed_accounts = "DU0000002"

    def connect_and_wait(self):
        self.connected_count += 1

    def disconnect_and_wait(self):
        self.disconnected_count += 1

    def resolve_contract(self, symbol):
        return _Resolved()

    def place_order(self, *, contract, order):
        self.placed.append((contract, order))
        oid = self._next
        self._next += 1
        return oid

    def place_bracket(self, *, contract, parent, children):
        self.brackets.append((contract, parent, children))
        entry = self._next
        self._next += 1
        # Mirror IbkrApiClient.place_bracket: stamp the OCA group +
        # parentId onto each child Order in place (these are the very
        # objects the endpoint remembers for later modify, so the OCA
        # fields must be present for the modify-path test to be faithful).
        oca_group = f"oca_{entry}"
        cids = []
        for child in children:
            child.parentId = entry
            child.ocaGroup = oca_group
            child.ocaType = 1
            cids.append(self._next)
            self._next += 1
        return entry, cids

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)

    def modify_order(self, *, order_id, contract, order):
        self.modified.append((order_id, contract, order))

    def order_status(self, order_id):
        return self.statuses.get(order_id, "Submitted")


def _endpoint():
    client = FakeIbkrApiClient()
    ep = IbkrFollowerEndpoint(client, env="demo", account_id="DU0000002")
    return ep, client


class TestIdentityAndProtocol(unittest.TestCase):

    def test_identity(self):
        ep, _ = _endpoint()
        self.assertEqual(ep.identity, "ibkr_demo_DU0000002")

    def test_satisfies_follower_protocol(self):
        ep, _ = _endpoint()
        self.assertIsInstance(ep, FollowerEndpoint)

    def test_connect_disconnect_delegate(self):
        ep, client = _endpoint()
        ep.connect()
        ep.disconnect()
        self.assertEqual(client.connected_count, 1)
        self.assertEqual(client.disconnected_count, 1)

    def test_rejection_handler_adapts_int_id_to_string(self):
        ep, client = _endpoint()
        seen = []
        handler = MagicMock(side_effect=lambda oid, code, msg: seen.append(oid))
        ep.set_rejection_handler(handler)
        # Simulate the client firing its int-id callback.
        client.on_order_rejected(12345, 201, "rejected")
        handler.assert_called_once_with("12345", 201, "rejected")
        self.assertEqual(seen, ["12345"])   # stringified

    def test_rejection_handler_none_clears(self):
        ep, client = _endpoint()
        ep.set_rejection_handler(MagicMock())
        ep.set_rejection_handler(None)
        self.assertIsNone(client.on_order_rejected)


class TestBuildOrderTranslation(unittest.TestCase):

    def test_market_buy(self):
        o = _build_order(OrderSpec(side=Side.BUY, quantity=2,
                                   order_type=OrderType.MARKET))
        self.assertEqual(o.action, "BUY")
        self.assertEqual(o.orderType, "MKT")
        self.assertEqual(o.totalQuantity, 2)
        # MKT carries no prices — ibapi leaves them at UNSET_DOUBLE
        # (its "not set" sentinel; NOT 0 or None — sending 0 would be
        # a real price IBKR might reject).
        self.assertEqual(o.lmtPrice, UNSET_DOUBLE)
        self.assertEqual(o.auxPrice, UNSET_DOUBLE)

    def test_limit_sell_gtc(self):
        o = _build_order(OrderSpec(side=Side.SELL, quantity=1,
                                   order_type=OrderType.LIMIT,
                                   limit_price=29292.0, tif=TimeInForce.GTC))
        self.assertEqual(o.action, "SELL")
        self.assertEqual(o.orderType, "LMT")
        self.assertEqual(o.lmtPrice, 29292.0)
        self.assertEqual(o.tif, "GTC")

    def test_stop_gates_to_aux(self):
        o = _build_order(OrderSpec(side=Side.SELL, quantity=1,
                                   order_type=OrderType.STOP,
                                   stop_price=28942.0))
        self.assertEqual(o.orderType, "STP")
        self.assertEqual(o.auxPrice, 28942.0)
        # limit price stays unset on a pure stop
        self.assertEqual(o.lmtPrice, UNSET_DOUBLE)

    def test_stop_limit_uses_ibkr_spaced_token_and_both_prices(self):
        o = _build_order(OrderSpec(side=Side.BUY, quantity=1,
                                   order_type=OrderType.STOP_LIMIT,
                                   limit_price=100.0, stop_price=99.0))
        self.assertEqual(o.orderType, "STP LMT")
        self.assertEqual(o.lmtPrice, 100.0)
        self.assertEqual(o.auxPrice, 99.0)


class TestPlaceOrder(unittest.TestCase):

    def test_place_returns_stringified_id_and_remembers(self):
        ep, client = _endpoint()
        ref = ep.place_order(
            OrderSpec(side=Side.BUY, quantity=2, order_type=OrderType.MARKET),
            symbol="MNQM6")
        self.assertIsInstance(ref, PlacedRef)
        self.assertEqual(ref.follower_order_id, "1000")
        self.assertEqual(ref.raw["conId"], 770561201)
        self.assertEqual(len(client.placed), 1)


class TestPlaceBracket(unittest.TestCase):

    def _bracket(self):
        entry = OrderSpec(side=Side.BUY, quantity=2,
                          order_type=OrderType.MARKET,
                          role=BracketRole.ENTRY)
        tp = OrderSpec(side=Side.SELL, quantity=2, order_type=OrderType.LIMIT,
                       limit_price=29292.0, role=BracketRole.TAKE_PROFIT)
        sl = OrderSpec(side=Side.SELL, quantity=2, order_type=OrderType.STOP,
                       stop_price=28942.0, role=BracketRole.STOP_LOSS)
        return BracketSpec(entry=entry, children=[tp, sl])

    def test_bracket_ids_and_translation(self):
        ep, client = _endpoint()
        ref = ep.place_bracket(self._bracket(), symbol="MNQM6")
        self.assertIsInstance(ref, PlacedBracketRef)
        self.assertEqual(ref.entry_order_id, "1000")
        self.assertEqual(ref.child_order_ids, ["1001", "1002"])
        self.assertIsNone(ref.oco_id)
        # the client received translated parent + children
        _contract, parent, children = client.brackets[0]
        self.assertEqual(parent.orderType, "MKT")
        self.assertEqual(children[0].orderType, "LMT")
        self.assertEqual(children[0].lmtPrice, 29292.0)
        self.assertEqual(children[1].orderType, "STP")
        self.assertEqual(children[1].auxPrice, 28942.0)


class TestAccountStamping(unittest.TestCase):
    """Stage 4: every order must carry order.account = the follower's
    account, so a Gateway login seeing several accounts routes it to the
    right one (and never the default account)."""

    def test_place_order_stamps_account(self):
        ep, client = _endpoint()   # account_id="DU0000002"
        ep.place_order(
            OrderSpec(side=Side.BUY, quantity=1, order_type=OrderType.MARKET),
            symbol="MNQM6")
        _contract, order = client.placed[0]
        self.assertEqual(order.account, "DU0000002")

    def test_bracket_stamps_account_on_all_legs(self):
        ep, client = _endpoint()
        entry = OrderSpec(side=Side.BUY, quantity=1,
                          order_type=OrderType.MARKET, role=BracketRole.ENTRY)
        tp = OrderSpec(side=Side.SELL, quantity=1, order_type=OrderType.LIMIT,
                       limit_price=10.0, role=BracketRole.TAKE_PROFIT)
        sl = OrderSpec(side=Side.SELL, quantity=1, order_type=OrderType.STOP,
                       stop_price=9.0, role=BracketRole.STOP_LOSS)
        ep.place_bracket(BracketSpec(entry=entry, children=[tp, sl]),
                         symbol="MNQM6")
        _contract, parent, children = client.brackets[0]
        self.assertEqual(parent.account, "DU0000002")
        self.assertTrue(all(c.account == "DU0000002" for c in children))

    def test_modify_stamps_account(self):
        ep, client = _endpoint()
        ref = ep.place_order(
            OrderSpec(side=Side.BUY, quantity=1, order_type=OrderType.LIMIT,
                      limit_price=10.0),
            symbol="MNQM6")
        ep.modify_order(ref.follower_order_id,
                        ModifySpec(new_limit_price=11.0,
                                   order_type=OrderType.LIMIT))
        _id, _contract, order = client.modified[0]
        self.assertEqual(order.account, "DU0000002")


class TestCancelModifyStatus(unittest.TestCase):

    def test_cancel(self):
        ep, client = _endpoint()
        ep.cancel_order("1234")
        self.assertEqual(client.cancelled, [1234])

    def test_modify_rebuilds_from_remembered_order(self):
        ep, client = _endpoint()
        # First place a limit order so it's remembered.
        ref = ep.place_order(
            OrderSpec(side=Side.BUY, quantity=2, order_type=OrderType.LIMIT,
                      limit_price=100.0),
            symbol="MNQM6")
        ep.modify_order(ref.follower_order_id,
                        ModifySpec(new_limit_price=105.0,
                                   order_type=OrderType.LIMIT))
        self.assertEqual(len(client.modified), 1)
        oid, _contract, order = client.modified[0]
        self.assertEqual(oid, 1000)
        self.assertEqual(order.lmtPrice, 105.0)
        self.assertEqual(order.orderType, "LMT")

    def test_modify_unknown_id_raises(self):
        ep, _ = _endpoint()
        with self.assertRaises(IbkrApiError):
            ep.modify_order("99999", ModifySpec(new_quantity=3))

    def test_modify_quantity_only(self):
        ep, client = _endpoint()
        ref = ep.place_order(
            OrderSpec(side=Side.BUY, quantity=2, order_type=OrderType.LIMIT,
                      limit_price=100.0),
            symbol="MNQM6")
        ep.modify_order(ref.follower_order_id,
                        ModifySpec(new_quantity=5, order_type=OrderType.LIMIT))
        _oid, _c, order = client.modified[0]
        self.assertEqual(order.totalQuantity, 5)
        # price unchanged from the remembered order
        self.assertEqual(order.lmtPrice, 100.0)

    def test_order_status_delegates(self):
        ep, client = _endpoint()
        client.statuses[555] = "Filled"
        self.assertEqual(ep.order_status("555"), "Filled")

    def test_modify_bracket_leg_strips_oca_group(self):
        # Regression for the live OCA errors on a bracket-leg modify,
        # both verified on paper with the stop price proven unchanged
        # afterwards via reqAllOpenOrders:
        #   * sending no OCA fields → code 10327 "OCA group type revision
        #     is not allowed";
        #   * re-sending ocaGroup+ocaType → code 10326 "OCA group revision
        #     is not allowed", and the stop did NOT move.
        # The fix: a modify re-places ONLY the changed economic fields and
        # leaves OCA grouping (and parentId) out entirely — IBKR keeps the
        # leg in its group, set at placement, from the order id. So the
        # re-placed leg must carry NO ocaGroup / ocaType / parentId, while
        # the new price still applies.
        ep, client = _endpoint()
        entry = OrderSpec(side=Side.BUY, quantity=1,
                          order_type=OrderType.MARKET, role=BracketRole.ENTRY)
        tp = OrderSpec(side=Side.SELL, quantity=1, order_type=OrderType.LIMIT,
                       limit_price=120.0, role=BracketRole.TAKE_PROFIT)
        sl = OrderSpec(side=Side.SELL, quantity=1, order_type=OrderType.STOP,
                       stop_price=90.0, role=BracketRole.STOP_LOSS)
        ref = ep.place_bracket(BracketSpec(entry=entry, children=[tp, sl]),
                               symbol="MNQM6")
        # Move the stop-loss leg (the second child).
        sl_id = ref.child_order_ids[1]
        ep.modify_order(sl_id, ModifySpec(new_stop_price=88.0,
                                          order_type=OrderType.STOP))
        _oid, _contract, order = client.modified[-1]
        # The re-placed leg must NOT restate OCA grouping or the parent —
        # restating either is what IBKR rejects as a group revision.
        self.assertFalse(getattr(order, "ocaGroup", ""))
        self.assertFalse(getattr(order, "ocaType", 0))
        self.assertFalse(getattr(order, "parentId", 0))
        # And the price change still went through.
        self.assertEqual(order.auxPrice, 88.0)

    def test_modify_single_order_has_no_oca_group(self):
        # A plain (non-bracket) order has no OCA group, and a modify must
        # NOT invent one — only bracket legs carry it.
        ep, client = _endpoint()
        ref = ep.place_order(
            OrderSpec(side=Side.BUY, quantity=1, order_type=OrderType.LIMIT,
                      limit_price=100.0),
            symbol="MNQM6")
        ep.modify_order(ref.follower_order_id,
                        ModifySpec(new_limit_price=101.0,
                                   order_type=OrderType.LIMIT))
        _oid, _contract, order = client.modified[-1]
        self.assertFalse(getattr(order, "ocaGroup", ""))


class TestAccountReachableGuardrail(unittest.TestCase):
    """connect() must refuse to operate if the connected Gateway does
    not manage the configured follower account — the guardrail that
    stops orders landing on the wrong IBKR account (critical for
    IBKR→IBKR, where the follower is a different account)."""

    def test_connect_ok_when_account_managed(self):
        ep, client = _endpoint()                 # account_id DU0000002
        client.managed_accounts = "DU0000002"
        ep.connect()                             # must not raise
        self.assertEqual(client.connected_count, 1)

    def test_connect_ok_when_account_among_several(self):
        ep, client = _endpoint()
        client.managed_accounts = "DU111,DU0000002,DU222"
        ep.connect()                             # must not raise

    def test_connect_raises_on_account_mismatch(self):
        ep, client = _endpoint()
        client.managed_accounts = "DU999999"     # not the configured one
        with self.assertRaises(IbkrApiError):
            ep.connect()

    def test_connect_proceeds_when_managed_unknown(self):
        # Empty/absent managed list → can't verify → warn + proceed
        # (safety net, not a hard gate), so other flows keep working.
        ep, client = _endpoint()
        client.managed_accounts = ""
        ep.connect()                             # must not raise


if __name__ == "__main__":
    unittest.main()
