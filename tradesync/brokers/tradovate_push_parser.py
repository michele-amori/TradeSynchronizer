"""
tradovate_push_parser — turn Tradovate user-data WebSocket push frames
into broker-neutral OrderEvents.

Calibrated against REAL frames captured live 2026-06-14 (market open,
MNQM2026 bracket on DEMO account 50000001 — see
captures/tradovate_frames_2026-06-14.json). The shape is more involved
than a single self-contained order frame, so this is a STATEFUL parser:

How Tradovate models an order over the wire
-------------------------------------------
A single order's information is SPLIT across several entity types, each
arriving as its own push frame `{"e":"props","d":{"entityType":...,
"eventType":"Created"|"Updated","entity":{...}}}`:

  * `order`            — identity + status: id, accountId, contractId,
                         action (Buy/Sell), ordStatus (Unknown →
                         PendingNew → Working → Filled/Canceled).
                         NO price / qty / type.
  * `orderVersion`     — the mutable details, linked by `orderId`:
                         orderQty, orderType (Limit/Stop/Market/
                         StopLimit), price, stopPrice, timeInForce.
                         A MODIFY creates a NEW orderVersion (new id,
                         same orderId). NO accountId.
  * `orderStrategyLink`— ties an order to a strategy (a bracket):
                         orderId, orderStrategyId.
  * `orderStrategy`    — the bracket as a whole (params JSON). Used
                         only to recognise that a group of orders form
                         one bracket; the LIVE prices come from each
                         leg's orderVersion, not from params (params
                         keeps the original setup even after a modify).
  * `executionReport`  — the lifecycle TRIGGER: execType New / Replaced
                         / Canceled / Filled, carrying orderId,
                         accountId, contractId, action, ordStatus.

So no single frame is enough. This parser accumulates per-order state
as the constituent frames arrive, and EMITS a neutral OrderEvent when
an `executionReport` tells us something actually happened:

  execType  New      → EventKind.NEW    (single, or a bracket once all
                                          of a strategy's legs are New)
  execType  Replaced → EventKind.MODIFY
  execType  Canceled → EventKind.CANCEL
  execType  Filled / Trade → EventKind.FILL (informational)

Bracket assembly (the "Strada B" / native-bracket path)
-------------------------------------------------------
Orders sharing an `orderStrategyId` are one bracket. When the LAST leg
of a strategy reaches New, we emit a single NEW event carrying a
BracketSpec (entry + TP + SL), so the IBKR follower can place a native
OCO bracket rather than three unlinked orders. The entry is the leg
whose action matches the strategy action; the children are classified
TAKE_PROFIT (a Limit exit) / STOP_LOSS (a Stop exit) by their order
type. Subsequent per-leg Replaced / Canceled events are emitted as
MODIFY / CANCEL against that leg's own source id, so the follower can
adjust or tear down individual legs.

Symbol resolution
-----------------
Frames carry only a numeric `contractId`. We resolve it to a Tradovate
symbol (e.g. 4327110 → "MNQM6") via an injected resolver
(TradovateClient.get_contract_name), cached by that client. The neutral
event then carries `symbol`, which the IBKR follower resolves to its
own Contract.

Bracket-leg MODIFY attribution
------------------------------
Tradovate fires the `executionReport execType=Replaced` against the
ENTRY's orderId (the strategy parent) even when a CHILD leg is the one
that changed, and it ALSO re-sends the entry's (unchanged) orderVersion
on every leg modify. So neither the executionReport's orderId nor "an
orderVersion arrived for leg X" reliably identifies what moved. This
parser instead keeps a per-leg fingerprint (qty, type, price, stop) of
each leg as last emitted; on a bracket Replaced it emits a MODIFY for
every leg whose fingerprint actually changed, against that leg's own
source id. Verified against the real capture: modifying the entry, the
take-profit, and the stop-loss each produces exactly one MODIFY routed
to the correct leg, with no redundant entry MODIFYs.

Safety
------
Anything not understood yields no event (the observer logs + drops it).
A frame for another account is ignored. The parser never raises into
the observer's listener thread; callers wrap it, and it returns [] on
anything malformed.
"""

from __future__ import annotations

import json
import logging
from typing import Callable, Dict, List, Optional

from ..order_event import (
    BracketRole,
    BracketSpec,
    EventKind,
    ModifySpec,
    OrderEvent,
    OrderSpec,
    OrderType,
    Side,
    TimeInForce,
)


logger = logging.getLogger("tradesync.tradovate_push")


# ── Tradovate wire → neutral maps (inverse of tradovate_endpoint) ────── #

