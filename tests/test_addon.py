"""
Tests for TradeSyncAddon — the mitmproxy addon that orchestrates IBKR
observation → replication.

The addon's *pieces* (the parsers, the source observer, the replicator)
are tested in their own modules. These tests cover the ADDON's job:
wiring those pieces together inside the request/response hooks —
  * hook routing: which handler fires for POST/DELETE/modify/orders-list,
    host gating, passive-only requests;
  * the two-phase cOID ↔ IBKR-id binding (stash on request, bind on
    response), including the positional fallback and bracket children;
  * the orders-list parent→child resolution and its deferral;
  * robustness: no response, unparseable order, the _coids_by_flow
    cleanup.

A FakeSource stands in for the source observer (records emit_* /
register_ibkr_id calls and answers coid_for_ibkr_id), and _spawn is made
synchronous so a hook's effect is observable immediately without racing
the real daemon thread.
"""

import json
import unittest
from dataclasses import dataclass, field
from typing import Optional
from unittest.mock import MagicMock, patch

from tradesync.proxy.addon import TradeSyncAddon
from tradesync.event_replicator import EventResult


# ── mitmproxy flow stubs ───────────────────────────────────────────────── #

@dataclass
class _Req:
    method: str
    pretty_host: str
    path: str
    content: bytes = b""
    headers: dict = field(default_factory=dict)


@dataclass
class _Resp:
    status_code: int
    content: bytes = b""


@dataclass
class _Flow:
    request: _Req
    response: Optional[_Resp] = None


_ACCT_PATH = "/v1/tv/iserver/account/U1234567/orders"


def _req_flow(body, *, method="POST", host="api.ibkr.com", path=_ACCT_PATH,
              headers=None):
    raw = body if isinstance(body, (bytes, bytearray)) else \
        json.dumps(body).encode("utf-8")
    return _Flow(request=_Req(method=method, pretty_host=host, path=path,
                              content=raw, headers=headers or {}))


def _resp_flow(req_body, resp_body, *, method="POST", path=_ACCT_PATH,
               status=200, host="api.ibkr.com"):
    f = _req_flow(req_body, method=method, host=host, path=path)
    raw = resp_body if isinstance(resp_body, (bytes, bytearray)) else \
        (json.dumps(resp_body).encode("utf-8") if resp_body is not None
         else b"")
    f.response = _Resp(status_code=status, content=raw)
    return f


# ── fakes ──────────────────────────────────────────────────────────────── #

class FakeSource:
    """Stands in for IbkrSourceObserver. Records what the addon emits and
    binds, and resolves coid_for_ibkr_id from a dict the test sets up."""

    def __init__(self):
        self.emitted = []          # (kind, payload)
        self.bound = []            # (coid, ibkr_id)
        self.coid_by_ibkr = {}     # ibkr_id -> coid (for orders-list)
        self.result = EventResult(success=True, skipped=False,
                                  reason="ok")

    def emit_new(self, parsed):
        self.emitted.append(("new", parsed))
        return self.result

    def emit_cancel(self, cancel):
        self.emitted.append(("cancel", cancel))
        return self.result

    def emit_modify(self, modify):
        self.emitted.append(("modify", modify))
        return self.result

    def coid_for_ibkr_id(self, ibkr_id):
        return self.coid_by_ibkr.get(str(ibkr_id))

    def register_ibkr_id(self, coid, ibkr_id):
        self.bound.append((coid, str(ibkr_id)))


def _cfg():
    cfg = MagicMock()
    cfg.ibkr_watched_accounts = []
    cfg.tradovate_env = "demo"
    return cfg


def _make_addon(source):
    addon = TradeSyncAddon(cfg=_cfg(), tradovate=MagicMock(),
                           resolver=MagicMock(), source=source)
    # Make dispatch synchronous so a hook's emit is observable at once,
    # without racing the real daemon thread.
    addon._spawn = lambda fn, payload, *, label: fn(payload)
    return addon


# Bodies in the shape the parser accepts (see test_ibkr_parser).
def _single_body(coid="tv-1", **over):
    o = {"side": "BUY", "quantity": 2, "conid": 845307883,
         "orderType": "LMT", "price": 21500.0, "cOID": coid}
    o.update(over)
    return {"orders": [o]}


def _bracket_body(entry_coid="E1", tp_coid="E1#LMT", sl_coid="E1#STP"):
    return {"orders": [
        {"side": "BUY", "quantity": 2, "conid": 845307883,
         "orderType": "LMT", "price": 21500.0, "cOID": entry_coid},
        {"side": "SELL", "quantity": 2, "conid": 845307883,
         "orderType": "LMT", "price": 21600.0, "cOID": tp_coid,
         "parentId": entry_coid},
        {"side": "SELL", "quantity": 2, "conid": 845307883,
         "orderType": "STP", "auxPrice": 21400.0, "cOID": sl_coid,
         "parentId": entry_coid},
    ]}


