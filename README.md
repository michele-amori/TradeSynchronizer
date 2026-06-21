# TradeSynchronizer

> Mirrors orders placed on Interactive Brokers (via TradingView
> Desktop) onto a Tradovate LEADER account, so [TradeSyncer][]
> can fan them out to every prop-firm follower account.

[TradeSyncer]: https://www.tradesyncer.com

## ⚠️ Disclaimer — read before using

**This is an unofficial, independent project.** It is not affiliated
with, endorsed by, or supported by Interactive Brokers, Tradovate,
TradingView, or TradeSyncer. All trademarks belong to their respective
owners.

- **Real money / real risk.** This software places and mirrors live
  orders on real brokerage accounts. Trading futures involves
  substantial risk of loss. A bug, a missed fill, a connectivity
  drop, or a stop that executes on one side but not the other can
  cause real financial loss. You alone are responsible for every
  order it sends. **Validate thoroughly on demo/paper accounts first.**
- **No warranty.** Provided "as is", without warranty of any kind,
  to the extent permitted by the GPL-3.0 license (see `LICENSE`). The
  authors accept no liability for any loss or damage arising from its
  use.
- **It intercepts TLS traffic.** To read orders, TradeSynchronizer
  runs a local man-in-the-middle proxy and installs its own CA so it
  can decrypt TradingView Desktop's HTTPS traffic to the broker. Doing
  this may violate the Terms of Service of TradingView, Interactive
  Brokers, Tradovate, or your prop firm. Check those terms yourself —
  using this tool could put your accounts at risk of suspension.
- **Your jurisdiction, your responsibility.** Automated and
  cross-account order routing may be regulated where you live. Make
  sure what you're doing is permitted.

Use it only on accounts you own or are explicitly authorized to
operate, and only if you accept full responsibility for the outcome.

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
├── scripts/
│   ├── launch-tradingview.sh         # opens TV Desktop with the right --proxy-server
│   ├── check-tradovate-status.py     # READ-ONLY pre-flight health check
│   ├── install-hooks.sh / pre-commit.sh  # git hook installer + body
│   └── install_ca_cert.sh            # trusts mitmproxy CA in System.keychain
└── tradesync/
    ├── config.py                 # .env loader + validation
    ├── event_replicator.py       # neutral order event → follower place-order
    ├── order_event.py            # broker-neutral order vocabulary (see below)
    ├── order_map.py              # persistent source-id <-> follower-id map
    ├── brokers/
    │   ├── ibkr.py               # conid → symbol resolver (passive + active)
    │   ├── tradovate.py          # auth, renew, contract/find, order/placeorder
    │   ├── endpoint.py           # SourceEndpoint / FollowerEndpoint protocols
    │   ├── ibkr_endpoint.py      # IBKR SourceEndpoint + IBKR→neutral translation
    │   └── tradovate_endpoint.py # Tradovate FollowerEndpoint + neutral→TV translation
    ├── symbols/
    │   └── converter.py          # MESH2026 ↔ MESH6
    ├── proxy/
    │   ├── addon.py                # mitmproxy hooks
    │   └── ibkr_parser.py          # IBKR JSON order body decoder
    └── ui/
        └── app.py                # Tkinter GUI (Settings + Log + Start/Stop)
