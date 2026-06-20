"""
replication_settings — GUI panel for choosing source→follower
replication pairs (the bidirectional direction picker).

Design: the data logic and the tkinter rendering are SEPARATED.

  * ReplicationSettingsController — pure, headless logic over a
    ReplicationConfig: list/add/remove/toggle pairs, edit the gateway,
    validate, load/save config/replication.json. Fully unit-testable
    with no display.

  * ReplicationSettingsPanel — a ttk.Frame that renders the controller's
    state and wires buttons to it. Imported lazily by the main GUI so a
    headless environment (tests, CI) can import the controller without
    pulling in tkinter.

This panel is the user-facing half of the bidirectional work: it writes
the config/replication.json that main.py reads when
TRADESYNC_ENABLE_WS_PIPELINES=1. It does NOT start/stop engines itself —
it only edits the declaration of which pairs exist.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from ..replication_config import (
    EndpointRef,
    ReplicationConfig,
    ReplicationConfigError,
    ReplicationPair,
    default_replication_config_path,
)


logger = logging.getLogger("tradesync.ui.replication")


@dataclass
class PairDraft:
    """The fields the UI collects to build one pair. Kept separate from
    ReplicationPair so the UI can hold partially-filled / invalid input
    without the dataclass invariants firing mid-edit."""
    name:         str = ""
    source_broker:   str = "tradovate"
    source_env:      str = "demo"
    source_account:  str = ""
    follower_broker: str = "ibkr"
    follower_env:    str = "demo"
    follower_account: str = ""
    enabled:         bool = True
    # Held as a string so the field can be empty / mid-edit without the
    # dataclass invariants firing; parsed in to_pair (blank → 1.0).
    ratio:           str = "1.0"

    def to_pair(self) -> ReplicationPair:
        raw = (self.ratio or "").strip()
        try:
            ratio = float(raw) if raw else 1.0
        except ValueError:
            raise ReplicationConfigError(
                f"ratio must be a number, got {raw!r}")
        return ReplicationPair(
            name=self.name.strip() or "unnamed",
            source=EndpointRef(broker=self.source_broker,
                               env=self.source_env,
                               account_id=self.source_account.strip()),
            follower=EndpointRef(broker=self.follower_broker,
                                 env=self.follower_env,
                                 account_id=self.follower_account.strip()),
            enabled=self.enabled,
            ratio=ratio,
        )


class ReplicationSettingsController:
    """Headless logic backing the replication settings panel."""

    def __init__(self, config_path: Optional[Path] = None,
                 project_root: Optional[Path] = None):
        if config_path is None:
            from ..config import PROJECT_ROOT
            root = project_root or PROJECT_ROOT
            config_path = default_replication_config_path(root)
        self._path = config_path
        self._config = ReplicationConfig()

    @property
    def config_path(self) -> Path:
        return self._path

    @property
    def pairs(self) -> List[ReplicationPair]:
        return list(self._config.pairs)

    @property
    def gateway(self):
        return self._config.ibkr_gateway

    # ── load / save ──────────────────────────────────────────────── #

    def load(self) -> None:
        """Load from disk. A missing/invalid file yields an empty config
        (and logs); the panel then starts blank rather than erroring."""
        try:
            self._config = ReplicationConfig.load(self._path)
        except ReplicationConfigError as e:
            logger.warning("replication.json invalid (%s) — starting empty", e)
            self._config = ReplicationConfig()

    def save(self) -> None:
        """Validate + persist. Raises ReplicationConfigError if the
        current set of pairs is invalid, so the panel can show the
        message instead of writing a broken file."""
        self._config.save(self._path)

    # ── mutate ───────────────────────────────────────────────────── #

    def add_pair(self, draft: PairDraft) -> ReplicationPair:
        """Validate a draft and append it. Raises ReplicationConfigError
        on invalid input (unknown broker/env, empty account, loop, or
        duplicate name) WITHOUT mutating state."""
        pair = draft.to_pair()
        pair.validate()
        if any(p.name == pair.name for p in self._config.pairs):
            raise ReplicationConfigError(
                f"a pair named {pair.name!r} already exists — names must be "
                f"unique")
        self._config.pairs.append(pair)
        return pair

    def update_pair(self, index: int, draft: PairDraft) -> ReplicationPair:
        """Validate a draft and replace the pair at `index` with it.
        Raises ReplicationConfigError on invalid input (and IndexError on
        a bad index) WITHOUT mutating state. The duplicate-name check
        excludes the pair being edited, so keeping its own name is fine."""
        if not (0 <= index < len(self._config.pairs)):
            raise IndexError(f"pair index {index} out of range")
        pair = draft.to_pair()
        pair.validate()
        if any(i != index and p.name == pair.name
               for i, p in enumerate(self._config.pairs)):
            raise ReplicationConfigError(
                f"a pair named {pair.name!r} already exists — names must be "
                f"unique")
        self._config.pairs[index] = pair
        return pair

    def draft_for(self, index: int) -> PairDraft:
        """Return the pair at `index` as an editable PairDraft, for
        loading into the form when the user clicks Edit."""
        if not (0 <= index < len(self._config.pairs)):
            raise IndexError(f"pair index {index} out of range")
        p = self._config.pairs[index]
        return PairDraft(
            name=p.name,
            source_broker=p.source.broker, source_env=p.source.env,
            source_account=p.source.account_id,
            follower_broker=p.follower.broker, follower_env=p.follower.env,
            follower_account=p.follower.account_id,
            enabled=p.enabled,
            ratio=f"{p.ratio:g}",
        )

    def remove_pair(self, index: int) -> None:
        if not (0 <= index < len(self._config.pairs)):
            raise IndexError(f"pair index {index} out of range")
        del self._config.pairs[index]

    def toggle_pair(self, index: int) -> bool:
        """Flip a pair's enabled flag; returns the new value."""
        if not (0 <= index < len(self._config.pairs)):
            raise IndexError(f"pair index {index} out of range")
        p = self._config.pairs[index]
        p.enabled = not p.enabled
        return p.enabled

    def set_gateway(self, *, host: Optional[str] = None,
                    port: Optional[int] = None,
                    client_id: Optional[int] = None) -> None:
        g = self._config.ibkr_gateway
        if host is not None:
            g.host = host
        if port is not None:
            g.port = port
        if client_id is not None:
            g.client_id = client_id
        g.validate()

    def summary_rows(self) -> List[dict]:
        """A render-friendly view of the pairs for a table/listbox."""
        rows = []
        for p in self._config.pairs:
            rows.append({
                "name": p.name,
                "source": p.source.identity,
                "follower": p.follower.identity,
                "enabled": p.enabled,
                "ratio": p.ratio,
                "needs_gateway": p.follower.broker == "ibkr",
            })
        return rows

    def needs_gateway(self) -> bool:
        """True if any pair (enabled or not) has IBKR as its follower —
        i.e. the 'Open IB Gateway' affordance is relevant."""
        return any(p.follower.broker == "ibkr" for p in self._config.pairs)

    def open_gateway(self):
        """Open IB Gateway if it isn't already running (never restarts a
        running one). Returns the GatewayStatus so the panel can show its
        message. Imported lazily so the controller stays importable
        without the launcher's deps."""
        from ..ibkr_gateway_launcher import ensure_gateway_running
        g = self._config.ibkr_gateway
        return ensure_gateway_running(api_host=g.host, api_port=g.port)