# ── hook routing ───────────────────────────────────────────────────────── #

class TestRequestRouting(unittest.TestCase):

    def setUp(self):
        self.src = FakeSource()
        self.addon = _make_addon(self.src)

    def test_non_ibkr_host_is_ignored(self):
        self.addon.request(_req_flow(_single_body(),
                                     host="charts.tradingview.com"))
        self.assertEqual(self.src.emitted, [])

    def test_post_orders_routes_to_emit_new(self):
        self.addon.request(_req_flow(_single_body()))
        self.assertEqual(len(self.src.emitted), 1)
        self.assertEqual(self.src.emitted[0][0], "new")

    def test_delete_routes_to_emit_cancel(self):
        self.addon.request(_req_flow(
            b"", method="DELETE", path=_ACCT_PATH.rstrip("s") + "/319073567"))
        self.assertEqual(len(self.src.emitted), 1)
        self.assertEqual(self.src.emitted[0][0], "cancel")

    def test_post_to_order_id_routes_to_emit_modify(self):
        self.addon.request(_req_flow(
            {"orderType": "LMT", "price": 21550.0, "quantity": 2},
            method="POST", path=_ACCT_PATH.rstrip("s") + "/319073567"))
        self.assertEqual(len(self.src.emitted), 1)
        self.assertEqual(self.src.emitted[0][0], "modify")

    def test_unmatched_ibkr_request_emits_nothing(self):
        # A GET to some other IBKR endpoint is passive-only.
        self.addon.request(_req_flow(
            b"", method="GET", path="/v1/api/iserver/accounts"))
        self.assertEqual(self.src.emitted, [])

    def test_unparseable_order_is_skipped_not_raised(self):
        # quantity=0 is rejected by the parser; the hook must swallow it.
        self.addon.request(_req_flow(_single_body(quantity=0)))
        self.assertEqual(self.src.emitted, [])

    def test_auth_header_is_captured(self):
        self.addon.request(_req_flow(
            _single_body(), headers={"Authorization": "Bearer abc"}))
        self.addon._resolver.capture_token.assert_called_with("Bearer abc")


class TestResponseRouting(unittest.TestCase):

    def setUp(self):
        self.src = FakeSource()
        self.addon = _make_addon(self.src)

    def test_none_response_does_not_raise(self):
        f = _req_flow(_single_body())
        f.response = None
        self.addon.response(f)   # must not raise

    def test_non_ibkr_response_ignored(self):
        f = _resp_flow(_single_body(), {"order_id": "ibkr-1"},
                       host="charts.tradingview.com")
        self.addon.response(f)
        self.assertEqual(self.src.bound, [])

    def test_get_contract_info_is_observed(self):
        f = _resp_flow(b"", {"conid": 1}, method="GET",
                       path="/v1/api/iserver/contract/123/info")
        self.addon.response(f)
        self.assertTrue(self.addon._resolver.observe_contract_info.called)


# ── two-phase cOID ↔ IBKR-id binding ───────────────────────────────────── #

class TestNewOrderBinding(unittest.TestCase):

    def setUp(self):
        self.src = FakeSource()
        self.addon = _make_addon(self.src)

    def test_single_order_stash_then_bind(self):
        # Phase 1: request stashes the cOID.
        req = _req_flow(_single_body(coid="tv-1"))
        self.addon.request(req)
        self.assertIn(id(req), self.addon._coids_by_flow)
        # Phase 2: response binds cOID ↔ IBKR id. Same flow id.
        req.response = _Resp(status_code=200,
                             content=json.dumps({"order_id": "ibkr-42"}
                                                ).encode())
        self.addon.response(req)
        self.assertIn(("tv-1", "ibkr-42"), self.src.bound)
        # Cleanup: the per-flow stash is popped.
        self.assertNotIn(id(req), self.addon._coids_by_flow)

    def test_positional_fallback_when_response_omits_coid(self):
        # Response carries only ids (no cOID echoed) → addon falls back to
        # positional matching against the stashed request cOIDs.
        req = _req_flow(_single_body(coid="tv-9"))
        self.addon.request(req)
        req.response = _Resp(status_code=200,
                             content=json.dumps([{"order_id": "ibkr-9"}]
                                                ).encode())
        self.addon.response(req)
        self.assertIn(("tv-9", "ibkr-9"), self.src.bound)

    def test_bracket_binds_each_leg_in_body_order(self):
        req = _req_flow(_bracket_body())
        self.addon.request(req)
        # Response echoes ids for entry + 2 children, no cOIDs → positional.
        req.response = _Resp(
            status_code=200,
            content=json.dumps([{"order_id": "ib-E"},
                                {"order_id": "ib-TP"},
                                {"order_id": "ib-SL"}]).encode())
        self.addon.response(req)
        self.assertEqual(self.src.bound,
                         [("E1", "ib-E"), ("E1#LMT", "ib-TP"),
                          ("E1#STP", "ib-SL")])

    def test_response_with_no_ids_binds_nothing(self):
        req = _req_flow(_single_body(coid="tv-1"))
        self.addon.request(req)
        req.response = _Resp(status_code=200,
                             content=json.dumps({"foo": "bar"}).encode())
        self.addon.response(req)   # warns, must not raise
        self.assertEqual(self.src.bound, [])
        # Stash is still cleaned up even on the no-ids path.
        self.assertNotIn(id(req), self.addon._coids_by_flow)


