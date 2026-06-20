"""
position_reconciler — periodic safety check that the SOURCE and FOLLOWER
brokers actually hold the same positions.

Why this exists
---------------
The replication pipeline forwards order events (NEW / MODIFY / CANCEL)
from the source broker to the follower. If any one of those events is
lost — a dropped WebSocket frame, a rejected order, a follower that was
briefly disconnected — the two accounts silently diverge: the source is
flat while the follower is still short, or a stop sits at the wrong
price on one side. Nothing in the event path notices, because the path
only sees events as they arrive, not the resulting state.

This reconciler closes that gap. On a timer it asks BOTH brokers for
their current net positions, lines them up by symbol, and compares. If
they disagree it raises a loud, specific WARNING (and invokes an
optional alert callback) so the operator can intervene — square the
position, restart the engine, fix a stop — before the divergence costs
money.

It is read-only: it NEVER places, cancels, or modifies an order. It
observes and reports. Auto-correcting a divergence by trading is
deliberately out of scope — that's a decision for a human, on a real
account.

Comparability
-------------
The two brokers identify the same instrument with different numeric ids
(IBKR conId vs Tradovate contractId), so positions are normalised to
the SYMBOL (e.g. "MNQM6") before comparison, via the resolver callbacks
the caller supplies. A position that can't be resolved to a symbol on
one side is reported as "unknown", never silently treated as flat.

Ratio awareness
---------------
When the pair has a follower size ratio != 1.0, the follower is meant
to hold source × ratio, not the same size. The reconciler compares in
scale so a non-1.0 ratio doesn't read as a permanent (false) mismatch —
within a small per-symbol tolerance, since scale_quantity rounds per
order and the netted follower position isn't exactly source × ratio. A
sign flip is always a real mismatch. With ratio == 1.0 the comparison
stays exact, identical to before.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional


logger = logging.getLogger("tradesync.reconciler")


# How often to reconcile, by default. Frequent enough to catch a
# divergence within a minute or so, infrequent enough that the two
# extra REST/socket round-trips are negligible.
_DEFAULT_INTERVAL_SECS = 30.0


@dataclass
class PositionMismatch:
    """One symbol where source and follower disagree."""
    symbol: str
    source_qty: float
    follower_qty: float

    def __str__(self) -> str:
        return (f"{self.symbol}: source={self.source_qty:+g} "
                f"follower={self.follower_qty:+g}")


@dataclass
class ReconcileReport:
    """Outcome of one reconciliation pass."""
    aligned: bool
    mismatches: List[PositionMismatch] = field(default_factory=list)
    # Raw per-symbol views, for logging / a future GUI panel.
    source_by_symbol: Dict[str, float] = field(default_factory=dict)
    follower_by_symbol: Dict[str, float] = field(default_factory=dict)
    error: Optional[str] = None

    def summary(self) -> str:
        if self.error:
            return f"reconcile inconclusive: {self.error}"
        if self.aligned:
            return "source and follower positions aligned"
        return ("POSITION MISMATCH — "
                + "; ".join(str(m) for m in self.mismatches))


class PositionReconciler:
    """Periodically compares source vs follower net positions.

    Parameters
    ----------
    source_positions / follower_positions:
        Callables returning {id: net_qty} for the source and follower
        brokers respectively. The ids are broker-native (Tradovate
        contractId, IBKR conId).
    source_symbol_of / follower_symbol_of:
        Callables mapping a broker-native id → symbol string. Used to
        normalise both sides to a common key before comparing. May
        raise / return None for an unknown id; such positions are
        surfaced as a mismatch against 'unknown', never dropped.
    ratio:
        The follower's per-pair size ratio (see scale_quantity). When
        1.0 (the default and the common case) the comparison is EXACT,
        byte-for-byte identical to before this parameter existed. When
        != 1.0 the follower is expected to hold roughly source × ratio,
        so a symbol is aligned when the follower is within `tolerance`
        contracts of source × ratio — see _is_aligned for why an exact
        equality is wrong here (per-order rounding in scale_quantity
        means the netted follower position is not exactly source×ratio).
    tolerance:
        Allowed absolute deviation (in contracts) of the follower from
        source × ratio before it's flagged, when ratio != 1.0. Absorbs
        the per-order round-half-up/floor-to-1 of scale_quantity. Unused
        when ratio == 1.0 (exact comparison).
    on_mismatch:
        Optional callback invoked with the ReconcileReport whenever a
        pass finds a divergence (for a GUI banner, a notification, …).
    health_source:
        Optional callable returning an object with `.connected` and
        `.seconds_since_last_frame` attributes (the observer's
        ObserverHealth). When supplied, every pass emits a compact
        `[health]` log line combining the feed state with the
        reconciliation outcome — the engine runs as a subprocess, so
        this single periodic line is how the GUI's merged log surfaces
        "is the replica connected AND in sync" at a glance, without a
        separate inter-process status channel.
    interval_secs:
        Seconds between automatic passes when run via start().
    """

    def __init__(
        self,
        *,
        source_positions: Callable[[], Dict[int, float]],
        follower_positions: Callable[[], Dict[int, float]],
        source_symbol_of: Callable[[int], Optional[str]],
        follower_symbol_of: Callable[[int], Optional[str]],
        ratio: float = 1.0,
        tolerance: float = 1.0,
        on_mismatch: Optional[Callable[[ReconcileReport], None]] = None,
        health_source: Optional[Callable[[], object]] = None,
        interval_secs: float = _DEFAULT_INTERVAL_SECS,
    ):
        self._source_positions = source_positions
        self._follower_positions = follower_positions
        self._source_symbol_of = source_symbol_of
        self._follower_symbol_of = follower_symbol_of
        self._ratio = ratio
        self._tolerance = tolerance
        self._on_mismatch = on_mismatch
        self._health_source = health_source
        self._interval = interval_secs

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        # Last report, readable by a GUI/status panel.
        self._last: Optional[ReconcileReport] = None
        self._last_lock = threading.Lock()

    # ── one pass ─────────────────────────────────────────────────── #

    def check_once(self) -> ReconcileReport:
        """Run a single reconciliation pass. Never raises — transport
        problems are captured in the report's `error` field so the
        timer loop keeps going."""
        try:
            src_raw = self._source_positions()
            fol_raw = self._follower_positions()
        except Exception as e:  # noqa: BLE001 - must not kill the loop
            report = ReconcileReport(aligned=True, error=str(e))
            self._store(report)
            logger.warning("Reconcile could not read positions: %s", e)
            return report

        src = self._normalise(src_raw, self._source_symbol_of)
        fol = self._normalise(fol_raw, self._follower_symbol_of)

        mismatches: List[PositionMismatch] = []
        for sym in sorted(set(src) | set(fol)):
            s = src.get(sym, 0.0)
            f = fol.get(sym, 0.0)
            if not self._is_aligned(s, f):
                mismatches.append(PositionMismatch(sym, s, f))

        report = ReconcileReport(
            aligned=not mismatches,
            mismatches=mismatches,
            source_by_symbol=src,
            follower_by_symbol=fol,
        )
        self._store(report)

        if mismatches:
            logger.warning(
                "⚠ POSITION MISMATCH between source and follower — %s. "
                "The accounts have diverged; review and square manually "
                "if needed.",
                "; ".join(str(m) for m in mismatches))
            if self._on_mismatch is not None:
                try:
                    self._on_mismatch(report)
                except Exception as e:  # noqa: BLE001
                    logger.warning("reconcile on_mismatch callback raised: %s", e)
        else:
            logger.info("Reconcile OK — source and follower positions match "
                        "(%d symbol(s))", len(src))

        self._emit_health_line(report)
        return report

    def _is_aligned(self, source_qty: float, follower_qty: float) -> bool:
        """Is the follower position consistent with the source, given the
        ratio?

        ratio == 1.0 → EXACT equality, identical to the original
        behaviour (no tolerance, no float fuzz introduced for the common
        case).

        ratio != 1.0 → the follower should hold about source × ratio.
        We do NOT require exact equality: scale_quantity rounds half-up
        and floors to 1 PER ORDER, so a netted follower position is not
        exactly source × ratio (e.g. three 1-lot orders at ratio 0.5
        each scale to 1 → follower 3, not 1.5). So the follower is
        aligned when it is within `tolerance` contracts of the expected
        source × ratio, AND on the same side (sign) — a sign flip is a
        real divergence regardless of magnitude.
        """
        if self._ratio == 1.0:
            return source_qty == follower_qty
        # Sign must match (flat expected ⇒ follower must be flat too).
        expected = source_qty * self._ratio
        if (expected > 0) != (follower_qty > 0) or \
                (expected < 0) != (follower_qty < 0):
            return False
        return abs(follower_qty - expected) <= self._tolerance

    def _emit_health_line(self, report: ReconcileReport) -> None:
        """Emit a single compact [health] line combining feed state and
        reconciliation outcome. This is the at-a-glance status the GUI's
        merged engine log shows, since the engine runs out-of-process."""
        feed = "feed=unknown"
        if self._health_source is not None:
            try:
                h = self._health_source()
                age = getattr(h, "seconds_since_last_frame", None)
                if not getattr(h, "connected", False):
                    feed = "feed=DISCONNECTED"
                elif age is None:
                    feed = "feed=connected (no frame yet)"
                else:
                    feed = f"feed=connected last_frame={age:.0f}s_ago"
            except Exception as e:  # noqa: BLE001 - health is best-effort
                feed = f"feed=unknown ({e})"

        if report.error:
            positions = f"positions=inconclusive ({report.error})"
        elif report.aligned:
            positions = (f"positions=aligned "
                         f"({len(report.source_by_symbol)} symbol(s))")
        else:
            positions = ("positions=MISMATCH "
                         + "; ".join(str(m) for m in report.mismatches))

        line = f"[health] {feed} | {positions}"
        # Warn-level when something needs attention so it stands out in
        # the GUI log colouring; info otherwise.
        if (not report.aligned and not report.error) or \
                "DISCONNECTED" in feed:
            logger.warning(line)
        else:
            logger.info(line)

    @staticmethod
    def _normalise(raw: Dict[int, float],
                   symbol_of: Callable[[int], Optional[str]]) -> Dict[str, float]:
        """Map a {native_id: qty} dict to {symbol: qty}, summing any
        ids that resolve to the same symbol. Unresolvable ids are keyed
        as 'unknown:<id>' so they show up as a mismatch rather than
        vanishing."""
        out: Dict[str, float] = {}
        for native_id, qty in raw.items():
            if qty == 0:
                continue
            try:
                sym = symbol_of(native_id)
            except Exception:  # noqa: BLE001
                sym = None
            key = sym if sym else f"unknown:{native_id}"
            out[key] = out.get(key, 0.0) + float(qty)
        return out

    def _store(self, report: ReconcileReport) -> None:
        with self._last_lock:
            self._last = report

    @property
    def last_report(self) -> Optional[ReconcileReport]:
        with self._last_lock:
            return self._last

    # ── timer loop ───────────────────────────────────────────────── #

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            logger.warning("PositionReconciler already running")
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="position-reconciler", daemon=True)
        self._thread.start()
        logger.info("PositionReconciler started — checking every %.0fs",
                    self._interval)

    def _run(self) -> None:
        # A short initial delay lets the pipeline finish connecting and
        # ingest its snapshot before the first comparison, avoiding a
        # spurious mismatch during startup.
        if self._stop.wait(timeout=min(self._interval, 10.0)):
            return
        while not self._stop.is_set():
            self.check_once()
            self._stop.wait(timeout=self._interval)

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=5.0)
        self._thread = None
        logger.info("PositionReconciler stopped")
