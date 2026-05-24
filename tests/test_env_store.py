"""
Unit tests for tradesync.ui.app.EnvStore — the three-file dotenv
manager that backs the GUI's Settings tabs.

Run from the repo root:

    python3 -m unittest tests.test_env_store
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from tradesync.ui.app import (
    ENVIRONMENTS, PER_ENV_DEFAULTS, PER_ENV_KEYS, SHARED, EnvStore,
)


class _TmpEnv:
    """Context manager: scratch project_root with optional .env,
    .env.live and .env.demo files."""

    def __init__(self, shared_text: str | None = None,
                 live_text: str | None = None,
                 demo_text: str | None = None):
        self.shared_text = shared_text
        self.live_text = live_text
        self.demo_text = demo_text

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        if self.shared_text is not None:
            (root / ".env").write_text(self.shared_text)
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

    def test_load_three_files(self):
        shared = (
            "TRADOVATE_APP_ID=TradeSynchronizer\n"
            "PROXY_LISTEN_HOST=127.0.0.1\n"
            "REPLICATION_MODE=mirror\n"
            "LOG_LEVEL=INFO\n"
        )
        live = (
            "TRADOVATE_USERNAME=u_live\n"
            "TRADOVATE_PASSWORD=p_live\n"
            "PROXY_LISTEN_PORT=8080\n"
            "IBKR_WATCHED_ACCOUNTS=U7713037\n"
        )
        demo = (
            "TRADOVATE_USERNAME=u_demo\n"
            "PROXY_LISTEN_PORT=8081\n"
        )
        with _TmpEnv(shared_text=shared, live_text=live, demo_text=demo) as s:
            s.load()
            # Shared keys land only in shared
            self.assertEqual(s.shared["PROXY_LISTEN_HOST"], "127.0.0.1")
            self.assertEqual(s.shared["TRADOVATE_APP_ID"], "TradeSynchronizer")
            self.assertEqual(s.shared["LOG_LEVEL"], "INFO")
            # Per-env keys land only in per_env[<env>]
            self.assertEqual(s.per_env["live"]["TRADOVATE_USERNAME"], "u_live")
            self.assertEqual(s.per_env["live"]["PROXY_LISTEN_PORT"], "8080")
            self.assertEqual(s.per_env["live"]["IBKR_WATCHED_ACCOUNTS"], "U7713037")
            self.assertEqual(s.per_env["demo"]["TRADOVATE_USERNAME"], "u_demo")
            self.assertEqual(s.per_env["demo"]["PROXY_LISTEN_PORT"], "8081")
            # No cross-contamination
            self.assertNotIn("PROXY_LISTEN_PORT", s.shared)
            self.assertNotIn("TRADOVATE_APP_ID", s.per_env["live"])

    def test_legacy_shared_in_env_file_is_migrated(self):
        """If the user is upgrading from the two-file model and their
        .env.live still has shared keys duplicated, load() picks them
        up so the next Save can move them to .env."""
        live = (
            "TRADOVATE_USERNAME=u_live\n"
            "PROXY_LISTEN_PORT=8080\n"
            # These were duplicated in the old design:
            "PROXY_LISTEN_HOST=10.0.0.5\n"
            "REPLICATION_MODE=market\n"
        )
        with _TmpEnv(live_text=live) as s:
            s.load()
            self.assertEqual(s.per_env["live"]["TRADOVATE_USERNAME"], "u_live")
            # Stray shared keys migrate to self.shared
            self.assertEqual(s.shared["PROXY_LISTEN_HOST"], "10.0.0.5")
            self.assertEqual(s.shared["REPLICATION_MODE"], "market")

    def test_shared_file_wins_over_legacy_stray(self):
        """If .env already has the canonical shared value, a stray
        shared key in an env file does NOT override it."""
        shared = "PROXY_LISTEN_HOST=127.0.0.1\n"
        live = "PROXY_LISTEN_HOST=999.999.999.999\n"
        with _TmpEnv(shared_text=shared, live_text=live) as s:
            s.load()
            self.assertEqual(s.shared["PROXY_LISTEN_HOST"], "127.0.0.1")

    def test_in_file_tradovate_environment_is_ignored(self):
        live = "TRADOVATE_ENVIRONMENT=demo\nTRADOVATE_USERNAME=u_live\n"
        with _TmpEnv(live_text=live) as s:
            s.load()
            self.assertEqual(s.per_env["live"]["TRADOVATE_USERNAME"], "u_live")
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

    def test_set_shared_via_env_helper(self):
        with _TmpEnv() as s:
            s.load()
            s.set_env("live", "PROXY_LISTEN_HOST", "0.0.0.0")
            # PROXY_LISTEN_HOST is shared, so even though we passed
            # env='live' it lands in self.shared
            self.assertEqual(s.shared["PROXY_LISTEN_HOST"], "0.0.0.0")
            self.assertNotIn("PROXY_LISTEN_HOST", s.per_env["live"])


class TestEnvStoreWrite(unittest.TestCase):

    def _populate(self, s: EnvStore):
        # Note: TRADOVATE_CID/_SEC are intentionally absent — they
        # moved to tradesync/_app_credentials.py at app level.
        s.per_env["live"] = {
            "TRADOVATE_USERNAME": "u_live",
            "TRADOVATE_PASSWORD": "p_live",
            "TRADOVATE_ACCOUNT_ID": "1290252",
            "IBKR_WATCHED_ACCOUNTS": "U7713037",
            "PROXY_LISTEN_PORT": "8080",
        }
        s.per_env["demo"] = {
            "TRADOVATE_USERNAME": "u_demo",
            "TRADOVATE_PASSWORD": "p_demo",
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

    def test_roundtrip_preserves_data(self):
        with _TmpEnv() as s:
            s.load()
            self._populate(s)
            written = s.write()
            # All three files were written
            self.assertEqual(len(written), 3)
            # Reload from disk and verify everything survived
            s2 = EnvStore(project_root=s.shared_path.parent)
            s2.load()
            self.assertEqual(s2.per_env["live"]["TRADOVATE_USERNAME"], "u_live")
            self.assertEqual(s2.per_env["demo"]["TRADOVATE_USERNAME"], "u_demo")
            self.assertEqual(s2.per_env["live"]["PROXY_LISTEN_PORT"], "8080")
            self.assertEqual(s2.per_env["demo"]["PROXY_LISTEN_PORT"], "8081")
            self.assertEqual(s2.shared["PROXY_LISTEN_HOST"], "127.0.0.1")
            self.assertEqual(s2.shared["REPLICATION_MODE"], "mirror")
            self.assertEqual(s2.shared["TRADOVATE_APP_ID"], "TradeSynchronizer")

    def test_targeted_write_touches_only_named_files(self):
        """The critical independence test: writing only 'demo' must
        NOT alter .env.live's content on disk."""
        with _TmpEnv() as s:
            s.load()
            self._populate(s)
            s.write()  # initial state
            live_before = s.env_paths["live"].read_text()
            shared_before = s.shared_path.read_text()

            # Modify live IN MEMORY but write only demo
            s.per_env["live"]["TRADOVATE_USERNAME"] = "MODIFIED_BUT_NOT_SAVED"
            s.per_env["demo"]["TRADOVATE_USERNAME"] = "modified_demo"
            written = s.write(only={"demo"})

            self.assertEqual([p.name for p in written], [".env.demo"])
            # live file untouched
            self.assertEqual(s.env_paths["live"].read_text(), live_before)
            # shared file untouched
            self.assertEqual(s.shared_path.read_text(), shared_before)
            # demo file actually has the new value
            self.assertIn("TRADOVATE_USERNAME=modified_demo",
                          s.env_paths["demo"].read_text())

    def test_targeted_write_shared_only(self):
        with _TmpEnv() as s:
            s.load()
            self._populate(s)
            s.write()
            live_before = s.env_paths["live"].read_text()
            demo_before = s.env_paths["demo"].read_text()

            s.shared["REPLICATION_MODE"] = "market"
            written = s.write(only={SHARED})

            self.assertEqual([p.name for p in written], [".env"])
            self.assertEqual(s.env_paths["live"].read_text(), live_before)
            self.assertEqual(s.env_paths["demo"].read_text(), demo_before)
            self.assertIn("REPLICATION_MODE=market",
                          s.shared_path.read_text())

    def test_per_env_file_has_no_shared_keys(self):
        """Each env file emits ONLY its per-env keys (plus a comment
        header). Shared keys never appear in env files."""
        with _TmpEnv() as s:
            s.load()
            self._populate(s)
            s.write()
            for env in ENVIRONMENTS:
                content = s.env_paths[env].read_text()
                # Shared keys MUST NOT appear
                self.assertNotIn("PROXY_LISTEN_HOST=", content)
                self.assertNotIn("REPLICATION_MODE=", content)
                self.assertNotIn("LOG_LEVEL=", content)
                self.assertNotIn("TRADOVATE_APP_ID=", content)
                # Per-env keys MUST appear
                self.assertIn("TRADOVATE_USERNAME=", content)
                self.assertIn("PROXY_LISTEN_PORT=", content)

    def test_shared_file_has_no_per_env_keys(self):
        with _TmpEnv() as s:
            s.load()
            self._populate(s)
            s.write()
            content = s.shared_path.read_text()
            for k in PER_ENV_KEYS:
                self.assertNotIn(f"{k}=", content,
                                 f"per-env key {k!r} leaked into .env")
            # And shared keys are there
            self.assertIn("PROXY_LISTEN_HOST=", content)
            self.assertIn("REPLICATION_MODE=", content)

    def test_write_uses_per_env_default_port_when_empty(self):
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


class TestSnapshotPerFile(unittest.TestCase):

    def test_snapshot_per_file_has_three_buckets(self):
        with _TmpEnv() as s:
            s.load()
            snap = s.snapshot_per_file()
            self.assertEqual(set(snap.keys()), {SHARED, "live", "demo"})

    def test_per_file_snapshot_isolates_changes(self):
        """Editing only the demo bucket changes the demo snapshot
        slot but leaves the live and shared slots untouched — this
        is what powers the dirty-file detection in the GUI."""
        with _TmpEnv() as s:
            s.load()
            before = s.snapshot_per_file()
            s.set_env("demo", "TRADOVATE_USERNAME", "x")
            after = s.snapshot_per_file()
            self.assertNotEqual(before["demo"], after["demo"])
            self.assertEqual(before["live"], after["live"])
            self.assertEqual(before[SHARED], after[SHARED])


if __name__ == "__main__":
    unittest.main()