class TestOrdersListBinding(unittest.TestCase):

    def setUp(self):
        self.src = FakeSource()
        self.addon = _make_addon(self.src)

    def _orders_list_flow(self, legs):
        return _resp_flow(b"", {"orders": legs}, method="GET",
                          path="/v1/tv/iserver/account/orders")

    def test_children_bound_when_parent_resolves(self):
        # The entry's IBKR id is already bound to its cOID.
        self.src.coid_by_ibkr["319073567"] = "E1"
        f = self._orders_list_flow([
            {"orderId": "319073568", "parentId": "319073567",
             "orderType": "LMT"},
            {"orderId": "319073569", "parentId": "319073567",
             "orderType": "STP"},
        ])
        self.addon.response(f)
        self.assertIn(("E1#LMT", "319073568"), self.src.bound)
        self.assertIn(("E1#STP", "319073569"), self.src.bound)

    def test_child_deferred_when_parent_not_yet_bound(self):
        # Parent IBKR id unknown (poll arrived before the entry bind) →
        # nothing bound, no crash; the next poll will retry.
        f = self._orders_list_flow([
            {"orderId": "319073568", "parentId": "999999",
             "orderType": "LMT"},
        ])
        self.addon.response(f)
        self.assertEqual(self.src.bound, [])


# ── failure alerting (Step B) ──────────────────────────────────────────── #

class TestFailureAlerting(unittest.TestCase):
    """On a failed replication the addon runner must surface the failure
    through emit_replication_failure (GUI panel + desktop notify).

    Unlike the other tests we DON'T stub _spawn to bypass the runner —
    the alerting logic lives inside the runner, so we run the real
    _spawn synchronously by making threads run inline."""

    def setUp(self):
        self.src = FakeSource()
        # Build the addon WITHOUT the _spawn shortcut so the real runner
        # (with the failure-alert branch) executes.
        self.addon = TradeSyncAddon(cfg=_cfg(), tradovate=MagicMock(),
                                    resolver=MagicMock(), source=self.src)

    def _run_inline(self):
        # Make threading.Thread run the target synchronously so the
        # runner's result-handling (and any emit) happens before assert.
        class _Inline:
            def __init__(self, *a, target=None, **k):
                self._t = target

            def start(self):
                self._t()
        return patch("tradesync.proxy.addon.threading.Thread", _Inline)

    def test_failed_result_emits_replication_failure(self):
        self.src.result = EventResult(success=False, skipped=False,
                                      reason="Tradovate rejected: margin")
        with self._run_inline(), \
             patch("tradesync.proxy.addon.emit_replication_failure") as emit:
            self.addon.request(_req_flow(_single_body()))
        emit.assert_called_once()
        kw = emit.call_args.kwargs
        self.assertEqual(kw["env"], "demo")   # _cfg() uses demo
        self.assertIn("margin", kw["reason"])

    def test_success_does_not_emit(self):
        self.src.result = EventResult(success=True, skipped=False, reason="ok")
        with self._run_inline(), \
             patch("tradesync.proxy.addon.emit_replication_failure") as emit:
            self.addon.request(_req_flow(_single_body()))
        emit.assert_not_called()

    def test_skipped_does_not_emit(self):
        self.src.result = EventResult(success=False, skipped=True,
                                      reason="not watched")
        with self._run_inline(), \
             patch("tradesync.proxy.addon.emit_replication_failure") as emit:
            self.addon.request(_req_flow(_single_body()))
        emit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
