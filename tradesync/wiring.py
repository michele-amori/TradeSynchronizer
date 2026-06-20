"""
wiring — assemble replication pipelines from a ReplicationConfig.

This is the integration seam that turns the declarative
config/replication.json (which source→follower pairs to run) into live,
runnable pipelines. It's where the Week 2-4 components finally get
connected:

    TradovateWSObserver → EventReplicator → FollowerEndpoint

for each enabled pair whose SOURCE is Tradovate.

Two pipeline families, two driving models
-----------------------------------------
The two replication directions are driven completely differently, so
wiring keeps them separate rather than pretending they're uniform:

  * IBKR source  → driven by mitmproxy. The TradeSyncAddon, over an
    IbkrEventSourceObserver → EventReplicator, handles this (the live
    IBKR→Tradovate path). wiring does NOT rebuild that; main.py still
    constructs it directly. A pair with source=ibkr is therefore noted
    here but its actual plumbing stays in the addon path.

  * Tradovate source → driven by a WebSocket observer thread. THIS is
    what wiring assembles: a SourcePipeline bundling the WS observer,
    an EventReplicator, and the follower endpoint, with start()/stop().

Status: validated live
-----------------------
The Tradovate-source pipeline is assembled, startable, and validated
end to end: the TradovateWSObserver's push-frame parser is calibrated
against real frames and a started pipeline connects, authorizes,
ingests the snapshot, and emits OrderEvents that replicate to the
follower (proven against IBKR paper: native OCO bracket + MKT + MODIFY
+ CANCEL). It remains behind a default-OFF flag (TRADESYNC_ENABLE_WS_
PIPELINES) purely so the live IBKR→Tradovate hot path is untouched
unless the user opts in; wiring deliberately changes nothing about that
path.

Dependency injection
--------------------
wiring does not itself create TradovateClients or IbkrApiClients — it
takes factories. That keeps this module pure and unit-testable with
fakes, and lets main.py inject the real, credentialed clients. The
factories are called lazily, only for the pairs that actually need
them, so e.g. no IB Gateway connection is attempted unless an enabled
pair has IBKR as its follower.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from .brokers.endpoint import FollowerEndpoint
from .brokers.ibkr_follower_endpoint import IbkrFollowerEndpoint
from .brokers.tradovate_endpoint import TradovateEndpoint
from .brokers.tradovate_ws_observer import TradovateWSObserver
from .event_replicator import EventReplicator
from .order_event import OrderEvent
from .order_map import OrderMap
from .position_reconciler import PositionReconciler
from .replication_alert import emit_replication_failure
from .replication_config import ReplicationConfig, ReplicationPair


logger = logging.getLogger("tradesync.wiring")


# Factory types injected by the caller (main.py passes real ones;
# tests pass fakes). Each takes the env + account id it's for.
TradovateClientFactory = Callable[[str, str], object]   # (env, account_id) -> TradovateClient
IbkrClientFactory = Callable[[object], object]          # (gateway) -> IbkrApiClient


@dataclass
class SourcePipeline:
    """A startable Tradovate-source replication pipeline: WS observer →
    EventReplicator → follower. start() begins observing; stop() tears
    down. Idempotent-ish; safe to stop() one that never start()ed."""
    pair_name:  str
    observer:   TradovateWSObserver
    replicator: EventReplicator
    follower:   FollowerEndpoint
    reconciler: Optional["PositionReconciler"] = None
    # env ("live"/"demo") routes failure alerts to the right GUI panel.
    env:        str = "live"
    _started:   bool = field(default=False)

    @property
    def identity(self) -> str:
        return f"{self.pair_name}: {self.observer.identity} → " \
               f"{self.follower.identity}"

    def start(self) -> None:
        if self._started:
            logger.warning("SourcePipeline %s already started", self.pair_name)
            return
        # Connect the follower first — if IBKR Gateway isn't reachable
        # we want to fail BEFORE we start observing the source, so we
        # don't observe orders we then can't replicate.
        self.follower.connect()
        # Now that the follower is connected (so order_status works) but
        # BEFORE we start observing new events, prune OrderMap entries
        # whose follower order went terminal while the engine was down.
        # A reconciliation failure must not block startup — the map just
        # keeps a few stale entries, which are harmless (one failed
        # lookup later), so swallow and proceed.
        try:
            self.replicator.reconcile_with_follower()
        except Exception:  # noqa: BLE001 - startup must not be blocked
            logger.exception("OrderMap reconciliation failed for %s — "
                             "starting anyway", self.pair_name)
        self.observer.start_observing(self._on_event)
        # Start the periodic position safety check once BOTH sides are
        # connected, so it can compare real holdings. Read-only: it only
        # warns on divergence, never trades.
        if self.reconciler is not None:
            self.reconciler.start()
        self._started = True
        logger.info("Started replication pipeline — %s", self.identity)

    def stop(self) -> None:
        if not self._started:
            return
        if self.reconciler is not None:
            try:
                self.reconciler.stop()
            except Exception:  # noqa: BLE001 - teardown must not raise
                logger.exception("reconciler stop failed for %s",
                                 self.pair_name)
        try:
            self.observer.stop_observing()
        finally:
            try:
                self.follower.disconnect()
            except Exception:  # noqa: BLE001 - teardown must not raise
                logger.exception("follower disconnect failed for %s",
                                 self.pair_name)
        self._started = False
        logger.info("Stopped replication pipeline — %s", self.pair_name)

    def _on_event(self, event: OrderEvent) -> None:
        """Observer callback (runs on the WS listener thread). Hand the
        event to the EventReplicator, which never raises."""
        result = self.replicator.apply(event)
        if result.success:
            logger.info("Replicated %s: %s", event.kind, result.reason)
        elif result.skipped:
            logger.debug("Skipped %s: %s", event.kind, result.reason)
        else:
            logger.warning("Failed to replicate %s: %s",
                           event.kind, result.reason)
            # Surface the failure structurally: light the GUI Sync-health
            # panel + desktop notification. (A synchronous follower error
            # — place/cancel/modify raised — that the EventReplicator
            # already turned into a failed result.)
            emit_replication_failure(
                env=self.env, kind=str(getattr(event.kind, "name", event.kind)),
                summary=f"{self.pair_name}: {event.kind}",
                reason=result.reason)


@dataclass
class WiringResult:
    """What wiring produced from a ReplicationConfig."""
    source_pipelines: List[SourcePipeline] = field(default_factory=list)
    # Pairs whose source is IBKR — these run on the existing mitmproxy
    # addon path, not here. Surfaced so the bootstrap (and logs) know
    # they exist and were intentionally left to the addon.
    ibkr_source_pairs: List[ReplicationPair] = field(default_factory=list)
    # Human-readable notes about anything skipped or deferred.
    notes: List[str] = field(default_factory=list)


def build_source_pipelines(
    replication_config: ReplicationConfig,
    *,
    tradovate_client_factory: TradovateClientFactory,
    ibkr_client_factory: IbkrClientFactory,
    order_map_factory: Callable[[str, str], OrderMap],
) -> WiringResult:
    """Assemble a SourcePipeline for every ENABLED pair whose source is
    Tradovate. Pairs with an IBKR source are recorded in
    `ibkr_source_pairs` (they run on the addon path) but not built here.

    Parameters
    ----------
    tradovate_client_factory(env, account_id) -> TradovateClient
        Returns a (not-necessarily-connected) Tradovate client for the
        source side. The WS observer calls connect() as needed.
    ibkr_client_factory(gateway) -> IbkrApiClient
        Returns the IbkrApiClient for one Gateway endpoint. Called once
        per distinct (host, port, client_id) across enabled IBKR
        followers — so separate-login followers each get their own
        connection, while same-login followers share one.
    order_map_factory(env, account_id) -> OrderMap
        Returns the persistent OrderMap for a given (env, FOLLOWER
        account). Keyed per-follower, NOT just per-env: two followers in
        the same env must not share one map, or the second follower's
        set_follower_id would overwrite the first's for the same source
        label (the source/Tradovate side is shared), silently breaking
        later modify/cancel routing.
    """
    result = WiringResult()
    # Lazily build IBKR clients, ONE per distinct Gateway endpoint
    # (host, port, client_id). Two followers reached through the SAME
    # Gateway login (one login that sees several accounts) share a
    # client; followers on SEPARATE logins each get their own. This
    # keyed cache is what makes multi-follower (separate-login)
    # replication possible.
    _ibkr_client_cache: dict = {}

    def _get_ibkr_client(gateway):
        key = (gateway.host, gateway.port, gateway.client_id)
        if key not in _ibkr_client_cache:
            _ibkr_client_cache[key] = ibkr_client_factory(gateway)
        return _ibkr_client_cache[key]

    for pair in replication_config.enabled_pairs:
        if pair.source.broker == "ibkr":
            result.ibkr_source_pairs.append(pair)
            result.notes.append(
                f"pair {pair.name!r}: IBKR source → handled by the mitmproxy "
                f"addon path, not the WS pipeline")
            continue

        if pair.source.broker != "tradovate":
            result.notes.append(
                f"pair {pair.name!r}: unsupported source broker "
                f"{pair.source.broker!r} — skipped")
            continue

        # Tradovate source → build the WS pipeline. Resolve THIS pair's
        # IBKR Gateway (its own override, or the config-level default)
        # and bind it, so the follower + reconciler connect through the
        # right Gateway for this follower account.
        gateway = pair.resolve_ibkr_gateway(replication_config.ibkr_gateway)
        get_ibkr_client = lambda gw=gateway: _get_ibkr_client(gw)  # noqa: E731
        follower = _build_follower(pair, get_ibkr_client,
                                   tradovate_client_factory)
        if follower is None:
            result.notes.append(
                f"pair {pair.name!r}: unsupported follower broker "
                f"{pair.follower.broker!r} — skipped")
            continue

        # Surface ASYNC follower rejections (e.g. IBKR rejects an order
        # for size/liquidity after placeOrder returned) on the same
        # structured channel as synchronous failures.
        _attach_rejection_alert(follower, env=pair.source.env,
                                pair_name=pair.name)

        source_client = tradovate_client_factory(
            pair.source.env, pair.source.account_id)
        observer = TradovateWSObserver(
            source_client, env=pair.source.env,
            account_id=pair.source.account_id)
        replicator = EventReplicator(
            follower=follower,
            order_map=order_map_factory(pair.follower.env,
                                        pair.follower.account_id),
            watched_source_accounts=[pair.source.account_id],
            ratio=pair.ratio,
        )
        # Build the periodic position safety check, but only when the
        # follower is IBKR (the side we can query positions on). It
        # compares source (Tradovate) vs follower (IBKR) net positions
        # by symbol and warns on divergence — read-only, never trades.
        reconciler = _build_reconciler(pair, source_client, get_ibkr_client,
                                       observer)
        result.source_pipelines.append(SourcePipeline(
            pair_name=pair.name, observer=observer,
            replicator=replicator, follower=follower,
            reconciler=reconciler, env=pair.source.env))
        result.notes.append(
            f"pair {pair.name!r}: Tradovate→{pair.follower.broker} pipeline "
            f"assembled and live (push-frame parser calibrated; NEW bracket "
            f"+ CANCEL + single-order MODIFY validated end to end)")

    return result


def _build_follower(
    pair: ReplicationPair,
    get_ibkr_client: Callable[[], object],
    tradovate_client_factory: TradovateClientFactory,
) -> Optional[FollowerEndpoint]:
    """Construct the FollowerEndpoint for a pair's follower side, or
    None if the follower broker isn't supported."""
    fb = pair.follower
    if fb.broker == "ibkr":
        return IbkrFollowerEndpoint(
            get_ibkr_client(), env=fb.env, account_id=fb.account_id)
    if fb.broker == "tradovate":
        client = tradovate_client_factory(fb.env, fb.account_id)
        return TradovateEndpoint(
            client, env=fb.env, account_id=fb.account_id)
    return None


