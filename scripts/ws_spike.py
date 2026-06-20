#!/usr/bin/env python3
"""
ws_spike.py — Tradovate user-data WebSocket diagnostic.

A small, standalone tool to observe the Tradovate user-data WS channel
live. It validated the channel before the real TradovateWSObserver was
built (Week 2 of the bidirectional-replication work), and is kept as a
reusable diagnostic: re-run it with the market open while you place /
modify / cancel a DEMO order to capture the exact push-event frame
shapes (orderUpdate / fillUpdate / executionReport) that the observer's
parser must handle.

What it does:
  1. Authenticates against the Tradovate REST API to get an
     accessToken + userId (the same /auth/accesstokenrequest call
     TradovateClient.connect makes).
  2. Opens wss://<env>.tradovateapi.com/v1/websocket.
  3. Performs the Tradovate WS handshake, then sends `authorize` and
     `user/syncrequest`.
  4. Prints every frame it receives, decoded, for N seconds, then
     closes cleanly.

Protocol notes (confirmed live against DEMO, 2026-06-13)
-------------------------------------------------------
Tradovate multiplexes a SockJS-like *textual* protocol over the
WebSocket:
  'o'      open frame
  'h'      heartbeat (~every 2.5 s; the client must reply with '[]')
  'a[...]' a JSON array of messages (the actual data)
  'c[...]' close
Requests are sent as the text frame:  "<endpoint>\n<id>\n<query>\n<body>"
  * authorize        -> body is the bare access token
  * user/syncrequest -> body is {"users":[<userId>]}
Response messages carry {"s":<status>,"i":<reqId>,"d":<data>}; pushed
events carry {"e":<eventName>,"d":<payload>}.

IMPORTANT — the 'o' frame is not spontaneous. Against the live DEMO
endpoint the server does NOT send the SockJS 'o' open frame on its own
after the upgrade; it stays silent until the client sends something.
So this tool sends `authorize` proactively right after the upgrade
rather than waiting for 'o' (which then arrives, followed by the
authorize response). The real observer follows the same rule.

IMPORTANT — wsproto may fragment a logical text frame across multiple
TextMessage events; accumulate `event.data` until `message_finished`
before decoding.

Run it (makes a live network call with real DEMO creds):

    .venv/bin/python scripts/ws_spike.py                 # uses .env.demo, 30s
    .venv/bin/python scripts/ws_spike.py --seconds 90    # longer window
    .venv/bin/python scripts/ws_spike.py --raw           # verbose frame dump
    .venv/bin/python scripts/ws_spike.py --capture frames.json
                                                         # save frames to disk

Capturing for calibration
-------------------------
`--capture <file>` writes every decoded message to a structured JSON
file as well as the screen. Each entry records the seconds-since-start,
whether it was a pushed event ("e") or a response ("i"), and the full
payload. This is the artifact the observer's _order_event_from_push is
calibrated against: run with the market open, place / modify / cancel a
DEMO order, then read the captured `pushes` list to see the exact
orderUpdate / executionReport frame shapes. Pushed events are collected
separately from request responses so the order-lifecycle frames are
easy to find.

Uses wsproto (already present via mitmproxy) + stdlib socket/ssl, so
no new dependency. Not imported by anything in the package.
"""

from __future__ import annotations

import argparse
import json
import socket
import ssl
import time
from datetime import datetime, timezone
from pathlib import Path

# wsproto is a sans-I/O WebSocket implementation (handshake + framing);
# we drive the socket ourselves. It ships as a mitmproxy dependency.
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

import requests
from dotenv import dotenv_values


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_creds(env: str) -> dict:
    """Read Tradovate credentials from .env + .env.<env> without
    importing the app's Config (keeps the spike standalone)."""
    shared = dotenv_values(PROJECT_ROOT / ".env")
    specific = dotenv_values(PROJECT_ROOT / f".env.{env}")
    return {**shared, **specific}


