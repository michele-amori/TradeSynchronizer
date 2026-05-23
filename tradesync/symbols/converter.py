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
from datetime import datetime
from typing import Optional, TypedDict

# Long form:  BASE + MONTH_CODE + 4-digit YEAR  →  e.g. MNQM2025
_LONG_PATTERN = re.compile(r"^([A-Z]+)([FGHJKMNQUVXZ])(\d{4})$")

# Short (Tradovate) form: BASE + MONTH_CODE + 1-digit YEAR  →  MNQM5
_SHORT_PATTERN = re.compile(r"^([A-Z]+)([FGHJKMNQUVXZ])(\d)$")


class ContractSymbol(TypedDict):
    """Parsed components of a futures symbol."""
    base: str
    monthCode: str
    yearDigit: str
    fullSymbol: str


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


def convert_from_tradovate_format(symbol: str, today: Optional[datetime] = None) -> str:
    """
    Expand a Tradovate short-form symbol into the long-form with full
    year, using today's date to disambiguate the decade.

    Example (in calendar year 2026):
        MESH6 -> MESH2026
        MESH5 -> MESH2035   (5 < 6 → next decade)

    Heuristic: if the last digit of the symbol is >= the last digit of
    the current year, the contract is in the current decade; otherwise
    in the next decade. This is the same rule used by the JS reference
    and is safe as long as a contract is never traded more than ~9
    years before its expiry, which is the case for all listed futures.
    """
    if not symbol:
        return symbol

    m = _SHORT_PATTERN.match(symbol)
    if m is None:
        return symbol

    base, month_code, year_digit = m.group(1), m.group(2), m.group(3)
    digit = int(year_digit)

    now = today or datetime.now()
    current_year = now.year
    current_decade = (current_year // 10) * 10
    current_year_last_digit = current_year % 10

    if digit >= current_year_last_digit:
        full_year = current_decade + digit
    else:
        full_year = current_decade + 10 + digit

    return f"{base}{month_code}{full_year}"


def is_futures_contract(symbol: str) -> bool:
    """True if the symbol matches either futures form."""
    if not symbol:
        return False
    return bool(_LONG_PATTERN.match(symbol) or _SHORT_PATTERN.match(symbol))


def parse_contract_symbol(symbol: str) -> Optional[ContractSymbol]:
    """
    Decompose a futures symbol into its base/month/year components.
    Accepts both long and short form; returns the canonical short form
    in `fullSymbol`. Returns None for non-futures.
    """
    if not symbol:
        return None

    standard = convert_to_tradovate_format(symbol)
    m = _SHORT_PATTERN.match(standard)
    if m is None:
        return None

    return {
        "base":       m.group(1),
        "monthCode":  m.group(2),
        "yearDigit":  m.group(3),
        "fullSymbol": standard,
    }
