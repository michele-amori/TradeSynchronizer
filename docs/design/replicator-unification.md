# Design / plan — unifying the two replicators (Step C / Step D)

Status: **PLAN ONLY — not started.** This is the tracked plan to remove
the largest piece of tech debt in the project: two parallel replication
engines. No code change is described here as "done"; this document exists
so the work is captured and can be executed safely, in order, in
dedicated sessions — NOT extemporaneously, because Step C changes the
engine that executes real daily trades.

## The debt

Two engines do the same job:

| | lines | role |
|---|---|---|
| `Replicator` (tradesync/replicator.py) | ~949 | the historical **live** IBKR→Tradovate hot path |
| `EventReplicator` (tradesync/event_replicator.py) | ~450 | the broker-neutral engine (Tradovate→IBKR, the Step-A neutral IBKR source, IBKR→IBKR) |

The cost is concrete and already felt: every change to replication logic
must be made twice and kept in sync. The follower **ratio** had to be
threaded into both; the **OCO cascade** had to be built in both. Each
duplication is a chance for the two to drift.

The end state is a single engine (`EventReplicator`) driving every
direction, with the historical `Replicator` deleted.

## Why this can't be done in one go

- **Step C** makes `EventReplicator` the default for the **live**
  IBKR→Tradovate path — i.e. it changes the engine that executes the
  user's real daily trades (and, in some configs, a family member's
  account). This MUST be validated in DEMO before it becomes the live
  default. The whole project's discipline is DEMO-first for anything
  that changes live behaviour.
- **Step D** deletes the proven `Replicator`. It can only happen after
  Step C has run as the live default, cleanly, for **weeks**. It is not
  executable until then by definition.

So the unification is a multi-session effort gated on live-DEMO
validation, not a refactor to land at the end of a session.

## The mechanism already half-exists

The flag `TRADESYNC_NEUTRAL_IBKR_SOURCE` already routes the IBKR→Tradovate
path through `EventReplicator` instead of `Replicator`
(`main._build_neutral_ibkr_source`, Tradovate-follower branch). That flag
IS the Step-C switch. What's missing is (a) closing the behaviour-parity
gaps below, and (b) the DEMO validation that lets the flag flip from
"validation switch" to "live default".

## Parity audit — what EventReplicator must match before Step C

Audited against `Replicator`'s IBKR→Tradovate behaviour. Already at
parity:

- **Follower ratio** — `scale_quantity`, both engines. ✓
- **Watched-account filter** — `watched_source_accounts`
  (event_replicator.py ~191) mirrors `ibkr_watched_accounts`. ✓
- **OCO sibling cascade** — on CANCEL and FILL, for non-native-OCO
  followers; no-op for IBKR. ✓

Gaps — progress:

1. **`replication_mode` (mirror vs market). RESOLVED — no code needed.**
   EventReplicator already replicates the true order type faithfully
   (MKT→Market, LMT→Limit, STP→Stop), which is the `mirror` behaviour
   and what the live config uses (`REPLICATION_MODE=mirror`). The
   `"market"` GLOBAL override (force everything to Market, dropping limit
   prices) is the only thing EventReplicator lacks, and it is deliberately
   NOT ported: it would corrupt faithful replication. Its actual removal
   from the legacy `Replicator` belongs to Step D, not Step C.
2. **`skip_protective_stops` policy. RESOLVED — feature removed entirely.**
   The user decided stop orders are ordinary orders, always replicated.
   The whole feature (`Config.skip_protective_stops`, the env var, the
   GUI field, `IbkrOrder.is_protective_stop`, the Replicator policy
   block) was deleted from BOTH engines, so there is no parity gap left
   to close. Safe because the live `.env` already had it `false`
   (replicate stops), so behaviour is unchanged.
3. **OrderMap startup reconciliation. RESOLVED — implemented.**
   `EventReplicator.reconcile_with_follower()` is the broker-neutral
   equivalent of `Replicator.reconcile_with_tradovate`: at startup it
   walks the persistent OrderMap (`OrderMap.source_labels()`), asks the
   FollowerEndpoint for each order's `order_status`, and prunes entries
   in a recognised TERMINAL state (filled/cancelled/rejected/expired/…,
   across both the Tradovate and IBKR vocabularies, case-insensitive).
   Deliberately conservative: it prunes ONLY on a recognised terminal
   status — an active status, an unknown string, or a query error leaves
   the entry untouched, so a transient hiccup can never wipe a valid
   mapping. Wired into `SourcePipeline.start()` after the follower
   connects (so order_status works) but before observing begins, and a
   reconciliation failure never blocks startup.
