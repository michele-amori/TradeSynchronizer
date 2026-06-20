"""
EventReplicator — broker-neutral replication engine.

This is the integration-phase counterpart to the existing Replicator.
Where Replicator consumes IBKR parser dataclasses and talks to a
Tradovate client with Tradovate-specific call shapes (the historical,
live IBKR→Tradovate hot path), EventReplicator consumes broker-neutral
OrderEvents and drives a FollowerEndpoint — so it works for ANY
source→follower direction (Tradovate→IBKR included).

Relationship to Replicator
---------------------------
Built ALONGSIDE Replicator, not replacing it. The live hot path still
runs through Replicator unchanged. EventReplicator is exercised by
tests now; switching the bootstrap over to it (so the engine consumes
OrderEvents from a SourceEndpoint and replicates via a FollowerEndpoint)
is the final integration step, done with live DEMO validation rather
than blind. Keeping both side by side means that switch is a bootstrap
change, reversible in one line, with the proven path still present.

Symbol resolution (per-direction)
----------------------------------
A FollowerEndpoint.place_* takes the FOLLOWER-side symbol. How we get
it depends on the source:
  * Tradovate source → the event already carries the Tradovate symbol,
    and the IBKR follower resolves it to a Contract internally
    (IbkrApiClient.resolve_contract). So event.symbol passes straight
    through. This is the direction the bidirectional work adds, and the
    one this replicator fully supports (validated live).
  * IBKR source → the event carries an IBKR conid, which must be mapped
    to the follower's symbol (conid→IBKR symbol→Tradovate symbol). That
    mapping is injected as an optional `conid_resolver` strategy: when
    present, a conid-only event resolves through it; when absent (the
    default, e.g. the Tradovate-source direction that never needs it),
    a conid-only event is surfaced as a clear, non-crashing failure
    rather than guessed. Injecting the resolver is the seam by which the
    historical IBKR→Tradovate hot path is unified onto this engine.

Never raises: like Replicator, every public entry point catches and
converts failures into an EventResult so a follower error never
propagates back into the source observer's thread.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, replace
from typing import Callable, Optional

from .brokers.endpoint import FollowerEndpoint
from .order_event import (
    BracketRole,
    EventKind,
    OrderEvent,
)
from .order_map import OrderMap


logger = logging.getLogger("tradesync.event_replicator")


@dataclass
class EventResult:
    """Outcome of replicating one OrderEvent. Broker-neutral analogue
    of ReplicationResult."""
    success: bool
    skipped: bool
    reason:  str


# Synthetic source-label roles for bracket children that arrive without
# their own source id — same convention the existing replicator uses
# ({entry}#LMT / {entry}#STP), generalised to the neutral roles.
_ROLE_TOKEN = {
    BracketRole.TAKE_PROFIT: "LMT",
    BracketRole.STOP_LOSS: "STP",
}
# Inverse, for cascade sibling lookup.
_SIBLING_TOKEN = {"LMT": "STP", "STP": "LMT"}


def scale_quantity(master_qty: int, ratio: float) -> int:
    """Scale a master order size to the follower's size.

        follower_qty = round(master_qty * ratio), min 1

    Rules (these touch REAL order sizes, so they're deliberate):
      * Round half UP, not Python's banker's rounding — 2.5 → 3, not 2 —
        so a user reading "× 0.5" gets the intuitive result.
      * Floor to 1, never 0: a replicated trade always opens at least the
        1-contract minimum. So master=1 × ratio=0.33 → 0.33 → 1, not a
        silently-dropped trade.
      * ratio 1.0 is exact mirror (returns master_qty unchanged).

    master_qty is assumed already validated as a positive int by the
    parser; a non-positive master qty is returned as-is (nothing
    meaningful to scale) rather than forced to 1.
    """
    if master_qty <= 0:
        return master_qty
    scaled = math.floor(master_qty * ratio + 0.5)   # round half up
    return max(1, scaled)


class EventReplicator:
    """Consumes OrderEvents and replicates them onto a FollowerEndpoint.

    Parameters
    ----------
    follower:
        The FollowerEndpoint orders are placed on (TradovateEndpoint,
        IbkrFollowerEndpoint, …).
    order_map:
        Persistent source-id ↔ follower-id map. The neutral API
        (bind_source_id / set_follower_id / follower_id_for_source_id /
        …) is used throughout.
    watched_source_accounts:
        Optional allow-list of source account ids. Empty = replicate
        all. Mirrors Replicator's ibkr_watched_accounts policy, but
        keyed on the event's source_account_id so it works regardless
        of which broker is the source.
    conid_resolver:
        Optional callable mapping an IBKR conId → the follower-side
        symbol. Only needed for the IBKR-source direction, where events
        carry a conid but no symbol (a Tradovate-source event already
        carries the symbol). Default None preserves today's behaviour:
        a conid-only event with no symbol is surfaced as a clear
        failure rather than guessed. Injecting a resolver is what
        unifies the historical IBKR→Tradovate hot path onto this engine.
    """

    def __init__(
        self,
        *,
        follower: FollowerEndpoint,
        order_map: OrderMap,
        watched_source_accounts: Optional[list] = None,
        conid_resolver: Optional[Callable[[int], Optional[str]]] = None,
        ratio: float = 1.0,
    ):
        self._follower = follower
        self._order_map = order_map
        self._watched = list(watched_source_accounts or [])
        self._conid_resolver = conid_resolver
        # Follower size scaling: follower_qty = round(master_qty * ratio),
        # min 1. 1.0 = exact mirror. See scale_quantity().
        self._ratio = ratio

    # ── size scaling ─────────────────────────────────────────────── #
    #
    # All follower-bound sizes pass through here. Each returns a COPY
    # (dataclasses.replace) so the source event's specs are never
    # mutated — the same event may be inspected/logged elsewhere.

    def _scaled_spec(self, spec: "OrderSpec") -> "OrderSpec":
        if self._ratio == 1.0:
            return spec
        return replace(spec, quantity=scale_quantity(spec.quantity,
                                                      self._ratio))

    def _scaled_bracket(self, bracket: "BracketSpec") -> "BracketSpec":
        if self._ratio == 1.0:
            return bracket
        return replace(
            bracket,
            entry=self._scaled_spec(bracket.entry),
            children=[self._scaled_spec(c) for c in bracket.children],
        )

    def _scaled_modify(self, modify: "ModifySpec") -> "ModifySpec":
        # Only a size change is scaled; a price-only modify is untouched.
        if self._ratio == 1.0 or modify.new_quantity is None:
            return modify
        return replace(modify, new_quantity=scale_quantity(
            modify.new_quantity, self._ratio))

    # ── public entry point ───────────────────────────────────────── #

    def apply(self, event: OrderEvent) -> EventResult:
        """Replicate one OrderEvent. Never raises."""
        try:
            return self._apply_inner(event)
        except Exception as e:  # noqa: BLE001 - must not escape to observer
            logger.exception("EventReplicator internal error: %s", e)
            return EventResult(success=False, skipped=False,
                               reason=f"internal error: {e}")

    def _apply_inner(self, event: OrderEvent) -> EventResult:
        # Account filter — works for any source broker.
        if self._watched and event.source_account_id not in self._watched:
            return EventResult(
                success=False, skipped=True,
                reason=f"source account {event.source_account_id} not in "
                       f"watch list")

        if event.kind is EventKind.NEW:
            return self._apply_new(event)
        if event.kind is EventKind.CANCEL:
            return self._apply_cancel(event)
        if event.kind is EventKind.MODIFY:
            return self._apply_modify(event)
        if event.kind is EventKind.FILL:
            return self._apply_fill(event)
        return EventResult(success=False, skipped=False,
                           reason=f"unknown event kind {event.kind}")

    # ── NEW ──────────────────────────────────────────────────────── #

    def _apply_new(self, event: OrderEvent) -> EventResult:
        symbol = self._follower_symbol(event)
        if symbol is None:
            if event.conid is not None and self._conid_resolver is None:
                reason = (f"event carries only conid {event.conid} and no "
                          f"symbol, and no conid_resolver is configured to "
                          f"map it to a follower symbol")
            elif event.conid is not None:
                reason = (f"conid_resolver could not resolve conid "
                          f"{event.conid} to a follower symbol")
            else:
                reason = "event carries neither a symbol nor a conid"
            return EventResult(success=False, skipped=False, reason=reason)

        if event.bracket is not None:
            return self._apply_new_bracket(event, symbol)
        return self._apply_new_single(event, symbol)

    def _apply_new_single(self, event: OrderEvent, symbol: str) -> EventResult:
        spec = self._scaled_spec(event.order)
        label = event.source_label or event.source_order_id
        if label and self._order_map.record_for_source_label(label) is None:
            # Reserve a slot so a racy fast cancel can find the label
            # even before the follower placement returns.
            self._order_map.add_pending(label)
        try:
            placed = self._follower.place_order(spec, symbol=symbol)
        except Exception as e:  # noqa: BLE001 - follower broker error
            return EventResult(success=False, skipped=False,
                               reason=f"follower place_order failed: {e}")
        if label:
            self._order_map.set_follower_id(label, placed.follower_order_id)
            if event.source_order_id:
                self._order_map.bind_source_id(label, event.source_order_id)
        return EventResult(success=True, skipped=False,
                           reason=f"placed follower order "
                                  f"{placed.follower_order_id}")

    def _apply_new_bracket(self, event: OrderEvent, symbol: str) -> EventResult:
        bracket = self._scaled_bracket(event.bracket)
        entry_label = event.source_label or event.source_order_id
        try:
            placed = self._follower.place_bracket(bracket, symbol=symbol)
        except Exception as e:  # noqa: BLE001
            return EventResult(success=False, skipped=False,
                               reason=f"follower place_bracket failed: {e}")
        # Register entry + children under (possibly synthetic) labels,
        # batched into one disk write.
        with self._order_map.batch():
            if entry_label:
                self._order_map.set_follower_id(entry_label,
                                                placed.entry_order_id)
                if event.source_order_id:
                    self._order_map.bind_source_id(entry_label,
                                                   event.source_order_id)
            for child_spec, child_fid in zip(bracket.children,
                                             placed.child_order_ids):
                clabel = child_spec.source_label
                if not clabel and entry_label:
                    token = _ROLE_TOKEN.get(child_spec.role)
                    if token:
                        clabel = f"{entry_label}#{token}"
                if clabel:
                    self._order_map.set_follower_id(clabel, child_fid)
                    # Bind the child leg's OWN source id too, not just the
                    # entry's. Without this a later MODIFY/CANCEL targeting
                    # a child leg (e.g. moving the stop-loss) can't resolve
                    # source_order_id → follower id, so the event is
                    # silently skipped as "no follower order known" and the
                    # leg change never reaches the follower broker.
                    if child_spec.source_order_id:
                        self._order_map.bind_source_id(
                            clabel, child_spec.source_order_id)
        return EventResult(
            success=True, skipped=False,
            reason=f"placed follower bracket entry={placed.entry_order_id} "
                   f"+ {len(placed.child_order_ids)} child(ren)")

    # ── CANCEL ───────────────────────────────────────────────────── #

    def _apply_cancel(self, event: OrderEvent) -> EventResult:
        sid = event.source_order_id
        # Resolve the cancelled order's source LABEL before we remove it
        # from the map — the OCO cascade needs it to find the sibling.
        label = self._order_map.source_label_for_source_id(sid)
        follower_id = self._order_map.follower_id_for_source_id(sid)
        if follower_id is None:
            return EventResult(
                success=False, skipped=True,
                reason=f"no follower order known for source id {sid} — "
                       f"nothing to cancel")
        try:
            self._follower.cancel_order(follower_id)
        except Exception as e:  # noqa: BLE001
            return EventResult(success=False, skipped=False,
                               reason=f"follower cancel_order failed: {e}")
        self._order_map.remove_by_source_id(sid)
        # OCO cascade: if the cancelled order was a bracket exit leg and
        # the follower does NOT enforce OCO natively, cancel the sibling
        # leg too. Runs AFTER the primary cancel succeeded; a cascade
        # failure never undoes the primary (see _cascade_oco_sibling).
        cascaded = self._cascade_oco_sibling(label)
        reason = f"cancelled follower order {follower_id}"
        if cascaded:
            reason += f" + OCO sibling {cascaded}"
        return EventResult(success=True, skipped=False, reason=reason)

    # ── MODIFY ───────────────────────────────────────────────────── #

    def _apply_modify(self, event: OrderEvent) -> EventResult:
        sid = event.source_order_id
        follower_id = self._order_map.follower_id_for_source_id(sid)
        if follower_id is None:
            return EventResult(
                success=False, skipped=True,
                reason=f"no follower order known for source id {sid} — "
                       f"nothing to modify")
        try:
            self._follower.modify_order(follower_id,
                                        self._scaled_modify(event.modify))
        except Exception as e:  # noqa: BLE001
            return EventResult(success=False, skipped=False,
                               reason=f"follower modify_order failed: {e}")
        return EventResult(success=True, skipped=False,
                           reason=f"modified follower order {follower_id}")

    # ── FILL ─────────────────────────────────────────────────────── #

    def _apply_fill(self, event: OrderEvent) -> EventResult:
        """A FILL is informational for mirroring (we mirror orders, not
        executions) — EXCEPT it is the second trigger of the OCO cascade.

        When a bracket EXIT leg fills (the take-profit executes, or the
        stop-loss is hit), the position is closed and the sibling exit
        leg must go away. A native-OCO follower (IBKR) cancels it itself;
        a non-native one (Tradovate) does not, so we must. We reuse the
        very same cascade as CANCEL, keyed off the filled leg's label.

        Crucially this only fires for EXIT legs: _cascade_oco_sibling
        matches the synthetic #LMT/#STP labels, so an ENTRY fill (which
        OPENS the position and must leave the exits live) carries a label
        without that shape and falls through, cancelling nothing. Same
        for single orders.

        We do NOT remove the filled leg from the map here: unlike a
        cancel, the fill arrives as a separate executionReport and the
        leg's own lifecycle (and any later cancel/cleanup) still resolves
        through the map. Removing the SIBLING is done inside the cascade.
        """
        sid = event.source_order_id
        label = self._order_map.source_label_for_source_id(sid)
        cascaded = self._cascade_oco_sibling(label)
        if cascaded:
            return EventResult(
                success=True, skipped=False,
                reason=f"fill on {sid} cascaded OCO cancel to sibling "
                       f"{cascaded}")
        # Nothing to cascade (entry fill, single order, native-OCO
        # follower, or sibling already gone): informational only.
        logger.debug("FILL event for %s — informational, no OCO cascade",
                     sid)
        return EventResult(success=False, skipped=True,
                           reason="fill event (informational)")

    # ── OCO cascade ──────────────────────────────────────────────── #

    def _cascade_oco_sibling(self, label: Optional[str]) -> Optional[str]:
        """If `label` was a bracket exit leg and the follower does NOT
        enforce OCO natively, cancel the OTHER exit leg too and return
        its follower id; otherwise return None.

        Why: a bracket's take-profit and stop-loss are one-cancels-other.
        IBKR enforces that natively (ocaGroup), but Tradovate's placeoso
        does not group them, so when one leg goes away (cancelled here,
        or filled — see the FILL path) the sibling would be left live.
        We mirror the native OCO by cancelling it explicitly.

        Identification: exit legs live under synthetic labels of the
        form f"{entry}#LMT" / f"{entry}#STP" (see _ROLE_TOKEN). Anything
        without that shape — single orders, the entry leg, custom labels
        — is not an exit leg and falls through unchanged.

        Resilience (mirrors the historical Replicator's hard-won rules):
          * runs only AFTER the primary cancel already succeeded;
          * a sibling that's already gone is success, not error;
          * any failure here is logged but NEVER undoes the primary
            cancel — the source side already shows both legs gone, so the
            least-bad state is the primary done + the user told about the
            orphan. We surface that by returning None (no cascade noted)
            and logging a warning.
        """
        # Skip entirely when the follower groups OCO itself.
        if getattr(self._follower, "native_oco", False):
            return None
        if not label or "#" not in label:
            return None
        entry, _, role = label.rpartition("#")
        sibling_role = _SIBLING_TOKEN.get(role)
        if not entry or sibling_role is None:
            return None
        sibling_label = f"{entry}#{sibling_role}"
        rec = self._order_map.record_for_source_label(sibling_label)
        if rec is None or rec.follower_order_id is None:
            # No sibling on file: single-leg bracket, or it already left
            # the map (e.g. the broker/user cancelled it first). Nothing
            # to do.
            return None
        sibling_fid = rec.follower_order_id
        try:
            self._follower.cancel_order(sibling_fid)
        except Exception as e:  # noqa: BLE001 - cascade must never raise
            # Could be already-gone (fine) or a real failure (orphan).
            # Either way we don't fail the primary; just log + tidy.
            logger.warning(
                "OCO cascade cancel of sibling (label=%s, follower id=%s) "
                "did not complete: %s", sibling_label, sibling_fid, e)
            self._order_map.remove_by_source_label(sibling_label)
            return None
        self._order_map.remove_by_source_label(sibling_label)
        return sibling_fid

    # ── helpers ──────────────────────────────────────────────────── #

    def _follower_symbol(self, event: OrderEvent) -> Optional[str]:
        """The follower-side symbol for this event.

        For a Tradovate source the event already carries the symbol, so
        it passes straight through. For an IBKR source the event carries
        only a conid; if a conid_resolver was injected we use it to map
        conid → follower symbol, otherwise we return None and the caller
        reports a clear failure rather than guessing."""
        if event.symbol:
            return event.symbol
        if event.conid is not None and self._conid_resolver is not None:
            try:
                return self._conid_resolver(event.conid)
            except Exception as e:  # noqa: BLE001 - resolver must not crash us
                logger.warning("conid_resolver(%s) failed: %s",
                               event.conid, e)
                return None
        return None

    # ── startup OrderMap reconciliation ──────────────────────────── #

    # Terminal statuses across BOTH follower vocabularies. Compared
    # case-insensitively. An order in any of these can never receive a
    # further modify/cancel, so its map entry is safe to prune.
    #   Tradovate: Filled, Canceled/Cancelled, Rejected, Expired
    #   IBKR:      Filled, Cancelled, ApiCancelled, Inactive
    # Deliberately CONSERVATIVE: we prune only on a recognised terminal
    # status. Anything else — an active status, an unknown string, or an
    # error querying the follower — leaves the entry untouched, so a
    # transient hiccup or an unfamiliar status can never wipe a valid
    # mapping (the cost of a stray entry is one harmless failed lookup
    # later; the cost of a wrong prune is a lost modify/cancel target).
    _TERMINAL_STATUSES = frozenset({
        "filled", "canceled", "cancelled", "apicancelled",
        "rejected", "expired", "inactive",
    })

    def reconcile_with_follower(self) -> dict:
        """Prune OrderMap entries whose follower order reached a terminal
        state while the engine was down.

        Called once at startup. Walks every mapped entry, asks the
        follower for the order's current status, and removes entries the
        follower reports as terminal (filled/cancelled/…) — so a later
        modify/cancel doesn't wait forever on an order that's already
        gone. Broker-neutral: it asks the FollowerEndpoint, so it works
        whether the follower is IBKR or Tradovate.

        Conservative by design (see _TERMINAL_STATUSES): prunes ONLY on a
        recognised terminal status; an active/unknown status or a query
        error leaves the entry in place. Never raises — returns a small
        stats dict for the startup log line and tests.
        """
        stats = {"checked": 0, "kept": 0, "pruned": 0,
                 "errors": 0, "skipped_no_follower_id": 0}

        labels = self._order_map.source_labels()
        if not labels:
            logger.info("OrderMap reconciliation: map is empty — nothing "
                        "to check.")
            return stats

        logger.info("OrderMap reconciliation: checking %d entr%s against "
                    "the follower…", len(labels),
                    "y" if len(labels) == 1 else "ies")

        for label in labels:
            stats["checked"] += 1
            rec = self._order_map.record_for_source_label(label)
            if rec is None:
                continue
            follower_id = rec.follower_order_id
            if follower_id is None:
                # Placement never completed (still in flight at shutdown,
                # or the source order never made it). Leave it alone — it
                # resolves itself or is harmless.
                stats["skipped_no_follower_id"] += 1
                continue

            try:
                status = self._follower.order_status(follower_id)
            except Exception as e:  # noqa: BLE001 - never prune on error
                logger.warning(
                    "  label=%s follower_id=%s → status query failed (%s) "
                    "— leaving as-is.", label, follower_id, e)
                stats["errors"] += 1
                continue

            if status and status.strip().lower() in self._TERMINAL_STATUSES:
                logger.info("  label=%s follower_id=%s → %s, pruning.",
                            label, follower_id, status)
                self._order_map.remove_by_source_label(label)
                stats["pruned"] += 1
            else:
                stats["kept"] += 1
                logger.debug("  label=%s follower_id=%s → %s (kept).",
                             label, follower_id, status)

        logger.info(
            "OrderMap reconciliation complete: %d kept, %d pruned, "
            "%d errors, %d skipped (no follower id yet).",
            stats["kept"], stats["pruned"], stats["errors"],
            stats["skipped_no_follower_id"])
        return stats
