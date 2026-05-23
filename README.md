# TradeSynchronizer

> Mirrors orders placed on Interactive Brokers (via TradingView
> Desktop) onto a Tradovate LEADER account, so [TradeSyncer][]
> can fan them out to every prop-firm follower account.

[TradeSyncer]: https://www.tradesyncer.com

## How it works

```
┌────────────────────────────┐
│  TradingView Desktop       │
│  (IBKR plugin)             │
│                            │   POST api.ibkr.com/v1/tv/iserver
│  --proxy-server=...        │──/account/<id>/orders ────────────┐
└────────────────────────────┘                                   │
                                                                 ▼
                              ┌──────────────────────────────────────────────┐
                              │  mitmproxy + TradeSynchronizer addon         │
                              │                                              │
                              │  1. Parse IBKR order (conid, qty, side, …)   │
                              │  2. Resolve conid → MESH6 (cache + fallback) │
                              │  3. /contract/find on Tradovate              │
                              │  4. /order/placeorder on Tradovate LEADER    │
                              └──────────────────────────────────────────────┘
                                                                 │
                                                                 ▼
                              ┌──────────────────────────────────────────────┐
                              │  Tradovate LEADER account                    │
                              │  (configured in TradeSyncer)                 │
                              │                                              │
                              │  → TradeSyncer copies to every follower      │
                              │    (prop-firm accounts)                      │
                              └──────────────────────────────────────────────┘
```

The original IBKR order is **never modified, blocked, or delayed** —
the proxy is purely passive on the IBKR side. Replication to Tradovate
happens in parallel on a background thread.

## Repository layout

```
TradeSynchronizer/
├── gui.py                        # GUI entry point (used by the .app bundle)
├── main.py                       # mitmproxy bootstrap (headless mode)
├── build_app.sh                  # generates TradeSynchronizer.app
├── requirements.txt
├── .env.example                  # copy to .env and fill in
└── tradesync/
    ├── config.py                 # .env loader + validation
    ├── replicator.py             # IBKR order → Tradovate place-order
    ├── brokers/
    │   ├── ibkr.py               # conid → symbol resolver (passive + active)
    │   └── tradovate.py          # auth, renew, contract/find, order/placeorder
    ├── symbols/
    │   └── converter.py          # MESH2026 ↔ MESH6
    ├── proxy/
    │   ├── addon.py              # mitmproxy hooks
    │   └── ibkr_parser.py        # IBKR JSON order body decoder
    └── ui/
        └── app.py                # Tkinter GUI (Settings + Log + Start/Stop)
```

## One-time setup

### 1. Install Python dependencies

```bash
cd TradeSynchronizer
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure credentials

```bash
cp .env.example .env
```

Open `.env` and fill in:

- **Tradovate credentials** *per environment*: each of `LIVE` and
  `DEMO` has its own block (`TRADOVATE_USERNAME_LIVE`,
  `TRADOVATE_USERNAME_DEMO`, etc.) so you can keep both accounts
  configured simultaneously. Get `TRADOVATE_CID` (string!) and
  `TRADOVATE_SEC` from <https://trader.tradovate.com/welcome> →
  *API Access*.
- **`TRADOVATE_ACCOUNT_ID_LIVE` / `_DEMO`**: pin each environment's
  LEADER account id. If empty, the first account from
  `/account/list` is used.
- **`PROXY_LISTEN_PORT_LIVE` / `_DEMO`**: each engine binds to its
  own port (default 8080 and 8081). TradingView's `--proxy-server`
  flag must point at the engine you want to feed.
- **`TRADOVATE_ENVIRONMENT`**: only matters in CLI mode (the GUI
  starts each engine with an explicit override).

### 3. Trust the mitmproxy CA on macOS

The proxy intercepts HTTPS traffic from TradingView; for the
certificates to validate, install and trust the local mitmproxy CA:

```bash
# Generates the CA the first time it runs and quits.
mitmdump --listen-port 8080 -q &
sleep 2
kill %1

# Add the CA to the system keychain
sudo security add-trusted-cert -d -r trustRoot \
     -k /Library/Keychains/System.keychain \
     ~/.mitmproxy/mitmproxy-ca-cert.pem
