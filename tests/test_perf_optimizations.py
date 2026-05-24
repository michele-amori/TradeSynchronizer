"""
Tests for the order-replication latency optimizations:

  * IbkrContractResolver.observe_contract_info() fires its
    on_new_symbol callback exactly once per NEW conid, and never
    for conids it has already cached. This is what powers the
    background pre-resolution of the Tradovate contract_id in
    TradeSyncAddon._pre_resolve_tradovate_contract — saving the
    /contract/find round-trip (50-150ms) from the first-order
    critical path on a previously unseen symbol.

  * TradeSyncAddon's response hook actually wires the resolver's
    new-symbol notification through to the Tradovate pre-resolve
    side-effect (a daemon thread that calls
    tradovate.get_contract_id).

Run from the repo root:

    python3 -m unittest tests.test_perf_optimizations
"""

from __future__ import annotations

import json
import time
import unittest
from unittest.mock import MagicMock, patch

from tradesync.brokers.ibkr import IbkrContractResolver


# ── A. observe_contract_info callback semantics ────────────────────────── #

class TestObserveContractInfoCallback(unittest.TestCase):
    """The whole point of the callback is to fire EXACTLY ONCE per
    new conid, so we don't repeatedly slam Tradovate's
    /contract/find for the same symbol while a chart is open."""

    @staticmethod
    def _info_body(symbol="MES") -> bytes:
        # IBKR /info responses use `tickerSymbol` (already in
        # Tradovate-style short form), not a bare `symbol` field —
        # see _extract_symbol() in tradesync/brokers/ibkr.py.
        return json.dumps({"tickerSymbol": symbol}).encode()

    def test_callback_fires_on_new_conid(self):
        resolver = IbkrContractResolver()
        cb = MagicMock()
        resolver.observe_contract_info(
            path="/v1/tv/iserver/contract/845307883/info",
            response_body=self._info_body("MES"),
            on_new_symbol=cb,
        )
        cb.assert_called_once_with("MES")

    def test_callback_does_NOT_fire_on_already_known_conid(self):
        resolver = IbkrContractResolver()
        cb = MagicMock()

        # First observation: callback fires
        resolver.observe_contract_info(
            path="/v1/tv/iserver/contract/845307883/info",
            response_body=self._info_body("MES"),
            on_new_symbol=cb,
        )
        # Second observation of the SAME conid: callback must not fire again.
        resolver.observe_contract_info(
            path="/v1/tv/iserver/contract/845307883/info",
            response_body=self._info_body("MES"),
            on_new_symbol=cb,
        )
        self.assertEqual(cb.call_count, 1,
                         "callback fired more than once for the same conid")

    def test_callback_fires_independently_per_conid(self):
        resolver = IbkrContractResolver()
        cb = MagicMock()
        resolver.observe_contract_info(
            "/v1/tv/iserver/contract/100/info", self._info_body("MES"),
            on_new_symbol=cb,
        )
        resolver.observe_contract_info(
            "/v1/tv/iserver/contract/200/info", self._info_body("MNQ"),
            on_new_symbol=cb,
        )
        self.assertEqual(cb.call_count, 2)
        self.assertEqual([c.args[0] for c in cb.call_args_list], ["MES", "MNQ"])

    def test_callback_optional_no_crash_when_absent(self):
        """Backward compatibility: existing callers don't pass the
        callback. observe_contract_info must keep working."""
        resolver = IbkrContractResolver()
        # No on_new_symbol kw — must not raise.
        resolver.observe_contract_info(
            "/v1/tv/iserver/contract/300/info", self._info_body("ES"),
        )
        # And the symbol still landed in the cache.
        self.assertEqual(resolver._symbol_cache[300], "ES")

    def test_callback_exception_is_swallowed(self):
        """A buggy callback must not break the response hook —
        we still want the symbol cached even if the side-effect
        fails."""
        resolver = IbkrContractResolver()
        def boom(_):
            raise RuntimeError("callback bug")
        resolver.observe_contract_info(
            "/v1/tv/iserver/contract/400/info", self._info_body("YM"),
            on_new_symbol=boom,
        )
        # Cache populated despite the callback raising.
        self.assertEqual(resolver._symbol_cache[400], "YM")

    def test_no_callback_when_response_unparseable(self):
        resolver = IbkrContractResolver()
        cb = MagicMock()
        resolver.observe_contract_info(
            "/v1/tv/iserver/contract/500/info",
            b"not json",
            on_new_symbol=cb,
        )
        cb.assert_not_called()

    def test_no_callback_when_path_not_info(self):
        resolver = IbkrContractResolver()
        cb = MagicMock()
        resolver.observe_contract_info(
            "/v1/tv/iserver/account/U1234567/orders",
            self._info_body("MES"),
            on_new_symbol=cb,
        )
        cb.assert_not_called()


# ── B. Addon pre-resolves Tradovate contract_id in the background ───── #

class TestAddonPreResolve(unittest.TestCase):
    """Integration: when the addon's response hook sees a new conid,
    it should kick off a background daemon thread that calls
    tradovate.get_contract_id(tradovate_symbol) so the cache is
    warm before the first order arrives."""

    def _build_addon_with_mock_tradovate(self):
        from tradesync.proxy.addon import TradeSyncAddon
        cfg = MagicMock()
        cfg.ibkr_watched_accounts = []
        cfg.tradovate_env = "demo"
        tradovate = MagicMock()
        # Explicitly OFF — pre-resolve no-ops in shadow mode, and an
        # un-configured MagicMock attribute would otherwise be truthy
        # and skip the call we're trying to verify.
        tradovate._shadow_mode = False
        resolver = IbkrContractResolver()
        replicator = MagicMock()
        return TradeSyncAddon(
            cfg=cfg, tradovate=tradovate,
            resolver=resolver, replicator=replicator,
        ), tradovate, resolver

    def test_pre_resolve_calls_get_contract_id_in_background(self):
        addon, tv, _resolver = self._build_addon_with_mock_tradovate()

        # The addon's _pre_resolve_tradovate_contract spawns a daemon
        # thread; we let it run and poll briefly.
        addon._pre_resolve_tradovate_contract("MESH2026")

        # Wait up to 500ms for the background call to land.
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            if tv.get_contract_id.called:
                break
            time.sleep(0.01)

        tv.get_contract_id.assert_called_once_with("MESH6")  # converted form

    def test_pre_resolve_swallows_tradovate_errors(self):
        """The pre-resolve is best-effort. If Tradovate fails (network
        glitch, unknown symbol etc.) the daemon thread must not
        propagate the exception or take the engine down."""
        from tradesync.brokers.tradovate import TradovateOrderError
        addon, tv, _resolver = self._build_addon_with_mock_tradovate()
        tv.get_contract_id.side_effect = TradovateOrderError("nope")

        # Must not raise even though the worker will hit the exception
        addon._pre_resolve_tradovate_contract("BAD")

        # Give the background thread time to fail silently
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            if tv.get_contract_id.called:
                break
            time.sleep(0.01)
        self.assertTrue(tv.get_contract_id.called)
        # Test passes simply by reaching this line without an exception.


if __name__ == "__main__":
    unittest.main()
