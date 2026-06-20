"""
Tests for TradovateWSObserver — the Tradovate-as-source WebSocket
observer.

Two layers are covered:
  1. Pure frame-parsing logic (the Tradovate textual protocol: 'o' /
     'h' / 'a[...]' / 'c', the {s,i,d} response envelope, snapshot
     ingestion, account filtering, and that uncalibrated push events
     do NOT emit a guessed OrderEvent). These run with no network.
  2. The connection loop driven by a fake in-process WebSocket server
     (a real wsproto server on a socketpair), exercising handshake →
     proactive authorize → syncrequest → heartbeat reply → snapshot,
     end-to-end without touching the real Tradovate endpoint.

The push-event → OrderEvent translation is intentionally NOT asserted
to produce events: it's `TODO calibrate` until the live frame shapes
are captured with the market open. The test pins the current
contract — uncalibrated push frames are logged and dropped, never
mistranslated.
"""

import json
import socket
import threading
import time
import unittest
from unittest.mock import MagicMock

from tradesync.brokers.endpoint import SourceEndpoint
from tradesync.brokers.tradovate_ws_observer import (
    TradovateWSObserver,
    _AuthExpired,
)


def _make_observer(account_id="50000001", authorize_fallback_secs=0.05):
    client = MagicMock()
    client.connected = True
    client.user_id = 3701228
    client._access_token = "fake-jwt-token"
    obs = TradovateWSObserver(
        client, env="demo", account_id=account_id,
        authorize_fallback_secs=authorize_fallback_secs)
    return obs, client


class TestIdentityAndProtocol(unittest.TestCase):

    def test_identity(self):
        obs, _ = _make_observer()
        self.assertEqual(obs.identity, "tradovate_demo_50000001")

    def test_satisfies_source_protocol(self):
        obs, _ = _make_observer()
        self.assertIsInstance(obs, SourceEndpoint)


class TestFrameParsing(unittest.TestCase):
    """Drive the protocol handlers directly — no socket."""

    def setUp(self):
        self.obs, self.client = _make_observer()
        self.sent = []
        # Capture outgoing text frames without a socket.
        self.obs._send_text = lambda t: self.sent.append(t)
        self.events = []
        self.obs._on_event = self.events.append

    def test_heartbeat_replies_empty_array(self):
        kind = self.obs._handle_frame("h")
        self.assertEqual(kind, "h")
        self.assertEqual(self.sent, ["[]"])

    def test_open_frame_is_noop(self):
        kind = self.obs._handle_frame("o")
        self.assertEqual(kind, "o")
        self.assertEqual(self.sent, [])

    def test_close_frame_kind(self):
        self.assertEqual(self.obs._handle_frame('c[1000,"x"]'), "c")

    def test_response_401_raises_auth_expired(self):
        with self.assertRaises(_AuthExpired):
            self.obs._handle_array(json.dumps([{"s": 401, "i": 1}]))

    def test_snapshot_ingestion_records_order_ids(self):
        snapshot = {
            "orders": [
                {"id": 111, "accountId": 50000001, "ordStatus": "Working"},
                {"id": 222, "accountId": 50000001, "ordStatus": "Working"},
            ],
            "positions": [],
        }
        self.obs._handle_array(json.dumps([{"s": 200, "i": 2, "d": snapshot}]))
        self.assertEqual(self.obs._snapshot_order_ids, {"111", "222"})
        self.assertTrue(self.obs._synced)

    def test_snapshot_filters_by_account(self):
        snapshot = {
            "orders": [
                {"id": 111, "accountId": 50000001},   # ours
                {"id": 999, "accountId": 88888888},   # another account
            ],
        }
        self.obs._handle_array(json.dumps([{"s": 200, "i": 2, "d": snapshot}]))
        self.assertEqual(self.obs._snapshot_order_ids, {"111"})

    def test_empty_snapshot(self):
        self.obs._handle_array(
            json.dumps([{"s": 200, "i": 2, "d": {"orders": []}}]))
        self.assertEqual(self.obs._snapshot_order_ids, set())
        self.assertTrue(self.obs._synced)

    def test_partial_push_event_emits_nothing(self):
        # An `order` frame alone is not actionable: the parser needs an
        # executionReport (and the orderVersion details) before it emits.
        # A lone order entity must therefore produce no OrderEvent.
        self.obs._handle_array(json.dumps([
            {"e": "props", "d": {"entityType": "order",
                                 "eventType": "Created",
                                 "entity": {"id": 333,
                                            "accountId": 50000001,
                                            "contractId": 4327110,
                                            "action": "Buy"}}}
        ]))
        self.assertEqual(self.events, [])

    def test_unparseable_aframe_is_swallowed(self):
        # Must not raise.
        self.obs._handle_array("not json{{{")
        self.assertEqual(self.events, [])


