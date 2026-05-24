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

### 2. Register the app once with Tradovate

Tradovate distinguishes between **application credentials** (cid +
sec — identify TradeSynchronizer itself as an app to their REST
API) and **user credentials** (username + password — identify the
human running it). Application credentials are app-wide; user
credentials are per-engine (LIVE vs DEMO).

1. Sign in to Tradovate at <https://trader.tradovate.com>. Any
   Tradovate account works for this step — a free Demo account is
   enough, and crucially you don't need an "API plan" or a
   prop-firm account. The app you're registering can later
   authenticate against ANY Tradovate user (your personal account
   or a prop-firm sub-account like Apex / TopStep / MFFU).
2. Click your user icon → *API Access* → *Register an App*. Give
   it any name and version.
3. Tradovate returns a **cid** and a **sec**. Copy them.
4. Create your local credentials file:
   ```bash
   cp tradesync/_app_credentials.py.example tradesync/_app_credentials.py
   ```
   Paste cid and sec into the two variables. The real file is
   gitignored, so your secret never lands in a commit.

### 3. Configure per-engine settings

Three dotenv files at the project root, all gitignored:

- **`.env`** — settings shared by every engine: `TRADOVATE_APP_ID`,
  `TRADOVATE_APP_VERSION`, `PROXY_LISTEN_HOST`, `REPLICATION_MODE`,
  `SKIP_PROTECTIVE_STOPS`, `LOG_LEVEL`, `LOG_FILE`.
- **`.env.live`** — LIVE engine private settings: `TRADOVATE_USERNAME`,
  `TRADOVATE_PASSWORD`, `TRADOVATE_ACCOUNT_ID`, `PROXY_LISTEN_PORT`
  (default `8080`), `IBKR_WATCHED_ACCOUNTS`.
- **`.env.demo`** — DEMO engine private settings: same key set as
  `.env.live`, but for the paper / second account. Default port
  `8081` so DEMO can run alongside LIVE.

The easiest workflow:
1. Launch `TradeSynchronizer.app`.
2. In the Live (or Demo) tab, fill *Username* and *Password*.
3. Click **Sign in & pick account**. The dialog authenticates with
   Tradovate, lists every account visible to the user (including
   any prop-firm sub-accounts), and lets you pick which one to
   pin as this engine's LEADER. The picked numeric id lands in the
   Account ID field automatically.
4. Save. Done.

Each engine subprocess loads `.env` first and then its env-specific
file at startup; modifying the DEMO config has zero effect on a
running LIVE engine because LIVE's file is never touched.

### 4. Trust the mitmproxy CA on macOS

The proxy intercepts HTTPS traffic from TradingView; without this
step TradingView Desktop will refuse the proxy's TLS certificate
when talking to `api.ibkr.com` and you'll see a generic SSL error
in its UI. Run the helper script (idempotent — safe to run again):

```bash
./scripts/install_ca_cert.sh
```

It bootstraps the CA via mitmproxy if it doesn't exist yet, then
calls `sudo security add-trusted-cert` to install it in
`/Library/Keychains/System.keychain` with `trustRoot` semantics.
You'll be asked for your macOS password once.

Verify the install at any time without modifying anything:

```bash
./scripts/install_ca_cert.sh --check
```

If you'd rather do it by hand:

```bash
mitmdump --listen-port 18080 -q & sleep 2 && kill %1   # generates CA
sudo security add-trusted-cert -d -r trustRoot \
     -k /Library/Keychains/System.keychain \
     ~/.mitmproxy/mitmproxy-ca-cert.pem
```

You'll only do this once. Every engine startup runs a pre-flight
check (see `tradesync/preflight.py`) and logs a clear warning if
the CA isn't trusted — no more guessing at TLS errors.

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
`~/Library/Logs/TradeSynchronizer/tradesync.log` and the tag tells
them apart; the file is rotated automatically at 5 MB):

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
| `TradovateAuthError: HTTP 401 / Invalid credentials` | Check `TRADOVATE_USERNAME` / `TRADOVATE_PASSWORD` in the relevant `.env.live` / `.env.demo`, and `APP_CID` / `APP_SEC` in `tradesync/_app_credentials.py`. |
| `MissingAppCredentialsError` at startup | Run §2 of *One-time setup* — register the app at Tradovate and populate `tradesync/_app_credentials.py`. |
| `Could not resolve conid=… not in cache` | Open the chart for that symbol in TradingView once; the contract `/info` response will be observed and cached. Active fallback also works once an IBKR token has been captured. |
| `Contract 'MESH6' not found on Tradovate` | The symbol resolver produced a symbol Tradovate doesn't recognise. Check the log line "Symbol map: conid=… → IBKR='…' → Tradovate='…'" and verify against Tradovate's contract list. |
| TradingView doesn't go through the proxy | Quit TradingView completely, then relaunch with `open -a TradingView --args --proxy-server=127.0.0.1:8080`. Chromium-based apps only read the flag at launch. |
| `SSL: CERTIFICATE_VERIFY_FAILED` from TradingView | Run `./scripts/install_ca_cert.sh --check` to confirm the CA isn't trusted, then `./scripts/install_ca_cert.sh` to install. |

