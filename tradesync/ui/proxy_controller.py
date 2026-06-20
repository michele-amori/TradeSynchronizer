"""
proxy_controller — spawns and supervises one `python main.py` engine
subprocess per environment, piping its stdout into a thread-safe queue.

Extracted from ui/app.py to keep subprocess lifecycle management
separate from the Tkinter presentation layer (and unit-testable without
a display). The GUI imports ProxyController from here; app.py re-exports
it for backward compatibility with existing imports.
"""

from __future__ import annotations

import os
import queue
import re
import signal
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

# Re-exported (and used below) under the old private name so existing
# imports — notably ui/app.py's back-compat re-export — keep working.
from ..platform_util import (  # noqa: F401
    is_apple_silicon_hardware as _is_apple_silicon_hardware,
)


class ProxyController:
    """
    Spawns `python main.py` and pipes its stdout into a thread-safe
    queue. One instance per environment.

    States:
        stopped   — no child process running
        starting  — process spawned, listening line not yet seen
        running   — proxy has bound its port
        error     — process exited with a non-zero status
    """

    STATE_STOPPED  = "stopped"
    STATE_STARTING = "starting"
    STATE_RUNNING  = "running"
    STATE_ERROR    = "error"

    _RUNNING_MARKER = re.compile(r"mitmproxy listening on", re.IGNORECASE)

    def __init__(self, env: str, project_root: Path,
                 log_q: "queue.Queue[str]"):
        self.env = env
        self.project_root = project_root
        self.log_q = log_q
        self._process: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._state = self.STATE_STOPPED
        self._state_cbs: list = []
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        return self._state

    def on_state_change(self, cb) -> None:
        self._state_cbs.append(cb)

    def start(self, env_overrides: Optional[dict[str, str]] = None) -> Optional[str]:
        with self._lock:
            if self._process and self._process.poll() is None:
                return "Already running."
            main_py = self.project_root / "main.py"
            if not main_py.exists():
                return f"main.py not found at {main_py}"
            py = self._resolve_python()
            if not py:
                return "No Python interpreter found."

            proc_env = {
                **os.environ,
                "PYTHONUNBUFFERED": "1",
                **(env_overrides or {}),
            }

            self._enqueue(
                f"──── {self.env.upper()} starting at "
                f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ────\n"
            )

            # On Apple Silicon, force the engine subprocess to launch
            # under arm64 even if macOS picked the x86_64 slice of the
            # universal Python framework for THIS process (i.e. THIS
            # GUI is itself running under Rosetta). Without this fix
            # the subprocess inherits x86_64 and dies on first
            # `from mitmproxy import http` with:
            # "(mach-o file, but is an incompatible architecture
            #  (have 'arm64', need 'x86_64'))".
            #
            # IMPORTANT: detect Apple Silicon via sysctl, not via
            # platform.machine(). When this Python interpreter is
            # itself running under Rosetta, platform.machine() and
            # os.uname().machine BOTH return "x86_64" — they reflect
            # the process arch, not the hardware. sysctl
            # hw.optional.arm64 always returns "1" on Apple Silicon
            # regardless of caller arch.
            argv: list[str] = [py, str(main_py)]
            if sys.platform == "darwin" and _is_apple_silicon_hardware():
                argv = ["/usr/bin/arch", "-arm64"] + argv

            try:
                self._process = subprocess.Popen(
                    argv,
                    cwd=str(self.project_root),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    bufsize=1,
                    text=True,
                    env=proc_env,
                )
            except OSError as e:
                return f"Failed to spawn: {e}"

            self._set_state(self.STATE_STARTING)
            self._reader = threading.Thread(target=self._read_loop, daemon=True)
            self._reader.start()
        return None

    def stop(self) -> None:
        with self._lock:
            proc, self._process = self._process, None
        if proc is None or proc.poll() is not None:
            self._set_state(self.STATE_STOPPED)
            return
        try:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._enqueue(
                    f"[{self.env.upper()}] SIGTERM ignored — sending SIGKILL\n"
                )
                proc.kill()
                proc.wait()
        except Exception as e:
            self._enqueue(f"[{self.env.upper()}] stop error: {e}\n")
        self._set_state(self.STATE_STOPPED)

    def _read_loop(self) -> None:
        proc = self._process
        if proc is None or proc.stdout is None:
            return
        try:
            for line in proc.stdout:
                # main.py already inserts [LIVE] / [DEMO] via its log
                # format, so we forward the line verbatim. For lines
                # that DON'T come from the logger (rare: crashes,
                # mitmproxy internals), we tag them ourselves so they
                # don't appear unattributable in the merged Log tab.
                tag = f"[{self.env.upper()}]"
                if tag not in line:
                    line = f"{tag} {line}"
                self._enqueue(line)
                if (self._state == self.STATE_STARTING
                        and self._RUNNING_MARKER.search(line)):
                    self._set_state(self.STATE_RUNNING)
        except Exception as e:
            self._enqueue(f"[{self.env.upper()}] reader error: {e}\n")
        rc = proc.wait()
        with self._lock:
            still_ours = self._process is proc
        if still_ours:
            if rc in (0, -signal.SIGTERM):
                self._set_state(self.STATE_STOPPED)
            else:
                self._enqueue(
                    f"[{self.env.upper()}] process exited with rc={rc}\n"
                )
                self._set_state(self.STATE_ERROR)
            with self._lock:
                self._process = None

    def _set_state(self, new: str) -> None:
        if new == self._state:
            return
        self._state = new
        for cb in list(self._state_cbs):
            try:
                cb(new)
            except Exception:
                pass

    def _enqueue(self, line: str) -> None:
        try:
            self.log_q.put_nowait(line)
        except queue.Full:
            try:
                self.log_q.get_nowait()
                self.log_q.put_nowait(line)
            except Exception:
                pass

    def _resolve_python(self) -> Optional[str]:
        candidates = [
            self.project_root / ".venv" / "bin" / "python",
            self.project_root / ".venv" / "bin" / "python3",
            self.project_root / "venv" / "bin" / "python",
        ]
        for c in candidates:
            if c.exists():
                return str(c)
        return sys.executable
