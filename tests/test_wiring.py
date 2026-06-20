"""
Tests for wiring — assembling replication pipelines from a
ReplicationConfig.

Uses fake client factories (no real Tradovate auth, no IB Gateway) to
verify: which pairs become WS SourcePipelines vs land on the addon
path, lazy IBKR-client construction, pipeline start/stop ordering
(follower connects before observing; observer stops before follower
disconnects), and that the observer callback routes events into the
EventReplicator.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from tradesync.order_map import OrderMap
from tradesync.replication_config import (
    EndpointRef,
    IbkrGatewayConfig,
    ReplicationConfig,
    ReplicationPair,
)
from tradesync.wiring import (
    SourcePipeline,
    build_source_pipelines,
)


def _pair(name, s_broker, s_acct, f_broker, f_acct, env="demo", enabled=True):
    return ReplicationPair(
        name=name,
        source=EndpointRef(broker=s_broker, env=env, account_id=s_acct),
        follower=EndpointRef(broker=f_broker, env=env, account_id=f_acct),
        enabled=enabled,
    )


class _Factories:
    """Bundles fake factories and records what they built."""

    def __init__(self):
        self.tradovate_clients = []
        self.ibkr_client_calls = 0
        self._tmp = tempfile.TemporaryDirectory()

    def tradovate(self, env, account_id):
        c = MagicMock(name=f"tradovate-{env}-{account_id}")
        self.tradovate_clients.append((env, account_id, c))
        return c

    def ibkr(self, gateway=None):
        self.ibkr_client_calls += 1
        self.ibkr_gateways_seen = getattr(self, "ibkr_gateways_seen", [])
        self.ibkr_gateways_seen.append(gateway)
        return MagicMock(name="ibkr-client")

    def order_map(self, env, account_id=""):
        self.order_map_keys = getattr(self, "order_map_keys", [])
        self.order_map_keys.append((env, account_id))
        return OrderMap(
            Path(self._tmp.name) / f"orders-{env}-{account_id}.json")

    def build(self, cfg):
        return build_source_pipelines(
            cfg,
            tradovate_client_factory=self.tradovate,
            ibkr_client_factory=self.ibkr,
            order_map_factory=self.order_map,
        )

    def cleanup(self):
        self._tmp.cleanup()


class TestPairRouting(unittest.TestCase):

    def setUp(self):
        self.f = _Factories()

    def tearDown(self):
        self.f.cleanup()

    def test_tradovate_source_builds_pipeline(self):
        cfg = ReplicationConfig(pairs=[
            _pair("tv→ibkr", "tradovate", "50000001", "ibkr", "DU0000002"),
        ])
        result = self.f.build(cfg)
        self.assertEqual(len(result.source_pipelines), 1)
        self.assertEqual(len(result.ibkr_source_pairs), 0)
        p = result.source_pipelines[0]
        self.assertEqual(p.pair_name, "tv→ibkr")
        self.assertIn("tradovate_demo_50000001", p.identity)
        self.assertIn("ibkr_demo_DU0000002", p.identity)

    def test_ibkr_source_goes_to_addon_path(self):
        cfg = ReplicationConfig(pairs=[
            _pair("ibkr→tv", "ibkr", "U0000001", "tradovate", "19000001"),
        ])
        result = self.f.build(cfg)
        self.assertEqual(len(result.source_pipelines), 0)
        self.assertEqual(len(result.ibkr_source_pairs), 1)
        # IBKR client must NOT have been built (no Tradovate-source pair
        # needs the gateway here).
        self.assertEqual(self.f.ibkr_client_calls, 0)

    def test_disabled_pair_ignored(self):
        cfg = ReplicationConfig(pairs=[
            _pair("off", "tradovate", "50000001", "ibkr", "DU0000002",
                  enabled=False),
        ])
        result = self.f.build(cfg)
        self.assertEqual(len(result.source_pipelines), 0)

    def test_ibkr_client_built_lazily_once(self):
        # Two Tradovate→IBKR pairs share one Gateway client.
        cfg = ReplicationConfig(pairs=[
            _pair("a", "tradovate", "111", "ibkr", "DU1"),
            _pair("b", "tradovate", "222", "ibkr", "DU2"),
        ])
        result = self.f.build(cfg)
        self.assertEqual(len(result.source_pipelines), 2)
        self.assertEqual(self.f.ibkr_client_calls, 1)   # shared, built once

    def test_tradovate_to_tradovate_builds_tradovate_follower(self):
        cfg = ReplicationConfig(pairs=[
            _pair("tv→tv", "tradovate", "111", "tradovate", "222"),
        ])
        result = self.f.build(cfg)
        self.assertEqual(len(result.source_pipelines), 1)
        # IBKR client not needed for a Tradovate follower.
        self.assertEqual(self.f.ibkr_client_calls, 0)


class TestPipelineLifecycle(unittest.TestCase):

    def _pipeline_with_fakes(self):
        observer = MagicMock()
        follower = MagicMock()
        replicator = MagicMock()
        p = SourcePipeline(pair_name="t", observer=observer,
                           replicator=replicator, follower=follower)
        return p, observer, follower, replicator

    def test_start_connects_follower_before_observing(self):
        p, observer, follower, _ = self._pipeline_with_fakes()
        calls = []
        follower.connect.side_effect = lambda: calls.append("connect")
        observer.start_observing.side_effect = \
            lambda cb: calls.append("observe")
        p.start()
        self.assertEqual(calls, ["connect", "observe"])

    def test_start_reconciles_map_after_connect_before_observing(self):
        # OrderMap reconciliation must run with the follower connected
        # (so order_status works) but before we observe new events.
        p, observer, follower, replicator = self._pipeline_with_fakes()
        calls = []
        follower.connect.side_effect = lambda: calls.append("connect")
        replicator.reconcile_with_follower.side_effect = \
            lambda: calls.append("reconcile")
        observer.start_observing.side_effect = \
            lambda cb: calls.append("observe")
        p.start()
        self.assertEqual(calls, ["connect", "reconcile", "observe"])

    def test_start_proceeds_if_reconcile_raises(self):
        # A reconciliation failure must NOT block startup.
        p, observer, follower, replicator = self._pipeline_with_fakes()
        replicator.reconcile_with_follower.side_effect = RuntimeError("boom")
        p.start()   # must not raise
        observer.start_observing.assert_called_once()

    def test_stop_stops_observer_before_disconnect(self):
        p, observer, follower, _ = self._pipeline_with_fakes()
        p.start()
        calls = []
        observer.stop_observing.side_effect = lambda: calls.append("stop_obs")
        follower.disconnect.side_effect = lambda: calls.append("disconnect")
        p.stop()
        self.assertEqual(calls, ["stop_obs", "disconnect"])

    def test_double_start_is_noop(self):
        p, observer, follower, _ = self._pipeline_with_fakes()
        p.start()
        p.start()
        self.assertEqual(follower.connect.call_count, 1)

    def test_stop_without_start_is_safe(self):
        p, observer, follower, _ = self._pipeline_with_fakes()
        p.stop()   # must not raise
        follower.disconnect.assert_not_called()

    def test_event_callback_routes_to_replicator(self):
        p, observer, follower, replicator = self._pipeline_with_fakes()
        replicator.apply.return_value = MagicMock(
            success=True, skipped=False, reason="ok")
        # Capture the callback the pipeline registers.
        captured = {}
        observer.start_observing.side_effect = \
            lambda cb: captured.update(cb=cb)
        p.start()
        sentinel_event = MagicMock(kind="NEW")
        captured["cb"](sentinel_event)
        replicator.apply.assert_called_once_with(sentinel_event)


class TestPipelineFailureAlerting(unittest.TestCase):
    """A failed replication on the WS pipeline must surface structurally
    (GUI panel + desktop notify) via emit_replication_failure, not just a
    log line."""

    def _pipeline(self, result):
        from unittest.mock import patch
        from tradesync.event_replicator import EventResult
        replicator = MagicMock()
        replicator.apply.return_value = result
        p = SourcePipeline(
            pair_name="P", observer=MagicMock(), replicator=replicator,
            follower=MagicMock(), reconciler=None, env="live")
        return p

    def test_failed_apply_emits_alert(self):
        from unittest.mock import patch
        from tradesync.event_replicator import EventResult
        ev = MagicMock()
        ev.kind = "NEW"
        p = self._pipeline(EventResult(success=False, skipped=False,
                                       reason="IBKR rejected"))
        with patch("tradesync.wiring.emit_replication_failure") as emit:
            p._on_event(ev)
        emit.assert_called_once()
        self.assertEqual(emit.call_args.kwargs["env"], "live")
        self.assertIn("rejected", emit.call_args.kwargs["reason"])

    def test_success_and_skip_do_not_emit(self):
        from unittest.mock import patch
        from tradesync.event_replicator import EventResult
        ev = MagicMock()
        ev.kind = "NEW"
        for res in (EventResult(success=True, skipped=False, reason="ok"),
                    EventResult(success=False, skipped=True, reason="skip")):
            p = self._pipeline(res)
            with patch("tradesync.wiring.emit_replication_failure") as emit:
                p._on_event(ev)
            emit.assert_not_called()


class TestAsyncRejectionAlert(unittest.TestCase):
    """The bootstrap attaches an async-rejection alert to IBKR followers
    that surfaces a REJECTION via emit_replication_failure."""

    def test_make_rejection_alert_emits(self):
        from unittest.mock import patch
        from tradesync.wiring import _make_rejection_alert
        handler = _make_rejection_alert(env="live", pair_name="P")
        with patch("tradesync.wiring.emit_replication_failure") as emit:
            handler("12345", 201, "size exceeds max")
        emit.assert_called_once()
        kw = emit.call_args.kwargs
        self.assertEqual(kw["env"], "live")
        self.assertEqual(kw["kind"], "REJECTION")
        self.assertIn("12345", kw["summary"])
        self.assertIn("201", kw["reason"])

    def test_attach_registers_on_ibkr_follower(self):
        from tradesync.wiring import _attach_rejection_alert
        follower = MagicMock()   # has set_rejection_handler
        _attach_rejection_alert(follower, env="live", pair_name="P")
        follower.set_rejection_handler.assert_called_once()

    def test_attach_is_noop_without_setter(self):
        from tradesync.wiring import _attach_rejection_alert

        class _NoSetter:
            pass
        # Must not raise on a follower lacking set_rejection_handler.
        _attach_rejection_alert(_NoSetter(), env="live", pair_name="P")


class TestMultiFollowerClientCache(unittest.TestCase):
    """Stage 2: IBKR clients are cached per (host, port, client_id), so
    separate-login followers each get their own client while same-login
    followers share one."""

    def setUp(self):
        self.f = _Factories()
        self.addCleanup(self.f.cleanup)

    def _tv_ibkr(self, name, f_acct, gw):
        p = _pair(name, "tradovate", "19000001", "ibkr", f_acct)
        p.ibkr_gateway = gw
        return p

    def test_distinct_gateways_get_distinct_clients(self):
        cfg = ReplicationConfig(
            pairs=[
                self._tv_ibkr("A", "U0000001",
                              IbkrGatewayConfig(port=4001, client_id=21)),
                self._tv_ibkr("B", "U9999999",
                              IbkrGatewayConfig(port=4011, client_id=22)),
            ],
            ibkr_gateway=IbkrGatewayConfig(port=4002, client_id=11),
        )
        result = self.f.build(cfg)
        self.assertEqual(len(result.source_pipelines), 2)
        # One client per distinct gateway → two factory calls.
        self.assertEqual(self.f.ibkr_client_calls, 2)

    def test_same_gateway_shares_one_client(self):
        gw = IbkrGatewayConfig(port=4001, client_id=21)
        cfg = ReplicationConfig(
            pairs=[
                self._tv_ibkr("A", "U0000001", gw),
                self._tv_ibkr("B", "U9999999", gw),
            ],
            ibkr_gateway=IbkrGatewayConfig(port=4002, client_id=11),
        )
        result = self.f.build(cfg)
        self.assertEqual(len(result.source_pipelines), 2)
        # Same (host,port,client_id) → shared client → one factory call.
        self.assertEqual(self.f.ibkr_client_calls, 1)

    def test_no_override_uses_global_gateway(self):
        cfg = ReplicationConfig(
            pairs=[_pair("A", "tradovate", "19000001", "ibkr", "U0000001")],
            ibkr_gateway=IbkrGatewayConfig(port=4002, client_id=11),
        )
        self.f.build(cfg)
        self.assertEqual(self.f.ibkr_client_calls, 1)
        seen = self.f.ibkr_gateways_seen[0]
        self.assertEqual(seen.port, 4002)
        self.assertEqual(seen.client_id, 11)

    def test_same_env_followers_get_distinct_order_maps(self):
        # Stage 3: two IBKR followers in the SAME env must get separate
        # OrderMaps (keyed per follower account), or the second would
        # overwrite the first's source-label -> follower-id mappings.
        cfg = ReplicationConfig(
            pairs=[
                self._tv_ibkr("A", "U0000001",
                              IbkrGatewayConfig(port=4001, client_id=21)),
                self._tv_ibkr("B", "U9999999",
                              IbkrGatewayConfig(port=4011, client_id=22)),
            ],
            ibkr_gateway=IbkrGatewayConfig(port=4002, client_id=11),
        )
        self.f.build(cfg)
        # Both pairs are env=demo, distinct follower accounts → distinct
        # (env, account) order-map keys.
        keys = self.f.order_map_keys
        self.assertEqual(len(keys), 2)
        self.assertEqual({k[1] for k in keys}, {"U0000001", "U9999999"})
        self.assertEqual(len(set(keys)), 2)


if __name__ == "__main__":
    unittest.main()
