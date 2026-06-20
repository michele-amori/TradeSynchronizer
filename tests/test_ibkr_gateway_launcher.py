"""
Tests for ibkr_gateway_launcher — opening IB Gateway when an IBKR
follower needs it, WITHOUT ever restarting a running session.

All system touch-points (port probe, pgrep, app discovery, the `open`
command) are injectable, so every branch is covered with no real
process launched and no real Gateway required.
"""

import unittest
from pathlib import Path
from unittest.mock import patch

from tradesync import ibkr_gateway_launcher as gl
from tradesync.ibkr_gateway_launcher import (
    GatewayLaunchStatus,
    ensure_gateway_running,
    find_gateway_app,
)


class TestFindGatewayApp(unittest.TestCase):

    def test_finds_nested_versioned_bundle(self):
        # Simulate IBKR's layout: ~/Applications/IB Gateway 10.45/
        #   IB Gateway 10.45.app  (+ an Uninstaller.app alongside)
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            inst = base / "IB Gateway 10.45"
            (inst / "IB Gateway 10.45.app").mkdir(parents=True)
            (inst / "IB Gateway 10.45 Uninstaller.app").mkdir(parents=True)
            found = find_gateway_app(search_dirs=[base])
            self.assertIsNotNone(found)
            self.assertEqual(found.name, "IB Gateway 10.45.app")

    def test_excludes_uninstaller(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            # ONLY an uninstaller present → must not be returned.
            (base / "IB Gateway 10.45 Uninstaller.app").mkdir(parents=True)
            self.assertIsNone(find_gateway_app(search_dirs=[base]))

    def test_prefers_higher_version(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "IB Gateway 10.45.app").mkdir()
            (base / "IB Gateway 10.46.app").mkdir()
            found = find_gateway_app(search_dirs=[base])
            self.assertEqual(found.name, "IB Gateway 10.46.app")

    def test_none_when_absent(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(find_gateway_app(search_dirs=[Path(tmp)]))


class TestEnsureGatewayRunning(unittest.TestCase):

    def test_api_port_open_means_already_running_untouched(self):
        opened = []
        with patch.object(gl, "is_api_port_open", return_value=True):
            res = ensure_gateway_running(
                api_port=4002,
                find_app=lambda: Path("/should/not/be/used.app"),
                running_check=lambda: True,
                opener=lambda app: opened.append(app))
        self.assertEqual(res.status, GatewayLaunchStatus.ALREADY_RUNNING)
        self.assertEqual(opened, [])   # nothing launched

    def test_running_but_port_closed_is_left_untouched(self):
        # Process up, API not listening yet (mid-login). Must NOT open
        # another instance; tells the user to finish logging in.
        opened = []
        with patch.object(gl, "is_api_port_open", return_value=False):
            res = ensure_gateway_running(
                api_port=4002,
                find_app=lambda: Path("/x.app"),
                running_check=lambda: True,
                opener=lambda app: opened.append(app))
        self.assertEqual(res.status, GatewayLaunchStatus.ALREADY_RUNNING)
        self.assertEqual(opened, [])
        self.assertIn("Finish logging in", res.message)

    def test_not_running_launches(self):
        opened = []
        app = Path("/Apps/IB Gateway 10.45.app")
        with patch.object(gl, "is_api_port_open", return_value=False):
            res = ensure_gateway_running(
                api_port=4002,
                find_app=lambda: app,
                running_check=lambda: False,
                opener=lambda a: opened.append(a))
        self.assertEqual(res.status, GatewayLaunchStatus.LAUNCHED)
        self.assertEqual(opened, [app])
        self.assertEqual(res.app_path, app)

    def test_not_running_and_not_installed(self):
        with patch.object(gl, "is_api_port_open", return_value=False):
            res = ensure_gateway_running(
                api_port=4002,
                find_app=lambda: None,
                running_check=lambda: False,
                opener=lambda a: None)
        self.assertEqual(res.status, GatewayLaunchStatus.NOT_FOUND)

    def test_open_failure_is_reported(self):
        def boom(app):
            raise RuntimeError("open failed")
        with patch.object(gl, "is_api_port_open", return_value=False):
            res = ensure_gateway_running(
                api_port=4002,
                find_app=lambda: Path("/x.app"),
                running_check=lambda: False,
                opener=boom)
        self.assertEqual(res.status, GatewayLaunchStatus.LAUNCH_FAILED)
        self.assertIn("failed", res.message)


class TestDetectionHelpers(unittest.TestCase):

    def test_is_gateway_running_uses_runner(self):
        from tradesync.ibkr_gateway_launcher import is_gateway_running
        self.assertTrue(is_gateway_running(pgrep_runner=lambda pat: True))
        self.assertFalse(is_gateway_running(pgrep_runner=lambda pat: False))

    def test_is_gateway_running_swallows_errors(self):
        from tradesync.ibkr_gateway_launcher import is_gateway_running

        def boom(pat):
            raise OSError("pgrep missing")
        self.assertFalse(is_gateway_running(pgrep_runner=boom))

    def test_is_api_port_open_false_on_closed_port(self):
        from tradesync.ibkr_gateway_launcher import is_api_port_open
        # Port 1 is reserved/closed; connection must fail fast → False.
        self.assertFalse(is_api_port_open("127.0.0.1", 1, timeout=0.2))


if __name__ == "__main__":
    unittest.main()
