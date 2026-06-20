"""
Tests for replication_config — load/save/validate of replication.json.
"""

import json
import tempfile
import unittest
from pathlib import Path

from tradesync.replication_config import (
    EndpointRef,
    IbkrGatewayConfig,
    ReplicationConfig,
    ReplicationConfigError,
    ReplicationPair,
    default_replication_config_path,
)


def _pair(name="IBKR→TV", s_broker="ibkr", s_env="live", s_acct="U0000001",
          f_broker="tradovate", f_env="live", f_acct="19000001", enabled=True):
    return ReplicationPair(
        name=name,
        source=EndpointRef(broker=s_broker, env=s_env, account_id=s_acct),
        follower=EndpointRef(broker=f_broker, env=f_env, account_id=f_acct),
        enabled=enabled,
    )


class TestEndpointRef(unittest.TestCase):

    def test_identity(self):
        e = EndpointRef(broker="ibkr", env="live", account_id="U0000001")
        self.assertEqual(e.identity, "ibkr_live_U0000001")

    def test_validate_bad_broker(self):
        with self.assertRaises(ReplicationConfigError):
            EndpointRef(broker="schwab", env="live",
                        account_id="X").validate("test")

    def test_validate_bad_env(self):
        with self.assertRaises(ReplicationConfigError):
            EndpointRef(broker="ibkr", env="prod",
                        account_id="X").validate("test")

    def test_validate_empty_account(self):
        with self.assertRaises(ReplicationConfigError):
            EndpointRef(broker="ibkr", env="live",
                        account_id="  ").validate("test")


class TestPairValidation(unittest.TestCase):

    def test_valid_pair(self):
        _pair().validate()   # must not raise

    def test_source_equals_follower_rejected(self):
        p = _pair(s_broker="ibkr", s_env="live", s_acct="X",
                  f_broker="ibkr", f_env="live", f_acct="X")
        with self.assertRaises(ReplicationConfigError):
            p.validate()

    def test_same_broker_different_account_is_ok(self):
        # Same broker+env but different accounts is a legitimate pair.
        p = _pair(s_broker="tradovate", s_env="demo", s_acct="111",
                  f_broker="tradovate", f_env="demo", f_acct="222")
        p.validate()


class TestPairRatio(unittest.TestCase):

    def test_default_ratio_is_one(self):
        self.assertEqual(_pair().ratio, 1.0)

    def test_valid_ratio_passes(self):
        p = _pair(); p.ratio = 0.33
        p.validate()

    def test_ratio_zero_rejected(self):
        p = _pair(); p.ratio = 0.0
        with self.assertRaises(ReplicationConfigError):
            p.validate()

    def test_ratio_negative_rejected(self):
        p = _pair(); p.ratio = -0.5
        with self.assertRaises(ReplicationConfigError):
            p.validate()

    def test_ratio_above_max_rejected(self):
        p = _pair(); p.ratio = 100.1
        with self.assertRaises(ReplicationConfigError):
            p.validate()

    def test_ratio_at_max_ok(self):
        p = _pair(); p.ratio = 100.0
        p.validate()

    def test_from_dict_reads_ratio(self):
        p = ReplicationPair.from_dict({
            "name": "p", "source": {"broker": "tradovate", "env": "live",
                                     "account_id": "A"},
            "follower": {"broker": "ibkr", "env": "live",
                         "account_id": "B"}, "ratio": 0.5})
        self.assertEqual(p.ratio, 0.5)

    def test_from_dict_default_ratio_when_absent(self):
        p = ReplicationPair.from_dict({
            "name": "p", "source": {"broker": "tradovate", "env": "live",
                                     "account_id": "A"},
            "follower": {"broker": "ibkr", "env": "live",
                         "account_id": "B"}})
        self.assertEqual(p.ratio, 1.0)

    def test_from_dict_non_numeric_ratio_rejected(self):
        with self.assertRaises(ReplicationConfigError):
            ReplicationPair.from_dict({
                "name": "p", "source": {"broker": "tradovate", "env": "live",
                                        "account_id": "A"},
                "follower": {"broker": "ibkr", "env": "live",
                             "account_id": "B"}, "ratio": "abc"})

    def test_save_load_round_trip_preserves_ratio(self):
        import tempfile
        from pathlib import Path
        from tradesync.replication_config import ReplicationConfig
        p = _pair(); p.ratio = 0.25
        cfg = ReplicationConfig(pairs=[p])
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "replication.json"
            cfg.save(path)
            loaded = ReplicationConfig.load(path)
        self.assertEqual(loaded.pairs[0].ratio, 0.25)


