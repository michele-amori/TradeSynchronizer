"""
Replicator — translates intercepted IBKR order events (new orders,
cancellations, modifications) into the corresponding Tradovate
calls.

Single-responsibility: takes a parsed `IbkrOrder` / `IbkrOrderCancel`
/ `IbkrOrderModify`, applies replication policy, dispatches to
`TradovateClient`, and maintains the persistent `OrderMap` that
links IBKR ids to Tradovate ids across new-order responses,
cancellations and modifications.

Errors are caught and converted into `ReplicationResult` objects;
they never propagate back to the proxy hook — a failed Tradovate
call must not affect the original IBKR order, which has already
gone through.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .brokers.ibkr import IbkrContractResolver, ContractResolutionError
from .brokers.tradovate import (
    PlacedBracket,
    PlacedOrder,
    TradovateClient,
    TradovateOrderError,
    TradovateOrderNotFound,
)
from .config import Config
from .order_map import OrderMap, default_store_path
from .proxy.ibkr_parser import (
    IbkrBracket,
    IbkrOrder,
    IbkrOrderCancel,
    IbkrOrderModify,
)
from .symbols.converter import convert_to_tradovate_format


logger = logging.getLogger("tradesync.replicator")


@dataclass
class ReplicationResult:
    """Outcome of an attempted replication. Useful for the UI / logs."""
    success:  bool
    skipped:  bool
    reason:   str
    order:    Optional[PlacedOrder] = None


# IBKR orderType → Tradovate orderType
_ORDER_TYPE_MAP = {
    "MKT":     "Market",
    "LMT":     "Limit",
    "STP":     "Stop",
    "STP LMT": "StopLimit",
}

# IBKR side → Tradovate action
_ACTION_MAP = {
    "BUY":  "Buy",
    "SELL": "Sell",
}

# IBKR tif → Tradovate timeInForce
_TIF_MAP = {
    "DAY": "Day",
    "GTC": "GTC",
    "IOC": "IOC",
    "FOK": "FOK",
}


class Replicator:

    def __init__(
        self,
        *,
        cfg: Config,
        tradovate: TradovateClient,
        resolver: IbkrContractResolver,
        order_map: Optional[OrderMap] = None,
    ):
        self._cfg = cfg
        self._tradovate = tradovate
        self._resolver = resolver
        # In production the bootstrap creates a per-env persistent
        # OrderMap; tests pass an in-memory or scratch-path version.
        if order_map is None:
            from .config import PROJECT_ROOT
            order_map = OrderMap(
                default_store_path(PROJECT_ROOT, cfg.tradovate_env)
            )
        self._order_map = order_map

    @property
    def order_map(self) -> OrderMap:
        return self._order_map

    # ================================================================== #
    #  New-order replication                                              #
    # ================================================================== #

    def replicate_new(self, parsed) -> ReplicationResult:
        """
        Apply policy, map fields, submit the order to Tradovate, and
        register the cOID → Tradovate orderId mapping so a later
        cancel/modify on this IBKR order can be replicated too.

        Accepts either an `IbkrOrder` (single-leg) or an `IbkrBracket`
        (entry + 1..2 OCO-linked exits). Never raises.
        """
        try:
            if isinstance(parsed, IbkrBracket):
                result = self._replicate_bracket_inner(parsed)
            else:
                result = self._replicate_new_inner(parsed)
        except Exception as e:
            logger.exception("Unexpected replicator failure: %s", e)
            result = ReplicationResult(
                success=False, skipped=False,
                reason=f"Internal error: {e}",
            )
        # Emit a structured DIVERGENCE event when we failed (and
        # didn't merely skip-by-policy). The GUI parses these lines to
        # render the per-env Sync-health panel; CLI mode just logs.
        if not result.success and not result.skipped:
            self._emit_divergence(parsed, kind=(
                "bracket" if isinstance(parsed, IbkrBracket) else "new"
            ), reason=result.reason)
        return result

    def _replicate_new_inner(self, ibkr_order: IbkrOrder) -> ReplicationResult:
        logger.debug("_replicate_new_inner: entering with %s", ibkr_order)

        # ── Policy: account filter ────────────────────────────────────── #
        watched = self._cfg.ibkr_watched_accounts
        if watched and ibkr_order.account_id not in watched:
            logger.debug("filter HIT: account %s not in watched=%s",
                         ibkr_order.account_id, watched)
            return ReplicationResult(
                success=False, skipped=True,
                reason=f"IBKR account {ibkr_order.account_id} not in watch list",
            )

        # ── Policy: skip protective stops ────────────────────────────── #
        if self._cfg.skip_protective_stops and ibkr_order.is_protective_stop:
            logger.debug("filter HIT: protective stop, type=%s",
                         ibkr_order.order_type)
            return ReplicationResult(
                success=False, skipped=True,
                reason=f"Skipping protective {ibkr_order.order_type} order "
                       f"(SKIP_PROTECTIVE_STOPS=true)",
            )

        # Reserve a slot in the map immediately, so a racy fast cancel
        # can at least find the cOID even before Tradovate replies.
        if ibkr_order.cOID:
            self._order_map.add_pending(ibkr_order.cOID)
            logger.debug("order map: added pending entry for cOID=%s",
                         ibkr_order.cOID)

        # ── Symbol resolution: conid → IBKR symbol → Tradovate symbol ── #
        try:
            ibkr_symbol = self._resolver.resolve_symbol(ibkr_order.conid)
        except ContractResolutionError as e:
            return ReplicationResult(
                success=False, skipped=False,
                reason=f"Could not resolve conid={ibkr_order.conid}: {e}",
            )

        tradovate_symbol = convert_to_tradovate_format(ibkr_symbol)
        logger.info("Symbol map: conid=%d → IBKR='%s' → Tradovate='%s'",
                    ibkr_order.conid, ibkr_symbol, tradovate_symbol)

        # ── Tradovate contract id ─────────────────────────────────────── #
        try:
            contract_id = self._tradovate.get_contract_id(tradovate_symbol)
        except TradovateOrderError as e:
            return ReplicationResult(
                success=False, skipped=False,
                reason=f"Tradovate /contract/find failed for "
                       f"'{tradovate_symbol}': {e}",
            )

        # ── Order type & price mapping ───────────────────────────────── #
        action = _ACTION_MAP[ibkr_order.side]

        if self._cfg.replication_mode == "market":
            tv_order_type = "Market"
            limit_price = None
            stop_price = None
        else:
            tv_order_type = _ORDER_TYPE_MAP[ibkr_order.order_type]
            limit_price = (ibkr_order.price
                           if tv_order_type in ("Limit", "StopLimit") else None)
            stop_price = (ibkr_order.aux_price
                          if tv_order_type in ("Stop", "StopLimit") else None)

        tif = _TIF_MAP.get(ibkr_order.tif, "Day")

        # ── Submit ───────────────────────────────────────────────────── #
        try:
            placed = self._tradovate.place_order(
                tradovate_symbol=tradovate_symbol,
                contract_id=contract_id,
                action=action,
                qty=ibkr_order.quantity,
                order_type=tv_order_type,
                limit_price=limit_price,
                stop_price=stop_price,
                tif=tif,
            )
        except (TradovateOrderError, ValueError) as e:
            return ReplicationResult(
                success=False, skipped=False,
                reason=f"Tradovate placeorder failed: {e}",
            )

        # Register the mapping for any subsequent cancel/modify on
        # this IBKR order.
        if ibkr_order.cOID:
            self._order_map.set_tradovate_id(ibkr_order.cOID, placed.order_id)

        return ReplicationResult(
            success=True, skipped=False,
            reason=f"Replicated to Tradovate orderId={placed.order_id}",
            order=placed,
        )

    # ================================================================== #
    #  Bracket replication                                                #
    # ================================================================== #

    def _replicate_bracket_inner(
        self, bracket: IbkrBracket
    ) -> ReplicationResult:
        entry = bracket.entry
        children = bracket.children

        # ── Policy: account filter ─────────────────────────────────── #
        watched = self._cfg.ibkr_watched_accounts
        if watched and entry.account_id not in watched:
            return ReplicationResult(
                success=False, skipped=True,
                reason=f"IBKR account {entry.account_id} not in watch list",
            )

        # NOTE: SKIP_PROTECTIVE_STOPS deliberately does NOT apply to
        # brackets. The stop-loss child here is part of a coordinated
        # bracket and must be replicated together with the entry and
        # take-profit to keep the Tradovate position protected.

        # Reserve slots in the map for every leg, so even a racy fast
        # cancel can find a cOID.
        for leg_coid in [entry.cOID] + [c.cOID for c in children]:
            if leg_coid:
                self._order_map.add_pending(leg_coid)

        # ── Symbol resolution ──────────────────────────────────────── #
        try:
            ibkr_symbol = self._resolver.resolve_symbol(entry.conid)
        except ContractResolutionError as e:
            return ReplicationResult(
                success=False, skipped=False,
                reason=f"Could not resolve conid={entry.conid}: {e}",
            )
        tradovate_symbol = convert_to_tradovate_format(ibkr_symbol)
        logger.info("Bracket symbol map: conid=%d → IBKR='%s' → Tradovate='%s'",
                    entry.conid, ibkr_symbol, tradovate_symbol)

        try:
            contract_id = self._tradovate.get_contract_id(tradovate_symbol)
        except TradovateOrderError as e:
            return ReplicationResult(
                success=False, skipped=False,
                reason=f"Tradovate /contract/find failed for "
                       f"'{tradovate_symbol}': {e}",
            )

        # ── Entry field mapping ────────────────────────────────────── #
        entry_action = _ACTION_MAP[entry.side]
        if self._cfg.replication_mode == "market":
            entry_tv_type = "Market"
            entry_limit = None
            entry_stop = None
        else:
            entry_tv_type = _ORDER_TYPE_MAP[entry.order_type]
            entry_limit = (entry.price
                           if entry_tv_type in ("Limit", "StopLimit") else None)
            entry_stop = (entry.aux_price
                          if entry_tv_type in ("Stop", "StopLimit") else None)
        entry_tif = _TIF_MAP.get(entry.tif, "Day")

        # ── Child legs payload ─────────────────────────────────────── #
        bracket_payloads: list = []
        for child in children:
            child_tv_type = _ORDER_TYPE_MAP[child.order_type]
            bracket_payloads.append({
                "action":      _ACTION_MAP[child.side],
                "order_type":  child_tv_type,
                "qty":         child.quantity,
                "limit_price": child.price
                    if child_tv_type in ("Limit", "StopLimit") else None,
                "stop_price":  child.aux_price
                    if child_tv_type in ("Stop", "StopLimit") else None,
                "tif":         _TIF_MAP.get(child.tif, "Day"),
            })

        # ── Submit ─────────────────────────────────────────────────── #
        try:
            placed: PlacedBracket = self._tradovate.place_bracket(
                tradovate_symbol=tradovate_symbol,
                contract_id=contract_id,
                entry_action=entry_action,
                entry_qty=entry.quantity,
                entry_order_type=entry_tv_type,
                entry_limit_price=entry_limit,
                entry_stop_price=entry_stop,
                entry_tif=entry_tif,
                brackets=bracket_payloads,
            )
        except (TradovateOrderError, ValueError) as e:
            return ReplicationResult(
                success=False, skipped=False,
                reason=f"Tradovate placeoso failed: {e}",
            )

        # Register one map entry per leg so any leg can be cancelled
        # or modified independently later. Batched so the bracket's
        # 1+N writes collapse into a single disk flush (the json file
        # write is ~1 ms on SSD and we're inside the order-replication
        # hot path).
        with self._order_map.batch():
            if entry.cOID:
                self._order_map.set_tradovate_id(entry.cOID, placed.entry_order_id)
            for child, tv_id in zip(children, placed.bracket_ids):
                if child.cOID:
                    self._order_map.set_tradovate_id(child.cOID, tv_id)

        child_summary = ", ".join(
            f"{c.order_type}@{c.price or c.aux_price}"
            for c in children
        ) or "(none)"
        return ReplicationResult(
            success=True, skipped=False,
            reason=(
                f"Replicated bracket: entry orderId={placed.entry_order_id} "
                f"+ {len(placed.bracket_ids)} exit(s) "
                f"[{child_summary}] on Tradovate (ocoId={placed.oco_id})"
            ),
        )

    # ================================================================== #
    #  IBKR response binding (cOID → IBKR order_id)                       #
    # ================================================================== #

    def register_ibkr_id(self, coid: str, ibkr_order_id: str) -> None:
        """
        Called by the addon's response hook when the IBKR new-order
        POST returns. Binds the IBKR-assigned order_id to the cOID
        we already stored, so the URL of a future DELETE/PUT can be
        looked up successfully.
        """
        self._order_map.set_ibkr_id(coid, ibkr_order_id)

    # ================================================================== #
    #  Cancellation                                                       #
    # ================================================================== #

    def replicate_cancel(
        self, cancel: IbkrOrderCancel
    ) -> ReplicationResult:
        """Look up the Tradovate orderId for the given IBKR order_id
        and call /order/cancelorder. Never raises."""
        try:
            result = self._replicate_cancel_inner(cancel)
        except Exception as e:
            logger.exception("Unexpected cancel failure: %s", e)
            result = ReplicationResult(
                success=False, skipped=False,
                reason=f"Internal error during cancel: {e}",
            )
        if not result.success and not result.skipped:
            self._emit_divergence(cancel, kind="cancel", reason=result.reason)
        return result

    def _replicate_cancel_inner(
        self, cancel: IbkrOrderCancel
    ) -> ReplicationResult:
        watched = self._cfg.ibkr_watched_accounts
        if watched and cancel.account_id not in watched:
            return ReplicationResult(
                success=False, skipped=True,
                reason=f"IBKR account {cancel.account_id} not in watch list",
            )

        tradovate_id = self._order_map.tradovate_for_ibkr_id(
            cancel.ibkr_order_id
        )
        if tradovate_id is None:
            # Either we never replicated this order (e.g. it was
            # placed before TradeSynchronizer started, or filtered
            # out by SKIP_PROTECTIVE_STOPS / watch list), or the
            # Tradovate placeorder hasn't returned yet (rare race).
            return ReplicationResult(
                success=False, skipped=True,
                reason=f"No Tradovate replica known for IBKR order "
                       f"{cancel.ibkr_order_id} — nothing to cancel.",
            )

        try:
            self._tradovate.cancel_order(tradovate_id)
        except TradovateOrderNotFound as e:
            # Already filled / cancelled out-of-band. Tidy up and
            # report as a skip rather than a failure.
            self._order_map.remove_by_ibkr_id(cancel.ibkr_order_id)
            return ReplicationResult(
                success=False, skipped=True,
                reason=f"Tradovate order {tradovate_id} already gone: {e}",
            )
        except (TradovateOrderError, ValueError) as e:
            return ReplicationResult(
                success=False, skipped=False,
                reason=f"Tradovate cancelorder failed: {e}",
            )

        self._order_map.remove_by_ibkr_id(cancel.ibkr_order_id)
        return ReplicationResult(
            success=True, skipped=False,
            reason=f"Cancelled Tradovate orderId={tradovate_id} "
                   f"(IBKR id={cancel.ibkr_order_id})",
        )

    # ================================================================== #
    #  Modification                                                       #
    # ================================================================== #

    def replicate_modify(
        self, modify: IbkrOrderModify
    ) -> ReplicationResult:
        """Look up the Tradovate orderId and call /order/modifyorder
        with the changed fields. Never raises."""
        try:
            result = self._replicate_modify_inner(modify)
        except Exception as e:
            logger.exception("Unexpected modify failure: %s", e)
            result = ReplicationResult(
                success=False, skipped=False,
                reason=f"Internal error during modify: {e}",
            )
        if not result.success and not result.skipped:
            self._emit_divergence(modify, kind="modify", reason=result.reason)
        return result

    def _replicate_modify_inner(
        self, modify: IbkrOrderModify
    ) -> ReplicationResult:
        watched = self._cfg.ibkr_watched_accounts
        if watched and modify.account_id not in watched:
            return ReplicationResult(
                success=False, skipped=True,
                reason=f"IBKR account {modify.account_id} not in watch list",
            )

        tradovate_id = self._order_map.tradovate_for_ibkr_id(
            modify.ibkr_order_id
        )
        if tradovate_id is None:
            return ReplicationResult(
                success=False, skipped=True,
                reason=f"No Tradovate replica known for IBKR order "
                       f"{modify.ibkr_order_id} — nothing to modify.",
            )

        # In 'market' replication mode we forced everything to Market
        # at placement time; modifying price/stop on a Market order is
        # meaningless. Quantity changes still work — pass them through.
        if self._cfg.replication_mode == "market":
            qty = modify.quantity
            limit_price = None
            stop_price = None
            tif = None
        else:
            qty = modify.quantity
            limit_price = modify.price
            stop_price = modify.aux_price
            tif = (
                _TIF_MAP.get(modify.tif) if modify.tif else None
            )

        if qty is None and limit_price is None and stop_price is None \
                and tif is None:
            return ReplicationResult(
                success=False, skipped=True,
                reason=f"Modify on IBKR {modify.ibkr_order_id}: no "
                       f"replicable fields changed.",
            )

        try:
            self._tradovate.modify_order(
                tradovate_id,
                qty=qty,
                limit_price=limit_price,
                stop_price=stop_price,
                tif=tif,
            )
        except TradovateOrderNotFound as e:
            self._order_map.remove_by_ibkr_id(modify.ibkr_order_id)
            return ReplicationResult(
                success=False, skipped=True,
                reason=f"Tradovate order {tradovate_id} already gone: {e}",
            )
        except (TradovateOrderError, ValueError) as e:
            return ReplicationResult(
                success=False, skipped=False,
                reason=f"Tradovate modifyorder failed: {e}",
            )

        parts = []
        if qty is not None:
            parts.append(f"qty={qty}")
        if limit_price is not None:
            parts.append(f"limit={limit_price}")
        if stop_price is not None:
            parts.append(f"stop={stop_price}")
        if tif is not None:
            parts.append(f"tif={tif}")
        return ReplicationResult(
            success=True, skipped=False,
            reason=(
                f"Modified Tradovate orderId={tradovate_id} "
                f"(IBKR id={modify.ibkr_order_id}): {', '.join(parts) or '(none)'}"
            ),
        )

    # ================================================================== #
    #  Divergence emit                                                    #
    # ================================================================== #

    def _emit_divergence(self, parsed, *, kind: str, reason: str) -> None:
        """
        Log a structured `DIVERGENCE {json}` line describing a failed
        replication. The GUI tails subprocess stdout, parses these
        lines, and surfaces them in the per-env Sync-health panel.

        In CLI mode (no GUI) the line still ends up in the rotating
        log file, so the trader can review missed replications later.
        """
        payload = {
            "ts":      time.time(),
            "env":     self._cfg.tradovate_env,
            "kind":    kind,
            "reason":  reason,
            "summary": self._format_divergence_summary(parsed),
            "ref":     self._divergence_ref(parsed),
        }
        try:
            line = json.dumps(payload, default=str)
        except (TypeError, ValueError) as e:
            line = json.dumps({
                "ts": payload["ts"], "env": payload["env"],
                "kind": kind, "reason": reason,
                "summary": "(serialisation failed: " + str(e) + ")",
                "ref": "",
            })
        logger.warning("DIVERGENCE %s", line)

    @staticmethod
    def _format_divergence_summary(parsed) -> str:
        if isinstance(parsed, IbkrBracket):
            e = parsed.entry
            return (
                f"BRACKET {e.side} {e.quantity} conid={e.conid} "
                f"{e.order_type}"
                + (f"@{e.price}" if e.price is not None else "")
                + f" + {len(parsed.children)} exit(s)"
            )
        if isinstance(parsed, IbkrOrder):
            return (
                f"{parsed.side} {parsed.quantity} conid={parsed.conid} "
                f"{parsed.order_type}"
                + (f"@{parsed.price}" if parsed.price is not None else "")
            )
        if isinstance(parsed, IbkrOrderCancel):
            return f"CANCEL ibkr_id={parsed.ibkr_order_id}"
        if isinstance(parsed, IbkrOrderModify):
            return (
                f"MODIFY ibkr_id={parsed.ibkr_order_id}"
                + (f" qty={parsed.quantity}" if parsed.quantity else "")
                + (f" price={parsed.price}" if parsed.price else "")
                + (f" stop={parsed.aux_price}" if parsed.aux_price else "")
            )
        return repr(parsed)

    @staticmethod
    def _divergence_ref(parsed) -> str:
        """Best-effort identifier to cross-reference this divergence
        with its cOID or IBKR id in the log."""
        if isinstance(parsed, IbkrBracket):
            return parsed.entry.cOID or ""
        if isinstance(parsed, IbkrOrder):
            return parsed.cOID or ""
        if isinstance(parsed, (IbkrOrderCancel, IbkrOrderModify)):
            return parsed.ibkr_order_id
        return ""

    # ================================================================== #
    #  Startup reconciliation                                             #
    # ================================================================== #

    # Tradovate `ordStatus` values that mean "this order is still
    # active on the book". Anything outside this set is terminal and
    # the OrderMap entry can be removed.
    _ACTIVE_STATUSES = frozenset({"Working", "PendingNew", "PendingReplace",
                                  "PendingCancel", "Accepted"})

    def reconcile_with_tradovate(self) -> dict:
        """
        Walk every entry in the persistent OrderMap and verify that
        the matched Tradovate order is still active. Prune entries
        whose Tradovate side is Filled / Cancelled / Rejected /
        Expired / unknown.

        Designed to run once at engine startup, AFTER tradovate.connect()
        succeeds. Bounded by the number of entries in the map (typically
        single digits) × the latency of /order/item (≈ 100-300 ms each).

        Never raises: a Tradovate outage during reconciliation logs a
        warning and leaves the map untouched (we'd rather start with
        a stale-but-recoverable map than refuse to boot the engine).

        Returns a small stats dict — useful for tests and for the
        startup log line.
        """
        stats = {"checked": 0, "kept": 0, "pruned": 0,
                 "errors": 0, "skipped_no_tv_id": 0}

        # Snapshot the cOIDs up front: we'll mutate the map inside
        # the loop and don't want to iterate over a moving target.
        # Access the internal _by_coid directly under the lock-safe
        # OrderMap API: there's no public "list all" so we use
        # get_by_coid by extracting keys.
        with self._order_map._lock:
            coids = list(self._order_map._by_coid.keys())

        if not coids:
            logger.info("Reconciliation: order map is empty — nothing to check.")
            return stats

        logger.info("Reconciliation: checking %d order map entr%s against Tradovate…",
                    len(coids), "y" if len(coids) == 1 else "ies")

        for coid in coids:
            stats["checked"] += 1
            rec = self._order_map.get_by_coid(coid)
            if rec is None:
                continue
            if rec.tradovate_id is None:
                # No Tradovate replica was ever recorded for this
                # cOID — either the placeorder is still in flight
                # (race on startup), or the original IBKR POST never
                # succeeded. Either way: leave the entry alone, it
                # will resolve itself.
                stats["skipped_no_tv_id"] += 1
                continue

            try:
                status = self._tradovate.get_order_status(rec.tradovate_id)
            except TradovateOrderNotFound:
                logger.info(
                    "  cOID=%s tv_id=%s → not found on Tradovate, pruning.",
                    coid, rec.tradovate_id,
                )
                self._order_map.remove_by_coid(coid)
                stats["pruned"] += 1
                continue
            except (TradovateOrderError, ValueError) as e:
                # Don't prune on transient errors — we don't want a
                # one-off HTTP 503 to wipe valid mappings.
                logger.warning(
                    "  cOID=%s tv_id=%s → reconcile error (%s) — leaving as-is.",
                    coid, rec.tradovate_id, e,
                )
                stats["errors"] += 1
                continue

            if status in self._ACTIVE_STATUSES:
                stats["kept"] += 1
                logger.debug("  cOID=%s tv_id=%s → %s (kept).",
                             coid, rec.tradovate_id, status)
            else:
                logger.info(
                    "  cOID=%s tv_id=%s → %s, pruning.",
                    coid, rec.tradovate_id, status,
                )
                self._order_map.remove_by_coid(coid)
                stats["pruned"] += 1

        logger.info(
            "Reconciliation complete: %d kept, %d pruned, %d errors, "
            "%d skipped (no Tradovate id yet).",
            stats["kept"], stats["pruned"], stats["errors"],
            stats["skipped_no_tv_id"],
        )
        return stats
