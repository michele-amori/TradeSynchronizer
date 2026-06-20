# Design — Tradovate→IBKR replication (the broker-neutral direction)

Status: **IMPLEMENTED and validated live.** This is the reverse
direction added by the bidirectional work: orders placed on a Tradovate
account are mirrored onto an IBKR account. It runs on the broker-neutral
`EventReplicator` and a WebSocket observer, assembled by `wiring.py`. It
is **OFF by default**, gated by `TRADESYNC_ENABLE_WS_PIPELINES`, so the
historical IBKR→Tradovate hot path is never affected unless the user
opts in.

## What it does

The user (or their strategy) trades on Tradovate; those orders are
mirrored onto IBKR. Where the IBKR→Tradovate path is driven by
intercepting HTTPS traffic, this direction has a real push feed —
Tradovate's user-data WebSocket — so no proxy is involved.

## How the master is observed: the Tradovate WebSocket

`TradovateWSObserver` (tradesync/brokers/tradovate_ws_observer.py)
connects to `wss://<env>.tradovateapi.com/v1/websocket`, authorizes,
sends `user/syncrequest`, and ingests the order snapshot, then listens
for live order frames. It translates each Tradovate push frame into a
broker-neutral `OrderEvent` and hands it to a callback.

Robustness built in:
- **Reconnect supervisor** with backoff: if the socket drops (e.g. the
  daily restart, or another login stealing the session), it reconnects.
- **Liveness watchdog**: Tradovate sends a heartbeat `h` frame ~every
  few seconds; if no frame arrives for ~6× that, the feed is treated as
  silently dead and a reconnect is forced (the socket can stay "open"
  while delivering nothing).
- **Snapshot vs live**: order ids present in the initial snapshot are
  tracked so pre-existing orders aren't re-emitted as new.
