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
    """Raised when /order/placeorder/cancelorder/modifyorder rejects or
    returns an unexpected body."""


class TradovateOrderNotFound(TradovateOrderError):
    """Raised when /order/cancelorder or /order/modifyorder reports
    that the target orderId is unknown — usually because the order
    was already filled or already cancelled by the time we got here.
    The caller can treat this as benign (best-effort sync)."""


@dataclass
class PlacedOrder:
    """Result of a successful placeOrder call."""
    order_id: int
    raw: dict


@dataclass
class PlacedBracket:
    """
    Result of a successful placeOSO (bracket) call. `bracket_ids`
    is a list of 1 or 2 child order ids in the same order as the
    `brackets` list passed to place_bracket().
    """
    entry_order_id: int
    bracket_ids:    list           # type: list[int]
    oco_id:         Optional[int]
    raw:            dict


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
    #  Order status query (used by startup reconciliation)                #
    # ------------------------------------------------------------------ #

    def get_order_status(self, order_id: int) -> str:
        """
        GET /order/item?id={order_id} → return the `ordStatus` field
        ("Working" | "Filled" | "Cancelled" | "Rejected" | "Expired").

        Raises TradovateOrderNotFound when the id is unknown to
        Tradovate (404 or empty body) — typically because the order
        has been pruned from the broker's recent-history window.
        Other transport / parse errors raise TradovateOrderError.

        Used by Replicator.reconcile_with_tradovate() at startup to
        prune the persistent OrderMap of entries whose underlying
        order is no longer active.
        """
        if not self.connected:
            raise TradovateOrderError("Not connected — call connect() first")
        if not isinstance(order_id, int) or order_id <= 0:
            raise ValueError(f"order_id must be a positive int, got {order_id!r}")

        self._ensure_fresh_token()
        try:
            resp = self._http.get(
                f"{self._api_url}/order/item",
                params={"id": int(order_id)},
                headers={"Authorization": f"Bearer {self._access_token}"},
                timeout=10,
            )
        except requests.RequestException as e:
            raise TradovateOrderError(f"order/item network error: {e}") from e

        if resp.status_code == 404:
            raise TradovateOrderNotFound(
                f"order/item id={order_id} → 404 (unknown to Tradovate)"
            )
        if resp.status_code != 200:
            raise TradovateOrderError(
                f"order/item HTTP {resp.status_code}: {resp.text[:300]}"
            )

        try:
            body = resp.json() if resp.content else None
        except ValueError as e:
            raise TradovateOrderError(f"order/item returned non-JSON: {e}") from e

        # Tradovate returns {} or null when the id is unknown but the
        # endpoint itself succeeded.
        if not body or not isinstance(body, dict):
            raise TradovateOrderNotFound(
                f"order/item id={order_id} → empty body"
            )

        status = body.get("ordStatus") or body.get("status")
        if not status:
            raise TradovateOrderError(
                f"order/item id={order_id} → no ordStatus in body: {body}"
            )
        return str(status)

    # ------------------------------------------------------------------ #
    #  Bracket placement (placeoso)                                       #
    # ------------------------------------------------------------------ #

    def place_bracket(
        self,
        *,
        tradovate_symbol: str,
        contract_id: int,
        entry_action: str,                 # "Buy" | "Sell"
        entry_qty: int,
        entry_order_type: str,             # "Market" | "Limit" | "Stop" | "StopLimit"
        entry_limit_price: Optional[float] = None,
        entry_stop_price:  Optional[float] = None,
        entry_tif: str = "Day",
        brackets: Optional[list] = None,   # type: list[dict] — 1 or 2 child legs
    ) -> PlacedBracket:
        """
        POST /order/placeoso for an entry-with-brackets group.

        Each entry of `brackets` is a dict shaped like:

            {
              "action":      "Buy" | "Sell",
              "order_type":  "Limit" | "Stop" | "StopLimit",
              "limit_price": float | None,
              "stop_price":  float | None,
              "tif":         "Day" | "GTC" | ...,
            }

        IMPORTANT — empirical disclaimer:
        Tradovate's bracket endpoint accepts a `bracket1` / `bracket2`
        sibling pair on the parent payload, and the success response
        carries `orderId` for the entry plus `oso1Id` / `oso2Id` for
        the children. This shape matches the documented Tradovate REST
        API as of the JS adapter we ported from, but the exact field
        names have not been verified against a live trade since this
        codebase only mirrors single orders so far. On first real-life
        bracket replication, expect this method to either succeed or
        fail loudly — the raw response body is included in any error
        for diagnostic purposes.
        """
        if not self.connected:
            raise TradovateOrderError("Not connected — call connect() first")
        if not brackets or len(brackets) > 2:
            raise ValueError(
                f"place_bracket requires 1..2 children, got {len(brackets or [])}"
            )

        self._ensure_fresh_token()

        payload: dict = {
            "accountId":   self._account_id,
            "action":      entry_action,
            "symbol":      tradovate_symbol,
            "contractId":  contract_id,
            "orderQty":    int(entry_qty),
            "orderType":   entry_order_type,
            "timeInForce": entry_tif,
            "isAutomated": True,
        }
        if entry_limit_price is not None:
            payload["price"] = float(entry_limit_price)
        if entry_stop_price is not None:
            payload["stopPrice"] = float(entry_stop_price)

        for idx, b in enumerate(brackets, start=1):
            slot = f"bracket{idx}"
            child: dict = {
                "action":      b["action"],
                "orderType":   b["order_type"],
                "orderQty":    int(b.get("qty", entry_qty)),
                "timeInForce": b.get("tif", "Day"),
                "isAutomated": True,
            }
            if b.get("limit_price") is not None:
                child["price"] = float(b["limit_price"])
            if b.get("stop_price") is not None:
                child["stopPrice"] = float(b["stop_price"])
            payload[slot] = child

        logger.info("Placing Tradovate bracket order: %s", payload)

        try:
            resp = self._http.post(
                f"{self._api_url}/order/placeoso",
                json=payload,
                headers={
                    "Authorization": f"Bearer {self._access_token}",
                    "Content-Type": "application/json",
                },
                timeout=15,
            )
        except requests.RequestException as e:
            raise TradovateOrderError(f"placeoso network error: {e}") from e

        if resp.status_code not in (200, 201):
            raise TradovateOrderError(
                f"placeoso HTTP {resp.status_code}: {resp.text[:600]}"
            )
        body = resp.json()
        if body.get("ordStatus") == "Rejected" or body.get("rejectReason"):
            reason = body.get("rejectReason") or "Rejected"
            text = body.get("text") or body.get("errorText") or ""
            raise TradovateOrderError(
                f"Tradovate rejected bracket: {reason} — {text}. Body: {body}"
            )

        entry_id = body.get("orderId")
        if entry_id is None:
            raise TradovateOrderError(
                f"placeoso response missing entry orderId. Body: {body}"
            )

        # Defensive: Tradovate has historically used oso1Id / oso2Id
        # for the children but a few variants exist (orderIds array,
        # bracket1Id naming, …). Try the obvious slot names in order.
        bracket_ids: list = []
        for idx in (1, 2):
            for key in (f"oso{idx}Id", f"bracket{idx}Id",
                        f"bracket{idx}OrderId"):
                if key in body and body[key] is not None:
                    bracket_ids.append(int(body[key]))
                    break
        # Fallback shape: {"orderIds": [entry_id, b1_id, b2_id, ...]}
        if not bracket_ids and isinstance(body.get("orderIds"), list):
            ids = [int(x) for x in body["orderIds"] if x is not None]
            bracket_ids = [i for i in ids if i != int(entry_id)]

        if len(bracket_ids) != len(brackets):
            logger.warning(
                "placeoso returned %d bracket id(s) but %d were sent — "
                "subsequent cancel/modify on missing children won't be "
                "replicated. Response body for calibration: %s",
                len(bracket_ids), len(brackets), body,
            )

        oco_id = body.get("ocoId")
        logger.info(
            "✓ Tradovate bracket placed — entry=%s brackets=%s oco=%s",
            entry_id, bracket_ids, oco_id,
        )
        return PlacedBracket(
            entry_order_id=int(entry_id),
            bracket_ids=bracket_ids,
            oco_id=int(oco_id) if oco_id is not None else None,
            raw=body,
        )

    # ------------------------------------------------------------------ #
    #  Order cancellation                                                 #
    # ------------------------------------------------------------------ #

    def cancel_order(self, order_id: int) -> dict:
        """
        POST /order/cancelorder for an existing order. Returns the raw
        Tradovate response on success (mostly useful for logs and
        tests); raises TradovateOrderNotFound if the order id is
        unknown (typical of orders that already filled or cancelled
        before we got here).
        """
        if not self.connected:
            raise TradovateOrderError("Not connected — call connect() first")
        if not isinstance(order_id, int) or order_id <= 0:
            raise ValueError(f"order_id must be a positive int, got {order_id!r}")

        self._ensure_fresh_token()

        payload = {"orderId": int(order_id)}
        logger.info("Cancelling Tradovate order: id=%s", order_id)

        try:
            resp = self._http.post(
                f"{self._api_url}/order/cancelorder",
                json=payload,
                headers={
                    "Authorization": f"Bearer {self._access_token}",
                    "Content-Type": "application/json",
                },
                timeout=10,
            )
        except requests.RequestException as e:
            raise TradovateOrderError(f"cancelorder network error: {e}") from e

        return self._unpack_lifecycle_response(
            resp, action="cancelorder", order_id=order_id
        )

    # ------------------------------------------------------------------ #
    #  Order modification                                                 #
    # ------------------------------------------------------------------ #

    def modify_order(
        self,
        order_id: int,
        *,
        qty: Optional[int] = None,
        limit_price: Optional[float] = None,
        stop_price: Optional[float] = None,
        tif: Optional[str] = None,
    ) -> dict:
        """
        POST /order/modifyorder for an existing order. Only the
        non-None fields are sent. Returns the raw response on success;
        raises TradovateOrderNotFound if the order id is unknown.
        """
        if not self.connected:
            raise TradovateOrderError("Not connected — call connect() first")
        if not isinstance(order_id, int) or order_id <= 0:
            raise ValueError(f"order_id must be a positive int, got {order_id!r}")
        if qty is None and limit_price is None and stop_price is None \
                and tif is None:
            raise ValueError(
                "modify_order called with nothing to change — pass at least "
                "one of qty / limit_price / stop_price / tif"
            )

        self._ensure_fresh_token()

        payload: dict = {"orderId": int(order_id), "isAutomated": True}
        if qty is not None:
            if not isinstance(qty, int) or qty <= 0:
                raise ValueError(f"qty must be a positive int, got {qty!r}")
            payload["orderQty"] = int(qty)
        if limit_price is not None:
            payload["price"] = float(limit_price)
        if stop_price is not None:
            payload["stopPrice"] = float(stop_price)
        if tif is not None:
            payload["timeInForce"] = tif

        logger.info("Modifying Tradovate order: %s", payload)

        try:
            resp = self._http.post(
                f"{self._api_url}/order/modifyorder",
                json=payload,
                headers={
                    "Authorization": f"Bearer {self._access_token}",
                    "Content-Type": "application/json",
                },
                timeout=10,
            )
        except requests.RequestException as e:
            raise TradovateOrderError(f"modifyorder network error: {e}") from e

        return self._unpack_lifecycle_response(
            resp, action="modifyorder", order_id=order_id
        )

    def _unpack_lifecycle_response(
        self, resp: requests.Response, *, action: str, order_id: int
    ) -> dict:
        """Common response handling for cancelorder / modifyorder."""
        if resp.status_code not in (200, 201):
            raise TradovateOrderError(
                f"{action} HTTP {resp.status_code}: {resp.text[:500]}"
            )

        body: dict
        try:
            body = resp.json() if resp.content else {}
        except ValueError as e:
            raise TradovateOrderError(
                f"{action} returned non-JSON body: {e}"
            ) from e

        # Tradovate's failure mode: 200 with a failureReason in the body.
        failure = body.get("failureReason") or body.get("rejectReason")
        text = body.get("failureText") or body.get("text") or body.get("errorText")
        if failure:
            # "OrderNotFound" / "NotFound" — typical when the order
            # already filled or was already cancelled out-of-band.
            if isinstance(failure, str) and "notfound" in failure.replace(" ", "").lower():
                raise TradovateOrderNotFound(
                    f"{action} for orderId={order_id} — not found "
                    f"({failure}{': ' + text if text else ''})"
                )
            raise TradovateOrderError(
                f"{action} failed: {failure}{': ' + text if text else ''}. "
                f"Body: {body}"
            )

        logger.info("✓ Tradovate %s OK — orderId=%s", action, order_id)
        return body

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
