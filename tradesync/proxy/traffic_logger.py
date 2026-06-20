"""
TrafficLoggerAddon — verbose troubleshooting mode for the proxy.

Registered alongside TradeSyncAddon when VERBOSE_TROUBLESHOOTING
is ON. Logs every HTTP transaction passing through the proxy with
enough detail to reconstruct exactly what TradingView sent and
what the upstream server returned. Three tiers:

  • IBKR endpoints (api.ibkr.com / interactivebrokers.com)
      → FULL log: method, URL, all relevant headers, request body
        pretty-printed JSON or text, response status, response
        body. This is the critical path — we need ground truth
        here so we can verify the ibkr_parser assumptions match
        reality and so we can debug rejections / 4xx / 5xx.
  • Other TradingView-related hosts (charts, push, datafeed)
      → SUMMARY only: method + URL + status + body length. No
        bodies — they're noisy and not useful for replication.
  • Telemetry / analytics / unrelated noise
      → SKIPPED entirely.

The flow id (mitmproxy assigns each pair request↔response the
same id) is prefixed to every line as an 8-char tag so you can
grep the log file to see one transaction's complete trace.

When the user turns VERBOSE_TROUBLESHOOTING OFF, this addon is
NOT registered and the proxy runs silent — back to minimal logs.
"""

from __future__ import annotations

import json
import logging


logger = logging.getLogger("tradesync.traffic")


# Hosts we care DEEPLY about — full request + response bodies.
_IBKR_HOST_PATTERNS = (
    "ibkr.com",
    "interactivebrokers.com",
    "interactivebrokers",
)

# Hosts we want a one-liner for — useful to confirm "yes TV is
# making requests through the proxy" without drowning the log.
_TV_HOST_PATTERNS = (
    "tradingview.com",
    "tradingview-cdn.com",
)

# Hosts we skip entirely — pure noise.
_SKIP_HOST_PATTERNS = (
    "telemetry",
    "analytics",
    "usercentrics",
    "googletagmanager",
    "google-analytics",
    "doubleclick",
    "sentry.io",
    "bugsnag",
)

# Truncate bodies at this many bytes so a single chart-data
# response doesn't blow up the rotating file.
_MAX_BODY_BYTES = 16 * 1024  # 16 KB per body — generous, but bounded


def _classify_host(host: str) -> str:
    """Return one of: 'ibkr' (full log), 'tv' (summary), 'skip'."""
    lower = host.lower()
    if any(p in lower for p in _IBKR_HOST_PATTERNS):
        return "ibkr"
    if any(p in lower for p in _SKIP_HOST_PATTERNS):
        return "skip"
    if any(p in lower for p in _TV_HOST_PATTERNS):
        return "tv"
    # Default: summary. Unknown hosts get one-line treatment.
    return "tv"


def _decode_body(raw: bytes, content_type: str) -> str:
    """Best-effort decode of an HTTP body for log display.
    Truncates to _MAX_BODY_BYTES. Never raises."""
    if not raw:
        return "(empty)"
    ct = (content_type or "").lower()
    # Don't try to log binary content as text.
    if any(b in ct for b in ("image/", "video/", "audio/",
                             "octet-stream", "font/",
                             "application/protobuf",
                             "application/zip", "application/pdf")):
        return f"({len(raw)} bytes of {ct!r}, binary, not shown)"

    truncated = raw[:_MAX_BODY_BYTES]
    try:
        text = truncated.decode("utf-8", errors="replace")
    except Exception as e:  # never happens with errors="replace" but defensive
        return f"({len(raw)} bytes, decode failed: {e})"

    # Pretty-print JSON when it's small enough to be useful.
    if "json" in ct:
        try:
            parsed = json.loads(text)
            pretty = json.dumps(parsed, indent=2, ensure_ascii=False)
            if len(pretty) <= _MAX_BODY_BYTES:
                text = pretty
        except (json.JSONDecodeError, ValueError):
            pass  # fall through to raw text

    suffix = "" if len(raw) <= _MAX_BODY_BYTES else \
        f"\n…(truncated, {len(raw) - _MAX_BODY_BYTES} more bytes)"
    return text + suffix


