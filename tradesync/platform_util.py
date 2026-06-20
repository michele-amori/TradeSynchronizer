"""
platform_util — small, dependency-free platform probes shared across
the codebase.

Kept deliberately minimal and import-light (only stdlib) so any module
— including the UI-free engine path and the TradingView launcher — can
import it without dragging in heavier packages.
"""

from __future__ import annotations

import subprocess
import sys


def is_apple_silicon_hardware() -> bool:
    """Return True iff the underlying CPU is Apple Silicon.

    Unlike platform.machine() / os.uname().machine, this detection is
    robust against the caller running under Rosetta translation (which
    makes both of those return "x86_64" even on M1/M2/M3 hardware). The
    kernel sysctl hw.optional.arm64 always returns "1" on Apple Silicon
    regardless of caller arch.
    """
    if sys.platform != "darwin":
        return False
    try:
        out = subprocess.check_output(
            ["/usr/sbin/sysctl", "-n", "hw.optional.arm64"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
        return out == "1"
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return False
