"""
OrderMap — persistent mapping between an IBKR order's identifiers
and the Tradovate replica's order id.

Why it exists
-------------
IBKR's TradingView-flavour API uses two distinct identifiers for
each order:

  * cOID (client-side, in the POST body)
  * order_id (server-assigned, returned in the POST response and
    used in the URL of subsequent cancel / modify calls)

Tradovate uses its own numeric `orderId`. To replicate an IBKR
cancellation or modification we must translate IBKR's order_id
into Tradovate's orderId, going through the cOID we already saw
when we placed the replica.

Lifecycle of one entry:

  1. New-order POST observed → cOID known         (`add_pending`)
  2. Tradovate place_order returns                (`set_tradovate_id`)
  3. IBKR POST response observed → IBKR id known  (`set_ibkr_id`)
  4. Cancel/modify arrives keyed by IBKR id       (`tradovate_for_ibkr_id`)
  5. Cancel succeeds → entry removed              (`remove_by_ibkr_id`)

Steps 2 and 3 can complete in either order (the worker thread that
calls Tradovate and mitmproxy's response hook race), so each is a
separate setter and both must tolerate already-set or
not-yet-set siblings.

Persistence
-----------
The map is serialised to a JSON file (one per environment) at:

    <project_root>/.tradesync-state/orders-<env>.json

so it survives a TradeSynchronizer restart while orders are still
open. Writes are atomic (tempfile + os.replace) and serialised
behind a per-instance threading.Lock.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


logger = logging.getLogger("tradesync.order_map")


@dataclass
class OrderRecord:
    """One mapped order. Both IDs may be None transiently while the
    two side-quests (Tradovate placeorder, IBKR POST response) are
    in flight."""
    cOID:          str
    ibkr_order_id: Optional[str] = None
    tradovate_id:  Optional[int] = None
    created_at:    str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_jsonable(self) -> dict:
        return asdict(self)


class OrderMap:
    """Thread-safe, JSON-persistent two-way order id map."""

    def __init__(self, store_path: Path):
        self._path = store_path
        self._lock = threading.Lock()
        self._by_coid: dict[str, OrderRecord] = {}
        # Reverse index: IBKR order_id → cOID. Rebuilt from _by_coid
        # on load and on every mutation that touches ibkr_order_id.
        self._coid_by_ibkr: dict[str, str] = {}
        # Batch-write depth counter. When >0, mutations skip the
        # _save_locked() disk write; a single flush happens when the
        # outermost batch() context manager exits. Lets the
        # replicator coalesce up to N disk writes per bracket into
        # one — critical because each write is ~1 ms on SSD and a
        # bracket can fire 3 set_tradovate_id() calls in a tight
        # loop on the order-replication hot path.
        self._batch_depth: int = 0
        self._batch_dirty: bool = False
        self._load()

    # ── batched writes ────────────────────────────────────────────── #

    def batch(self) -> "_OrderMapBatch":
        """
        Context manager that defers JSON writes for the duration of
        the block. Use around any sequence of 2+ mutations that
        logically commit together:

            with order_map.batch():
                order_map.set_tradovate_id(entry_coid, entry_id)
                for child_coid, child_id in zip(...):
                    order_map.set_tradovate_id(child_coid, child_id)
            # ← one disk write here, instead of N

        Re-entrant: nested batch() blocks coalesce into the outermost
        one. Exceptions inside the block still trigger the flush —
        we prefer a written-and-slightly-stale map over a lost
        in-memory mutation.
        """
        return _OrderMapBatch(self)

    # ── lifecycle ─────────────────────────────────────────────────── #

    def _mark_dirty_or_save(self) -> None:
        """Called from within _lock by every mutator. Either defers
        the write (batch in progress) or flushes immediately."""
        if self._batch_depth > 0:
            self._batch_dirty = True
        else:
            self._save_locked()

    def add_pending(self, coid: str) -> None:
        """Insert a new entry with both downstream ids still pending."""
        if not coid:
            return
        with self._lock:
            if coid in self._by_coid:
                return
            self._by_coid[coid] = OrderRecord(cOID=coid)
            self._mark_dirty_or_save()

    def set_tradovate_id(self, coid: str, tradovate_id: int) -> None:
        if not coid:
            return
        with self._lock:
            rec = self._by_coid.get(coid)
            if rec is None:
                rec = OrderRecord(cOID=coid)
                self._by_coid[coid] = rec
            rec.tradovate_id = int(tradovate_id)
            self._mark_dirty_or_save()

    def set_ibkr_id(self, coid: str, ibkr_order_id: str) -> None:
        if not coid or not ibkr_order_id:
            return
        with self._lock:
            rec = self._by_coid.get(coid)
            if rec is None:
                rec = OrderRecord(cOID=coid)
                self._by_coid[coid] = rec
            # Drop any previous reverse-index entry for this cOID
            if rec.ibkr_order_id and rec.ibkr_order_id != ibkr_order_id:
                self._coid_by_ibkr.pop(rec.ibkr_order_id, None)
            rec.ibkr_order_id = ibkr_order_id
            self._coid_by_ibkr[ibkr_order_id] = coid
            self._mark_dirty_or_save()

    # ── lookups ───────────────────────────────────────────────────── #

    def tradovate_for_ibkr_id(self, ibkr_order_id: str) -> Optional[int]:
        """Return the Tradovate orderId mapped to this IBKR id, or
        None if we never saw the new-order POST for it or the
        Tradovate placeorder hasn't completed yet."""
        with self._lock:
            coid = self._coid_by_ibkr.get(ibkr_order_id)
            if not coid:
                return None
            rec = self._by_coid.get(coid)
            return rec.tradovate_id if rec else None

    def get_by_coid(self, coid: str) -> Optional[OrderRecord]:
        with self._lock:
            return self._by_coid.get(coid)

    def __len__(self) -> int:
        with self._lock:
            return len(self._by_coid)

    # ── removal ───────────────────────────────────────────────────── #

    def remove_by_ibkr_id(self, ibkr_order_id: str) -> None:
        with self._lock:
            coid = self._coid_by_ibkr.pop(ibkr_order_id, None)
            if coid:
                self._by_coid.pop(coid, None)
                self._mark_dirty_or_save()

    def remove_by_coid(self, coid: str) -> None:
        with self._lock:
            rec = self._by_coid.pop(coid, None)
            if rec and rec.ibkr_order_id:
                self._coid_by_ibkr.pop(rec.ibkr_order_id, None)
            if rec is not None:
                self._mark_dirty_or_save()

    # ── persistence ───────────────────────────────────────────────── #

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(
                "Order map at %s is unreadable (%s) — starting fresh",
                self._path, e,
            )
            return
        records = data.get("orders") if isinstance(data, dict) else None
        if not isinstance(records, list):
            return
        for r in records:
            if not isinstance(r, dict):
                continue
            coid = r.get("cOID")
            if not coid:
                continue
            rec = OrderRecord(
                cOID=str(coid),
                ibkr_order_id=r.get("ibkr_order_id"),
                tradovate_id=r.get("tradovate_id"),
                created_at=r.get("created_at")
                    or datetime.now(timezone.utc).isoformat(),
            )
            self._by_coid[rec.cOID] = rec
            if rec.ibkr_order_id:
                self._coid_by_ibkr[rec.ibkr_order_id] = rec.cOID
        logger.info("Loaded %d order map entr%s from %s",
                    len(self._by_coid),
                    "y" if len(self._by_coid) == 1 else "ies",
                    self._path)

    def _save_locked(self) -> None:
        """Caller must hold self._lock."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "schema": 1,
                "orders": [r.to_jsonable() for r in self._by_coid.values()],
            }
            # Atomic write: tempfile in the same dir, then rename.
            fd, tmp_name = tempfile.mkstemp(
                prefix=".orders-", suffix=".json.tmp",
                dir=str(self._path.parent),
            )
            try:
                with os.fdopen(fd, "w") as fh:
                    json.dump(payload, fh, indent=2)
                os.replace(tmp_name, self._path)
            except Exception:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
        except OSError as e:
            logger.warning("Could not persist order map to %s: %s",
                           self._path, e)


class _OrderMapBatch:
    """Context manager returned by `OrderMap.batch()`. Re-entrant via
    a depth counter on the parent OrderMap. On exit, when the depth
    drops back to zero, performs a single _save_locked() if any
    mutation marked the map dirty during the block."""

    __slots__ = ("_om",)

    def __init__(self, om: "OrderMap"):
        self._om = om

    def __enter__(self) -> "OrderMap":
        with self._om._lock:
            self._om._batch_depth += 1
        return self._om

    def __exit__(self, exc_type, exc, tb) -> None:
        with self._om._lock:
            self._om._batch_depth -= 1
            if self._om._batch_depth == 0 and self._om._batch_dirty:
                self._om._batch_dirty = False
                self._om._save_locked()
        # Don't swallow exceptions — they propagate naturally.
        return False


def default_store_path(project_root: Path, env: str) -> Path:
    """Per-env default location for the persistent JSON file."""
    return project_root / ".tradesync-state" / f"orders-{env}.json"
