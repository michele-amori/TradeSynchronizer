"""
TradeSynchronizer — Tkinter GUI.

A minimal personal-use desktop wrapper around `main.py`:
  • Settings tab — edit and save the .env file
  • Log tab      — live tail of the proxy's stdout
  • Start / Stop — spawn / terminate the main.py subprocess

The settings store is **environment-aware**: credentials and the
IBKR-account whitelist are kept separately for `live` and `demo`,
both in memory and on disk (via `_LIVE` / `_DEMO` suffixed keys in
.env). Toggling the Environment dropdown swaps the visible form
contents without touching the other set, so flipping demo ↔ live
preserves both credentials sets.

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

# Keys whose values differ between the live and demo environments.
# Everything else in _FIELDS is shared across both.
PER_ENV_KEYS = frozenset({
    "TRADOVATE_USERNAME",
    "TRADOVATE_PASSWORD",
    "TRADOVATE_CID",
    "TRADOVATE_SEC",
    "TRADOVATE_ACCOUNT_ID",
    "IBKR_WATCHED_ACCOUNTS",
})

# Recognised environment names.
ENVIRONMENTS = ("demo", "live")

# (key, label, kind, default, options_or_help)
# kind: text | password | choice | bool | section
# The Environment dropdown is placed at the very top of the Tradovate
# section because every credential field below depends on it.
_FIELDS: list[tuple] = [
    ("__section__", "Tradovate Account",       "section",  None, None),
    ("TRADOVATE_ENVIRONMENT", "Environment",   "choice",   "demo", list(ENVIRONMENTS)),
    ("TRADOVATE_USERNAME",    "Username",       "text",     "", None),
    ("TRADOVATE_PASSWORD",    "Password",       "password", "", None),
    ("TRADOVATE_APP_ID",      "App ID",         "text",     "TradeSynchronizer", None),
    ("TRADOVATE_APP_VERSION", "App version",    "text",     "1.0", None),
    ("TRADOVATE_CID",         "Client ID (CID)", "text",    "",
        "From Tradovate API Access — string, not number."),
    ("TRADOVATE_SEC",         "API secret",     "password", "", None),
    ("TRADOVATE_ACCOUNT_ID",  "Account ID",     "text",     "",
        "Optional — pins the LEADER account."),

    ("__section__", "Proxy server",             "section",  None, None),
    ("PROXY_LISTEN_HOST", "Listen host", "text", "127.0.0.1", None),
    ("PROXY_LISTEN_PORT", "Listen port", "text", "8080",      None),

    ("__section__", "Replication policy",       "section",  None, None),
    ("REPLICATION_MODE",      "Mode",                  "choice", "mirror",
        list(("mirror", "market"))),
    ("SKIP_PROTECTIVE_STOPS", "Skip protective stops", "bool",   "true", None),
    ("IBKR_WATCHED_ACCOUNTS", "Watched IBKR accounts", "text",   "",
        "Comma-separated. Empty = all."),

    ("__section__", "Logging",                  "section",  None, None),
    ("LOG_LEVEL", "Level", "choice", "INFO",
        ["DEBUG", "INFO", "WARNING", "ERROR"]),
    ("LOG_FILE",  "File",  "text",   "/tmp/tradesync.log", None),
]


# ─────────────────────────────────────────────────────────────────────── #
#  Environment-aware .env store                                           #
# ─────────────────────────────────────────────────────────────────────── #

class EnvStore:
    """
    In-memory representation of an environment-aware .env file.

    Layout on disk:

        TRADOVATE_ENVIRONMENT=live          # active env selector
        TRADOVATE_APP_ID=TradeSynchronizer  # shared
        TRADOVATE_USERNAME_LIVE=foo         # per-env: live
        TRADOVATE_USERNAME_DEMO=            # per-env: demo
        ...

    Layout in memory:

        self.shared    = {"TRADOVATE_APP_ID": "TradeSynchronizer", ...}
        self.per_env   = {"live": {"TRADOVATE_USERNAME": "foo", ...},
                          "demo": {"TRADOVATE_USERNAME": "",    ...}}
        self.active_env = "live"

    Legacy .env files (no _LIVE / _DEMO suffixes) are auto-migrated:
    on `load()` the unsuffixed values go into whatever env is active.
    On the next `write()` the file is re-emitted in the suffixed
    format, so legacy ⇒ new is a one-way transition triggered by
    the first Save in the GUI.
    """

    def __init__(self, env_path: Path, template_path: Optional[Path] = None):
        self.env_path = env_path
        self.template_path = template_path
        self.shared:  dict[str, str]               = {}
        self.per_env: dict[str, dict[str, str]]    = {e: {} for e in ENVIRONMENTS}
        self.active_env: str = "demo"

    # ── load ──────────────────────────────────────────────────────── #

    def load(self) -> None:
        # Reset before re-reading.
        self.shared = {}
        self.per_env = {e: {} for e in ENVIRONMENTS}
        self.active_env = "demo"

        source = self.env_path if self.env_path.exists() else self.template_path
        if source is None or not source.exists():
            return

        legacy: dict[str, str] = {}
        active_seen: Optional[str] = None

        for line in source.read_text().splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, _, v = s.partition("=")
            k, v = k.strip(), v.strip()

            if k == "TRADOVATE_ENVIRONMENT":
                lo = v.lower()
                if lo in ENVIRONMENTS:
                    active_seen = lo
                continue

            # Suffixed per-env keys take precedence.
            matched_suffix = False
            for env in ENVIRONMENTS:
                suf = "_" + env.upper()
                if k.endswith(suf):
                    base = k[: -len(suf)]
                    if base in PER_ENV_KEYS:
                        self.per_env[env][base] = v
                        matched_suffix = True
                        break
            if matched_suffix:
                continue

            # Legacy unsuffixed per-env keys: stash until we know the
            # active env, then assign there as a fallback.
            if k in PER_ENV_KEYS:
                legacy[k] = v
                continue

            # Everything else is shared.
            self.shared[k] = v

        if active_seen is not None:
            self.active_env = active_seen

        # Migrate legacy: only fill keys that weren't already covered
        # by a suffixed value (suffixed wins).
        for k, v in legacy.items():
            self.per_env[self.active_env].setdefault(k, v)

    # ── value access ──────────────────────────────────────────────── #

    def get(self, key: str) -> str:
        if key == "TRADOVATE_ENVIRONMENT":
            return self.active_env
        if key in PER_ENV_KEYS:
            return self.per_env[self.active_env].get(key, "")
        return self.shared.get(key, "")

    def set(self, key: str, value: str) -> None:
        if key == "TRADOVATE_ENVIRONMENT":
            lo = value.lower()
            if lo in ENVIRONMENTS:
                self.active_env = lo
            return
        if key in PER_ENV_KEYS:
            self.per_env[self.active_env][key] = value
            return
        self.shared[key] = value

    def snapshot(self) -> tuple:
        """Hashable snapshot of the entire store state — used for
        dirty-flag tracking by the GUI."""
        return (
            self.active_env,
            tuple(sorted(self.shared.items())),
            tuple((env, tuple(sorted(self.per_env[env].items())))
                  for env in ENVIRONMENTS),
        )

    # ── write ─────────────────────────────────────────────────────── #

    def write(self) -> None:
        """Re-emit .env in the canonical suffixed layout."""
        lines: list[str] = [
            "# TradeSynchronizer configuration.",
            "# Auto-managed by the GUI — feel free to edit by hand.",
            "",
            "# ── Active environment ─────────────────────────────────────── #",
            "# 'demo' or 'live' — selects which credentials below are used.",
            f"TRADOVATE_ENVIRONMENT={self.active_env}",
            "",
            "# ── Tradovate app metadata (shared across environments) ────── #",
            f"TRADOVATE_APP_ID={self.shared.get('TRADOVATE_APP_ID', 'TradeSynchronizer')}",
            f"TRADOVATE_APP_VERSION={self.shared.get('TRADOVATE_APP_VERSION', '1.0')}",
        ]
        for env in ENVIRONMENTS:
            lines += [
                "",
                f"# ── {env.upper()} credentials ──────────────────────────────────────── #",
            ]
            for k in ("TRADOVATE_USERNAME", "TRADOVATE_PASSWORD",
                      "TRADOVATE_CID", "TRADOVATE_SEC",
                      "TRADOVATE_ACCOUNT_ID", "IBKR_WATCHED_ACCOUNTS"):
                lines.append(f"{k}_{env.upper()}={self.per_env[env].get(k, '')}")

        lines += [
            "",
            "# ── Proxy server ───────────────────────────────────────────── #",
            f"PROXY_LISTEN_HOST={self.shared.get('PROXY_LISTEN_HOST', '127.0.0.1')}",
            f"PROXY_LISTEN_PORT={self.shared.get('PROXY_LISTEN_PORT', '8080')}",
            "",
            "# ── Replication policy ─────────────────────────────────────── #",
            f"REPLICATION_MODE={self.shared.get('REPLICATION_MODE', 'mirror')}",
            f"SKIP_PROTECTIVE_STOPS={self.shared.get('SKIP_PROTECTIVE_STOPS', 'true')}",
            "",
            "# ── Logging ────────────────────────────────────────────────── #",
            f"LOG_LEVEL={self.shared.get('LOG_LEVEL', 'INFO')}",
            f"LOG_FILE={self.shared.get('LOG_FILE', '/tmp/tradesync.log')}",
            "",
        ]
        self.env_path.write_text("\n".join(lines))


# ─────────────────────────────────────────────────────────────────────── #
#  Subprocess controller                                                  #
# ─────────────────────────────────────────────────────────────────────── #

class ProxyController:
    """
    Spawns `python main.py` and pipes its stdout into a thread-safe
    queue consumed by the UI's main loop. Manages lifecycle state.

    States:
        stopped   — no child process running
        starting  — process spawned, listening line not yet seen
        running   — proxy has bound its port (mitmproxy "listening on" log)
        error     — process exited with a non-zero status
    """

    STATE_STOPPED  = "stopped"
    STATE_STARTING = "starting"
    STATE_RUNNING  = "running"
    STATE_ERROR    = "error"

    _RUNNING_MARKER = re.compile(r"mitmproxy listening on", re.IGNORECASE)

    def __init__(self, project_root: Path, log_q: "queue.Queue[str]"):
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

    def start(self) -> Optional[str]:
        with self._lock:
            if self._process and self._process.poll() is None:
                return "Already running."
            main_py = self.project_root / "main.py"
            if not main_py.exists():
                return f"main.py not found at {main_py}"
            py = self._resolve_python()
            if not py:
                return "No Python interpreter found."

            self._enqueue(
                f"\n──── started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ────\n"
            )
            self._enqueue(f"Using interpreter: {py}\n")

            try:
                self._process = subprocess.Popen(
                    [py, str(main_py)],
                    cwd=str(self.project_root),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    bufsize=1,
                    text=True,
                    env={**os.environ, "PYTHONUNBUFFERED": "1"},
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
                self._enqueue("[controller] SIGTERM ignored — sending SIGKILL\n")
                proc.kill()
                proc.wait()
        except Exception as e:
            self._enqueue(f"[controller] stop error: {e}\n")
        self._set_state(self.STATE_STOPPED)

    def _read_loop(self) -> None:
        proc = self._process
        if proc is None or proc.stdout is None:
            return
        try:
            for line in proc.stdout:
                self._enqueue(line)
                if (self._state == self.STATE_STARTING
                        and self._RUNNING_MARKER.search(line)):
                    self._set_state(self.STATE_RUNNING)
        except Exception as e:
            self._enqueue(f"[reader] {e}\n")
        rc = proc.wait()
        with self._lock:
            still_ours = self._process is proc
        if still_ours:
            if rc in (0, -signal.SIGTERM):
                self._set_state(self.STATE_STOPPED)
            else:
                self._enqueue(f"[controller] process exited with rc={rc}\n")
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
    ProxyController.STATE_STOPPED:  "#9aa0a6",   # gray
    ProxyController.STATE_STARTING: "#f9ab00",   # amber
    ProxyController.STATE_RUNNING:  "#1e8e3e",   # green
    ProxyController.STATE_ERROR:    "#d93025",   # red
}
_STATE_LABEL = {
    ProxyController.STATE_STOPPED:  "Stopped",
    ProxyController.STATE_STARTING: "Starting…",
    ProxyController.STATE_RUNNING:  "Running",
    ProxyController.STATE_ERROR:    "Error",
}


# ─────────────────────────────────────────────────────────────────────── #
#  Main application                                                       #
# ─────────────────────────────────────────────────────────────────────── #

class TradeSyncApp:

    def __init__(self, project_root: Path):
        self.project_root = project_root
        self.store = EnvStore(
            env_path=project_root / ".env",
            template_path=project_root / ".env.example",
        )
        self.log_q: queue.Queue[str] = queue.Queue(maxsize=20_000)
        self.controller = ProxyController(project_root, self.log_q)

        self.root = tk.Tk()
        self.root.title("TradeSynchronizer")
        self.root.geometry("780x640")
        self.root.minsize(640, 520)

        self.widgets: dict[str, tk.Variable] = {}
        self._dirty = False
        self._saved_snapshot: tuple = self.store.snapshot()
        # Tracks the env currently displayed in the form, used to detect
        # the OLD env when the dropdown changes.
        self._displayed_env: str = self.store.active_env
        # When True, widget writes do NOT flow back into the store (used
        # while we're repopulating the form programmatically).
        self._suppress_sync = False

        self._init_style()
        self._build_ui()
        self._load_settings()

        self.controller.on_state_change(
            lambda s: self.root.after(0, self._refresh_state, s)
        )
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._drain_log)
        self._refresh_state(self.controller.state)

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
        style.configure("EnvHint.TLabel",
                        foreground="#1a73e8", font=("Helvetica", 11, "italic"))

    # ── layout ─────────────────────────────────────────────────────── #

    def _build_ui(self):
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer)
        header.pack(fill="x", pady=(0, 12))
        ttk.Label(header, text="TradeSynchronizer",
                  font=("Helvetica", 17, "bold")).pack(side="left")
        self.start_btn = ttk.Button(header, text="Start", command=self._on_start)
        self.stop_btn  = ttk.Button(header, text="Stop",  command=self._on_stop)
        self.stop_btn.pack(side="right", padx=(6, 0))
        self.start_btn.pack(side="right")

        status_row = ttk.Frame(outer)
        status_row.pack(fill="x", pady=(0, 12))
        self.status_dot = tk.Canvas(status_row, width=14, height=14,
                                    highlightthickness=0,
                                    bg=self.root.cget("bg"))
        self.status_dot.pack(side="left")
        self._dot = self.status_dot.create_oval(2, 2, 12, 12,
                                                fill="#9aa0a6", outline="")
        self.status_label = ttk.Label(status_row, text="Stopped",
                                      style="Status.TLabel")
        self.status_label.pack(side="left", padx=(8, 0))

        notebook = ttk.Notebook(outer)
        notebook.pack(fill="both", expand=True)
        self.settings_tab = ttk.Frame(notebook)
        self.log_tab = ttk.Frame(notebook)
        notebook.add(self.settings_tab, text="Settings")
        notebook.add(self.log_tab, text="Log")
        self._build_settings_tab(self.settings_tab)
        self._build_log_tab(self.log_tab)

    def _build_settings_tab(self, parent):
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
                        lambda e: canvas.yview_scroll(int(-1 * (e.delta / 2)), "units"))

        inner.columnconfigure(1, weight=1)
        row = 0
        for key, label, kind, default, opts in _FIELDS:
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

            if kind == "bool":
                var: tk.Variable = tk.BooleanVar(
                    value=str(default).lower() == "true")
                w = ttk.Checkbutton(inner, variable=var,
                                    command=self._on_widget_change)
                w.grid(row=row, column=1, sticky="w", pady=4)
            elif kind == "choice":
                var = tk.StringVar(value=default)
                w = ttk.Combobox(inner, textvariable=var, values=opts,
                                 state="readonly", width=14)
                w.grid(row=row, column=1, sticky="w", pady=4)
                # Special: the Environment dropdown swaps the form contents
                # for per-env fields when it changes.
                if key == "TRADOVATE_ENVIRONMENT":
                    w.bind("<<ComboboxSelected>>",
                           lambda _e: self._on_env_change())
                else:
                    w.bind("<<ComboboxSelected>>",
                           lambda _e: self._on_widget_change())
            elif kind == "password":
                var = tk.StringVar(value=default)
                w = ttk.Entry(inner, textvariable=var, show="•")
                w.grid(row=row, column=1, sticky="ew", pady=4)
                var.trace_add("write", lambda *_a: self._on_widget_change())
            else:
                var = tk.StringVar(value=default)
                w = ttk.Entry(inner, textvariable=var)
                w.grid(row=row, column=1, sticky="ew", pady=4)
                var.trace_add("write", lambda *_a: self._on_widget_change())

            self.widgets[key] = var
            row += 1

            # Hint right under the Environment dropdown so the user
            # knows the credentials below depend on it.
            if key == "TRADOVATE_ENVIRONMENT":
                self.env_hint = ttk.Label(
                    inner, text="", style="EnvHint.TLabel",
                )
                self.env_hint.grid(row=row, column=1, sticky="w", pady=(0, 4))
                row += 1

            help_text = opts if isinstance(opts, str) else None
            if help_text:
                ttk.Label(inner, text="↪ " + help_text,
                          style="Help.TLabel").grid(
                    row=row, column=1, sticky="w", pady=(0, 4))
                row += 1

        ttk.Separator(inner, orient="horizontal").grid(
            row=row, column=0, columnspan=2, sticky="ew", pady=(14, 10))
        row += 1
        btn_row = ttk.Frame(inner)
        btn_row.grid(row=row, column=0, columnspan=2, sticky="e")
        self.reload_btn = ttk.Button(btn_row, text="Reload",
                                     command=self._load_settings)
        self.save_btn = ttk.Button(btn_row, text="Save",
                                   command=self._save_settings)
        self.reload_btn.pack(side="left", padx=(0, 8))
        self.save_btn.pack(side="left")

    def _build_log_tab(self, parent):
        toolbar = ttk.Frame(parent)
        toolbar.pack(fill="x", padx=8, pady=(8, 4))
        self.auto_scroll = tk.BooleanVar(value=True)
        ttk.Checkbutton(toolbar, text="Auto-scroll",
                        variable=self.auto_scroll).pack(side="left")
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

    # ── settings I/O ──────────────────────────────────────────────── #

    def _load_settings(self):
        self.store.load()
        self._displayed_env = self.store.active_env
        self._populate_form_from_store()
        self._saved_snapshot = self.store.snapshot()
        self._dirty = False
        self._refresh_save_button()
        self._refresh_env_hint()

    def _save_settings(self):
        # Flush any pending widget edits into the store first.
        self._flush_widgets_to_store()
        err = self._validate()
        if err:
            messagebox.showerror("Invalid settings", err)
            return
        try:
            self.store.write()
        except OSError as e:
            messagebox.showerror("Save failed",
                                 f"Could not write {self.store.env_path}: {e}")
            return
        self._saved_snapshot = self.store.snapshot()
        self._dirty = False
        self._refresh_save_button()
        self._append_log(
            f"⚙️  Saved {self.store.env_path}  "
            f"(active={self.store.active_env})\n"
        )

    def _validate(self) -> Optional[str]:
        """Validate only the ACTIVE environment's required credentials."""
        active = self.store.active_env
        required = ["TRADOVATE_USERNAME", "TRADOVATE_PASSWORD",
                    "TRADOVATE_CID", "TRADOVATE_SEC"]
        missing = [k for k in required
                   if not self.store.per_env[active].get(k)]
        if missing:
            return (f"Missing required for {active.upper()}: "
                    + ", ".join(missing))
        port = self.store.shared.get("PROXY_LISTEN_PORT", "")
        try:
            p = int(port)
            if not (1 <= p <= 65535):
                raise ValueError
        except ValueError:
            return f"PROXY_LISTEN_PORT must be 1-65535, got '{port}'."
        return None

    # ── store ↔ widgets sync ──────────────────────────────────────── #

    def _populate_form_from_store(self):
        """
        Copy every stored value (active env for per_env fields) into
        the corresponding widget. Widget writes are suppressed so we
        don't loop back through _on_widget_change and re-dirty.
        """
        self._suppress_sync = True
        try:
            for key, var in self.widgets.items():
                value = self.store.get(key)
                if not value:
                    # Fall back to the field default so the form isn't
                    # blank for things like APP_ID where the default
                    # is meaningful.
                    field = next((f for f in _FIELDS if f[0] == key), None)
                    if field:
                        value = field[3] or ""
                self._set_widget_value(key, value)
        finally:
            self._suppress_sync = False

    def _set_widget_value(self, key: str, value: str):
        var = self.widgets[key]
        field = next((f for f in _FIELDS if f[0] == key), None)
        kind = field[2] if field else "text"
        if kind == "bool":
            var.set(str(value).strip().lower() in ("1", "true", "yes", "on"))
        else:
            var.set(value)

    def _flush_widgets_to_store(self):
        """Copy every widget value into the store under the active env."""
        for key, var in self.widgets.items():
            v = var.get()
            value = ("true" if v else "false") if isinstance(v, bool) \
                else str(v).strip()
            self.store.set(key, value)

    # ── widget callbacks ──────────────────────────────────────────── #

    def _on_widget_change(self, *_):
        if self._suppress_sync:
            return
        self._flush_widgets_to_store()
        is_dirty = self.store.snapshot() != self._saved_snapshot
        if is_dirty != self._dirty:
            self._dirty = is_dirty
            self._refresh_save_button()

    def _on_env_change(self):
        """
        Triggered when the Environment combobox changes. Saves the
        currently displayed per-env values into the OLD environment's
        bucket, activates the NEW environment in the store, and
        repopulates the form from there. The other (non per-env)
        fields stay put.
        """
        new_env = self.widgets["TRADOVATE_ENVIRONMENT"].get().lower()
        if new_env not in ENVIRONMENTS or new_env == self._displayed_env:
            return

        old_env = self._displayed_env

        # 1. Snapshot the per_env widgets into the OLD env in the store.
        for key in PER_ENV_KEYS:
            if key not in self.widgets:
                continue
            v = self.widgets[key].get()
            value = ("true" if v else "false") if isinstance(v, bool) \
                else str(v).strip()
            self.store.per_env[old_env][key] = value

        # 2. Activate the new env.
        self.store.active_env = new_env
        self._displayed_env = new_env

        # 3. Repopulate per_env widgets from the NEW env.
        self._suppress_sync = True
        try:
            for key in PER_ENV_KEYS:
                if key in self.widgets:
                    self._set_widget_value(key, self.store.get(key))
        finally:
            self._suppress_sync = False

        # 4. Refresh dirty state and UI hint.
        is_dirty = self.store.snapshot() != self._saved_snapshot
        if is_dirty != self._dirty:
            self._dirty = is_dirty
            self._refresh_save_button()
        self._refresh_env_hint()
        self._append_log(
            f"🔀 Form switched: {old_env.upper()} → {new_env.upper()} "
            f"(credentials swapped, other settings unchanged)\n"
        )

    def _refresh_env_hint(self):
        if hasattr(self, "env_hint"):
            self.env_hint.configure(
                text=f"↳ credentials below apply to the "
                     f"{self.store.active_env.upper()} environment"
            )

    def _refresh_save_button(self):
        self.save_btn.configure(text="Save *" if self._dirty else "Save")

    # ── controller events ─────────────────────────────────────────── #

    def _on_start(self):
        if self._dirty:
            if not messagebox.askyesno(
                "Unsaved changes",
                "You have unsaved changes. Save them first?",
            ):
                return
            self._save_settings()
            if self._dirty:
                return
        err = self.controller.start()
        if err:
            messagebox.showerror("Cannot start", err)

    def _on_stop(self):
        self.controller.stop()

    def _refresh_state(self, state: str):
        self.status_dot.itemconfigure(self._dot, fill=_STATE_COLOR[state])
        self.status_label.configure(text=_STATE_LABEL[state])
        is_running = state in (ProxyController.STATE_STARTING,
                               ProxyController.STATE_RUNNING)
        self.start_btn.configure(state="disabled" if is_running else "normal")
        self.stop_btn.configure(state="normal" if is_running else "disabled")

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
        self.log_text.insert("end", line)
        if self.auto_scroll.get():
            self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    # ── lifecycle ─────────────────────────────────────────────────── #

    def _on_close(self):
        if self.controller.state in (ProxyController.STATE_STARTING,
                                     ProxyController.STATE_RUNNING):
            if not messagebox.askyesno(
                "Proxy is running",
                "The proxy is currently running. Stop it and quit?",
            ):
                return
        self.controller.stop()
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
