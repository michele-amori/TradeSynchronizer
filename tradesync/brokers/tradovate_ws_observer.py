"""
TradovateWSObserver — observes a Tradovate account's user-data
WebSocket and emits broker-neutral OrderEvents.

This is the Tradovate realisation of SourceEndpoint: it lets a
Tradovate account act as the SOURCE in the bidirectional flow
(Tradovate → IBKR), the mirror of the existing IBKR-via-mitmproxy
source.

Status: COMPLETE and validated live. The connection lifecycle,
handshake, snapshot ingestion, heartbeat, reconnect-with-backoff and
account filtering are implemented and unit-tested. The push-event
translation is done by the stateful TradovatePushParser, calibrated
against real frames captured with the market open (2026-06-14) and
validated end to end against IBKR paper (native OCO bracket + MKT +
MODIFY + CANCEL). `_handle_push_event` routes every entity event to
that parser; anything the parser can't map yields no event (logged,
never a half-guessed order). `scripts/ws_spike.py --raw` remains the
tool to re-capture frames should the wire shape ever change.

Protocol (confirmed live against DEMO, see ws_spike.py):
  * Auth: reuse a connected TradovateClient's access token + userId.
  * WS: wss://<env>.tradovateapi.com/v1/websocket
  * Textual SockJS-like framing: 'o' open, 'h' heartbeat (reply '[]'),
    'a[...]' JSON message array, 'c[...]' close.
  * The 'o' frame is NOT spontaneous — send `authorize` right after
    the upgrade rather than waiting for it.
  * Request frame: "<endpoint>\n<id>\n\n<body>". authorize body is the
    bare token; user/syncrequest body is {"users":[<userId>]}.
  * Response: {"s":<status>,"i":<reqId>,"d":<data>}.
  * Push event: {"e":"props","d":{"entityType":..,"eventType":..,
    "entity":{..}}} — an entity event; the parser reassembles orders
    from the order / orderVersion / link / strategy entity stream.

Threading: a single daemon listener thread owns the socket. It both
reads frames and writes heartbeat replies (Tradovate's 'h' arrives on
the same read loop, so no separate heartbeat thread is needed). The
public start_observing / stop_observing methods manage that thread.
on_event is invoked from the listener thread; the replicator's
downstream handling must therefore be thread-safe (it already is — the
addon path calls it from daemon threads too).
"""

from __future__ import annotations

import json
import logging
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

from wsproto import WSConnection, ConnectionType
from wsproto.events import (
    AcceptConnection,
    CloseConnection,
    Ping,
    Pong,
    RejectConnection,
    Request,
    TextMessage,
)
from wsproto.frame_protocol import CloseReason

from ..order_event import OrderEvent
from .tradovate import TradovateClient
from .tradovate_push_parser import TradovatePushParser


logger = logging.getLogger("tradesync.tradovate_ws")


# Reconnect backoff schedule (seconds). Capped; the last value repeats.
_BACKOFF_SCHEDULE = (1, 2, 5, 10, 30, 60)

# How long to wait after the upgrade before sending authorize if no
# 'o' frame arrives on its own (it usually doesn't — see module doc).
_AUTHORIZE_FALLBACK_SECS = 1.0

# Liveness watchdog. Tradovate sends a heartbeat 'h' frame about every
# 2.5s, so the read loop should never go many seconds without receiving
# SOMETHING. If it does, the connection is silently dead — typically
# because another session for the same user (a second app login, a
# diagnostic, TradingView itself) took over the user-data channel: the
# TCP socket stays open and recv() just keeps timing out, so no error is
# ever raised and the old code would wait forever without reconnecting.
# When this many seconds pass with no frame at all, treat the connection
# as stale and force a reconnect through the supervisor. ~6x the
# heartbeat interval: long enough to ride out a hiccup, short enough to
# recover an order feed quickly.
_STALE_AFTER_SECS = 15.0