class TestAccountFilter(unittest.TestCase):

    def setUp(self):
        self.obs, _ = _make_observer(account_id="50000001")

    def test_matching_account(self):
        self.assertTrue(
            self.obs._order_belongs_to_account({"accountId": 50000001}))

    def test_other_account(self):
        self.assertFalse(
            self.obs._order_belongs_to_account({"accountId": 88888888}))

    def test_missing_account_is_permissive(self):
        # An entity with no accountId isn't filtered out (we can't tell);
        # the calibrated push handler will tighten this once we know the
        # real shape.
        self.assertTrue(self.obs._order_belongs_to_account({"id": 5}))


class TestPushTranslationCalibrated(unittest.TestCase):
    """The push translation is now CALIBRATED (against real frames):
    the observer routes entity events to a stateful TradovatePushParser
    and emits real OrderEvents. A complete single-order sequence
    (order + orderVersion + executionReport New) must produce one NEW."""

    def test_single_order_sequence_emits_new(self):
        obs, client = _make_observer(account_id="50000001")
        events = []
        client.get_contract_name.return_value = "MNQM6"
        obs.start_observing(events.append)
        obs._handle_array(json.dumps([{"e": "props", "d": {
            "entityType": "order", "eventType": "Created",
            "entity": {"id": 1, "accountId": 50000001,
                       "contractId": 4327110, "action": "Buy",
                       "ordStatus": "Working"}}}]))
        obs._handle_array(json.dumps([{"e": "props", "d": {
            "entityType": "orderVersion", "eventType": "Created",
            "entity": {"id": 1, "orderId": 1, "orderQty": 1,
                       "orderType": "Market"}}}]))
        obs._handle_array(json.dumps([{"e": "props", "d": {
            "entityType": "executionReport", "eventType": "Created",
            "entity": {"orderId": 1, "accountId": 50000001,
                       "execType": "New"}}}]))
        news = [e for e in events if e.kind.value == "NEW"]
        self.assertEqual(len(news), 1)


# ── End-to-end loop against a fake in-process wsproto server ─────────── #

class _FakeTradovateWSServer:
    """A minimal real-wsproto server on one end of a socketpair, playing
    the Tradovate textual protocol back at the observer. Lets us drive
    _connect_and_run end-to-end with no real network."""

    def __init__(self, server_sock):
        from wsproto import WSConnection, ConnectionType
        self._sock = server_sock
        self._ws = WSConnection(ConnectionType.SERVER)
        self._frames_to_send_after_sync = []
        self.received_frames = []

    def serve(self, hello_with_o=False, snapshot=None, run_seconds=2.0):
        from wsproto.events import (
            Request, AcceptConnection, TextMessage, CloseConnection,
        )
        self._sock.settimeout(0.05)
        deadline = time.monotonic() + run_seconds
        accepted = False
        sent_o = False
        sent_sync_resp = False
        while time.monotonic() < deadline:
            try:
                data = self._sock.recv(65535)
            except socket.timeout:
                # Periodically nudge: after accept, optionally emit 'o'
                if accepted and hello_with_o and not sent_o:
                    sent_o = True
                    self._send("o")
                continue
            except OSError:
                return
            if not data:
                return
            self._ws.receive_data(data)
            for event in self._ws.events():
                if isinstance(event, Request):
                    self._sock.sendall(self._ws.send(AcceptConnection()))
                    accepted = True
                elif isinstance(event, TextMessage):
                    frame = event.data
                    self.received_frames.append(frame)
                    # When the observer sends authorize, reply with the
                    # authorize ack as an 'a' frame.
                    if frame.startswith("authorize"):
                        self._send('a[{"s":200,"i":1}]')
                    elif frame.startswith("user/syncrequest"):
                        if not sent_sync_resp:
                            sent_sync_resp = True
                            snap = snapshot or {"orders": []}
                            msg = {"s": 200, "i": 2, "d": snap}
                            self._send("a" + json.dumps([msg]))
                elif isinstance(event, CloseConnection):
                    return

    def _send(self, text):
        from wsproto.events import TextMessage
        try:
            self._sock.sendall(self._ws.send(TextMessage(data=text)))
        except OSError:
            pass


