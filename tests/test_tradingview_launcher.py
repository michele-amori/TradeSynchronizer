"""
Tests for tradesync.tradingview_launcher — the helper that
auto-attaches TradingView Desktop to the right TradeSynchronizer
proxy port.

Run from the repo root:

    python3 -m unittest tests.test_tradingview_launcher
"""

from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch, MagicMock

from tradesync import tradingview_launcher as tvl


def _cp(returncode=0, stdout="", stderr=""):
    """Quick CompletedProcess factory."""
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr,
    )


# ── installation / process queries ────────────────────────────────────── #

class TestIsInstalled(unittest.TestCase):

    def test_true_when_binary_present(self):
        with patch("os.path.isfile", return_value=True):
            self.assertTrue(tvl.is_installed())

    def test_false_when_binary_absent(self):
        with patch("os.path.isfile", return_value=False):
            self.assertFalse(tvl.is_installed())


class TestIsRunning(unittest.TestCase):

    def test_true_when_pgrep_returncode_0(self):
        with patch("subprocess.run", return_value=_cp(returncode=0)):
            self.assertTrue(tvl.is_running())

    def test_false_when_pgrep_returncode_1(self):
        with patch("subprocess.run", return_value=_cp(returncode=1)):
            self.assertFalse(tvl.is_running())


class TestRunningProxyPort(unittest.TestCase):

    def _ps_output(self, *cmd_lines: str) -> str:
        # Mimic `ps -axww -o command` output: one process per line,
        # full argv on one line.
        return "\n".join(cmd_lines) + "\n"

    def test_returns_port_when_tv_has_correct_flag(self):
        ps = self._ps_output(
            "/Applications/TradingView.app/Contents/MacOS/TradingView "
            "--proxy-server=127.0.0.1:8081 --ignore-certificate-errors",
            # A noise helper process that must NOT confuse us:
            "/Applications/TradingView.app/Contents/Frameworks/"
            "TradingView Helper (GPU).app/Contents/MacOS/TradingView Helper",
        )
        with patch.object(tvl, "is_running", return_value=True), \
             patch("subprocess.run", return_value=_cp(stdout=ps)):
            self.assertEqual(tvl.running_proxy_port(), 8081)

    def test_returns_none_when_tv_not_running(self):
        with patch.object(tvl, "is_running", return_value=False):
            self.assertIsNone(tvl.running_proxy_port())

    def test_returns_none_when_no_proxy_flag(self):
        ps = self._ps_output(
            "/Applications/TradingView.app/Contents/MacOS/TradingView",
        )
        with patch.object(tvl, "is_running", return_value=True), \
             patch("subprocess.run", return_value=_cp(stdout=ps)):
            self.assertIsNone(tvl.running_proxy_port())

    def test_ignores_non_loopback_proxy(self):
        # If TV is somehow attached to a remote proxy (e.g. corporate
        # config), we don't claim to "manage" it.
        ps = self._ps_output(
            "/Applications/TradingView.app/Contents/MacOS/TradingView "
            "--proxy-server=10.0.0.5:3128",
        )
        with patch.object(tvl, "is_running", return_value=True), \
             patch("subprocess.run", return_value=_cp(stdout=ps)):
            self.assertIsNone(tvl.running_proxy_port())

    def test_handles_protocol_prefixed_form(self):
        # --proxy-server=https=127.0.0.1:8081 is also legal Chromium syntax
        ps = self._ps_output(
            "/Applications/TradingView.app/Contents/MacOS/TradingView "
            "--proxy-server=https=127.0.0.1:8081",
        )
        with patch.object(tvl, "is_running", return_value=True), \
             patch("subprocess.run", return_value=_cp(stdout=ps)):
            self.assertEqual(tvl.running_proxy_port(), 8081)

    def test_distinguishes_8080_from_8081(self):
        """The whole point of running_proxy_port returning the port:
        an engine-mismatch scenario (DEMO wanted :8081, TV running
        on LIVE's :8080) needs to be detectable."""
        ps = self._ps_output(
            "/Applications/TradingView.app/Contents/MacOS/TradingView "
            "--proxy-server=127.0.0.1:8080",
        )
        with patch.object(tvl, "is_running", return_value=True), \
             patch("subprocess.run", return_value=_cp(stdout=ps)):
            self.assertEqual(tvl.running_proxy_port(), 8080)


# ── port wait ─────────────────────────────────────────────────────────── #