```

You'll only do this once.

## Daily use — Desktop app (recommended)

After §1 of *One-time setup*, build the .app bundle once:

```bash
./build_app.sh
```

This produces `TradeSynchronizer.app` in the project root. Drag it to
`/Applications` (or to the Dock) and double-click to launch.

The UI is dual-engine: LIVE and DEMO run as independent subprocesses,
each on its own port, and can be active simultaneously.

- **Header**: two side-by-side engine cards (LIVE, DEMO), each with
  a status dot (grey = stopped, amber = starting, green = running,
  red = error), the current port, and a Start / Stop button pair.
  *Reload* and *Save* (with a `*` when dirty) sit in the top-right.
- **General tab** *(active by default)*: settings shared by both
  engines — app metadata, proxy listen host, replication policy,
  logging.
- **Live tab** / **Demo tab**: per-environment credentials, the
  engine's listen port, and the IBKR account(s) to mirror. The two
  tabs are mirrors of each other — fill in one or both depending
  on which engine(s) you intend to use.
- **Log tab**: merged stdout of both engines, with `[LIVE]` lines
  tinted soft-red and `[DEMO]` lines tinted soft-blue so trades are
  always attributable at a glance. A legend in the toolbar
  documents the colour mapping.

Closing the window while any engine is running asks for confirmation
and sends SIGTERM to each (fallback SIGKILL after 5 s) before quitting.

The bundle is a thin shell wrapper: it just invokes `gui.py` using
the project's `.venv` interpreter (or `python3` from PATH as fallback).
If you move the project to a new path, re-run `./build_app.sh` to
re-bake the path into the launcher.

## Daily use — Headless / CLI mode

If you'd rather skip the GUI:

```bash
source .venv/bin/activate
python main.py
```

You should see (note the `[LIVE]` / `[DEMO]` tag in every line —
when both engines run via the GUI those logs interleave in
/tmp/tradesync.log and the tag tells them apart):

```
HH:MM:SS INFO    [LIVE] tradesync.bootstrap  TradeSynchronizer starting up
HH:MM:SS INFO    [LIVE] tradesync.tradovate  Tradovate auth OK — userId=…
HH:MM:SS INFO    [LIVE] tradesync.addon      TradeSyncAddon active — listening for IBKR orders on api.ibkr.com
HH:MM:SS INFO    [LIVE] tradesync.bootstrap  mitmproxy listening on 127.0.0.1:8080
```

### Launch TradingView Desktop through the proxy

```bash
open -a "TradingView" --args --proxy-server=127.0.0.1:8080
```

(If TradingView is already running, quit it first — Chromium-based
apps only pick up the `--proxy-server` flag at launch time.)

Place an order on IBKR from TradingView as usual. In the
TradeSynchronizer log (GUI **Log** tab or terminal) you'll see:

```
HH:MM:SS INFO    [LIVE] tradesync.addon      📥 IBKR order intercepted: BUY 1 U1234567 @ conid=… type=LMT price=21500.0 …
HH:MM:SS INFO    [LIVE] tradesync.tradovate  Placing Tradovate order: {…}
HH:MM:SS INFO    [LIVE] tradesync.addon      ✅ Replicated to Tradovate orderId=987654
```

TradeSyncer then fans the LEADER fill out to every follower
account configured there.

## Replication policy

Set via `.env`:

| Variable | Effect |
|---|---|
| `REPLICATION_MODE=mirror` *(default)* | Match the IBKR order type 1:1 (MKT→Market, LMT→Limit with same price, STP→Stop, STP LMT→StopLimit) — shared by both engines |
| `REPLICATION_MODE=market` | Always send a Market order on Tradovate, regardless of the IBKR type — fastest sync, no missed fills |
| `SKIP_PROTECTIVE_STOPS=true` *(default)* | Don't replicate `STP` / `STP LMT` orders (they're usually protective stop-loss orders on existing IBKR positions, and Tradesyncer's followers manage their own stops) |
| `IBKR_WATCHED_ACCOUNTS_LIVE`, `IBKR_WATCHED_ACCOUNTS_DEMO` | Per-engine: only replicate orders from these IBKR account(s). Empty = all accounts. Typically you'd set the LIVE engine to watch your live IBKR account (`U…`) and the DEMO engine to watch a paper account (`DU…`). |

## Troubleshooting

| Problem | Fix |
|---|---|
| `TradovateAuthError: HTTP 401 / Invalid credentials` | Check `TRADOVATE_USERNAME`, `TRADOVATE_PASSWORD`, `TRADOVATE_CID` (must be a string, not int), `TRADOVATE_SEC` in `.env` |
| `Could not resolve conid=… not in cache` | Open the chart for that symbol in TradingView once; the contract `/info` response will be observed and cached. Active fallback also works once an IBKR token has been captured. |
| `Contract 'MESH6' not found on Tradovate` | The symbol resolver produced a symbol Tradovate doesn't recognise. Check the log line "Symbol map: conid=… → IBKR='…' → Tradovate='…'" and verify against Tradovate's contract list. |
| TradingView doesn't go through the proxy | Quit TradingView completely, then relaunch with `open -a TradingView --args --proxy-server=127.0.0.1:8080`. Chromium-based apps only read the flag at launch. |
| `SSL: CERTIFICATE_VERIFY_FAILED` from TradingView | Re-run the keychain trust step from §3 of *One-time setup*. |

## What it does NOT do

- **Does not place orders directly on prop-firm accounts**. That's
  TradeSyncer's job; this tool only feeds its LEADER account.
- **Does not modify or cancel existing IBKR orders**. Only fresh
  POSTs to `/orders` are observed; PUT/PATCH/DELETE are ignored.
- **Does not replicate bracket/OCO legs**. If a future build needs
  them, see `mytradingguardMacOs/proxy/addon.py` for the
  multi-leg pattern.
- **Does not run on Linux / Windows out of the box**. The keychain
  trust command is macOS-specific; everything else is portable.
