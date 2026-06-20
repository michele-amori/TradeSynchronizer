#!/usr/bin/env python3
"""
Tradovate account health check — strictly READ-ONLY.

What this script does (and EXPLICITLY does NOT do)
==================================================

This is a pre-flight / post-mortem diagnostic for the Tradovate side
of the replicator. It loads `.env.<env>` (default: live), authenticates
with Tradovate, and probes a dozen read-only REST endpoints to surface:

  * Whether the credentials work end-to-end (auth + token).
  * Which account id maps to the pinned TRADOVATE_ACCOUNT_ID (handles
    the id-vs-name foot-gun documented in the README troubleshooting
    table).
  * Whether contract-lookup works for the symbols you trade.
  * Account state, balance, realised + open P&L for today.
  * Daily-loss / liquidate-only blocks: `accountRiskStatus` shows
    `userTriggeredLiqOnly`, `liquidateOnly` timestamp, and
    `autoLiqCounter` — exactly what tells you the account is locked
    because of a Max Daily Loss configured by the user.
  * Recent orders + their status (Filled / Rejected / Canceled).

It does NOT, under any circumstance, send a placeorder / cancelorder /
modifyorder / placeOSO. Every call here is GET or POST-with-empty-body
to lookup endpoints. Worst case if a probe goes wrong: a single 4xx
response that we log and move on.

Designed to be runnable as a pre-flight check before launching the
engine in LIVE mode — if the account is locked, you'll know now
rather than discovering it via a stream of HTTP-400s in the engine
log later.

Usage
=====
From the repo root:

    .venv/bin/python scripts/check-tradovate-status.py
    .venv/bin/python scripts/check-tradovate-status.py --env demo
    .venv/bin/python scripts/check-tradovate-status.py --env live

Exit codes
==========
   0 — credentials valid, probes ran, no orders placed
   2 — shadow mode active (credentials missing) — refuses to run
   3 — Tradovate authentication failed
   4 — unexpected error during connect()
   5 — unexpected error during list_accounts()

Anything > 0 means STOP before launching the engine.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path


# ── Repo root resolution ─────────────────────────────────────────── #
# Locate the repo root from THIS file's path (scripts/<this>.py →
# repo root is the parent of `scripts`). Works regardless of cwd
# when the user invoked the script.

REPO_ROOT = Path(__file__).resolve().parent.parent


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Tradovate account health check — READ-ONLY, no orders placed.",
    )
    p.add_argument(
        "--env",
        choices=("demo", "live"),
        default="live",
        help="Which TRADOVATE_ENVIRONMENT to use (default: live). "
             "Reads the matching .env.<env> file from the repo root.",
    )
    p.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Show DEBUG-level Tradovate client chatter as well.",
    )
    return p.parse_args()


def _setup_logging(verbose: bool) -> logging.Logger:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-24s %(message)s",
        datefmt="%H:%M:%S",
    )
    return logging.getLogger("status")


def _load_config(env: str, log: logging.Logger):
    """Force the chosen environment, then ask Config.load() to read
    everything from disk (this handles `.env` shared + `.env.<env>` +
    `tradesync/_app_credentials.py`)."""
    os.environ["TRADOVATE_ENVIRONMENT"] = env
    sys.path.insert(0, str(REPO_ROOT))
    os.chdir(REPO_ROOT)

    from tradesync.config import Config
    cfg = Config.load()

    log.info("=" * 64)
    log.info("Tradovate account health check (READ-ONLY, no orders placed)")
    log.info("=" * 64)
    log.info("Environment:           %s", cfg.tradovate_env)
    log.info("API endpoint:          %s", cfg.tradovate_api_url)
    log.info("Username:              %s", cfg.tradovate_username or "(empty)")
    log.info("Password length:       %d", len(cfg.tradovate_password))
    log.info("App ID:                %s", cfg.tradovate_app_id)
    log.info("App version:           %s", cfg.tradovate_app_ver)
    log.info("Client ID (cid):       %s", cfg.tradovate_cid)
    log.info("Client secret length:  %d", len(cfg.tradovate_sec))
    log.info("Pinned account ID:     %s", cfg.tradovate_acct_id)
    log.info("Device ID:             %s",
             cfg.tradovate_device_id or "(none → uuid4 per session)")
    log.info("Watched IBKR accounts: %s", cfg.ibkr_watched_accounts)
    log.info("Shadow mode:           %s", cfg.is_shadow_mode)
    log.info("-" * 64)

    if cfg.is_shadow_mode:
        log.error(
            "Config says shadow mode is ACTIVE — credentials are not "
            "complete. Check .env.%s + tradesync/_app_credentials.py. "
            "Aborting before connect() to avoid testing the shadow stub "
            "instead of the real client.",
            env,
        )
        sys.exit(2)

    return cfg


def _connect(cfg, log: logging.Logger):
    """Instantiate the client and authenticate. Return the live
    TradovateClient, or sys.exit on failure."""
    from tradesync.brokers.tradovate import (
        TradovateClient, TradovateAuthError,
    )

    client = TradovateClient(
        api_url=cfg.tradovate_api_url,
        username=cfg.tradovate_username,
        password=cfg.tradovate_password,
        app_id=cfg.tradovate_app_id,
        app_version=cfg.tradovate_app_ver,
        cid=cfg.tradovate_cid,
        sec=cfg.tradovate_sec,
        pinned_account_id=cfg.tradovate_acct_id,
        device_id=cfg.tradovate_device_id or None,
    )
    log.info(
        "TradovateClient instantiated. Shadow mode (per client) = %s",
        client._shadow_mode,
    )

    log.info("→ Calling connect() …")
    try:
        client.connect()
    except TradovateAuthError as e:
        log.error("❌ Authentication FAILED: %s", e)
        sys.exit(3)
    except Exception as e:  # noqa: BLE001
        log.exception("❌ Unexpected error during connect(): %s", e)
        sys.exit(4)

    log.info("✓ connect() OK")
    log.info("  resolved account_id: %s", client.account_id)
    log.info("  user_id:             %s", client._user_id)
    log.info("  token expires at:    %s", client._expiration)
    return client


def _list_accounts(client, cfg, log: logging.Logger) -> None:
    log.info("→ Calling list_accounts() …")
    try:
        accounts = client.list_accounts()
    except Exception as e:  # noqa: BLE001
        log.exception("❌ Unexpected error during list_accounts(): %s", e)
        sys.exit(5)

    log.info("✓ list_accounts() returned %d account(s):", len(accounts))
    resolved_id = client.account_id
    for a in accounts:
        aid = a.get("id")
        name = a.get("name") or a.get("nickname") or "?"
        atype = a.get("accountType") or "?"
        active = a.get("active")
        marker = " ← RESOLVED" if aid == resolved_id else ""
        log.info(
            "    id=%s name=%s type=%s active=%s%s",
            aid, name, atype, active, marker,
        )

    if resolved_id not in {a.get("id") for a in accounts}:
        log.warning(
            "⚠ resolved account_id=%s does NOT appear in "
            "list_accounts() — this should not happen after the "
            "_resolve_pinned_account fix; check connect() logic.",
            resolved_id,
        )
    elif cfg.tradovate_acct_id and resolved_id != cfg.tradovate_acct_id:
        log.info(
            "ℹ TRADOVATE_ACCOUNT_ID=%s was the account NAME; "
            "internal id=%s. Both forms are accepted.",
            cfg.tradovate_acct_id, resolved_id,
        )


def _probe_contracts(client, log: logging.Logger) -> None:
    """Hit /contract/find for a couple of well-known symbols + one
    deliberate miss. Validates the lookup path the replicator uses
    on every placement."""
    probe = ["MESH6", "MESM6"]
    log.info("-" * 64)
    log.info("→ Probing contract lookup with %s …", probe)
    for sym in probe:
        try:
            cid = client.get_contract_id(sym)
            log.info("  ✓ %s → contract_id=%d", sym, cid)
        except Exception as e:  # noqa: BLE001
            log.warning("  ✗ %s lookup failed: %s", sym, e)

    bogus = "ZZZZZ_NOT_A_REAL_CONTRACT_42"
    log.info(
        "→ Probing deliberately-bogus symbol %r (expecting error) …",
        bogus,
    )
    try:
        cid = client.get_contract_id(bogus)
        log.error("  ✗ UNEXPECTED success: bogus symbol resolved to %d", cid)
    except Exception as e:  # noqa: BLE001
        log.info("  ✓ correctly rejected: %s", str(e)[:120])


def _try_call(label: str, fn, log: logging.Logger):
    """Run fn() and either return its result or log a one-line warning
    and return None. Used for the probes below — we want to surface
    whatever the API gives us even if some endpoints don't exist for
    this account tier."""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        log.warning("  ✗ %s failed: %s", label, str(e)[:200])
        return None


def _probe_risk_status(client, log: logging.Logger) -> None:
    """The main payload of this script: surface any block / lock /
    daily-loss-trigger that's currently active on the account."""
    log.info("-" * 64)
    log.info("→ Probing risk / lock status (read-only) …")
    acct_id = client.account_id

    # ── /cashBalance/getCashBalanceSnapshot — realised+open PnL ── #
    log.info("")
    log.info("  [a] POST /cashBalance/getCashBalanceSnapshot")

    def _post_snapshot():
        resp = client._http.post(
            f"{client._api_url}/cashBalance/getCashBalanceSnapshot",
            json={"accountId": acct_id},
            headers={
                "Authorization": f"Bearer {client._access_token}",
                "Content-Type": "application/json",
            },
            timeout=10,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json() if resp.content else {}

    snap = _try_call("cashBalanceSnapshot", _post_snapshot, log)
    if isinstance(snap, dict):
        interesting = [
            "accountId", "totalCashValue", "totalPnL", "realizedPnL",
            "weekRealizedPnL", "openPnL", "initialMargin",
            "maintenanceMargin", "marginBalance", "intradayMarketValue",
            "intradayMarketValueChange", "minMarginBalance",
            "buyingPower", "dayBuyingPower",
        ]
        for k in interesting:
            if k in snap:
                log.info("      %-30s = %s", k, snap[k])
        extra = {k: v for k, v in snap.items() if k not in interesting}
        if extra:
            log.info("      (other fields: %s)", sorted(extra.keys()))

    # ── /cashBalance/list — historical balance entries ──────────── #
    log.info("")
    log.info("  [b] GET /cashBalance/list  (most recent for this account)")
    balances = _try_call(
        "/cashBalance/list",
        lambda: client._authed_get("/cashBalance/list"),
        log,
    )
    if isinstance(balances, list):
        relevant = [b for b in balances if b.get("accountId") == acct_id]
        log.info(
            "      %d total balance entries, %d for our account",
            len(balances), len(relevant),
        )
        if relevant:
            latest = max(relevant, key=lambda b: b.get("id", 0))
            for k in ("id", "accountId", "timestamp", "amount",
                      "realizedPnL", "weekRealizedPnL", "archived"):
                if k in latest:
                    log.info("      %-22s = %s", k, latest[k])

    # ── /userPlugin/list — which API plugins are entitled ─────── #
    log.info("")
    log.info("  [c] GET /userPlugin/list  (api / tradingview entitlements)")
    plugins = _try_call(
        "/userPlugin/list",
        lambda: client._authed_get("/userPlugin/list"),
        log,
    )
    if isinstance(plugins, list):
        log.info("      %d plugins configured for this user", len(plugins))
        for p in plugins[:10]:
            pid = p.get("id")
            name = p.get("pluginName") or p.get("name") or "?"
            approval = p.get("approval")
            expires = p.get("expirationDate") or "—"
            log.info(
                "      id=%s name=%s approval=%s expires=%s",
                pid, name, approval, expires,
            )

    # ── accountRiskStatus / tradingPermission / order / position ── #
    # The big one: accountRiskStatus contains liquidateOnly +
    # userTriggeredLiqOnly fields that prove a Max-Daily-Loss block.
    for path, label in [
        ("/accountRiskStatus/list", "accountRiskStatus list"),
        ("/tradingPermission/list", "tradingPermission"),
        ("/order/list", "order/list"),
        ("/position/list", "position/list"),
    ]:
        log.info("")
        log.info("  [+] GET %s", path)
        body = _try_call(label, lambda p=path: client._authed_get(p), log)
        if body is None:
            continue
        if isinstance(body, list):
            log.info("      %d entries", len(body))
            for item in body[:5]:
                log.info("      %s",
                         json.dumps(item, default=str, indent=2))
        elif isinstance(body, dict):
            log.info("      %s",
                     json.dumps(body, default=str, indent=2))


def main() -> int:
    args = _parse_args()
    log = _setup_logging(args.verbose)

    cfg = _load_config(args.env, log)
    client = _connect(cfg, log)
    _list_accounts(client, cfg, log)
    _probe_contracts(client, log)
    _probe_risk_status(client, log)

    log.info("=" * 64)
    log.info("✓ Health check complete — credentials valid, no orders placed.")
    log.info(
        "  If accountRiskStatus.liquidateOnly is set OR "
        "userTriggeredLiqOnly is true, the account is LOCKED — "
        "the engine will see HTTP 400 Rejected on every new placement "
        "until the lock clears (typically the next prop-firm daily reset)."
    )
    log.info("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
