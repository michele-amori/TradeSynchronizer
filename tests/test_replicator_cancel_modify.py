"""
Replicator tests for the cancellation and modification flows. Uses
fakes for TradovateClient and the contract resolver so we exercise
the dispatch logic without hitting the network.

Run from the repo root:

    python3 -m unittest tests.test_replicator_cancel_modify
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Optional

from tradesync.brokers.tradovate import (
    PlacedOrder,
    TradovateOrderError,
    TradovateOrderNotFound,
)
from tradesync.config import Config
from tradesync.order_map import OrderMap
from tradesync.proxy.ibkr_parser import (
    IbkrOrder,
    IbkrOrderCancel,
    IbkrOrderModify,
)
from tradesync.replicator import Replicator


# ── Fakes ──────────────────────────────────────────────────────────────── #

class FakeResolver:
    """Trivial conid→symbol map for tests."""
    def __init__(self):
        self.calls = []

    def resolve_symbol(self, conid: int) -> str:
        self.calls.append(conid)
        return "MESH6"

    def capture_token(self, _auth):
        pass

    def observe_contract_info(self, _path, _body):
        pass


class FakeTradovate:
    """In-memory Tradovate stub with controllable behaviour."""

    def __init__(self):
        self.placed: list[dict] = []
        self.cancelled: list[int] = []
        self.modified: list[dict] = []
        self.next_order_id = 1000
        self.cancel_raises: Optional[BaseException] = None
        self.modify_raises: Optional[BaseException] = None
        self.connected = True

    def get_contract_id(self, _symbol: str) -> int:
        return 7777

    def place_order(self, **kwargs):
        self.placed.append(kwargs)
        oid = self.next_order_id
        self.next_order_id += 1
        return PlacedOrder(order_id=oid, raw={"orderId": oid})

    def cancel_order(self, order_id: int):
        if self.cancel_raises:
            raise self.cancel_raises
        self.cancelled.append(int(order_id))
        return {"orderId": order_id, "ok": True}

    def modify_order(self, order_id: int, **changes):
        if self.modify_raises:
            raise self.modify_raises
        rec = {"orderId": int(order_id)}
        rec.update(changes)
        self.modified.append(rec)
        return rec


def _make_config(*, watched=None, mode="mirror") -> Config:
    return Config(
        tradovate_username="u", tradovate_password="p",
        tradovate_app_id="TradeSynchronizer", tradovate_app_ver="1.0",
        tradovate_cid="cid", tradovate_sec="sec",
        tradovate_env="demo", tradovate_acct_id=None,
        proxy_host="127.0.0.1", proxy_port=8081,
        replication_mode=mode,
        skip_protective_stops=True,
        ibkr_watched_accounts=watched or [],
        log_level="INFO", log_file="/tmp/x.log",
    )


def _make_ibkr_order(**overrides) -> IbkrOrder:
    base = dict(
        account_id="U7713037",
        conid=845307883,
        side="BUY",
        quantity=2,
        order_type="LMT",
        price=21500.0,
        aux_price=None,
        tif="DAY",
        cOID="tv-1",
        raw={},
    )
    base.update(overrides)
    return IbkrOrder(**base)


def _build_replicator(cfg=None, tradovate=None, resolver=None,
                      tmp_path: Optional[Path] = None):
    cfg = cfg or _make_config()
    tradovate = tradovate or FakeTradovate()
    resolver = resolver or FakeResolver()
    store = OrderMap(tmp_path / "orders.json") if tmp_path else \
        OrderMap(Path(tempfile.mkdtemp()) / "orders.json")
    r = Replicator(
        cfg=cfg, tradovate=tradovate, resolver=resolver, order_map=store,
    )
    return r, tradovate, store


# ── Tests ─────────────────────────────────────────────────────────────── #

class TestReplicateNewRegistersMap(unittest.TestCase):
    """The first thing we have to prove: a successful replicate_new
    leaves the order_map populated with cOID → Tradovate id, so a
    subsequent cancel/modify can find it."""

    def test_successful_new_order_registers_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            r, tradovate, store = _build_replicator(tmp_path=Path(tmp))
            result = r.replicate_new(_make_ibkr_order(cOID="tv-1"))
            self.assertTrue(result.success, result.reason)
            self.assertEqual(len(tradovate.placed), 1)
            rec = store.get_by_coid("tv-1")
            self.assertIsNotNone(rec)
            self.assertEqual(rec.tradovate_id, 1000)

    def test_register_ibkr_id_makes_cancel_lookup_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            r, tradovate, store = _build_replicator(tmp_path=Path(tmp))
            r.replicate_new(_make_ibkr_order(cOID="tv-1"))
            # Simulating the response-hook callback
            r.register_ibkr_id("tv-1", "ibkr-42")
            self.assertEqual(store.tradovate_for_ibkr_id("ibkr-42"), 1000)


class TestReplicateCancel(unittest.TestCase):

    def _setup(self, tmp):
        r, tradovate, store = _build_replicator(tmp_path=Path(tmp))
        r.replicate_new(_make_ibkr_order(cOID="tv-1"))
        r.register_ibkr_id("tv-1", "ibkr-42")
        return r, tradovate, store

    def test_happy_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            r, tradovate, store = self._setup(tmp)
            result = r.replicate_cancel(IbkrOrderCancel(
                account_id="U7713037", ibkr_order_id="ibkr-42"))
            self.assertTrue(result.success, result.reason)
            self.assertEqual(tradovate.cancelled, [1000])
            # Entry should be gone after cancel
            self.assertIsNone(store.tradovate_for_ibkr_id("ibkr-42"))

    def test_unknown_ibkr_id_is_skipped_not_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            r, tradovate, _ = self._setup(tmp)
            result = r.replicate_cancel(IbkrOrderCancel(
                account_id="U7713037", ibkr_order_id="ibkr-unknown"))
            self.assertFalse(result.success)
            self.assertTrue(result.skipped)
            self.assertEqual(tradovate.cancelled, [])

    def test_account_filter_skips_cancel(self):
        with tempfile.TemporaryDirectory() as tmp:
            r, tradovate, _ = _build_replicator(
                cfg=_make_config(watched=["U9999999"]),
                tmp_path=Path(tmp),
            )
            # Place + register so the map has the entry
            r.replicate_new(_make_ibkr_order(cOID="tv-1",
                                              account_id="U9999999"))
            r.register_ibkr_id("tv-1", "ibkr-42")
            # But the cancel is for a non-watched account
            result = r.replicate_cancel(IbkrOrderCancel(
                account_id="U7713037", ibkr_order_id="ibkr-42"))
            self.assertTrue(result.skipped)
            self.assertEqual(tradovate.cancelled, [])

    def test_tradovate_not_found_is_treated_as_skip_and_cleans_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            r, tradovate, store = self._setup(tmp)
            tradovate.cancel_raises = TradovateOrderNotFound(
                "OrderNotFound: order 1000 already filled"
            )
            result = r.replicate_cancel(IbkrOrderCancel(
                account_id="U7713037", ibkr_order_id="ibkr-42"))
            self.assertFalse(result.success)
            self.assertTrue(result.skipped)
            # Map entry tidied up
            self.assertIsNone(store.tradovate_for_ibkr_id("ibkr-42"))

    def test_other_tradovate_error_is_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            r, tradovate, store = self._setup(tmp)
            tradovate.cancel_raises = TradovateOrderError("HTTP 500")
            result = r.replicate_cancel(IbkrOrderCancel(
                account_id="U7713037", ibkr_order_id="ibkr-42"))
            self.assertFalse(result.success)
            self.assertFalse(result.skipped)
            # Map entry preserved (we may retry later)
            self.assertEqual(store.tradovate_for_ibkr_id("ibkr-42"), 1000)


class TestReplicateModify(unittest.TestCase):

    def _setup(self, tmp, *, mode="mirror"):
        r, tradovate, store = _build_replicator(
            cfg=_make_config(mode=mode), tmp_path=Path(tmp),
        )
        r.replicate_new(_make_ibkr_order(cOID="tv-1"))
        r.register_ibkr_id("tv-1", "ibkr-42")
        return r, tradovate, store

    def test_price_modification(self):
        with tempfile.TemporaryDirectory() as tmp:
            r, tradovate, _ = self._setup(tmp)
            result = r.replicate_modify(IbkrOrderModify(
                account_id="U7713037", ibkr_order_id="ibkr-42",
                quantity=None, price=21600.0, aux_price=None, tif=None,
                raw={},
            ))
            self.assertTrue(result.success, result.reason)
            self.assertEqual(len(tradovate.modified), 1)
            self.assertEqual(tradovate.modified[0]["limit_price"], 21600.0)

    def test_qty_and_tif_modification(self):
        with tempfile.TemporaryDirectory() as tmp:
            r, tradovate, _ = self._setup(tmp)
            result = r.replicate_modify(IbkrOrderModify(
                account_id="U7713037", ibkr_order_id="ibkr-42",
                quantity=5, price=None, aux_price=None, tif="GTC",
                raw={},
            ))
            self.assertTrue(result.success, result.reason)
            mod = tradovate.modified[0]
            self.assertEqual(mod["qty"], 5)
            self.assertEqual(mod["tif"], "GTC")
            self.assertIsNone(mod["limit_price"])

    def test_market_mode_drops_price_change_but_keeps_qty(self):
        with tempfile.TemporaryDirectory() as tmp:
            r, tradovate, _ = self._setup(tmp, mode="market")
            result = r.replicate_modify(IbkrOrderModify(
                account_id="U7713037", ibkr_order_id="ibkr-42",
                quantity=3, price=21600.0, aux_price=None, tif=None,
                raw={},
            ))
            self.assertTrue(result.success, result.reason)
            mod = tradovate.modified[0]
            self.assertEqual(mod["qty"], 3)
            # price/stop dropped because replication_mode='market' →
            # the Tradovate replica is a Market order, no prices.
            self.assertIsNone(mod["limit_price"])
            self.assertIsNone(mod["stop_price"])

    def test_modify_with_no_replicable_fields_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            r, tradovate, _ = self._setup(tmp)
            result = r.replicate_modify(IbkrOrderModify(
                account_id="U7713037", ibkr_order_id="ibkr-42",
                quantity=None, price=None, aux_price=None, tif=None,
                raw={},
            ))
            self.assertFalse(result.success)
            self.assertTrue(result.skipped)
            self.assertEqual(tradovate.modified, [])

    def test_unknown_ibkr_id_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            r, tradovate, _ = self._setup(tmp)
            result = r.replicate_modify(IbkrOrderModify(
                account_id="U7713037", ibkr_order_id="ibkr-unknown",
                quantity=1, price=None, aux_price=None, tif=None,
                raw={},
            ))
            self.assertTrue(result.skipped)
            self.assertEqual(tradovate.modified, [])

    def test_tradovate_not_found_cleans_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            r, tradovate, store = self._setup(tmp)
            tradovate.modify_raises = TradovateOrderNotFound("gone")
            result = r.replicate_modify(IbkrOrderModify(
                account_id="U7713037", ibkr_order_id="ibkr-42",
                quantity=1, price=None, aux_price=None, tif=None,
                raw={},
            ))
            self.assertTrue(result.skipped)
            self.assertIsNone(store.tradovate_for_ibkr_id("ibkr-42"))


if __name__ == "__main__":
    unittest.main()
