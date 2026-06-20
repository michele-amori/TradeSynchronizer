# Design — IBKR→IBKR replication (master account → a second IBKR follower)

Status: **IMPLEMENTED — paper validation PAUSED BY CHOICE.** The code is
in place (behind the `TRADESYNC_NEUTRAL_IBKR_SOURCE` flag, OFF by
default) with unit tests only. Paper→paper validation has **not** been
done and is **deliberately deferred** — the user has decided not to
pursue IBKR→IBKR for now. This is a parked feature, not forgotten work:
it stays disabled until the user chooses to revisit it, and it MUST be
validated paper→paper (see the validation sequence at the end) before
any live use. This document describes how it copies orders from one IBKR
account (the master, traded by hand in TradingView Desktop) to a
*second, separate* IBKR account (the follower), reusing existing,
already-validated pieces.

## Implementation summary (what was built)

- `main._build_neutral_ibkr_source` now chooses the follower from the
  matching `replication.json` pair: an `ibkr` follower builds an
  `IbkrFollowerEndpoint` on a second `IbkrApiClient` (from the
  `ibkr_gateway` block), with **no** conid_resolver and the pair's
  `ratio` threaded in; a `tradovate` follower (or no pair) keeps the
  original Step-A behaviour (TradovateEndpoint + conid_resolver).
- `main._neutral_ibkr_source_pair` finds the first enabled IBKR-source
  pair; never raises (config problems → None → legacy behaviour).
- `IbkrFollowerEndpoint.connect()` asserts the connected Gateway manages
  the configured follower account (`_assert_account_reachable`), raising
  `IbkrApiError` on mismatch — the wrong-account guardrail. An
  unknown/empty managed list warns and proceeds (safety net, not a hard
  gate, so the Tradovate→IBKR direction is unaffected).
- Still gated by `TRADESYNC_NEUTRAL_IBKR_SOURCE` (OFF by default): nothing
  changes unless explicitly enabled AND a matching config pair exists.

## Goal & scope

- **Master**: orders placed by hand in TradingView Desktop, logged into
  the master IBKR account. They are already intercepted by the existing
  mitmproxy addon — this is the same source the daily IBKR→Tradovate hot
  path observes. **Nothing new is needed on the source side.**
- **Follower**: a *different* IBKR account (different login, owned by a
  family member who has agreed to this). Reached through **one** IB
  Gateway — the follower's. The master needs **no** Gateway: its orders
  are seen on the wire by mitmproxy, not via the Gateway API.
- **Out of scope**: two simultaneous Gateways, a proxy on the family
  member's machine, any change to how the master is observed.

### Non-engineering precondition (must stay true)

Driving another person's account automatically is a regulated area in
many jurisdictions even between family members. This design assumes the
account owner has knowingly agreed and that the arrangement's *form* has
been checked by the user out-of-band. This document does not and cannot
assess that; it only covers the engineering.

## Why this is small: it's a recomposition, not new machinery

Both halves already exist and are validated:

- **Source** — `IbkrEventSourceObserver` (the "Step A" neutral IBKR
  source) translates each observed IBKR order into a broker-neutral
  `OrderEvent` and feeds an `EventReplicator`. The user reports Step A/B
  validated in DEMO.
- **Follower** — `IbkrFollowerEndpoint` (+ `IbkrApiClient`) is exactly
  what the Tradovate→IBKR direction uses as its follower, validated
  live. `IbkrApiClient.__init__` already takes `host` / `port` /
  `client_id`, so it can point at the follower's Gateway with no change.

The only place that changed is `main._build_neutral_ibkr_source`, which
previously **hardcoded** a `TradovateEndpoint` as the follower; it now
builds an `IbkrFollowerEndpoint` instead when the pair's follower is
IBKR.

## The three changes (as built)

### 1. Follower selection in `_build_neutral_ibkr_source` (main.py)

It previously always did:

    follower = TradovateEndpoint(tradovate, env=..., account_id=...)

It now decides the follower from the matching `replication.json` pair
(looked up by `_neutral_ibkr_source_pair`). When the follower broker is
`ibkr` it builds:

    client   = IbkrApiClient(host=<gw.host>, port=<gw.port>,
                             client_id=<gw.client_id>)   # ibkr_gateway block
    follower = IbkrFollowerEndpoint(client, env=<env>,
                                    account_id=<follower account>)

