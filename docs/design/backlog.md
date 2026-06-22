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

## 2. OCA error on every bracket-leg MODIFY — FIX REWRITTEN, DEMO-VALIDATED

**The full story (two wrong attempts, both seen live on paper):**

1. **`code=10327 OCA group type revision is not allowed`.** Originally
   the modify path (`_clone_order`) re-placed the leg with NO OCA fields.
   IBKR rejected the group-type revision.
2. **`code=10326 OCA group revision is not allowed`.** The first "fix"
   then re-sent `ocaGroup` + `ocaType` (and `parentId`) on the modify to
   keep the leg "in its group". IBKR rejected this too — AND, verified
   directly via `reqAllOpenOrders` on both demo followers, **the stop
   price did NOT move** (leg stayed at the old aux price). So 10326 was
   BLOCKING, not cosmetic: the modify was rejected outright.

**Root cause (confirmed):** IBKR modifies by re-placing the full order
under the same id, but OCA grouping is a PLACE-TIME property. IBKR
already remembers the leg's group from its order id; a modify that
restates `ocaGroup` / `ocaType` (or `parentId`) is treated as a group
revision and rejected. The leg must NOT carry OCA fields on a modify.

**Fix (committed, demo-validated):** `_clone_order` no longer copies
`ocaGroup` / `ocaType` / `parentId`. A bracket-leg modify now carries
only action / type / qty / tif + the changed price. IBKR accepts it and
the placement-time OCO grouping stays intact at the broker.

**Validation method (now the reliable tool):** `/tmp/check_stops.py` —
a READ-ONLY ibapi client (`reqAllOpenOrders`, spare client_id, no
place/modify/cancel) that connects to each follower Gateway and prints
each open order's actual price as IBKR holds it. This is how the 10326
rejection was proven to leave the stop unmoved, and how the rewritten
fix should be re-confirmed: move the stop, then read both followers and
check the aux price actually changed.

**Tests (committed, green):** `test_modify_bracket_leg_strips_oca_group`
now asserts the modify carries NO ocaGroup / ocaType / parentId while
the new price still applies; `test_modify_single_order_has_no_oca_group`
unchanged. The fake client still stamps OCA on bracket children at place
time (correct), and the test verifies the modify STRIPS them.

**Note:** this is the LIVE order-modification path for bracket legs (it
moves real stops); there is no Replicator fallback. Re-validate on demo
after any change before relying on it in a real-money session.

---

### Operational note

The running engine reads code only at startup (it loads everything into
memory at launch and does not re-read disk). Editing these files does
NOT affect a running session — the changes take effect only on the next
restart. So these can be written any time, but their *behaviour* only
lands when the engine is restarted with markets closed.