## Order lifecycle: cancellations & modifications

TradeSynchronizer mirrors the full lifecycle of an IBKR order, not
just placement:

  * `POST   /orders`                  → places a new replica on Tradovate
  * `DELETE /orders/{ibkr_order_id}`  → cancels the replica
  * `POST/PUT /orders/{ibkr_order_id}` → modifies the replica
    (new price, stop, quantity, or TIF)

To make this possible the addon captures the IBKR-assigned
`order_id` from each new-order POST response and stores the
mapping `cOID ↔ IBKR id ↔ Tradovate orderId` in a small JSON file
(per environment):

```
.tradesync-state/orders-live.json
.tradesync-state/orders-demo.json
```

The file is gitignored and written atomically (tempfile + rename),
so it survives a TradeSynchronizer restart while orders are still
open. Successful cancels delete their entry; `OrderNotFound` from
Tradovate (the order already filled or was cancelled out-of-band)
is logged as a skip and the map entry is tidied up.

**Startup reconciliation.** On every engine start (right after
authenticating with Tradovate), TradeSynchronizer walks the map
and calls `GET /order/item?id={tv_id}` for each known Tradovate
order. Entries whose status is no longer active (Filled,
Cancelled, Rejected, Expired, or 404) are pruned. Transient errors
(HTTP 503, network blip) leave the entry untouched on the
assumption "a stale-but-recoverable map beats wiping valid
mappings on a flake". The Log tab summarises with
`Reconciliation complete: N kept, M pruned, K errors`.

### Bracket / OCO orders

A bracket order (entry + take-profit + stop-loss, OCO-linked)
arrives as a multi-leg `orders` array where the exit legs carry
`parentId` referencing the entry's `cOID`. The parser detects that
structure and the replicator forwards the whole group to
Tradovate's `/order/placeoso` (Order Sends OCO) — entry as the
parent, the two exits as `bracket1` / `bracket2`. After placement
each leg gets its own entry in the order map, so cancelling or
modifying any individual leg from TradingView later (e.g. raising
the take-profit price) propagates to the matching Tradovate leg.

`SKIP_PROTECTIVE_STOPS` does NOT apply to bracket children — the
stop-loss leg of a bracket is part of the coordinated structure and
gets replicated together with entry + TP regardless of that flag.

### When Tradovate rejects a replication

If Tradovate refuses an order (insufficient margin, unknown symbol,
rate limit, …) the divergence is surfaced in three places at once,
so missing it requires actively ignoring all three:

1. **Desktop notification** via `osascript` — a Notification Center
   banner titled "TradeSynchronizer LIVE/DEMO: order rejected" with
   the rejection reason. Survives in Notification Center for review
   later if you were AFK.
2. **Tab title** in the GUI gets a ⚠ suffix (e.g. `Live  ●  ⚠`) so
   you see the warning from any tab. Combined with the running-dot
   indicator, the four combinations are: clean, running, error,
   running with error.
3. **Sync-health panel** inside the Live/Demo tab shows a red
   counter, the most recent failure summary, and an
   "Acknowledge & clear" button. Acknowledging only resets the
   indicator — it does NOT auto-recover the Tradovate position;
   reconcile manually first if the divergence matters.

The structured `DIVERGENCE {json}` event is also persisted to the
rotating log file, so you can audit failures days later.

**Empirical disclaimer.** The exact field names of Tradovate's
`placeoso` response (`oso1Id` / `bracket1Id` / `orderIds[]`) and
of IBKR's multi-leg POST response are educated guesses based on
common API patterns — not yet verified against live traffic. On
the first real bracket replication, expect either a clean success
or a clearly logged failure: the full Tradovate response body is
dumped on any error so the parser can be calibrated quickly.

## What it does NOT do

- **Does not place orders directly on prop-firm accounts**. That's
  TradeSyncer's job; this tool only feeds its LEADER account.
- **Does not run on Linux / Windows out of the box**. The keychain
  trust command is macOS-specific; everything else is portable.
