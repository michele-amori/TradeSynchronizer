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

## 2. OCA error on every bracket-leg MODIFY — RESOLVED PER IBKR DOCS,
##    AWAITING FINAL DEMO RE-VALIDATION

**Three states observed live on paper (stop price checked via
`reqAllOpenOrders` each time — the stop NEVER moved in the two failures):**

1. **`code=10327 OCA group type revision is not allowed`.** Modify
   re-placed the leg with NO OCA fields (`_clone_order` rebuilt a fresh
   Order with only action/type/qty/tif/price). Rejected.
2. **`code=10326 OCA group revision is not allowed`.** Then re-sent
   `ocaGroup` + `ocaType` (+`parentId`) but STILL as a hand-picked
   subset. Rejected.
3. Back to NO OCA fields → **10327 again**, stop confirmed unmoved at
   the old aux price on both followers. This proved the
   "which OCA fields?" framing was a dead end: BOTH directions fail.

**Root cause (per IBKR TWS API "Modifying Orders" docs):** a modify is
`placeOrder` "with the same fields as the open order, except for the
parameter to modify" (same `orderId`). The bug was never *which* OCA
fields to send — it was that `_clone_order` rebuilt a PARTIAL order, so
whatever subset it chose, the re-place differed from the live leg in
some field (parentId / ocaGroup / ocaType / transmit / …) and IBKR read
the difference as an OCA group revision. The leg must be re-sent with
EVERY field identical, only the price changed.

**Fix (committed, awaiting demo re-validation):** `_clone_order` now
returns `copy.copy(src)` — a full shallow copy of the remembered order,
preserving every attribute the bracket placement stamped (parentId,
ocaGroup, ocaType, transmit, account, …) automatically, with no risk of
dropping one. `modify_order` then overwrites only the changed price/qty/
tif on the copy; the remembered original is left intact until the modify
is confirmed.

**Validation method (the reliable tool):** `/tmp/check_stops.py` —
READ-ONLY ibapi (`reqAllOpenOrders`, spare client_id, no
place/modify/cancel) printing each open order's actual price as IBKR
holds it. Both prior failures were caught with it.

**STILL REQUIRED — final demo re-validation:** unit tests prove the
payload now re-sends all fields; they do NOT prove IBKR accepts it. On
the demo Gateways: place ONE fresh bracket, move the stop, then read
both followers with check_stops.py and confirm the aux price ACTUALLY
CHANGED (and no 10326/10327 in the log). Only then rely on it with real
money.

**Tests (committed, green):** `test_modify_bracket_leg_preserves_all_fields`
asserts the modify re-sends ocaGroup/ocaType/parentId identical to the
placement, with only the price changed; `test_modify_single_order_has_no_oca_group`
unchanged (a plain order has no OCA, and copy doesn't invent one).

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
