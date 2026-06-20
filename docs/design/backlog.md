# Backlog — markets-closed cleanups

Low-priority items found during live operation. Do these ONLY with
markets closed and positions flat, then restart the engine to pick them
up. None of them is urgent; the live system behaves correctly today.

---

## 1. Spurious `ratio=1` log line on the IBKR-source branch (cosmetic)

**Symptom (live log, 2026-06-19):**

    IBKR→Tradovate replication active (neutral EventReplicator, ratio=1).

**Why it's misleading:** this line is emitted by the IBKR-source branch
of `_build_neutral_ibkr_source` (main.py). When there is no *enabled*
IBKR-source pair in replication.json, the branch still runs and logs a
default `ratio=1`. But that is NOT the active replication — the active
direction is the Tradovate→IBKR WS pipeline (its own ratio, 0.5, is
applied correctly inside the pipeline). On a real-money run a log line
saying "ratio=1" is alarming for no reason.

**Fix idea:** only build/log the IBKR-source neutral observer when an
enabled IBKR-source pair actually exists (mirror what
`_neutral_ibkr_source_pair` already resolves). When there's no such
pair, skip the branch and its log line entirely rather than logging a
default ratio.

**Risk:** LOW. Touches bootstrap wiring + a log line, not the live
replication math. Still: add/keep a test that the IBKR-source observer
is only built when an enabled IBKR-source pair is present.

---

## 2. `code=10327 OCA group type revision is not allowed` on every
##    bracket-leg MODIFY (real, but currently harmless)

**Symptom (live log, 2026-06-19):** after placing a bracket on the IBKR
follower, every modify of a stop/TP leg logs, right after
"Replicated EventKind.MODIFY":

    IBKR error reqId=3 code=10327: OCA group type revision is not allowed.

**Diagnosis (confirmed from the SENDING payloads):** the original
`placeBracket` sends each exit leg with its OCA group name (`oca_1`).
The MODIFY path re-sends the leg via placeOrder with an EMPTY OCA field,
so IBKR complains it can't revise the OCA group type. Crucially the
PRICE change IS applied — the new stop/TP price goes through; only the
(redundant) OCA re-declaration is rejected. Verified live: positions
stayed `aligned` and the new prices took effect after each modify.

**So why fix it:** today it's noise, but it fires on every single
bracket-leg modify, which means a genuinely problematic 10327 (or a
related OCA error) would be lost in the routine noise. A modify that
keeps a leg in its OCA group cleanly would silence it and make future
OCA errors meaningful.

**Fix idea:** in the IBKR follower's modify path, carry the existing
order's OCA group name (and OCA type) through the modify payload instead
of clearing it, so IBKR sees an in-group modify rather than an OCA-type
revision. Confirm against IBKR's rules for modifying an OCA member.

**Risk:** MEDIUM — this is the LIVE order-modification path for bracket
legs (it moves real stops). NOT a quick at-the-keyboard cleanup. Do it
deliberately: unit-test the modify payload (OCA group preserved), then
validate on paper (place bracket, move stop + TP, confirm no 10327 and
the prices update) BEFORE trusting it with real stops. Because there is
no longer a Replicator fallback, treat this with the same care as any
change to the live engine.

---

### Operational note

The running engine reads code only at startup (confirmed: it loads
everything into memory at launch and does not re-read disk). Editing
these files does NOT affect a running session — the changes take effect
only on the next restart. So these can be written any time, but their
*behaviour* only lands when the engine is restarted with markets closed.
