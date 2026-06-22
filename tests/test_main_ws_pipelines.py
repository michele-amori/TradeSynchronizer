"""
Tests for main.py's Tradovate-source WS pipeline wiring — specifically
that it stays OFF by default and only assembles pipelines when the
explicit opt-in flag is set.

The point is to PROVE the live IBKR→Tradovate hot path is unaffected:
with the flag unset, _build_source_pipelines_or_empty returns [] and
never touches the WS / ibapi machinery.
"""

import logging
import os
import unittest
from unittest.mock import MagicMock, patch

import main


log = logging.getLogger("test")


class TestFlagParsing(unittest.TestCase):

    def test_off_by_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TRADESYNC_ENABLE_WS_PIPELINES", None)
            self.assertFalse(main._ws_pipelines_enabled())

    def test_on_values(self):
        for v in ("1", "true", "yes", "on", "TRUE", "On"):
            with patch.dict(os.environ,
                            {"TRADESYNC_ENABLE_WS_PIPELINES": v}):
                self.assertTrue(main._ws_pipelines_enabled())

    def test_off_values(self):
        for v in ("0", "false", "no", "", "off"):
            with patch.dict(os.environ,
                            {"TRADESYNC_ENABLE_WS_PIPELINES": v}):
                self.assertFalse(main._ws_pipelines_enabled())


