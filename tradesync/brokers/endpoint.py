"""
Broker endpoint protocols — the abstraction that lets the replicator
treat "where orders come from" and "where orders go" uniformly,
regardless of which broker is on each side.

Two roles, two protocols
------------------------
The bidirectional plan originally sketched a single BrokerEndpoint
with both `as_source` and `as_follower` behaviour. Looking at the
concrete code, source and follower are cleanly different
responsibilities with different lifecycles, so we model them as two
separate Protocols:

  * SourceEndpoint   — OBSERVES a broker and emits OrderEvent objects
                       through a callback. Lifecycle: start/stop.
  * FollowerEndpoint — EXECUTES orders on a broker. Lifecycle:
                       connect / place / cancel / modify / disconnect.

A concrete class may implement both (e.g. a Tradovate adapter that can
act as either side depending on the configured direction), but keeping
the contracts separate means no implementation is forced to stub out
methods that don't apply to its role. For example, the IBKR source
(mitmproxy observer) has no business exposing place_order.

Neutral vocabulary
------------------
Follower methods speak the broker-neutral OrderSpec / BracketSpec
vocabulary from tradesync.order_event, NOT a specific broker's wire
fields. Each concrete FollowerEndpoint translates neutral specs into
its own broker's API at its boundary. Results come back as the neutral
PlacedRef / PlacedBracketRef below rather than a broker-specific
dataclass, so the replicator never imports a broker's result type.

These are typing.Protocol definitions: structural, runtime-checkable,
and dependency-light. Implementing them requires no inheritance — a
class just needs matching methods. This module imports only from
order_event (pure data) to avoid any import cycle with the broker
clients that will implement it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Optional, Protocol, runtime_checkable

from ..order_event import BracketSpec, ModifySpec, OrderEvent, OrderSpec


# ── Neutral follower-side result types ───────────────────────────────── #

@dataclass
class PlacedRef:
    """Neutral result of placing a single order on a follower broker.

    `follower_order_id` is the id by which the follower broker now
    knows the order (Tradovate numeric orderId, IBKR order id, …),
    stringified so the order map can key on it uniformly across
    brokers."""
    follower_order_id: str
    raw: dict = field(default_factory=dict)


@dataclass
class PlacedBracketRef:
    """Neutral result of placing a bracket on a follower broker.

    `child_order_ids` are in the same order as the BracketSpec's
    `children`. `oco_id` is the broker's native OCO-group id when it
    provides one (Tradovate's placeoso returns null, so this is often
    None and the replicator handles OCO cascade itself)."""
    entry_order_id:  str
    child_order_ids: List[str] = field(default_factory=list)
    oco_id:          Optional[str] = None
    raw:             dict = field(default_factory=dict)


# ── Source role ──────────────────────────────────────────────────────── #

@runtime_checkable
class SourceEndpoint(Protocol):
    """Observes a broker and emits OrderEvent objects.

    The implementation calls the `on_event` callback (passed to
    start_observing) once per observed order event. It does NOT own
    the policy decision of what to do with the event — that's the
    replicator's job — it only normalises the broker's native order
    feed into the neutral OrderEvent vocabulary.
    """

    @property
    def identity(self) -> str:
        """Stable id like 'ibkr_live_U0000001' for logs/config."""
        ...

    def start_observing(self, on_event: Callable[[OrderEvent], None]) -> None:
        """Begin observing. Each observed order event is delivered to
        `on_event`. May spawn threads / open sockets as needed; must
        be safe to call once per engine run."""
        ...

    def stop_observing(self) -> None:
        """Stop observing and release any resources (threads, sockets).
        Idempotent."""
        ...


# ── Follower role ────────────────────────────────────────────────────── #

@runtime_checkable
class FollowerEndpoint(Protocol):
    """Executes replicated orders on a broker.

    All methods speak neutral OrderSpec / BracketSpec / ModifySpec.
    Implementations translate to/from their broker's wire format and
    raise their own broker error types on failure; the replicator
    catches those and turns them into ReplicationResult / divergence
    as it already does today.
    """

    @property
    def identity(self) -> str:
        """Stable id like 'tradovate_live_19000001' for logs/config."""
        ...

    @property
    def native_oco(self) -> bool:
        """Whether this broker enforces OCO (one-cancels-other) on
        bracket exit legs NATIVELY.

        IBKR groups bracket children via ocaGroup, so cancelling or
        filling one leg auto-cancels the sibling at the broker — True.
        Tradovate's /order/placeoso does NOT group the children (ocoId
        comes back null), so the sibling must be cancelled explicitly —
        False. The replicator reads this to decide whether to simulate
        the OCO cascade itself; when True it stays out of the way so it
        doesn't issue a redundant (or erroneous) second cancel."""
        ...

    def connect(self) -> None:
        """Establish the session needed to place orders. For Tradovate
        this is the REST auth; for IBKR-as-follower it's the IB Gateway
        socket handshake. Idempotent where possible."""
        ...

    def disconnect(self) -> None:
        """Tear down the session. Idempotent."""
        ...

    def place_order(self, spec: OrderSpec, *, symbol: str) -> PlacedRef:
        """Place a single order described by `spec` on instrument
        `symbol` (the follower-side symbol, already resolved). Returns
        the neutral PlacedRef. Raises a broker-specific error on
        failure."""
        ...

    def place_bracket(
        self, spec: BracketSpec, *, symbol: str
    ) -> PlacedBracketRef:
        """Place an entry-plus-exits bracket. Returns PlacedBracketRef
        with child ids in `spec.children` order."""
        ...

    def cancel_order(self, follower_order_id: str) -> None:
        """Cancel a previously-placed order by its follower-side id.
        Raises a broker 'not found' error if already gone."""
        ...

    def modify_order(self, follower_order_id: str, changes: ModifySpec) -> None:
        """Modify a previously-placed order. Only the non-None fields
        of `changes` are applied."""
        ...

    def order_status(self, follower_order_id: str) -> str:
        """Return the broker's status string for the order (used by
        startup reconciliation). Raises a broker 'not found' error if
        the order no longer exists."""
        ...
