"""
Tests for the GUI's decision to enable the Tradovate→IBKR WS-pipeline
direction when launching an engine.

Bug this guards against: the GUI builds the engine subprocess env from
os.environ + a few overrides but never set TRADESYNC_ENABLE_WS_PIPELINES,
so launching from the app silently never started the Tradovate→IBKR
direction — only the IBKR→Tradovate proxy hot path ran. The flag must be
passed iff there's an enabled Tradovate-source pair for that env.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tradesync.ui.app import _has_enabled_tradovate_source_pair
from tradesync.replication_config import (
    EndpointRef, IbkrGatewayConfig, ReplicationConfig, ReplicationPair)


def _write(pairs):
    tmp = tempfile.TemporaryDirectory()
    path = Path(tmp.name) / "config" / "replication.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    ReplicationConfig(pairs=pairs,
                      ibkr_gateway=IbkrGatewayConfig()).save(path)
    return tmp, path


def _tv_source(env, account="19000001", enabled=True):
    return ReplicationPair(
        name=f"tv-{env}",
        source=EndpointRef(broker="tradovate", env=env, account_id=account),
        follower=EndpointRef(broker="ibkr", env=env, account_id="U1"),
        enabled=enabled)


def _ibkr_source(env):
    return ReplicationPair(
        name=f"ibkr-{env}",
        source=EndpointRef(broker="ibkr", env=env, account_id="U1"),
        follower=EndpointRef(broker="tradovate", env=env, account_id="1"),
        enabled=True)


class TestWsFlagDecision(unittest.TestCase):

    def _check(self, pairs, env):
        tmp, path = _write(pairs)
        try:
            with patch("tradesync.replication_config."
                       "default_replication_config_path", return_value=path):
                return _has_enabled_tradovate_source_pair(
                    path.parent.parent, env)
        finally:
            tmp.cleanup()

    def test_enabled_tradovate_source_enables_flag(self):
        self.assertTrue(self._check([_tv_source("live")], "live"))

    def test_disabled_pair_does_not_enable(self):
        self.assertFalse(
            self._check([_tv_source("live", enabled=False)], "live"))

    def test_wrong_env_does_not_enable(self):
        # An enabled live pair must NOT enable the demo engine.
        self.assertFalse(self._check([_tv_source("live")], "demo"))

    def test_ibkr_source_does_not_enable(self):
        # IBKR-source is the OTHER direction; it must not switch on the
        # WS pipeline.
        self.assertFalse(self._check([_ibkr_source("live")], "live"))

    def test_no_config_file_is_false(self):
        with patch("tradesync.replication_config."
                   "default_replication_config_path",
                   return_value=Path("/nonexistent/replication.json")):
            self.assertFalse(
                _has_enabled_tradovate_source_pair(Path("/nonexistent"),
                                                   "live"))


if __name__ == "__main__":
    unittest.main()
