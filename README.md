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
├── .env                          # shared settings  (gitignored)
├── .env.live                     # LIVE engine private settings  (gitignored)
├── .env.demo                     # DEMO engine private settings  (gitignored)
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

Three dotenv files at the project root, one per concern, all
gitignored. The easiest path is to launch `TradeSynchronizer.app`
and fill the General / Live / Demo tabs — it auto-creates each
file and writes only the ones you actually edit. For hand-editing,
the layout is plain `KEY=VALUE` dotenv format:

- **`.env`** — settings shared by every engine: `TRADOVATE_APP_ID`,
  `TRADOVATE_APP_VERSION`, `PROXY_LISTEN_HOST`, `REPLICATION_MODE`,
  `SKIP_PROTECTIVE_STOPS`, `LOG_LEVEL`, `LOG_FILE`.
- **`.env.live`** — LIVE engine private settings: `TRADOVATE_USERNAME`,
  `TRADOVATE_PASSWORD`, `TRADOVATE_CID` (a *string*, not an integer),
  `TRADOVATE_SEC`, `TRADOVATE_ACCOUNT_ID`, `PROXY_LISTEN_PORT`
  (default `8080`), `IBKR_WATCHED_ACCOUNTS`.
- **`.env.demo`** — DEMO engine private settings: same key set as
  `.env.live`, but for the paper account. Default port `8081` so
  DEMO can run alongside LIVE.

Each engine subprocess loads `.env` first and then its env-specific
file at startup; modifying the DEMO config has zero effect on a
running LIVE engine because LIVE's file is never touched.

Get the Tradovate API key (cid + sec) from
<https://trader.tradovate.com/welcome> → *API Access*.

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

- **Header**: title + *Reload* and *Save* buttons (a `*` next to
  Save marks unsaved changes). Save is targeted — only the files
  whose tab you actually edited get rewritten, so saving a Demo
  change can't disturb a running LIVE engine on disk.
- **General tab** *(active by default)*: settings shared by both
  engines (`.env` file). Editing here marks the General bucket
  dirty.
- **Live tab** / **Demo tab**: at the top, an *ACTIVE/STOPPED* toggle
  card with a status dot, the listen port, and a single button that
  flips between ▶ *Start engine* and ■ *Stop engine*. Below, the
  form on that env's credentials, port, and IBKR account(s). A `●`
  next to the tab title in the notebook is the at-a-glance
  indicator that the engine is running.
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

The first three keys live in `.env` (shared by both engines);
`IBKR_WATCHED_ACCOUNTS` is per-engine and lives in `.env.live` /
`.env.demo`:

| Variable | Effect |
|---|---|
| `REPLICATION_MODE=mirror` *(default)* | Match the IBKR order type 1:1 (MKT→Market, LMT→Limit with same price, STP→Stop, STP LMT→StopLimit) |
| `REPLICATION_MODE=market` | Always send a Market order on Tradovate, regardless of the IBKR type — fastest sync, no missed fills |
| `SKIP_PROTECTIVE_STOPS=true` *(default)* | Don't replicate `STP` / `STP LMT` orders (they're usually protective stop-loss orders on existing IBKR positions, and TradeSyncer's followers manage their own stops) |
| `IBKR_WATCHED_ACCOUNTS` | Per-engine — set in each file. Only replicate orders from these IBKR account(s); empty = all. Typically the live file watches your live IBKR account (`U…`) and the demo file watches a paper account (`DU…`). |

## Troubleshooting

| Problem | Fix |
|---|---|
| `TradovateAuthError: HTTP 401 / Invalid credentials` | Check `TRADOVATE_USERNAME`, `TRADOVATE_PASSWORD`, `TRADOVATE_CID` (must be a string, not int), `TRADOVATE_SEC` in the relevant `.env.live` / `.env.demo` file |
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