# ── tkinter panel (imported lazily by the main GUI) ──────────────────── #

def build_panel(parent, controller: ReplicationSettingsController):
    """Construct and return a ttk.Frame rendering the controller. Kept
    as a function (not a module-level class) so importing this module
    headless — for the controller and its tests — never imports tkinter.

    The panel is intentionally minimal: a list of existing pairs with
    enable/remove, a small form to add a pair, and gateway host/port
    fields. It reads/writes through the controller and calls
    controller.save() on demand."""
    import tkinter as tk
    from tkinter import ttk, messagebox

    frame = ttk.Frame(parent, padding=12)

    ttk.Label(frame, text="Replication pairs (source → follower)",
              font=("", 13, "bold")).pack(anchor="w")
    ttk.Label(
        frame,
        text=("Declare which account mirrors onto which. Tradovate→IBKR "
              "needs IB Gateway running. Saved to config/replication.json; "
              "the engine reads it when WS pipelines are enabled."),
        wraplength=520, foreground="#666",
    ).pack(anchor="w", pady=(0, 8))

    # Existing pairs list.
    list_frame = ttk.Frame(frame)
    list_frame.pack(fill="both", expand=True)
    listbox = tk.Listbox(list_frame, height=6)
    listbox.pack(side="left", fill="both", expand=True)

    def refresh():
        listbox.delete(0, tk.END)
        for row in controller.summary_rows():
            mark = "✓" if row["enabled"] else "·"
            gw = " [GW]" if row["needs_gateway"] else ""
            # Only surface the ratio when it actually scales (≠ 1.0), to
            # keep the common exact-mirror case uncluttered.
            ratio = row.get("ratio", 1.0)
            rt = f" ×{ratio:g}" if ratio != 1.0 else ""
            listbox.insert(
                tk.END,
                f"{mark} {row['name']}: {row['source']} → "
                f"{row['follower']}{rt}{gw}")

    def _persist_and_refresh():
        """Write the current pairs to disk and redraw the list. Every
        mutation (add, update, toggle, remove) goes through here, so the
        config file always reflects the list — there's no separate Save
        step. A validation error leaves the list as-is and reports it."""
        try:
            controller.save()
        except ReplicationConfigError as e:
            messagebox.showerror("Invalid configuration", str(e))
        refresh()

    btns = ttk.Frame(list_frame)
    btns.pack(side="left", fill="y", padx=(8, 0))

    # Which pair (if any) the form is currently editing. None = the form
    # adds a new pair; an int = the form updates that existing pair.
    # A one-element list so the inner closures can rebind it.
    edit_index = [None]

    def _selected_index() -> Optional[int]:
        sel = listbox.curselection()
        return sel[0] if sel else None

    def on_toggle():
        i = _selected_index()
        if i is not None:
            controller.toggle_pair(i)
            _persist_and_refresh()

    def on_remove():
        i = _selected_index()
        if i is not None:
            controller.remove_pair(i)
            # If we were editing the removed pair, drop back to add mode.
            if edit_index[0] == i:
                _exit_edit_mode()
            _persist_and_refresh()

    def on_open_gateway():
        status = controller.open_gateway()
        messagebox.showinfo("IB Gateway", status.message)

    ttk.Button(btns, text="Enable/Disable", command=on_toggle).pack(fill="x")
    ttk.Button(btns, text="Edit",
               command=lambda: on_edit()).pack(fill="x", pady=4)
    ttk.Button(btns, text="Remove", command=on_remove).pack(fill="x")
    # Open IB Gateway only matters when a pair uses it as follower; the
    # button is harmless otherwise (it just reports 'already running' /
    # 'not installed'), so always show it but label it clearly.
    ttk.Button(btns, text="Open IB Gateway",
               command=on_open_gateway).pack(fill="x", pady=(4, 0))

    # Add-pair form.
    form = ttk.LabelFrame(frame, text="Add a pair", padding=8)
    form.pack(fill="x", pady=(10, 0))

    name_var = tk.StringVar()
    s_broker = tk.StringVar(value="tradovate")
    s_env = tk.StringVar(value="demo")
    s_acct = tk.StringVar()
    f_broker = tk.StringVar(value="ibkr")
    f_env = tk.StringVar(value="demo")
    f_acct = tk.StringVar()
    ratio_var = tk.StringVar(value="1.0")

    def _row(label, build_widgets):
        """Create one labelled row. build_widgets(row_frame) creates the
        widgets WITH the row frame as their parent and packs them — this
        is what keeps each row self-contained (a prior version created
        the widgets with `form` as parent, so they all escaped onto a
        single overflowing line and pushed the buttons off-screen)."""
        r = ttk.Frame(form)
        r.pack(fill="x", pady=2)
        ttk.Label(r, text=label, width=10).pack(side="left")
        build_widgets(r)

    def _build_name(r):
        ttk.Entry(r, textvariable=name_var, width=24).pack(side="left", padx=2)

    def _build_source(r):
        ttk.Combobox(r, textvariable=s_broker, width=10,
                     values=["tradovate", "ibkr"], state="readonly"
                     ).pack(side="left", padx=2)
        ttk.Combobox(r, textvariable=s_env, width=6,
                     values=["demo", "live"], state="readonly"
                     ).pack(side="left", padx=2)
        ttk.Label(r, text="Account").pack(side="left", padx=(8, 2))
        ttk.Entry(r, textvariable=s_acct, width=14).pack(side="left", padx=2)

    def _build_follower(r):
        ttk.Combobox(r, textvariable=f_broker, width=10,
                     values=["ibkr", "tradovate"], state="readonly"
                     ).pack(side="left", padx=2)
        ttk.Combobox(r, textvariable=f_env, width=6,
                     values=["demo", "live"], state="readonly"
                     ).pack(side="left", padx=2)
        ttk.Label(r, text="Account").pack(side="left", padx=(8, 2))
        ttk.Entry(r, textvariable=f_acct, width=14).pack(side="left", padx=2)

    def _build_ratio(r):
        ttk.Entry(r, textvariable=ratio_var, width=8).pack(side="left", padx=2)
        ttk.Label(r, text="follower size = master size × ratio "
                          "(rounded, min 1; 1.0 = exact mirror)"
                  ).pack(side="left", padx=2)

    _row("Name", _build_name)
    _row("Source", _build_source)
    _row("Follower", _build_follower)
    _row("Ratio", _build_ratio)
    ttk.Label(
        form,
        text=("Account = the broker's account id: Tradovate is numeric "
              "(e.g. 19000001), IBKR looks like U0000001 (or DU… for paper)."),
        wraplength=520, foreground="#666",
    ).pack(anchor="w", pady=(4, 0))

    def _clear_form():
        name_var.set(""); s_acct.set(""); f_acct.set("")
        ratio_var.set("1.0")
        s_broker.set("tradovate"); s_env.set("demo")
        f_broker.set("ibkr"); f_env.set("demo")

    def _exit_edit_mode():
        """Leave edit mode: clear the form and restore the add-pair UI."""
        edit_index[0] = None
        form.configure(text="Add a pair")
        submit_btn.configure(text="Add pair")
        cancel_btn.pack_forget()
        _clear_form()

    def on_edit():
        i = _selected_index()
        if i is None:
            return
        draft = controller.draft_for(i)
        name_var.set(draft.name)
        s_broker.set(draft.source_broker); s_env.set(draft.source_env)
        s_acct.set(draft.source_account)
        f_broker.set(draft.follower_broker); f_env.set(draft.follower_env)
        f_acct.set(draft.follower_account)
        ratio_var.set(draft.ratio)
        edit_index[0] = i
        form.configure(text=f"Edit pair: {draft.name}")
        submit_btn.configure(text="Update pair")
        cancel_btn.pack(in_=btn_row, side="right", padx=(6, 0))

    def on_submit():
        draft = PairDraft(
            name=name_var.get(), source_broker=s_broker.get(),
            source_env=s_env.get(), source_account=s_acct.get(),
            follower_broker=f_broker.get(), follower_env=f_env.get(),
            follower_account=f_acct.get(), ratio=ratio_var.get())
        try:
            if edit_index[0] is None:
                controller.add_pair(draft)
            else:
                controller.update_pair(edit_index[0], draft)
        except (ReplicationConfigError, IndexError) as e:
            messagebox.showerror("Invalid pair", str(e))
            return
        # Add pair also SAVES — there is no separate Save button.
        _persist_and_refresh()
        _exit_edit_mode()

    # Button row, pinned to the bottom of the form as its own full-width
    # strip so it can never be pushed off-screen by the fields above.
    btn_row = ttk.Frame(form)
    btn_row.pack(side="top", fill="x", pady=(8, 0))
    submit_btn = ttk.Button(btn_row, text="Add pair", command=on_submit)
    submit_btn.pack(side="right", padx=(6, 0))
    cancel_btn = ttk.Button(btn_row, text="Cancel edit",
                            command=lambda: _exit_edit_mode())
    # cancel_btn is packed (to the left of submit) only while editing.

    controller.load()
    refresh()
    return frame
