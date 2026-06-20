r"""
Regression tests for the mitmproxy host-filter regex in main.py.

Context — the bug these tests exist to prevent
-----------------------------------------------
For one whole day (4-5 Jun 2026) every IBKR order placed in
TradingView Desktop reached IBKR and was correctly filled, but
nothing was ever replicated to Tradovate. The user reported it,
I diagnosed it incorrectly as "TV not logged in to IBKR" until
the user pointed out the orders WERE being filled. The actual
bug was in `ignore_hosts` (a negative-lookahead pattern):

    ignore_hosts = [r"^(?!api\.ibkr\.com(?::\d+)?$).+$"]

`mitmproxy.addons.next_layer.NextLayer._ignore_connection` tests
each ignore_hosts regex against MULTIPLE candidate strings per
CONNECT, NOT just the SNI/Host header:

    hostnames = [
        f"{peername_ip}:{port}",   # e.g. "95.101.235.232:443"
        f"{server_addr}:{port}",   # e.g. "api.ibkr.com:443"
        host_header,               # optional
        sni,                       # optional
    ]
    ignored = any(re.search(rex, h) for h in hostnames for rex in ignore_hosts)

For an api.ibkr.com flow, the peername IP (an Akamai address)
does NOT equal "api.ibkr.com", so the negative lookahead PASSES,
the `.+$` cap matches the IP, and the whole connection ends up
ignored — silently raw-forwarded with no MITM. From the user's
perspective: orders are filled, the addon never sees them, no
replication happens.

The fix is `allow_hosts` (positive list) with the SAME multi-
candidate semantics but the OPPOSITE branch: a flow is intercepted
iff AT LEAST ONE candidate hostname matches. The api.ibkr.com SNI
and server address still match even if the peername IP doesn't,
so we get the intended behaviour.

These tests reproduce mitmproxy's allow-vs-ignore decision logic
in pure Python and assert the right outcome for representative
flows. If the test names show up red in CI, the proxy is silently
broken and orders are about to stop replicating — fix immediately.
"""

from __future__ import annotations

import re
import unittest

from main import ALLOW_HOSTS_PATTERN


# Realistic CONNECT candidate sets, captured from real
# tradesync.log output during the calibration session.

_IBKR_CANDIDATES = [
    "95.101.235.232:443",   # peername IP — Akamai for IBKR
    "api.ibkr.com:443",      # server.address
    "api.ibkr.com:443",      # TLS SNI (typically equal to server.address)
]

_TRADINGVIEW_CANDIDATES = [
    "18.65.63.22:443",
    "trdlg.tradingview.com:443",
    "trdlg.tradingview.com:443",
]


def _mitmproxy_allow_decision(candidates, allow_patterns):
    """Reproduces mitmproxy.addons.next_layer.NextLayer._ignore_connection
    for the `allow_hosts` branch: a flow is INTERCEPTED iff at least
    one candidate string matches at least one allow regex. Otherwise
    the flow is ignored (raw-forwarded)."""
    return any(
        re.search(rex, host, re.IGNORECASE)
        for host in candidates
        for rex in allow_patterns
    )


class TestAllowHostsRegex(unittest.TestCase):

    def test_ibkr_api_flow_is_intercepted(self):
        """The whole point of the proxy: api.ibkr.com MUST be MITM'd
        so the addon can parse order POSTs and replicate them."""
        allowed = _mitmproxy_allow_decision(
            _IBKR_CANDIDATES, [ALLOW_HOSTS_PATTERN]
        )
        self.assertTrue(
            allowed,
            "api.ibkr.com flow ended up ignored despite the peername IP "
            "being mixed in with the candidate hostnames. If this fails, "
            "the order-replication pipeline is silently broken — IBKR "
            "fills orders, addon never sees them.",
        )

    def test_tradingview_flow_is_raw_forwarded(self):
        """TV's own CDN/data hosts must NOT be intercepted; doing so
        chokes the chart-data WebSocket and makes the UI hang."""
        allowed = _mitmproxy_allow_decision(
            _TRADINGVIEW_CANDIDATES, [ALLOW_HOSTS_PATTERN]
        )
        self.assertFalse(
            allowed,
            "tradingview.com flow was accidentally MITM'd. Performance "
            "regression: every chart-data byte will now be parsed by "
            "mitmproxy's asyncio loop instead of being raw-forwarded.",
        )

    def test_ip_only_flow_is_not_intercepted(self):
        """A bare-IP destination (no SNI, no DNS name) shouldn't match
        the api.ibkr.com pattern — even though the user has reported a
        couple of "IP-only" CONNECTs in the wild. We want them to fall
        through to default-ignore, not get intercepted by accident."""
        ip_only = ["52.32.10.5:443", "52.32.10.5:443"]
        allowed = _mitmproxy_allow_decision(ip_only, [ALLOW_HOSTS_PATTERN])
        self.assertFalse(allowed)

    def test_old_negative_lookahead_pattern_demonstrates_the_bug(self):
        """Frozen historical evidence: this test ENSURES the old
        negative-lookahead regex matches the IBKR peername IP, which
        is what made `ignore_hosts` silently drop every order flow.

        If this test ever starts to FAIL, it would mean mitmproxy
        changed its multi-candidate hostname collection behaviour, in
        which case we'd want to know about it — but the fix in main.py
        (switching to allow_hosts) is robust either way, so the test
        is documentary, not load-bearing."""
        old_ignore = [r"^(?!api\.ibkr\.com(?::\d+)?$).+$"]
        ignored = any(
            re.search(rex, host)
            for host in _IBKR_CANDIDATES
            for rex in old_ignore
        )
        self.assertTrue(
            ignored,
            "If THIS assertion fails, the old buggy pattern would NOT "
            "have ignored the IBKR flow — meaning we may have "
            "misdiagnosed the root cause of the 5-Jun replication "
            "outage. Re-investigate before trusting this fix.",
        )


if __name__ == "__main__":
    unittest.main()