class TestBuildSourcePipelines(unittest.TestCase):

    def _cfg(self):
        cfg = MagicMock()
        cfg.tradovate_api_url = "https://demo.tradovateapi.com/v1"
        cfg.tradovate_username = "u"
        cfg.tradovate_password = "p"
        cfg.tradovate_app_id = "app"
        cfg.tradovate_app_ver = "1.0"
        cfg.tradovate_cid = "cid"
        cfg.tradovate_sec = "sec"
        cfg.tradovate_device_id = ""
        cfg.tradovate_is_automated = False
        return cfg

    def test_disabled_returns_empty_and_touches_nothing(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TRADESYNC_ENABLE_WS_PIPELINES", None)
            # If it tried to import wiring/ibapi or load config, this
            # would do real work; we assert it returns [] immediately.
            out = main._build_source_pipelines_or_empty(self._cfg(), log)
            self.assertEqual(out, [])

    def test_enabled_but_no_config_returns_empty(self):
        # Flag on, but replication.json load yields an empty config →
        # no enabled pairs → no pipelines.
        with patch.dict(os.environ,
                        {"TRADESYNC_ENABLE_WS_PIPELINES": "1"}):
            with patch("tradesync.replication_config.ReplicationConfig.load") \
                    as mock_load:
                mock_load.return_value = MagicMock(enabled_pairs=[])
                out = main._build_source_pipelines_or_empty(self._cfg(), log)
                self.assertEqual(out, [])

    def test_enabled_invalid_config_returns_empty_not_raises(self):
        from tradesync.replication_config import ReplicationConfigError
        with patch.dict(os.environ,
                        {"TRADESYNC_ENABLE_WS_PIPELINES": "1"}):
            with patch("tradesync.replication_config.ReplicationConfig.load",
                       side_effect=ReplicationConfigError("bad")):
                # Must contain the failure, not crash the engine.
                out = main._build_source_pipelines_or_empty(self._cfg(), log)
                self.assertEqual(out, [])

    def test_enabled_with_pair_builds_pipeline(self):
        # Flag on + one enabled Tradovate→IBKR pair → one pipeline.
        from tradesync.replication_config import (
            EndpointRef, IbkrGatewayConfig, ReplicationConfig,
            ReplicationPair,
        )
        pair = ReplicationPair(
            name="tv→ibkr",
            source=EndpointRef(broker="tradovate", env="demo",
                               account_id="50000001"),
            follower=EndpointRef(broker="ibkr", env="demo",
                                 account_id="DU0000002"),
        )
        rep_cfg = ReplicationConfig(pairs=[pair],
                                    ibkr_gateway=IbkrGatewayConfig())
        with patch.dict(os.environ,
                        {"TRADESYNC_ENABLE_WS_PIPELINES": "1"}):
            with patch("tradesync.replication_config.ReplicationConfig.load",
                       return_value=rep_cfg), \
                 patch("tradesync.brokers.ibkr_api_client.IbkrApiClient"), \
                 patch("tradesync.brokers.tradovate.TradovateClient"), \
                 patch("tradesync.ibc_gateway_orchestrator."
                       "ensure_ports_listening",
                       return_value=[]) as mock_gw:
                out = main._build_source_pipelines_or_empty(self._cfg(), log)
                self.assertEqual(len(out), 1)
                self.assertEqual(out[0].pair_name, "tv→ibkr")
                # An IBKR-follower pair must trigger the port orchestrator.
                mock_gw.assert_called_once()

    def test_tradovate_follower_does_not_open_gateway(self):
        # A Tradovate→Tradovate pair needs no Gateway; the opener must
        # NOT be called.
        from tradesync.replication_config import (
            EndpointRef, ReplicationConfig, ReplicationPair,
        )
        pair = ReplicationPair(
            name="tv→tv",
            source=EndpointRef(broker="tradovate", env="demo",
                               account_id="111"),
            follower=EndpointRef(broker="tradovate", env="demo",
                                 account_id="222"))
        rep_cfg = ReplicationConfig(pairs=[pair])
        with patch.dict(os.environ,
                        {"TRADESYNC_ENABLE_WS_PIPELINES": "1"}):
            with patch("tradesync.replication_config.ReplicationConfig.load",
                       return_value=rep_cfg), \
                 patch("tradesync.brokers.tradovate.TradovateClient"), \
                 patch("tradesync.ibc_gateway_orchestrator."
                       "ensure_ports_listening",
                       return_value=[]) as mock_gw:
                main._build_source_pipelines_or_empty(self._cfg(), log)
                mock_gw.assert_not_called()


class TestNeutralIbkrSourcePair(unittest.TestCase):
    """The lookup that finds the enabled IBKR-source pair for the
    neutral/IBKR→IBKR path."""

    def _pair(self, s_broker="ibkr", f_broker="tradovate", enabled=True):
        from tradesync.replication_config import EndpointRef, ReplicationPair
        return ReplicationPair(
            name="p",
            source=EndpointRef(broker=s_broker, env="live",
                               account_id="U0000001"),
            follower=EndpointRef(broker=f_broker, env="live",
                                 account_id="U999"),
            enabled=enabled)

    def _load(self, *pairs):
        from tradesync.replication_config import ReplicationConfig
        return patch(
            "tradesync.replication_config.ReplicationConfig.load",
            return_value=ReplicationConfig(pairs=list(pairs)))

    def test_finds_enabled_ibkr_source_pair(self):
        with self._load(self._pair()):
            p = main._neutral_ibkr_source_pair(log)
            self.assertIsNotNone(p)
            self.assertEqual(p.source.broker, "ibkr")

    def test_none_when_no_ibkr_source(self):
        with self._load(self._pair(s_broker="tradovate")):
            self.assertIsNone(main._neutral_ibkr_source_pair(log))

    def test_skips_disabled_pair(self):
        with self._load(self._pair(enabled=False)):
            self.assertIsNone(main._neutral_ibkr_source_pair(log))

    def test_none_on_missing_config(self):
        with patch("tradesync.replication_config.ReplicationConfig.load",
                   side_effect=FileNotFoundError):
            self.assertIsNone(main._neutral_ibkr_source_pair(log))


class TestBuildNeutralIbkrSourceFollower(unittest.TestCase):
    """_build_neutral_ibkr_source picks the follower from the matching
    pair: IBKR follower → IbkrFollowerEndpoint (no conid_resolver, ratio
    threaded); else → Tradovate follower (original Step-A behaviour)."""

    def _cfg(self):
        cfg = MagicMock()
        cfg.tradovate_env = "live"
        cfg.tradovate_acct_id = "19000001"
        return cfg

    def _pair(self, follower_broker, ratio=1.0):
        from tradesync.replication_config import (
            EndpointRef, IbkrGatewayConfig, ReplicationConfig, ReplicationPair,
        )
        pair = ReplicationPair(
            name="MASTER→FAMILY",
            source=EndpointRef(broker="ibkr", env="live",
                               account_id="U0000001"),
            follower=EndpointRef(broker=follower_broker, env="live",
                                 account_id="U999"),
            ratio=ratio)
        return ReplicationConfig(pairs=[pair],
                                 ibkr_gateway=IbkrGatewayConfig(
                                     host="127.0.0.1", port=4001, client_id=12))

    def test_ibkr_follower_builds_ibkr_endpoint_with_ratio_no_resolver(self):
        rep = self._pair("ibkr", ratio=0.5)
        with patch("tradesync.replication_config.ReplicationConfig.load",
                   return_value=rep), \
             patch("tradesync.brokers.ibkr_api_client.IbkrApiClient") as MockClient, \
             patch("tradesync.brokers.ibkr_follower_endpoint."
                   "IbkrFollowerEndpoint") as MockFollower, \
             patch("tradesync.event_replicator.EventReplicator") as MockER, \
             patch("tradesync.proxy.ibkr_event_source_observer."
                   "IbkrEventSourceObserver"):
            main._build_neutral_ibkr_source(
                self._cfg(), MagicMock(), MagicMock(), MagicMock(), log)
            # IBKR client built from the gateway block
            MockClient.assert_called_once_with(
                host="127.0.0.1", port=4001, client_id=12)
            # follower is the IBKR endpoint on the follower account
            self.assertTrue(MockFollower.called)
            self.assertEqual(MockFollower.call_args.kwargs["account_id"], "U999")
            # EventReplicator got ratio=0.5 and NO conid_resolver
            er_kwargs = MockER.call_args.kwargs
            self.assertEqual(er_kwargs["ratio"], 0.5)
            self.assertIsNone(er_kwargs["conid_resolver"])

    def test_tradovate_follower_keeps_resolver(self):
        rep = self._pair("tradovate", ratio=1.0)
        with patch("tradesync.replication_config.ReplicationConfig.load",
                   return_value=rep), \
             patch("tradesync.brokers.tradovate_endpoint.TradovateEndpoint"), \
             patch("tradesync.event_replicator.EventReplicator") as MockER, \
             patch("tradesync.proxy.ibkr_event_source_observer."
                   "IbkrEventSourceObserver"):
            resolver = MagicMock()
            main._build_neutral_ibkr_source(
                self._cfg(), MagicMock(), resolver, MagicMock(), log)
            er_kwargs = MockER.call_args.kwargs
            # conid_resolver IS set for the Tradovate follower
            self.assertIs(er_kwargs["conid_resolver"], resolver.resolve_symbol)

    def test_neutral_path_self_reconciles_at_build(self):
        # The neutral path must reconcile its own OrderMap at startup
        # (it no longer relies on the Replicator's reconcile_with_tradovate).
        rep = self._pair("tradovate", ratio=1.0)
        with patch("tradesync.replication_config.ReplicationConfig.load",
                   return_value=rep), \
             patch("tradesync.brokers.tradovate_endpoint.TradovateEndpoint"), \
             patch("tradesync.event_replicator.EventReplicator") as MockER, \
             patch("tradesync.proxy.ibkr_event_source_observer."
                   "IbkrEventSourceObserver"):
            main._build_neutral_ibkr_source(
                self._cfg(), MagicMock(), MagicMock(), MagicMock(), log)
            MockER.return_value.reconcile_with_follower.assert_called_once_with()

    def test_neutral_path_build_survives_reconcile_failure(self):
        # A reconciliation error must not break the build / startup.
        rep = self._pair("tradovate", ratio=1.0)
        with patch("tradesync.replication_config.ReplicationConfig.load",
                   return_value=rep), \
             patch("tradesync.brokers.tradovate_endpoint.TradovateEndpoint"), \
             patch("tradesync.event_replicator.EventReplicator") as MockER, \
             patch("tradesync.proxy.ibkr_event_source_observer."
                   "IbkrEventSourceObserver") as MockObs:
            MockER.return_value.reconcile_with_follower.side_effect = \
                RuntimeError("boom")
            obs = main._build_neutral_ibkr_source(
                self._cfg(), MagicMock(), MagicMock(), MagicMock(), log)
            # Still returns the observer despite the reconcile error.
            self.assertIs(obs, MockObs.return_value)


if __name__ == "__main__":
    unittest.main()
