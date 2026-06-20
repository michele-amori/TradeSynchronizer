"""
Tests for tradesync.proxy.traffic_logger.

The TrafficLoggerAddon is the "calibration mode" packet sniffer:
it logs every HTTP transaction passing through mitmproxy in three
tiers (full for IBKR, summary for TV-own hosts, skipped for noise).

Run from the repo root:

    python3 -m unittest tests.test_traffic_logger
"""

from __future__ import annotations

import logging
import unittest
from unittest.mock import MagicMock

from tradesync.proxy.traffic_logger import (
    TrafficLoggerAddon,
    _classify_host,
    _decode_body,
)


# ── helpers ─────────────────────────────────────────────────────────────── #

class _LogCapture:
    """Capture records from the tradesync.traffic logger."""
    def __enter__(self):
        self._records: list[logging.LogRecord] = []
        self._handler = logging.Handler()
        self._handler.emit = self._records.append
        self._handler.setLevel(logging.DEBUG)
        self._log = logging.getLogger("tradesync.traffic")
        self._old = self._log.level
        self._log.setLevel(logging.DEBUG)
        self._log.addHandler(self._handler)
        return self

    def __exit__(self, *exc):
        self._log.removeHandler(self._handler)
        self._log.setLevel(self._old)

    def messages(self) -> list[str]:
        return [r.getMessage() for r in self._records]


def _make_flow(*, host="api.ibkr.com", method="POST",
               url="https://api.ibkr.com/v1/api/iserver/account/U1/orders",
               request_body=b"", request_ct="application/json",
               response_status=200, response_body=b"",
               response_ct="application/json", flow_id="abcdef1234"):
    """Build a minimal mock of a mitmproxy HTTPFlow good enough to
    drive the addon's hooks."""
    flow = MagicMock()
    flow.id = flow_id
    flow.request = MagicMock()
    flow.request.pretty_host = host
    flow.request.pretty_url = url
    flow.request.method = method
    flow.request.raw_content = request_body
    flow.request.headers = {"content-type": request_ct}
    flow.response = MagicMock()
    flow.response.status_code = response_status
    flow.response.raw_content = response_body
    flow.response.headers = {"content-type": response_ct}
    flow.error = None
    return flow


# ── host classification ─────────────────────────────────────────────────── #

class TestClassifyHost(unittest.TestCase):

    def test_ibkr_hosts_classified_ibkr(self):
        self.assertEqual(_classify_host("api.ibkr.com"), "ibkr")
        self.assertEqual(_classify_host("www.interactivebrokers.com"), "ibkr")
        # Case-insensitive match
        self.assertEqual(_classify_host("API.IBKR.COM"), "ibkr")

    def test_skip_hosts(self):
        self.assertEqual(_classify_host("telemetry.tradingview.com"), "skip")
        self.assertEqual(_classify_host("analytics.example.com"), "skip")
        self.assertEqual(_classify_host("sentry.io"), "skip")

    def test_tv_hosts_default_to_tv_summary(self):
        self.assertEqual(_classify_host("tradingview.com"), "tv")
        self.assertEqual(_classify_host("prodata.tradingview.com"), "tv")
        # Unknown hosts fall through to tv-summary tier so we still
        # see them in the log; calibration mode is "log everything,
        # judge later".
        self.assertEqual(_classify_host("some.other.host.com"), "tv")


# ── body decoding ───────────────────────────────────────────────────────── #

