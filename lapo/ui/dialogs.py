"""Modal dialogs for Crash Recovery, Manual Phase Approval, and Final Review / Unload."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any, Dict, Optional, Tuple

from lapo.core.recovery_manager import RecoveryInspection
from lapo.models import PhaseDefinition, VerificationSuiteResult
from lapo.ui.theme_manager import ThemeManager


class _BaseModal(tk.Toplevel):
    def __init__(self, parent: tk.Misc, title: str, theme: ThemeManager, width: int = 620, height: int = 440) -> None:
        super().__init__(parent)
        self.title(title)
        self.theme = theme
        self.configure(bg=theme.current["bg"])
        self.transient(parent.winfo_toplevel())
        self.grab_set()
        self.geometry(f"{width}x{height}")
        self.minsize(460, 320)
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)

    def _on_cancel(self) -> None:
        self.destroy()

    def _center_over_parent(self, parent: tk.Misc) -> None:
        self.update_idletasks()
        root = parent.winfo_toplevel()
        px = root.winfo_rootx() + (root.winfo_width() - self.winfo_width()) // 2
        py = root.winfo_rooty() + (root.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{max(20, px)}+{max(20, py)}")


class RecoveryDialog(_BaseModal):
    """Prompts user to Resume an interrupted session, Archive & Start Fresh, or Cancel."""

    def __init__(self, parent: tk.Misc, theme: ThemeManager, inspection: RecoveryInspection) -> None:
        super().__init__(parent, "Session Recovery Detected", theme, width=580, height=360)
        self.choice: str = "cancel"  # "resume" | "fresh" | "cancel"

        body = ttk.Frame(self, style="Panel.TFrame", padding=14)
        body.pack(fill="both", expand=True)

        ttk.Label(body, text="An existing LAPO session was found in this workspace:", font=("Segoe UI", 10, "bold")).pack(
            anchor="w", pady=(0, 8)
        )

        details = [
            f"Plan ID: {inspection.plan_id or 'N/A'}",
            f"Active Phase: {inspection.active_phase_id} ({inspection.active_phase_name})",
            f"Completed Phases: {inspection.completed_phases} / {inspection.total_phases}",
            f"Saved Journal Messages: {inspection.journal_message_count}",
            f"Status: {inspection.reason}",
        ]
        if inspection.lock_integrity_issues:
            details.append(f"\nWARNING — Locked file integrity issues ({len(inspection.lock_integrity_issues)}):")
            for issue in inspection.lock_integrity_issues[:8]:
                details.append(f"  • {issue.get('path')}: {issue.get('issue')}")

        txt = tk.Text(body, height=10, wrap="word", font=("Consolas", 9))
        theme.style_text_widget(txt)
        txt.insert("1.0", "\n".join(details))
        txt.configure(state="disabled")
        txt.pack(fill="both", expand=True, pady=(0, 12))

        btns = ttk.Frame(body, style="Panel.TFrame")
        btns.pack(fill="x")

        if inspection.can_resume:
            ttk.Button(
                btns,
                text="▶ Resume Saved Session",
                style="Success.TButton",
                command=lambda: self._pick("resume"),
            ).pack(side="left", padx=(0, 8))

        ttk.Button(
            btns,
            text="Archive Old & Start Fresh",
            style="Warning.TButton",
            command=lambda: self._pick("fresh"),
        ).pack(side="left", padx=(0, 8))

        ttk.Button(btns, text="Cancel", command=lambda: self._pick("cancel")).pack(side="right")
        self._center_over_parent(parent)

    def _pick(self, value: str) -> None:
        self.choice = value
        self.destroy()


class ManualApprovalDialog(_BaseModal):
    """Requests explicit user approval when a phase has no automated verification commands."""

    def __init__(
        self,
        parent: tk.Misc,
        theme: ThemeManager,
        phase: PhaseDefinition,
        suite: VerificationSuiteResult,
    ) -> None:
        super().__init__(parent, f"Manual Approval — Phase {phase.id}: {phase.name}", theme, width=640, height=440)
        self.result: Tuple[bool, str] = (False, "Approval canceled by user.")

        body = ttk.Frame(self, style="Panel.TFrame", padding=14)
        body.pack(fill="both", expand=True)

        ttk.Label(
            body,
            text=f"Phase {phase.id} ('{phase.name}') is ready for your manual review.",
            font=("Segoe UI", 10, "bold"),
        ).pack(anchor="w", pady=(0, 6))

        ttk.Label(
            body,
            text="No automated verification commands were configured for this phase. Inspect workspace files before approving.",
            style="Muted.TLabel",
            wraplength=590,
        ).pack(anchor="w", pady=(0, 8))

        info_text = tk.Text(body, height=8, wrap="word", font=("Consolas", 9))
        theme.style_text_widget(info_text)
        files_str = "\n".join(f"  - {f}" for f in phase.created_or_modified_files) or "  (none recorded)"
        info_text.insert(
            "1.0",
            f"Worktree Fingerprint: {suite.worktree_fingerprint}\n\n"
            f"Created / Modified Files:\n{files_str}\n",
        )
        info_text.configure(state="disabled")
        info_text.pack(fill="both", expand=True, pady=(0, 8))

        ttk.Label(body, text="Revision Feedback (used if you click Reject & Revise):").pack(anchor="w")
        self.feedback_entry = ttk.Entry(body)
        self.feedback_entry.pack(fill="x", pady=(2, 12))

        btns = ttk.Frame(body, style="Panel.TFrame")
        btns.pack(fill="x")

        ttk.Button(
            btns,
            text="✅ Approve & Lock Phase",
            style="Success.TButton",
            command=self._approve,
        ).pack(side="left", padx=(0, 8))

        ttk.Button(
            btns,
            text="↩ Reject & Request Revision",
            style="Danger.TButton",
            command=self._reject,
        ).pack(side="right")

        self._center_over_parent(parent)

    def _approve(self) -> None:
        self.result = (True, "Approved via Desktop GUI")
        self.destroy()

    def _reject(self) -> None:
        note = self.feedback_entry.get().strip() or "User requested revisions before approving this phase."
        self.result = (False, note)
        self.destroy()


class FinalReviewDialog(_BaseModal):
    """Shows full execution & token report and asks for explicit approval + optional model unload."""

    def __init__(
        self,
        parent: tk.Misc,
        theme: ThemeManager,
        report_dict: Dict[str, Any],
        report_md: str,
    ) -> None:
        super().__init__(parent, "Final Project Review & Execution Report", theme, width=760, height=560)
        self.result: Tuple[bool, bool] = (False, False)  # (approved, request_unload)

        body = ttk.Frame(self, style="Panel.TFrame", padding=14)
        body.pack(fill="both", expand=True)

        ttk.Label(
            body,
            text="All project phases completed. Review the execution report and workspace outputs:",
            font=("Segoe UI", 10, "bold"),
        ).pack(anchor="w", pady=(0, 8))

        wrap = ttk.Frame(body, style="Panel.TFrame")
        wrap.pack(fill="both", expand=True, pady=(0, 10))

        txt = tk.Text(wrap, wrap="word", font=("Consolas", 9))
        vbar = ttk.Scrollbar(wrap, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=vbar.set)
        theme.style_text_widget(txt)
        txt.insert("1.0", report_md)
        txt.configure(state="disabled")
        txt.pack(side="left", fill="both", expand=True)
        vbar.pack(side="right", fill="y")

        self.unload_var = tk.BooleanVar(value=True)
        model_name = report_dict.get("active_model", "active model")
        ttk.Checkbutton(
            body,
            text=f"Send UNLOAD request to Strata/model server for '{model_name}' after approval",
            variable=self.unload_var,
        ).pack(anchor="w", pady=(0, 10))

        btns = ttk.Frame(body, style="Panel.TFrame")
        btns.pack(fill="x")

        ttk.Button(
            btns,
            text="✅ Approve Final Output",
            style="Success.TButton",
            command=self._approve,
        ).pack(side="left", padx=(0, 8))

        ttk.Button(
            btns,
            text="Close Without Unloading",
            command=self._close_only,
        ).pack(side="right")

        self._center_over_parent(parent)

    def _approve(self) -> None:
        self.result = (True, bool(self.unload_var.get()))
        self.destroy()

    def _close_only(self) -> None:
        self.result = (False, False)
        self.destroy()