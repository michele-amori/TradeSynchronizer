# Design — IBKR→Tradovate replication (the historical hot path)

Status: **IMPLEMENTED and live.** This is the original direction
TradeSynchronizer was built for: orders placed by hand on IBKR (through
TradingView Desktop) are mirrored onto a Tradovate account. It runs on
the `Replicator` and the mitmproxy addon. This document describes how it
works end to end.

## What it does

The user trades on IBKR via TradingView Desktop. Every order
placement / modification / cancellation TradingView sends to IBKR is
observed on the wire and replicated onto a Tradovate account, so the two
stay in sync. The original IBKR order is never modified, blocked, or
delayed — replication happens in parallel.

## How the master is observed: mitmproxy

There is no IBKR push feed available to a third-party app for this; the
only way to see the orders is to intercept the HTTPS traffic between
TradingView Desktop and IBKR. So:

- TradingView Desktop is launched pointing at a local mitmproxy
  (`--proxy-server=127.0.0.1:<port>`), and the mitmproxy CA cert is
  trusted by the system Keychain (one-time setup, see README).
- mitmproxy is configured to MITM **only** `api.ibkr.com` (via
  `allow_hosts`, not `ignore_hosts` — see the long comment in
  `main.main()` for why the negative-list approach silently raw-forwards
  and breaks interception).
- `TradeSyncAddon` (tradesync/proxy/addon.py) is the mitmproxy addon. Its
  `request` / `response` hooks see every IBKR order flow.

## The order-management endpoints (discovered empirically)

IBKR's URL pattern as used by TradingView Desktop is asymmetric, which
caused a real bug once (modify/cancel silently ignored). The addon
matches:

- `POST   .../orders`              → new order (single or bracket)
- `DELETE .../order/{ibkr_id}`     → cancel   (note: **singular** `order`)
- `POST/PUT .../order/{ibkr_id}`   → modify   (singular, excludes the
  `/order/whatif` preview endpoint)

## Pipeline

    TradingView Desktop
        │ HTTPS to api.ibkr.com
        ▼
    mitmproxy  ──►  TradeSyncAddon (request/response hooks)
        │ emit_new / emit_cancel / emit_modify
        ▼
    IbkrSourceObserver        (the source seam)
        │
        ▼
    Replicator                (policy + field mapping + Tradovate calls)
        │
        ▼
    TradovateClient  ──►  Tradovate REST (placeorder / placeoso /
                          modifyorder / cancelorder)

The addon does NOT call the `Replicator` directly — it goes through
`IbkrSourceObserver`, a thin seam exposing `emit_new` / `emit_cancel` /
`emit_modify` plus the two id-binding helpers below. That seam is what
later let the neutral path (`IbkrEventSourceObserver` → `EventReplicator`)
be swapped in behind a flag without touching the mitmproxy hooks.

Replication runs on a worker thread (`_spawn`), never on mitmproxy's
event loop: Tradovate's contract/find + placeorder can take 500–2000 ms
and must not stall the proxy or delay the user's real IBKR order.

## The two-phase order-id binding (the subtle part)

IBKR identifies a later modify/cancel by the **IBKR order id**, but at
placement time TradingView only knows its own client order id (cOID).
The addon learns the IBKR id in two phases on the `response` hook:

1. **New-order response** (`parse_new_order_response_ids`): binds each
   cOID → IBKR id via `register_ibkr_id(coid, ibkr_id)`. A bracket POST
   produces several cOIDs in body order, so the addon stashes the cOIDs
   per flow (`_coids_by_flow`, keyed by `id(flow)`) on the request hook
   and pairs them up on the response.
2. **Orders-list poll** (`is_orders_list_response` +
   `parse_orders_list_bracket_children`): bracket child legs' IBKR ids
   surface on a later `GET /orders`. The addon resolves each child's
   parent (the entry's IBKR id) back to the entry cOID via
   `coid_for_ibkr_id`, then registers the child under a synthetic cOID.
   These polls are idempotent — `register_ibkr_id` can be called
   repeatedly with no harm.

With both phases done, a later modify/cancel carrying the IBKR id
resolves through the OrderMap to the right Tradovate order id.

## Field mapping & policy (Replicator)

- **Order types**: MKT / LMT / STP / STP LMT mapped to Tradovate's
  vocabulary. Price fields are gated by the target type (a Limit modify
  must not carry a stop price, and vice-versa — TradingView sends both
  with `auxPrice: 0`, which Tradovate rejects; the Replicator filters
  per type). Stop-limit is mapped but the user does not use it.
- **Brackets**: entry + exits placed via Tradovate `/order/placeoso`.
- **Replication mode**: `mirror` (as-is) or `market` (force everything
  to Market); in market mode a price-only modify is meaningless and
  dropped, quantity changes still pass through.
- **Account filter**: only IBKR orders from `ibkr_watched_accounts` (if
  set) are replicated; others are skipped by policy.
- **Skip protective stops**: optional policy flag.
- **Follower size ratio**: `follower_qty = round(master × ratio)`, min 1
  (see the ratio design / README). The bootstrap reads the ratio from
  the matching pair in `config/replication.json`
  (`_ibkr_to_tradovate_ratio`), so both directions configure scaling in
  one place.

## OCO handling

Tradovate's `/order/placeoso` returns `ocoId=null` — its exit legs are
NOT a broker-enforced OCO group. The historical `Replicator` therefore
simulates the sibling cancel itself: cancelling or filling one exit leg
cancels the other. (The neutral `EventReplicator` implements the same
behaviour via `native_oco` + `_cascade_oco_sibling`; see the
Tradovate→IBKR design and the OCO-cascade work.)

## Failure handling & divergence

- The addon never lets a replication failure affect the original IBKR
  order (separate thread; the original request is untouched).
- On a failed (not merely skipped) replication, the Replicator emits a
  structured DIVERGENCE log line; the GUI parses these for its per-env
  Sync-health panel.
- The persistent OrderMap is reconciled with Tradovate at startup
  (`reconcile_with_tradovate`): orders filled/cancelled out-of-band
  while the engine was down are pruned so they don't wait forever for a
  modify/cancel that will never come.

## Position reconciler

A periodic read-only check compares IBKR vs Tradovate net positions by
symbol and warns on divergence (never trades). IBKR positions are
filtered to the followed account and to futures only (`secType=FUT`) —
a multi-account Gateway login otherwise leaks other accounts' positions
and non-futures holdings (e.g. sovereign bonds), manufacturing phantom
mismatches. A transient mismatch right at a fill (one side filled, the
other a beat behind) is expected and clears on the next pass.

## Key files

- `tradesync/proxy/addon.py` — mitmproxy addon, hooks, two-phase id bind.
- `tradesync/proxy/ibkr_source_observer.py` — the source seam.
- `tradesync/proxy/ibkr_parser.py` — request/response detection + parsing.
- `tradesync/replicator.py` — policy, field mapping, Tradovate calls,
  OCO cascade, ratio, OrderMap reconciliation.
- `tradesync/brokers/tradovate.py` — Tradovate REST client.
- `main.py` — bootstrap, mitmproxy options (`allow_hosts`), wiring.

## Validation status

Validated live against real IBKR + Tradovate accounts using
far-from-market unfillable orders (zero position risk) and, separately,
on real open trades: NEW single (LMT/MKT) and bracket, MODIFY of entry
and exit legs, CANCEL (single, bracket via OCA, single STP), FILL +
position close. Stop-limit is intentionally unit-test-only (the user
does not use it).
