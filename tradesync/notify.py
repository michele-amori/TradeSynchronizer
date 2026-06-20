"""
Desktop notifications via macOS `osascript`.

Fire-and-forget: every call spawns `osascript -e 'display notification …'`
in the background and returns immediately. Failures (osascript missing,
non-Darwin host, whatever) are swallowed silently — a missed
notification must never propagate into the replication path.

The notification banner appears in the macOS Notification Center
with the app name "TradeSynchronizer" — so the user can review
recent rejections after the fact even if they were AFK.
"""

from __future__ import annotations

import logging
import platform
import subprocess


logger = logging.getLogger("tradesync.notify")


def _escape_applescript(s: str) -> str:
    """Escape a Python string so it can be safely embedded inside an
    AppleScript double-quoted literal. AppleScript treats `\\` and
    `"` specially; newlines also break the one-liner form."""
    return (
        s.replace("\\", "\\\\")
         .replace('"', '\\"')
         .replace("\n", " ")
         .replace("\r", " ")
    )


def notify(title: str, message: str, *, subtitle: str = "") -> bool:
    """
    Show a macOS desktop notification.

    Returns True on success (the process was spawned), False otherwise.
    Never raises — designed to be safe inside a worker thread that
    can't afford to be interrupted by I/O errors.
    """
    if platform.system() != "Darwin":
        # No-op on other OSes. We don't speculate about Linux's
        # notify-send vs Windows' toast notifications — that's a
        # later enhancement if anyone ever needs it.
        return False

    # Truncate to AppleScript's practical limits to avoid surprises.
    title_e    = _escape_applescript(title)[:120]
    message_e  = _escape_applescript(message)[:300]
    subtitle_e = _escape_applescript(subtitle)[:120]

    script = f'display notification "{message_e}" with title "{title_e}"'
    if subtitle_e:
        script += f' subtitle "{subtitle_e}"'

    try:
        # Popen with discarded streams = fire-and-forget. osascript
        # typically returns within ~100 ms; we don't wait.
        subprocess.Popen(
            ["osascript", "-e", script],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except (OSError, FileNotFoundError) as e:
        logger.debug("osascript notification failed: %s", e)
        return False
