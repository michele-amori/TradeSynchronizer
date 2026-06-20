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

    def capture_token(self, authorization_header: str) -> str:
        """
        Stash an IBKR Bearer token if (and only if) the intercepted
        request uses Bearer auth. Returns a short string describing
        what was seen, so callers can log accurately:
            "bearer"   → captured a fresh JWT-style token
            "bearer-same"  → same token as already stored (no-op)
            "oauth"    → OAuth 1.0a header (NOT captured; see below)
            "other"    → unrecognised auth scheme
            "none"     → no Authorization header

        IMPORTANT: TradingView Desktop authenticates to api.ibkr.com
        with OAuth 1.0a (`OAuth realm="…", oauth_consumer_key="…",
        oauth_signature="…"`), NOT a Bearer token. Replaying an
        OAuth-signed request would require TV's consumer secret +
        re-computing the HMAC for our own URL, which we don't have
        access to. So the active resolve path
        (resolve_symbol → GET /contract/{conid}/info) silently
        does nothing useful against TV traffic. Conids still get
        cached PASSIVELY via observe_contract_info() whenever TV
        opens or pages through a chart — that path works fine.
        """
        if not authorization_header:
            return "none"
        if not authorization_header.startswith("Bearer "):
            # Be specific about OAuth vs other so the log message is
            # informative — OAuth is the expected TV case, anything
            # else is a true unknown worth flagging.
            if authorization_header.lstrip().lower().startswith("oauth"):
                return "oauth"
            return "other"
        token = authorization_header[len("Bearer "):].strip()
        if not token:
            return "none"
        with self._lock:
            if token == self._bearer_token:
                return "bearer-same"
            self._bearer_token = token
            logger.debug("IBKR bearer token captured (len=%d)", len(token))
            return "bearer"

    def observe_contract_info(self, path: str, response_body: bytes,
                              *, on_new_symbol=None) -> None:
        """
        Inspect `/v1/tv/iserver/contract/{conid}/info` responses and
        cache the resulting symbol. Called from the addon's response()
        hook for IBKR flows; silent on parse failure.

        on_new_symbol: optional callback fired (synchronously, in
        the caller's thread) when a previously-unknown conid is
        observed. Used by the addon to launch a background
        pre-resolve of the Tradovate contract_id so the FIRST order
        on this symbol doesn't pay for the /contract/find round-
        trip (~50-150ms saved).
        """
        m = _INFO_PATH_RE.match(path)
        if m is None:
            return
        try:
            conid = int(m.group(1))
        except ValueError:
            return

        body = response_body or b""

        # Defensive decompression: mitmproxy normally decompresses
        # automatically via flow.response.content, but in practice
        # we've seen `/info` payloads arriving still gzip-compressed
        # (magic bytes 0x1F 0x8B confirmed via xxd on the live
        # traffic log). This bypass-the-mitmproxy fallback covers
        # those cases — and the explicit fallback also lets unit
        # tests pass compressed bytes directly without rigging up
        # the full HTTPFlow machinery.
        if body[:2] == b"\x1f\x8b":
            try:
                import gzip
                body = gzip.decompress(body)
            except OSError as e:
                logger.debug("observe_contract_info: conid=%d gzip "
                             "decompression failed: %s", conid, e)
                return

        try:
            import json
            data = json.loads(body or b"{}")
        except (ValueError, TypeError) as e:
            logger.debug("observe_contract_info: conid=%d JSON parse "
                         "failed (%d bytes, first 80: %r): %s",
                         conid, len(body), body[:80], e)
            return

        symbol = _extract_symbol(data)
        if not symbol:
            logger.debug("observe_contract_info: conid=%d JSON parsed "
                         "but no symbol extracted; keys=%s",
                         conid, sorted(data.keys()) if isinstance(data, dict) else type(data).__name__)
            return

        with self._lock:
            already_known = conid in self._symbol_cache
            self._symbol_cache[conid] = symbol

        if not already_known:
            logger.info("IBKR contract observed: conid=%d → %s", conid, symbol)
            if on_new_symbol is not None:
                try:
                    on_new_symbol(symbol)
                except Exception as e:
                    logger.debug("on_new_symbol callback raised: %s", e)

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
                f"conid={conid} not in cache and active fallback is "
                f"unavailable (TradingView authenticates to api.ibkr.com "
                f"with OAuth 1.0a, which we can't replay; passive "
                f"resolution depends on observing the /info response "
                f"flowing through the proxy). Open or refresh the chart "
                f"for this contract in TradingView and the conid will be "
                f"cached automatically."
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

    The IBKR Client Portal API used by TradingView Desktop returns
    fields in snake_case (`local_symbol`, `expiry_full`), not the
    camelCase forms (`localSymbol`, `expirationDate`) that older
    IBKR client APIs use. We accept BOTH so this works whether the
    payload reaches us via TV or via a direct CPAPI call.

    Empirically observed key set from a real /info for conid=770561201
    (after gzip-decompression):
        allow_sell_long, cfi_code, classifier, company_name, con_id,
        contract_clarification_type, contract_month, currency, cusip,
        exchange, expiry_full, has_related_contracts, instrument_type,
        is_zero_commission_security, local_symbol, maturity_date,
        multiplier, r_t_h, symbol, text, trading_class,
        underlying_con_id, underlying_issuer, valid_exchanges

    Preference order:
      1. tickerSymbol / ticker  — already short-form (legacy schemas)
      2. local_symbol / localSymbol  — IBKR's canonical short form
         (e.g. "MESH6" or "MES H6"). Snake_case in the TV-routed
         CPAPI, camelCase elsewhere.
      3. symbol + expiry_full / expirationDate / expiry  —
         reconstruct via CME month code as a final fallback.
    """
    if not isinstance(info, dict):
        return None

    # 1. Already-short ticker fields (legacy / non-CPAPI schemas).
    ticker = info.get("tickerSymbol") or info.get("ticker")
    if ticker and isinstance(ticker, str):
        s = ticker.strip().replace(" ", "").upper()
        if s:
            return s

    # 2. local_symbol — moved BEFORE the symbol+expiry reconstruction
    # because when present it's authoritative (IBKR's own short
    # form), no month-code arithmetic needed and no risk of getting
    # the year digit wrong on contracts spanning a decade boundary.
    for key in ("local_symbol", "localSymbol"):
        local = info.get(key)
        if isinstance(local, str):
            s = local.strip().replace(" ", "").upper()
            if s:
                return s

    # 3. symbol + expiry reconstruction — last-resort fallback for
    # payloads that only carry the root symbol + an expiry date.
    sym = info.get("symbol")
    expiry = (info.get("expiry_full")
              or info.get("expirationDate")
              or info.get("expiry"))
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

    return None