def _make_rejection_alert(env: str, pair_name: str):
    """Build the handler that turns an async follower rejection into a
    structured failure alert. Module-level (not a closure inside the
    build loop) so it's independently testable."""
    def _handler(order_id, code, msg):
        emit_replication_failure(
            env=env, kind="REJECTION",
            summary=f"{pair_name}: follower order {order_id} rejected",
            reason=f"[{code}] {msg}")
    return _handler


def _attach_rejection_alert(follower, *, env: str, pair_name: str) -> None:
    """Register an async-rejection alert on followers that support it
    (IBKR). Followers without set_rejection_handler (e.g. Tradovate, whose
    rejections surface synchronously) are left untouched."""
    setter = getattr(follower, "set_rejection_handler", None)
    if setter is None:
        return
    setter(_make_rejection_alert(env, pair_name))


def _build_reconciler(
    pair: ReplicationPair,
    source_client,
    get_ibkr_client: Callable[[], object],
    observer: TradovateWSObserver,
) -> Optional[PositionReconciler]:
    """Build the source-vs-follower position reconciler for a pair, or
    None when the follower isn't IBKR (the only follower we can query
    positions on today).

    Source = Tradovate (positions keyed by contractId, symbol via
    get_contract_name). Follower = IBKR (positions keyed by conId,
    symbol via symbol_for_con_id). The reconciler normalises both to
    symbols before comparing, so the differing native ids don't matter.

    The observer's `health` is passed through so each reconcile pass
    also emits a combined [health] log line (feed state + sync state).
    """
    if pair.follower.broker != "ibkr":
        return None
    ibkr = get_ibkr_client()
    follower_account = pair.follower.account_id
    return PositionReconciler(
        source_positions=source_client.list_positions,
        # Pin the follower read to THIS pair's IBKR account — the Gateway
        # login may see several, and blending them would manufacture
        # phantom mismatches.
        follower_positions=lambda: ibkr.get_positions(account=follower_account),
        source_symbol_of=source_client.get_contract_name,
        follower_symbol_of=ibkr.symbol_for_con_id,
        # The follower trades source × ratio, so the reconciler must
        # compare in scale — otherwise a non-1.0 ratio reads as a
        # permanent (false) mismatch. ratio 1.0 keeps the exact compare.
        ratio=pair.ratio,
        health_source=lambda: observer.health,
    )
