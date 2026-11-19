"""Final / Locked Files inspector with explicit user-authorized Unlock for controlled repair."""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Any, Callable, Dict, List, Optional

from lapo.config import RuntimeConfig
from lapo.core.file_lock_manager import FileLockManager


class LockPanel(ttk.LabelFrame):
    """Displays finalized SHA-256 locked files, dependents, and controlled Unlock/Lock controls."""

    def __init__(
        self,
        parent: tk.Widget,
        get_workspace_dir: Callable[[], Optional[Path]],
        get_active_lock_manager: Callable[[], Optional[FileLockManager]],
        on_log: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        super().__init__(parent, text=" Final / Locked Files & Repair Queue ", padding=8)
        self._get_workspace_dir = get_workspace_dir
        self._get_active_lock_manager = get_active_lock_manager
        self._on_log = on_log

        self._build_ui()

    def _build_ui(self) -> None:
        toolbar = ttk.Frame(self, style="Panel.TFrame")
        toolbar.pack(fill="x", pady=(0, 6))

        ttk.Button(toolbar, text="🔄 Refresh", command=self.refresh_from_disk).pack(side="left", padx=(0, 4))
        ttk.Button(toolbar, text="🔒 Lock File...", command=self.manual_lock_file).pack(side="left", padx=(0, 4))
        ttk.Button(
            toolbar,
            text="🔓 Unlock Selected for Repair",
            style="Warning.TButton",
            command=self.unlock_selected,
        ).pack(side="right")

        cols = ("path", "sha256", "phase", "status", "dependents")
        tree_wrap = ttk.Frame(self, style="Panel.TFrame")
        tree_wrap.pack(fill="both", expand=True)

        self.tree = ttk.Treeview(tree_wrap, columns=cols, show="headings", height=6)
        self.tree.heading("path", text="File Path")
        self.tree.heading("sha256", text="SHA-256")
        self.tree.heading("phase", text="Phase")
        self.tree.heading("status", text="Status")
        self.tree.heading("dependents", text="Dependents")

        self.tree.column("path", width=200, anchor="w")
        self.tree.column("sha256", width=110, anchor="w")
        self.tree.column("phase", width=55, anchor="center")
        self.tree.column("status", width=105, anchor="center")
        self.tree.column("dependents", width=160, anchor="w")

        vbar = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vbar.pack(side="right", fill="y")

        self.lbl_repair = ttk.Label(self, text="Repair Queue: Empty", style="Muted.TLabel")
        self.lbl_repair.pack(anchor="w", pady=(6, 0))

    def _resolve_manager(self) -> Optional[FileLockManager]:
        active = self._get_active_lock_manager()
        if active is not None:
            return active
        ws = self._get_workspace_dir()
        if ws is None or not ws.is_dir():
            return None
        return FileLockManager(RuntimeConfig(workspace_dir=ws))

    def update_view(self, locks: List[Dict[str, Any]], repairs: Optional[List[Dict[str, Any]]] = None) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)

        for entry in locks:
            deps = ", ".join(entry.get("dependents", [])) or "-"
            self.tree.insert(
                "",
                "end",
                values=(
                    entry.get("path", ""),
                    str(entry.get("sha256", ""))[:12],
                    entry.get("owner_phase_id", ""),
                    entry.get("verification_status", ""),
                    deps,
                ),
            )

        if repairs:
            names = [f"{r.get('path')} (P{r.get('owner_phase_id')})" for r in repairs]
            self.lbl_repair.configure(text=f"Repair Queue ({len(repairs)}): {', '.join(names)}")
        else:
            self.lbl_repair.configure(text="Repair Queue: Empty")

    def refresh_from_disk(self) -> None:
        mgr = self._resolve_manager()
        if mgr is None:
            self.update_view([], [])
            return
        mgr.load()
        self.update_view(mgr.list_locks_dicts(), mgr.list_repair_queue_dicts())

    def manual_lock_file(self) -> None:
        ws = self._get_workspace_dir()
        if ws is None or not ws.is_dir():
            messagebox.showerror("Workspace Required", "Please select a valid Workspace folder first.", parent=self)
            return

        chosen = filedialog.askopenfilename(title="Select Workspace File to Lock", initialdir=str(ws), parent=self)
        if not chosen:
            return

        try:
            rel = Path(chosen).resolve().relative_to(ws.resolve()).as_posix()
            mgr = self._resolve_manager()
            if mgr is None:
                return
            mgr.lock_existing_file_manual(rel, owner_phase_id=0)
            self.refresh_from_disk()
            if self._on_log:
                self._on_log(f"Manually locked '{rel}'.", "info")
        except Exception as err:
            messagebox.showerror("Lock Error", f"Could not lock file:\n{err}", parent=self)

    def unlock_selected(self) -> None:
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("No Selection", "Select a locked file in the table first.", parent=self)
            return

        values = self.tree.item(sel[0], "values")
        rel_path = str(values[0])
        owner_phase = values[2]

        confirmed = messagebox.askyesno(
            "Authorize Controlled Unlock",
            f"Unlock '{rel_path}' (finalized in Phase {owner_phase}) for controlled repair?\n\n"
            "The model will be required to reread this file before editing it, and its owning "
            "phase checks must pass again to re-lock it before the active phase can close.",
            parent=self,
        )
        if not confirmed:
            return

        reason = (
            simpledialog.askstring(
                "Repair Reason",
                f"Optional repair note for '{rel_path}':",
                initialvalue="Authorized interface/bug repair",
                parent=self,
            )
            or "User authorized repair"
        )

        try:
            mgr = self._resolve_manager()
            if mgr is None:
                return
            mgr.unlock_for_repair(rel_path, reason=reason)
            self.refresh_from_disk()
            if self._on_log:
                self._on_log(f"Authorized repair unlock for '{rel_path}'.", "warning")
        except Exception as err:
            messagebox.showerror("Unlock Error", str(err), parent=self)