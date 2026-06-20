# Multi-follower replication (Tradovate -> N IBKR followers)

## What it is

One Tradovate source can fan out to **several IBKR followers at once**,
each on a **separate IBKR login** (Scenario B). Every replicated order is
mirrored onto each enabled follower, with its own size `ratio`.

This is declarative: add one `ReplicationPair` per follower in
`config/replication.json`, all sharing the same Tradovate source. The
engine already builds and starts one independent pipeline per enabled
pair; the work below made that safe for *multiple IBKR followers on
separate logins*.

## Why the architecture allowed it

Most of the groundwork was already there and is unchanged:

- `pairs` is a list; the bootstrap builds + starts one pipeline per
  enabled pair. N pipelines from one source already worked.
- Each pipeline is isolated: its own observer, EventReplicator, follower,
  reconciler. No shared mutable engine state.
- `ratio` is per-pair, so followers can trade different sizes.
- The PositionReconciler already pins its follower read to the pair's
  account (`get_positions(account=...)`), so multi-account reads never
  blend books.

## What had to change (staged, each its own tested commit)

**S1 - per-follower Gateway in config.** `ReplicationPair` gained an
optional `ibkr_gateway`. When set, that pair's follower connects through
its own host/port/client_id; when absent it falls back to the top-level
`ibkr_gateway`. Backward-compatible (the key is only written when set).

**S2 - IBKR client cache per Gateway endpoint.** Wiring stopped treating
the Gateway as a process-wide singleton. Clients are now cached by
`(host, port, client_id)`: separate-login followers each get their own
connection; same-login followers share one. `ibkr_client_factory` is now
`(gateway) -> IbkrApiClient`.

**S3 - per-follower OrderMap.** The OrderMap is keyed per follower
account (`orders-<env>-<account>.json`), not per env. Without this, two
followers in one env would share a map and the second's `set_follower_id`
would overwrite the first's mapping for the same (shared) source label,
silently breaking modify/cancel routing.

**S4 - safety nets.**
- `order.account` is stamped on every IBKR order (single, both bracket
  legs, modify), so a Gateway login that can see several accounts always
  routes to the configured one. Complements the connect-time
  `_assert_account_reachable` guard.
- `ReplicationConfig.validate()` rejects two ENABLED pairs targeting the
  same follower endpoint (that account would receive every order twice).

## Operational caveats (the real cost)

- **One IB Gateway per distinct login.** Separate-login followers each
  need their own running Gateway (distinct port and/or client_id),
  logged into the right account, each with its own daily restart. This
  operational overhead, not the code, is the main cost of Scenario B.
- **Distinct client_id per Gateway.** Each API connection to a Gateway
  needs a unique client_id for that Gateway.
- **OrderMap filename changed** (S3): `orders-<env>.json` ->
  `orders-<env>-<account>.json`, even for a single follower. The old
  file is not auto-migrated, so the first restart after S3 should be from
  a known/flat state, or an in-flight order tracked only in the old file
  would not be found for a later modify/cancel.

## Before real-money multi-account use

Validate with **two demo followers first** (separate demo logins). A
routing bug here would place orders on the wrong account with real money
belonging to more than one person. The async follower-rejection alerting
(see the alert channel) means a genuinely misrouted/rejected order now
fails *visibly* (GUI Sync-health + desktop notification), but that's a
backstop, not a substitute for paper validation.

## Status

S1-S4 implemented and unit-tested; structurally complete and
safe-by-construction. **Not yet paper-validated end to end** with two
live Gateways. Inert until a second follower pair is enabled.
