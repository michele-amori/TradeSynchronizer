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
        r = self.c.modify_order(9_000_123, order_type="Limit", qty=2)
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

    def test_device_id_defaults_to_empty(self):
        # Stable device_id is optional — when not provided, the empty
        # string signals "let TradovateClient generate a fresh uuid".
        self.assertEqual(self._cfg().tradovate_device_id, "")

    def test_device_id_is_preserved_when_set(self):
        # Stable device id from .env.<env> (TRADOVATE_DEVICE_ID) must
        # round-trip through Config so main.py can pass it to
        # TradovateClient — otherwise the client falls back to a
        # fresh uuid every restart, which Tradovate's anti-fraud
        # heuristics can interpret as "new device, please re-MFA".
        uid = "12345678-1234-1234-1234-123456789abc"
        self.assertEqual(
            self._cfg(tradovate_device_id=uid).tradovate_device_id,
            uid,
        )


class TestNonNumericAccountIdHandling(unittest.TestCase):
    """A user filling TRADOVATE_ACCOUNT_ID with a prop-firm nickname
    (e.g. 'BGF46274' instead of 12345678) used to crash Config.load()
    with a bare ValueError before the engine could even reach its
    log setup. This regression test guarantees the resilient parse.

    In shadow mode it's logged + dropped silently (None), so the user
    can still validate IBKR interception without sorting out the
    numeric id first. Out of shadow it raises a clear RuntimeError
    that points at the GUI's 'Sign in & pick account' workflow."""

    def _load(self, env_overrides):
        """Run Config.load() with a controlled environment. Builds
        a tmp project root with empty .env.demo so dotenv finds
        the env file but doesn't override anything."""
        import os, tempfile
        from pathlib import Path
        from unittest.mock import patch
        from tradesync.config import Config

        base_env = {
            "TRADOVATE_ENVIRONMENT": "demo",
            "PROXY_LISTEN_PORT": "8081",
        }
        base_env.update(env_overrides)
        # Wipe the real env's account-id so it doesn't leak through.
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, ".env.demo").write_text("")
            with patch.dict(os.environ, base_env, clear=False), \
                 patch("tradesync.config.PROJECT_ROOT", Path(tmp)):
                return Config.load()

    def test_non_numeric_acct_id_silently_dropped_in_shadow(self):
        # Empty user creds → shadow mode
        cfg = self._load({
            "TRADOVATE_USERNAME": "",
            "TRADOVATE_PASSWORD": "",
            "TRADOVATE_ACCOUNT_ID": "BGF46274",
        })
        self.assertTrue(cfg.is_shadow_mode)
        # The bad value got dropped, not propagated
        self.assertIsNone(cfg.tradovate_acct_id)

    def test_numeric_acct_id_works_in_shadow(self):
        cfg = self._load({
            "TRADOVATE_USERNAME": "",
            "TRADOVATE_PASSWORD": "",
            "TRADOVATE_ACCOUNT_ID": "12345678",
        })
        self.assertTrue(cfg.is_shadow_mode)
        self.assertEqual(cfg.tradovate_acct_id, 12345678)

    def test_empty_acct_id_works_in_shadow(self):
        cfg = self._load({
            "TRADOVATE_USERNAME": "",
            "TRADOVATE_PASSWORD": "",
            "TRADOVATE_ACCOUNT_ID": "",
        })
        self.assertIsNone(cfg.tradovate_acct_id)


# ── _resolve_pinned_account: accept id OR name ─────────────────────────── #

class TestResolvePinnedAccount(unittest.TestCase):
    """connect() now translates a user-supplied TRADOVATE_ACCOUNT_ID
    to Tradovate's internal numeric id. The user can supply either:
      * the real id  (the unambiguous primary key in /account/list)
      * the human-readable name shown in the Tradovate UI (typically
        a prop-firm-assigned account number — e.g. Apex / TopStep
        ids the user sees on tradovate.com)

    Pre-fix, the pin was assigned to self._account_id without any
    /account/list validation. Engines started cleanly with the WRONG
    pin (the name) but every placeOrder failed at runtime because
    Tradovate's placeOrder endpoint needs the numeric id, not the
    name. This class guards against re-introducing that foot-gun."""

    def _client(self):
        # Build a client with empty credentials → shadow mode True.
        # We don't go through connect(); we exercise the helper
        # directly with synthetic /account/list payloads.
        return TradovateClient(
            api_url="https://demo.tradovateapi.com/v1",
            username="u", password="p", app_id="x",
            app_version="1.0", cid="", sec="",
        )

    # A representative /account/list response. id != name on purpose,
    # mirroring the real shape captured during calibration: internal
    # primary key id=49000001 / human-readable account number "19000001".
    _ACCOUNTS = [
        {"id": 49000001, "name": "19000001", "accountType": "Customer",
         "active": True, "userId": 3701228},
    ]

    def test_pin_matches_internal_id(self):
        c = self._client()
        self.assertEqual(
            c._resolve_pinned_account(self._ACCOUNTS, pin=49000001),
            49000001,
        )

    def test_pin_matches_human_readable_name(self):
        # The realistic foot-gun case: the user pasted the value
        # they see in the Tradovate UI (19000001 = the `name` field).
        # The resolver must translate it to the internal id (49000001).
        c = self._client()
        self.assertEqual(
            c._resolve_pinned_account(self._ACCOUNTS, pin=19000001),
            49000001,
        )

    def test_id_match_wins_over_name_match(self):
        # Synthetic but possible: account A's id equals account B's
        # name (as int). The id match should win — it's unambiguous
        # and is the field placeOrder actually consumes.
        accounts = [
            {"id": 111, "name": "999"},
            {"id": 999, "name": "19000001"},
        ]
        c = self._client()
        self.assertEqual(
            c._resolve_pinned_account(accounts, pin=999),
            999,
        )

    def test_unknown_pin_raises_with_diagnostic(self):
        from tradesync.brokers.tradovate import TradovateAuthError
        c = self._client()
        with self.assertRaises(TradovateAuthError) as ctx:
            c._resolve_pinned_account(self._ACCOUNTS, pin=9999999)
        msg = str(ctx.exception)
        self.assertIn("9999999", msg)
        self.assertIn("49000001", msg)
        self.assertIn("19000001", msg)
        self.assertIn("TRADOVATE_ACCOUNT_ID", msg)

    def test_empty_account_list_raises(self):
        from tradesync.brokers.tradovate import TradovateAuthError
        c = self._client()
        with self.assertRaises(TradovateAuthError):
            c._resolve_pinned_account([], pin=49000001)

    def test_account_with_non_integer_id_is_skipped(self):
        accounts = [
            {"id": "bad", "name": "19000001"},
            {"id": 49000001, "name": "999"},
        ]
        c = self._client()
        self.assertEqual(
            c._resolve_pinned_account(accounts, pin=49000001),
            49000001,
        )


if __name__ == "__main__":
    unittest.main()
