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
    ENVIRONMENTS, GENERAL_FIELDS, PER_ENV_DEFAULTS, PER_ENV_FIELDS,
    PER_ENV_KEYS, SHARED, EnvStore,
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


class TestFieldsAreActuallySerialized(unittest.TestCase):
    """Regression guard: every key declared in GENERAL_FIELDS /
    PER_ENV_FIELDS must round-trip through _build_shared /
    _build_env. Forgetting to add a new field to the writer is
    what caused the 'Unsaved changes' alert to fire on every
    engine start — the widget would set a default value into
    store.shared that the writer then silently dropped, so the
    next load would never see it and dirty-detection ran in a
    loop. This test catches that class of bug at write time."""

    @staticmethod
    def _user_keys(fields) -> set[str]:
        """The actual settable keys (skip section markers)."""
        return {f[0] for f in fields if f[0] != "__section__"}

    def test_every_general_field_is_in_build_shared(self):
        with _TmpEnv() as s:
            # Seed the store with non-empty placeholders so the
            # writer can't get away with conditional omission.
            for key in self._user_keys(GENERAL_FIELDS):
                s.shared[key] = "test-value-" + key.lower()

            written_text = "\n".join(s._build_shared())
            missing = [k for k in self._user_keys(GENERAL_FIELDS)
                       if f"{k}=" not in written_text]
            self.assertEqual(missing, [],
                f"GENERAL_FIELDS keys missing from _build_shared(): "
                f"{missing}. When you add a field to GENERAL_FIELDS, "
                f"also add a `KEY={{s.get('KEY', default)}}` line to "
                f"EnvStore._build_shared() — otherwise the GUI's "
                f"dirty-tracking will see a permanent diff between "
                f"store.shared (which gets the widget default) and "
                f"the on-disk file (which doesn't have the line), "
                f"and the 'Unsaved changes' dialog will fire on "
                f"every engine start.")

    def test_every_per_env_field_is_in_build_env(self):
        with _TmpEnv() as s:
            for env in ENVIRONMENTS:
                for key in self._user_keys(PER_ENV_FIELDS):
                    s.per_env[env][key] = "test-value-" + key.lower()
                written_text = "\n".join(s._build_env(env))
                missing = [k for k in self._user_keys(PER_ENV_FIELDS)
                           if f"{k}=" not in written_text]
                self.assertEqual(missing, [],
                    f"PER_ENV_FIELDS keys missing from "
                    f"_build_env({env!r}): {missing}.")

    def test_round_trip_preserves_widget_defaults(self):
        """The full GUI flow simulated at the store level:
          1. Empty .env on disk → load → shared is empty
          2. Widget defaults flushed into shared
          3. Snapshot taken
          4. Save (writes shared via _build_shared)
          5. Re-load from the freshly written file
          6. After a re-flush from widget defaults, snapshot should
             match step 3 — anything missed by the writer would
             surface as a permanent diff here."""
        with _TmpEnv() as s:
            s.load()
            # Step 2: simulate _flush_widgets_to_store loading
            # widget defaults from GENERAL_FIELDS.
            for f in GENERAL_FIELDS:
                if f[0] == "__section__":
                    continue
                key, _, kind, default, _ = f
                s.shared[key] = default if default is not None else ""

            snapshot_before = s.snapshot_per_file()

            # Step 4: write, then re-read fresh from a NEW EnvStore
            # rooted at the same temp dir.
            s.write(only={SHARED})
            s2 = EnvStore(project_root=s.shared_path.parent)
            s2.load()

            # Step 6: re-flush widget defaults — for any key the
            # writer DROPPED, this re-injection mimics exactly what
            # the GUI's first _flush_widgets_to_store does and
            # would cause the dirty loop.
            for f in GENERAL_FIELDS:
                if f[0] == "__section__":
                    continue
                key, _, kind, default, _ = f
                if key not in s2.shared:
                    s2.shared[key] = default if default is not None else ""

            snapshot_after = s2.snapshot_per_file()
            self.assertEqual(
                snapshot_before[SHARED], snapshot_after[SHARED],
                "shared snapshot drifted across save+reload — "
                "some GENERAL_FIELDS key isn't being written by "
                "_build_shared(), which causes the 'Unsaved "
                "changes' loop on every engine start.")


if __name__ == "__main__":
    unittest.main()
