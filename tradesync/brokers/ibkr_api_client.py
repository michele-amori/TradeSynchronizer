"""
IbkrApiClient — synchronous wrapper around the ibapi (TWS API) socket
client, used when IBKR is the FOLLOWER (the Tradovate→IBKR direction).

It talks to a locally-running IB Gateway / TWS over the socket API
(port 4001 live / 4002 paper). ibapi is asynchronous and callback-
driven (you call EClient methods; results arrive via EWrapper
callbacks on a background reader thread). This class hides that behind
blocking, request/response-shaped methods — connect / resolve_contract
/ place_order / place_bracket / cancel_order / modify_order /
order_status — so the FollowerEndpoint adapter on top reads like the
TradovateClient one.

Threading model
---------------
ibapi's EClient.run() is a blocking read loop; we run it on a daemon
thread. EWrapper callbacks fire on THAT thread. We bridge to the
calling thread with threading.Event + per-request result slots, all
guarded by a lock. The public methods block (with a timeout) until the
relevant callback arrives.

Order ids
---------
IBKR assigns the first valid order id via nextValidId at connect time;
the client owns a monotonic counter from there, handed out under the
lock. (IBKR order ids are per-client-session integers.)

Daily restart
-------------
IB Gateway forces a disconnect once a day (overnight). `connected`
reflects the live socket state; callers (the reconnect supervisor we
add later) re-`connect()` after a drop. A daily restart also resets
the nextValidId sequence, which is why we re-read it on every connect.

Status: validated live. The connection + contract resolution paths are
exercised live against the paper Gateway in tests guarded by
TRADESYNC_IBKR_LIVE=1; the order-placement methods are unit-tested
against a fake EClient and have placed real paper orders end to end
(native OCO bracket + MKT + MODIFY + CANCEL) as the IBKR follower.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
from ibapi.order import Order


logger = logging.getLogger("tradesync.ibkr_api")


# IBKR "error" callback codes that are actually benign status notices,
# not failures. We log them at debug and never treat them as errors.
_BENIGN_CODES = frozenset({
    2104,  # Market data farm connection is OK
    2106,  # HMDS data farm connection is OK
    2107,  # HMDS data farm connection is inactive but should be available
    2108,  # Market data farm connection is inactive but should be available
    2158,  # Sec-def data farm connection is OK
    2119,  # Market data farm is connecting
    2100,  # API client has been unsubscribed from account data
})

# IBKR "error" codes that mean an ORDER was rejected / will not live —
# the async counterpart of a synchronous place failure. These arrive on
# the reader thread AFTER placeOrder already returned an id, so they're
# the only way to learn the follower won't actually hold the order
# (e.g. size exceeds the account/instrument max, often due to thin
# liquidity).
_ORDER_REJECT_CODES = frozenset({
    103,   # Duplicate order id
    104,   # Can't modify a filled order
    201,   # Order rejected - reason: ...
    202,   # Order cancelled - reason: ...
    203,   # The security is not available/allowed for this account
    434,   # The order size cannot be zero
    461,   # Order size/limit exceeded
})

# CME month codes, shared with the symbol converter's vocabulary.
_MONTH_CODE_TO_NUM = {
    "F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6,
    "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12,
}


class IbkrApiError(RuntimeError):
    """Raised when an IBKR API call fails (rejected order, contract not
    found, transport error)."""


class IbkrOrderNotFound(IbkrApiError):
    """Raised when an order id is unknown to IBKR — usually because it
    already filled or was cancelled out of band."""


class IbkrNotConnected(IbkrApiError):
    """Raised when an order call is made before connect() succeeded, or
    after the Gateway dropped the session (daily restart)."""


@dataclass
class _ContractResolution:
    """Result of resolve_contract: the IBKR Contract plus its conId."""
    contract: Contract
    con_id: int


@dataclass
class _PendingResolve:
    event: threading.Event = field(default_factory=threading.Event)
    contract: Optional[Contract] = None
    con_id: Optional[int] = None
    error: Optional[str] = None


@dataclass
class _PendingStatus:
    event: threading.Event = field(default_factory=threading.Event)
    status: Optional[str] = None
    error: Optional[str] = None


class _IbkrWrapper(EWrapper):
    """EWrapper half — receives callbacks on the reader thread and
    routes them into the owning IbkrApiClient's pending slots."""

    def __init__(self, owner: "IbkrApiClient"):
        EWrapper.__init__(self)
        self._owner = owner

    # — connection / ids —
    def nextValidId(self, orderId: int):
        self._owner._on_next_valid_id(orderId)

    def managedAccounts(self, accountsList: str):
        self._owner._managed_accounts = accountsList

    def connectAck(self):
        logger.debug("IBKR connectAck")

    # — errors —
    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
        self._owner._on_error(reqId, errorCode, errorString)

    # — contract resolution —
    def contractDetails(self, reqId: int, contractDetails):
        self._owner._on_contract_details(reqId, contractDetails)

    def contractDetailsEnd(self, reqId: int):
        self._owner._on_contract_details_end(reqId)

    # — order lifecycle —
    def orderStatus(self, orderId, status, filled, remaining, avgFillPrice,
                    permId, parentId, lastFillPrice, clientId, whyHeld,
                    mktCapPrice):
        self._owner._on_order_status(orderId, status)

    def openOrder(self, orderId, contract, order, orderState):
        self._owner._on_open_order(orderId, orderState)

    # — positions (reconciliation) —
    def position(self, account, contract, position, avgCost):
        self._owner._on_position(account, contract, position)

    def positionEnd(self):
        self._owner._on_position_end()