def _interesting_headers(headers, tier: str) -> str:
    """Pick the few headers actually useful for debugging.
    For 'ibkr' we keep everything except cookies & noise."""
    if tier != "ibkr":
        return ""
    keep = []
    for k, v in headers.items():
        kl = k.lower()
        if kl in ("cookie", "set-cookie", "accept-encoding",
                  "user-agent", "accept-language", "sec-ch-ua",
                  "sec-ch-ua-platform", "sec-ch-ua-mobile",
                  "sec-fetch-site", "sec-fetch-mode",
                  "sec-fetch-dest", "dnt", "accept"):
            continue
        # Truncate very long header values (e.g. session tokens).
        vs = str(v)
        if len(vs) > 200:
            vs = vs[:200] + f"…({len(vs)} chars total)"
        keep.append(f"{k}: {vs}")
    return "\n        ".join(keep)


class TrafficLoggerAddon:
    """
    mitmproxy addon that emits structured DEBUG-level traffic logs.
    Has no side-effects on the flow — it only reads.
    """

    def __init__(self, env_label: str = "?"):
        # env_label is the per-engine tag (LIVE/DEMO) so the
        # traffic logger output interleaves cleanly when both
        # engines run side by side.
        self._env = env_label.upper()

    # ── request hook ─────────────────────────────────────────── #

    def request(self, flow) -> None:
        try:
            host = flow.request.pretty_host
        except Exception:
            return
        tier = _classify_host(host)
        if tier == "skip":
            return

        fid = (flow.id or "")[:8]
        method = flow.request.method
        url = flow.request.pretty_url
        body_len = len(flow.request.raw_content or b"")
        ct = flow.request.headers.get("content-type", "")

        if tier == "tv":
            # One-liner — just confirm the call happened.
            logger.info("TV→ %s %s %s %s (%d bytes %s)",
                        fid, self._env, method, url, body_len, ct or "no-ct")
            return

        # tier == "ibkr": full dump.
        body_preview = _decode_body(flow.request.raw_content or b"", ct)
        headers_str = _interesting_headers(flow.request.headers, tier)
        logger.info(
            "TV→ %s %s [IBKR REQUEST] %s %s\n"
            "    headers:\n        %s\n"
            "    body (%d bytes, %s):\n%s\n"
            "    ──────────────────────────────",
            fid, self._env, method, url,
            headers_str or "(none of interest)",
            body_len, ct or "no-ct",
            _indent(body_preview, 4),
        )

    # ── response hook ────────────────────────────────────────── #

    def response(self, flow) -> None:
        try:
            host = flow.request.pretty_host
        except Exception:
            return
        tier = _classify_host(host)
        if tier == "skip":
            return
        if flow.response is None:
            return

        fid = (flow.id or "")[:8]
        method = flow.request.method
        url = flow.request.pretty_url
        status = flow.response.status_code
        body_len = len(flow.response.raw_content or b"")
        ct = flow.response.headers.get("content-type", "")

        if tier == "tv":
            logger.info("TV← %s %s %d  %s %s (%d bytes %s)",
                        fid, self._env, status, method, url,
                        body_len, ct or "no-ct")
            return

        # tier == "ibkr": full dump.
        body_preview = _decode_body(flow.response.raw_content or b"", ct)
        logger.info(
            "TV← %s %s [IBKR RESPONSE] %d %s %s\n"
            "    body (%d bytes, %s):\n%s\n"
            "    ══════════════════════════════",
            fid, self._env, status, method, url,
            body_len, ct or "no-ct",
            _indent(body_preview, 4),
        )

    # ── error hook (network failures, TLS errors etc.) ───────── #

    def error(self, flow) -> None:
        try:
            host = flow.request.pretty_host
            url = flow.request.pretty_url
        except Exception:
            host, url = "?", "?"
        if _classify_host(host) == "skip":
            return
        fid = (flow.id or "")[:8]
        err = getattr(flow, "error", None)
        logger.warning("TV✗ %s %s flow error on %s — %s",
                       fid, self._env, url, err)


def _indent(text: str, n: int) -> str:
    """Indent every line of `text` by n spaces. Helps the eye scan
    the multi-line body block inside the surrounding log line."""
    pad = " " * n
    return "\n".join(pad + line for line in text.splitlines())
