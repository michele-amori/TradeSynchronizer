"""
Replicator — translates an intercepted IBKR order into the
corresponding Tradovate placeorder call.

Single-responsibility: takes a parsed `IbkrOrder`, applies the
replication policy (mirror vs always-market, skip protective stops),
maps the symbol and order type, then asks the TradovateClient to
submit it.

Errors are caught and logged but never propagate back to the proxy
hook — a failed Tradovate replication must not affect the original
IBKR order, which has already gone through.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from .brokers.ibkr import IbkrContractResolver, ContractResolutionError
from .brokers.tradovate import (
    PlacedOrder,
    TradovateClient,
    TradovateOrderError,
)
from .config import Config
from .proxy.ibkr_parser import IbkrOrder
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
    ):
        self._cfg = cfg
        self._tradovate = tradovate
        self._resolver = resolver

    def replicate(self, ibkr_order: IbkrOrder) -> ReplicationResult:
        """
        Apply policy, map fields, and submit the order. Never raises.
        """
        try:
            return self._replicate_inner(ibkr_order)
        except Exception as e:
            logger.exception("Unexpected replicator failure: %s", e)
            return ReplicationResult(
                success=False, skipped=False,
                reason=f"Internal error: {e}",
            )

    # ------------------------------------------------------------------ #
    #  Implementation                                                     #
    # ------------------------------------------------------------------ #

    def _replicate_inner(self, ibkr_order: IbkrOrder) -> ReplicationResult:
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
                reason=f"Tradovate /contract/find failed for '{tradovate_symbol}': {e}",
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

        return ReplicationResult(
            success=True, skipped=False,
            reason=f"Replicated to Tradovate orderId={placed.order_id}",
            order=placed,
        )