class TestEndToEndLoop(unittest.TestCase):
    """Exercise the real _connect_and_run loop against the fake server,
    bypassing only the socket-open step (we hand it the socketpair)."""

    def _run_against_fake(self, snapshot=None, hello_with_o=False):
        client_sock, server_sock = socket.socketpair()
        obs, client = _make_observer()

        # Patch _open_socket to use our pre-made client socket and a
        # client wsproto that we drive through the upgrade.
        from wsproto import WSConnection, ConnectionType
        from wsproto.events import Request

        def fake_open():
            obs._sock = client_sock
            obs._ws = WSConnection(ConnectionType.CLIENT)
            client_sock.sendall(
                obs._ws.send(Request(host=obs._ws_host, target="/v1/websocket")))

        obs._open_socket = fake_open

        server = _FakeTradovateWSServer(server_sock)
        server_thread = threading.Thread(
            target=server.serve,
            kwargs=dict(hello_with_o=hello_with_o, snapshot=snapshot,
                        run_seconds=2.0),
            daemon=True,
        )
        server_thread.start()

        # Run the observer loop in a thread; stop it after a beat.
        obs_thread = threading.Thread(target=obs._connect_and_run, daemon=True)
        obs_thread.start()

        # Give the handshake + sync a moment.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if obs._synced:
                break
            time.sleep(0.02)

        obs._stop.set()
        obs_thread.join(timeout=2.0)
        server_thread.join(timeout=2.0)
        client_sock.close()
        server_sock.close()
        return obs, server

    def test_handshake_authorize_sync_snapshot(self):
        snapshot = {"orders": [{"id": 777, "accountId": 50000001}]}
        obs, server = self._run_against_fake(snapshot=snapshot)
        # Observer must have authorized and syncrequested.
        joined = "\n".join(server.received_frames)
        self.assertIn("authorize", joined)
        self.assertIn("user/syncrequest", joined)
        # And ingested the snapshot.
        self.assertTrue(obs._synced)
        self.assertEqual(obs._snapshot_order_ids, {"777"})

    def test_works_when_server_sends_spontaneous_o(self):
        # Even if a deployment DOES send 'o', the proactive-authorize
        # path must still converge.
        obs, server = self._run_against_fake(
            snapshot={"orders": []}, hello_with_o=True)
        self.assertTrue(obs._synced)


class TestInternalAccountIdMatching(unittest.TestCase):
    """Regression: a pair pins the account by its human-readable NUMBER
    (the 'name', e.g. 19000001), but Tradovate stamps order push frames
    with the account's internal primary-key id (e.g. 49000001). The
    observer must match frames by the internal id (resolved by the
    connected client) yet report the configured number downstream, or
    every live order is silently filtered out."""

    def _make(self, config_id="19000001", internal_id=49000001):
        client = MagicMock()
        client.connected = True
        client.user_id = 3701228
        client._access_token = "tok"
        client.account_id = internal_id          # resolved internal id
        client.get_contract_name.return_value = "MNQM6"
        obs = TradovateWSObserver(
            client, env="live", account_id=config_id,
            authorize_fallback_secs=0.05)
        return obs, client

    def test_effective_id_is_internal_when_connected(self):
        obs, _ = self._make()
        self.assertEqual(obs._effective_account_id(), "49000001")

    def test_frame_with_internal_id_is_not_filtered(self):
        obs, _ = self._make()
        # A frame carrying the INTERNAL id must be recognised as ours.
        self.assertTrue(
            obs._order_belongs_to_account({"accountId": 49000001}))
        # A frame for a different account must not.
        self.assertFalse(
            obs._order_belongs_to_account({"accountId": 999999}))
        # And the CONFIG number is NOT what frames carry, so it must not
        # match (proving we switched to the internal id).
        self.assertFalse(
            obs._order_belongs_to_account({"accountId": 19000001}))

    def test_emitted_event_reports_config_number(self):
        obs, _ = self._make()
        events = []
        obs.start_observing(events.append)
        def push(et, ev, entity):
            obs._handle_array(json.dumps([{"e": "props", "d": {
                "entityType": et, "eventType": ev, "entity": entity}}]))
        push("order", "Created", {
            "id": 1, "accountId": 49000001, "contractId": 4327110,
            "action": "Buy", "ordStatus": "Working"})
        push("orderVersion", "Created", {
            "id": 1, "orderId": 1, "orderQty": 1, "orderType": "Market"})
        push("executionReport", "Created", {
            "orderId": 1, "accountId": 49000001, "execType": "New"})
        news = [e for e in events if e.kind.value == "NEW"]
        self.assertEqual(len(news), 1)
        # Filtered in by internal id, but reported as the config number
        # so the downstream watch list (keyed on config) matches.
        self.assertEqual(news[0].source_account_id, "19000001")


