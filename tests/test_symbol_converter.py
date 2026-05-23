"""
Unit tests for tradesync.symbols.converter.

Run from the repo root:

    python3 -m unittest tests.test_symbol_converter
"""

import unittest
from datetime import datetime

from tradesync.symbols.converter import (
    convert_from_tradovate_format,
    convert_to_tradovate_format,
    is_futures_contract,
    parse_contract_symbol,
)


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


class TestConvertFromTradovate(unittest.TestCase):

    def test_decade_disambiguation_current(self):
        # Pretend today is 2026: H6 is in the current decade
        today = datetime(2026, 5, 1)
        self.assertEqual(convert_from_tradovate_format("MESH6", today=today), "MESH2026")
        self.assertEqual(convert_from_tradovate_format("MNQU6", today=today), "MNQU2026")

    def test_decade_disambiguation_next(self):
        # H5 < current year 2026 last digit (6) → next decade
        today = datetime(2026, 5, 1)
        self.assertEqual(convert_from_tradovate_format("MESH5", today=today), "MESH2035")

    def test_passthrough_non_futures(self):
        self.assertEqual(convert_from_tradovate_format("SPY"), "SPY")


class TestIsFuturesContract(unittest.TestCase):

    def test_long_form(self):
        self.assertTrue(is_futures_contract("MNQM2025"))

    def test_short_form(self):
        self.assertTrue(is_futures_contract("MNQM5"))

    def test_non_futures(self):
        self.assertFalse(is_futures_contract("AAPL"))
        self.assertFalse(is_futures_contract(""))
        self.assertFalse(is_futures_contract(None))


class TestParseContractSymbol(unittest.TestCase):

    def test_long_form(self):
        parsed = parse_contract_symbol("MNQM2025")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["base"], "MNQ")
        self.assertEqual(parsed["monthCode"], "M")
        self.assertEqual(parsed["yearDigit"], "5")
        self.assertEqual(parsed["fullSymbol"], "MNQM5")

    def test_short_form(self):
        parsed = parse_contract_symbol("MESH6")
        self.assertEqual(parsed["fullSymbol"], "MESH6")
        self.assertEqual(parsed["base"], "MES")

    def test_non_futures_returns_none(self):
        self.assertIsNone(parse_contract_symbol("AAPL"))


if __name__ == "__main__":
    unittest.main()
