"""
Tests for PositionReconciler — the periodic source-vs-follower position
safety check.
"""

import unittest
from dataclasses import dataclass
from typing import Optional

from tradesync.position_reconciler import (
    PositionReconciler, ReconcileReport, PositionMismatch)


@dataclass
class _FakeHealth:
    connected: bool
    seconds_since_last_frame: Optional[float]


# Symbol maps for the two fake brokers (different native ids, same
# instrument MNQM6).
_SRC_SYMBOLS = {4327110: "MNQM6", 4327111: "MESM6"}
_FOL_SYMBOLS = {770561201: "MNQM6", 770561202: "MESM6"}


def _make(src, fol, on_mismatch=None):
    return PositionReconciler(
        source_positions=lambda: dict(src),
        follower_positions=lambda: dict(fol),
        source_symbol_of=lambda i: _SRC_SYMBOLS.get(i),
        follower_symbol_of=lambda i: _FOL_SYMBOLS.get(i),
        on_mismatch=on_mismatch,
    )


class TestReconcile(unittest.TestCase):

    def test_aligned_same_position(self):
        # Both short 1 MNQ (different native ids, same symbol).
        r = _make({4327110: -1}, {770561201: -1})
        report = r.check_once()
        self.assertTrue(report.aligned)
        self.assertEqual(report.mismatches, [])

    def test_both_flat_is_aligned(self):
        r = _make({}, {})
        report = r.check_once()
        self.assertTrue(report.aligned)

    def test_source_has_position_follower_flat(self):
        # The classic dropped-event divergence: source short, follower flat.
        r = _make({4327110: -1}, {})
        report = r.check_once()
        self.assertFalse(report.aligned)
        self.assertEqual(len(report.mismatches), 1)
        m = report.mismatches[0]
        self.assertEqual(m.symbol, "MNQM6")
        self.assertEqual(m.source_qty, -1)
        self.assertEqual(m.follower_qty, 0)

    def test_different_quantities(self):
        r = _make({4327110: 2}, {770561201: 1})
        report = r.check_once()
        self.assertFalse(report.aligned)
        self.assertEqual(report.mismatches[0].symbol, "MNQM6")

    def test_opposite_signs(self):
        # Source long, follower short — the most dangerous divergence.
        r = _make({4327110: 1}, {770561201: -1})
        report = r.check_once()
        self.assertFalse(report.aligned)

    def test_mismatch_invokes_callback(self):
        seen = []
        r = _make({4327110: -1}, {}, on_mismatch=seen.append)
        r.check_once()
        self.assertEqual(len(seen), 1)
        self.assertIsInstance(seen[0], ReconcileReport)
        self.assertFalse(seen[0].aligned)

    def test_aligned_does_not_invoke_callback(self):
        seen = []
        r = _make({4327110: -1}, {770561201: -1}, on_mismatch=seen.append)
        r.check_once()
        self.assertEqual(seen, [])

    def test_transport_error_is_captured_not_raised(self):
        def boom():
            raise ConnectionError("gateway down")
        r = PositionReconciler(
            source_positions=boom,
            follower_positions=lambda: {},
            source_symbol_of=lambda i: None,
            follower_symbol_of=lambda i: None,
        )
        report = r.check_once()        # must not raise
        self.assertIsNotNone(report.error)
        self.assertIn("gateway down", report.error)
        # An inconclusive pass is reported aligned=True so it doesn't
        # cry wolf, but the error field makes the inconclusiveness clear.
        self.assertTrue(report.aligned)

    def test_unresolvable_id_surfaces_as_mismatch(self):
        # A follower position whose id maps to no symbol must NOT be
        # silently treated as flat — it shows up as 'unknown'.
        r = _make({}, {999999: -1})
        report = r.check_once()
        self.assertFalse(report.aligned)
        self.assertTrue(any("unknown" in m.symbol
                            for m in report.mismatches))

    def test_multiple_symbols_partial_mismatch(self):
        # MNQ aligned, MES diverged.
        r = _make({4327110: -1, 4327111: 2},
                  {770561201: -1, 770561202: 1})
        report = r.check_once()
        self.assertFalse(report.aligned)
        self.assertEqual([m.symbol for m in report.mismatches], ["MESM6"])

    def test_last_report_is_stored(self):
        r = _make({4327110: -1}, {770561201: -1})
        self.assertIsNone(r.last_report)
        r.check_once()
        self.assertIsNotNone(r.last_report)
        self.assertTrue(r.last_report.aligned)

    def test_report_summary_text(self):
        r = _make({4327110: -1}, {})
        report = r.check_once()
        self.assertIn("MISMATCH", report.summary())
        self.assertIn("MNQM6", report.summary())


