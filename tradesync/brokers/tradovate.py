"""
TradovateClient — REST client that authenticates against the
Tradovate API with username/password + cid/sec and submits orders
on behalf of the LEADER account.

Port of brokers/tradovateAdapter.js from
Intraday-Nasdaq-Trading-Strategy, trimmed to only what
TradeSynchronizer needs:

  - connect()       → POST /auth/accesstokenrequest, captures the
                      Bearer access token and the accountId
  - renew()         → GET /auth/renewaccesstoken (called when the
                      token is within 5 min of expiry)
  - get_contract_id → GET /contract/find?name=<symbol>  (cached)
  - place_order     → POST /order/placeorder

Market data tokens, WebSocket, OCO/bracket logic, contract roll
helpers are intentionally NOT ported here — TradeSynchronizer only
mirrors a single order at a time and stops there. TradeSyncer
takes over from the LEADER position onwards.

Thread-safety: all HTTP calls go through a single `requests.Session`
guarded by a `threading.Lock`. Token expiry is checked lazily on
every authenticated call.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests


logger = logging.getLogger("tradesync.tradovate")


# Tradovate returns ISO-8601 timestamps. We refresh the token whenever
# it is within this many seconds of expiry, well before the API would
# start rejecting it.
_TOKEN_REFRESH_LEEWAY_SECS = 5 * 60   # 5 minutes


class TradovateAuthError(RuntimeError):
    """Raised when /auth/accesstokenrequest fails or returns no token."""


class TradovateOrderError(RuntimeError):
    """Raised when /order/placeorder rejects or returns an unexpected body."""


@dataclass
class PlacedOrder:
    """Result of a successful placeOrder call."""
    order_id: int
    raw: dict


class TradovateClient:

    def __init__(
        self,
        *,
        api_url: str,
        username: str,
        password: str,
        app_id: str,
        app_version: str,
        cid: str,
        sec: str,
        pinned_account_id: Optional[int] = None,
        device_id: Optional[str] = None,
    ):
        self._api_url = api_url.rstrip("/")
        self._username = username
        self._password = password
        self._app_id = app_id
        self._app_version = app_version
        self._cid = cid
        self._sec = sec
        self._pinned_account_id = pinned_account_id
        self._device_id = device_id or str(uuid.uuid4())

        # State populated by connect()
        self._access_token: Optional[str] = None
        self._md_access_token: Optional[str] = None
        self._expiration: Optional[datetime] = None
        self._account_id: Optional[int] = None
        self._user_id: Optional[int] = None

        # Cache: tradovate_symbol → contract_id
        self._contract_id_cache: dict[str, int] = {}

        self._lock = threading.Lock()
        self._http = requests.Session()
        self._http.headers.update({"Accept": "application/json"})

    # ------------------------------------------------------------------ #
    #  Public properties                                                  #
    # ------------------------------------------------------------------ #

    @property
    def connected(self) -> bool:
        return self._access_token is not None and self._account_id is not None

    @property
    def account_id(self) -> Optional[int]:
        return self._account_id

    @property
    def user_id(self) -> Optional[int]:
        return self._user_id

    # ------------------------------------------------------------------ #
    #  Authentication                                                     #
    # ------------------------------------------------------------------ #

    def connect(self) -> None:
        """
        Obtain an access token via /auth/accesstokenrequest, then
        resolve the LEADER account ID (either from `pinned_account_id`
        or from /account/list).
        """
        credentials = {
            "name":       self._username,
            "password":   self._password,
            "appId":      self._app_id,
            "appVersion": self._app_version,
            "cid":        self._cid,    # MUST be a string, not int
            "sec":        self._sec,
            "deviceId":   self._device_id,
        }

        logger.info("Authenticating with Tradovate (%s, user=%s)",
                    self._api_url, self._username)

        try:
            resp = self._http.post(
                f"{self._api_url}/auth/accesstokenrequest",
                json=credentials,
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
        except requests.RequestException as e:
            raise TradovateAuthError(f"Auth request failed: {e}") from e

        if resp.status_code != 200:
            raise TradovateAuthError(
                f"Auth returned HTTP {resp.status_code}: {resp.text[:300]}"
            )

        data = resp.json()
        token = data.get("accessToken")
        if not token:
            err = data.get("errorText") or data.get("p-ticket") or data.get("p-time")
            raise TradovateAuthError(
                f"No accessToken in response. Body: {data}. Hint: {err}"
            )

        self._access_token = token
        self._md_access_token = data.get("mdAccessToken")
        self._expiration = _parse_iso(data.get("expirationTime"))
        self._user_id = data.get("userId")
        logger.info("Tradovate auth OK — userId=%s, token expires at %s",
                    self._user_id, self._expiration)

        # Resolve account ID
        if self._pinned_account_id:
            self._account_id = self._pinned_account_id
            logger.info("Using pinned Tradovate accountId=%s", self._account_id)
        else:
            self._account_id = self._fetch_first_account_id()
            logger.info("Resolved Tradovate accountId=%s from /account/list",
                        self._account_id)

    def _fetch_first_account_id(self) -> int:
        resp = self._authed_get("/account/list")
        if not isinstance(resp, list) or not resp:
            raise TradovateAuthError("/account/list returned no accounts")
        # Prefer accounts owned by this user, else the first one.
        for acc in resp:
            if acc.get("userId") == self._user_id:
                return int(acc["id"])
        return int(resp[0]["id"])

    def _ensure_fresh_token(self) -> None:
        """
        Renew the access token when within REFRESH_LEEWAY of expiry.
        Mirrors the JS adapter's _renewAccessToken() but synchronous —
        renewal happens just-in-time before the next API call.
        """
        if self._expiration is None or self._access_token is None:
            raise TradovateAuthError("Not connected — call connect() first")

        now_utc = datetime.now(tz=timezone.utc)
        leeway = timedelta(seconds=_TOKEN_REFRESH_LEEWAY_SECS)
        if self._expiration - now_utc > leeway:
            return  # still fresh

        logger.info("Tradovate token nearing expiry — renewing")
        try:
            resp = self._http.get(
                f"{self._api_url}/auth/renewaccesstoken",
                headers={
                    "Authorization": f"Bearer {self._access_token}",
                    "Accept": "application/json",
                },
                timeout=10,
            )
        except requests.RequestException as e:
            logger.warning("Token renew failed (%s) — falling back to full re-login", e)
            self.connect()
            return

        if resp.status_code != 200:
            logger.warning("Token renew HTTP %s — falling back to full re-login",
                           resp.status_code)
            self.connect()
            return

        data = resp.json()
        new_token = data.get("accessToken")
        if not new_token:
            logger.warning("Renew response missing accessToken — falling back to re-login")
            self.connect()
            return

        self._access_token = new_token
        self._md_access_token = data.get("mdAccessToken") or self._md_access_token
        self._expiration = _parse_iso(data.get("expirationTime"))
        logger.info("Tradovate token renewed — new expiry %s", self._expiration)

    # ------------------------------------------------------------------ #
    #  Contract resolution                                                #
    # ------------------------------------------------------------------ #

    def get_contract_id(self, tradovate_symbol: str) -> int:
        """
        Resolve a Tradovate-format symbol (e.g. `MESH6`) to its numeric
        contract id via /contract/find. Cached per session.
        """
        if not tradovate_symbol:
            raise ValueError("Empty Tradovate symbol")

        with self._lock:
            cached = self._contract_id_cache.get(tradovate_symbol)
            if cached:
                return cached

        self._ensure_fresh_token()

        resp = self._http.get(
            f"{self._api_url}/contract/find",
            params={"name": tradovate_symbol},
            headers={"Authorization": f"Bearer {self._access_token}"},
            timeout=10,
        )

        if resp.status_code != 200:
            raise TradovateOrderError(
                f"/contract/find?name={tradovate_symbol} → HTTP {resp.status_code}: "
                f"{resp.text[:300]}"
            )

        body = resp.json()
        if not isinstance(body, dict) or "id" not in body:
            raise TradovateOrderError(
                f"Contract '{tradovate_symbol}' not found on Tradovate. Body: {body}"
            )

        contract_id = int(body["id"])
        with self._lock:
            self._contract_id_cache[tradovate_symbol] = contract_id
        logger.info("Resolved Tradovate contract %s → id=%s",
                    tradovate_symbol, contract_id)
        return contract_id

    # ------------------------------------------------------------------ #
    #  Order placement                                                    #
    # ------------------------------------------------------------------ #

    def place_order(
        self,
        *,
        tradovate_symbol: str,
        contract_id: int,
        action: str,                # "Buy" | "Sell"
        qty: int,
        order_type: str,            # "Market" | "Limit" | "Stop" | "StopLimit"
        limit_price: Optional[float] = None,
        stop_price:  Optional[float] = None,
        tif: str = "Day",           # "Day" | "GTC" | "IOC" | "FOK"
    ) -> PlacedOrder:
        """
        POST /order/placeorder for the LEADER account.

        Tradovate field reference (verified against the Intraday-Nasdaq
        adapter, lines ~2400-2510):

          accountId   — int, the LEADER account id
          action      — "Buy" | "Sell"  (capitalized first letter)
          symbol      — Tradovate short-form symbol (MESH6 etc.)
          contractId  — numeric contract id from /contract/find
          orderQty    — int, contracts
          orderType   — "Market" | "Limit" | "Stop" | "StopLimit"
          limitPrice  — required for Limit / StopLimit
          stopPrice   — required for Stop / StopLimit
          timeInForce — "Day" by default
          isAutomated — true (regulatory flag for algorithmic orders)
        """
        if not self.connected:
            raise TradovateOrderError("Not connected — call connect() first")
        if action not in ("Buy", "Sell"):
            raise ValueError(f"Invalid action '{action}' — must be Buy or Sell")
        if order_type not in ("Market", "Limit", "Stop", "StopLimit"):
            raise ValueError(f"Invalid orderType '{order_type}'")
        if qty <= 0:
            raise ValueError(f"qty must be > 0, got {qty}")

        if order_type in ("Limit", "StopLimit") and limit_price is None:
            raise ValueError(f"{order_type} order requires limit_price")
        if order_type in ("Stop", "StopLimit") and stop_price is None:
            raise ValueError(f"{order_type} order requires stop_price")

        self._ensure_fresh_token()

        payload: dict = {
            "accountId":   self._account_id,
            "action":      action,
            "symbol":      tradovate_symbol,
            "contractId":  contract_id,
            "orderQty":    int(qty),
            "orderType":   order_type,
            "timeInForce": tif,
            "isAutomated": True,
        }
        if limit_price is not None:
            payload["price"] = float(limit_price)   # canonical Tradovate field
        if stop_price is not None:
            payload["stopPrice"] = float(stop_price)

        logger.info("Placing Tradovate order: %s", payload)

        try:
            resp = self._http.post(
                f"{self._api_url}/order/placeorder",
                json=payload,
                headers={
                    "Authorization": f"Bearer {self._access_token}",
                    "Content-Type": "application/json",
                },
                timeout=10,
            )
        except requests.RequestException as e:
            raise TradovateOrderError(f"Order request network error: {e}") from e

        if resp.status_code not in (200, 201):
            raise TradovateOrderError(
                f"placeorder HTTP {resp.status_code}: {resp.text[:500]}"
            )

        body = resp.json()
        # Reject detection (mirrors JS lines 2449-2505)
        if body.get("ordStatus") == "Rejected" or body.get("rejectReason"):
            reason = body.get("rejectReason") or "Rejected"
            text = body.get("text") or body.get("errorText") or ""
            raise TradovateOrderError(
                f"Tradovate rejected order: {reason} — {text}. Body: {body}"
            )

        order_id = body.get("orderId")
        if order_id is None:
            raise TradovateOrderError(
                f"placeorder response missing orderId. Body: {body}"
            )

        logger.info("✓ Tradovate order placed — id=%s", order_id)
        return PlacedOrder(order_id=int(order_id), raw=body)

    # ------------------------------------------------------------------ #
    #  HTTP helpers                                                       #
    # ------------------------------------------------------------------ #

    def _authed_get(self, path: str, params: Optional[dict] = None):
        self._ensure_fresh_token()
        resp = self._http.get(
            f"{self._api_url}{path}",
            params=params,
            headers={"Authorization": f"Bearer {self._access_token}"},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()


# ─────────────────────────────────────────────────────────────────────── #
#  Helpers                                                                #
# ─────────────────────────────────────────────────────────────────────── #

def _parse_iso(value) -> Optional[datetime]:
    """
    Tradovate's `expirationTime` is ISO-8601 with a trailing Z. Python
    3.11+ handles that natively via fromisoformat; for older versions
    we strip the Z and tag UTC manually.
    """
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    s = str(value)
    try:
        # Python 3.11+: fromisoformat accepts 'Z'
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