mirroring the `ibkr_factory` construction used by the WS pipelines. When
the follower is `tradovate` (or there's no pair) the original Step-A
behaviour is kept (TradovateEndpoint + conid_resolver).

### 2. The conid_resolver is omitted for an IBKR follower

For a **Tradovate** follower the `EventReplicator` needs a
`conid_resolver` because the IBKR-source event carries a *conId* and
Tradovate needs a *symbol*. For an **IBKR** follower this is NOT needed —
the conId is already the follower's native instrument id — so the
`EventReplicator` is built with `conid_resolver=None`.

**Still to verify on paper** (not covered by unit tests): that
`IbkrFollowerEndpoint` resolves/accepts the instrument from the neutral
`OrderSpec` as produced by the IBKR source. It should, since the source
already carries IBKR conId/contract data, but a wrong/absent contract on
the follower side is the highest-impact failure mode, so confirm it on
paper before trusting it.

### 3. The ratio is threaded in from the pair

`_build_neutral_ibkr_source` builds the `EventReplicator` with
`ratio=pair.ratio` (default 1.0 when there's no matching pair), so
IBKR→IBKR honours a non-1.0 ratio like every other pair type — the
family member can trade a scaled size. This reuses the same per-pair
ratio the GUI writes and the other paths read; there is no separate
place to configure it.

## Configuration shape (replication.json)

A pair expresses the whole thing; no new top-level concept is required
beyond pointing the gateway block at the follower's Gateway:

    {
      "pairs": [
        {
          "name": "MASTER→FAMILY",
          "source":   { "broker": "ibkr", "env": "live",
                        "account_id": "<MY master account>" },
          "follower": { "broker": "ibkr", "env": "live",
                        "account_id": "<FAMILY follower account>" },
          "enabled": true,
          "ratio": 0.5
        }
      ],
      "ibkr_gateway": { "host": "127.0.0.1", "port": 4001, "client_id": 12 }
    }

Notes / open config questions:
- `source` and `follower` are both `broker: ibkr` with **different**
  `account_id` — `ReplicationPair.validate` already rejects identical
  endpoints, which is the right guard here too.
- The `ibkr_gateway` block points at the **follower's** Gateway. Pick a
  `client_id` distinct from any other IBKR client used in the same run
  to avoid TWS client-id collisions.
- This IBKR-source pair is NOT a WS pipeline; it runs through the addon
  path (`_build_neutral_ibkr_source`), gated by
  `TRADESYNC_NEUTRAL_IBKR_SOURCE`. The bootstrap must learn to read the
  follower side of that pair from replication.json (today it only reads
  the legacy .env for this path).

## Risks & mitigations (real money, two accounts)

1. **Wrong account.** Orders must land on the family follower account,
   never the master. Mitigations: the follower Gateway is logged into
   the follower account; `IbkrFollowerEndpoint` already carries
   `account_id`; assert at startup that the connected Gateway's managed
   account matches the configured follower account, and refuse to start
   if not.
2. **Contract resolution on the follower** (see change 2) — verify on
   paper first.
3. **Feedback loop.** With both sides IBKR, be certain the follower's
   own placements can never be re-observed as new master orders. The
   master is observed via mitmproxy on TradingView traffic; the follower
   is driven via the Gateway API and does not flow through TradingView,
   so there is no loop — but this must be re-confirmed, because it's the
   assumption the whole safety of the design rests on.
4. **Ratio scales real sizes** on a second person's account. Default
   1.0; validate any non-1.0 on paper; change only while both sides flat
   with the engine restarted.
5. **Two daily logins / sessions.** The follower Gateway needs its own
   daily 2FA. Operationally one extra login each morning.

## Validation sequence (do not skip)

> **PAUSED BY CHOICE.** Steps 2-3 below are not currently planned — the
> user has decided not to validate IBKR→IBKR for now. The feature stays
> behind its flag, disabled. When/if it's revisited, run these steps in
> order; do not skip to live.

1. **Unit**: follower-selection branch in `_build_neutral_ibkr_source`
   builds an `IbkrFollowerEndpoint` for an ibkr follower and a
   `TradovateEndpoint` otherwise; ratio threaded; no conid_resolver for
   the IBKR follower. Startup account-match assertion.
2. **Paper → paper**: master paper account in TradingView → a second
   paper account as follower. Exercise NEW single, NEW bracket, all
   MODIFY legs, CANCEL, FILL + close, and a non-1.0 ratio. Confirm sizes,
   that the follower account (not the master) receives the orders, and
   that positions stay aligned (× ratio).
3. **Live**: only after paper is clean, and only with the far-from-market
   unfillable-order discipline used throughout this project, then a small
   real trade. Both sides flat, engine restarted to pick up config.

## What is explicitly NOT being changed

- The master observation path (mitmproxy addon) — untouched.
- The Tradovate→IBKR WS pipelines — untouched.
- The historical IBKR→Tradovate `Replicator` — untouched.
- Default behaviour with the flag OFF — untouched.
