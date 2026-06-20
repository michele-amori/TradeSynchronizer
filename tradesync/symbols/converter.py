"""
Symbol converter — IBKR / generic → Tradovate format.

Port of brokers/utils/symbolConverter.js from
Intraday-Nasdaq-Trading-Strategy. Behaviour preserved byte-for-byte
(same regexes, same year-disambiguation heuristic).

Tradovate uses a compact futures symbol format: BASE + MONTH_CODE +
SINGLE_YEAR_DIGIT, e.g. `MESH6` for MES March 2026. Brokers such as
IBKR or generic feeds emit the long form with the full year, e.g.
`MESH2026`. This module translates between the two without ambiguity
for the next 10 years.

Month codes (CME standard):
    F=Jan G=Feb H=Mar J=Apr K=May M=Jun
    N=Jul Q=Aug U=Sep V=Oct X=Nov Z=Dec
"""

from __future__ import annotations

import re

# Long form:  BASE + MONTH_CODE + 4-digit YEAR  →  e.g. MNQM2025
_LONG_PATTERN = re.compile(r"^([A-Z]+)([FGHJKMNQUVXZ])(\d{4})$")

# Short (Tradovate) form: BASE + MONTH_CODE + 1-digit YEAR  →  MNQM5
_SHORT_PATTERN = re.compile(r"^([A-Z]+)([FGHJKMNQUVXZ])(\d)$")


def convert_to_tradovate_format(symbol: str) -> str:
    """
    Convert a long-form futures symbol to the Tradovate short form.

    Examples:
        MNQM2025 -> MNQM5
        MNQZ2024 -> MNQZ4
        ESH2026  -> ESH6

    If `symbol` is already short-form, or is not a futures pattern at
    all, it is returned unchanged.
    """
    if not symbol:
        return symbol

    m = _LONG_PATTERN.match(symbol)
    if m is None:
        return symbol

    base, month_code, year = m.group(1), m.group(2), m.group(3)
    year_digit = year[-1]
    return f"{base}{month_code}{year_digit}"