class TestDecodeBody(unittest.TestCase):

    def test_empty(self):
        self.assertEqual(_decode_body(b"", "application/json"), "(empty)")

    def test_text_body_returned_verbatim(self):
        self.assertEqual(_decode_body(b"hello", "text/plain"), "hello")

    def test_json_body_pretty_printed(self):
        out = _decode_body(b'{"k":"v","x":1}', "application/json")
        # Pretty-printed JSON has newlines & indentation.
        self.assertIn("\n", out)
        self.assertIn('"k": "v"', out)
        self.assertIn('"x": 1', out)

    def test_binary_content_type_not_decoded(self):
        out = _decode_body(b"\x89PNG\r\n", "image/png")
        self.assertIn("binary, not shown", out)
        self.assertIn("image/png", out)

    def test_invalid_utf8_uses_replacement(self):
        # \xff is not valid UTF-8; with errors='replace' it becomes U+FFFD.
        out = _decode_body(b"hello\xff", "text/plain")
        self.assertIn("hello", out)

    def test_oversized_body_truncated(self):
        big = b"x" * (20 * 1024)
        out = _decode_body(big, "text/plain")
        self.assertIn("truncated", out)
        # The first chunk is preserved
        self.assertTrue(out.startswith("x"))


# ── addon end-to-end ────────────────────────────────────────────────────── #

class TestTrafficLoggerAddon(unittest.TestCase):

    def test_ibkr_request_logs_full(self):
        addon = TrafficLoggerAddon(env_label="live")
        body = b'{"orders":[{"side":"BUY","quantity":2,"conid":845307883,"orderType":"LMT","price":21500.0,"cOID":"tv-1"}]}'
        flow = _make_flow(request_body=body)
        with _LogCapture() as cap:
            addon.request(flow)
        msgs = cap.messages()
        self.assertEqual(len(msgs), 1)
        m = msgs[0]
        self.assertIn("TV→", m)
        self.assertIn("IBKR REQUEST", m)
        self.assertIn("LIVE", m)   # env_label uppercased
        self.assertIn("abcdef12", m)   # 8-char flow id
        self.assertIn("BUY", m)        # body content actually dumped

    def test_tv_request_logs_summary_only(self):
        addon = TrafficLoggerAddon(env_label="demo")
        flow = _make_flow(
            host="charts.tradingview.com",
            method="GET",
            url="https://charts.tradingview.com/data/123",
            request_body=b"",
            request_ct="",
        )
        with _LogCapture() as cap:
            addon.request(flow)
        msgs = cap.messages()
        self.assertEqual(len(msgs), 1)
        m = msgs[0]
        # Summary tier — body is NOT in the message
        self.assertIn("TV→", m)
        self.assertIn("DEMO", m)
        self.assertNotIn("IBKR REQUEST", m)
        self.assertNotIn("body:", m)

    def test_telemetry_host_is_silent(self):
        addon = TrafficLoggerAddon()
        flow = _make_flow(host="telemetry.tradingview.com")
        with _LogCapture() as cap:
            addon.request(flow)
            addon.response(flow)
        self.assertEqual(cap.messages(), [])

    def test_ibkr_response_logs_full_status_and_body(self):
        addon = TrafficLoggerAddon(env_label="live")
        flow = _make_flow(
            response_status=400,
            response_body=b'{"error":"insufficient margin","detail":"..."}',
        )
        with _LogCapture() as cap:
            addon.response(flow)
        m = cap.messages()[0]
        self.assertIn("TV←", m)
        self.assertIn("IBKR RESPONSE", m)
        self.assertIn("400", m)
        self.assertIn("insufficient margin", m)

    def test_response_with_none_response_does_not_raise(self):
        addon = TrafficLoggerAddon()
        flow = _make_flow()
        flow.response = None
        with _LogCapture() as cap:
            addon.response(flow)   # must not raise
        self.assertEqual(cap.messages(), [])

    def test_error_hook_logs_warning(self):
        addon = TrafficLoggerAddon(env_label="live")
        flow = _make_flow()
        flow.error = "ConnectionRefusedError"
        with _LogCapture() as cap:
            addon.error(flow)
        msgs = cap.messages()
        self.assertEqual(len(msgs), 1)
        self.assertIn("TV✗", msgs[0])
        self.assertIn("ConnectionRefusedError", msgs[0])


if __name__ == "__main__":
    unittest.main()
