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

from ..account_book import (
    Account,
    AccountBook,
    default_account_book_path,
)
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
                 project_root: Optional[Path] = None,
                 accounts_path: Optional[Path] = None):
        if config_path is None or accounts_path is None:
            from ..config import PROJECT_ROOT
            root = project_root or PROJECT_ROOT
            if config_path is None:
                config_path = default_replication_config_path(root)
            if accounts_path is None:
                accounts_path = default_account_book_path(root)
        self._path = config_path
        self._accounts_path = accounts_path
        self._config = ReplicationConfig()
        self._book = AccountBook()

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
        (and logs); the panel then starts blank rather than erroring.
        The account book is loaded the same forgiving way."""
        try:
            self._config = ReplicationConfig.load(self._path)
        except ReplicationConfigError as e:
            logger.warning("replication.json invalid (%s) — starting empty", e)
            self._config = ReplicationConfig()
        try:
            self._book = AccountBook.load(self._accounts_path)
        except ReplicationConfigError as e:
            logger.warning("accounts.json invalid (%s) — starting empty", e)
            self._book = AccountBook()

    def save(self) -> None:
        """Validate + persist. Raises ReplicationConfigError if the
        current set of pairs is invalid, so the panel can show the
        message instead of writing a broken file."""
        self._config.save(self._path)

    def save_accounts(self) -> None:
        """Validate + persist the account book. Separate from save() so a
        pair-config problem can't block saving the address book and vice
        versa."""
        self._book.save(self._accounts_path)

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

    # ── account book ─────────────────────────────────────────────── #

    @property
    def accounts(self) -> List[Account]:
        return list(self._book.accounts)

    def account_labels(self) -> List[str]:
        """Labels for the pair-form dropdowns, in insertion order."""
        return [a.label for a in self._book.accounts]

    def account_by_label(self, label: str) -> Optional[Account]:
        for a in self._book.accounts:
            if a.label == label:
                return a
        return None

    def add_account(self, account: Account) -> Account:
        """Validate an account and append it. Raises
        ReplicationConfigError on invalid input or a duplicate label
        WITHOUT mutating state."""
        account.validate()
        if any(a.label.strip().lower() == account.label.strip().lower()
               for a in self._book.accounts):
            raise ReplicationConfigError(
                f"an account labelled {account.label!r} already exists — "
                f"labels must be unique")
        self._book.accounts.append(account)
        return account

    def pairs_using_account(self, label: str) -> List[str]:
        """Names of pairs whose source or follower matches this account's
        identity. Used to block deletion of an in-use account."""
        acct = self.account_by_label(label)
        if acct is None:
            return []
        ident = acct.identity
        used = []
        for p in self._config.pairs:
            if p.source.identity == ident or p.follower.identity == ident:
                used.append(p.name)
        return used

    def remove_account(self, label: str) -> None:
        """Delete an account by label. Refuses (ReplicationConfigError)
        if any pair still references it, naming the offending pair(s), so
        the user can't orphan a pair's dropdown selection."""
        idx = next((i for i, a in enumerate(self._book.accounts)
                    if a.label == label), None)
        if idx is None:
            raise ReplicationConfigError(
                f"no account labelled {label!r}")
        used_by = self.pairs_using_account(label)
        if used_by:
            joined = ", ".join(repr(n) for n in used_by)
            raise ReplicationConfigError(
                f"account {label!r} is still used by pair(s) {joined}. "
                f"Remove or edit those pair(s) first, then delete the "
                f"account.")
        del self._book.accounts[idx]

    def draft_from_labels(self, *, name: str, source_label: str,
                          follower_label: str, ratio: str,
                          enabled: bool = True) -> PairDraft:
        """Build a PairDraft from two account labels (the dropdown
        selections), copying each account's broker/env/account_id in.
        Raises ReplicationConfigError if a label isn't in the book."""
        src = self.account_by_label(source_label)
        flw = self.account_by_label(follower_label)
        if src is None:
            raise ReplicationConfigError(
                f"source account {source_label!r} not found — create it "
                f"in Accounts first")
        if flw is None:
            raise ReplicationConfigError(
                f"follower account {follower_label!r} not found — create it "
                f"in Accounts first")
        return PairDraft(
            name=name,
            source_broker=src.broker, source_env=src.env,
            source_account=src.account_id,
            follower_broker=flw.broker, follower_env=flw.env,
            follower_account=flw.account_id,
            enabled=enabled, ratio=ratio)

    def account_rows(self) -> List[dict]:
        """A render-friendly view of the saved accounts for a listbox."""
        rows = []
        for a in self._book.accounts:
            rows.append({
                "label": a.label,
                "broker": a.broker,
                "env": a.env,
                "account_id": a.account_id,
                "in_use_by": self.pairs_using_account(a.label),
            })
        return rows

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

    Two stacked sections:
      * Accounts — a reusable address book. Add accounts here ONCE
        (label + broker + env + id); they then populate the pair-form
        dropdowns. An account in use by a pair can't be deleted.
      * Replication pairs — pick a saved source + follower account from
        dropdowns (no re-typing), set a ratio, add/edit/enable/remove.

    It reads/writes through the controller, saving on every mutation."""
    import tkinter as tk
    from tkinter import ttk, messagebox

    frame = ttk.Frame(parent, padding=12)

    # ══ Section 1: Accounts (the reusable address book) ══════════════ #
    acct_box = ttk.LabelFrame(frame, text="Accounts", padding=8)
    acct_box.pack(fill="x")
    ttk.Label(
        acct_box,
        text=("Register each broker account once, then pick it from the "
              "dropdowns below. Saved to config/accounts.json (GUI-only; "
              "the engine never reads it)."),
        wraplength=520, foreground="#666",
    ).pack(anchor="w", pady=(0, 6))

    acct_list_frame = ttk.Frame(acct_box)
    acct_list_frame.pack(fill="both", expand=True)
    acct_listbox = tk.Listbox(acct_list_frame, height=4)
    acct_listbox.pack(side="left", fill="both", expand=True)
    acct_btns = ttk.Frame(acct_list_frame)
    acct_btns.pack(side="left", fill="y", padx=(8, 0))

    # Account add-form fields.
    al_label = tk.StringVar()
    al_broker = tk.StringVar(value="tradovate")
    al_env = tk.StringVar(value="demo")
    al_acct = tk.StringVar()

    def refresh_accounts():
        acct_listbox.delete(0, tk.END)
        for row in controller.account_rows():
            used = row["in_use_by"]
            tag = f"  (used by {len(used)})" if used else ""
            acct_listbox.insert(
                tk.END,
                f"{row['label']}: {row['broker']}/{row['env']}/"
                f"{row['account_id']}{tag}")
        # Keep the pair-form dropdowns in sync with the book.
        _sync_account_dropdowns()

    def on_add_account():
        try:
            controller.add_account(Account(
                label=al_label.get(), broker=al_broker.get(),
                env=al_env.get(), account_id=al_acct.get()))
            controller.save_accounts()
        except ReplicationConfigError as e:
            messagebox.showerror("Invalid account", str(e))
            return
        al_label.set(""); al_acct.set("")
        al_broker.set("tradovate"); al_env.set("demo")
        refresh_accounts()

    def on_remove_account():
        sel = acct_listbox.curselection()
        if not sel:
            return
        label = controller.account_labels()[sel[0]]
        try:
            controller.remove_account(label)
            controller.save_accounts()
        except ReplicationConfigError as e:
            # In-use accounts land here: tell the user why, naming pairs.
            messagebox.showwarning("Account in use", str(e))
            return
        refresh_accounts()

    # Account add-form: one compact row of fields + an Add button.
    acct_form = ttk.Frame(acct_box)
    acct_form.pack(fill="x", pady=(8, 0))
    ttk.Label(acct_form, text="Label").pack(side="left")
    ttk.Entry(acct_form, textvariable=al_label, width=16
              ).pack(side="left", padx=(2, 6))
    ttk.Combobox(acct_form, textvariable=al_broker, width=10,
                 values=["tradovate", "ibkr"], state="readonly"
                 ).pack(side="left", padx=2)
    ttk.Combobox(acct_form, textvariable=al_env, width=6,
                 values=["demo", "live"], state="readonly"
                 ).pack(side="left", padx=2)
    ttk.Label(acct_form, text="Id").pack(side="left", padx=(6, 2))
    ttk.Entry(acct_form, textvariable=al_acct, width=14
              ).pack(side="left", padx=2)

    ttk.Button(acct_btns, text="Add account",
               command=on_add_account).pack(fill="x")
    ttk.Button(acct_btns, text="Remove account",
               command=on_remove_account).pack(fill="x", pady=(4, 0))

    # ══ Section 2: Replication pairs ═════════════════════════════════ #
    ttk.Label(frame, text="Replication pairs (source → follower)",
              font=("", 13, "bold")).pack(anchor="w", pady=(14, 0))
    ttk.Label(
        frame,
        text=("Pick a saved source and follower account. Tradovate→IBKR "
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
        """Write the current pairs to disk and redraw both lists. Every
        pair mutation goes through here, so the config file always
        reflects the list — there's no separate Save step. A validation
        error leaves the list as-is and reports it. Accounts are redrawn
        too so their in-use markers stay correct."""
        try:
            controller.save()
        except ReplicationConfigError as e:
            messagebox.showerror("Invalid configuration", str(e))
        refresh()
        refresh_accounts()

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

    # Add-pair form: name + source/follower DROPDOWNS (from the book) +
    # ratio. No broker/env/id typing here — that lives in Accounts.
    form = ttk.LabelFrame(frame, text="Add a pair", padding=8)
    form.pack(fill="x", pady=(10, 0))

    name_var = tk.StringVar()
    source_label_var = tk.StringVar()
    follower_label_var = tk.StringVar()
    ratio_var = tk.StringVar(value="1.0")

    def _row(label, build_widgets):
        r = ttk.Frame(form)
        r.pack(fill="x", pady=2)
        ttk.Label(r, text=label, width=10).pack(side="left")
        build_widgets(r)

    # The two account dropdowns, kept in module scope so refresh_accounts
    # can repopulate their value lists when the book changes.
    source_combo = [None]
    follower_combo = [None]

    def _sync_account_dropdowns():
        labels = controller.account_labels()
        for holder in (source_combo, follower_combo):
            combo = holder[0]
            if combo is not None:
                combo.configure(values=labels)

    def _build_name(r):
        ttk.Entry(r, textvariable=name_var, width=24).pack(side="left", padx=2)

    def _build_source(r):
        c = ttk.Combobox(r, textvariable=source_label_var, width=28,
                         values=controller.account_labels(), state="readonly")
        c.pack(side="left", padx=2)
        source_combo[0] = c

    def _build_follower(r):
        c = ttk.Combobox(r, textvariable=follower_label_var, width=28,
                         values=controller.account_labels(), state="readonly")
        c.pack(side="left", padx=2)
        follower_combo[0] = c

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
        text=("Source and Follower are accounts from the Accounts list "
              "above. Add the account there first if it isn't listed."),
        wraplength=520, foreground="#666",
    ).pack(anchor="w", pady=(4, 0))

    def _clear_form():
        name_var.set(""); source_label_var.set(""); follower_label_var.set("")
        ratio_var.set("1.0")

    def _exit_edit_mode():
        """Leave edit mode: clear the form and restore the add-pair UI."""
        edit_index[0] = None
        form.configure(text="Add a pair")
        submit_btn.configure(text="Add pair")
        cancel_btn.pack_forget()
        _clear_form()

    def _label_for_endpoint(identity: str) -> str:
        """Find the account label whose identity matches a pair endpoint,
        so Edit can preselect the right dropdown entry. Empty string if
        the pair refers to an account not in the book (legacy / hand-
        edited config)."""
        for a in controller.accounts:
            if a.identity == identity:
                return a.label
        return ""

    def on_edit():
        i = _selected_index()
        if i is None:
            return
        draft = controller.draft_for(i)
        name_var.set(draft.name)
        src_id = f"{draft.source_broker}_{draft.source_env}_{draft.source_account}"
        flw_id = (f"{draft.follower_broker}_{draft.follower_env}_"
                  f"{draft.follower_account}")
        src_label = _label_for_endpoint(src_id)
        flw_label = _label_for_endpoint(flw_id)
        source_label_var.set(src_label)
        follower_label_var.set(flw_label)
        ratio_var.set(draft.ratio)
        edit_index[0] = i
        form.configure(text=f"Edit pair: {draft.name}")
        submit_btn.configure(text="Update pair")
        cancel_btn.pack(in_=btn_row, side="right", padx=(6, 0))
        if not src_label or not flw_label:
            messagebox.showinfo(
                "Account not in list",
                "This pair refers to an account that isn't in the Accounts "
                "list (it may have been hand-edited). Add the matching "
                "account above, or pick replacements from the dropdowns.")

    def on_submit():
        try:
            draft = controller.draft_from_labels(
                name=name_var.get(),
                source_label=source_label_var.get(),
                follower_label=follower_label_var.get(),
                ratio=ratio_var.get())
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
    refresh_accounts()
    refresh()
    return frame