class TestWaitForPort(unittest.TestCase):

    def test_returns_true_when_connect_succeeds(self):
        sock_mock = MagicMock()
        sock_mock.__enter__ = MagicMock(return_value=sock_mock)
        sock_mock.__exit__ = MagicMock(return_value=False)
        with patch("socket.create_connection", return_value=sock_mock):
            self.assertTrue(tvl._wait_for_port(8081, timeout=0.5))

    def test_returns_false_when_port_never_opens(self):
        with patch("socket.create_connection",
                   side_effect=OSError("refused")), \
             patch("time.sleep"):   # speed up the busy-wait
            self.assertFalse(tvl._wait_for_port(8081, timeout=0.05))


# ── ensure_tradingview_via_proxy state machine ────────────────────────── #

class TestEnsureFlow(unittest.TestCase):

    def test_not_installed_short_circuits(self):
        with patch.object(tvl, "is_installed", return_value=False):
            self.assertEqual(
                tvl.ensure_tradingview_via_proxy(8081),
                "not_installed",
            )

    def test_proxy_not_ready_does_not_touch_tv(self):
        with patch.object(tvl, "is_installed", return_value=True), \
             patch.object(tvl, "_wait_for_port", return_value=False), \
             patch.object(tvl, "_launch") as mock_launch, \
             patch.object(tvl, "_quit_tradingview") as mock_quit:
            self.assertEqual(
                tvl.ensure_tradingview_via_proxy(8081),
                "proxy_not_ready",
            )
            mock_launch.assert_not_called()
            mock_quit.assert_not_called()

    def test_launches_when_tv_not_running(self):
        with patch.object(tvl, "is_installed", return_value=True), \
             patch.object(tvl, "_wait_for_port", return_value=True), \
             patch.object(tvl, "is_running", return_value=False), \
             patch.object(tvl, "running_proxy_port", return_value=None), \
             patch.object(tvl, "_launch") as mock_launch, \
             patch.object(tvl, "_quit_tradingview") as mock_quit:
            self.assertEqual(
                tvl.ensure_tradingview_via_proxy(8081),
                "launched",
            )
            mock_launch.assert_called_once_with(8081)
            mock_quit.assert_not_called()

    def test_noop_when_tv_already_on_correct_port(self):
        with patch.object(tvl, "is_installed", return_value=True), \
             patch.object(tvl, "_wait_for_port", return_value=True), \
             patch.object(tvl, "is_running", return_value=True), \
             patch.object(tvl, "running_proxy_port", return_value=8081), \
             patch.object(tvl, "_launch") as mock_launch, \
             patch.object(tvl, "_quit_tradingview") as mock_quit:
            self.assertEqual(
                tvl.ensure_tradingview_via_proxy(8081),
                "already_proxied",
            )
            mock_launch.assert_not_called()
            mock_quit.assert_not_called()

    def test_restarts_when_tv_on_wrong_port(self):
        """The critical dual-engine case: TV is alive on LIVE's :8080
        but the user just started DEMO (:8081). Must quit + relaunch."""
        with patch.object(tvl, "is_installed", return_value=True), \
             patch.object(tvl, "_wait_for_port", return_value=True), \
             patch.object(tvl, "is_running", return_value=True), \
             patch.object(tvl, "running_proxy_port", return_value=8080), \
             patch.object(tvl, "_launch") as mock_launch, \
             patch.object(tvl, "_quit_tradingview") as mock_quit:
            self.assertEqual(
                tvl.ensure_tradingview_via_proxy(8081),
                "restarted",
            )
            mock_quit.assert_called_once()
            mock_launch.assert_called_once_with(8081)

    def test_restarts_when_tv_running_without_proxy(self):
        with patch.object(tvl, "is_installed", return_value=True), \
             patch.object(tvl, "_wait_for_port", return_value=True), \
             patch.object(tvl, "is_running", return_value=True), \
             patch.object(tvl, "running_proxy_port", return_value=None), \
             patch.object(tvl, "_launch") as mock_launch, \
             patch.object(tvl, "_quit_tradingview") as mock_quit:
            self.assertEqual(
                tvl.ensure_tradingview_via_proxy(8081),
                "restarted",
            )
            mock_quit.assert_called_once()
            mock_launch.assert_called_once_with(8081)

    def test_wait_for_proxy_false_skips_the_wait(self):
        """wait_for_proxy=False is for callers who've already
        verified the port is up (or who don't care)."""
        with patch.object(tvl, "is_installed", return_value=True), \
             patch.object(tvl, "_wait_for_port") as mock_wait, \
             patch.object(tvl, "is_running", return_value=False), \
             patch.object(tvl, "running_proxy_port", return_value=None), \
             patch.object(tvl, "_launch"):
            tvl.ensure_tradingview_via_proxy(8081, wait_for_proxy=False)
            mock_wait.assert_not_called()


if __name__ == "__main__":
    unittest.main()
