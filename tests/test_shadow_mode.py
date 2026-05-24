"""
Tests for shadow mode: the engine boots and intercepts IBKR orders
even when no Tradovate credentials are configured, logging what
WOULD have been sent to Tradovate without actually placing orders.

This is the calibration path the user wants BEFORE registering an
app at trader.tradovate.com — they want to validate the IBKR-side
interception with real TradingView Desktop traffic first.

Run from the repo root:

    python3 -m unittest tests.test_shadow_mode
"""

from __future__ import annotations

import unittest

from tradesync.brokers.tradovate import (
    TradovateClient, PlacedOrder, PlacedBracket,
)


def _make_client(*, cid="real_cid", sec="real_sec",
                 username="user", password="pw"):
    """Build a TradovateClient with the credential subset under test.
    Empty fields trigger shadow mode."""
    return TradovateClient(
        api_url="https://demo.tradovateapi.com/v1",
        username=username,
        password=password,
        app_id="TradeSynchronizer",
        app_version="1.0",
        cid=cid,
        sec=sec,
    )


class TestShadowModeDetection(unittest.TestCase):
    """The _shadow_mode flag is set in __init__ based on credential
    completeness — any missing field flips it to True."""

    def test_all_creds_present_means_NOT_shadow(self):
        c = _make_client()
        self.assertFalse(c._shadow_mode)

    def test_missing_cid_triggers_shadow(self):
        c = _make_client(cid="")
        self.assertTrue(c._shadow_mode)

    def test_missing_sec_triggers_shadow(self):
        c = _make_client(sec="")
        self.assertTrue(c._shadow_mode)

    def test_missing_username_triggers_shadow(self):
        c = _make_client(username="")
        self.assertTrue(c._shadow_mode)

    def test_missing_password_triggers_shadow(self):
        c = _make_client(password="")
        self.assertTrue(c._shadow_mode)


class TestShadowModeConnect(unittest.TestCase):
    """connect() must NOT make HTTP calls in shadow mode."""

    def test_connect_does_not_raise_in_shadow(self):
        c = _make_client(cid="", sec="")
        c.connect()    # must not raise even though no Tradovate is reachable

    def test_connect_marks_as_connected_in_shadow(self):
        c = _make_client(cid="")
        c.connect()
        self.assertTrue(c.connected,
                        "shadow client should report connected=True so "
                        "downstream code doesn't special-case it")

    def test_connect_uses_pinned_account_id_in_shadow(self):
        c = TradovateClient(
            api_url="https://x", username="", password="", app_id="X",
            app_version="1.0", cid="", sec="",
            pinned_account_id=42,
        )
        c.connect()
        self.assertEqual(c.account_id, 42)


class TestShadowModeOperations(unittest.TestCase):
    """Each HTTP-bound method returns a plausible fake instead of
    making a network call."""

    def setUp(self):
        self.c = _make_client(cid="", sec="")
        self.c.connect()

    def test_get_contract_id_returns_fake_and_caches(self):
        cid1 = self.c.get_contract_id("MESH6")
        self.assertIsInstance(cid1, int)
        self.assertGreaterEqual(cid1, 9_000_000)

        # Cached: second call returns the same fake id
        cid2 = self.c.get_contract_id("MESH6")
        self.assertEqual(cid1, cid2)

    def test_place_order_returns_placedorder_with_fake_id(self):
        result = self.c.place_order(
            tradovate_symbol="MESH6", contract_id=12345,
            action="Buy", qty=1, order_type="Limit",
            limit_price=4000.0,
        )
        self.assertIsInstance(result, PlacedOrder)
        self.assertGreaterEqual(result.order_id, 9_000_000)
        # The "raw" field carries a 'shadow' marker so downstream
        # code can tell this wasn't a real placement.
        self.assertTrue(result.raw.get("shadow"))
        # And the payload we WOULD have sent is preserved for review
        self.assertIn("would_have_sent", result.raw)

    def test_place_bracket_returns_placedbracket_with_fake_ids(self):
        result = self.c.place_bracket(
            tradovate_symbol="MESH6", contract_id=12345,
            entry_action="Buy", entry_qty=1, entry_order_type="Limit",
            entry_limit_price=4000.0,
            brackets=[
                {"action": "Sell", "order_type": "Limit",
                 "limit_price": 4010.0},
                {"action": "Sell", "order_type": "Stop",
                 "stop_price": 3990.0},
            ],
        )
        self.assertIsInstance(result, PlacedBracket)
        self.assertGreaterEqual(result.entry_order_id, 9_000_000)
        self.assertEqual(len(result.bracket_ids), 2)
        for tv_id in result.bracket_ids:
            self.assertGreaterEqual(tv_id, 9_000_000)

    def test_cancel_order_returns_ok_marker(self):
        r = self.c.cancel_order(9_000_123)
        self.assertTrue(r.get("shadow"))
        self.assertTrue(r.get("ok"))

    def test_modify_order_returns_ok_marker(self):
        r = self.c.modify_order(9_000_123, qty=2)
        self.assertTrue(r.get("shadow"))
        self.assertTrue(r.get("ok"))

    def test_get_order_status_returns_working(self):
        # In shadow mode the engine never reconciles, but get_order_status
        # is part of the public API — it should answer something safe.
        s = self.c.get_order_status(9_000_123)
        self.assertEqual(s, "Working")

    def test_list_accounts_returns_empty(self):
        accs = self.c.list_accounts()
        self.assertEqual(accs, [])


class TestShadowFakeIdsAreMonotonic(unittest.TestCase):
    """Multiple shadow calls produce DISTINCT, monotonic fake ids —
    so two different orders don't collide in the OrderMap or in logs."""

    def test_distinct_ids_across_calls(self):
        c = _make_client(cid="")
        c.connect()

        ids = set()
        for sym in ("MESH6", "MNQU6", "ESM6"):
            ids.add(c.get_contract_id(sym))
        # Three lookups → three distinct ids
        self.assertEqual(len(ids), 3)

        # Each well above the 9_000_000 sentinel
        for tv_id in ids:
            self.assertGreaterEqual(tv_id, 9_000_000)


class TestConfigShadowProperty(unittest.TestCase):
    """The Config dataclass exposes is_shadow_mode so callers can
    check the state without poking the TradovateClient."""

    def _cfg(self, **kw) -> "Config":
        from tradesync.config import Config
        defaults = dict(
            tradovate_username="u", tradovate_password="p",
            tradovate_app_id="x", tradovate_app_ver="1",
            tradovate_cid="c", tradovate_sec="s",
            tradovate_env="demo", tradovate_acct_id=None,
            proxy_host="127.0.0.1", proxy_port=8081,
            replication_mode="mirror", skip_protective_stops=True,
            ibkr_watched_accounts=[],
        )
        defaults.update(kw)
        return Config(**defaults)

    def test_all_set_is_not_shadow(self):
        self.assertFalse(self._cfg().is_shadow_mode)

    def test_missing_any_credential_is_shadow(self):
        self.assertTrue(self._cfg(tradovate_cid="").is_shadow_mode)
        self.assertTrue(self._cfg(tradovate_sec="").is_shadow_mode)
        self.assertTrue(self._cfg(tradovate_username="").is_shadow_mode)
        self.assertTrue(self._cfg(tradovate_password="").is_shadow_mode)


if __name__ == "__main__":
    unittest.main()
