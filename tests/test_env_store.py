"""
Unit tests for tradesync.ui.app.EnvStore — the environment-aware
.env reader/writer that backs the GUI's Settings tab.

Run from the repo root:

    python3 -m unittest tests.test_env_store
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tradesync.ui.app import ENVIRONMENTS, PER_ENV_KEYS, EnvStore


class _TmpEnv:
    """Context manager: scratch dir with .env and (optional) .env.example."""

    def __init__(self, env_text: str | None = None,
                 template_text: str | None = None):
        self.env_text = env_text
        self.template_text = template_text

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        env = root / ".env"
        tpl = root / ".env.example"
        if self.env_text is not None:
            env.write_text(self.env_text)
        if self.template_text is not None:
            tpl.write_text(self.template_text)
        return EnvStore(env_path=env, template_path=tpl)

    def __exit__(self, *exc):
        self._tmp.cleanup()


class TestEnvStoreLoad(unittest.TestCase):

    def test_load_missing_file_uses_template(self):
        tpl = "TRADOVATE_APP_ID=FromTemplate\nTRADOVATE_ENVIRONMENT=demo\n"
        with _TmpEnv(env_text=None, template_text=tpl) as s:
            s.load()
            self.assertEqual(s.shared["TRADOVATE_APP_ID"], "FromTemplate")
            self.assertEqual(s.active_env, "demo")

    def test_load_missing_file_and_no_template_is_clean(self):
        with _TmpEnv(env_text=None, template_text=None) as s:
            s.load()
            self.assertEqual(s.shared, {})
            for env in ENVIRONMENTS:
                self.assertEqual(s.per_env[env], {})
            self.assertEqual(s.active_env, "demo")

    def test_load_legacy_unsuffixed_assigns_to_active_env(self):
        legacy = (
            "TRADOVATE_USERNAME=u_live\n"
            "TRADOVATE_PASSWORD=p_live\n"
            "TRADOVATE_CID=cid_live\n"
            "TRADOVATE_SEC=sec_live\n"
            "TRADOVATE_ENVIRONMENT=live\n"
            "TRADOVATE_ACCOUNT_ID=1290252\n"
            "IBKR_WATCHED_ACCOUNTS=U7713037\n"
            "TRADOVATE_APP_ID=TradeSynchronizer\n"
            "PROXY_LISTEN_PORT=8080\n"
        )
        with _TmpEnv(env_text=legacy) as s:
            s.load()
            self.assertEqual(s.active_env, "live")
            # Per-env values land in the active env
            self.assertEqual(s.per_env["live"]["TRADOVATE_USERNAME"], "u_live")
            self.assertEqual(s.per_env["live"]["IBKR_WATCHED_ACCOUNTS"], "U7713037")
            self.assertEqual(s.per_env["live"]["TRADOVATE_ACCOUNT_ID"], "1290252")
            # The other env is empty (no demo data in legacy file)
            self.assertEqual(s.per_env["demo"], {})
            # Shared is unaffected
            self.assertEqual(s.shared["TRADOVATE_APP_ID"], "TradeSynchronizer")
            self.assertEqual(s.shared["PROXY_LISTEN_PORT"], "8080")

    def test_load_new_format(self):
        new_format = (
            "TRADOVATE_ENVIRONMENT=demo\n"
            "TRADOVATE_USERNAME_LIVE=u_live\n"
            "TRADOVATE_USERNAME_DEMO=u_demo\n"
            "TRADOVATE_PASSWORD_LIVE=p_live\n"
            "TRADOVATE_PASSWORD_DEMO=p_demo\n"
            "TRADOVATE_CID_LIVE=cid_live\n"
            "TRADOVATE_CID_DEMO=cid_demo\n"
            "TRADOVATE_SEC_LIVE=sec_live\n"
            "TRADOVATE_SEC_DEMO=sec_demo\n"
            "IBKR_WATCHED_ACCOUNTS_LIVE=U7713037\n"
            "IBKR_WATCHED_ACCOUNTS_DEMO=DU1234567\n"
            "PROXY_LISTEN_HOST=127.0.0.1\n"
        )
        with _TmpEnv(env_text=new_format) as s:
            s.load()
            self.assertEqual(s.active_env, "demo")
            self.assertEqual(s.per_env["live"]["TRADOVATE_USERNAME"], "u_live")
            self.assertEqual(s.per_env["demo"]["TRADOVATE_USERNAME"], "u_demo")
            self.assertEqual(s.per_env["live"]["IBKR_WATCHED_ACCOUNTS"], "U7713037")
            self.assertEqual(s.per_env["demo"]["IBKR_WATCHED_ACCOUNTS"], "DU1234567")

    def test_suffixed_wins_over_legacy_for_same_key(self):
        mixed = (
            "TRADOVATE_ENVIRONMENT=live\n"
            "TRADOVATE_USERNAME=u_legacy\n"
            "TRADOVATE_USERNAME_LIVE=u_new_live\n"
        )
        with _TmpEnv(env_text=mixed) as s:
            s.load()
            # Suffixed wins for live; legacy fills only gaps in active env
            self.assertEqual(s.per_env["live"]["TRADOVATE_USERNAME"], "u_new_live")

    def test_invalid_active_env_defaults_to_demo(self):
        with _TmpEnv(env_text="TRADOVATE_ENVIRONMENT=banana\n") as s:
            s.load()
            self.assertEqual(s.active_env, "demo")


class TestEnvStoreGetSet(unittest.TestCase):

    def test_get_for_per_env_returns_active_env_value(self):
        with _TmpEnv(env_text="") as s:
            s.load()
            s.per_env["live"]["TRADOVATE_USERNAME"] = "live_u"
            s.per_env["demo"]["TRADOVATE_USERNAME"] = "demo_u"
            s.active_env = "live"
            self.assertEqual(s.get("TRADOVATE_USERNAME"), "live_u")
            s.active_env = "demo"
            self.assertEqual(s.get("TRADOVATE_USERNAME"), "demo_u")

    def test_set_writes_to_active_env_only(self):
        with _TmpEnv(env_text="") as s:
            s.load()
            s.active_env = "live"
            s.set("TRADOVATE_USERNAME", "live_u")
            s.active_env = "demo"
            s.set("TRADOVATE_USERNAME", "demo_u")
            self.assertEqual(s.per_env["live"]["TRADOVATE_USERNAME"], "live_u")
            self.assertEqual(s.per_env["demo"]["TRADOVATE_USERNAME"], "demo_u")

    def test_set_environment_switches_active(self):
        with _TmpEnv(env_text="") as s:
            s.load()
            s.set("TRADOVATE_ENVIRONMENT", "live")
            self.assertEqual(s.active_env, "live")
            s.set("TRADOVATE_ENVIRONMENT", "DEMO")  # case insensitive
            self.assertEqual(s.active_env, "demo")
            s.set("TRADOVATE_ENVIRONMENT", "banana")  # invalid → no-op
            self.assertEqual(s.active_env, "demo")

    def test_shared_key_not_per_env(self):
        with _TmpEnv(env_text="") as s:
            s.load()
            s.set("PROXY_LISTEN_PORT", "9000")
            self.assertEqual(s.shared["PROXY_LISTEN_PORT"], "9000")
            self.assertNotIn("PROXY_LISTEN_PORT", s.per_env["live"])
            self.assertNotIn("PROXY_LISTEN_PORT", s.per_env["demo"])


class TestEnvStoreWrite(unittest.TestCase):

    def test_roundtrip_preserves_data(self):
        with _TmpEnv(env_text="") as s:
            s.load()
            s.active_env = "live"
            s.per_env["live"] = {
                "TRADOVATE_USERNAME": "u_live",
                "TRADOVATE_PASSWORD": "p_live",
                "TRADOVATE_CID": "cid_live",
                "TRADOVATE_SEC": "sec_live",
                "TRADOVATE_ACCOUNT_ID": "1290252",
                "IBKR_WATCHED_ACCOUNTS": "U7713037",
            }
            s.per_env["demo"] = {
                "TRADOVATE_USERNAME": "u_demo",
                "TRADOVATE_PASSWORD": "p_demo",
                "TRADOVATE_CID": "cid_demo",
                "TRADOVATE_SEC": "sec_demo",
                "TRADOVATE_ACCOUNT_ID": "",
                "IBKR_WATCHED_ACCOUNTS": "DU9999999",
            }
            s.shared.update({
                "TRADOVATE_APP_ID": "TradeSynchronizer",
                "TRADOVATE_APP_VERSION": "1.0",
                "PROXY_LISTEN_HOST": "127.0.0.1",
                "PROXY_LISTEN_PORT": "8080",
                "REPLICATION_MODE": "mirror",
                "SKIP_PROTECTIVE_STOPS": "true",
                "LOG_LEVEL": "INFO",
                "LOG_FILE": "/tmp/tradesync.log",
            })
            s.write()

            # Reload from disk and verify everything survived
            s2 = EnvStore(env_path=s.env_path, template_path=s.template_path)
            s2.load()
            self.assertEqual(s2.active_env, "live")
            self.assertEqual(s2.per_env["live"]["TRADOVATE_USERNAME"], "u_live")
            self.assertEqual(s2.per_env["demo"]["TRADOVATE_USERNAME"], "u_demo")
            self.assertEqual(s2.per_env["live"]["IBKR_WATCHED_ACCOUNTS"], "U7713037")
            self.assertEqual(s2.per_env["demo"]["IBKR_WATCHED_ACCOUNTS"], "DU9999999")
            self.assertEqual(s2.shared["PROXY_LISTEN_HOST"], "127.0.0.1")
            self.assertEqual(s2.shared["REPLICATION_MODE"], "mirror")

    def test_legacy_to_new_migration(self):
        """Load a legacy .env, write, reload — data preserved + suffixed."""
        legacy = (
            "TRADOVATE_ENVIRONMENT=live\n"
            "TRADOVATE_USERNAME=amoreyda1977\n"
            "TRADOVATE_PASSWORD=secret\n"
            "TRADOVATE_CID=xxx\n"
            "TRADOVATE_SEC=xxx\n"
            "TRADOVATE_ACCOUNT_ID=1290252\n"
            "IBKR_WATCHED_ACCOUNTS=U7713037\n"
            "TRADOVATE_APP_ID=TradeSynchronizer\n"
            "TRADOVATE_APP_VERSION=1.0\n"
            "PROXY_LISTEN_HOST=127.0.0.1\n"
            "PROXY_LISTEN_PORT=8080\n"
            "REPLICATION_MODE=mirror\n"
            "SKIP_PROTECTIVE_STOPS=true\n"
            "LOG_LEVEL=INFO\n"
            "LOG_FILE=/tmp/tradesync.log\n"
        )
        with _TmpEnv(env_text=legacy) as s:
            s.load()
            s.write()
            on_disk = s.env_path.read_text()

            # New format markers should be present
            self.assertIn("TRADOVATE_USERNAME_LIVE=amoreyda1977", on_disk)
            self.assertIn("TRADOVATE_USERNAME_DEMO=", on_disk)
            self.assertIn("IBKR_WATCHED_ACCOUNTS_LIVE=U7713037", on_disk)
            # Legacy unsuffixed lines should NOT survive
            for legacy_key in (
                "TRADOVATE_USERNAME=", "TRADOVATE_PASSWORD=",
                "IBKR_WATCHED_ACCOUNTS=",
            ):
                self.assertNotIn("\n" + legacy_key, on_disk)

            s2 = EnvStore(env_path=s.env_path, template_path=s.template_path)
            s2.load()
            self.assertEqual(s2.per_env["live"]["TRADOVATE_USERNAME"], "amoreyda1977")
            self.assertEqual(s2.per_env["live"]["IBKR_WATCHED_ACCOUNTS"], "U7713037")
            # Demo keys are emitted with empty strings on write, which is
            # how they come back on reload. get('') is equivalent to
            # missing for UI purposes.
            for k in PER_ENV_KEYS:
                self.assertEqual(s2.per_env["demo"].get(k, ""), "")


class TestSnapshot(unittest.TestCase):

    def test_snapshot_changes_on_any_edit(self):
        with _TmpEnv(env_text="") as s:
            s.load()
            before = s.snapshot()
            s.active_env = "live"
            s.set("TRADOVATE_USERNAME", "foo")
            after = s.snapshot()
            self.assertNotEqual(before, after)

    def test_snapshot_includes_active_env(self):
        with _TmpEnv(env_text="") as s:
            s.load()
            s.active_env = "live"
            snap_live = s.snapshot()
            s.active_env = "demo"
            snap_demo = s.snapshot()
            self.assertNotEqual(snap_live, snap_demo)


if __name__ == "__main__":
    unittest.main()
