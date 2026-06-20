"""
Tests for TradovateClient.get_contract_name — the contractId → symbol
reverse lookup the WS observer needs (order frames carry only a numeric
contractId). Validated live against 4327110 → "MNQM6"; these tests
exercise the HTTP path + caching with a mocked session.
"""

import unittest
from unittest.mock import MagicMock, patch

from tradesync.brokers.tradovate import TradovateClient, TradovateOrderError


def _live_client():
    # Non-empty creds so the client is NOT in shadow mode.
    c = TradovateClient(
        api_url="https://demo.tradovateapi.com/v1",
        username="u", password="p", app_id="a", app_version="1.0",
        cid="cid", sec="sec")
    c._access_token = "tok"
    return c


class TestGetContractName(unittest.TestCase):

    def test_resolves_and_caches(self):
        c = _live_client()
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"id": 4327110, "name": "MNQM6"}
        with patch.object(c, "_ensure_fresh_token"), \
             patch.object(c._http, "get", return_value=resp) as mock_get:
            self.assertEqual(c.get_contract_name(4327110), "MNQM6")
            # Second call hits the cache — no further HTTP.
            self.assertEqual(c.get_contract_name(4327110), "MNQM6")
            self.assertEqual(mock_get.call_count, 1)

    def test_populates_reverse_cache_too(self):
        c = _live_client()
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"id": 4327110, "name": "MNQM6"}
        with patch.object(c, "_ensure_fresh_token"), \
             patch.object(c._http, "get", return_value=resp):
            c.get_contract_name(4327110)
        # The forward cache is now warm: get_contract_id("MNQM6") must
        # not need another HTTP call.
        with patch.object(c, "_ensure_fresh_token"), \
             patch.object(c._http, "get") as mock_get:
            self.assertEqual(c.get_contract_id("MNQM6"), 4327110)
            mock_get.assert_not_called()

    def test_http_error_raises(self):
        c = _live_client()
        resp = MagicMock(status_code=404, text="not found")
        with patch.object(c, "_ensure_fresh_token"), \
             patch.object(c._http, "get", return_value=resp):
            with self.assertRaises(TradovateOrderError):
                c.get_contract_name(999999)

    def test_missing_name_raises(self):
        c = _live_client()
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"id": 4327110}   # no "name"
        with patch.object(c, "_ensure_fresh_token"), \
             patch.object(c._http, "get", return_value=resp):
            with self.assertRaises(TradovateOrderError):
                c.get_contract_name(4327110)

    def test_shadow_mode_returns_placeholder(self):
        # Empty creds → shadow mode → no HTTP, deterministic placeholder.
        c = TradovateClient(
            api_url="https://demo.tradovateapi.com/v1",
            username="", password="", app_id="", app_version="",
            cid="", sec="")
        name = c.get_contract_name(4327110)
        self.assertIn("4327110", name)


if __name__ == "__main__":
    unittest.main()
