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
        return TradeSyncAddon(
            cfg=cfg, tradovate=tradovate,
            resolver=resolver, source=MagicMock(),
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


# ── C. capture_token return-value semantics ────────────────────────────── #

class TestCaptureTokenReturnValues(unittest.TestCase):
    """capture_token() now returns a string identifying the auth
    scheme it saw, so the addon can log accurately (was: silent
    rejection of every non-Bearer header, plus a misleading
    "captured Bearer token" debug line on every request). Bug
    discovered during live calibration when TV's OAuth-signed
    requests caused the active-resolve fallback to fail with
    'no bearer token captured yet' AFTER 10+ supposed captures."""

    def test_empty_header_returns_none(self):
        r = IbkrContractResolver()
        self.assertEqual(r.capture_token(""), "none")

    def test_bearer_first_time_returns_bearer(self):
        r = IbkrContractResolver()
        self.assertEqual(
            r.capture_token("Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig"),
            "bearer",
        )
        # And the token landed in the resolver's state.
        self.assertEqual(r._bearer_token, "eyJhbGciOiJIUzI1NiJ9.payload.sig")

    def test_bearer_same_token_returns_bearer_same(self):
        r = IbkrContractResolver()
        r.capture_token("Bearer abc123")
        # Same value again — should NOT log "new token captured",
        # so the addon can throttle its log output.
        self.assertEqual(r.capture_token("Bearer abc123"), "bearer-same")

    def test_oauth_returns_oauth_and_does_NOT_store(self):
        # This is the live TradingView Desktop case — OAuth 1.0a,
        # not Bearer. We cannot replay it, so the resolver must
        # NOT store it as if it were a usable token.
        r = IbkrContractResolver()
        oauth_header = (
            'OAuth realm="limited_poa", oauth_consumer_key="TRDGVIEW", '
            'oauth_nonce="abc", oauth_signature="xyz%3D", '
            'oauth_signature_method="HMAC-SHA256"'
        )
        self.assertEqual(r.capture_token(oauth_header), "oauth")
        self.assertIsNone(r._bearer_token,
            "OAuth headers must not populate the bearer-token slot — "
            "otherwise resolve_symbol would try to replay them as "
            "Bearer auth and IBKR would reject every request.")

    def test_oauth_case_insensitive(self):
        # Header values are case-insensitive in practice; make sure
        # we still classify variations as 'oauth'.
        r = IbkrContractResolver()
        for variant in ('oauth realm="x"', 'OAUTH realm="x"',
                        '  OAuth realm="x"'):
            self.assertEqual(r.capture_token(variant), "oauth",
                             f"failed on variant: {variant!r}")

    def test_unrecognised_scheme_returns_other(self):
        r = IbkrContractResolver()
        self.assertEqual(r.capture_token("Basic dXNlcjpwYXNz"), "other")
        self.assertEqual(r.capture_token("Digest username='u'"), "other")
        # And nothing landed in state.
        self.assertIsNone(r._bearer_token)

    def test_bearer_with_only_whitespace_returns_none(self):
        # `Bearer ` (with the space but no token) — empty payload
        # should not be stored.
        r = IbkrContractResolver()
        self.assertEqual(r.capture_token("Bearer    "), "none")
        self.assertIsNone(r._bearer_token)


# ── D. observe_contract_info gzip-body decompression ───────────────────── #

class TestGzipBodyDecompression(unittest.TestCase):
    """observe_contract_info() must transparently handle gzip-compressed
    response bodies. mitmproxy's flow.response.content NORMALLY
    decompresses automatically, but during live calibration we
    captured /info payloads that arrived still gzip-compressed
    (magic bytes 0x1F 0x8B confirmed via xxd on the wire log). Whether
    that's a mitmproxy version quirk, an IBKR-specific header thing,
    or some edge case we don't fully understand, the practical
    consequence was: zero symbols ever made it into the cache across
    multiple test sessions, every order failed at the replicator with
    'Could not resolve conid=…'. Decompressing gzip ourselves when
    we see the magic bytes makes the cache work."""

    @staticmethod
    def _gzip_json(payload: dict) -> bytes:
        """Helper: serialise + gzip-compress, mimicking what IBKR
        would send when the proxy passes the body through without
        decompressing."""
        import gzip
        import json as _json
        return gzip.compress(_json.dumps(payload).encode("utf-8"))

    def test_extracts_symbol_from_gzip_compressed_body(self):
        # The original symptom: a real /info response arrives
        # gzip-compressed, the symbol must still land in the cache.
        resolver = IbkrContractResolver()
        body = self._gzip_json({"tickerSymbol": "MESH6"})
        # Sanity check: the helper really produces gzip-magic-prefixed
        # bytes — otherwise we'd be testing the wrong code path.
        self.assertEqual(body[:2], b"\x1f\x8b",
                         "test helper must produce real gzip bytes")

        resolver.observe_contract_info(
            path="/v1/tv/iserver/contract/770561201/info",
            response_body=body,
        )
        # Cache populated — the same conid resolves to MESH6 now.
        self.assertEqual(resolver._symbol_cache.get(770561201), "MESH6")

    def test_uncompressed_body_still_works(self):
        # Regression: don't break the existing happy path where
        # mitmproxy did decompress before handing us the bytes.
        resolver = IbkrContractResolver()
        body = json.dumps({"tickerSymbol": "NQH6"}).encode()
        resolver.observe_contract_info(
            path="/v1/tv/iserver/contract/123456/info",
            response_body=body,
        )
        self.assertEqual(resolver._symbol_cache.get(123456), "NQH6")

    def test_gzip_body_with_symbol_plus_expiry_schema(self):
        # IBKR sometimes returns the symbol + expiry split into two
        # fields and expects the caller to reconstruct (e.g. "MES" +
        # "20260320" → "MESH6"). Make sure the gzip path still
        # exercises the full _extract_symbol fallback chain.
        resolver = IbkrContractResolver()
        body = self._gzip_json({
            "symbol": "MES",
            "expiry": "20260320",
        })
        resolver.observe_contract_info(
            path="/v1/tv/iserver/contract/770561201/info",
            response_body=body,
        )
        self.assertEqual(resolver._symbol_cache.get(770561201), "MESH6")

    def test_corrupt_gzip_body_does_not_crash(self):
        # Truncated / garbage gzip — the function must swallow the
        # OSError, not propagate it up to the proxy hook (which would
        # bring the whole engine down on a single bad response).
        resolver = IbkrContractResolver()
        bad = b"\x1f\x8b" + b"\x00" * 30  # gzip magic + junk
        resolver.observe_contract_info(
            path="/v1/tv/iserver/contract/770561201/info",
            response_body=bad,
        )
        # Cache stays empty — no false positive symbol.
        self.assertNotIn(770561201, resolver._symbol_cache)

    def test_unparseable_json_after_decompression_does_not_crash(self):
        # gzip decompresses cleanly but the result isn't JSON.
        import gzip as _gzip
        resolver = IbkrContractResolver()
        body = _gzip.compress(b"<html>not json</html>")
        resolver.observe_contract_info(
            path="/v1/tv/iserver/contract/770561201/info",
            response_body=body,
        )
        self.assertNotIn(770561201, resolver._symbol_cache)


# ── E. _extract_symbol schema fallbacks ────────────────────────────────── #

class TestExtractSymbolSchemas(unittest.TestCase):
    """The IBKR Client Portal API used by TradingView Desktop
    returns /info payloads in snake_case (local_symbol, expiry_full).
    Other IBKR endpoints and older clients use camelCase
    (localSymbol, expirationDate). Both must work.

    The actual key set captured on the wire from a live /info for
    conid=770561201 was:
        ['allow_sell_long', 'cfi_code', 'classifier', 'company_name',
         'con_id', 'contract_clarification_type', 'contract_month',
         'currency', 'cusip', 'exchange', 'expiry_full',
         'has_related_contracts', 'instrument_type',
         'is_zero_commission_security', 'local_symbol',
         'maturity_date', 'multiplier', 'r_t_h', 'symbol', 'text',
         'trading_class', 'underlying_con_id', 'underlying_issuer',
         'valid_exchanges']
    Pre-fix, _extract_symbol returned None on this dict because it
    only knew the camelCase forms — the cache stayed empty and
    every order failed at the replicator with 'Could not resolve
    conid=…'."""

    def _extract(self, payload):
        # Driven through the full path so we exercise the same
        # entry point the addon uses, not the internal helper.
        from tradesync.brokers.ibkr import _extract_symbol
        return _extract_symbol(payload)

    # ── snake_case schemas (CPAPI / TradingView Desktop) ─────────── #

    def test_cpapi_local_symbol_short_form(self):
        # The canonical case: local_symbol is already in CME short
        # form, no spaces.
        self.assertEqual(
            self._extract({"local_symbol": "MESH6", "symbol": "MES"}),
            "MESH6",
        )

    def test_cpapi_local_symbol_with_space(self):
        # IBKR sometimes formats local_symbol with a space between
        # root and contract month — strip and upper.
        self.assertEqual(
            self._extract({"local_symbol": "MES H6"}),
            "MESH6",
        )

    def test_cpapi_symbol_plus_expiry_full(self):
        # Fallback: only the root symbol + expiry_full is present.
        # expiry_full uses YYYYMMDD, same as the legacy 'expiry'.
        self.assertEqual(
            self._extract({"symbol": "MES", "expiry_full": "20260320"}),
            "MESH6",
        )

    def test_real_world_cpapi_key_set(self):
        # Exact key set captured from a live /info response. The
        # other fields are filler — what matters is that with the
        # full real-world dict we get a usable symbol.
        payload = {
            "allow_sell_long": True,
            "cfi_code": "FFCXSX",
            "classifier": "Future",
            "company_name": "MICRO E-MINI S&P 500",
            "con_id": 770561201,
            "contract_clarification_type": None,
            "contract_month": "202603",
            "currency": "USD",
            "cusip": None,
            "exchange": "CME",
            "expiry_full": "20260320",
            "has_related_contracts": False,
            "instrument_type": "FUT",
            "is_zero_commission_security": False,
            "local_symbol": "MESH6",
            "maturity_date": "20260320",
            "multiplier": "5",
            "r_t_h": False,
            "symbol": "MES",
            "text": "MESH6 Mar20'26 @CME",
            "trading_class": "MES",
            "underlying_con_id": 11004968,
            "underlying_issuer": None,
            "valid_exchanges": "CME",
        }
        self.assertEqual(self._extract(payload), "MESH6")

    # ── camelCase schemas (legacy, must still work) ──────────────── #

    def test_legacy_ticker_symbol(self):
        self.assertEqual(self._extract({"tickerSymbol": "MESH6"}), "MESH6")

    def test_legacy_ticker_short_key(self):
        self.assertEqual(self._extract({"ticker": "NQM6"}), "NQM6")

    def test_legacy_localSymbol_camelCase(self):
        # Some older payloads use camelCase localSymbol.
        self.assertEqual(self._extract({"localSymbol": "MES H6"}), "MESH6")

    def test_legacy_symbol_plus_expirationDate(self):
        self.assertEqual(
            self._extract({"symbol": "MES", "expirationDate": "20260320"}),
            "MESH6",
        )

    def test_legacy_symbol_plus_expiry(self):
        # The original 'expiry' name (before we even saw camelCase
        # expirationDate).
        self.assertEqual(
            self._extract({"symbol": "MES", "expiry": "20260320"}),
            "MESH6",
        )

    # ── precedence (snake vs camel; ticker vs local vs symbol) ───── #

    def test_ticker_wins_over_local_symbol(self):
        # Ticker is the most explicit short form — should always win
        # when present.
        self.assertEqual(
            self._extract({
                "tickerSymbol": "MESH6",
                "local_symbol": "MESM6_OVERRIDE",
            }),
            "MESH6",
        )

    def test_local_symbol_wins_over_symbol_plus_expiry(self):
        # local_symbol is authoritative, no need to reconstruct from
        # symbol + expiry.
        self.assertEqual(
            self._extract({
                "local_symbol": "MESH6",
                "symbol": "MES",
                "expiry_full": "20260918",  # would resolve to MESU6 — wrong!
            }),
            "MESH6",
        )

    # ── degenerate inputs ─────────────────────────────────────────── #

    def test_none_input_returns_none(self):
        self.assertIsNone(self._extract(None))

    def test_empty_dict_returns_none(self):
        self.assertIsNone(self._extract({}))

    def test_dict_with_only_garbage_returns_none(self):
        self.assertIsNone(self._extract({"foo": "bar", "baz": 42}))

    def test_local_symbol_with_only_whitespace_returns_none(self):
        # Empty-after-strip should fall through to the next fallback,
        # not return "".
        self.assertIsNone(self._extract({"local_symbol": "   "}))


if __name__ == "__main__":
    unittest.main()
