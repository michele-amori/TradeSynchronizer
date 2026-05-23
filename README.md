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

- **Tradovate credentials**: get `TRADOVATE_CID` (string!) and
  `TRADOVATE_SEC` from <https://trader.tradovate.com/welcome> →
  *API Access*. Set `TRADOVATE_ENVIRONMENT=demo` for paper, `live`
  for live.
- **`TRADOVATE_ACCOUNT_ID`**: pin the account id of the LEADER you
  configured on TradeSyncer. If empty, the first account from
  `/account/list` is used (fine for single-account users).

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

The UI has three pieces:

- **Header** with a Start / Stop button and a coloured status dot
  (grey = stopped, amber = starting, green = running, red = error).
- **Settings tab** — a form bound to `.env`. Edit any value, click
  **Save**, and the file is rewritten while preserving the layout of
  `.env.example`. A `*` next to *Save* marks unsaved changes; clicking
  *Start* with unsaved changes prompts to save first.
- **Log tab** — live tail of the proxy's stdout, dark theme, with
  auto-scroll toggle and Clear button.

Closing the window while the proxy is running asks for confirmation
and then sends SIGTERM (fallback SIGKILL after 5 s) before quitting.

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

You should see:

```
HH:MM:SS INFO    tradesync.bootstrap  TradeSynchronizer starting up
HH:MM:SS INFO    tradesync.tradovate  Tradovate auth OK — userId=…
HH:MM:SS INFO    tradesync.addon      TradeSyncAddon active — listening for IBKR orders on api.ibkr.com
HH:MM:SS INFO    tradesync.bootstrap  mitmproxy listening on 127.0.0.1:8080
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
HH:MM:SS INFO    tradesync.addon      📥 IBKR order intercepted: BUY 1 U1234567 @ conid=… type=LMT price=21500.0 …
HH:MM:SS INFO    tradesync.tradovate  Placing Tradovate order: {…}
HH:MM:SS INFO    tradesync.addon      ✅ Replicated to Tradovate orderId=987654
```

TradeSyncer then fans the LEADER fill out to every follower
account configured there.

## Replication policy

Set via `.env`:

| Variable | Effect |
|---|---|
| `REPLICATION_MODE=mirror` *(default)* | Match the IBKR order type 1:1 (MKT→Market, LMT→Limit with same price, STP→Stop, STP LMT→StopLimit) |
| `REPLICATION_MODE=market` | Always send a Market order on Tradovate, regardless of the IBKR type — fastest sync, no missed fills |
| `SKIP_PROTECTIVE_STOPS=true` *(default)* | Don't replicate `STP` / `STP LMT` orders (they're usually protective stop-loss orders on existing IBKR positions, and Tradesyncer's followers manage their own stops) |
| `IBKR_WATCHED_ACCOUNTS=U1234567,U2345678` | Only replicate orders from these IBKR accounts. Empty = all accounts |

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