class IbkrApiClient(EClient):
    """Synchronous façade over ibapi for the IBKR follower role."""

    def __init__(self, *, host: str = "127.0.0.1", port: int = 4002,
                 client_id: int = 11):
        self._wrapper = _IbkrWrapper(self)
        EClient.__init__(self, self._wrapper)
        self._host = host
        self._port = port
        self._client_id = client_id

        self._lock = threading.Lock()
        self._reader_thread: Optional[threading.Thread] = None

        self._next_order_id: Optional[int] = None
        self._next_id_event = threading.Event()
        self._managed_accounts: Optional[str] = None

        # Optional callback for ASYNC order rejections (after placeOrder
        # returned). Signature: on_order_rejected(order_id, code, msg).
        # Injected by the follower/bootstrap; runs on the reader thread.
        self.on_order_rejected = None

        self._req_id_seq = 0
        self._pending_resolves: Dict[int, _PendingResolve] = {}
        self._pending_status: Dict[int, _PendingStatus] = {}
        # Latest known status per order id, updated by orderStatus
        # callbacks (which stream in unsolicited as well as on demand).
        self._order_status: Dict[int, str] = {}

        # conId cache: tradovate-short-symbol → _ContractResolution
        self._contract_cache: Dict[str, _ContractResolution] = {}

        # Position snapshot, refreshed on demand by get_positions().
        # reqPositions streams position() callbacks then a single
        # positionEnd(); we accumulate into _positions_accum under the
        # lock and hand back a {conId: net_qty} dict.
        self._positions_accum: Dict[int, float] = {}
        self._positions_event = threading.Event()

    # ── connection lifecycle ─────────────────────────────────────── #

    @property
    def is_connected(self) -> bool:
        # EClient.isConnected() reflects the live socket.
        try:
            return bool(self.isConnected())
        except Exception:  # noqa: BLE001 - defensive
            return False

    @property
    def managed_accounts(self) -> Optional[str]:
        return self._managed_accounts

    def connect_and_wait(self, timeout: float = 10.0) -> None:
        """Open the socket, start the reader thread, and block until
        the API handshake completes (nextValidId received) or timeout.

        Idempotent-ish: if already connected, returns immediately."""
        if self.is_connected and self._next_order_id is not None:
            return
        self._next_id_event.clear()
        self.connect(self._host, self._port, self._client_id)
        self._reader_thread = threading.Thread(
            target=self.run, name=f"ibkr-api-reader-{self._client_id}",
            daemon=True,
        )
        self._reader_thread.start()
        if not self._next_id_event.wait(timeout):
            self.disconnect()
            raise IbkrNotConnected(
                f"IBKR API handshake did not complete within {timeout}s "
                f"(is IB Gateway running and logged in on "
                f"{self._host}:{self._port}, with API enabled and this IP "
                f"trusted?)"
            )
        logger.info("IBKR API connected — server v%s, accounts=%s, "
                    "nextValidId=%s", self.serverVersion(),
                    self._managed_accounts, self._next_order_id)

    def disconnect_and_wait(self) -> None:
        try:
            self.disconnect()
        except Exception:  # noqa: BLE001
            pass
        t = self._reader_thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=3.0)
        self._reader_thread = None

    # ── callback handlers (run on reader thread) ─────────────────── #

    def _on_next_valid_id(self, order_id: int) -> None:
        with self._lock:
            self._next_order_id = order_id
        self._next_id_event.set()

    def _is_order_rejection(self, req_id, code) -> bool:
        """Whether an error callback denotes an ORDER rejection (vs a
        contract/status request error or a transport notice).

        True when the code is a known order-reject code, OR the reqId
        matches an order we placed (IBKR reuses the order id as the
        error reqId). Pending contract-resolve / status requests are
        excluded — those are routed to their waiting caller instead."""
        if req_id in self._pending_resolves or req_id in self._pending_status:
            return False
        if code in _ORDER_REJECT_CODES:
            return True
        # Fallback: an error tied to an id we placed or are tracking.
        return req_id in self._order_status

    def _on_error(self, req_id, code, msg) -> None:
        if code in _BENIGN_CODES:
            logger.debug("IBKR notice %s: %s", code, msg)
            return
        logger.warning("IBKR error reqId=%s code=%s: %s", req_id, code, msg)
        # Route contract-resolution errors to the waiting caller.
        with self._lock:
            pend = self._pending_resolves.get(req_id)
            if pend is not None:
                pend.error = f"[{code}] {msg}"
                pend.event.set()
            pst = self._pending_status.get(req_id)
            if pst is not None:
                pst.error = f"[{code}] {msg}"
                pst.event.set()
            is_rejection = self._is_order_rejection(req_id, code)

        # An async ORDER rejection — surface it via the injected callback
        # (outside the lock). Wrapped so a handler error never escapes
        # back into the reader thread.
        if is_rejection and self.on_order_rejected is not None:
            try:
                self.on_order_rejected(req_id, code, msg)
            except Exception as e:  # noqa: BLE001 - reader must survive
                logger.debug("on_order_rejected handler raised: %s", e)

    def _on_contract_details(self, req_id: int, details) -> None:
        with self._lock:
            pend = self._pending_resolves.get(req_id)
            if pend is None:
                return
            # First match wins; IBKR may send several for ambiguous
            # queries, but our query is specific enough.
            if pend.contract is None:
                c = details.contract
                pend.contract = c
                pend.con_id = c.conId

    def _on_contract_details_end(self, req_id: int) -> None:
        with self._lock:
            pend = self._pending_resolves.get(req_id)
            if pend is not None:
                pend.event.set()

    def _on_order_status(self, order_id: int, status: str) -> None:
        with self._lock:
            self._order_status[order_id] = status

    def _on_open_order(self, order_id: int, order_state) -> None:
        status = getattr(order_state, "status", None)
        if status:
            with self._lock:
                self._order_status[order_id] = status

    def _on_position(self, account, contract, position) -> None:
        # One callback per (account, contract). reqPositions streams
        # EVERY account the Gateway login can see AND every instrument
        # type the account holds — including bonds, stocks, etc. The
        # reconciler only cares about FUTURES (the only thing the
        # Tradovate↔IBKR replication trades; an IBKR account can also
        # hold sovereign bonds, cash funds and the like that Tradovate
        # knows nothing about, which would read as permanent phantom
        # mismatches). So we record the account AND secType and let
        # get_positions filter on both. Key by conId, the stable
        # contract id resolve_contract also returns.
        con_id = getattr(contract, "conId", None)
        if con_id is None:
            return
        sec_type = getattr(contract, "secType", None)
        with self._lock:
            self._positions_accum[(str(account), int(con_id))] = (
                float(position), sec_type)

    def _on_position_end(self) -> None:
        self._positions_event.set()

    # ── order id allocation ──────────────────────────────────────── #

    def _alloc_order_id(self) -> int:
        with self._lock:
            if self._next_order_id is None:
                raise IbkrNotConnected("no nextValidId yet — not connected")
            oid = self._next_order_id
            self._next_order_id += 1
            return oid

    def _next_req_id(self) -> int:
        with self._lock:
            self._req_id_seq += 1
            return 1_000_000 + self._req_id_seq

    # ── contract resolution ──────────────────────────────────────── #

    def resolve_contract(self, tradovate_symbol: str) -> _ContractResolution:
        """Resolve a Tradovate short-form futures symbol (e.g. 'MNQM6')
        to a fully-qualified IBKR Contract + conId, via
        reqContractDetails. Cached per symbol.

        Raises IbkrApiError if the symbol can't be parsed or IBKR
        returns no matching contract."""
        cached = self._contract_cache.get(tradovate_symbol)
        if cached is not None:
            return cached
        if not self.is_connected:
            raise IbkrNotConnected("resolve_contract called while disconnected")

        contract = self._build_query_contract(tradovate_symbol)
        req_id = self._next_req_id()
        pend = _PendingResolve()
        with self._lock:
            self._pending_resolves[req_id] = pend
        try:
            self.reqContractDetails(req_id, contract)
            if not pend.event.wait(10.0):
                raise IbkrApiError(
                    f"contract resolution for {tradovate_symbol!r} timed out")
            if pend.error and pend.contract is None:
                raise IbkrApiError(
                    f"contract resolution for {tradovate_symbol!r} failed: "
                    f"{pend.error}")
            if pend.contract is None or pend.con_id is None:
                raise IbkrApiError(
                    f"no IBKR contract found for {tradovate_symbol!r}")
            resolved = _ContractResolution(contract=pend.contract,
                                           con_id=pend.con_id)
        finally:
            with self._lock:
                self._pending_resolves.pop(req_id, None)
        self._contract_cache[tradovate_symbol] = resolved
        logger.info("Resolved %s → IBKR conId=%s (%s %s)",
                    tradovate_symbol, resolved.con_id,
                    resolved.contract.symbol,
                    resolved.contract.lastTradeDateOrContractMonth)
        return resolved

    @staticmethod
    def _build_query_contract(tradovate_symbol: str) -> Contract:
        """Build a partial IBKR FUT Contract from a Tradovate short
        symbol like 'MNQM6' → base=MNQ, month=M(Jun), year-digit=6.

        We set symbol + secType=FUT + lastTradeDateOrContractMonth
        (YYYYMM) and let reqContractDetails fill in exchange/conId.
        The single year digit is disambiguated to the nearest future
        year, matching the symbol converter's 10-year window."""
        import re
        m = re.match(r"^([A-Z]+)([FGHJKMNQUVXZ])(\d)$", tradovate_symbol)
        if m is None:
            raise IbkrApiError(
                f"can't parse Tradovate futures symbol {tradovate_symbol!r}")
        base, month_code, year_digit = m.group(1), m.group(2), int(m.group(3))
        month = _MONTH_CODE_TO_NUM[month_code]
        # Disambiguate single-digit year to the nearest year >= current
        # whose last digit matches (same heuristic the converter uses
        # in reverse).
        now = time.gmtime()
        cur_year = now.tm_year
        decade = (cur_year // 10) * 10
        year = decade + year_digit
        if year < cur_year:
            year += 10

        c = Contract()
        c.symbol = base
        c.secType = "FUT"
        c.lastTradeDateOrContractMonth = f"{year}{month:02d}"
        c.currency = "USD"
        # Exchange left blank lets IBKR resolve the primary listing for
        # the future; for CME micros it returns CME. If ambiguity bites
        # in calibration we'll pin c.exchange explicitly per product.
        return c

    # ── order placement ──────────────────────────────────────────── #

    def place_order(self, *, contract: Contract, order: Order) -> int:
        """Submit a single order. Returns the IBKR order id. Does NOT
        block for a fill — only allocates the id and transmits. The
        order's status streams in via orderStatus callbacks."""
        if not self.is_connected:
            raise IbkrNotConnected("place_order called while disconnected")
        oid = self._alloc_order_id()
        self.placeOrder(oid, contract, order)
        logger.info("IBKR placeOrder id=%s %s %s %s",
                    oid, order.action, order.totalQuantity, order.orderType)
        return oid

    def place_bracket(self, *, contract: Contract, parent: Order,
                      children: list, oca_group_seed: Optional[str] = None
                      ) -> tuple:
        """Submit an entry order plus its OCO children as an IBKR
        bracket. Returns (entry_id, [child_id, ...]).

        IBKR bracket semantics: children carry parentId = entry id, and
        only the LAST order in the group has transmit=True so the whole
        group is sent atomically. The children are OCA-grouped so one
        fill cancels the siblings (native OCO — unlike Tradovate, IBKR
        does this for us when ocaGroup is set).

        OCA group name uniqueness — IBKR requires OCA group names to be
        globally unique. The entry id alone is NOT enough once there are
        several followers: each follower is a SEPARATE client whose order
        ids restart from nextValidId (=1) on every connect, so the first
        bracket on every follower would otherwise be named "oca_1" — a
        collision. Modifying a leg of a collided group is then rejected
        with code 10326 "OCA group revision is not allowed", and the stop
        never moves. Callers pass oca_group_seed (the follower's account
        id) so the name is unique per follower: "oca_<seed>_<entry_id>".
        With one follower this regression couldn't occur (a single id
        stream gives globally unique names); it only appeared once a
        second IBKR follower was added."""
        if not self.is_connected:
            raise IbkrNotConnected("place_bracket called while disconnected")
        entry_id = self._alloc_order_id()
        child_ids = [self._alloc_order_id() for _ in children]
        if oca_group_seed:
            # Sanitise the seed (account ids are already simple, but keep
            # the group name free of spaces/control chars defensively).
            safe = "".join(ch for ch in str(oca_group_seed)
                           if ch.isalnum() or ch in ("-", "_"))
            oca_group = f"oca_{safe}_{entry_id}"
        else:
            oca_group = f"oca_{entry_id}"

        # Entry: transmit=False so it's held until the children arrive.
        parent.orderId = entry_id
        parent.transmit = False
        self.placeOrder(entry_id, contract, parent)

        for idx, (child, cid) in enumerate(zip(children, child_ids)):
            child.orderId = cid
            child.parentId = entry_id
            child.ocaGroup = oca_group
            child.ocaType = 1   # cancel remaining on fill, proportional
            # Last child transmits the whole group.
            child.transmit = (idx == len(children) - 1)
            self.placeOrder(cid, contract, child)

        logger.info("IBKR placeBracket entry=%s children=%s oca=%s",
                    entry_id, child_ids, oca_group)
        return entry_id, child_ids

    def cancel_order(self, order_id: int) -> None:
        """Cancel a previously-placed order by IBKR id."""
        if not self.is_connected:
            raise IbkrNotConnected("cancel_order called while disconnected")
        # ibapi 9.81's cancelOrder takes just the id; newer takes
        # (id, manualCancelOrderTime). Call defensively.
        try:
            self.cancelOrder(order_id, "")
        except TypeError:
            self.cancelOrder(order_id)
        logger.info("IBKR cancelOrder id=%s", order_id)

    def modify_order(self, *, order_id: int, contract: Contract,
                     order: Order) -> None:
        """Modify an order: in IBKR you re-place with the SAME order id
        and the changed fields. transmit=True sends it immediately."""
        if not self.is_connected:
            raise IbkrNotConnected("modify_order called while disconnected")
        order.orderId = order_id
        order.transmit = True
        self.placeOrder(order_id, contract, order)
        logger.info("IBKR modifyOrder id=%s %s", order_id, order.orderType)

    def order_status(self, order_id: int) -> str:
        """Return the last-known status for an order id, or raise
        IbkrOrderNotFound if we've never seen a status for it.

        Status strings are IBKR's: PendingSubmit / PreSubmitted /
        Submitted / Filled / Cancelled / ApiCancelled / Inactive."""
        with self._lock:
            status = self._order_status.get(order_id)
        if status is None:
            raise IbkrOrderNotFound(
                f"no status known for IBKR order id={order_id}")
        return status

    # ── positions (reconciliation) ───────────────────────────────── #

    def symbol_for_con_id(self, con_id: int) -> Optional[str]:
        """Reverse-lookup a conId → the Tradovate-style symbol we
        resolved it from (e.g. 770561201 → 'MNQM6'), using the contract
        cache populated by resolve_contract. Returns None if we've never
        resolved that conId — the reconciler then reports it as unknown
        rather than guessing."""
        for symbol, resolution in self._contract_cache.items():
            if resolution.con_id == int(con_id):
                return symbol
        return None

    def get_positions(self, timeout: float = 5.0,
                      account: Optional[str] = None,
                      sec_types=("FUT",)) -> Dict[int, float]:
        """Return current net positions as {conId: net_qty} (signed:
        +long / -short) for ONE account, restricted to the given
        instrument types (FUTURES only by default). Only non-zero
        positions are included.

        reqPositions streams positions for EVERY account the Gateway
        login can see, AND every instrument type the account holds, so
        we filter twice:
          * account — `account` if given, else the sole managed account
            if the login manages exactly one, else raise (a multi-account
            login MUST say which one, or we'd blend unrelated books).
          * sec_types — keep only these IBKR secTypes. Defaults to FUT
            because that's all the Tradovate↔IBKR replication trades; an
            IBKR account may also hold bonds / stocks / cash funds that
            Tradovate knows nothing about, which would otherwise read as
            permanent phantom mismatches. Pass sec_types=None to disable
            the type filter.

        Blocks until IBKR streams the full set (positionEnd) or the
        timeout elapses. Raises IbkrNotConnected if the socket is down."""
        if not self.is_connected:
            raise IbkrNotConnected("get_positions called while disconnected")

        want = account
        if want is None:
            managed = (self._managed_accounts or "").split(",")
            managed = [a.strip() for a in managed if a.strip()]
            if len(managed) == 1:
                want = managed[0]
            else:
                raise IbkrApiError(
                    "get_positions: Gateway manages multiple accounts "
                    f"({managed}); pass account= to pick which one to read")

        with self._lock:
            self._positions_accum = {}
        self._positions_event.clear()
        self.reqPositions()
        got = self._positions_event.wait(timeout)
        # Always cancel the subscription so we get a fresh snapshot next
        # time rather than a stream of live updates.
        try:
            self.cancelPositions()
        except Exception:  # noqa: BLE001 - defensive
            pass
        with self._lock:
            snapshot = dict(self._positions_accum)
        if not got:
            logger.warning("IBKR get_positions timed out after %.1fs — "
                           "returning partial snapshot (%d row(s))",
                           timeout, len(snapshot))
        # Keep only the wanted account's rows of the wanted instrument
        # type(s), dropping flat positions.
        allowed = set(sec_types) if sec_types is not None else None
        out: Dict[int, float] = {}
        for (acct, cid), (qty, sec_type) in snapshot.items():
            if acct != want or qty == 0:
                continue
            if allowed is not None and sec_type not in allowed:
                continue
            out[cid] = qty
        return out
