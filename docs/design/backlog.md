# Backlog — markets-closed cleanups

Items found during live operation. Do code changes here ONLY with
markets closed and positions flat, then restart the engine to pick them
up. The live system behaves correctly today regardless.

---

## 1. Spurious `ratio=1` log line on the IBKR-source branch — DONE

**Status:** RESOLVED in `main.py` (`_build_neutral_ibkr_source`).

When no enabled IBKR-source pair exists, the neutral observer is still
built (it must be ready in case IBKR orders arrive) but no longer logs a
misleading `ratio=1`. Instead it logs:

    IBKR→Tradovate neutral observer ready (no enabled IBKR-source
    pair; dormant unless IBKR orders arrive).

The `ratio=%g` "replication active" line is now emitted ONLY when an
enabled IBKR-source pair is actually present. No further action.

---

## 2. `code=10327 OCA group type revision is not allowed` on every
##    bracket-leg MODIFY — FIX WRITTEN, AWAITING PAPER VALIDATION

**Symptom (live log, 2026-06-19):** after placing a bracket on the IBKR
follower, every modify of a stop/TP leg logged, right after
"Replicated EventKind.MODIFY":

    IBKR error reqId=3 code=10327: OCA group type revision is not allowed.

**Root cause (confirmed):** IBKR modifies an order by re-placing the
full order under the same id. `IbkrApiClient.place_bracket` stamps each
exit leg with its OCA group (`ocaGroup="oca_<entry>"`, `ocaType=1`) on
the very `Order` object the follower remembers. But the modify path
rebuilt the payload via `_clone_order`, which copied action / type /
qty / tif / prices but NOT `ocaGroup` / `ocaType` / `parentId`. So the
re-placed leg arrived with an empty OCA group, and IBKR rejected the
group-type revision. The price change still applied; only the
(redundant) OCA re-declaration was rejected.

**Fix (committed):** `_clone_order` in
`tradesync/brokers/ibkr_follower_endpoint.py` now carries `ocaGroup`,
`ocaType` and `parentId` through when the remembered order has them, so
a bracket-leg modify is an in-group modify rather than an OCA-type
revision. A plain (non-bracket) order has none of these set, so it's a
no-op there.

**Tests (committed, green):** in `tests/test_ibkr_follower_endpoint.py`
the fake client now mirrors the real client by stamping ocaGroup /
ocaType / parentId on bracket children, and two cases pin the fix:
`test_modify_bracket_leg_preserves_oca_group` (leg keeps its group +
type + parent, price still updates) and
`test_modify_single_order_has_no_oca_group` (a single order's modify
does NOT invent a group).

**STILL REQUIRED before trusting with real stops — PAPER VALIDATION:**
unit tests prove the payload shape; they do NOT prove IBKR accepts it.
On the demo Gateways, place a bracket, move the stop and the take-profit,
and confirm in the live log that:
  1. no `code=10327` fires on the modify, and
  2. the new stop / TP prices actually take effect, and
  3. the OCO still works (filling/cancelling one leg cancels the other).
Only after that should this be relied on in a real-money session.
This is the LIVE order-modification path for bracket legs (it moves real
stops); there is no Replicator fallback, so treat it with the same care
as any change to the live engine.

---

### Operational note

The running engine reads code only at startup (it loads everything into
memory at launch and does not re-read disk). Editing these files does
NOT affect a running session — the changes take effect only on the next
restart. So these can be written any time, but their *behaviour* only
lands when the engine is restarted with markets closed.
