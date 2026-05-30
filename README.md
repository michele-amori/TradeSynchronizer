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

**Auto (default).** Just press **▶ Start engine** in the GUI. The
*AUTO_LAUNCH_TRADINGVIEW* checkbox in the General tab (ON by
default) makes the engine also launch TradingView Desktop with the
right `--proxy-server` flag once its mitmproxy port is accepting
connections. If TV is already running on the wrong proxy port (or
no proxy at all) it gets quit and relaunched automatically.

The auto-launcher (`tradesync/tradingview_launcher.py`) handles
all four gotchas internally: TV already running, wrong-port
mismatch, CA-not-trusted (passes `--ignore-certificate-errors` as
a safety net), and proxy not yet bound (waits up to 8 s before
giving up). The Log tab gets a one-line summary:

    [DEMO] 🚀 Launched TradingView with proxy.
    [DEMO] ✓ TradingView already running on the right port.
    [DEMO] 🔄 Restarted TradingView with proxy.
    [DEMO] ⚠ Proxy port wasn't ready in time — TradingView was NOT launched.

**Manual fallback.** Untick the checkbox if you'd rather manage
TV yourself. Two ways to do it from the CLI:

```bash
# Wrapper script with pre-flight checks (CA trust, port readiness):
./scripts/launch-tradingview.sh demo   # → DEMO engine on :8081
./scripts/launch-tradingview.sh live   # → LIVE engine on :8080
./scripts/launch-tradingview.sh --check  # diagnostics only

# Bare command:
osascript -e 'quit app "TradingView"'     # if it's running
open -a "TradingView" --args --proxy-server=127.0.0.1:8081
```

Why the quit-first dance: Chromium-based apps (TradingView is
Electron 38 under the hood) only read `--proxy-server` at launch
time. If TV is already running, `open -a` just brings the focus
to the existing instance — the flag is silently ignored. The
in-app launcher and the wrapper script both handle this for you.

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

## Verbose troubleshooting mode (default ON)

While calibrating the system against real Tradovate + real
TradingView Desktop traffic, the `VERBOSE_TROUBLESHOOTING` flag in
the General tab (default ON) cranks up the diagnostics so any
parsing/replication mismatch is visible from the log:

* All `tradesync.*` loggers run at **DEBUG** — every Tradovate HTTP
  request and response is dumped to the rotating log file
  (`~/Library/Logs/TradeSynchronizer/tradesync.log`).
* The proxy registers an additional `TrafficLoggerAddon` that
  watches all flows passing through mitmproxy. Three tiers:
  - **api.ibkr.com**: full dump — method, URL, headers, request &
    response bodies (pretty-printed JSON, capped at 16 KB).
  - **tradingview.com / charts.tradingview.com / unknown hosts**:
    one-line summary — method, URL, status, body length.
  - **Telemetry / analytics / sentry**: silently dropped.

Each mitmproxy flow has an 8-char tag (`flow.id[:8]`) prefixed to
its log lines so you can `grep` the file for one transaction's
complete TV→IBKR + IBKR→TV pair.

Third-party loggers (`mitmproxy.*`, `urllib3`, `asyncio`) are NOT
elevated — they stay at INFO so the diagnostic signal stays clean.

When the first replicated trades have been verified to work
end-to-end, untick the checkbox (or set `VERBOSE_TROUBLESHOOTING=false`
in `.env`) and restart the engines: logs drop back to normal.

## Shadow mode (no Tradovate credentials yet)