class TestStaleConnectionWatchdog(unittest.TestCase):
    """Regression: if Tradovate silently stops sending frames (e.g. the
    user-data session is taken over by another login), the TCP socket
    stays open and recv() just keeps timing out. The old loop would wait
    forever, silently receiving no order updates. The watchdog must
    detect the silence and raise so the supervisor reconnects."""

    def test_silent_connection_raises_connection_error(self):
        import tradesync.brokers.tradovate_ws_observer as mod

        # A server that completes the WS upgrade then says NOTHING —
        # no snapshot, no heartbeat — mimicking a hijacked session.
        client_sock, server_sock = socket.socketpair()

        def serve_then_go_silent():
            from wsproto import WSConnection, ConnectionType
            from wsproto.events import Request, AcceptConnection
            ws = WSConnection(ConnectionType.SERVER)
            server_sock.settimeout(0.05)
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                try:
                    data = server_sock.recv(65535)
                except socket.timeout:
                    continue
                except OSError:
                    return
                if not data:
                    return
                ws.receive_data(data)
                for event in ws.events():
                    if isinstance(event, Request):
                        server_sock.sendall(ws.send(AcceptConnection()))
                    # After upgrade: deliberately send nothing further.

        obs, _ = _make_observer()

        from wsproto import WSConnection, ConnectionType
        from wsproto.events import Request

        def fake_open():
            obs._sock = client_sock
            obs._ws = WSConnection(ConnectionType.CLIENT)
            client_sock.sendall(
                obs._ws.send(Request(host=obs._ws_host,
                                     target="/v1/websocket")))

        obs._open_socket = fake_open

        server_thread = threading.Thread(target=serve_then_go_silent,
                                          daemon=True)
        server_thread.start()

        # Shrink the watchdog so the test is fast.
        original = mod._STALE_AFTER_SECS
        mod._STALE_AFTER_SECS = 0.5
        try:
            with self.assertRaises(ConnectionError) as ctx:
                obs._connect_and_run()
            self.assertIn("stale", str(ctx.exception).lower())
        finally:
            mod._STALE_AFTER_SECS = original
            obs._stop.set()
            client_sock.close()
            server_sock.close()
            server_thread.join(timeout=2.0)

    def test_heartbeats_keep_connection_alive(self):
        # A connection that keeps sending heartbeats must NOT be torn
        # down by the watchdog, even with a short stale window.
        import tradesync.brokers.tradovate_ws_observer as mod
        client_sock, server_sock = socket.socketpair()

        def serve_with_heartbeats():
            from wsproto import WSConnection, ConnectionType
            from wsproto.events import Request, AcceptConnection, TextMessage
            ws = WSConnection(ConnectionType.SERVER)
            server_sock.settimeout(0.05)
            deadline = time.monotonic() + 1.5
            last_hb = 0.0
            accepted = False
            while time.monotonic() < deadline:
                now = time.monotonic()
                if accepted and now - last_hb > 0.1:  # heartbeat in window
                    last_hb = now
                    try:
                        server_sock.sendall(ws.send(TextMessage(data="h")))
                    except OSError:
                        return
                try:
                    data = server_sock.recv(65535)
                except socket.timeout:
                    continue
                except OSError:
                    return
                if not data:
                    return
                ws.receive_data(data)
                for event in ws.events():
                    if isinstance(event, Request):
                        server_sock.sendall(ws.send(AcceptConnection()))
                        accepted = True

        obs, _ = _make_observer()
        from wsproto import WSConnection, ConnectionType
        from wsproto.events import Request

        def fake_open():
            obs._sock = client_sock
            obs._ws = WSConnection(ConnectionType.CLIENT)
            client_sock.sendall(
                obs._ws.send(Request(host=obs._ws_host,
                                     target="/v1/websocket")))

        obs._open_socket = fake_open
        server_thread = threading.Thread(target=serve_with_heartbeats,
                                         daemon=True)
        server_thread.start()

        original = mod._STALE_AFTER_SECS
        mod._STALE_AFTER_SECS = 0.5
        raised = []
        def run():
            try:
                obs._connect_and_run()
            except Exception as e:  # noqa: BLE001
                raised.append(e)
        try:
            obs_thread = threading.Thread(target=run, daemon=True)
            obs_thread.start()
            # Let it run longer than the stale window; heartbeats should
            # keep it alive the whole time.
            time.sleep(1.0)
            obs._stop.set()
            obs_thread.join(timeout=2.0)
            # No stale ConnectionError should have been raised while
            # heartbeats were flowing.
            stale = [e for e in raised
                     if isinstance(e, ConnectionError)
                     and "stale" in str(e).lower()]
            self.assertEqual(stale, [])
        finally:
            mod._STALE_AFTER_SECS = original
            obs._stop.set()
            client_sock.close()
            server_sock.close()
            server_thread.join(timeout=2.0)


if __name__ == "__main__":
    unittest.main()
