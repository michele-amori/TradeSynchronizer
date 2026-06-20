# Design docs

Per-flow design documents for TradeSynchronizer's replication paths.
The top-level `README.md` is the operational/setup guide; these go
deeper on how each direction is built.

- [`ibkr-to-tradovate.md`](ibkr-to-tradovate.md) — the historical hot
  path: IBKR orders (observed via mitmproxy on TradingView traffic)
  mirrored onto Tradovate. **Implemented, live.**
- [`tradovate-to-ibkr.md`](tradovate-to-ibkr.md) — the broker-neutral
  direction: Tradovate orders (observed via the user-data WebSocket)
  mirrored onto IBKR, on the `EventReplicator`. **Implemented, validated
  live; OFF by default behind `TRADESYNC_ENABLE_WS_PIPELINES`.**
- [`ibkr-to-ibkr.md`](ibkr-to-ibkr.md) — copying the master IBKR account
  onto a second, separate IBKR follower account. **Implemented (behind
  `TRADESYNC_NEUTRAL_IBKR_SOURCE`, OFF by default); NOT yet validated on
  paper/live.**
- [`replicator-unification.md`](replicator-unification.md) — the plan to
  remove the duplication between the historical `Replicator` and the
  `EventReplicator` (Step C: make EventReplicator the live default; Step
  D: delete the old one). **Plan only — not started; gated on DEMO
  validation.**

Shared building blocks referenced across the docs: the neutral
vocabulary (`OrderEvent` / `OrderSpec` / `BracketSpec` / `ModifySpec`),
the `SourceEndpoint` / `FollowerEndpoint` protocols, the per-pair
follower size `ratio`, and the OCO sibling cascade (`native_oco` +
`_cascade_oco_sibling`).
