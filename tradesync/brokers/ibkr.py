"""
IBKR contract resolver — translates a numeric `conid` into a
broker-agnostic futures symbol that can be fed to the Tradovate
converter.

Strategy
========
Two-tier resolution to minimise outbound network calls:

  1. Passive cache. TradingView Desktop calls
     `GET /v1/tv/iserver/contract/{conid}/info` for every chart it
     opens and for the symbol picker. The proxy addon hands those
     responses to `observe_contract_info()`, which extracts the
     symbol fields and caches them keyed by conid. By the time the
     user clicks BUY/SELL, the symbol is usually already in cache.

  2. Active fallback. If we see an order POST for a conid we've never
     observed, `resolve_symbol()` issues a GET to the same endpoint
     using the most recently captured `Authorization: Bearer …` token.
     The result is cached for subsequent lookups.

IBKR `/contract/{conid}/info` response shape (verified via the spy
addon on TradingView Desktop):

    {
      "symbol":          "MES",          # underlying root
      "expiry":          "20260320",     # YYYYMMDD, futures only
      "fullName":        "Micro E-mini S&P 500 March 2026",
      "tickerSymbol":    "MESH6",        # ← Tradovate-compatible!
      "secType":         "FUT",
      ...
    }

When `tickerSymbol` is present we use it directly — it's already in
the compact CME format Tradovate expects. Otherwise we reconstruct it
from `symbol` + `expiry` using CME month codes.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Optional

import requests

logger = logging.getLogger("tradesync.ibkr")

# CME futures month codes, 1-indexed by month-of-year.
_MONTH_CODES = [None, "F", "G", "H", "J", "K", "M", "N", "Q", "U", "V", "X", "Z"]

_INFO_PATH_RE = re.compile(r"^/v1/tv/iserver/contract/(\d+)/info\b")


class ContractResolutionError(RuntimeError):
    """Raised when neither cache nor active lookup yields a symbol."""


class IbkrContractResolver:

    def __init__(self):
        self._lock = threading.Lock()
        # conid → tradovate-style short symbol (e.g. "MESH6")
        self._symbol_cache: dict[int, str] = {}
        # Most recently observed Bearer token on api.ibkr.com.
        # Captured passively — see capture_token().
        self._bearer_token: Optional[str] = None

    # ------------------------------------------------------------------ #
    #  Passive observation                                                #
    # ------------------------------------------------------------------ #

    def capture_token(self, authorization_header: str) -> None:
        """
        Stash the most recent IBKR Bearer token from any intercepted
        request. The token is short-lived (~24h) but is refreshed
        every time TradingView pings the IBKR API.
        """
        if not authorization_header:
            return
        if not authorization_header.startswith("Bearer "):
            return
        token = authorization_header[len("Bearer "):].strip()
        if not token:
            return
        with self._lock:
            if token != self._bearer_token:
                self._bearer_token = token
                logger.debug("IBKR bearer token captured (len=%d)", len(token))

    def observe_contract_info(self, path: str, response_body: bytes) -> None:
        """
        Inspect `/v1/tv/iserver/contract/{conid}/info` responses and
        cache the resulting symbol. Called from the addon's response()
        hook for IBKR flows; silent on parse failure.
        """
        m = _INFO_PATH_RE.match(path)
        if m is None:
            return
        try:
            conid = int(m.group(1))
        except ValueError:
            return

        try:
            import json
            data = json.loads(response_body or b"{}")
        except (ValueError, TypeError):
            return

        symbol = _extract_symbol(data)
        if symbol:
            with self._lock:
                self._symbol_cache[conid] = symbol
            logger.info("IBKR contract observed: conid=%d → %s", conid, symbol)

    # ------------------------------------------------------------------ #
    #  Active resolution                                                  #
    # ------------------------------------------------------------------ #

    def resolve_symbol(self, conid: int) -> str:
        """
        Return the Tradovate-compatible short symbol for the given
        conid. Uses the cache first; falls back to an active GET to
        IBKR if needed. Raises ContractResolutionError on failure.
        """
        with self._lock:
            cached = self._symbol_cache.get(conid)
            token = self._bearer_token
        if cached:
            return cached

        if not token:
            raise ContractResolutionError(
                f"conid={conid} not in cache and no IBKR bearer token captured "
                f"yet. Open the chart in TradingView once so the proxy can "
                f"observe the token."
            )

        url = f"https://api.ibkr.com/v1/tv/iserver/contract/{conid}/info"
        try:
            resp = requests.get(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
                timeout=8,
            )
        except requests.RequestException as e:
            raise ContractResolutionError(
                f"Active lookup for conid={conid} network error: {e}"
            ) from e

        if resp.status_code != 200:
            raise ContractResolutionError(
                f"Active lookup for conid={conid} returned HTTP "
                f"{resp.status_code}: {resp.text[:300]}"
            )

        try:
            data = resp.json()
        except ValueError as e:
            raise ContractResolutionError(
                f"Active lookup for conid={conid} body not JSON: {e}"
            ) from e

        symbol = _extract_symbol(data)
        if not symbol:
            raise ContractResolutionError(
                f"Couldn't extract a symbol from /info response for "
                f"conid={conid}. Body: {data}"
            )

        with self._lock:
            self._symbol_cache[conid] = symbol
        logger.info("IBKR contract resolved (active): conid=%d → %s", conid, symbol)
        return symbol


# ─────────────────────────────────────────────────────────────────────── #
#  Symbol extraction                                                      #
# ─────────────────────────────────────────────────────────────────────── #

def _extract_symbol(info: dict) -> Optional[str]:
    """
    Pull a Tradovate-style short symbol from an IBKR /info payload.

    Preference order:
      1. tickerSymbol  — already short-form (e.g. "MESH6")
      2. symbol + expiry  — reconstruct via CME month code
      3. localSymbol   — IBKR alternative; sometimes equal to the
                         short form, sometimes the long form
                         ("MES H6" with a space). Last resort.
    """
    if not isinstance(info, dict):
        return None

    ticker = info.get("tickerSymbol") or info.get("ticker")
    if ticker and isinstance(ticker, str):
        s = ticker.strip().replace(" ", "").upper()
        if s:
            return s

    sym = info.get("symbol")
    expiry = info.get("expiry") or info.get("expirationDate")
    if isinstance(sym, str) and isinstance(expiry, str) and len(expiry) >= 6:
        sym_u = sym.strip().upper()
        try:
            year = int(expiry[:4])
            month = int(expiry[4:6])
        except ValueError:
            year, month = 0, 0
        if 1 <= month <= 12 and year >= 2000:
            mc = _MONTH_CODES[month]
            return f"{sym_u}{mc}{year % 10}"

    local = info.get("localSymbol")
    if isinstance(local, str):
        s = local.strip().replace(" ", "").upper()
        if s:
            return s

    return None
