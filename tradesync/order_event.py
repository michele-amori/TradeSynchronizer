"""
order_event — broker-neutral order vocabulary.

This module defines the lingua franca that decouples the SOURCE of an
order (where the user traded) from the FOLLOWER (where we replicate
it). Today the only source is IBKR-via-mitmproxy and the only follower
is Tradovate-via-REST, so these types are not yet on the hot path.
They exist so that, as the bidirectional work lands, every source
endpoint (IBKR proxy observer, Tradovate WebSocket observer, …) emits
the SAME `OrderEvent`, and the replicator + every follower endpoint
consumes that single shape without knowing which broker produced it.

Design notes
------------
* **Normalised vocabulary.** `Side`, `OrderType` and `TimeInForce`
  use neutral tokens, NOT a specific broker's wire strings. IBKR says
  "BUY"/"MKT"/"DAY"; Tradovate says "Buy"/"Market"/"Day". Neither
  vocabulary leaks here. Each endpoint translates its own broker's
  wire format to/from these tokens at its boundary, so the replicator
  in the middle is broker-agnostic.

* **`source_*` provenance.** Every event carries the id by which the
  SOURCE broker knows the order, plus an optional human/client label
  (IBKR's cOID, Tradovate's order `text` or numeric id). The follower
  side ids live in the OrderMap, keyed by these source ids.

* **Brackets are one event.** A bracket arrives as a single
  `OrderEvent(kind=NEW, bracket=BracketSpec(...))` rather than three
  separate NEW events, mirroring how both IBKR (`placeoso`-style
  multi-leg POST) and Tradovate (`placeoso` with linked children)
  model it. The replicator already has a dedicated bracket path.

* **No `raw` here.** Unlike the IBKR parser dataclasses, OrderEvent
  deliberately omits a `raw: dict` escape hatch. The whole point is a
  clean broker-neutral contract; if a follower needs broker-specific
  detail it should be normalised into an explicit field rather than
  smuggled through raw. (The source-side parser dataclasses keep their
  raw dumps for diagnostics — that's a separate concern.)

This is a pure data module: no I/O, no broker imports, no logging.
Keeping it dependency-free means both the proxy layer and the broker
layer can import it without creating an import cycle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


# ── Normalised enumerations ──────────────────────────────────────────── #

class Side(str, Enum):
    """Order direction, broker-neutral."""
    BUY = "BUY"
    SELL = "SELL"

    @property
    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY


class OrderType(str, Enum):
    """Order type, broker-neutral.

    The four types every broker we target supports natively. Exotic
    Tradovate types (MIT/LIT/TrailingStop) are intentionally NOT here
    yet — when we add them, each follower endpoint decides how (or
    whether) to express them, and unsupported combinations surface as
    a divergence rather than a silent mistranslation."""
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"


class TimeInForce(str, Enum):
    """Time-in-force, broker-neutral."""
    DAY = "DAY"
    GTC = "GTC"
    IOC = "IOC"
    FOK = "FOK"


class BracketRole(str, Enum):
    """Role of a leg within a bracket.

    Mirrors the synthetic-cOID role tokens the replicator already
    emits (`{entry}#LMT`, `{entry}#STP`). ENTRY is the parent; TAKE_
    PROFIT and STOP_LOSS are the OCO-linked exit children."""
    ENTRY = "ENTRY"
    TAKE_PROFIT = "TAKE_PROFIT"   # a LIMIT exit
    STOP_LOSS = "STOP_LOSS"       # a STOP / STOP_LIMIT exit


class EventKind(str, Enum):
    """What happened to the order on the source side."""
    NEW = "NEW"
    CANCEL = "CANCEL"
    MODIFY = "MODIFY"
    FILL = "FILL"     # informational; not replicated, used for telemetry


# ── Order specifications ─────────────────────────────────────────────── #

@dataclass
class OrderSpec:
    """A single order, described in broker-neutral terms.

    Used as the entry of a NEW single-order event and as each leg of a
    bracket. Price semantics follow the order type:
      * LIMIT      → limit_price set, stop_price None
      * STOP       → stop_price set, limit_price None
      * STOP_LIMIT → both set
      * MARKET     → neither set
    """
    side:        Side
    quantity:    int
    order_type:  OrderType
    limit_price: Optional[float] = None
    stop_price:  Optional[float] = None
    tif:         TimeInForce = TimeInForce.DAY

    # Provenance on the source broker. For a bracket child arriving
    # without its own id (the TradingView case), source_order_id /
    # source_label may be None and the replicator mints a synthetic
    # key from the parent — exactly as it does today.
    source_order_id: Optional[str] = None
    source_label:    Optional[str] = None
    role:            BracketRole = BracketRole.ENTRY


@dataclass
class BracketSpec:
    """An entry plus 1..2 OCO-linked exit legs.

    `entry.role` is ENTRY; each child's role is TAKE_PROFIT or
    STOP_LOSS. The replicator's existing placeoso path consumes this.
    """
    entry:    OrderSpec
    children: List[OrderSpec] = field(default_factory=list)


@dataclass
class ModifySpec:
    """The changed fields of a modify. Only what actually changed is
    populated; everything else stays None. `order_type` is carried
    explicitly because some follower brokers (Tradovate) hard-require
    it on their modify endpoint even when it didn't change."""
    new_quantity:    Optional[int] = None
    new_limit_price: Optional[float] = None
    new_stop_price:  Optional[float] = None
    new_tif:         Optional[TimeInForce] = None
    order_type:      Optional[OrderType] = None


# ── The event ────────────────────────────────────────────────────────── #

@dataclass
class OrderEvent:
    """A single thing that happened to an order on the source broker.

    The shape depends on `kind`:
      * NEW    → exactly one of `order` / `bracket` is set
      * MODIFY → `modify` is set, `source_order_id` identifies target
      * CANCEL → `source_order_id` identifies target
      * FILL   → `source_order_id` identifies target (informational)

    `source_account_id` lets the replicator apply its watch-list /
    account-filter policy regardless of which broker emitted the
    event. `symbol` and `conid` describe the instrument; a source
    endpoint populates whichever it natively knows, and the follower
    endpoint resolves the rest (e.g. Tradovate symbol → IBKR conId).
    """
    kind:              EventKind
    source_broker:     str                      # "ibkr" | "tradovate"
    source_account_id: str
    source_order_id:   Optional[str] = None
    source_label:      Optional[str] = None

    # Instrument identity — at least one of these is set; the follower
    # endpoint resolves whatever it additionally needs.
    symbol:            Optional[str] = None     # e.g. "MNQM6"
    conid:             Optional[int] = None      # IBKR numeric contract id

    # Payloads, by kind:
    order:             Optional[OrderSpec] = None
    bracket:           Optional[BracketSpec] = None
    modify:            Optional[ModifySpec] = None

    def __post_init__(self) -> None:
        # Light invariant checks — these catch programming errors at
        # the boundary where an endpoint constructs an event, long
        # before the replicator tries to act on a malformed one.
        if self.kind is EventKind.NEW:
            if (self.order is None) == (self.bracket is None):
                raise ValueError(
                    "NEW event must set exactly one of `order` or "
                    "`bracket`"
                )
        elif self.kind is EventKind.MODIFY:
            if self.modify is None:
                raise ValueError("MODIFY event must set `modify`")
            if not self.source_order_id:
                raise ValueError(
                    "MODIFY event must set `source_order_id`"
                )
        elif self.kind in (EventKind.CANCEL, EventKind.FILL):
            if not self.source_order_id:
                raise ValueError(
                    f"{self.kind.value} event must set `source_order_id`"
                )
