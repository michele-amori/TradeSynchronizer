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

import logging
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
    IbkrBracketChild,
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
                return self._replicate_bracket_inner(parsed)
            return self._replicate_new_inner(parsed)
        except Exception as e:
            logger.exception("Unexpected replicator failure: %s", e)
            return ReplicationResult(
                success=False, skipped=False,
                reason=f"Internal error: {e}",
            )

    # Back-compat alias for callers that still use the old name.
    replicate = replicate_new

    def _replicate_new_inner(self, ibkr_order: IbkrOrder) -> ReplicationResult:
        # ── Policy: account filter ────────────────────────────────────── #
        watched = self._cfg.ibkr_watched_accounts
        if watched and ibkr_order.account_id not in watched:
            return ReplicationResult(
                success=False, skipped=True,
                reason=f"IBKR account {ibkr_order.account_id} not in watch list",
            )

        # ── Policy: skip protective stops ────────────────────────────── #
        if self._cfg.skip_protective_stops and ibkr_order.is_protective_stop:
            return ReplicationResult(
                success=False, skipped=True,
                reason=f"Skipping protective {ibkr_order.order_type} order "
                       f"(SKIP_PROTECTIVE_STOPS=true)",
            )

        # Reserve a slot in the map immediately, so a racy fast cancel
        # can at least find the cOID even before Tradovate replies.
        if ibkr_order.cOID:
            self._order_map.add_pending(ibkr_order.cOID)

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
        # or modified independently later.
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
            return self._replicate_cancel_inner(cancel)
        except Exception as e:
            logger.exception("Unexpected cancel failure: %s", e)
            return ReplicationResult(
                success=False, skipped=False,
                reason=f"Internal error during cancel: {e}",
            )

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
            return self._replicate_modify_inner(modify)
        except Exception as e:
            logger.exception("Unexpected modify failure: %s", e)
            return ReplicationResult(
                success=False, skipped=False,
                reason=f"Internal error during modify: {e}",
            )

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
