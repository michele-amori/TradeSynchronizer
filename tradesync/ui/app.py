"""
TradeSynchronizer — Tkinter GUI.

Personal-use desktop wrapper around `main.py`. Each Tradovate
environment (LIVE and DEMO) runs as an independent mitmproxy
subprocess, so the user can have both engines active at the same
time, on different ports, mirroring orders to different Tradovate
LEADER accounts.

UI:
  • Engine cards (header): two status dots + Start/Stop pairs,
    one per environment, fully independent.
  • Tabs:
      - General : shared settings (app metadata, proxy host,
                  replication policy, logging) — active by default
      - Live    : LIVE-only credentials, port, IBKR account
      - Demo    : DEMO-only credentials, port, IBKR account
      - Log     : merged stdout of both engines, lines coloured by
                  env (LIVE = red-ish, DEMO = blue-ish)

Zero external GUI dependencies (stdlib tkinter / ttk only).
"""

from __future__ import annotations

import os
import queue
import re
import signal
import subprocess
import sys
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import font as tkfont
from tkinter import messagebox, ttk
from typing import Optional


# ─────────────────────────────────────────────────────────────────────── #
#  Field declarations — single source of truth for the settings form     #
# ─────────────────────────────────────────────────────────────────────── #

ENVIRONMENTS = ("live", "demo")

# Keys whose values differ between LIVE and DEMO. Everything else in
# the settings file is shared across both engines.
PER_ENV_KEYS = frozenset({
    "TRADOVATE_USERNAME",
    "TRADOVATE_PASSWORD",
    "TRADOVATE_CID",
    "TRADOVATE_SEC",
    "TRADOVATE_ACCOUNT_ID",
    "IBKR_WATCHED_ACCOUNTS",
    "PROXY_LISTEN_PORT",
})

# Per-env defaults for fields where the LIVE and DEMO defaults must
# differ (two processes can't bind the same port).
PER_ENV_DEFAULTS: dict[str, dict[str, str]] = {
    "PROXY_LISTEN_PORT": {"live": "8080", "demo": "8081"},
}

# Tuple shape: (key, label, kind, default, options_or_help)
# kind = text | password | choice | bool | section
GENERAL_FIELDS: list[tuple] = [
    ("__section__", "Tradovate application",  "section",  None, None),
    ("TRADOVATE_APP_ID",      "App ID",        "text",     "TradeSynchronizer", None),
    ("TRADOVATE_APP_VERSION", "App version",   "text",     "1.0", None),

    ("__section__", "Proxy server",            "section",  None, None),
    ("PROXY_LISTEN_HOST", "Listen host", "text", "127.0.0.1",
        "Both engines bind to this host. Ports are configured per engine "
        "in the Live / Demo tabs."),

    ("__section__", "Replication policy",      "section",  None, None),
    ("REPLICATION_MODE",      "Mode",                  "choice", "mirror",
        ["mirror", "market"]),
    ("SKIP_PROTECTIVE_STOPS", "Skip protective stops", "bool",   "true",   None),

    ("__section__", "Logging",                 "section",  None, None),
    ("LOG_LEVEL", "Level", "choice", "INFO",
        ["DEBUG", "INFO", "WARNING", "ERROR"]),
    ("LOG_FILE",  "File",  "text",
        "~/Library/Logs/TradeSynchronizer/tradesync.log",
        "Both engines write here, tagged [LIVE] / [DEMO] for disambiguation. "
        "Rotated automatically at 5 MB (5 backups kept). Path supports ~."),
]

PER_ENV_FIELDS: list[tuple] = [
    ("__section__", "Tradovate account",       "section",  None, None),
    ("TRADOVATE_USERNAME",    "Username",       "text",     "", None),
    ("TRADOVATE_PASSWORD",    "Password",       "password", "", None),
    ("TRADOVATE_CID",         "Client ID (CID)", "text",    "",
        "From Tradovate API Access — string, not number."),
    ("TRADOVATE_SEC",         "API secret",     "password", "", None),
    ("TRADOVATE_ACCOUNT_ID",  "Account ID",     "text",     "",
        "Optional — pins the LEADER account."),

    ("__section__", "Proxy",                   "section",  None, None),
    ("PROXY_LISTEN_PORT", "Listen port", "text", "",
        "Point TradingView's --proxy-server flag at this port to feed "
        "orders to this engine."),

    ("__section__", "IBKR account to mirror",  "section",  None, None),
    ("IBKR_WATCHED_ACCOUNTS", "Watched accounts", "text",   "",
        "Comma-separated IBKR account IDs (e.g. U7713037). Empty = all."),
]