def _rest_auth(base_url: str, creds: dict) -> tuple[str, int]:
    """POST /auth/accesstokenrequest -> (accessToken, userId)."""
    payload = {
        "name":       creds.get("TRADOVATE_USERNAME", ""),
        "password":   creds.get("TRADOVATE_PASSWORD", ""),
        "appId":      creds.get("TRADOVATE_APP_ID", ""),
        "appVersion": creds.get("TRADOVATE_APP_VERSION", "0.0.1"),
        "cid":        creds.get("TRADOVATE_CID", ""),
        "sec":        creds.get("TRADOVATE_SEC", ""),
        # ALWAYS use a dedicated, distinct deviceId — deliberately NOT
        # the env's TRADOVATE_DEVICE_ID. Tradovate appears to allow only
        # one live user-data session per deviceId, so a second auth with
        # the SAME deviceId silently invalidates the first. If this
        # diagnostic shared the engine's deviceId it would steal the
        # engine's session the moment it connected (the real cause of
        # the "lost MODIFY" episodes during live debugging). A separate
        # id lets the spike observe alongside the engine without
        # knocking it off its channel.
        "deviceId":   "ws-spike-DIAGNOSTIC-do-not-share",
    }
    print(f"[auth] POST {base_url}/auth/accesstokenrequest "
          f"(user={payload['name']})")
    resp = requests.post(f"{base_url}/auth/accesstokenrequest",
                         json=payload, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    token = data.get("accessToken")
    if not token:
        raise SystemExit(f"[auth] no accessToken — body: {data}")
    print(f"[auth] OK — userId={data.get('userId')}, "
          f"expires {data.get('expirationTime')}")
    return token, data.get("userId")


class TradovateWsSpike:
    """Minimal driver for the Tradovate WS textual protocol."""

    def __init__(self, host: str, token: str, user_id: int, *,
                 raw: bool = False, capture: bool = False):
        self._host = host
        self._token = token
        self._user_id = user_id
        self._raw = raw          # verbose low-level frame logging
        self._capture = capture  # collect frames for the JSON artifact
        self._sock: socket.socket | None = None
        self._ws: WSConnection | None = None
        self._req_id = 0
        self._t0 = time.monotonic()
        # Captured messages, split so the order-lifecycle pushes are
        # easy to find separately from request/response traffic.
        self.captured_pushes: list[dict] = []   # {"e":...} events
        self.captured_responses: list[dict] = []  # {"i":...} responses
        self.captured_other: list[dict] = []

    # ── connection ───────────────────────────────────────────────── #

    def connect(self) -> None:
        raw = socket.create_connection((self._host, 443), timeout=10)
        ctx = ssl.create_default_context()
        self._sock = ctx.wrap_socket(raw, server_hostname=self._host)
        self._ws = WSConnection(ConnectionType.CLIENT)
        req = Request(host=self._host, target="/v1/websocket")
        self._sock.sendall(self._ws.send(req))
        self._pump_until_open()

    def _pump_until_open(self) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            data = self._sock.recv(65535)
            if not data:
                raise SystemExit("[ws] socket closed during handshake")
            self._ws.receive_data(data)
            for event in self._ws.events():
                if isinstance(event, AcceptConnection):
                    print("[ws] upgrade accepted")
                    return
                if isinstance(event, RejectConnection):
                    raise SystemExit(f"[ws] upgrade rejected: {event.status_code}")
        raise SystemExit("[ws] timed out waiting for upgrade")

    # ── Tradovate textual framing ────────────────────────────────── #

    def _send_text(self, text: str, *, echo: bool = True) -> None:
        if echo:
            # The authorize frame carries the JWT; truncate so we don't
            # dump the whole token into the log.
            shown = text if len(text) < 80 else f"{text[:77]}…"
            print(f"[ws→] {shown!r}")
        self._sock.sendall(self._ws.send(TextMessage(data=text)))

    def _send_request(self, endpoint: str, body: str = "") -> int:
        self._req_id += 1
        frame = f"{endpoint}\n{self._req_id}\n\n{body}"
        self._send_text(frame)
        return self._req_id

    def authorize(self) -> None:
        # The authorize frame body is just the bare access token.
        self._send_request("authorize", self._token)

    def syncrequest(self) -> None:
        body = json.dumps({"users": [self._user_id]})
        self._send_request("user/syncrequest", body)

    # ── main loop ────────────────────────────────────────────────── #

    def run(self, seconds: int) -> None:
        self._sock.settimeout(1.0)
        authorized_sent = False
        synced = False
        text_buf = ""          # accumulate fragmented TextMessages
        deadline = time.monotonic() + seconds
        # The 'o' open frame is NOT spontaneous on this endpoint (see
        # module docstring): send authorize shortly after the upgrade
        # rather than waiting for 'o'. A short delay lets a spontaneous
        # 'o' arrive first on deployments that do send one.
        authorize_by = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if not authorized_sent and time.monotonic() > authorize_by:
                authorized_sent = True
                self.authorize()
            try:
                data = self._sock.recv(65535)
            except socket.timeout:
                continue
            if not data:
                print("[ws] server closed the socket")
                return
            if self._raw:
                print(f"[ws raw] recv {len(data)} bytes")
            self._ws.receive_data(data)
            for event in self._ws.events():
                if isinstance(event, TextMessage):
                    text_buf += event.data
                    if not event.message_finished:
                        continue
                    frame = text_buf
                    text_buf = ""
                    kind = self._handle_frame(frame)
                    if kind == "a" and not synced:
                        synced = True
                        print("[ws] first data frame — sending syncrequest")
                        self.syncrequest()
                    elif kind == "c":
                        return
                elif isinstance(event, Ping):
                    self._sock.sendall(self._ws.send(Pong(event.payload)))
                elif isinstance(event, CloseConnection):
                    print(f"[ws] CloseConnection: {event.code} {event.reason}")
                    return
        print(f"[ws] {seconds}s elapsed — closing")

    def _handle_frame(self, frame: str) -> str:
        """Decode one textual frame; return its single-char kind."""
        kind = frame[:1]
        if kind == "o":
            print("[ws←] 'o' open frame")
        elif kind == "h":
            # heartbeat — reply with '[]' keepalive (no echo: too noisy)
            self._send_text("[]", echo=False)
        elif kind == "a":
            self._handle_array(frame[1:])
        elif kind == "c":
            print(f"[ws←] close frame: {frame!r}")
        else:
            print(f"[ws←] (other) {frame!r}")
        return kind

    def _handle_array(self, json_text: str) -> None:
        try:
            messages = json.loads(json_text)
        except json.JSONDecodeError as e:
            print(f"[ws←] a-frame (unparseable): {json_text!r} ({e})")
            return
        for m in messages:
            # Responses to our requests carry 'i' (request id) + 's'
            # (status) + 'd' (data). Pushed events carry 'e' (event
            # name) + 'd' (payload). Print both shapes verbatim — these
            # are exactly what the observer's parser keys off.
            print(f"[ws← msg] {json.dumps(m)}")
            if self._capture and isinstance(m, dict):
                entry = {"t": round(time.monotonic() - self._t0, 3), "msg": m}
                if "e" in m:
                    self.captured_pushes.append(entry)
                elif "i" in m:
                    self.captured_responses.append(entry)
                else:
                    self.captured_other.append(entry)

    def dump_capture(self, path: Path, env: str) -> None:
        """Write the captured frames to a structured JSON file."""
        payload = {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "env": env,
            "counts": {
                "pushes": len(self.captured_pushes),
                "responses": len(self.captured_responses),
                "other": len(self.captured_other),
            },
            # Pushed order-lifecycle events first — these are what the
            # observer's _order_event_from_push needs to be calibrated
            # against.
            "pushes": self.captured_pushes,
            "responses": self.captured_responses,
            "other": self.captured_other,
        }
        path.write_text(json.dumps(payload, indent=2))
        print(f"[capture] wrote {len(self.captured_pushes)} push event(s), "
              f"{len(self.captured_responses)} response(s) to {path}")
        if not self.captured_pushes:
            print("[capture] NOTE: no push events captured — was the market "
                  "open and did you place/modify/cancel an order during the "
                  "window?")

    def close(self) -> None:
        if self._ws and self._sock:
            try:
                self._sock.sendall(
                    self._ws.send(CloseConnection(code=CloseReason.NORMAL_CLOSURE))
                )
            except OSError:
                pass
        if self._sock:
            self._sock.close()


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Tradovate user-data WebSocket diagnostic.")
    ap.add_argument("--env", default="demo", choices=["demo", "live"])
    ap.add_argument("--seconds", type=int, default=30)
    ap.add_argument("--raw", action="store_true",
                    help="verbose low-level frame logging")
    ap.add_argument("--capture", metavar="FILE", default=None,
                    help="save decoded frames to a structured JSON file "
                         "for offline calibration of the observer's parser")
    args = ap.parse_args()

    creds = _load_creds(args.env)
    if not creds.get("TRADOVATE_USERNAME"):
        raise SystemExit(f"No TRADOVATE_USERNAME in .env/.env.{args.env} — "
                         "fill credentials first.")

    rest_base = f"https://{args.env}.tradovateapi.com/v1"
    ws_host = f"{args.env}.tradovateapi.com"

    token, user_id = _rest_auth(rest_base, creds)

    spike = TradovateWsSpike(ws_host, token, user_id, raw=args.raw,
                             capture=bool(args.capture))
    print(f"[ws] connecting to wss://{ws_host}/v1/websocket")
    spike.connect()
    try:
        spike.run(args.seconds)
    finally:
        spike.close()
        if args.capture:
            spike.dump_capture(Path(args.capture), args.env)
        print("[ws] done")


if __name__ == "__main__":
    main()