class TestIbkrGatewayConfig(unittest.TestCase):

    def test_defaults(self):
        g = IbkrGatewayConfig()
        self.assertEqual(g.host, "127.0.0.1")
        self.assertEqual(g.port, 4002)
        self.assertEqual(g.client_id, 11)

    def test_bad_port(self):
        with self.assertRaises(ReplicationConfigError):
            IbkrGatewayConfig(port=99999).validate()

    def test_from_dict_partial(self):
        g = IbkrGatewayConfig.from_dict({"port": 4001})
        self.assertEqual(g.port, 4001)
        self.assertEqual(g.host, "127.0.0.1")   # default kept

    def test_from_dict_bad_port_type(self):
        with self.assertRaises(ReplicationConfigError):
            IbkrGatewayConfig.from_dict({"port": "abc"})


class TestConfigQueries(unittest.TestCase):

    def test_enabled_pairs(self):
        cfg = ReplicationConfig(pairs=[
            _pair(name="on", enabled=True),
            _pair(name="off", enabled=False),
        ])
        self.assertEqual([p.name for p in cfg.enabled_pairs], ["on"])

    def test_needs_ibkr_gateway_true_when_ibkr_follower(self):
        cfg = ReplicationConfig(pairs=[
            _pair(name="tv→ibkr", s_broker="tradovate", s_env="demo",
                  s_acct="50000001", f_broker="ibkr", f_env="demo",
                  f_acct="DU0000002"),
        ])
        self.assertTrue(cfg.needs_ibkr_gateway())

    def test_needs_ibkr_gateway_false_when_ibkr_only_source(self):
        cfg = ReplicationConfig(pairs=[_pair()])  # ibkr is source here
        self.assertFalse(cfg.needs_ibkr_gateway())

    def test_needs_ibkr_gateway_ignores_disabled(self):
        cfg = ReplicationConfig(pairs=[
            _pair(name="tv→ibkr", s_broker="tradovate", s_env="demo",
                  s_acct="50000001", f_broker="ibkr", f_env="demo",
                  f_acct="DU0000002", enabled=False),
        ])
        self.assertFalse(cfg.needs_ibkr_gateway())


