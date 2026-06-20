"""
The GUI's log-line parser (_maybe_extract_divergence) must pick up the
structured DIVERGENCE {json} marker emitted by replication_alert and
route it to the per-env Sync-health panel — including the REJECTION kind
used for async IBKR follower rejections.

We test the parser without standing up tkinter by creating a bare
instance via object.__new__ and injecting only the attributes the method
touches (_divergences + a stubbed _on_divergence).
"""

from __future__ import annotations

import json
import unittest
import unittest.mock
from unittest.mock import MagicMock

from tradesync.ui.app import TradeSyncApp
from tradesync import replication_alert as ra


def _bare_app():
    app = object.__new__(TradeSyncApp)   # no __init__, no tkinter
    app._divergences = {"live": [], "demo": []}
    app._on_divergence = MagicMock()
    return app


def _alert_line(env, kind, summary, reason):
    """Build the exact log line replication_alert would emit."""
    payload = {"env": env, "ts": 0, "kind": kind,
               "summary": summary, "reason": reason}
    return f"{ra.DIVERGENCE_MARKER} {json.dumps(payload)}\n"


class TestGuiDivergenceParse(unittest.TestCase):

    def test_rejection_line_is_routed(self):
        app = _bare_app()
        line = _alert_line("live", "REJECTION",
                           "P: follower order 12345 rejected",
                           "[201] size exceeds max")
        app._maybe_extract_divergence(line)
        app._on_divergence.assert_called_once()
        env, payload = app._on_divergence.call_args.args
        self.assertEqual(env, "live")
        self.assertEqual(payload["kind"], "REJECTION")
        self.assertIn("12345", payload["summary"])

    def test_non_divergence_line_ignored(self):
        app = _bare_app()
        app._maybe_extract_divergence("just a normal log line\n")
        app._on_divergence.assert_not_called()

    def test_malformed_json_ignored(self):
        app = _bare_app()
        app._maybe_extract_divergence("DIVERGENCE {not valid json\n")
        app._on_divergence.assert_not_called()

    def test_unknown_env_not_routed(self):
        app = _bare_app()
        app._maybe_extract_divergence(_alert_line("staging", "NEW", "s", "r"))
        app._on_divergence.assert_not_called()

    def test_marker_emitted_by_alert_module_matches_parser(self):
        # End-to-end: the line replication_alert actually logs is parseable.
        app = _bare_app()
        import logging

        records = []

        class _Cap(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        h = _Cap()
        ra.logger.addHandler(h)
        ra.logger.setLevel(logging.DEBUG)
        try:
            with unittest.mock.patch("tradesync.replication_alert.notify"):
                ra.emit_replication_failure(
                    env="demo", kind="REJECTION", summary="s", reason="r")
        finally:
            ra.logger.removeHandler(h)
        div = [m for m in records if m.startswith(ra.DIVERGENCE_MARKER)]
        self.assertEqual(len(div), 1)
        app._maybe_extract_divergence(div[0] + "\n")
        app._on_divergence.assert_called_once()


if __name__ == "__main__":
    unittest.main()
