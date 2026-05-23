"""
TradeSynchronizer — Tkinter GUI.

A minimal personal-use desktop wrapper around `main.py`:
  • Settings tab — edit and save the .env file
  • Log tab      — live tail of the proxy's stdout
  • Start / Stop — spawn / terminate the main.py subprocess

Zero external GUI dependencies (stdlib tkinter / ttk only). The
application is single-user and runs entirely locally — it never
opens a network socket of its own; the only network activity comes
from the supervised `main.py` subprocess.
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
#  .env file I/O                                                          #
# ─────────────────────────────────────────────────────────────────────── #

class EnvFile:
    """
    Tiny .env reader/writer that preserves the structure of
    .env.example (comments, blank lines, ordering) when present.
    Values are NOT quoted — same convention as python-dotenv default.
    """

    def __init__(self, path: Path, template: Optional[Path] = None):
        self.path = path
        self.template = template

    def read(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        out: dict[str, str] = {}
        for line in self.path.read_text().splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, _, v = s.partition("=")
            out[k.strip()] = v.strip()
        return out

    def write(self, kv: dict[str, str]) -> None:
        if self.template and self.template.exists():
            lines: list[str] = []
            seen: set[str] = set()
            for line in self.template.read_text().splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    lines.append(line)
                    continue
                key = stripped.partition("=")[0].strip()
                seen.add(key)
                lines.append(f"{key}={kv.get(key, '')}")
            extras = [k for k in kv if k not in seen]
            if extras:
                lines += ["", "# Custom keys not in .env.example"]
                lines += [f"{k}={kv[k]}" for k in extras]
            self.path.write_text("\n".join(lines) + "\n")
        else:
            self.path.write_text(
                "\n".join(f"{k}={v}" for k, v in kv.items()) + "\n"
            )


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
        """Register a callback invoked (from any thread) on state transitions."""
        self._state_cbs.append(cb)

    def start(self) -> Optional[str]:
        """
        Spawn the subprocess. Returns None on success or an error
        string suitable for an error dialog.
        """
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
        """Send SIGTERM, fall back to SIGKILL after 5 s."""
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

    # ── internals ──────────────────────────────────────────────────── #

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
        """Push a log line. If the queue is full, drop the oldest entry —
        logging must never block the proxy."""
        try:
            self.log_q.put_nowait(line)
        except queue.Full:
            try:
                self.log_q.get_nowait()
                self.log_q.put_nowait(line)
            except Exception:
                pass

    def _resolve_python(self) -> Optional[str]:
        """Prefer the project's venv interpreter; fall back to sys.executable."""
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
#  Field declarations — single source of truth for the settings form     #
# ─────────────────────────────────────────────────────────────────────── #