class TestPersistence(unittest.TestCase):

    def test_load_missing_file_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = ReplicationConfig.load(Path(tmp) / "nope.json")
            self.assertEqual(cfg.pairs, [])

    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config" / "replication.json"
            cfg = ReplicationConfig(
                pairs=[
                    _pair(name="live mirror"),
                    _pair(name="tv→ibkr demo", s_broker="tradovate",
                          s_env="demo", s_acct="50000001",
                          f_broker="ibkr", f_env="demo", f_acct="DU0000002"),
                ],
                ibkr_gateway=IbkrGatewayConfig(port=4002, client_id=11),
            )
            cfg.save(path)
            self.assertTrue(path.exists())

            loaded = ReplicationConfig.load(path)
            self.assertEqual(len(loaded.pairs), 2)
            self.assertEqual(loaded.pairs[0].name, "live mirror")
            self.assertEqual(loaded.pairs[0].source.identity,
                             "ibkr_live_U0000001")
            self.assertEqual(loaded.pairs[1].follower.broker, "ibkr")
            self.assertEqual(loaded.ibkr_gateway.port, 4002)

    def test_saved_file_has_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "replication.json"
            ReplicationConfig(pairs=[_pair()]).save(path)
            data = json.loads(path.read_text())
            self.assertEqual(data["schema"], 1)
            self.assertIn("pairs", data)
            self.assertIn("ibkr_gateway", data)

    def test_load_rejects_duplicate_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "replication.json"
            payload = {
                "schema": 1,
                "pairs": [
                    {"name": "dup", "source": {"broker": "ibkr", "env": "live",
                                               "account_id": "A"},
                     "follower": {"broker": "tradovate", "env": "live",
                                  "account_id": "B"}},
                    {"name": "dup", "source": {"broker": "ibkr", "env": "demo",
                                               "account_id": "C"},
                     "follower": {"broker": "tradovate", "env": "demo",
                                  "account_id": "D"}},
                ],
            }
            path.write_text(json.dumps(payload))
            with self.assertRaises(ReplicationConfigError):
                ReplicationConfig.load(path)

    def test_load_rejects_bad_broker(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "replication.json"
            payload = {
                "pairs": [
                    {"name": "x", "source": {"broker": "schwab", "env": "live",
                                             "account_id": "A"},
                     "follower": {"broker": "tradovate", "env": "live",
                                  "account_id": "B"}},
                ],
            }
            path.write_text(json.dumps(payload))
            with self.assertRaises(ReplicationConfigError):
                ReplicationConfig.load(path)

    def test_load_rejects_missing_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "replication.json"
            payload = {"pairs": [{"name": "x",
                                  "source": {"broker": "ibkr", "env": "live"}}]}
            path.write_text(json.dumps(payload))
            with self.assertRaises(ReplicationConfigError):
                ReplicationConfig.load(path)

    def test_default_path(self):
        p = default_replication_config_path(Path("/proj"))
        self.assertEqual(p, Path("/proj/config/replication.json"))


class TestDuplicateFollowerGuard(unittest.TestCase):
    """Stage 4: two ENABLED pairs must not target the same follower
    endpoint (that account would receive every order twice)."""

    def test_two_enabled_pairs_same_follower_rejected(self):
        cfg = ReplicationConfig(pairs=[
            _pair(name="A", s_broker="tradovate", s_acct="19000001",
                  f_broker="ibkr", f_acct="U0000001"),
            _pair(name="B", s_broker="tradovate", s_acct="OTHER",
                  f_broker="ibkr", f_acct="U0000001"),   # same follower
        ])
        with self.assertRaises(ReplicationConfigError):
            cfg.validate()

    def test_same_follower_ok_when_one_disabled(self):
        cfg = ReplicationConfig(pairs=[
            _pair(name="A", s_broker="tradovate", s_acct="19000001",
                  f_broker="ibkr", f_acct="U0000001"),
            _pair(name="B", s_broker="tradovate", s_acct="OTHER",
                  f_broker="ibkr", f_acct="U0000001", enabled=False),
        ])
        cfg.validate()   # must not raise

    def test_distinct_followers_ok(self):
        cfg = ReplicationConfig(pairs=[
            _pair(name="A", s_broker="tradovate", s_acct="19000001",
                  f_broker="ibkr", f_acct="U0000001"),
            _pair(name="B", s_broker="tradovate", s_acct="19000001",
                  f_broker="ibkr", f_acct="U9999999"),
        ])
        cfg.validate()   # distinct followers, fine


class TestPerFollowerGateway(unittest.TestCase):
    """Stage 1 of multi-follower: a pair may carry its own IBKR Gateway
    override (separate-login followers). Backward-compatible: absent =>
    None => use the config-level ibkr_gateway."""

    def test_from_dict_parses_per_pair_gateway(self):
        p = ReplicationPair.from_dict({
            "name": "tv->ibkr A",
            "source": {"broker": "tradovate", "env": "live",
                       "account_id": "19000001"},
            "follower": {"broker": "ibkr", "env": "live",
                         "account_id": "U0000001"},
            "ibkr_gateway": {"host": "127.0.0.1", "port": 4001,
                             "client_id": 21},
        })
        self.assertIsNotNone(p.ibkr_gateway)
        self.assertEqual(p.ibkr_gateway.port, 4001)
        self.assertEqual(p.ibkr_gateway.client_id, 21)

    def test_from_dict_absent_gateway_is_none(self):
        p = ReplicationPair.from_dict({
            "name": "tv->ibkr B",
            "source": {"broker": "tradovate", "env": "live",
                       "account_id": "19000001"},
            "follower": {"broker": "ibkr", "env": "live",
                         "account_id": "U9999999"},
        })
        self.assertIsNone(p.ibkr_gateway)

    def test_resolve_uses_override_when_set(self):
        override = IbkrGatewayConfig(port=4001, client_id=21)
        default = IbkrGatewayConfig(port=4002, client_id=11)
        p = _pair(f_broker="ibkr", f_acct="U0000001")
        p.ibkr_gateway = override
        self.assertIs(p.resolve_ibkr_gateway(default), override)

    def test_resolve_falls_back_to_default(self):
        default = IbkrGatewayConfig(port=4002, client_id=11)
        p = _pair(f_broker="ibkr", f_acct="U0000001")
        self.assertIs(p.resolve_ibkr_gateway(default), default)

    def test_validate_rejects_bad_per_pair_gateway(self):
        p = _pair(f_broker="ibkr", f_acct="U0000001")
        p.ibkr_gateway = IbkrGatewayConfig(port=99999)   # out of range
        with self.assertRaises(ReplicationConfigError):
            p.validate()

    def test_round_trip_preserves_per_pair_gateway(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "replication.json"
            pa = _pair(name="A", s_broker="tradovate", s_acct="19000001",
                       f_broker="ibkr", f_acct="U0000001")
            pa.ibkr_gateway = IbkrGatewayConfig(port=4001, client_id=21)
            pb = _pair(name="B", s_broker="tradovate", s_acct="19000001",
                       f_broker="ibkr", f_acct="U9999999")  # no override
            ReplicationConfig(pairs=[pa, pb]).save(path)

            data = json.loads(path.read_text())
            # Override pair writes the key; the other does NOT (compat).
            self.assertIn("ibkr_gateway", data["pairs"][0])
            self.assertNotIn("ibkr_gateway", data["pairs"][1])

            loaded = ReplicationConfig.load(path)
            self.assertEqual(loaded.pairs[0].ibkr_gateway.client_id, 21)
            self.assertIsNone(loaded.pairs[1].ibkr_gateway)


if __name__ == "__main__":
    unittest.main()