class TestHealthLine(unittest.TestCase):
    """The combined [health] log line — the at-a-glance status the GUI's
    merged engine log shows (engine runs out-of-process)."""

    def _make_with_health(self, src, fol, health):
        return PositionReconciler(
            source_positions=lambda: dict(src),
            follower_positions=lambda: dict(fol),
            source_symbol_of=lambda i: _SRC_SYMBOLS.get(i),
            follower_symbol_of=lambda i: _FOL_SYMBOLS.get(i),
            health_source=lambda: health,
        )

    def test_health_line_connected_and_aligned_is_info(self):
        r = self._make_with_health(
            {4327110: -1}, {770561201: -1},
            _FakeHealth(connected=True, seconds_since_last_frame=2.0))
        with self.assertLogs("tradesync.reconciler", level="INFO") as cm:
            r.check_once()
        line = "\n".join(cm.output)
        self.assertIn("[health]", line)
        self.assertIn("feed=connected", line)
        self.assertIn("positions=aligned", line)
        # Healthy → no WARNING for the health line.
        self.assertNotIn("WARNING:tradesync.reconciler:[health]", line)

    def test_health_line_disconnected_is_warning(self):
        r = self._make_with_health(
            {4327110: -1}, {770561201: -1},
            _FakeHealth(connected=False, seconds_since_last_frame=None))
        with self.assertLogs("tradesync.reconciler", level="INFO") as cm:
            r.check_once()
        line = "\n".join(cm.output)
        self.assertIn("feed=DISCONNECTED", line)
        # Disconnected feed must stand out as a warning.
        self.assertTrue(any("WARNING" in o and "[health]" in o
                            for o in cm.output))

    def test_health_line_mismatch_is_warning(self):
        r = self._make_with_health(
            {4327110: -1}, {},
            _FakeHealth(connected=True, seconds_since_last_frame=1.0))
        with self.assertLogs("tradesync.reconciler", level="INFO") as cm:
            r.check_once()
        self.assertTrue(any("WARNING" in o and "[health]" in o
                            for o in cm.output))

    def test_health_line_without_health_source_says_unknown(self):
        # No health_source supplied → feed=unknown, still emits the line.
        r = _make({4327110: -1}, {770561201: -1})
        with self.assertLogs("tradesync.reconciler", level="INFO") as cm:
            r.check_once()
        line = "\n".join(cm.output)
        self.assertIn("feed=unknown", line)


def _make_ratio(src, fol, ratio, tolerance=1.0):
    return PositionReconciler(
        source_positions=lambda: dict(src),
        follower_positions=lambda: dict(fol),
        source_symbol_of=lambda i: _SRC_SYMBOLS.get(i),
        follower_symbol_of=lambda i: _FOL_SYMBOLS.get(i),
        ratio=ratio,
        tolerance=tolerance,
    )


class TestRatioAwareReconcile(unittest.TestCase):
    """With a non-1.0 ratio the follower holds source × ratio, so the
    comparison must scale — otherwise it reads as a permanent mismatch."""

    def test_ratio_half_scaled_position_is_aligned(self):
        # The real case that prompted this: source -20, follower -10,
        # ratio 0.5 → aligned, not a mismatch.
        r = _make_ratio({4327110: -20}, {770561201: -10}, ratio=0.5)
        report = r.check_once()
        self.assertTrue(report.aligned, report.summary())

    def test_ratio_half_exact_equality_is_now_mismatch(self):
        # Under ratio 0.5, follower == source (no scaling) is WRONG.
        r = _make_ratio({4327110: -20}, {770561201: -20}, ratio=0.5)
        report = r.check_once()
        self.assertFalse(report.aligned)

    def test_within_tolerance_is_aligned(self):
        # source 7 × 0.5 = 3.5; follower 4 is within ±1 → aligned
        # (per-order round-half-up makes the netted follower drift).
        r = _make_ratio({4327110: 7}, {770561201: 4}, ratio=0.5)
        self.assertTrue(r.check_once().aligned)

    def test_outside_tolerance_is_mismatch(self):
        # source 20 × 0.5 = 10; follower 6 is 4 off → real divergence.
        r = _make_ratio({4327110: 20}, {770561201: 6}, ratio=0.5)
        self.assertFalse(r.check_once().aligned)

    def test_sign_flip_is_always_mismatch(self):
        # Even within magnitude tolerance, opposite sign is a real
        # divergence: source +2 × 0.5 = +1, follower -1.
        r = _make_ratio({4327110: 2}, {770561201: -1}, ratio=0.5)
        self.assertFalse(r.check_once().aligned)

    def test_follower_flat_when_source_has_position_is_mismatch(self):
        # source -20 × 0.5 = -10 expected, follower flat → missed
        # replication, must be flagged.
        r = _make_ratio({4327110: -20}, {}, ratio=0.5)
        self.assertFalse(r.check_once().aligned)

    def test_ratio_scale_up_aligned(self):
        # ratio 2.0: source 5 → follower 10 expected.
        r = _make_ratio({4327110: 5}, {770561201: 10}, ratio=2.0)
        self.assertTrue(r.check_once().aligned)

    def test_ratio_one_keeps_exact_compare(self):
        # Guard: ratio 1.0 must still flag a 1-contract difference that
        # the tolerance band would otherwise absorb under a non-1.0 ratio.
        r = _make_ratio({4327110: 10}, {770561201: 9}, ratio=1.0)
        self.assertFalse(r.check_once().aligned)


if __name__ == "__main__":
    unittest.main()