@dataclass(frozen=True)
class ObserverHealth:
    """A point-in-time view of the observer's connection, for the GUI
    status indicator."""
    identity: str
    connected: bool
    last_frame_at: Optional[float]          # wall-clock secs (time.time)
    seconds_since_last_frame: Optional[float]

    @property
    def receiving(self) -> bool:
        """True if connected AND a frame arrived recently (within the
        stale window). This is the 'green light' condition: the feed is
        actually alive, not just nominally connected."""
        return (self.connected
                and self.seconds_since_last_frame is not None
                and self.seconds_since_last_frame <= _STALE_AFTER_SECS)


class TradovateWSObserver:
    """SourceEndpoint that observes a Tradovate account via WebSocket.

    Structurally satisfies tradesync.brokers.endpoint.SourceEndpoint.

    Parameters
    ----------
    client:
        A TradovateClient. The observer calls connect() on it if it is
        not already connected, then reuses its access token + userId +
        account id. Sharing the client means one auth, one token-renew
        policy, and one account resolution across the follower and
        source roles.
    env:
        "demo" | "live" — selects the WS host.
    account_id:
        The Tradovate account id to observe. Events for other accounts
        on the same user are ignored (a user may have several).
    """

    def __init__(
        self,
        client: TradovateClient,
        *,
        env: str,
        account_id: str,
        authorize_fallback_secs: float = _AUTHORIZE_FALLBACK_SECS,
    ):
        self._client = client
        self._env = env
        self._account_id = str(account_id)
        self._ws_host = f"{env}.tradovateapi.com"
        # How long to wait for a spontaneous 'o' frame before sending
        # authorize anyway. Production keeps the ~1s default; tests
        # shrink it so the e2e loop converges fast.
        self._authorize_fallback_secs = authorize_fallback_secs

        self._on_event: Optional[Callable[[OrderEvent], None]] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

        # Connection state, owned by the listener thread.
        self._sock: Optional[socket.socket] = None
        self._ws: Optional[WSConnection] = None
        self._req_id = 0
        self._text_buf = ""

        # Set of source order ids seen in the initial snapshot, so we
        # add them to the map (for later cancel/modify) WITHOUT
        # re-replicating them as if they were brand-new. Populated on
        # each (re)sync; consulted by the push handler.
        self._snapshot_order_ids: set[str] = set()
        self._synced = False

        # Health, readable by a GUI status panel (thread-safe scalars).
        # _last_frame_at is wall-clock seconds (time.time) of the most
        # recent frame of any kind; _connected reflects whether the
        # current connection has completed its snapshot sync.
        self._last_frame_at: Optional[float] = None
        self._connected = False

        # Stateful push-frame parser, calibrated against real captured
        # frames. Resolves contractId → symbol via the shared client.
        # Created lazily so a test can inject its own before observing.
        self._parser: Optional[TradovatePushParser] = None

    # ── SourceEndpoint API ───────────────────────────────────────── #

    @property
    def identity(self) -> str:
        return f"tradovate_{self._env}_{self._account_id}"

    @property
    def health(self) -> "ObserverHealth":
        """A snapshot of the observer's connection health, for a GUI
        status indicator. Thread-safe: reads plain scalars written by
        the listener thread (worst case a slightly stale reading, which
        is fine for a status light)."""
        last = self._last_frame_at
        age = (time.time() - last) if last is not None else None
        return ObserverHealth(
            identity=self.identity,
            connected=self._connected,
            last_frame_at=last,
            seconds_since_last_frame=age,
        )

    def _effective_account_id(self) -> str:
        """The account id Tradovate actually stamps on order frames.

        The config pins an account by its human-readable NUMBER (the
        'name', e.g. 19000001), but Tradovate's order/fill push frames
        carry the account's internal primary-key `id` (e.g. 49000001). The
        connected client resolves name → internal id; use that for
        matching frames. Fall back to the configured value before the
        client has connected (e.g. during the initial snapshot path in
        tests). This is the difference between every live order being
        silently filtered out and the pipeline actually working.
        """
        internal = getattr(self._client, "account_id", None)
        # Only trust a concrete numeric id (the real client exposes an
        # int once connected). Anything else — None before connect, or a
        # test double — falls back to the configured value.
        if isinstance(internal, int):
            return str(internal)
        if isinstance(internal, str) and internal.isdigit():
            return internal
        return self._account_id

    def start_observing(self, on_event: Callable[[OrderEvent], None]) -> None:
        if self._thread is not None and self._thread.is_alive():
            logger.warning("TradovateWSObserver already observing — ignoring "
                           "duplicate start_observing()")
            return
        self._on_event = on_event
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_with_reconnect,
            name=f"tradovate-ws-{self._account_id}",
            daemon=True,
        )
        self._thread.start()
        logger.info("TradovateWSObserver started for %s", self.identity)

    def stop_observing(self) -> None:
        self._stop.set()
        self._close_socket()
        t = self._thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=5.0)
        self._thread = None
        logger.info("TradovateWSObserver stopped for %s", self.identity)

    # ── reconnect supervisor ─────────────────────────────────────── #

    def _run_with_reconnect(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            try:
                self._connect_and_run()
                # Clean return (server close / stop) resets backoff.
                attempt = 0
            except _AuthExpired:
                self._connected = False
                # Token rejected — refresh via the client and retry
                # immediately (no backoff; this is expected daily).
                logger.info("Tradovate WS token expired — refreshing and "
                            "reconnecting")
                try:
                    self._client.connect()
                except Exception as e:  # noqa: BLE001 - want to retry
                    logger.warning("Token refresh failed: %s", e)
                    self._sleep_backoff(attempt)
                    attempt += 1
                continue
            except Exception as e:  # noqa: BLE001 - supervisor must survive
                self._connected = False
                if self._stop.is_set():
                    break
                logger.warning("Tradovate WS connection error (%s) — "
                               "reconnecting", e)
                self._sleep_backoff(attempt)
                attempt += 1
                continue
            # If we got here without exception and we're not stopping,
            # the server closed the socket; back off then reconnect.
            self._connected = False
            if not self._stop.is_set():
                self._sleep_backoff(attempt)
                attempt += 1

    def _sleep_backoff(self, attempt: int) -> None:
        idx = min(attempt, len(_BACKOFF_SCHEDULE) - 1)
        delay = _BACKOFF_SCHEDULE[idx]
        # Interruptible sleep so stop_observing() doesn't wait the full
        # backoff.
        self._stop.wait(timeout=delay)

    # ── one connection's lifecycle ───────────────────────────────── #

    def _connect_and_run(self) -> None:
        if not self._client.connected:
            self._client.connect()
        token = self._access_token()
        if not token:
            raise _AuthExpired("no access token available from client")

        self._open_socket()
        self._handshake()

        # State for this connection.
        self._req_id = 0
        self._text_buf = ""
        self._synced = False
        authorized_sent = False
        synced_sent = False
        authorize_by = time.monotonic() + self._authorize_fallback_secs

        self._sock.settimeout(1.0)
        # Liveness watchdog: timestamp of the last frame received. The
        # heartbeat 'h' (~every 2.5s) keeps this fresh on a healthy link;
        # if it goes stale the feed is silently dead and we reconnect.
        last_rx = time.monotonic()
        while not self._stop.is_set():
            if not authorized_sent and time.monotonic() > authorize_by:
                authorized_sent = True
                self._send_request("authorize", token)
            try:
                data = self._sock.recv(65535)
            except socket.timeout:
                # No data this tick — only a problem if we've heard
                # NOTHING (not even a heartbeat) for too long.
                if time.monotonic() - last_rx > _STALE_AFTER_SECS:
                    raise ConnectionError(
                        f"no frame for {_STALE_AFTER_SECS:.0f}s — connection "
                        f"went stale, reconnecting")
                continue
            except OSError as e:
                raise ConnectionError(f"socket recv failed: {e}") from e
            if not data:
                logger.info("Tradovate WS server closed the socket")
                return
            last_rx = time.monotonic()
            self._last_frame_at = time.time()
            self._ws.receive_data(data)
            for event in self._ws.events():
                if isinstance(event, TextMessage):
                    self._text_buf += event.data
                    if not event.message_finished:
                        continue
                    frame = self._text_buf
                    self._text_buf = ""
                    kind = self._handle_frame(frame)
                    if kind == "a" and not synced_sent:
                        synced_sent = True
                        self._send_request(
                            "user/syncrequest",
                            json.dumps({"users": [self._client.user_id]}),
                        )
                    elif kind == "c":
                        return
                elif isinstance(event, Ping):
                    self._sock.sendall(self._ws.send(Pong(event.payload)))
                elif isinstance(event, CloseConnection):
                    logger.info("Tradovate WS CloseConnection: %s %s",
                                event.code, event.reason)
                    return

    def _open_socket(self) -> None:
        raw = socket.create_connection((self._ws_host, 443), timeout=10)
        ctx = ssl.create_default_context()
        self._sock = ctx.wrap_socket(raw, server_hostname=self._ws_host)
        self._ws = WSConnection(ConnectionType.CLIENT)
        self._sock.sendall(
            self._ws.send(Request(host=self._ws_host, target="/v1/websocket"))
        )

    def _handshake(self) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            data = self._sock.recv(65535)
            if not data:
                raise ConnectionError("socket closed during WS handshake")
            self._ws.receive_data(data)
            for event in self._ws.events():
                if isinstance(event, AcceptConnection):
                    return
                if isinstance(event, RejectConnection):
                    if event.status_code in (401, 403):
                        raise _AuthExpired(
                            f"WS upgrade rejected {event.status_code}")
                    raise ConnectionError(
                        f"WS upgrade rejected: {event.status_code}")
        raise ConnectionError("timed out waiting for WS upgrade")

    # ── frame handling ───────────────────────────────────────────── #

    def _handle_frame(self, frame: str) -> str:
        """Decode one textual frame; return its single-char kind."""
        kind = frame[:1]
        if kind == "o":
            pass  # open — nothing to do (we authorize proactively)
        elif kind == "h":
            self._send_text("[]")            # heartbeat keepalive
        elif kind == "a":
            self._handle_array(frame[1:])
        elif kind == "c":
            logger.info("Tradovate WS close frame: %r", frame)
        else:
            logger.debug("Tradovate WS (other) frame: %r", frame)
        return kind

    def _handle_array(self, json_text: str) -> None:
        try:
            messages = json.loads(json_text)
        except json.JSONDecodeError as e:
            logger.warning("Tradovate WS unparseable a-frame: %r (%s)",
                           json_text[:200], e)
            return
        if not isinstance(messages, list):
            return
        for m in messages:
            if not isinstance(m, dict):
                continue
            if "e" in m:
                self._handle_push_event(m)
            elif "i" in m:
                self._handle_response(m)

    def _handle_response(self, m: dict) -> None:
        """A response to one of our requests: {s,i,d}."""
        status = m.get("s")
        data = m.get("d")
        if status == 401:
            raise _AuthExpired("Tradovate WS response status 401")
        # The big one is the syncrequest response, whose `d` is the
        # account snapshot. Ingest its orders for dedup.
        if isinstance(data, dict) and "orders" in data:
            self._ingest_snapshot(data)

    def _ingest_snapshot(self, snapshot: dict) -> None:
        """Record the ids of orders that already exist at connect time
        so we don't re-replicate them as new. We DON'T emit events for
        these — they predate our observation."""
        orders = snapshot.get("orders") or []
        ids = set()
        for o in orders:
            if not isinstance(o, dict):
                continue
            if self._order_belongs_to_account(o):
                oid = o.get("id")
                if oid is not None:
                    ids.add(str(oid))
        self._snapshot_order_ids = ids
        self._synced = True
        self._connected = True
        logger.info("Tradovate WS snapshot ingested for %s: %d pre-existing "
                    "order(s) recorded for dedup", self.identity, len(ids))

    # ── push events (calibrated against real frames) ─────────────── #

    def _ensure_parser(self) -> TradovatePushParser:
        """Lazily build the stateful push parser, wiring contractId →
        symbol resolution through the shared client."""
        if self._parser is None:
            self._parser = TradovatePushParser(
                self._effective_account_id(),
                resolve_symbol=self._client.get_contract_name,
                # Match frames by the internal id, but report the
                # configured account number downstream so the replicator's
                # watch list (keyed on config) stays consistent.
                report_account_id=self._account_id,
            )
        return self._parser

    def _handle_push_event(self, m: dict) -> None:
        """Handle a pushed entity event: {"e": <name>, "d": <payload>}.

        Calibrated shape (real frames, 2026-06-14): the payload `d` is
        an entity event {"entityType":..., "eventType":..., "entity":
        {...}}. The stateful TradovatePushParser accumulates the
        constituent frames (order / orderVersion / link / strategy) and
        emits an OrderEvent when an executionReport says something
        actionable happened. We route every entity event to it; it
        decides what (if anything) to emit.

        Anything it doesn't map yields no event; we log such frames at
        debug so a live run can still surface unexpected shapes without
        spamming, but we never emit a half-guessed order.
        """
        payload = m.get("d")
        if not isinstance(payload, dict):
            return
        entity_type = payload.get("entityType")
        event_type = payload.get("eventType")
        entity = payload.get("entity")
        if not isinstance(entity, dict) or entity_type is None:
            return
        events = self._ensure_parser().handle(
            str(entity_type), str(event_type or ""), entity)
        for event in events:
            self._emit(event)

    def _order_belongs_to_account(self, entity: dict) -> bool:
        """True if a Tradovate order/fill entity is for the account we
        observe. Tradovate order entities carry `accountId` as the
        account's internal id, so match against the resolved internal
        id (see _effective_account_id), not the configured number."""
        acct = entity.get("accountId")
        return acct is None or str(acct) == self._effective_account_id()

    # ── low-level send ───────────────────────────────────────────── #

    def _send_text(self, text: str) -> None:
        if self._sock and self._ws:
            self._sock.sendall(self._ws.send(TextMessage(data=text)))

    def _send_request(self, endpoint: str, body: str = "") -> int:
        self._req_id += 1
        self._send_text(f"{endpoint}\n{self._req_id}\n\n{body}")
        return self._req_id

    def _emit(self, event: OrderEvent) -> None:
        cb = self._on_event
        if cb is None:
            logger.warning("TradovateWSObserver produced an event before "
                           "start_observing() — dropping %s", event.kind)
            return
        cb(event)

    def _access_token(self) -> Optional[str]:
        # TradovateClient stores it privately; read it directly. (Same
        # module family; a public accessor could be added later.)
        return getattr(self._client, "_access_token", None)

    def _close_socket(self) -> None:
        ws, sock = self._ws, self._sock
        if ws and sock:
            try:
                sock.sendall(ws.send(
                    CloseConnection(code=CloseReason.NORMAL_CLOSURE)))
            except OSError:
                pass
        if sock:
            try:
                sock.close()
            except OSError:
                pass
        self._sock = None
        self._ws = None


class _AuthExpired(Exception):
    """Internal signal: the WS rejected our token (401) or the upgrade
    was refused for auth reasons — trigger a token refresh + reconnect
    rather than a plain backoff retry."""