# (key, label, kind, default, options_or_help)
# kind: text | password | choice | bool | section
_FIELDS: list[tuple] = [
    ("__section__", "Tradovate Account",       "section",  None, None),
    ("TRADOVATE_USERNAME",    "Username",        "text",     "", None),
    ("TRADOVATE_PASSWORD",    "Password",        "password", "", None),
    ("TRADOVATE_APP_ID",      "App ID",          "text",     "TradeSynchronizer", None),
    ("TRADOVATE_APP_VERSION", "App version",     "text",     "1.0", None),
    ("TRADOVATE_CID",         "Client ID (CID)", "text",     "",
        "From Tradovate API Access — string, not number."),
    ("TRADOVATE_SEC",         "API secret",      "password", "", None),
    ("TRADOVATE_ENVIRONMENT", "Environment",     "choice",   "demo", ["demo", "live"]),
    ("TRADOVATE_ACCOUNT_ID",  "Account ID",      "text",     "",
        "Optional — pins the LEADER account."),

    ("__section__", "Proxy server",             "section",  None, None),
    ("PROXY_LISTEN_HOST", "Listen host", "text", "127.0.0.1", None),
    ("PROXY_LISTEN_PORT", "Listen port", "text", "8080",      None),

    ("__section__", "Replication policy",       "section",  None, None),
    ("REPLICATION_MODE",      "Mode",                  "choice", "mirror",
        ["mirror", "market"]),
    ("SKIP_PROTECTIVE_STOPS", "Skip protective stops", "bool",   "true", None),
    ("IBKR_WATCHED_ACCOUNTS", "Watched IBKR accounts", "text",   "",
        "Comma-separated. Empty = all."),

    ("__section__", "Logging",                  "section",  None, None),
    ("LOG_LEVEL", "Level", "choice", "INFO",
        ["DEBUG", "INFO", "WARNING", "ERROR"]),
    ("LOG_FILE",  "File",  "text",   "/tmp/tradesync.log", None),
]


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
        self.env_file = EnvFile(
            path=project_root / ".env",
            template=project_root / ".env.example",
        )
        self.log_q: queue.Queue[str] = queue.Queue(maxsize=20_000)
        self.controller = ProxyController(project_root, self.log_q)

        self.root = tk.Tk()
        self.root.title("TradeSynchronizer")
        self.root.geometry("780x620")
        self.root.minsize(640, 520)

        self.widgets: dict[str, tk.Variable] = {}
        self._dirty = False
        self._saved_values: dict[str, str] = {}

        self._init_style()
        self._build_ui()
        self._load_settings()

        # ProxyController callbacks fire from a worker thread; marshal
        # them onto the Tk main loop via after(0, ...).
        self.controller.on_state_change(
            lambda s: self.root.after(0, self._refresh_state, s)
        )
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._drain_log)
        self._refresh_state(self.controller.state)

    # ── styling ────────────────────────────────────────────────────── #

    def _init_style(self):
        style = ttk.Style()
        # macOS 11+ ships the 'aqua' theme; on Linux/Windows fall back.
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

        # Header: title on the left, Start/Stop buttons on the right.
        header = ttk.Frame(outer)
        header.pack(fill="x", pady=(0, 12))
        ttk.Label(header, text="TradeSynchronizer",
                  font=("Helvetica", 17, "bold")).pack(side="left")
        self.start_btn = ttk.Button(header, text="Start", command=self._on_start)
        self.stop_btn  = ttk.Button(header, text="Stop",  command=self._on_stop)
        self.stop_btn.pack(side="right", padx=(6, 0))
        self.start_btn.pack(side="right")

        # Status row: coloured dot + label.
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

        # Tabs: Settings / Log
        notebook = ttk.Notebook(outer)
        notebook.pack(fill="both", expand=True)
        self.settings_tab = ttk.Frame(notebook)
        self.log_tab = ttk.Frame(notebook)
        notebook.add(self.settings_tab, text="Settings")
        notebook.add(self.log_tab, text="Log")
        self._build_settings_tab(self.settings_tab)
        self._build_log_tab(self.log_tab)

    def _build_settings_tab(self, parent):
        # Scrollable canvas so the form is usable even on small windows.
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
        # Mouse-wheel scrolling. macOS sends e.delta in single-unit increments
        # rather than the 120-per-notch convention of other platforms.
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
                                    command=self._mark_dirty)
                w.grid(row=row, column=1, sticky="w", pady=4)
            elif kind == "choice":
                var = tk.StringVar(value=default)
                w = ttk.Combobox(inner, textvariable=var, values=opts,
                                 state="readonly", width=14)
                w.grid(row=row, column=1, sticky="w", pady=4)
                w.bind("<<ComboboxSelected>>", lambda _e: self._mark_dirty())
            elif kind == "password":
                var = tk.StringVar(value=default)
                w = ttk.Entry(inner, textvariable=var, show="•")
                w.grid(row=row, column=1, sticky="ew", pady=4)
                var.trace_add("write", lambda *_a: self._mark_dirty())
            else:  # plain text
                var = tk.StringVar(value=default)
                w = ttk.Entry(inner, textvariable=var)
                w.grid(row=row, column=1, sticky="ew", pady=4)
                var.trace_add("write", lambda *_a: self._mark_dirty())

            self.widgets[key] = var
            row += 1

            # Show a help line under the field when opts is a string.
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
        # Dark log viewer for log readability — keeps a terminal feel.
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
        existing = self.env_file.read()
        for key, var in self.widgets.items():
            field = next((f for f in _FIELDS if f[0] == key), None)
            default = field[3] if field else ""
            kind = field[2] if field else "text"
            raw = existing.get(key, default)
            if kind == "bool":
                var.set(str(raw).strip().lower() in ("1", "true", "yes", "on"))
            else:
                var.set(raw)
        self._saved_values = self._collect_values()
        self._dirty = False
        self._refresh_save_button()

    def _save_settings(self):
        values = self._collect_values()
        err = self._validate(values)
        if err:
            messagebox.showerror("Invalid settings", err)
            return
        try:
            self.env_file.write(values)
        except OSError as e:
            messagebox.showerror("Save failed",
                                 f"Could not write {self.env_file.path}: {e}")
            return
        self._saved_values = dict(values)
        self._dirty = False
        self._refresh_save_button()
        self._append_log(f"⚙️  Saved {self.env_file.path}\n")

    def _collect_values(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for key, var in self.widgets.items():
            v = var.get()
            out[key] = ("true" if v else "false") if isinstance(v, bool) \
                       else str(v).strip()
        return out

    def _validate(self, values: dict[str, str]) -> Optional[str]:
        required = ["TRADOVATE_USERNAME", "TRADOVATE_PASSWORD",
                    "TRADOVATE_CID", "TRADOVATE_SEC"]
        missing = [k for k in required if not values.get(k)]
        if missing:
            return "Missing required: " + ", ".join(missing)
        port = values.get("PROXY_LISTEN_PORT", "")
        try:
            p = int(port)
            if not (1 <= p <= 65535):
                raise ValueError
        except ValueError:
            return f"PROXY_LISTEN_PORT must be an integer 1-65535, got '{port}'."
        return None

    def _mark_dirty(self, *_):
        is_dirty = (self._saved_values
                    and self._collect_values() != self._saved_values)
        if is_dirty != self._dirty:
            self._dirty = is_dirty
            self._refresh_save_button()

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
                return   # save failed validation
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
        # Bound work per tick so a bursty proxy can't freeze the UI.
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
