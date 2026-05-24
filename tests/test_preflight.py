"""
Tests for tradesync.preflight — the lightweight startup health checks
that warn (never abort) when the host environment is misconfigured.

Run from the repo root:

    python3 -m unittest tests.test_preflight
"""

from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from tradesync import preflight


class TestCheckMitmproxyCaTrusted(unittest.TestCase):

    def test_returns_true_on_non_macos(self):
        with patch("platform.system", return_value="Linux"):
            self.assertTrue(preflight.check_mitmproxy_ca_trusted())

    def test_returns_true_when_security_exits_0(self):
        with patch("platform.system", return_value="Darwin"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=b"cert info", stderr=b"",
            )
            self.assertTrue(preflight.check_mitmproxy_ca_trusted())
            # Confirm we actually queried the System keychain
            args, _ = mock_run.call_args
            cmd = args[0]
            self.assertIn("/Library/Keychains/System.keychain", cmd)
            self.assertIn("mitmproxy", cmd)

    def test_returns_false_when_security_exits_nonzero(self):
        with patch("platform.system", return_value="Darwin"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=44, stdout=b"", stderr=b"not found",
            )
            self.assertFalse(preflight.check_mitmproxy_ca_trusted())

    def test_returns_true_when_security_binary_missing(self):
        # On macOS this shouldn't happen, but be defensive: a missing
        # binary or a hung query must not block engine startup.
        with patch("platform.system", return_value="Darwin"), \
             patch("subprocess.run",
                   side_effect=FileNotFoundError("security")):
            self.assertTrue(preflight.check_mitmproxy_ca_trusted())

    def test_returns_true_when_security_query_times_out(self):
        with patch("platform.system", return_value="Darwin"), \
             patch("subprocess.run",
                   side_effect=subprocess.TimeoutExpired("security", 5)):
            self.assertTrue(preflight.check_mitmproxy_ca_trusted())


class TestRunAllNeverRaises(unittest.TestCase):

    def test_run_all_swallows_check_failures(self):
        with patch("platform.system", return_value="Darwin"), \
             patch("subprocess.run",
                   return_value=subprocess.CompletedProcess(
                       args=[], returncode=1, stdout=b"", stderr=b"")):
            # Whatever happens, run_all() must not raise; this guards
            # bootstrap startup from any pre-flight surprise.
            preflight.run_all()


if __name__ == "__main__":
    unittest.main()