If you haven't registered the app at `trader.tradovate.com → API
Access` yet — or you just want to validate the IBKR-side
interception before going live — TradeSynchronizer happily boots
in **shadow mode**: it intercepts every IBKR order, parses it, and
logs in detail what it WOULD have sent to Tradovate, without
actually placing any Tradovate orders.

Shadow mode kicks in automatically whenever ANY of these is
missing or empty:

* `APP_CID` / `APP_SEC` in `tradesync/_app_credentials.py`
* `TRADOVATE_USERNAME` / `TRADOVATE_PASSWORD` in the per-engine
  `.env.live` / `.env.demo`

The startup log makes the state unambiguous:

    🔮 SHADOW MODE — Tradovate credentials are not configured.
    The proxy will intercept and log every IBKR order, but no
    real Tradovate orders will be placed.

Every Tradovate-bound call that the replicator would have made
gets logged with a `🔮 SHADOW:` prefix and the full would-have-
sent payload:

    🔮 SHADOW: would GET /contract/find?name=MESH6 → returning fake contract_id=9000001
    🔮 SHADOW: would POST /order/placeorder → returning fake order_id=9000002
        payload: {"accountId": 999999, "action": "Buy", "symbol": "MESH6", "contractId": 9000001, …}
    🔮 SHADOW: would POST /order/cancelorder id=9000002 → pretending it succeeded

The fake ids start at 9_000_000 (visibly distinct from real
Tradovate ids) and increment monotonically so multiple orders
don't collide in the OrderMap or in the log.

To exit shadow mode: register the app at Tradovate, fill in
`_app_credentials.py` and the relevant `.env.<env>` file, then
restart the engine. The TradovateClient detects the credentials
on its next `connect()` and switches to live replication
automatically — no code change, no flag to flip.

## Troubleshooting

| Problem | Fix |
|---|---|
| `TradovateAuthError: HTTP 401 / Invalid credentials` | Check `TRADOVATE_USERNAME` / `TRADOVATE_PASSWORD` in the relevant `.env.live` / `.env.demo`, and `APP_CID` / `APP_SEC` in `tradesync/_app_credentials.py`. |
| `MissingAppCredentialsError` at startup | Run §2 of *One-time setup* — register the app at Tradovate and populate `tradesync/_app_credentials.py`. |
| `Could not resolve conid=… not in cache` | Open the chart for that symbol in TradingView once; the contract `/info` response will be observed and cached. Active fallback also works once an IBKR token has been captured. |
| `Contract 'MESH6' not found on Tradovate` | The symbol resolver produced a symbol Tradovate doesn't recognise. Check the log line "Symbol map: conid=… → IBKR='…' → Tradovate='…'" and verify against Tradovate's contract list. |
| TradingView doesn't go through the proxy | Run `./scripts/launch-tradingview.sh demo` (or `live`) — it quits any running instance first and re-opens with the proxy flag. Chromium-based apps only read `--proxy-server` at launch. |
| `SSL: CERTIFICATE_VERIFY_FAILED` from TradingView | Run `./scripts/install_ca_cert.sh --check` to confirm the CA isn't trusted, then `./scripts/install_ca_cert.sh` to install. |
| "Unsaved changes" dialog on every engine start | Pre-fix: a boolean field added to `GENERAL_FIELDS` (e.g. `AUTO_LAUNCH_TRADINGVIEW`, `VERBOSE_TROUBLESHOOTING`) wasn't being written to `.env` by `EnvStore._build_shared()`, so each boot showed permanent drift between memory and disk. Fixed; the test `tests/test_env_store.py::TestFieldsAreActuallySerialized` now guards every `GENERAL_FIELDS` / `PER_ENV_FIELDS` key against this class of bug. If you ever see it again, that test catches it before commit. |
| Pre-commit hook hangs forever after "README freshness check" | The hook prompts via `/dev/tty` when staged code changes don't touch `README.md`. In non-interactive contexts (CI, MCP tool runners) the read used to block indefinitely; it's now capped at a 30 s timeout and gated on `[[ -t 1 ]]`, so it falls through with a warning instead of stalling. Use `--no-verify` to bypass the whole hook if needed. |

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

## Performance characteristics

The replication critical path (a TradingView order entering the
proxy → a Tradovate order placed) is dominated by the latency of
two HTTPS round-trips to Tradovate (~50–200 ms each on a healthy
link). Everything else in the in-process path is sub-millisecond
and has been kept that way on purpose:

- **Contract id cache** in `TradovateClient._contract_id_cache`:
  the first order for a symbol resolves the Tradovate contract id
  (one extra HTTP call); subsequent orders on the same symbol skip
  that call entirely.
- **conid → symbol cache** in `IbkrContractResolver._symbol_cache`:
  populated passively from `/info` response bodies that TradingView
  itself requests when rendering the chart, so by the time an order
  POST hits the proxy the symbol is usually already resolved.
- **Persistent HTTP connection** to Tradovate via `requests.Session`
  with keep-alive — saves TLS handshake on every order.
- **OrderMap batched writes** (`OrderMap.batch()` context manager):
  the bracket replication path coalesces up to 3 JSON file writes
  (entry + 2 children) into a single disk flush, saving ~2 ms per
  bracket on SSD. Same mechanism applies to any sequence of
  mutations the caller groups together.
- **Notifications fire-and-forget**: desktop notifications spawn
  `osascript` via `subprocess.Popen` with discarded stdio and
  never block the replicator thread.
- **`_coids_by_flow` housekeeping**: bracket↔response cOID lookups
  are evicted from the in-memory dict in the response hook, so the
  proxy doesn't accumulate state across the trading day.

For personal day-trading scale (≤ ~100 orders/day) this is
comfortable. If the workload ever scaled to hundreds of orders per
second, the natural next step would be a `ThreadPoolExecutor` for
the replication workers and an `async` HTTP client — but neither
is needed at current scale, so neither is wired in.

## Development

### Pre-commit hook

A versioned pre-commit hook lives at `scripts/pre-commit.sh` and
runs three checks on every `git commit`:

1. **Tests** (`python -m unittest discover tests`) — blocks on
   failure. Currently 175 tests, ~0.15 s wall.
2. **`.app` rebuild** (`./build_app.sh`) — blocks if the bundle
   doesn't build (catches stale imports, broken shebangs, etc.).
3. **README freshness** — warns and prompts if the staged diff
   touches `tradesync/`, `main.py`, `gui.py`, `build_app.sh`,
   `requirements.txt` or `scripts/`, but leaves `README.md`
   unmodified. Answer `y` to proceed anyway; anything else aborts.

Install once per clone:

```bash
./scripts/install-hooks.sh
```

It symlinks `scripts/pre-commit.sh` into `.git/hooks/pre-commit`
(re-runnable to refresh). Bypass for a single commit (e.g. WIP
push) with `git commit --no-verify`.

### Test + coverage

```bash
.venv/bin/python -m unittest discover tests              # ~0.15s, 175 tests
.venv/bin/python -m coverage run --source=tradesync \
    -m unittest discover tests
.venv/bin/python -m coverage report --skip-empty
```

Current coverage of the core business logic (parser, replicator,
order map, symbol converter, traffic logger, preflight, notify,
launcher) sits at **85–100 %**. HTTP boundaries (`brokers/ibkr.py`,
`brokers/tradovate.py`) and the Tkinter UI (`ui/app.py`) are
deliberately lower because integration coverage there requires
mitmproxy `HTTPFlow` fixtures and Tk-on-CI plumbing that aren't
worth the maintenance burden at this scale.

## What it does NOT do

- **Does not place orders directly on prop-firm accounts**. That's
  TradeSyncer's job; this tool only feeds its LEADER account.
- **Does not run on Linux / Windows out of the box**. The keychain
  trust command is macOS-specific; everything else is portable.