4. **Stop-limit mapping. RESOLVED — already supported, no code needed.**
   Audited end-to-end: the neutral path already carries stop-limit
   symmetrically. The IBKR source builds `OrderType.STOP_LIMIT` with
   BOTH prices (`_order_spec_from_ibkr_order`: limit_price ← IBKR price,
   stop_price ← IBKR aux_price), the neutral `OrderSpec` documents
   "STOP_LIMIT → both set", and both followers re-map it with both prices
   gated correctly: `TradovateEndpoint` → "StopLimit",
   `IbkrFollowerEndpoint` → "STP LMT" (each via `_wants_limit` /
   `_wants_stop`). So the EventReplicator handles stop-limit at least as
   well as the Replicator. (The Replicator's "STPLMT exits out of scope"
   note is narrower — it's about classifying a bracket exit leg's ROLE
   in the OCO cascade, not about replicating a stop-limit order.)
   DECISION: leave it supported (already covered by endpoint unit tests);
   nothing to port or remove. The user has stated they will never use
   stop-limit orders, so this path is not exercised in practice, but
   keeping it working costs nothing and avoids a silent capability drop.

**All four parity gaps are now resolved** (1 & 2 by decision/removal,
3 by implementation, 4 by audit). The behaviour-parity prerequisite for
Step C is therefore complete, and an initial DEMO validation has passed
(step 2 below). What remains is a SUSTAINED DEMO period (step 3) and then
the flag flip (step 4) — still NOT a now-task, and gated on markets-open
DEMO time.

## Step C — make EventReplicator the live IBKR→Tradovate default

Progress: **steps 1-4 DONE — the neutral path is now the live default.**
The flip is reversible (see step 4) and the Replicator stays in the tree
as the rollback target until Step D.

1. Parity gaps 1-4: **DONE** (see above).
2. Validate the neutral IBKR→Tradovate path (flag ON) in **DEMO**, with
   the same matrix used for the other directions: NEW single (LMT/MKT) +
   bracket, MODIFY of entry/SL/TP on active and suspended brackets,
   CANCEL (single, legs, whole bracket), FILL + close.
   → **DONE.** User ran a DEMO validation and reported behaviour OK
   across the matrix. (Reported by the user; the per-run DEMO logs were
   not independently re-audited here.)
3. Run the flag ON in DEMO for a sustained period.
   → **DONE per the user**, who reports having run the neutral path on
   another machine and confirms readiness to flip. NOTE: this sustained
   period was not observed/log-audited in-session — it rests on the
   user's report. The user explicitly accepts the risk (small size, own
   funds) and directed the flip.
4. Flip the default.
   → **DONE.** `_neutral_ibkr_source_enabled()` is now opt-OUT: the
   neutral path is the default, ON unless TRADESYNC_NEUTRAL_IBKR_SOURCE
   is set to 0/false/no/off.
   **ROLLBACK (one env var):** set TRADESYNC_NEUTRAL_IBKR_SOURCE=0 (or
   false/no/off) → instantly back on the proven Replicator, which stays
   in the tree untouched as the rollback target. Watch the first live
   sessions' `[health]` / divergence logs; roll back at the first sign
   of engine-attributable divergence.

## Step D — remove the Replicator — **DONE**

The historical `Replicator` has been removed. The neutral
`EventReplicator` is now the ONLY IBKR→Tradovate engine.

What was done (D-prep → D-remove-1 → D-remove-2):
1. **D-prep:** made the neutral path self-sufficient — the bootstrap now
   creates the OrderMap (it was owned by the Replicator) and the neutral
   path does its own startup reconciliation via
   `EventReplicator.reconcile_with_follower()` instead of relying on
   `Replicator.reconcile_with_tradovate()`.
2. **D-remove-1:** deleted `tradesync/replicator.py` and
   `tradesync/proxy/ibkr_source_observer.py`; removed the
   `TRADESYNC_NEUTRAL_IBKR_SOURCE` flag, the fallback branch, and
   `_ibkr_to_tradovate_ratio` from `main.py`; made the addon's `source`
   required (no `replicator` param, no fallback). Deleted the
   Replicator-specific tests (behaviour coverage lives on
   `test_event_replicator.py`).
3. **D-remove-2:** removed the now-dead `replication_mode` / "market"
   override config from `Config`, the GUI, and `env_store`.

### Deviation from the original criteria (recorded honestly)

The original plan required Step C to run as the live default for ≥ ~4
weeks with zero engine-attributable divergence BEFORE removal, with a
one-env-var rollback kept until then. That waiting period was **not**
served: the user decided to step away from real-money use for now and
directed Step D immediately, accepting that this compresses the timeline
and removes the rollback target. Because the user is not trading live,
the prime risk the waiting period guarded against (a latent engine bug
surfacing on real money) is deferred rather than incurred. This is a
conscious, user-directed trade-off, not an oversight.

## Rollback posture — CHANGED

There is **no longer a rollback to the Replicator** — it has been
deleted. The neutral `EventReplicator` is the only IBKR→Tradovate engine.
If a problem surfaces, the recourse is to fix forward on the
EventReplicator (or `git revert` the Step D commits), not to flip a flag.
Before returning to real-money live use, run a sustained markets-open
DEMO of the neutral path, since there is no proven fallback to fall onto.