_SIDE_FROM_TRADOVATE = {
    "Buy":  Side.BUY,
    "Sell": Side.SELL,
}

_ORDER_TYPE_FROM_TRADOVATE = {
    "Market":    OrderType.MARKET,
    "Limit":     OrderType.LIMIT,
    "Stop":      OrderType.STOP,
    "StopLimit": OrderType.STOP_LIMIT,
}

_TIF_FROM_TRADOVATE = {
    "Day": TimeInForce.DAY,
    "GTC": TimeInForce.GTC,
    "IOC": TimeInForce.IOC,
    "FOK": TimeInForce.FOK,
}


class _OrderState:
    """Accumulated knowledge about one Tradovate order, built up as its
    constituent frames (order / orderVersion / link) arrive."""

    __slots__ = ("order_id", "account_id", "contract_id", "action",
                 "strategy_id", "qty", "order_type",
                 "price", "stop_price", "tif", "emitted_new",
                 "emitted_fingerprint")

    def __init__(self, order_id: str):
        self.order_id = order_id
        self.account_id: Optional[str] = None
        self.contract_id: Optional[int] = None
        self.action: Optional[str] = None
        self.strategy_id: Optional[str] = None
        self.qty: Optional[int] = None
        self.order_type: Optional[str] = None
        self.price: Optional[float] = None
        self.stop_price: Optional[float] = None
        self.tif: Optional[str] = None
        self.emitted_new = False
        # Fingerprint (qty, type, price, stopPrice) of this leg as last
        # emitted (NEW or MODIFY). Used to attribute a bracket Replaced
        # to the leg whose VALUE actually changed: Tradovate fires the
        # Replaced executionReport against the ENTRY's orderId and also
        # re-sends the entry's (unchanged) orderVersion even when only a
        # child leg moved, so "received a new version" isn't enough — we
        # compare the actual values.
        self.emitted_fingerprint: Optional[tuple] = None

    @property
    def fingerprint(self) -> tuple:
        """Current (qty, type, price, stopPrice) — what a modify would
        carry. Compared against emitted_fingerprint to detect a real
        change."""
        return (self.qty, self.order_type, self.price, self.stop_price)

    def to_spec(self, role: BracketRole = BracketRole.ENTRY) -> Optional[OrderSpec]:
        """Build a neutral OrderSpec from the accumulated detail, or
        None if we don't yet have enough (type/qty/side)."""
        side = _SIDE_FROM_TRADOVATE.get(self.action or "")
        otype = _ORDER_TYPE_FROM_TRADOVATE.get(self.order_type or "")
        if side is None or otype is None or self.qty is None:
            return None
        return OrderSpec(
            side=side,
            quantity=int(self.qty),
            order_type=otype,
            limit_price=self.price if otype in (
                OrderType.LIMIT, OrderType.STOP_LIMIT) else None,
            stop_price=self.stop_price if otype in (
                OrderType.STOP, OrderType.STOP_LIMIT) else None,
            tif=_TIF_FROM_TRADOVATE.get(self.tif or "Day", TimeInForce.DAY),
            source_order_id=self.order_id,
            source_label=self.order_id,
            role=role,
        )


