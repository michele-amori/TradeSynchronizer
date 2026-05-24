"""
Tests for tradesync.config.load_app_credentials / has_app_credentials.

These functions resolve the Tradovate APPLICATION cid/sec from
either env vars or the gitignored tradesync/_app_credentials.py
module. They're called once at engine startup and once at GUI
startup (banner check) — keeping them lean and well-behaved is
worth a few targeted tests.

Run from the repo root:

    python3 -m unittest tests.test_app_credentials
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

from tradesync.config import (
    MissingAppCredentialsError,
    has_app_credentials,
    load_app_credentials,
)


class _FakeCreds:
    """Stand-in module object that load_app_credentials' import
    statement will receive. Set the two attributes to drive the
    test scenario."""
    def __init__(self, cid="", sec=""):
        self.APP_CID = cid
        self.APP_SEC = sec


class TestEnvVarOverride(unittest.TestCase):
    """TRADOVATE_CID / TRADOVATE_SEC env vars short-circuit the
    file lookup entirely — useful for CI and for users who'd
    rather not commit a file."""

    def test_env_vars_take_precedence(self):
        with patch.dict(os.environ, {
            "TRADOVATE_CID": "env-cid",
            "TRADOVATE_SEC": "env-sec",
        }, clear=False):
            cid, sec = load_app_credentials()
        self.assertEqual((cid, sec), ("env-cid", "env-sec"))

    def test_env_vars_ignored_when_only_one_is_set(self):
        """Both must be set to short-circuit; otherwise we proceed
        to the file lookup so a half-configured env doesn't silently
        leak whitespace into the auth call."""
        # Stub _app_credentials so the file path also yields a value
        # we can disambiguate from the env one.
        stub = _FakeCreds("file-cid", "file-sec")
        with patch.dict(os.environ, {"TRADOVATE_CID": "env-cid"},
                        clear=False):
            os.environ.pop("TRADOVATE_SEC", None)
            with patch.dict(sys.modules,
                            {"tradesync._app_credentials": stub}):
                cid, sec = load_app_credentials()
        self.assertEqual((cid, sec), ("file-cid", "file-sec"))


class TestModuleLookup(unittest.TestCase):
    """Without env vars, fall back to tradesync._app_credentials."""

    def _clean_env(self):
        # Return a context manager that strips the override vars.
        return patch.dict(
            os.environ, {"TRADOVATE_CID": "", "TRADOVATE_SEC": ""},
            clear=False,
        )

    def test_reads_from_module(self):
        stub = _FakeCreds("file-cid", "file-sec")
        with self._clean_env(), patch.dict(
                sys.modules, {"tradesync._app_credentials": stub}):
            cid, sec = load_app_credentials()
        self.assertEqual((cid, sec), ("file-cid", "file-sec"))

    def test_empty_module_raises_missing(self):
        stub = _FakeCreds("", "")
        with self._clean_env(), patch.dict(
                sys.modules, {"tradesync._app_credentials": stub}):
            with self.assertRaises(MissingAppCredentialsError) as ctx:
                load_app_credentials()
        self.assertIn("APP_CID", str(ctx.exception))

    def test_module_with_only_cid_raises(self):
        stub = _FakeCreds("file-cid", "")
        with self._clean_env(), patch.dict(
                sys.modules, {"tradesync._app_credentials": stub}):
            with self.assertRaises(MissingAppCredentialsError):
                load_app_credentials()

    def test_module_with_only_sec_raises(self):
        stub = _FakeCreds("", "file-sec")
        with self._clean_env(), patch.dict(
                sys.modules, {"tradesync._app_credentials": stub}):
            with self.assertRaises(MissingAppCredentialsError):
                load_app_credentials()

    def test_module_strips_whitespace(self):
        """Trailing newlines from copy-paste shouldn't break auth."""
        stub = _FakeCreds("  file-cid  \n", "\tfile-sec\n")
        with self._clean_env(), patch.dict(
                sys.modules, {"tradesync._app_credentials": stub}):
            cid, sec = load_app_credentials()
        self.assertEqual((cid, sec), ("file-cid", "file-sec"))


class TestHasAppCredentials(unittest.TestCase):
    """has_app_credentials is the non-raising probe used by the GUI
    banner. Must return True/False, never propagate."""

    def test_true_when_load_succeeds(self):
        stub = _FakeCreds("file-cid", "file-sec")
        with patch.dict(os.environ,
                        {"TRADOVATE_CID": "", "TRADOVATE_SEC": ""},
                        clear=False), patch.dict(
                sys.modules, {"tradesync._app_credentials": stub}):
            self.assertTrue(has_app_credentials())

    def test_false_when_module_empty(self):
        stub = _FakeCreds("", "")
        with patch.dict(os.environ,
                        {"TRADOVATE_CID": "", "TRADOVATE_SEC": ""},
                        clear=False), patch.dict(
                sys.modules, {"tradesync._app_credentials": stub}):
            self.assertFalse(has_app_credentials())


if __name__ == "__main__":
    unittest.main()