# ─────────────────────────────────────────────────────────────────────── #
#  Three-file .env store                                                  #
# ─────────────────────────────────────────────────────────────────────── #

# Buckets used by snapshot/write to address one file at a time.
SHARED = "shared"
_FILE_BUCKETS = (SHARED, "live", "demo")


class EnvStore:
    """
    In-memory representation of the project's THREE dotenv files:

        .env        — shared settings (proxy host, replication
                      policy, logging, app metadata)
        .env.live   — LIVE-only credentials, port, IBKR watchlist
        .env.demo   — DEMO-only credentials, port, IBKR watchlist

    Layout in memory:

        self.shared    = {"PROXY_LISTEN_HOST": "127.0.0.1", ...}
        self.per_env   = {"live": {"TRADOVATE_USERNAME": "foo", ...},
                          "demo": {"TRADOVATE_USERNAME": "",    ...}}

    The GUI can save just the files that actually changed (targeted
    write) so the two engines never end up touching each other's
    config — modifying DEMO while LIVE is running cannot disturb
    LIVE's file on disk.

    Migration: if .env doesn't exist yet but .env.live or .env.demo
    contains shared keys (legacy from the previous design where
    they were duplicated in both env files), load() still picks the
    shared values up — they migrate to .env on the next Save.
    """

    def __init__(self, project_root: Path):
        self.shared_path: Path = project_root / ".env"
        self.env_paths: dict[str, Path] = {
            env: project_root / f".env.{env}" for env in ENVIRONMENTS
        }
        self.shared:  dict[str, str]            = {}
        self.per_env: dict[str, dict[str, str]] = {e: {} for e in ENVIRONMENTS}

    # ── parsing helper ────────────────────────────────────────────── #

    @staticmethod
    def _parse(path: Path) -> dict[str, str]:
        out: dict[str, str] = {}
        if not path.exists():
            return out
        for line in path.read_text().splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, _, v = s.partition("=")
            out[k.strip()] = v.strip()
        return out

    # ── load ──────────────────────────────────────────────────────── #

    def load(self) -> None:
        """
        Read all three files into memory. Shared keys come from .env
        (authoritative); anything found in .env.live / .env.demo that
        looks shared is treated as legacy and migrated transparently
        (next Save writes it into .env and drops it from the env
        files).
        """
        self.shared = {}
        self.per_env = {e: {} for e in ENVIRONMENTS}

        # 1. Shared file (authoritative for shared keys).
        for k, v in self._parse(self.shared_path).items():
            if k == "TRADOVATE_ENVIRONMENT":
                continue
            if k in PER_ENV_KEYS:
                continue   # shared file shouldn't have per-env keys
            self.shared[k] = v

        # 2. Env-specific files.
        for env in ENVIRONMENTS:
            for k, v in self._parse(self.env_paths[env]).items():
                if k == "TRADOVATE_ENVIRONMENT":
                    continue
                if k in PER_ENV_KEYS:
                    self.per_env[env][k] = v
                else:
                    # Legacy stray shared key in an env file. Use
                    # setdefault so we don't overwrite the canonical
                    # .env value if both exist.
                    self.shared.setdefault(k, v)

    # ── value access ──────────────────────────────────────────────── #

    def get_env(self, env: str, key: str) -> str:
        if key in PER_ENV_KEYS:
            return self.per_env[env].get(key, "")
        return self.shared.get(key, "")

    def set_env(self, env: str, key: str, value: str) -> None:
        if key in PER_ENV_KEYS:
            self.per_env[env][key] = value
        else:
            self.shared[key] = value

    # ── snapshot (per-file, used for dirty tracking) ──────────────── #

    def snapshot_per_file(self) -> dict[str, tuple]:
        snap: dict[str, tuple] = {
            SHARED: tuple(sorted(self.shared.items())),
        }
        for env in ENVIRONMENTS:
            snap[env] = tuple(sorted(self.per_env[env].items()))
        return snap

    # Convenience: combined snapshot, equivalent to old .snapshot().
    def snapshot(self) -> tuple:
        s = self.snapshot_per_file()
        return tuple(s[bucket] for bucket in _FILE_BUCKETS)

    # ── write (targeted) ──────────────────────────────────────────── #

    def write(self, only: set[str] | None = None) -> list[Path]:
        """
        Write the dotenv files. If `only` is None, write all three;
        otherwise write only the named buckets ('shared', 'live',
        'demo'). Returns the list of paths actually written.

        Targeted writes are the heart of environment independence:
        if the user modified only the Demo tab and clicks Save, only
        .env.demo gets touched — .env.live's mtime stays unchanged,
        so a running LIVE engine can't possibly notice anything.
        """
        if only is None:
            only = set(_FILE_BUCKETS)
        written: list[Path] = []
        if SHARED in only:
            self.shared_path.write_text("\n".join(self._build_shared()))
            written.append(self.shared_path)
        for env in ENVIRONMENTS:
            if env in only:
                self.env_paths[env].write_text(
                    "\n".join(self._build_env(env))
                )
                written.append(self.env_paths[env])
        return written

    def _build_shared(self) -> list[str]:
        s = self.shared
        return [
            "# TradeSynchronizer — settings shared by every engine.",
            "# Auto-managed by the GUI's \"General\" tab — feel free to edit by hand.",
            "# Per-environment data (credentials, ports, IBKR watch lists) lives",
            "# in .env.live and .env.demo.",
            "",
            "# ── Tradovate application metadata ──────────────────────────────── #",
            f"TRADOVATE_APP_ID={s.get('TRADOVATE_APP_ID', 'TradeSynchronizer')}",
            f"TRADOVATE_APP_VERSION={s.get('TRADOVATE_APP_VERSION', '1.0')}",
            "",
            "# ── Proxy listen host (ports are per-engine) ────────────────────── #",
            f"PROXY_LISTEN_HOST={s.get('PROXY_LISTEN_HOST', '127.0.0.1')}",
            "",
            "# ── Replication policy ──────────────────────────────────────────── #",
            f"REPLICATION_MODE={s.get('REPLICATION_MODE', 'mirror')}",
            f"SKIP_PROTECTIVE_STOPS={s.get('SKIP_PROTECTIVE_STOPS', 'true')}",
            "",
            "# ── Logging ─────────────────────────────────────────────────────── #",
            f"LOG_LEVEL={s.get('LOG_LEVEL', 'INFO')}",
            f"LOG_FILE={s.get('LOG_FILE', '~/Library/Logs/TradeSynchronizer/tradesync.log')}",
            "",
        ]

    def _build_env(self, env: str) -> list[str]:
        p = self.per_env[env]
        default_port = PER_ENV_DEFAULTS["PROXY_LISTEN_PORT"][env]
        return [
            f"# TradeSynchronizer — {env.upper()} engine private settings.",
            f"# Auto-managed by the GUI's \"{env.capitalize()}\" tab — feel free to edit by hand.",
            "# Shared settings (proxy host, replication, logging) live in .env.",
            "",
            "# ── Tradovate (LEADER account) credentials ─────────────────────── #",
            f"TRADOVATE_USERNAME={p.get('TRADOVATE_USERNAME', '')}",
            f"TRADOVATE_PASSWORD={p.get('TRADOVATE_PASSWORD', '')}",
            f"TRADOVATE_CID={p.get('TRADOVATE_CID', '')}",
            f"TRADOVATE_SEC={p.get('TRADOVATE_SEC', '')}",
            f"TRADOVATE_ACCOUNT_ID={p.get('TRADOVATE_ACCOUNT_ID', '')}",
            "",
            "# ── IBKR accounts to mirror ────────────────────────────────────── #",
            f"IBKR_WATCHED_ACCOUNTS={p.get('IBKR_WATCHED_ACCOUNTS', '')}",
            "",
            "# ── Proxy listen port ──────────────────────────────────────────── #",
            f"PROXY_LISTEN_PORT={p.get('PROXY_LISTEN_PORT', default_port)}",
            "",
        ]


