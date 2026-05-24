"""
Unit tests for tradesync.order_map — the persistent, thread-safe
map between IBKR order ids and Tradovate order ids.

Run from the repo root:

    python3 -m unittest tests.test_order_map
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from tradesync.order_map import OrderMap, default_store_path


class _TmpMap:
    """Context manager: fresh scratch directory with an OrderMap inside."""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "orders.json"
        return OrderMap(self.path)

    def __exit__(self, *exc):
        self._tmp.cleanup()


class TestOrderMapBasics(unittest.TestCase):

    def test_empty_map_initially(self):
        with _TmpMap() as m:
            self.assertEqual(len(m), 0)
            self.assertIsNone(m.tradovate_for_ibkr_id("anything"))

    def test_add_pending_then_complete(self):
        with _TmpMap() as m:
            m.add_pending("cOID-1")
            self.assertEqual(len(m), 1)
            # No IBKR id, no Tradovate id yet → lookups return None
            self.assertIsNone(m.tradovate_for_ibkr_id("ibkr-42"))

            m.set_tradovate_id("cOID-1", 987654)
            m.set_ibkr_id("cOID-1", "ibkr-42")
            self.assertEqual(m.tradovate_for_ibkr_id("ibkr-42"), 987654)

    def test_setters_tolerate_either_order(self):
        """The Tradovate worker thread and the mitmproxy response
        hook race; both setters must succeed in either order."""
        with _TmpMap() as m1, _TmpMap() as m2:
            # Tradovate-first
            m1.set_tradovate_id("cOID-A", 111)
            m1.set_ibkr_id("cOID-A", "ibkr-A")
            self.assertEqual(m1.tradovate_for_ibkr_id("ibkr-A"), 111)
            # IBKR-first
            m2.set_ibkr_id("cOID-B", "ibkr-B")
            m2.set_tradovate_id("cOID-B", 222)
            self.assertEqual(m2.tradovate_for_ibkr_id("ibkr-B"), 222)

    def test_set_ibkr_id_overwrite_clears_reverse_index(self):
        with _TmpMap() as m:
            m.set_tradovate_id("cOID-1", 100)
            m.set_ibkr_id("cOID-1", "old-id")
            self.assertEqual(m.tradovate_for_ibkr_id("old-id"), 100)
            # Overwrite with a new IBKR id
            m.set_ibkr_id("cOID-1", "new-id")
            self.assertEqual(m.tradovate_for_ibkr_id("new-id"), 100)
            self.assertIsNone(m.tradovate_for_ibkr_id("old-id"))

    def test_empty_args_are_noops(self):
        with _TmpMap() as m:
            m.add_pending("")
            m.set_tradovate_id("", 1)
            m.set_ibkr_id("cOID-1", "")
            self.assertEqual(len(m), 0)


class TestOrderMapRemoval(unittest.TestCase):

    def test_remove_by_ibkr_id(self):
        with _TmpMap() as m:
            m.set_tradovate_id("c-1", 100)
            m.set_ibkr_id("c-1", "i-1")
            m.remove_by_ibkr_id("i-1")
            self.assertEqual(len(m), 0)
            self.assertIsNone(m.tradovate_for_ibkr_id("i-1"))
            self.assertIsNone(m.get_by_coid("c-1"))

    def test_remove_by_coid_clears_reverse_index(self):
        with _TmpMap() as m:
            m.set_tradovate_id("c-1", 100)
            m.set_ibkr_id("c-1", "i-1")
            m.remove_by_coid("c-1")
            self.assertEqual(len(m), 0)
            self.assertIsNone(m.tradovate_for_ibkr_id("i-1"))


class TestOrderMapPersistence(unittest.TestCase):

    def test_save_and_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "subdir" / "orders.json"
            m1 = OrderMap(path)
            m1.set_tradovate_id("c-1", 100)
            m1.set_ibkr_id("c-1", "i-1")
            m1.set_tradovate_id("c-2", 200)

            self.assertTrue(path.exists(),
                            "OrderMap should create the file + dir")
            data = json.loads(path.read_text())
            self.assertEqual(data["schema"], 1)
            self.assertEqual(len(data["orders"]), 2)

            # Fresh instance reads the same data
            m2 = OrderMap(path)
            self.assertEqual(len(m2), 2)
            self.assertEqual(m2.tradovate_for_ibkr_id("i-1"), 100)
            rec = m2.get_by_coid("c-2")
            self.assertIsNotNone(rec)
            self.assertEqual(rec.tradovate_id, 200)

    def test_corrupt_file_is_tolerated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "orders.json"
            path.write_text("not json {")
            m = OrderMap(path)        # must NOT raise
            self.assertEqual(len(m), 0)

    def test_default_store_path_includes_env(self):
        p_live = default_store_path(Path("/root"), "live")
        p_demo = default_store_path(Path("/root"), "demo")
        self.assertEqual(p_live, Path("/root/.tradesync-state/orders-live.json"))
        self.assertEqual(p_demo, Path("/root/.tradesync-state/orders-demo.json"))
        self.assertNotEqual(p_live, p_demo)


class TestOrderMapThreadSafety(unittest.TestCase):

    def test_concurrent_writes(self):
        """Spin up several threads each adding entries and verify
        they all land in the map (no lost writes)."""
        with _TmpMap() as m:
            errors: list[BaseException] = []

            def worker(prefix: str, n: int):
                try:
                    for i in range(n):
                        coid = f"{prefix}-{i}"
                        m.set_tradovate_id(coid, hash(coid) & 0xFFFFFF)
                        m.set_ibkr_id(coid, f"ibkr-{prefix}-{i}")
                except BaseException as e:
                    errors.append(e)

            threads = [
                threading.Thread(target=worker, args=("A", 20)),
                threading.Thread(target=worker, args=("B", 20)),
                threading.Thread(target=worker, args=("C", 20)),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(errors, [])
            self.assertEqual(len(m), 60)
            # Every entry is roundtrip-lookupable
            for prefix in ("A", "B", "C"):
                for i in range(20):
                    self.assertEqual(
                        m.tradovate_for_ibkr_id(f"ibkr-{prefix}-{i}"),
                        hash(f"{prefix}-{i}") & 0xFFFFFF,
                    )


class TestBatchedWrites(unittest.TestCase):
    """OrderMap.batch() coalesces multiple mutations into a single
    disk write. Critical for the bracket-replication hot path."""

    def test_batch_collapses_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "orders.json"
            om = OrderMap(p)
            calls = []
            real_save = om._save_locked
            om._save_locked = lambda: (calls.append(1), real_save())[1]

            with om.batch():
                om.add_pending("c1")
                om.set_tradovate_id("c1", 100)
                om.set_ibkr_id("c1", "ibkr-1")
                om.add_pending("c2")
                om.set_tradovate_id("c2", 101)

            self.assertEqual(len(calls), 1,
                             f"expected 1 disk write, got {len(calls)}")
            self.assertEqual(om.tradovate_for_ibkr_id("ibkr-1"), 100)
            self.assertEqual(om.get_by_coid("c2").tradovate_id, 101)

    def test_batch_no_write_if_nothing_changed(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "orders.json"
            om = OrderMap(p)
            calls = []
            om._save_locked = lambda: calls.append(1)
            with om.batch():
                pass
            self.assertEqual(calls, [])

    def test_batch_nested_only_outermost_flushes(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "orders.json"
            om = OrderMap(p)
            calls = []
            real = om._save_locked
            om._save_locked = lambda: (calls.append(1), real())[1]

            with om.batch():
                om.add_pending("c1")
                with om.batch():
                    om.set_tradovate_id("c1", 100)
                self.assertEqual(len(calls), 0)
                om.set_ibkr_id("c1", "ibkr-1")
            self.assertEqual(len(calls), 1)

    def test_batch_flushes_even_on_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "orders.json"
            om = OrderMap(p)
            calls = []
            real = om._save_locked
            om._save_locked = lambda: (calls.append(1), real())[1]

            with self.assertRaises(ValueError):
                with om.batch():
                    om.add_pending("c1")
                    om.set_tradovate_id("c1", 100)
                    raise ValueError("boom")

            self.assertEqual(len(calls), 1)
            om2 = OrderMap(p)
            self.assertEqual(om2.get_by_coid("c1").tradovate_id, 100)


if __name__ == "__main__":
    unittest.main()
