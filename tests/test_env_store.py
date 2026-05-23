"""
Unit tests for tradesync.ui.app.EnvStore — the two-file dotenv
manager that backs the GUI's Settings tab.

Run from the repo root:

    python3 -m unittest tests.test_env_store
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tradesync.ui.app import (
    ENVIRONMENTS, PER_ENV_DEFAULTS, PER_ENV_KEYS, EnvStore,
)


class _TmpEnv:
    """Context manager: scratch project_root with optional .env.live
    and .env.demo files."""

    def __init__(self, live_text: str | None = None,
                 demo_text: str | None = None):
        self.live_text = live_text
        self.demo_text = demo_text

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        if self.live_text is not None:
            (root / ".env.live").write_text(self.live_text)
        if self.demo_text is not None:
            (root / ".env.demo").write_text(self.demo_text)
        return EnvStore(project_root=root)

    def __exit__(self, *exc):
        self._tmp.cleanup()


class TestEnvStoreLoad(unittest.TestCase):

    def test_load_no_files_is_clean(self):
        with _TmpEnv() as s:
            s.load()
            self.assertEqual(s.shared, {})
            for env in ENVIRONMENTS:
                self.assertEqual(s.per_env[env], {})

    def test_load_only_live(self):
        live = (
            "TRADOVATE_USERNAME=u_live\n"
            "TRADOVATE_PASSWORD=p_live\n"
            "TRADOVATE_CID=cid_live\n"
            "TRADOVATE_SEC=sec_live\n"
            "TRADOVATE_ACCOUNT_ID=1290252\n"
            "IBKR_WATCHED_ACCOUNTS=U7713037\n"
            "PROXY_LISTEN_PORT=8080\n"
            "PROXY_LISTEN_HOST=127.0.0.1\n"
            "TRADOVATE_APP_ID=TradeSynchronizer\n"
            "REPLICATION_MODE=mirror\n"
        )
        with _TmpEnv(live_text=live) as s:
            s.load()
            # Per-env keys land in per_env[live]
            self.assertEqual(s.per_env["live"]["TRADOVATE_USERNAME"], "u_live")
            self.assertEqual(s.per_env["live"]["PROXY_LISTEN_PORT"], "8080")
            self.assertEqual(s.per_env["live"]["IBKR_WATCHED_ACCOUNTS"], "U7713037")
            # Demo is empty (no file)
            self.assertEqual(s.per_env["demo"], {})
            # Shared keys land in shared
            self.assertEqual(s.shared["PROXY_LISTEN_HOST"], "127.0.0.1")
            self.assertEqual(s.shared["TRADOVATE_APP_ID"], "TradeSynchronizer")

    def test_load_both_files(self):
        live = (
            "TRADOVATE_USERNAME=u_live\n"
            "PROXY_LISTEN_PORT=8080\n"
            "PROXY_LISTEN_HOST=127.0.0.1\n"
            "REPLICATION_MODE=mirror\n"
        )
        demo = (
            "TRADOVATE_USERNAME=u_demo\n"
            "PROXY_LISTEN_PORT=8081\n"
            "PROXY_LISTEN_HOST=127.0.0.1\n"
            "REPLICATION_MODE=mirror\n"
        )
        with _TmpEnv(live_text=live, demo_text=demo) as s:
            s.load()
            self.assertEqual(s.per_env["live"]["TRADOVATE_USERNAME"], "u_live")
            self.assertEqual(s.per_env["live"]["PROXY_LISTEN_PORT"], "8080")
            self.assertEqual(s.per_env["demo"]["TRADOVATE_USERNAME"], "u_demo")
            self.assertEqual(s.per_env["demo"]["PROXY_LISTEN_PORT"], "8081")
            # Shared values match in both files → no conflict
            self.assertEqual(s.shared["PROXY_LISTEN_HOST"], "127.0.0.1")
            self.assertEqual(s.shared["REPLICATION_MODE"], "mirror")

    def test_shared_conflict_resolves_live_first(self):
        """If the two files disagree on a shared key (hand-edited
        inconsistently), the live file's value wins because we load
        live first."""
        live = "PROXY_LISTEN_HOST=10.0.0.1\nLOG_LEVEL=DEBUG\n"
        demo = "PROXY_LISTEN_HOST=192.168.1.1\nLOG_LEVEL=ERROR\n"
        with _TmpEnv(live_text=live, demo_text=demo) as s:
            s.load()
            self.assertEqual(s.shared["PROXY_LISTEN_HOST"], "10.0.0.1")
            self.assertEqual(s.shared["LOG_LEVEL"], "DEBUG")

    def test_in_file_tradovate_environment_is_ignored(self):
        """TRADOVATE_ENVIRONMENT lines in the files are informational
        only — the filename determines the env."""
        live = "TRADOVATE_ENVIRONMENT=demo\nTRADOVATE_USERNAME=u_live\n"
        with _TmpEnv(live_text=live) as s:
            s.load()
            # u_live still lands in per_env[live] because file is .env.live
            self.assertEqual(s.per_env["live"]["TRADOVATE_USERNAME"], "u_live")
            # And TRADOVATE_ENVIRONMENT is not stored anywhere
            self.assertNotIn("TRADOVATE_ENVIRONMENT", s.shared)


class TestEnvStoreGetSet(unittest.TestCase):

    def test_get_env_per_env(self):
        with _TmpEnv() as s:
            s.load()
            s.per_env["live"]["TRADOVATE_USERNAME"] = "live_u"
            s.per_env["demo"]["TRADOVATE_USERNAME"] = "demo_u"
            self.assertEqual(s.get_env("live", "TRADOVATE_USERNAME"), "live_u")
            self.assertEqual(s.get_env("demo", "TRADOVATE_USERNAME"), "demo_u")

    def test_set_env_per_env(self):
        with _TmpEnv() as s:
            s.load()
            s.set_env("live", "TRADOVATE_USERNAME", "live_u")
            s.set_env("demo", "TRADOVATE_USERNAME", "demo_u")
            self.assertEqual(s.per_env["live"]["TRADOVATE_USERNAME"], "live_u")
            self.assertEqual(s.per_env["demo"]["TRADOVATE_USERNAME"], "demo_u")

    def test_shared_key_not_per_env(self):
        with _TmpEnv() as s:
            s.load()
            # PROXY_LISTEN_HOST is shared; PROXY_LISTEN_PORT is per-env.
            s.set_env("live", "PROXY_LISTEN_HOST", "0.0.0.0")
            self.assertEqual(s.shared["PROXY_LISTEN_HOST"], "0.0.0.0")
            self.assertNotIn("PROXY_LISTEN_HOST", s.per_env["live"])
            self.assertNotIn("PROXY_LISTEN_HOST", s.per_env["demo"])


class TestEnvStoreWrite(unittest.TestCase):

    def test_roundtrip_preserves_data(self):
        with _TmpEnv() as s:
            s.load()
            s.per_env["live"] = {
                "TRADOVATE_USERNAME": "u_live",
                "TRADOVATE_PASSWORD": "p_live",
                "TRADOVATE_CID": "cid_live",
                "TRADOVATE_SEC": "sec_live",
                "TRADOVATE_ACCOUNT_ID": "1290252",
                "IBKR_WATCHED_ACCOUNTS": "U7713037",
                "PROXY_LISTEN_PORT": "8080",
            }
            s.per_env["demo"] = {
                "TRADOVATE_USERNAME": "u_demo",
                "TRADOVATE_PASSWORD": "p_demo",
                "TRADOVATE_CID": "cid_demo",
                "TRADOVATE_SEC": "sec_demo",
                "TRADOVATE_ACCOUNT_ID": "",
                "IBKR_WATCHED_ACCOUNTS": "DU9999999",
                "PROXY_LISTEN_PORT": "8081",
            }
            s.shared.update({
                "TRADOVATE_APP_ID": "TradeSynchronizer",
                "TRADOVATE_APP_VERSION": "1.0",
                "PROXY_LISTEN_HOST": "127.0.0.1",
                "REPLICATION_MODE": "mirror",
                "SKIP_PROTECTIVE_STOPS": "true",
                "LOG_LEVEL": "INFO",
                "LOG_FILE": "/tmp/tradesync.log",
            })
            s.write()

            # Both files should exist now
            for env in ENVIRONMENTS:
                self.assertTrue(s.env_paths[env].exists(),
                                f"{env} file not written")

            # Reload from disk and verify everything survived
            s2 = EnvStore(project_root=s.env_paths["live"].parent)
            s2.load()
            self.assertEqual(s2.per_env["live"]["TRADOVATE_USERNAME"], "u_live")
            self.assertEqual(s2.per_env["demo"]["TRADOVATE_USERNAME"], "u_demo")
            self.assertEqual(s2.per_env["live"]["PROXY_LISTEN_PORT"], "8080")
            self.assertEqual(s2.per_env["demo"]["PROXY_LISTEN_PORT"], "8081")
            self.assertEqual(s2.per_env["live"]["IBKR_WATCHED_ACCOUNTS"], "U7713037")
            self.assertEqual(s2.per_env["demo"]["IBKR_WATCHED_ACCOUNTS"], "DU9999999")
            self.assertEqual(s2.shared["PROXY_LISTEN_HOST"], "127.0.0.1")
            self.assertEqual(s2.shared["REPLICATION_MODE"], "mirror")

    def test_write_shared_settings_appear_in_both_files(self):
        with _TmpEnv() as s:
            s.load()
            s.shared.update({
                "PROXY_LISTEN_HOST": "10.0.0.99",
                "REPLICATION_MODE": "market",
                "LOG_LEVEL": "DEBUG",
                "LOG_FILE": "/tmp/x.log",
            })
            s.write()
            for env in ENVIRONMENTS:
                content = s.env_paths[env].read_text()
                self.assertIn("PROXY_LISTEN_HOST=10.0.0.99", content)
                self.assertIn("REPLICATION_MODE=market", content)
                self.assertIn("LOG_LEVEL=DEBUG", content)
                self.assertIn("LOG_FILE=/tmp/x.log", content)

    def test_write_uses_per_env_default_port(self):
        """Empty per_env[env]['PROXY_LISTEN_PORT'] falls back to the
        smart default (8080 for live, 8081 for demo)."""
        with _TmpEnv() as s:
            s.load()
            s.write()
            live_content = s.env_paths["live"].read_text()
            demo_content = s.env_paths["demo"].read_text()
            self.assertIn(
                f"PROXY_LISTEN_PORT={PER_ENV_DEFAULTS['PROXY_LISTEN_PORT']['live']}",
                live_content,
            )
            self.assertIn(
                f"PROXY_LISTEN_PORT={PER_ENV_DEFAULTS['PROXY_LISTEN_PORT']['demo']}",
                demo_content,
            )

    def test_write_does_not_emit_suffixed_keys(self):
        """No more _LIVE / _DEMO suffixes anywhere — each file uses
        unsuffixed keys."""
        with _TmpEnv() as s:
            s.load()
            s.per_env["live"]["TRADOVATE_USERNAME"] = "u_live"
            s.per_env["demo"]["TRADOVATE_USERNAME"] = "u_demo"
            s.write()
            for env in ENVIRONMENTS:
                content = s.env_paths[env].read_text()
                self.assertNotIn("_LIVE=", content)
                self.assertNotIn("_DEMO=", content)


class TestSnapshot(unittest.TestCase):

    def test_snapshot_changes_on_any_edit(self):
        with _TmpEnv() as s:
            s.load()
            before = s.snapshot()
            s.set_env("live", "TRADOVATE_USERNAME", "foo")
            after = s.snapshot()
            self.assertNotEqual(before, after)

    def test_snapshot_distinguishes_envs(self):
        with _TmpEnv() as s:
            s.load()
            s.set_env("live", "TRADOVATE_USERNAME", "x")
            snap1 = s.snapshot()
            s.set_env("demo", "TRADOVATE_USERNAME", "x")
            snap2 = s.snapshot()
            # Same value in different envs → still a different snapshot
            self.assertNotEqual(snap1, snap2)


if __name__ == "__main__":
    unittest.main()
