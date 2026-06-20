"""
Tests for deriving the IBKR source-account watch list from the
replication pair model (Vision A, step 1).

The pair model is now the source of truth for which IBKR accounts the
production IBKR→Tradovate filter mirrors FROM; IBKR_WATCHED_ACCOUNTS is
a backward-compat fallback. These tests pin that precedence so the
filter is never left ungoverned when the env field is later removed.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tradesync.config as cfgmod
from tradesync.replication_config import (
    EndpointRef, IbkrGatewayConfig, ReplicationConfig, ReplicationPair)


def _write_config(pairs):
    tmp = tempfile.TemporaryDirectory()
    path = Path(tmp.name) / "replication.json"
    ReplicationConfig(pairs=pairs,
                      ibkr_gateway=IbkrGatewayConfig()).save(path)
    return tmp, path


def _ibkr_source_pair(account="U0000001", enabled=True):
    return ReplicationPair(
        name="ibkr->tv",
        source=EndpointRef(broker="ibkr", env="live", account_id=account),
        follower=EndpointRef(broker="tradovate", env="live",
                             account_id="9000001"),
        enabled=enabled)


def _tradovate_source_pair():
    return ReplicationPair(
        name="tv->ibkr",
        source=EndpointRef(broker="tradovate", env="demo",
                           account_id="9000002"),
        follower=EndpointRef(broker="ibkr", env="demo",
                             account_id="DU0000002"),
        enabled=True)


class TestDeriveWatchList(unittest.TestCase):

    def test_ibkr_source_pair_contributes(self):
        tmp, path = _write_config([_ibkr_source_pair("U0000001")])
        try:
            with patch("tradesync.replication_config."
                       "default_replication_config_path", return_value=path):
                self.assertEqual(
                    cfgmod._ibkr_source_accounts_from_pairs(), ["U0000001"])
        finally:
            tmp.cleanup()

    def test_disabled_pair_excluded(self):
        tmp, path = _write_config([_ibkr_source_pair(enabled=False)])
        try:
            with patch("tradesync.replication_config."
                       "default_replication_config_path", return_value=path):
                self.assertEqual(
                    cfgmod._ibkr_source_accounts_from_pairs(), [])
        finally:
            tmp.cleanup()

    def test_tradovate_source_pair_does_not_contribute(self):
        # A Tradovate→IBKR pair is the OTHER direction; its IBKR account
        # is a FOLLOWER, not a source, so it must not enter the IBKR
        # source watch list.
        tmp, path = _write_config([_tradovate_source_pair()])
        try:
            with patch("tradesync.replication_config."
                       "default_replication_config_path", return_value=path):
                self.assertEqual(
                    cfgmod._ibkr_source_accounts_from_pairs(), [])
        finally:
            tmp.cleanup()

    def test_no_config_file_returns_empty(self):
        with patch("tradesync.replication_config."
                   "default_replication_config_path",
                   return_value=Path("/nonexistent/replication.json")):
            self.assertEqual(cfgmod._ibkr_source_accounts_from_pairs(), [])

    def test_multiple_ibkr_sources_deduped(self):
        tmp, path = _write_config([
            _ibkr_source_pair("U1"),
            ReplicationPair(
                name="ibkr2",
                source=EndpointRef(broker="ibkr", env="live",
                                   account_id="U2"),
                follower=EndpointRef(broker="tradovate", env="live",
                                     account_id="999"),
                enabled=True),
        ])
        try:
            with patch("tradesync.replication_config."
                       "default_replication_config_path", return_value=path):
                self.assertEqual(
                    cfgmod._ibkr_source_accounts_from_pairs(), ["U1", "U2"])
        finally:
            tmp.cleanup()


def _skip_without_demo_env():
    """Config.load() reads a real .env.demo from disk. On a fresh clone
    that file doesn't exist (it's per-user and gitignored), so these two
    tests can't run there — skip rather than fail. They run normally on a
    configured machine."""
    if not cfgmod.env_file_for("demo").exists():
        raise unittest.SkipTest(
            ".env.demo not present (expected on a fresh checkout)")


class TestConfigLoadPrecedence(unittest.TestCase):
    """Config.load: pairs win; env var is the fallback."""

    def test_env_fallback_when_no_pairs(self):
        _skip_without_demo_env()
        tmp, path = _write_config([_tradovate_source_pair()])  # no ibkr src
        try:
            with patch("tradesync.replication_config."
                       "default_replication_config_path", return_value=path), \
                 patch.dict(os.environ,
                            {"IBKR_WATCHED_ACCOUNTS": "U999, U888",
                             "TRADOVATE_ENVIRONMENT": "demo"}):
                cfg = cfgmod.Config.load()
                self.assertEqual(cfg.ibkr_watched_accounts, ["U999", "U888"])
        finally:
            tmp.cleanup()

    def test_pairs_win_over_env(self):
        _skip_without_demo_env()
        tmp, path = _write_config([_ibkr_source_pair("U0000001")])
        try:
            with patch("tradesync.replication_config."
                       "default_replication_config_path", return_value=path), \
                 patch.dict(os.environ,
                            {"IBKR_WATCHED_ACCOUNTS": "U999",
                             "TRADOVATE_ENVIRONMENT": "demo"}):
                cfg = cfgmod.Config.load()
                # Pair-derived account wins; env fallback ignored.
                self.assertEqual(cfg.ibkr_watched_accounts, ["U0000001"])
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
