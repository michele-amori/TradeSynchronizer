"""
Unit tests for tradesync.notify — the osascript-based desktop
notification wrapper.

Run from the repo root:

    python3 -m unittest tests.test_notify
"""

from __future__ import annotations

import unittest
from unittest.mock import patch, MagicMock

from tradesync import notify as notify_mod


class TestNotifyOnNonMacOS(unittest.TestCase):
    """On non-macOS hosts the function is a no-op (returns False)
    and must NOT shell out — there's no osascript to call."""

    def test_returns_false_on_linux(self):
        with patch("platform.system", return_value="Linux"), \
             patch("subprocess.Popen") as mock_popen:
            self.assertFalse(notify_mod.notify("t", "m"))
            mock_popen.assert_not_called()


class TestNotifyOnMacOS(unittest.TestCase):

    def _patched(self, popen_side_effect=None):
        popen_mock = MagicMock()
        if popen_side_effect is not None:
            popen_mock.side_effect = popen_side_effect
        return (
            patch("platform.system", return_value="Darwin"),
            patch("subprocess.Popen", popen_mock),
        )

    def test_invokes_osascript_with_title_and_message(self):
        platform_p, popen_p = self._patched()
        with platform_p, popen_p as mock_popen:
            self.assertTrue(notify_mod.notify(
                title="LIVE rejection",
                message="insufficient margin",
            ))
            mock_popen.assert_called_once()
            args, _ = mock_popen.call_args
            cmd = args[0]
            self.assertEqual(cmd[0], "osascript")
            self.assertEqual(cmd[1], "-e")
            script = cmd[2]
            self.assertIn("display notification", script)
            self.assertIn("LIVE rejection", script)
            self.assertIn("insufficient margin", script)

    def test_includes_subtitle_when_provided(self):
        platform_p, popen_p = self._patched()
        with platform_p, popen_p as mock_popen:
            notify_mod.notify("t", "m", subtitle="DEMO engine")
            script = mock_popen.call_args[0][0][2]
            self.assertIn('subtitle "DEMO engine"', script)

    def test_escapes_quotes_in_message(self):
        platform_p, popen_p = self._patched()
        with platform_p, popen_p as mock_popen:
            notify_mod.notify(
                title='He said "hi"',
                message='Error: "Insufficient" margin',
            )
            script = mock_popen.call_args[0][0][2]
            # Quotes must be backslash-escaped so AppleScript still
            # parses the one-liner correctly.
            self.assertNotIn('He said "hi"', script)
            self.assertIn('\\"hi\\"', script)
            self.assertIn('\\"Insufficient\\"', script)

    def test_escapes_backslashes(self):
        platform_p, popen_p = self._patched()
        with platform_p, popen_p as mock_popen:
            notify_mod.notify(title="t", message=r"path C:\foo\bar")
            script = mock_popen.call_args[0][0][2]
            # Each backslash gets doubled — AppleScript-safe.
            self.assertIn(r"C:\\foo\\bar", script)

    def test_strips_newlines(self):
        platform_p, popen_p = self._patched()
        with platform_p, popen_p as mock_popen:
            notify_mod.notify(title="t", message="line1\nline2\rline3")
            script = mock_popen.call_args[0][0][2]
            self.assertNotIn("\n", script.split('"')[1])  # message field
            self.assertNotIn("\r", script.split('"')[1])

    def test_truncates_very_long_message(self):
        # Implementation caps message at 300 chars to keep banners
        # readable and avoid AppleScript argument-length surprises.
        platform_p, popen_p = self._patched()
        with platform_p, popen_p as mock_popen:
            notify_mod.notify(title="t", message="x" * 1000)
            script = mock_popen.call_args[0][0][2]
            # The message section between the first pair of quotes
            # should have been truncated.
            msg_inside_quotes = script.split('"', 2)[1]
            self.assertLessEqual(len(msg_inside_quotes), 300)

    def test_popen_failure_returns_false_no_raise(self):
        # If osascript isn't there for some reason, the function MUST
        # NOT raise — it's called from a worker thread that can't
        # afford to be interrupted.
        platform_p, popen_p = self._patched(
            popen_side_effect=FileNotFoundError("osascript")
        )
        with platform_p, popen_p:
            self.assertFalse(notify_mod.notify("t", "m"))


if __name__ == "__main__":
    unittest.main()