- **Session contention**: one Tradovate session per deviceId — diagnostic
  probes use a dedicated deviceId so they don't knock a running engine
  off its session (see README's session note).

## Pipeline

    Tradovate user-data WebSocket
        │ push frames
        ▼
    TradovateWSObserver        (parse → neutral OrderEvent)
        │ on_event callback (on the WS listener thread)
        ▼
    EventReplicator            (broker-neutral; id resolution, OCO,
        │                       ratio, fill cascade)
        ▼
    IbkrFollowerEndpoint  ──►  IbkrApiClient  ──►  IB Gateway (TWS API)

Assembled per enabled Tradovate-source pair by
`wiring.build_source_pipelines`, which returns startable
`SourcePipeline`s (observer + replicator + follower + reconciler), each
with `start()` / `stop()`.

## The broker-neutral core: EventReplicator

Unlike the historical `Replicator` (which is IBKR-source → Tradovate
specific), `EventReplicator` speaks a neutral vocabulary (`OrderEvent` /
`OrderSpec` / `BracketSpec` / `ModifySpec`) and drives any
`FollowerEndpoint`. This is what makes it reusable (it's also the engine
behind the Step-A neutral IBKR source, and the planned IBKR→IBKR path).

Responsibilities:
- **Id resolution**: maps each source order id to the placed follower
  order id via the OrderMap, so a later MODIFY/CANCEL finds its target.
  Bracket child legs bind their OWN source id (a fix: previously only the
  entry's source id was bound, so child-leg modify/cancel was silently
  skipped).
- **conid_resolver seam**: an injected callable used when the follower
  needs a symbol the source event doesn't carry. (Not relevant for the
  Tradovate→IBKR direction the way the IBKR-source direction needs it,
  but it's the same seam.)
- **Follower size ratio**: `follower_qty = round(master × ratio)`, min 1,
  round half-up; applied to new singles, brackets (entry + every leg)
  and size modifies; price-only modifies untouched. Threaded in from
  `pair.ratio` by `wiring.py`.
- **OCO cascade** (see below).

## OCO cascade for a non-native-OCO follower

A bracket's two exit legs are an OCO pair: when one is cancelled or
filled, the other must go away. Whether the follower enforces this
itself depends on the broker, so `FollowerEndpoint` exposes a
`native_oco` property:

- **IBKR** (`native_oco = True`): groups bracket children via `ocaGroup`,
  so the broker auto-cancels the sibling. The EventReplicator does
  NOTHING extra — a second cancel would be redundant.
- **Tradovate** (`native_oco = False`): legs are independent, so the
  replicator simulates the cascade itself.

`_cascade_oco_sibling` runs only when the follower lacks native OCO,
identifies exit legs by their synthetic `#LMT` / `#STP` labels, and
cancels the sibling — on both the CANCEL trigger and the FILL trigger.
Crucially, an **entry** fill cascades nothing (it opens the position;
the exits must stay live). The cascade never undoes the primary action
if the sibling cancel fails, and treats an already-gone sibling as
success.

Since this direction's follower is IBKR (native OCO), the cascade is a
no-op here in practice — but the machinery exists and is tested because
the same EventReplicator drives non-native-OCO followers too.

## The IBKR follower

`IbkrFollowerEndpoint` (tradesync/brokers/ibkr_follower_endpoint.py)
adapts `IbkrApiClient` to the `FollowerEndpoint` protocol, translating
neutral specs into ibapi `Contract` + `Order` objects. `IbkrApiClient`
is a blocking wrapper over the async, callback-driven TWS API
(EClient/EWrapper on a daemon reader thread), talking to a local IB
Gateway (port 4001 live / 4002 paper). It owns the monotonic order-id
counter IBKR hands out via `nextValidId`, re-read on every (re)connect
because the daily Gateway restart resets it.

IBKR modifies an order by re-placing the whole order, so the follower
remembers each placed order's contract + last Order (keyed by id) to
rebuild a modify payload.

## IB Gateway lifecycle

If an enabled pair has IBKR as follower, the bootstrap opens IB Gateway
for the user if it isn't already running — but NEVER restarts a running
one (that would drop the authenticated 2FA session). Opening only lands
the user on the login screen; the API becomes ready once they finish the
daily 2FA login, which the engine's connect step waits for.

## Position reconciler

Built only when the follower is IBKR (the side positions can be queried
on). Compares Tradovate (source) vs IBKR (follower) net positions,
normalised to symbols (Tradovate keys by contractId, IBKR by conId), and
warns on divergence — read-only, never trades. The IBKR read is pinned
to the pair's follower account so a multi-account Gateway login can't
manufacture phantom mismatches. Each pass also emits a combined
`[health]` log line (feed state + sync state).

## Dependency injection / testability

`wiring.py` takes client factories rather than building clients itself,
so it's pure and unit-testable with fakes; `main.py` injects the real,
credentialed clients. Factories are called lazily — no IB Gateway
connection is attempted unless an enabled pair actually has IBKR as its
follower.

## Key files

- `tradesync/brokers/tradovate_ws_observer.py` — WS observer, reconnect,
  watchdog, snapshot handling.
- `tradesync/brokers/tradovate_push_parser.py` — push frame → OrderEvent.
- `tradesync/event_replicator.py` — neutral core: id resolution, ratio,
  OCO cascade (cancel + fill), scale_quantity.
- `tradesync/brokers/ibkr_follower_endpoint.py` — neutral → ibapi.
- `tradesync/brokers/ibkr_api_client.py` — blocking TWS API wrapper.
- `tradesync/wiring.py` — assembles SourcePipelines from the config.
- `tradesync/position_reconciler.py` — read-only position safety check.
- `main.py` — `_build_source_pipelines_or_empty`, the WS-pipelines flag.

## Validation status

Validated live end to end against a real Tradovate account (source) and
IBKR (follower): NEW single (LMT/MKT) and bracket, MODIFY of entry,
stop-loss and take-profit on both suspended and active brackets, CANCEL
(single, individual legs, whole bracket via IBKR's native OCA), FILL +
position close — with positions confirmed aligned across both brokers.
Stop-limit is intentionally unit-test-only.