class TradovatePushParser:
    """Stateful translator: feed it decoded push entity frames; it emits
    OrderEvents when something actionable happens.

    Parameters
    ----------
    account_id:
        The observed account, as Tradovate stamps it on order frames
        (the internal primary-key id). Frames for other accounts are
        ignored.
    resolve_symbol:
        contract_id (int) → Tradovate symbol (str). Injected so the
        parser stays unit-testable without network; in production it's
        TradovateClient.get_contract_name.
    report_account_id:
        The account id to put on emitted OrderEvents' source_account_id.
        Defaults to account_id. This exists because Tradovate stamps
        frames with the internal id (used for matching) while the rest
        of the system identifies the account by its configured number;
        emitting the configured number keeps the downstream account
        filter consistent with config/replication.json.
    """

    def __init__(self, account_id: str,
                 resolve_symbol: Callable[[int], str],
                 report_account_id: Optional[str] = None):
        self._account_id = str(account_id)
        self._report_account_id = (
            str(report_account_id) if report_account_id is not None
            else str(account_id))
        self._resolve_symbol = resolve_symbol
        self._orders: Dict[str, _OrderState] = {}
        # strategy id → list of order ids belonging to it (in arrival
        # order), so we can assemble a bracket once its legs are known.
        self._strategies: Dict[str, List[str]] = {}
        # strategies we've already emitted a NEW bracket for, so later
        # legs/updates don't re-emit it.
        self._bracket_emitted: set[str] = set()
        # strategies whose ENTRY leg has had its executionReport New —
        # the signal the bracket is live. The OCO child legs never get
        # their own New (they sit Suspended until the entry fills), so
        # we can't wait for "all legs New"; instead we arm on the
        # entry's New and emit once all linked legs' details have
        # arrived (they stream in within milliseconds AFTER the entry's
        # New).
        self._strategy_armed: set[str] = set()
        # strategy id → number of legs to expect, parsed from the
        # orderStrategy params (1 entry + one per profitTarget/stopLoss).
        # Lets us hold bracket emission until EVERY leg has linked in,
        # rather than firing as soon as 2 happen to be ready (which
        # would drop the still-arriving stop-loss leg).
        self._strategy_expected_legs: Dict[str, int] = {}

    # ── public entry point ───────────────────────────────────────── #

    def handle(self, entity_type: str, event_type: str,
               entity: dict) -> List[OrderEvent]:
        """Feed one decoded push entity. Returns 0+ OrderEvents."""
        try:
            if entity_type == "order":
                return self._on_order(event_type, entity)
            if entity_type == "orderVersion":
                order_id = entity.get("orderId")
                self._on_order_version(entity)
                # A late-arriving leg detail may complete an armed
                # bracket — retry emission.
                return self._retry_armed_for_order(order_id)
            if entity_type == "orderStrategyLink":
                self._on_strategy_link(entity)
                return self._retry_armed_for_order(entity.get("orderId"))
            if entity_type == "orderStrategy":
                sid = self._on_order_strategy(entity)
                # Learning the expected leg count may complete an armed
                # bracket that was waiting for it.
                return self._maybe_emit_bracket(sid)
            if entity_type == "executionReport":
                return self._on_execution_report(entity)
            # everything else: no direct event.
            return []
        except Exception as e:  # noqa: BLE001 - never break the listener
            logger.exception("Tradovate push parse error on %s: %s",
                             entity_type, e)
            return []

    # ── state accumulation ───────────────────────────────────────── #

    def _state(self, order_id) -> _OrderState:
        oid = str(order_id)
        st = self._orders.get(oid)
        if st is None:
            st = _OrderState(oid)
            self._orders[oid] = st
        return st

    def _on_order(self, event_type: str, entity: dict) -> List[OrderEvent]:
        oid = entity.get("id")
        if oid is None:
            return []
        st = self._state(oid)
        if "accountId" in entity:
            st.account_id = str(entity["accountId"])
        if "contractId" in entity:
            st.contract_id = entity["contractId"]
        if "action" in entity:
            st.action = entity["action"]
        return []

    def _on_order_version(self, entity: dict) -> None:
        order_id = entity.get("orderId")
        if order_id is None:
            return
        st = self._state(order_id)
        if "orderQty" in entity:
            st.qty = entity["orderQty"]
        if "orderType" in entity:
            st.order_type = entity["orderType"]
        # price / stopPrice only present for the relevant type; update
        # whatever is given (a modify sends a fresh version).
        if "price" in entity:
            st.price = entity["price"]
        if "stopPrice" in entity:
            st.stop_price = entity["stopPrice"]
        if "timeInForce" in entity:
            st.tif = entity["timeInForce"]

    def _on_strategy_link(self, entity: dict) -> None:
        order_id = entity.get("orderId")
        strategy_id = entity.get("orderStrategyId")
        if order_id is None or strategy_id is None:
            return
        st = self._state(order_id)
        st.strategy_id = str(strategy_id)
        legs = self._strategies.setdefault(str(strategy_id), [])
        if str(order_id) not in legs:
            legs.append(str(order_id))

    def _on_order_strategy(self, entity: dict) -> Optional[str]:
        """Parse an orderStrategy frame to learn how many legs the
        bracket has. params is a JSON STRING: an entry plus a
        `brackets` list, each entry contributing a profitTarget and/or
        stopLoss exit. Expected legs = 1 (entry) + number of exits.
        Returns the strategy id (so the caller can retry emission)."""
        sid = entity.get("id")
        if sid is None:
            return None
        sid = str(sid)
        raw = entity.get("params")
        if isinstance(raw, str) and raw:
            try:
                params = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return sid
            exits = 0
            for b in params.get("brackets", []) or []:
                if not isinstance(b, dict):
                    continue
                if b.get("profitTarget") is not None:
                    exits += 1
                if b.get("stopLoss") is not None:
                    exits += 1
            if exits:
                # Keep the largest count we've seen (an Updated frame
                # could in principle differ; the bracket only grows).
                prev = self._strategy_expected_legs.get(sid, 0)
                self._strategy_expected_legs[sid] = max(prev, 1 + exits)
        return sid

    # ── the trigger: executionReport ─────────────────────────────── #

    def _on_execution_report(self, entity: dict) -> List[OrderEvent]:
        if not self._belongs(entity):
            return []
        order_id = entity.get("orderId")
        if order_id is None:
            return []
        exec_type = entity.get("execType")
        st = self._state(order_id)

        if exec_type == "New":
            return self._emit_new(st)
        if exec_type == "Replaced":
            return self._emit_modify(st)
        if exec_type in ("Canceled", "Cancelled"):
            return self._emit_cancel(st)
        if exec_type in ("Filled", "Trade"):
            return self._emit_fill(st)
        return []

    def _belongs(self, entity: dict) -> bool:
        acct = entity.get("accountId")
        return acct is None or str(acct) == self._account_id

    # ── event construction ───────────────────────────────────────── #

    def _symbol_for(self, st: _OrderState) -> Optional[str]:
        if st.contract_id is None:
            return None
        try:
            return self._resolve_symbol(int(st.contract_id))
        except Exception as e:  # noqa: BLE001
            logger.warning("contractId %s → symbol resolution failed: %s",
                           st.contract_id, e)
            return None

    def _emit_new(self, st: _OrderState) -> List[OrderEvent]:
        # Part of a bracket strategy? Arm it — the entry's New is the
        # signal the bracket is live. The OCO children never get their
        # own New (they stay Suspended until the entry fills), so we
        # arm here and emit once all linked legs' details have arrived.
        if st.strategy_id is not None:
            self._strategy_armed.add(st.strategy_id)
            return self._maybe_emit_bracket(st.strategy_id)
        # Single order.
        if st.emitted_new:
            return []
        spec = st.to_spec()
        symbol = self._symbol_for(st)
        if spec is None or symbol is None:
            return []
        st.emitted_new = True
        st.emitted_fingerprint = st.fingerprint
        return [OrderEvent(
            kind=EventKind.NEW, source_broker="tradovate",
            source_account_id=self._report_account_id,
            source_order_id=st.order_id, source_label=st.order_id,
            symbol=symbol, order=spec)]

    def _retry_armed_for_order(self, order_id) -> List[OrderEvent]:
        """A leg detail (orderVersion / link) just arrived. If its
        strategy is armed and now complete, emit the bracket."""
        if order_id is None:
            return []
        st = self._orders.get(str(order_id))
        if st is None or st.strategy_id is None:
            return []
        if st.strategy_id not in self._strategy_armed:
            return []
        return self._maybe_emit_bracket(st.strategy_id)

    def _maybe_emit_bracket(self, sid: Optional[str]) -> List[OrderEvent]:
        if sid is None or sid in self._bracket_emitted:
            # Bracket already emitted as NEW; later legs/updates of the
            # same strategy are part of the already-replicated bracket.
            return []
        if sid not in self._strategy_armed:
            # Entry hasn't confirmed New yet — don't emit prematurely.
            return []
        legs = [self._orders[oid] for oid in self._strategies.get(sid, [])
                if oid in self._orders]
        # A bracket is entry + at least one exit; wait until we have
        # 2+ legs AND every linked leg has enough detail to build a
        # spec. The children stream in just after the entry's New.
        specs_ready = [l for l in legs if l.to_spec() is not None]
        if len(specs_ready) < 2 or len(specs_ready) != len(legs):
            # Not all KNOWN legs have details yet; hold.
            return []
        # If the orderStrategy told us how many legs to expect, hold
        # until they've ALL linked in — otherwise we'd emit as soon as
        # the entry + take-profit are ready and drop the stop-loss leg
        # that's still streaming in.
        expected = self._strategy_expected_legs.get(sid)
        if expected is not None and len(legs) < expected:
            return []

        # Classify legs: entry = the one whose action matches the
        # strategy's entry (the first-linked leg, label != "0" in the
        # capture, but more robustly: the leg that is NOT a pure exit).
        # Exits are the OCO children: a Limit child is TAKE_PROFIT, a
        # Stop child is STOP_LOSS. The entry is the remaining leg.
        entry_leg = self._pick_entry(legs)
        if entry_leg is None:
            return []
        children = [l for l in legs if l is not entry_leg]

        entry_spec = entry_leg.to_spec(role=BracketRole.ENTRY)
        if entry_spec is None:
            return []
        child_specs = []
        for c in children:
            ctype = _ORDER_TYPE_FROM_TRADOVATE.get(c.order_type or "")
            role = (BracketRole.STOP_LOSS
                    if ctype in (OrderType.STOP, OrderType.STOP_LIMIT)
                    else BracketRole.TAKE_PROFIT)
            cspec = c.to_spec(role=role)
            if cspec is None:
                return []
            child_specs.append(cspec)

        symbol = self._symbol_for(entry_leg)
        if symbol is None:
            return []

        self._bracket_emitted.add(sid)
        for l in legs:
            l.emitted_new = True
            # Baseline each leg's fingerprint to its current state so the
            # next Replaced only flags legs whose VALUE changes after the
            # bracket was placed.
            l.emitted_fingerprint = l.fingerprint
        return [OrderEvent(
            kind=EventKind.NEW, source_broker="tradovate",
            source_account_id=self._report_account_id,
            source_order_id=entry_leg.order_id,
            source_label=entry_leg.order_id, symbol=symbol,
            bracket=BracketSpec(entry=entry_spec, children=child_specs))]

    @staticmethod
    def _pick_entry(legs: List[_OrderState]) -> Optional[_OrderState]:
        """The entry leg is the one whose side differs from the exits,
        OR — more robustly for a long bracket — the leg that is not an
        OCO exit. In a standard bracket the entry has the opposite
        action to its two exits (Buy entry, Sell TP+SL). Pick the
        minority-action leg; ties fall back to the first leg."""
        from collections import Counter
        actions = Counter(l.action for l in legs if l.action)
        if len(actions) >= 2:
            minority = min(actions, key=lambda a: actions[a])
            for l in legs:
                if l.action == minority:
                    return l
        return legs[0] if legs else None

    def _emit_modify(self, st: _OrderState) -> List[OrderEvent]:
        # Bracket case: Tradovate fires the Replaced executionReport
        # against the ENTRY's orderId even when a CHILD leg is what
        # actually changed. So if this order belongs to an emitted
        # bracket, don't blindly attribute the modify to `st` — find
        # the leg(s) whose orderVersion changed since we last reported
        # them and emit a MODIFY against each of those real legs.
        if (st.strategy_id is not None
                and st.strategy_id in self._bracket_emitted):
            return self._emit_bracket_modify(st.strategy_id)
        return self._modify_event_for(st)

    def _emit_bracket_modify(self, sid: str) -> List[OrderEvent]:
        """Emit a MODIFY for each bracket leg whose VALUE changed since
        it was last emitted. Tradovate re-sends the entry's unchanged
        orderVersion on every leg modify, so we compare fingerprints
        rather than trusting that a version arrived — this attributes
        the modify to the leg that genuinely moved and skips no-op
        re-sends. Empty if nothing actually changed."""
        legs = [self._orders[oid] for oid in self._strategies.get(sid, [])
                if oid in self._orders]
        changed = [l for l in legs
                   if l.fingerprint != l.emitted_fingerprint]
        events: List[OrderEvent] = []
        for leg in changed:
            events.extend(self._modify_event_for(leg))
        return events

    def _modify_event_for(self, st: _OrderState) -> List[OrderEvent]:
        """Build a single MODIFY event from a leg's current details and
        record the emitted fingerprint."""
        otype = _ORDER_TYPE_FROM_TRADOVATE.get(st.order_type or "")
        if otype is None:
            return []
        st.emitted_fingerprint = st.fingerprint
        return [OrderEvent(
            kind=EventKind.MODIFY, source_broker="tradovate",
            source_account_id=self._report_account_id,
            source_order_id=st.order_id,
            modify=ModifySpec(
                new_quantity=int(st.qty) if st.qty is not None else None,
                new_limit_price=st.price if otype in (
                    OrderType.LIMIT, OrderType.STOP_LIMIT) else None,
                new_stop_price=st.stop_price if otype in (
                    OrderType.STOP, OrderType.STOP_LIMIT) else None,
                order_type=otype))]

    def _emit_cancel(self, st: _OrderState) -> List[OrderEvent]:
        return [OrderEvent(
            kind=EventKind.CANCEL, source_broker="tradovate",
            source_account_id=self._report_account_id,
            source_order_id=st.order_id)]

    def _emit_fill(self, st: _OrderState) -> List[OrderEvent]:
        return [OrderEvent(
            kind=EventKind.FILL, source_broker="tradovate",
            source_account_id=self._report_account_id,
            source_order_id=st.order_id)]