# ─────────────────────────────────────────────────────────────────────── #
#  Subprocess controller                                                  #
# ─────────────────────────────────────────────────────────────────────── #

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

            try:
                self._process = subprocess.Popen(
                    [py, str(main_py)],
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


# ─────────────────────────────────────────────────────────────────────── #
#  UI palette                                                             #
# ─────────────────────────────────────────────────────────────────────── #

_STATE_COLOR = {
    ProxyController.STATE_STOPPED:  "#9aa0a6",
    ProxyController.STATE_STARTING: "#f9ab00",
    ProxyController.STATE_RUNNING:  "#1e8e3e",
    ProxyController.STATE_ERROR:    "#d93025",
}
_STATE_LABEL = {
    ProxyController.STATE_STOPPED:  "Stopped",
    ProxyController.STATE_STARTING: "Starting…",
    ProxyController.STATE_RUNNING:  "Running",
    ProxyController.STATE_ERROR:    "Error",
}

# Log-line colours per env. Light-on-dark to keep readable against the
# dark Log background.
_ENV_LOG_COLOR = {
    "live": "#ff8a80",   # soft red
    "demo": "#82b1ff",   # soft blue
}
_ENV_TAG_RE = {
    env: re.compile(r"\[" + env.upper() + r"\]") for env in ENVIRONMENTS
}


# ─────────────────────────────────────────────────────────────────────── #
#  Main application                                                       #
# ─────────────────────────────────────────────────────────────────────── #

class TradeSyncApp:

    def __init__(self, project_root: Path):
        self.project_root = project_root
        self.store = EnvStore(project_root)
        self.log_q: queue.Queue[str] = queue.Queue(maxsize=40_000)

        # One ProxyController per environment.
        self.controllers: dict[str, ProxyController] = {
            env: ProxyController(env, project_root, self.log_q)
            for env in ENVIRONMENTS
        }

        self.root = tk.Tk()
        self.root.title("TradeSynchronizer")
        self.root.geometry("840x720")
        self.root.minsize(720, 580)

        # Widget storage:
        #   general_widgets[key] → Variable      (General tab)
        #   env_widgets[env][key] → Variable     (Live / Demo tabs)
        self.general_widgets: dict[str, tk.Variable] = {}
        self.env_widgets: dict[str, dict[str, tk.Variable]] = {
            env: {} for env in ENVIRONMENTS
        }
        # Per-env engine-toggle UI slots (one toggle inside each per-
        # env tab; no engine cards in the header anymore).
        self.engine_toggle: dict[str, ttk.Button] = {}
        self.engine_status: dict[str, tuple[tk.Canvas, int, ttk.Label]] = {}

        self._dirty = False
        # Per-file dirty tracking. Keys are 'shared' / 'live' / 'demo'.
        # Allows targeted writes: changing only the Demo tab and saving
        # touches only .env.demo — .env and .env.live stay byte-identical
        # on disk so a running LIVE engine can't be disturbed.
        self._saved_snapshot_per_file: dict[str, tuple] = \
            self.store.snapshot_per_file()
        self._suppress_sync = False

        self._init_style()
        self._build_ui()
        self._load_settings()

        for env, ctrl in self.controllers.items():
            ctrl.on_state_change(
                lambda s, _env=env:
                    self.root.after(0, self._refresh_engine_state, _env, s)
            )
            self._refresh_engine_state(env, ctrl.state)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._drain_log)

    # ── styling ────────────────────────────────────────────────────── #

    def _init_style(self):
        style = ttk.Style()
        if "aqua" in style.theme_names():
            style.theme_use("aqua")
        else:
            style.theme_use("clam")
        style.configure("Section.TLabel", font=("Helvetica", 13, "bold"))
        style.configure("Help.TLabel", foreground="#5f6368")
        style.configure("Status.TLabel", font=("Helvetica", 13))

    # ── layout ─────────────────────────────────────────────────────── #

    def _build_ui(self):
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)

        # Title row.
        title_row = ttk.Frame(outer)
        title_row.pack(fill="x", pady=(0, 8))
        ttk.Label(title_row, text="TradeSynchronizer",
                  font=("Helvetica", 17, "bold")).pack(side="left")
        # Reload / Save in the same row, right-aligned.
        self.save_btn = ttk.Button(title_row, text="Save",
                                   command=self._save_settings)
        self.reload_btn = ttk.Button(title_row, text="Reload",
                                     command=self._load_settings)
        self.save_btn.pack(side="right", padx=(6, 0))
        self.reload_btn.pack(side="right")

        # Tabs: General | Live | Demo | Log
        # Engine on/off toggles live INSIDE the per-env tabs (top of
        # each Live / Demo tab) — see _build_per_env_tab.
        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill="both", expand=True, pady=(8, 0))

        general_tab = ttk.Frame(self.notebook)
        self.notebook.add(general_tab, text="General")
        self._build_general_tab(general_tab)

        self.env_tabs: dict[str, ttk.Frame] = {}
        for env in ENVIRONMENTS:
            tab = ttk.Frame(self.notebook)
            self.notebook.add(tab, text=env.capitalize())
            self.env_tabs[env] = tab
            self._build_per_env_tab(tab, env)

        log_tab = ttk.Frame(self.notebook)
        self.notebook.add(log_tab, text="Log")
        self._build_log_tab(log_tab)

        # General is active by default per the spec.
        self.notebook.select(general_tab)

    def _build_general_tab(self, parent):
        self._render_fields(parent, GENERAL_FIELDS, env=None)

    def _build_per_env_tab(self, parent, env: str):
        """
        Per-env tab layout: an engine toggle panel pinned at the top
        (always visible, no scroll), then the scrollable form for
        that env's settings below.
        """
        panel = self._build_engine_panel(parent, env)
        panel.pack(fill="x", padx=12, pady=(12, 0))

        form_container = ttk.Frame(parent)
        form_container.pack(fill="both", expand=True)
        self._render_fields(form_container, PER_ENV_FIELDS, env=env)

    def _build_engine_panel(self, parent, env: str) -> ttk.LabelFrame:
        """
        The ACTIVE/STOPPED toggle for one environment. Shows a status
        dot + state label + listen port, and a single big button
        that flips between "Start engine" and "Stop engine".
        """
        panel = ttk.LabelFrame(parent, text=f"{env.upper()} engine",
                               padding=10)

        status_row = ttk.Frame(panel)
        status_row.pack(fill="x")
        dot = tk.Canvas(status_row, width=14, height=14,
                        highlightthickness=0, bg=self.root.cget("bg"))
        dot.pack(side="left")
        dot_id = dot.create_oval(2, 2, 12, 12, fill="#9aa0a6", outline="")
        label = ttk.Label(status_row, text="Stopped", style="Status.TLabel")
        label.pack(side="left", padx=(8, 0))
        self.engine_status[env] = (dot, dot_id, label)

        toggle = ttk.Button(
            panel, text="▶  Start engine",
            command=lambda e=env: self._toggle_engine(e),
        )
        toggle.pack(fill="x", pady=(10, 0))
        self.engine_toggle[env] = toggle
        return panel

    def _build_log_tab(self, parent):
        toolbar = ttk.Frame(parent)
        toolbar.pack(fill="x", padx=8, pady=(8, 4))
        self.auto_scroll = tk.BooleanVar(value=True)
        ttk.Checkbutton(toolbar, text="Auto-scroll",
                        variable=self.auto_scroll).pack(side="left")
        ttk.Label(toolbar, text=" • ", foreground="#5f6368").pack(side="left")
        for env in ENVIRONMENTS:
            sw = tk.Canvas(toolbar, width=10, height=10,
                           highlightthickness=0, bg=self.root.cget("bg"))
            sw.create_oval(0, 0, 10, 10, fill=_ENV_LOG_COLOR[env], outline="")
            sw.pack(side="left", padx=(4, 2))
            ttk.Label(toolbar, text=env.upper(),
                      foreground=_ENV_LOG_COLOR[env]).pack(side="left",
                                                            padx=(0, 8))
        ttk.Button(toolbar, text="Clear",
                   command=self._clear_log).pack(side="right")

        text_frame = ttk.Frame(parent)
        text_frame.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        font_mono = tkfont.nametofont("TkFixedFont").copy()
        font_mono.configure(size=11)
        self.log_text = tk.Text(text_frame, wrap="none", state="disabled",
                                font=font_mono, bg="#1e1e1e", fg="#e6e6e6",
                                insertbackground="#e6e6e6",
                                relief="flat", borderwidth=0)
        for env in ENVIRONMENTS:
            self.log_text.tag_configure(f"env-{env}",
                                        foreground=_ENV_LOG_COLOR[env])

        vsb = ttk.Scrollbar(text_frame, orient="vertical",
                            command=self.log_text.yview)
        hsb = ttk.Scrollbar(text_frame, orient="horizontal",
                            command=self.log_text.xview)
        self.log_text.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        text_frame.rowconfigure(0, weight=1)
        text_frame.columnconfigure(0, weight=1)

    def _render_fields(self, parent, fields, env: Optional[str]):
        """
        Build a scrollable form for either General (env=None) or one of
        the per-env tabs (env='live' / 'demo'). Widget references go
        into the appropriate dict: general_widgets or env_widgets[env].
        """
        canvas = tk.Canvas(parent, highlightthickness=0)
        sb = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        inner = ttk.Frame(canvas, padding=12)
        inner_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>",
                   lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfigure(inner_id, width=e.width))
        canvas.bind_all("<MouseWheel>",
                        lambda e: canvas.yview_scroll(
                            int(-1 * (e.delta / 2)), "units"))

        inner.columnconfigure(1, weight=1)
        row = 0
        for key, label, kind, default, opts in fields:
            if kind == "section":
                if row > 0:
                    ttk.Separator(inner, orient="horizontal").grid(
                        row=row, column=0, columnspan=2, sticky="ew",
                        pady=(14, 6))
                    row += 1
                ttk.Label(inner, text=label, style="Section.TLabel").grid(
                    row=row, column=0, columnspan=2, sticky="w", pady=(0, 6))
                row += 1
                continue

            ttk.Label(inner, text=label + ":").grid(
                row=row, column=0, sticky="w", padx=(0, 12), pady=4)

            # Per-env default override (e.g. PROXY_LISTEN_PORT).
            actual_default = default
            if env and key in PER_ENV_DEFAULTS:
                actual_default = PER_ENV_DEFAULTS[key].get(env, default) or ""

            if kind == "bool":
                var: tk.Variable = tk.BooleanVar(
                    value=str(actual_default).lower() == "true")
                w = ttk.Checkbutton(inner, variable=var,
                                    command=self._on_widget_change)
                w.grid(row=row, column=1, sticky="w", pady=4)
            elif kind == "choice":
                var = tk.StringVar(value=actual_default)
                w = ttk.Combobox(inner, textvariable=var, values=opts,
                                 state="readonly", width=14)
                w.grid(row=row, column=1, sticky="w", pady=4)
                w.bind("<<ComboboxSelected>>",
                       lambda _e: self._on_widget_change())
            elif kind == "password":
                var = tk.StringVar(value=actual_default)
                w = ttk.Entry(inner, textvariable=var, show="•")
                w.grid(row=row, column=1, sticky="ew", pady=4)
                var.trace_add("write", lambda *_a: self._on_widget_change())
            else:
                var = tk.StringVar(value=actual_default)
                w = ttk.Entry(inner, textvariable=var)
                w.grid(row=row, column=1, sticky="ew", pady=4)
                var.trace_add("write", lambda *_a: self._on_widget_change())

            if env is None:
                self.general_widgets[key] = var
            else:
                self.env_widgets[env][key] = var
            row += 1

            help_text = opts if isinstance(opts, str) else None
            if help_text:
                ttk.Label(inner, text="↪ " + help_text,
                          style="Help.TLabel",
                          wraplength=540, justify="left").grid(
                    row=row, column=1, sticky="w", pady=(0, 4))
                row += 1

    # ── settings I/O ──────────────────────────────────────────────── #

    def _load_settings(self):
        self.store.load()
        self._populate_form_from_store()
        self._saved_snapshot_per_file = self.store.snapshot_per_file()
        self._dirty = False
        self._refresh_save_button()

    def _dirty_files(self) -> set[str]:
        """Return which of {'shared', 'live', 'demo'} have unsaved
        changes in the form, by comparing the current store state
        against the snapshot taken at the last load/save."""
        self._flush_widgets_to_store()
        now = self.store.snapshot_per_file()
        return {bucket for bucket, snap in now.items()
                if snap != self._saved_snapshot_per_file.get(bucket)}

    def _save_settings(self):
        dirty = self._dirty_files()
        if not dirty:
            self._append_log("⚙️  Nothing to save.\n")
            return
        err = self._validate_all()
        if err:
            messagebox.showerror("Invalid settings", err)
            return
        try:
            written = self.store.write(only=dirty)
        except OSError as e:
            messagebox.showerror(
                "Save failed",
                f"Could not write .env files: {e}",
            )
            return
        self._saved_snapshot_per_file = self.store.snapshot_per_file()
        self._dirty = False
        self._refresh_save_button()
        paths = ", ".join(p.name for p in written)
        self._append_log(f"⚙️  Saved {paths}\n")
        # If the user just rewrote a file whose engine is currently
        # running, warn that the change won't take effect until the
        # engine restarts.
        for env in ENVIRONMENTS:
            if env in dirty and self.controllers[env].state in (
                ProxyController.STATE_STARTING,
                ProxyController.STATE_RUNNING,
            ):
                self._append_log(
                    f"   ↪ {env.upper()} engine is running with the "
                    f"previous values — stop & start to apply.\n"
                )
        if SHARED in dirty:
            running = [env for env in ENVIRONMENTS
                       if self.controllers[env].state in (
                           ProxyController.STATE_STARTING,
                           ProxyController.STATE_RUNNING)]
            if running:
                self._append_log(
                    f"   ↪ shared settings changed — restart "
                    f"{', '.join(e.upper() for e in running)} to apply.\n"
                )

    def _validate_all(self) -> Optional[str]:
        """
        Validate every field that can be sanity-checked without
        contacting a remote (missing required, bad port range, bad
        port collision). Credential emptiness is allowed if that env's
        engine is never started; it'll surface as a more specific
        error in _validate_env when the user actually clicks Start.
        """
        port_seen: dict[str, int] = {}
        for env in ENVIRONMENTS:
            port_raw = self.store.per_env[env].get(
                "PROXY_LISTEN_PORT",
                PER_ENV_DEFAULTS["PROXY_LISTEN_PORT"][env],
            )
            try:
                p = int(port_raw)
                if not (1 <= p <= 65535):
                    raise ValueError
            except ValueError:
                return (f"{env.upper()}: listen port must be "
                        f"an integer 1-65535, got '{port_raw}'.")
            if p in port_seen:
                return (f"Port {p} is configured for both "
                        f"{port_seen[p].upper()} and {env.upper()} engines. "
                        f"Use distinct ports.")
            port_seen[p] = env
        return None

    def _validate_env(self, env: str) -> Optional[str]:
        """Tighter validation, run before starting a specific engine."""
        required = ["TRADOVATE_USERNAME", "TRADOVATE_PASSWORD",
                    "TRADOVATE_CID", "TRADOVATE_SEC"]
        missing = [k for k in required
                   if not self.store.per_env[env].get(k)]
        if missing:
            return (f"{env.upper()} engine cannot start — missing: "
                    + ", ".join(missing) +
                    f". Fill them in the '{env.capitalize()}' tab.")
        return self._validate_all()

    # ── store ↔ widgets sync ──────────────────────────────────────── #

    def _populate_form_from_store(self):
        self._suppress_sync = True
        try:
            for key, var in self.general_widgets.items():
                value = self.store.shared.get(key, "")
                if not value:
                    field = next((f for f in GENERAL_FIELDS if f[0] == key), None)
                    if field:
                        value = field[3] or ""
                self._set_var(var, key, value, fields=GENERAL_FIELDS)
            for env in ENVIRONMENTS:
                for key, var in self.env_widgets[env].items():
                    value = self.store.per_env[env].get(key, "")
                    if not value:
                        if key in PER_ENV_DEFAULTS:
                            value = PER_ENV_DEFAULTS[key].get(env, "")
                        if not value:
                            field = next((f for f in PER_ENV_FIELDS
                                         if f[0] == key), None)
                            if field:
                                value = field[3] or ""
                    self._set_var(var, key, value, fields=PER_ENV_FIELDS)
        finally:
            self._suppress_sync = False

    def _set_var(self, var: tk.Variable, key: str, value: str, *, fields):
        field = next((f for f in fields if f[0] == key), None)
        kind = field[2] if field else "text"
        if kind == "bool":
            var.set(str(value).strip().lower() in ("1", "true", "yes", "on"))
        else:
            var.set(value)

    def _flush_widgets_to_store(self):
        for key, var in self.general_widgets.items():
            v = var.get()
            value = ("true" if v else "false") if isinstance(v, bool) \
                else str(v).strip()
            self.store.shared[key] = value
        for env in ENVIRONMENTS:
            for key, var in self.env_widgets[env].items():
                v = var.get()
                value = ("true" if v else "false") if isinstance(v, bool) \
                    else str(v).strip()
                self.store.per_env[env][key] = value

    # ── widget callbacks ──────────────────────────────────────────── #

    def _on_widget_change(self, *_):
        if self._suppress_sync:
            return
        is_dirty = bool(self._dirty_files())
        if is_dirty != self._dirty:
            self._dirty = is_dirty
            self._refresh_save_button()

    def _refresh_save_button(self):
        self.save_btn.configure(text="Save *" if self._dirty else "Save")

    # ── controller events ─────────────────────────────────────────── #

    def _toggle_engine(self, env: str):
        """Single entry point for the ACTIVE/STOPPED button in each
        per-env tab. Decides whether to start or stop based on the
        current state of THAT env's controller — completely
        independent of the other env."""
        state = self.controllers[env].state
        if state in (ProxyController.STATE_STARTING,
                     ProxyController.STATE_RUNNING):
            self._on_stop(env)
        else:
            self._on_start(env)

    def _on_start(self, env: str):
        self._flush_widgets_to_store()
        err = self._validate_env(env)
        if err:
            messagebox.showerror("Cannot start", err)
            return
        # Save only the files affected by the user's pending edits;
        # we never touch the OTHER engine's file, even if that env's
        # widgets happen to be dirty too — independence is the rule.
        dirty = self._dirty_files()
        if dirty:
            if not messagebox.askyesno(
                "Unsaved changes",
                "You have unsaved changes. Save them first?",
            ):
                return
            self._save_settings()
            if self._dirty:
                return

        port = self.store.per_env[env].get(
            "PROXY_LISTEN_PORT",
            PER_ENV_DEFAULTS["PROXY_LISTEN_PORT"][env],
        )
        env_overrides = {
            "TRADOVATE_ENVIRONMENT": env,
            "PROXY_LISTEN_PORT": port,
        }
        err = self.controllers[env].start(env_overrides=env_overrides)
        if err:
            messagebox.showerror(f"Cannot start {env.upper()}", err)

    def _on_stop(self, env: str):
        self.controllers[env].stop()

    def _refresh_engine_state(self, env: str, state: str):
        # Update the per-env tab's status dot + label.
        dot, dot_id, label = self.engine_status[env]
        dot.itemconfigure(dot_id, fill=_STATE_COLOR[state])
        port = self.store.per_env[env].get(
            "PROXY_LISTEN_PORT",
            PER_ENV_DEFAULTS["PROXY_LISTEN_PORT"][env],
        )
        label.configure(text=f"{_STATE_LABEL[state]}  :{port}")

        # Flip the toggle button between Start ↔ Stop based on state.
        # Disable it only briefly while STARTING (to prevent a
        # second click during the spawn race); STOP is always usable
        # from STARTING/RUNNING.
        toggle = self.engine_toggle[env]
        is_active = state in (ProxyController.STATE_STARTING,
                              ProxyController.STATE_RUNNING)
        if state == ProxyController.STATE_STARTING:
            toggle.configure(text="…  Starting", state="disabled")
        elif state == ProxyController.STATE_RUNNING:
            toggle.configure(text="■  Stop engine", state="normal")
        else:
            toggle.configure(text="▶  Start engine", state="normal")

        # At-a-glance: stamp a dot next to the env's tab title when
        # active, so the user can see status without switching tabs.
        if env in self.env_tabs:
            base = env.capitalize()
            self.notebook.tab(
                self.env_tabs[env],
                text=(f"{base}  ●" if is_active else base),
            )

    # ── log streaming ─────────────────────────────────────────────── #

    def _drain_log(self):
        drained = 0
        try:
            while drained < 200:
                line = self.log_q.get_nowait()
                self._append_log(line)
                drained += 1
        except queue.Empty:
            pass
        self.root.after(100, self._drain_log)

    def _append_log(self, line: str):
        self.log_text.configure(state="normal")
        start = self.log_text.index("end-1c")
        self.log_text.insert("end", line)
        end = self.log_text.index("end-1c")
        # Tint the line based on which env's tag appears in it.
        for env, pat in _ENV_TAG_RE.items():
            if pat.search(line):
                self.log_text.tag_add(f"env-{env}", start, end)
                break
        if self.auto_scroll.get():
            self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    # ── lifecycle ─────────────────────────────────────────────────── #

    def _any_engine_running(self) -> bool:
        return any(
            c.state in (ProxyController.STATE_STARTING,
                        ProxyController.STATE_RUNNING)
            for c in self.controllers.values()
        )

    def _on_close(self):
        if self._any_engine_running():
            if not messagebox.askyesno(
                "Engines running",
                "One or more engines are running. Stop them and quit?",
            ):
                return
        for c in self.controllers.values():
            c.stop()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


# ─────────────────────────────────────────────────────────────────────── #
#  Entry point                                                            #
# ─────────────────────────────────────────────────────────────────────── #

def main():
    project_root = Path(__file__).resolve().parent.parent.parent
    TradeSyncApp(project_root).run()


if __name__ == "__main__":
    main()
