"""
IbkrEventSourceObserver — the broker-NEUTRAL source-side façade between
the mitmproxy addon and the EventReplicator. This is the source side of
the live IBKR→Tradovate path: it TRANSLATES each observed IBKR order into
the broker-neutral OrderEvent vocabulary and replicates it via the
EventReplicator — the same engine every other direction uses.

Addon-facing surface
---------------------
The addon depends only on this small, stable surface:

    emit_new(parsed) / emit_cancel(cancel) / emit_modify(modify)
        → return an object with .success / .skipped / .reason
    coid_for_ibkr_id(ibkr_id) / register_ibkr_id(coid, ibkr_id)
        → the two-phase id binding done on the mitmproxy response hook

The mitmproxy hooks never change; the bootstrap just injects this
observer into the addon.

Id-binding semantics
--------------------
In the IBKR→Tradovate direction IBKR is the SOURCE, so an IBKR order id
is a *source* id. The addon learns it in two phases (the new-order POST
response for singles + the entry, then the GET /orders poll for bracket
children) and calls register_ibkr_id(coid, ibkr_id). In neutral terms
that's bind_source_id(label=coid, source_order_id=ibkr_id) — on the
OrderMap that's the same operation as set_ibkr_id (a documented alias).
coid_for_ibkr_id reads the same reverse index. A later MODIFY/CANCEL
arrives carrying the IBKR id as source_order_id and resolves through
follower_id_for_source_id to the Tradovate follower order.

Result shape
------------
EventReplicator returns an EventResult (success/skipped/reason); the
addon's _spawn runner only reads those three attributes.
"""

from __future__ import annotations

import logging
from typing import Optional

from ..event_replicator import EventReplicator
from ..brokers.ibkr_endpoint import (
    order_event_from_cancel,
    order_event_from_modify,
    order_event_from_new,
)
from .ibkr_parser import (
    IbkrOrderCancel,
    IbkrOrderModify,
)


logger = logging.getLogger("tradesync.ibkr_event_source")


class IbkrEventSourceObserver:
    """Routes observed IBKR order events to the EventReplicator,
        translating IBKR parser dataclasses → neutral OrderEvents first.
    """

    def __init__(self, replicator: EventReplicator, order_map):
        self._replicator = replicator
        # The addon's response-hook binding talks directly to the map's
        # cOID↔IBKR-id index (set_ibkr_id / coid_for_ibkr_id).
        self._order_map = order_map

    # ── Order-event emission ─────────────────────────────────────── #
    #
    # Translate the IBKR dataclass to a neutral OrderEvent, then apply
    # it. EventResult carries .success/.skipped/.reason, which is all the
    # addon's runner reads.

    def emit_new(self, parsed):
        """A new single order or bracket was observed on IBKR."""
        return self._replicator.apply(order_event_from_new(parsed))

    def emit_cancel(self, cancel: IbkrOrderCancel):
        """An order cancellation was observed on IBKR."""
        return self._replicator.apply(order_event_from_cancel(cancel))

    def emit_modify(self, modify: IbkrOrderModify):
        """An order modification was observed on IBKR."""
        return self._replicator.apply(order_event_from_modify(modify))

    # ── Source-side id binding ───────────────────────────────────── #
    #
    # Same two methods the addon's response hook uses. They operate on
    # the OrderMap's cOID↔IBKR-id index directly.

    def coid_for_ibkr_id(self, ibkr_order_id: str) -> Optional[str]:
        """Resolve an IBKR order id to the cOID (source label) it was
        bound to, or None. Used to translate a bracket child's parentId
        (the entry's IBKR id) back to the entry's cOID."""
        return self._order_map.coid_for_ibkr_id(ibkr_order_id)

    def register_ibkr_id(self, coid: str, ibkr_order_id: str) -> None:
        """Bind an IBKR-assigned order id to a (possibly synthetic)
        cOID. In neutral terms this is bind_source_id(coid, ibkr_id);
        on the OrderMap that's the same operation as set_ibkr_id, which
        also maintains the reverse index coid_for_ibkr_id reads."""
        self._order_map.bind_source_id(coid, ibkr_order_id)
