"""
Structured replication-failure alerts — the single channel every failure
surface flows through.

A replication failure can originate in three places:
  * a SYNCHRONOUS follower error (place/cancel/modify raised) — surfaced
    by the addon runner and the WS pipeline as an EventResult(success=
    False);
  * an ASYNCHRONOUS follower rejection (e.g. IBKR rejects an order for
    size / liquidity AFTER placeOrder already returned) — surfaced on the
    broker's error callback thread;
  * (historically) a position divergence found by the reconciler.

They all converge here. `emit_replication_failure` does two things:

  1. Logs a structured `DIVERGENCE {json}` line. The GUI's log-queue
     drainer parses exactly this marker (see _maybe_extract_divergence)
     and lights the per-env Sync-health panel red with a count + the
     latest summary/reason, plus an Acknowledge button. The JSON schema
     the panel reads is: {env, ts, kind, summary, reason}.

  2. Fires a fire-and-forget desktop notification (notify()), the
     "tap on the shoulder" so the trader sees a rejection even when not
     looking at the GUI.

Keeping the marker name `DIVERGENCE` (rather than inventing `REJECTION`)
means the existing GUI parser + panel work unchanged — the `kind` field
distinguishes a rejection from a divergence for display.

This module must be import-light and never raise: a failure in the alert
path must not take down the replication path or a broker reader thread.
"""

from __future__ import annotations

import json
import logging
import time

from tradesync.notify import notify


logger = logging.getLogger("tradesync.replication_alert")

# The marker the GUI's _maybe_extract_divergence() scans for. Kept as a
# module constant so the producer and any test reference one string.
DIVERGENCE_MARKER = "DIVERGENCE"


def emit_replication_failure(
    *,
    env: str,
    kind: str,
    summary: str,
    reason: str,
    notify_desktop: bool = True,
) -> None:
    """Surface a replication failure on every channel at once.

    Args:
      env:     "live" | "demo" — which engine; routes to the right GUI
               panel/tab.
      kind:    short tag for display, e.g. "NEW", "MODIFY", "CANCEL",
               "REJECTION" (async broker rejection), "MISMATCH".
      summary: one-line human description (e.g. the order/symbol).
      reason:  the failure reason / broker message.
      notify_desktop: set False to suppress the desktop banner (e.g. when
               the caller has already notified, or for noisy bulk events).

    Never raises — any error inside is logged at debug and swallowed, so
    a broken alert can't break the replication path or a reader thread.
    """
    try:
        payload = {
            "env": env,
            "ts": time.time(),
            "kind": kind,
            "summary": summary,
            "reason": reason,
        }
        # The structured marker line the GUI parses. Logged at ERROR so it
        # also stands out in the Log tab and any file handler.
        logger.error("%s %s", DIVERGENCE_MARKER, json.dumps(payload))

        if notify_desktop:
            env_label = env.upper()
            notify(
                title=f"TradeSynchronizer {env_label}: replication failure",
                message=f"{summary} — {reason}"[:300],
                subtitle=kind,
            )
    except Exception as e:  # noqa: BLE001 - the alert path must never raise
        logger.debug("emit_replication_failure swallowed error: %s", e)
