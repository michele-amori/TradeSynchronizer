"""
Unit tests for tradesync.symbols.converter.

Run from the repo root:

    python3 -m unittest tests.test_symbol_converter
"""

import unittest

from tradesync.symbols.converter import convert_to_tradovate_format


class TestConvertToTradovate(unittest.TestCase):

    def test_long_form_is_shortened(self):
        self.assertEqual(convert_to_tradovate_format("MNQM2025"), "MNQM5")
        self.assertEqual(convert_to_tradovate_format("MNQZ2024"), "MNQZ4")
        self.assertEqual(convert_to_tradovate_format("MNQH2026"), "MNQH6")
        self.assertEqual(convert_to_tradovate_format("ESM2025"),  "ESM5")
        self.assertEqual(convert_to_tradovate_format("MESH2026"), "MESH6")

    def test_short_form_unchanged(self):
        self.assertEqual(convert_to_tradovate_format("MNQM5"), "MNQM5")

    def test_non_futures_unchanged(self):
        self.assertEqual(convert_to_tradovate_format("SPY"),  "SPY")
        self.assertEqual(convert_to_tradovate_format("AAPL"), "AAPL")

    def test_empty_passthrough(self):
        self.assertEqual(convert_to_tradovate_format(""), "")
        self.assertIsNone(convert_to_tradovate_format(None))


if __name__ == "__main__":
    unittest.main()
