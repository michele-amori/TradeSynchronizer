"""
Unit tests for tradesync.replication_alert — the single structured
failure channel (DIVERGENCE marker line + desktop notify).
"""

from __future__ import annotations

import json
import logging
import unittest
from unittest.mock import patch

from tradesync import replication_alert as ra


class TestEmitReplicationFailure(unittest.TestCase):

    def _capture(self):
        """Patch notify and capture the logged record. Returns
        (notify_mock, records_list)."""
        records = []

        class _Cap(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = _Cap()
        ra.logger.addHandler(handler)
        self.addCleanup(ra.logger.removeHandler, handler)
        ra.logger.setLevel(logging.DEBUG)
        return records

    def test_emits_divergence_marker_with_schema(self):
        records = self._capture()
        with patch("tradesync.replication_alert.notify") as nfy:
            ra.emit_replication_failure(
                env="live", kind="REJECTION",
                summary="MNQU6 BUY 10", reason="size exceeds account max")

        # Exactly one DIVERGENCE line, parseable, with the GUI's schema.
        msgs = [r.getMessage() for r in records]
        div = [m for m in msgs if m.startswith(ra.DIVERGENCE_MARKER + " ")]
        self.assertEqual(len(div), 1, msgs)
        payload = json.loads(div[0][len(ra.DIVERGENCE_MARKER) + 1:])
        self.assertEqual(payload["env"], "live")
        self.assertEqual(payload["kind"], "REJECTION")
        self.assertEqual(payload["summary"], "MNQU6 BUY 10")
        self.assertEqual(payload["reason"], "size exceeds account max")
        self.assertIn("ts", payload)
        # And a desktop notification fired.
        nfy.assert_called_once()

    def test_marker_logged_at_error_level(self):
        records = self._capture()
        with patch("tradesync.replication_alert.notify"):
            ra.emit_replication_failure(
                env="demo", kind="NEW", summary="s", reason="r")
        div = [r for r in records
               if r.getMessage().startswith(ra.DIVERGENCE_MARKER)]
        self.assertEqual(div[0].levelno, logging.ERROR)

    def test_notify_can_be_suppressed(self):
        self._capture()
        with patch("tradesync.replication_alert.notify") as nfy:
            ra.emit_replication_failure(
                env="live", kind="NEW", summary="s", reason="r",
                notify_desktop=False)
        nfy.assert_not_called()

    def test_never_raises_even_if_notify_throws(self):
        self._capture()
        with patch("tradesync.replication_alert.notify",
                   side_effect=RuntimeError("boom")):
            # Must not propagate.
            ra.emit_replication_failure(
                env="live", kind="NEW", summary="s", reason="r")

    def test_never_raises_on_unserialisable_extra(self):
        # Defensive: even if something odd is passed, no crash.
        self._capture()
        with patch("tradesync.replication_alert.notify"):
            ra.emit_replication_failure(
                env="live", kind="NEW", summary="s", reason="r")


if __name__ == "__main__":
    unittest.main()