```

### Bidirectional-replication foundation (work in progress)

The modules `order_event.py`, `brokers/endpoint.py`,
`brokers/ibkr_endpoint.py`, `brokers/tradovate_endpoint.py`
and `proxy/ibkr_event_source_observer.py`
are the foundation for replicating in either direction (IBKR→Tradovate,
the current live path, **and** Tradovate→IBKR, now validated live). They
introduce a broker-neutral order vocabulary (`OrderEvent` / `OrderSpec`
/ `BracketSpec` / `ModifySpec`) and two role protocols — `SourceEndpoint`
(observes a broker, emits events) and `FollowerEndpoint` (executes
orders). Concrete adapters translate each broker's wire format to/from
the neutral vocabulary at their boundary.

The full Tradovate→IBKR chain now exists end to end and is unit-tested:
a `TradovateWSObserver` (source) → `EventReplicator` → an
`IbkrFollowerEndpoint` driving `IbkrApiClient` against a local IB
Gateway. The GUI has a **Replication** tab to declare source→follower
pairs (saved to `config/replication.json`), and `main.py` can start
those pipelines — but only when `TRADESYNC_ENABLE_WS_PIPELINES=1`.

When an enabled pair has IBKR as its follower, the engine opens IB
Gateway for you if it isn't already running — and deliberately leaves a
running Gateway untouched (restarting it would drop the authenticated
2FA session). Opening it only lands you on the login screen; the API is
ready once you finish logging in.

By default this is all **OFF**, and the live IBKR→Tradovate hot path is
unchanged. The push-frame parser has been calibrated against real
Tradovate order frames (captured with the market open via
`scripts/ws_spike.py`), and the full Tradovate→IBKR chain has been
validated end to end against a live IBKR account using unfillable
far-from-market limit orders (zero position risk). Covered live: NEW
(single LMT/MKT + bracket); MODIFY of the entry, stop-loss and
take-profit, on both an active bracket (entry filled) and a suspended
one (entry not yet filled); CANCEL of a single order, of individual
bracket legs, and of a whole bracket (via the entry, with IBKR's native
OCA cancelling the children); and FILL + position close. Stop-limit
orders are intentionally not validated live (not used in practice);
they remain covered by unit tests only. `scripts/ws_spike.py` remains
the tool to re-capture frames if the wire shape ever changes; it now
uses a dedicated deviceId so it can observe alongside a running engine
without knocking it off its Tradovate session (see the session note
below).

**Unified replication engine (Step C/D complete).** Every direction now
runs on the single broker-neutral `EventReplicator`; the historical
`Replicator` has been removed. For the IBKR→Tradovate hot path, an
`IbkrEventSourceObserver` presents the exact surface the mitmproxy addon
expects but translates each observed IBKR order into an `OrderEvent` and
replicates it via a `TradovateEndpoint` follower (with
`resolver.resolve_symbol` injected as the conid→symbol resolver, since
IBKR-source events carry a conid, not a symbol). There is no longer a
flag or a fallback engine: the neutral path is the only path. (Note:
because the old `Replicator` is gone, there is no one-flag rollback — see
`docs/design/replicator-unification.md`. Run a sustained DEMO of the
neutral path before any return to real-money live use.)

**OCO cascade for non-native-OCO followers.** A bracket's two exit legs
form an OCO pair: when one is cancelled or filled, the other must go
away. IBKR enforces this natively (the legs share an `ocaGroup`), but
Tradovate's `/order/placeoso` returns `ocoId=null` — its legs are
independent. So `FollowerEndpoint` now exposes a `native_oco` property
(IBKR `True`, Tradovate `False`), and `EventReplicator` simulates the
cascade itself **only when the follower lacks native OCO**: cancelling
or filling one exit leg cancels its sibling. It is a no-op when the
follower has native OCO (avoiding a redundant second cancel), and an
**entry** fill cascades nothing — that opens the position, so the exits
must stay live. The cascade never undoes the primary action if the
sibling cancel fails, and treats an already-gone sibling as success.
This OCO cascade is part of the unified `EventReplicator` that now drives
every direction, including the live IBKR→Tradovate path.

**Per-pair follower size ratio.** Each pair carries a `ratio` (default
`1.0`): the follower's order size is `round(master_size × ratio)`,
floored to 1. So a ratio of `0.33` makes a 90-contract master trade
place 30 on the follower (90 × 0.33 = 29.7 → 30), and a small trade
never rounds away to nothing (1 × 0.33 → 1, never 0). Rounding is
half-up (2.5 → 3), not Python's banker's rounding. The factor applies to
new single orders, brackets (entry and every leg), and size-changing
modifies; a price-only modify is untouched. It is validated `> 0` and
`<= 100` (the cap stops a mistyped `33` for `0.33` from 33×-ing real
exposure). The scaling rule (`scale_quantity`) is applied by the unified
`EventReplicator` for every direction. There is a **single place** to
configure it: the pair in `config/replication.json`, edited from the
Replication tab, matched by the follower's endpoint (Tradovate
env+account for the IBKR-to-Tradovate path, IBKR source). Anything unexpected (no file, no matching
pair, parse error) falls back to `1.0`, and a non-1.0 ratio is logged
loudly at boot. Because it scales **real** order sizes, validate a
non-1.0 ratio in DEMO first, and change it only while flat (it is read
at engine start).

#### Divergence safeguards

Because a copy-trader is only as safe as its weakest moment, two
safeguards run automatically whenever a Tradovate→IBKR pipeline is live:

- **Liveness watchdog** — if the Tradovate user-data feed goes silent
  (the socket stays open but no frames arrive for ~15s, e.g. another
  login took the channel), the observer forces a reconnect instead of
  sitting deaf. It self-heals; it does not by itself prevent two logins
  contending for one channel.
- **Periodic position reconciler** — every ~30s the engine queries BOTH
  brokers' net positions, normalises them to symbols (IBKR conId vs
  Tradovate contractId differ), and compares. Any divergence is logged
  as a loud `⚠ POSITION MISMATCH` warning. It is **read-only** — it
  alerts, it never trades; squaring a divergence is a human decision.
  Each pass also emits a compact `[health]` line (feed state + sync
  state) so the GUI's merged engine log shows at a glance whether the
  replica is connected and in sync.

  Two filters keep the comparison honest. The IBKR read is scoped to the
  **followed account** (a Gateway login can see several accounts;
  blending them would invent phantom mismatches) and to **futures only**
  (`secType=FUT`). The latter matters because an IBKR account can also
  hold instruments Tradovate — a futures-only venue — knows nothing
  about: a real account used in testing held several sovereign **bonds**
  (POLAND, FRTR, …) that, before this filter, showed up as a permanent
  false `POSITION MISMATCH` even while flat on futures.

#### Tradovate session contention (important operational note)

Tradovate appears to allow only **one live user-data session per
deviceId**. A second authentication with the same deviceId silently
invalidates the first — the displaced session's socket stays open but
stops receiving order frames. Keep the engine's `TRADOVATE_DEVICE_ID`
**dedicated and distinct** from anything else that logs into the same
Tradovate user (the diagnostic spike already pins its own; TradingView
Desktop's behaviour on a full restart/re-login is not yet characterised
— a tab reload was observed NOT to displace the engine). If live MODIFY
/ CANCEL events ever stop arriving, suspect session contention first and
check for a `[health] feed=DISCONNECTED` line.

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
  `TRADOVATE_APP_VERSION`, `TRADOVATE_IS_AUTOMATED`,
  `PROXY_LISTEN_HOST`, `LOG_LEVEL`, `LOG_FILE`.
- **`.env.live`** — LIVE engine private settings. `TRADOVATE_USERNAME`,
  `TRADOVATE_PASSWORD`, `TRADOVATE_ACCOUNT_ID` are written by hand (the
  GUI manages only `PROXY_LISTEN_PORT`, default `8080`, here). The GUI
  preserves the hand-written credentials verbatim across Saves. The IBKR
  source-account filter is no longer set here — it's driven by the
  **Replication** tab's pairs (see below); `IBKR_WATCHED_ACCOUNTS` is
  still honoured as a fallback if present.
- **`.env.demo`** — DEMO engine private settings: same key set as
  `.env.live`, but for the paper / second account. Default port
  `8081` so DEMO can run alongside LIVE.

The Tradovate credentials are written by hand:
1. Open `.env.live` (or `.env.demo`) in an editor.
2. Set `TRADOVATE_USERNAME`, `TRADOVATE_PASSWORD`, and
   `TRADOVATE_ACCOUNT_ID`. For the account id you may use EITHER the
   internal numeric id OR the account name you see in the Tradovate UI
   (numeric like `19000001`, or alphanumeric like `DEMO3701228`) — the
   engine resolves a name to the internal id at startup via
   `/account/list`.
3. Save the file. The engine reads these values on its next start.

(The GUI no longer edits these three fields, and the old "Sign in &
pick account" picker has been removed — name resolution that the picker
used to do now happens automatically in the engine at connect.)

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

These keys live in `.env`, shared by every engine. The unified `EventReplicator` always replicates the true order type faithfully (Market, Limit, Stop, StopLimit); there is no longer a global order-type override.

| Variable | Effect |
|---|---|
| IBKR source-account filter | Which IBKR account(s) to replicate orders FROM. Now driven by the **Replication** tab: each enabled IBKR-source pair contributes its source account. A legacy `IBKR_WATCHED_ACCOUNTS` value in an env file is still honoured as a fallback when no IBKR-source pairs are configured; empty = all. |
| `TRADOVATE_IS_AUTOMATED=false` *(default)* | Value put into the `isAutomated` field of every Tradovate order payload. Default OFF because trade-copier services (incl. TradeSyncer) typically filter out algorithmic orders by default — leaving this ON would let the leader account fill but silently skip the leader→follower fanout. Flip ON only if your upstream is genuinely autonomous and you want orders labelled as such for regulatory/reporting purposes. |

### Multiple followers (one source → N followers)

A single source can fan out to several follower accounts at once. In
the **Replication** tab, add one enabled pair per follower, all sharing
the same source. Each pair carries its own replication `ratio` (so one
follower can mirror full size and another half size) and, for IBKR
followers reached through separate logins, its own gateway `host` /
`port` / `client_id` — letting each follower run on its own IB Gateway
instance. Config validation rejects two enabled pairs that point at the
same follower identity, which would otherwise double-trade it. See
[`docs/design/multi-follower.md`](docs/design/multi-follower.md) for the
design and the per-follower gateway model.

#### Reusable accounts

When the same account appears in several pairs (a fan-out source, say),
you don't retype its broker / env / id each time. The **Accounts**
section at the top of the Replication tab is a small address book: add
each account once under a friendly label, and the pair form's Source /
Follower dropdowns then list those labels. Picking one copies its fields
into the pair. The book is saved to `config/accounts.json` (gitignored;
see `config/accounts.json.example`) and is a **GUI convenience only** —
the engine never reads it, and pairs keep their own copy of the fields,
so the book can be changed or deleted without affecting replication.

Add / Edit / Remove sit beside the account list. An account still
referenced by a pair can't be **deleted** (the GUI names the pair using
it), and **editing** such an account is restricted to its label only —
broker / env / id are frozen, because each pair holds its own copy of
those fields and changing them here would silently desync the pair.
Accounts not used by any pair can be edited freely. To change the
broker / env / id of an in-use account, edit or remove those pairs
first.

#### Grouped view + bulk follower add

The pairs are shown as a tree grouped by master: each source account is
a top-level row ("Master — N followers") with its followers nested under
it, so a fan-out is read at a glance instead of scanning a flat list.
Select a follower row to Edit / Enable-Disable / Remove that pair; the
master rows are just headers.

To mirror one master onto several followers, the add form takes multiple
follower rows — click **+ follower** to add a row, pick the account and
its own ratio on each, then **Add pair(s)**. Each follower becomes its
own ordinary pair (master ↔ 1 follower); there is no new multi-follower
object, so the `replication.json` schema and the engine are unchanged —
it's purely an input shortcut. Pair names are generated as
"`<name> – <follower label>`" to stay unique. A follower already paired
with that master is skipped (the GUI lists which), and the rest are
still added.

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
| IBKR modify/cancel orders silently ignored | Pre-fix: `_SINGLE_ORDER_PATH_RE` expected `/orders/{id}` (plural), but IBKR's real URL pattern is asymmetric — `POST /orders` (plural) for placement and `POST` / `DELETE /order/{id}` (**singular**) for modify and cancel. The regex now matches `/order/{digits}$`, excluding the `/order/whatif` preview endpoint, so every modify/cancel sent by TradingView Desktop is captured. Discovered empirically by running the proxy against live TV traffic during calibration. |
| IBKR cancel ignored when DELETE URL has a query string | Pre-fix: the `\d+$` anchor in `_SINGLE_ORDER_PATH_RE` rejected URLs like `DELETE /order/1398750350?manualIndicator=true` — which is exactly what TradingView Desktop sends on every cancel from the chart's right-click menu (POST modify URLs don't have the suffix, only DELETE). Pattern now ends with `(?:\?.*)?$` and is tolerant of any future query parameters. Test `tests/test_ibkr_parser.py::TestCancelRequest::test_matches_delete_with_query_string` guards the regression. |
| `launch-tradingview.sh` silently kills TV without relaunching | Pre-fix: the script used `${TARGET^^}` (bash-4 uppercase expansion) for the banner, which fails on macOS's default bash 3.2 with "bad substitution" — and the failure happened AFTER the kill step but BEFORE the relaunch, leaving TV closed. Replaced with `tr '[:lower:]' '[:upper:]'` for bash 3.2 compatibility. |
| `❌ Replication failed: Could not resolve conid=…: no IBKR bearer token captured yet` | TradingView Desktop authenticates to api.ibkr.com with OAuth 1.0a, NOT a Bearer token; we can't replay OAuth-signed requests (we don't have TV's consumer secret), so the active-resolve fallback (`GET /contract/{conid}/info` with auth) never works against TV traffic. The PASSIVE path still works — open or refresh the chart for the contract once and the `/info` response observed by the proxy will populate the cache. Pre-fix log said `captured Bearer token, len=264` on every TV request, which was a false positive — we logged unconditionally but `capture_token()` silently rejected OAuth. Now the log distinguishes `Bearer captured` from `OAuth 1.0a header seen (not replayable)` so the operator knows which mode is actually in play. |
| Passive symbol cache never gets populated even though `/info` is observed | Pre-fix: `observe_contract_info()` ran `json.loads()` directly on `flow.response.content` and swallowed every `ValueError` silently. In practice IBKR `/info` responses arrive gzip-compressed (magic bytes `1F 8B` confirmed on the wire) and — in the mitmproxy version we're running — `flow.response.content` was passing through the raw compressed bytes for this endpoint, so JSON parsing failed silently on every single response. Symptom: zero `IBKR contract observed: conid=… → SYMBOL` log lines across multiple test sessions; every order fell back to "Could not resolve conid=…". Fix in `observe_contract_info`: detect the gzip magic prefix and decompress manually before parsing, plus a DEBUG log when JSON parsing or symbol extraction fails so the next regression is visible instead of silent. Test `tests/test_perf_optimizations.py::TestGzipBodyDecompression` guards this. |
| `JSON parsed but no symbol extracted; keys=['…', 'local_symbol', 'expiry_full', …]` | Pre-fix: `_extract_symbol()` only knew the camelCase IBKR client API schema (`localSymbol`, `expirationDate`). The IBKR Client Portal API used by TradingView Desktop returns snake_case (`local_symbol`, `expiry_full`), so the function returned None on every real /info payload. Fix: accept both naming conventions, prefer `local_symbol`/`localSymbol` as the authoritative short form (no month-code arithmetic needed), fall back to `symbol`+`expiry_full`/`expirationDate`/`expiry` reconstruction. Test `tests/test_perf_optimizations.py::TestExtractSymbolSchemas` covers the real-world key set captured from conid=770561201 plus the legacy schemas, so future API drift in either direction stays caught. |
| Engine connects OK but every `placeOrder` fails with "unknown account" on Tradovate | Pre-fix: `connect()` accepted `TRADOVATE_ACCOUNT_ID` verbatim and assigned it to `self._account_id` without ever calling `/account/list`. Most users see the account number (the `name` field, e.g. a prop-firm-assigned id like "NNNNNNN") in the Tradovate UI and naturally put THAT into `.env.<env>`. But `placeOrder` consumes Tradovate's internal numeric `id` (a separate field), not the name. Fix: `connect()` now hits `/account/list` and runs `_resolve_pinned_account()` to translate either form (id OR name) to the canonical internal id, raising `TradovateAuthError` with a listing of available accounts if neither matches. So you can paste whatever Tradovate shows you in the UI and it just works. |
| `TRADOVATE_DEVICE_ID` set in `.env.<env>` but Tradovate keeps prompting for MFA / treats every restart as a new device | Pre-fix: the env var was read nowhere — `Config.load()` had fields for username/password/cid/sec/app_id/version but not device_id, so the `TradovateClient` constructor's `device_id=None` default kicked in and `__init__` generated a fresh `uuid4()` per process. Tradovate's anti-fraud heuristics then saw "new device" on every engine restart. Fix: `Config.load()` reads `TRADOVATE_DEVICE_ID` (defaulting to empty string), and `main.py` passes it to the client as `device_id=cfg.tradovate_device_id or None` so empty still falls back to uuid4 (preserves old behaviour when the env var is absent). |
| `Tradovate modifyorder failed: HTTP 400: Invalid JSON: missing required field "orderType"` | First live divergence captured against the real Tradovate API: a BUY LMT placed correctly on Tradovate, then a price-only modify in TradingView (30180.25 → 30180.50) failed because the `modifyorder` payload omitted `orderType`. Tradovate's `/order/modifyorder` requires `orderType` on every call (unlike IBKR's modify, which infers the type from the existing order). Fix in 3 places: (1) `IbkrOrderModify` parser now extracts `order_type` from TradingView's modify body — TV does send it, we just weren't reading it; (2) `Replicator.replicate_modify` maps IBKR `LMT`/`STP`/`STP LMT`/`MKT` → Tradovate `Limit`/`Stop`/`StopLimit`/`Market` and passes it through; (3) `TradovateClient.modify_order` now requires `order_type` as a keyword arg and includes it as `orderType` in the JSON payload. Test `tests/test_replicator_cancel_modify.py::test_modify_without_order_type_is_rejected` guards the regression. |
| `Tradovate placeoso failed: HTTP 400: {"violations":[{"constraint":"stopPrice","value":"None","description":"Stop Price should be specified"}]}` on a bracket whose SL leg is an `STP` | TradingView's bracket-child payload is asymmetric with its own STP modify payload. For STP/STP LMT children of a bracket, TV puts the stop TRIGGER in `price` and OMITS `auxPrice` entirely; for standalone STP modifies, it sends `price == auxPrice == trigger`. Pre-fix, the parser stored `aux_price=None`, the replicator's `place_bracket()` passed `stop_price=None` to Tradovate, and Tradovate rejected the OSO. Fix in `ibkr_parser.py::parse_ibkr_order`: when a bracket child has `orderType == STP` and `auxPrice` is absent, the trigger from `price` is moved to `aux_price` and `price` is cleared (pure STP has no limit). For `STP LMT` with no `auxPrice`, the trigger is mirrored from `price → aux_price` (the resulting StopLimit ends up with limit == trigger, which is better than HTTP 400). Tests `tests/test_ibkr_parser.py::TestBracketParsing::test_stp_child_trigger_in_price_field_normalises_to_aux` + 3 sibling cases guard the regression. |
| `Tradovate modifyorder failed: HTTP 400: ...Stop Price should not be specified` on a Limit-order modify (symmetric "stray field" rejection) | TradingView's modify body for a LIMIT order always includes `"auxPrice": 0` — literal zero, NOT null, NOT absent — captured live during calibration as the cause of repeated HTTP 400s on qty changes. Pre-fix the replicator forwarded `aux_price=0.0` as `stop_price=0.0` in the modifyorder payload, and Tradovate (correctly) refused: a Limit order shouldn't carry a stop price. Symmetric foot-gun on the Stop side: TV sends `price == auxPrice == trigger` on STP modifies, but Tradovate's Stop order has no limit_price slot. Fix in `Replicator.replicate_modify`: gate `limit_price` and `stop_price` by the target Tradovate `tv_order_type` (Limit/StopLimit can carry limit; Stop/StopLimit can carry stop; Market carries neither), matching the same field rules that `place_order` already enforces. Tests `tests/test_replicator_cancel_modify.py::test_lmt_modify_does_not_forward_auxprice_zero` + 3 sibling cases (lmt_with_aux_set, stp_does_not_forward_price, stplmt_keeps_both) guard the regression. |
| Engine looks fine but every Tradovate placement gets `HTTP 400 Rejected` and you don't know why | The account may be in `liquidateOnly` mode — typically because a user-configured Max Daily Loss threshold (Tradovate's `accountRiskStatus.userTriggeredLiqOnly = true`) has been hit and auto-liquidated the account, or because the prop firm imposed a restriction. The engine can't distinguish "my payload is wrong" from "the account is locked" — both look like HTTP 400. Run `.venv/bin/python scripts/check-tradovate-status.py --env <live\|demo>` BEFORE launching the engine to surface any block explicitly. It hits `accountRiskStatus/list`, `cashBalance/getCashBalanceSnapshot`, `order/list`, `position/list` and a handful of other endpoints — all read-only, no orders placed — and prints `liquidateOnly` timestamps, today's P&L vs the daily threshold, recent order statuses (Filled / Rejected / Canceled), and the resolved account id. Captured the first Max-Daily-Loss block during calibration this way without sending a single order. See script docstring for full details. |

| Engine subprocess spawned by the GUI dies immediately with `ImportError: ... (mach-o file, but is an incompatible architecture (have 'arm64', need 'x86_64'))` on `from mitmproxy import http` / `cryptography` / `_cffi_backend` (Apple Silicon only) | The .app, when launched from Finder / Launchpad / Dock, is started by launchd UNDER ROSETTA (verifiable via `vmmap <pid>` → `Code Type: X86-64 (translated)`) because the bundle is unsigned and macOS makes that choice opaquely. The GUI itself runs fine that way — it's only the engine subprocess that needs to be arm64 (to dlopen the arm64-only native wheels: cryptography, cffi via `_cffi_backend`, mitmproxy TLS). Fix in `ProxyController.start`: prefix the subprocess argv with `/usr/bin/arch -arm64` on Apple Silicon hardware. The detection has a subtle gotcha: `platform.machine()` and `os.uname().machine` BOTH return `x86_64` when the caller is itself running under Rosetta — they reflect the process arch, not the hardware. The correct test is `sysctl -n hw.optional.arm64 == "1"`, which always returns truthy on Apple Silicon CPUs regardless of caller arch. The earlier attempt to wrap `arch -arm64` around the .app launcher itself broke launchd with exit code 126 (a shell-script-as-arch-target edge case), and is no longer used. |

### Pre-flight: check Tradovate account health before launching

The replicator can't tell whether a Tradovate HTTP 400 means "my payload
is malformed" or "your account is locked" — both look the same from the
engine's perspective. Before starting a LIVE session, run:

```bash
.venv/bin/python scripts/check-tradovate-status.py --env live
# or: --env demo
# or: -v   to include DEBUG-level client logs
```

The script is **strictly read-only** — every call it makes is either GET
or POST-with-empty-body to lookup endpoints, never `placeorder` /
`cancelorder` / `modifyorder` / `placeOSO`. It surfaces:

  * **Auth + account resolution:** credentials work, and the pinned
    `TRADOVATE_ACCOUNT_ID` resolves to a real internal id (handles the
    id-vs-name foot-gun).
  * **Contract lookup:** `/contract/find` works for the symbols you trade
    (sample probe with MES; refine as needed in `_probe_contracts`).
  * **Account health:** today's realised P&L, open P&L, week realised
    P&L, total cash value, margin requirements.
  * **Lock status:** `accountRiskStatus/list` shows `liquidateOnly` +
    `userTriggeredLiqOnly` + `autoLiqCounter` — if these are set the
    account is in liquidate-only mode and every new placement WILL be
    rejected by Tradovate until the lock clears (typically the next
    prop-firm daily reset).
  * **Recent order tape:** last `Filled` / `Rejected` / `Canceled` orders
    + intraday positions, so you can see end-state without opening the
    Tradovate web UI.

Exit codes:

| Code | Meaning |
|------|---------|
| `0` | All probes ran, no orders placed |
| `2` | Shadow mode active (credentials missing) — refused to run |
| `3` | Tradovate authentication failed |
| `4` | Unexpected error during `connect()` |
| `5` | Unexpected error during `list_accounts()` |

Anything > 0 means **stop and investigate before launching the engine.**

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
   failure. Currently 653 tests, ~6 s wall.
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
.venv/bin/python -m unittest discover tests              # ~6s, 653 tests
.venv/bin/python -m coverage run --source=tradesync \
    -m unittest discover tests
.venv/bin/python -m coverage report --skip-empty
```

The suite runs with **no configuration** — you don't need credentials
or a broker connection to run it. A couple of tests exercise
`Config.load()`, which reads a real `.env.<env>` file; on a fresh clone
those files don't exist yet, so those tests **skip themselves** (you'll
see `skipped=2`) rather than fail. Once you've created your `.env.demo`
/ `.env.live` (see *One-time setup*), they run normally.

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
