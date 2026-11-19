"""Excel-sheet-style multi-phase builder with horizontal tab overflow navigation."""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, simpledialog, ttk
from typing import Any, Callable, Dict, List, Optional

from lapo.ui.theme_manager import ThemeManager


class PhaseEditor(ttk.LabelFrame):
    """
    Allows creating independent ordered phases with:
    - Horizontal tab strip + Left/Right overflow navigation buttons
    - Add Phase / Rename / Remove
    - Per-phase multiline prompt & verification commands
    - Explicit 'Finish Phases Input' lock-in before execution
    """

    def __init__(
        self,
        parent: tk.Widget,
        theme_manager: ThemeManager,
        on_phases_changed: Optional[Callable[[List[Dict[str, Any]], bool], None]] = None,
    ) -> None:
        super().__init__(parent, text=" Phase Builder (Independent Milestones) ", padding=8)
        self.theme = theme_manager
        self._on_phases_changed = on_phases_changed

        self._phases: List[Dict[str, Any]] = [
            {
                "name": "Phase 1: Foundation",
                "prompt": "Design core architecture and shared interfaces for this project.",
                "verification_commands": [],
            }
        ]
        self._selected_index: int = 0
        self._tab_scroll_offset: int = 0
        self._visible_tab_window: int = 4
        self._plan_locked: bool = False
        self._active_running_phase_id: Optional[int] = None

        self._tab_buttons: List[tk.Button] = []
        self._build_ui()
        self._render_tabs()
        self._load_selected_into_editors()

    def _build_ui(self) -> None:
        # Row 1: Action toolbar
        bar = ttk.Frame(self, style="Panel.TFrame")
        bar.pack(fill="x", pady=(0, 6))

        self.btn_add = ttk.Button(bar, text="+ Add Phase", command=self.add_phase)
        self.btn_add.pack(side="left", padx=(0, 4))

        self.btn_rename = ttk.Button(bar, text="Rename", command=self.rename_phase)
        self.btn_rename.pack(side="left", padx=(0, 4))

        self.btn_remove = ttk.Button(bar, text="Remove", command=self.remove_phase)
        self.btn_remove.pack(side="left", padx=(0, 8))

        self.btn_finish = ttk.Button(
            bar,
            text="Finish Phases Input",
            style="Accent.TButton",
            command=self.toggle_finish_phases,
        )
        self.btn_finish.pack(side="right")

        self.lbl_lock_state = ttk.Label(bar, text="Draft (unlocked)", style="Muted.TLabel")
        self.lbl_lock_state.pack(side="right", padx=(0, 8))

        # Row 2: Excel-like horizontal phase tab bar with < > arrows
        tab_row = ttk.Frame(self, style="Panel.TFrame")
        tab_row.pack(fill="x", pady=(0, 6))

        self.btn_prev_tabs = ttk.Button(tab_row, text="◀", width=3, command=self._scroll_tabs_left)
        self.btn_prev_tabs.pack(side="left")

        self.tabs_container = ttk.Frame(tab_row, style="Panel.TFrame")
        self.tabs_container.pack(side="left", fill="x", expand=True, padx=4)

        self.btn_next_tabs = ttk.Button(tab_row, text="▶", width=3, command=self._scroll_tabs_right)
        self.btn_next_tabs.pack(side="right")

        # Row 3: Active phase prompt editor
        ttk.Label(self, text="Phase Prompt (Only this milestone will be implemented when active):").pack(
            anchor="w", pady=(2, 2)
        )
        prompt_wrap = ttk.Frame(self, style="Panel.TFrame")
        prompt_wrap.pack(fill="both", expand=True)

        self.prompt_text = tk.Text(prompt_wrap, height=7, wrap="word", font=("Consolas", 10), undo=True)
        prompt_scroll = ttk.Scrollbar(prompt_wrap, orient="vertical", command=self.prompt_text.yview)
        self.prompt_text.configure(yscrollcommand=prompt_scroll.set)
        self.prompt_text.pack(side="left", fill="both", expand=True)
        prompt_scroll.pack(side="right", fill="y")
        self.prompt_text.bind("<KeyRelease>", lambda _e: self._save_editors_to_selected())
        self._attach_context_menu(self.prompt_text)

        # Row 4: Verification commands editor
        ttk.Label(
            self,
            text="Verification Commands (optional, one command per line — e.g. `pytest -q` or `python -m unittest`):",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(6, 2))
        cmd_wrap = ttk.Frame(self, style="Panel.TFrame")
        cmd_wrap.pack(fill="x")

        self.cmds_text = tk.Text(cmd_wrap, height=3, wrap="none", font=("Consolas", 9), undo=True)
        cmd_scroll = ttk.Scrollbar(cmd_wrap, orient="vertical", command=self.cmds_text.yview)
        self.cmds_text.configure(yscrollcommand=cmd_scroll.set)
        self.cmds_text.pack(side="left", fill="x", expand=True)
        cmd_scroll.pack(side="right", fill="y")
        self.cmds_text.bind("<KeyRelease>", lambda _e: self._save_editors_to_selected())
        self._attach_context_menu(self.cmds_text)

        self.apply_theme()

    def _attach_context_menu(self, widget: tk.Text) -> None:
        menu = tk.Menu(widget, tearoff=0)
        menu.add_command(label="Cut", command=lambda: widget.event_generate("<<Cut>>"))
        menu.add_command(label="Copy", command=lambda: widget.event_generate("<<Copy>>"))
        menu.add_command(label="Paste", command=lambda: widget.event_generate("<<Paste>>"))
        menu.add_separator()
        menu.add_command(label="Select All", command=lambda: widget.tag_add("sel", "1.0", "end-1c"))

        def _popup(event: tk.Event) -> None:
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()

        widget.bind("<Button-3>", _popup)

    def apply_theme(self) -> None:
        self.theme.style_text_widget(self.prompt_text)
        self.theme.style_text_widget(self.cmds_text)
        self._render_tabs()

    # ------------------------------------------------------------------
    # Tab Rendering & Overflow Navigation
    # ------------------------------------------------------------------
    def _scroll_tabs_left(self) -> None:
        if self._tab_scroll_offset > 0:
            self._tab_scroll_offset -= 1
            self._render_tabs()

    def _scroll_tabs_right(self) -> None:
        max_off = max(0, len(self._phases) - self._visible_tab_window)
        if self._tab_scroll_offset < max_off:
            self._tab_scroll_offset += 1
            self._render_tabs()

    def _ensure_selected_visible(self) -> None:
        if self._selected_index < self._tab_scroll_offset:
            self._tab_scroll_offset = self._selected_index
        elif self._selected_index >= self._tab_scroll_offset + self._visible_tab_window:
            self._tab_scroll_offset = self._selected_index - self._visible_tab_window + 1

    def _render_tabs(self) -> None:
        for btn in self._tab_buttons:
            btn.destroy()
        self._tab_buttons.clear()

        p = self.theme.current
        total = len(self._phases)
        start = self._tab_scroll_offset
        end = min(total, start + self._visible_tab_window)

        for idx in range(start, end):
            phase = self._phases[idx]
            phase_num = idx + 1
            is_selected = idx == self._selected_index
            is_running = self._active_running_phase_id == phase_num

            prefix = "▶ " if is_running else ""
            label = f"{prefix}P{phase_num}: {phase['name']}"
            if len(label) > 22:
                label = label[:20] + "…"

            bg = p["accent"] if is_selected else (p["success"] if is_running else p["tab_bg"])
            fg = "#ffffff" if (is_selected or is_running) else p["fg"]

            btn = tk.Button(
                self.tabs_container,
                text=label,
                bg=bg,
                fg=fg,
                activebackground=p["accent_hover"],
                activeforeground="#ffffff",
                relief="flat",
                bd=1,
                padx=8,
                pady=3,
                cursor="hand2",
                font=("Segoe UI", 9, "bold" if is_selected else "normal"),
                command=lambda i=idx: self.select_phase(i),
            )
            btn.pack(side="left", padx=(0, 3))
            self._tab_buttons.append(btn)

        self.btn_prev_tabs.configure(state="normal" if self._tab_scroll_offset > 0 else "disabled")
        self.btn_next_tabs.configure(
            state="normal" if (self._tab_scroll_offset + self._visible_tab_window) < total else "disabled"
        )

    # ------------------------------------------------------------------
    # CRUD & State Synchronization
    # ------------------------------------------------------------------
    def _save_editors_to_selected(self) -> None:
        if self._plan_locked or not (0 <= self._selected_index < len(self._phases)):
            return
        self._phases[self._selected_index]["prompt"] = self.prompt_text.get("1.0", "end-1c").strip()
        raw_cmds = self.cmds_text.get("1.0", "end-1c").splitlines()
        self._phases[self._selected_index]["verification_commands"] = [
            c.strip() for c in raw_cmds if c.strip()
        ]
        self._notify()

    def _load_selected_into_editors(self) -> None:
        if not (0 <= self._selected_index < len(self._phases)):
            return
        phase = self._phases[self._selected_index]

        was_locked = self._plan_locked
        self.prompt_text.configure(state="normal")
        self.cmds_text.configure(state="normal")

        self.prompt_text.delete("1.0", "end")
        self.prompt_text.insert("1.0", phase.get("prompt", ""))

        self.cmds_text.delete("1.0", "end")
        self.cmds_text.insert("1.0", "\n".join(phase.get("verification_commands", [])))

        if was_locked:
            self.prompt_text.configure(state="disabled")
            self.cmds_text.configure(state="disabled")

    def select_phase(self, index: int) -> None:
        self._save_editors_to_selected()
        if 0 <= index < len(self._phases):
            self._selected_index = index
            self._ensure_selected_visible()
            self._render_tabs()
            self._load_selected_into_editors()

    def add_phase(self) -> None:
        if self._plan_locked:
            return
        self._save_editors_to_selected()
        new_num = len(self._phases) + 1
        self._phases.append(
            {
                "name": f"Phase {new_num}",
                "prompt": "",
                "verification_commands": [],
            }
        )
        self._selected_index = len(self._phases) - 1
        self._ensure_selected_visible()
        self._render_tabs()
        self._load_selected_into_editors()
        self.prompt_text.focus_set()
        self._notify()

    def rename_phase(self) -> None:
        if self._plan_locked:
            return
        cur = self._phases[self._selected_index]
        new_name = simpledialog.askstring("Rename Phase", "Enter phase name:", initialvalue=cur["name"], parent=self)
        if new_name and new_name.strip():
            cur["name"] = new_name.strip()
            self._render_tabs()
            self._notify()

    def remove_phase(self) -> None:
        if self._plan_locked:
            return
        if len(self._phases) <= 1:
            messagebox.showwarning("Minimum Phases", "At least one phase is required.", parent=self)
            return
        self._phases.pop(self._selected_index)
        self._selected_index = min(self._selected_index, len(self._phases) - 1)
        self._tab_scroll_offset = min(self._tab_scroll_offset, max(0, len(self._phases) - self._visible_tab_window))
        self._render_tabs()
        self._load_selected_into_editors()
        self._notify()

    def toggle_finish_phases(self) -> None:
        if not self._plan_locked:
            self._save_editors_to_selected()
            for idx, ph in enumerate(self._phases, start=1):
                if not ph.get("prompt", "").strip():
                    messagebox.showerror(
                        "Empty Phase Prompt",
                        f"Phase {idx} ('{ph['name']}') has an empty prompt. Please fill it before finishing.",
                        parent=self,
                    )
                    self.select_phase(idx - 1)
                    return
            self.set_plan_locked(True)
        else:
            self.set_plan_locked(False)

    def set_plan_locked(self, locked: bool) -> None:
        self._plan_locked = locked
        state = "disabled" if locked else "normal"
        self.btn_add.configure(state=state)
        self.btn_rename.configure(state=state)
        self.btn_remove.configure(state=state)
        self.prompt_text.configure(state=state)
        self.cmds_text.configure(state=state)

        if locked:
            self.btn_finish.configure(text="Edit Phases")
            self.lbl_lock_state.configure(text=f"Locked ({len(self._phases)} phases ready)")
        else:
            self.btn_finish.configure(text="Finish Phases Input")
            self.lbl_lock_state.configure(text="Draft (unlocked)")
        self._notify()

    @property
    def is_plan_locked(self) -> bool:
        return self._plan_locked

    def get_phases(self) -> List[Dict[str, Any]]:
        self._save_editors_to_selected()
        return [
            {
                "name": p["name"],
                "prompt": p["prompt"],
                "verification_commands": list(p.get("verification_commands", [])),
            }
            for p in self._phases
        ]

    def set_phases(self, phases: List[Dict[str, Any]], locked: bool = False) -> None:
        if not phases:
            return
        self.set_plan_locked(False)
        self._phases = [
            {
                "name": str(p.get("name") or f"Phase {i + 1}"),
                "prompt": str(p.get("prompt") or ""),
                "verification_commands": list(p.get("verification_commands", [])),
            }
            for i, p in enumerate(phases)
        ]
        self._selected_index = 0
        self._tab_scroll_offset = 0
        self._render_tabs()
        self._load_selected_into_editors()
        self.set_plan_locked(locked)

    def highlight_active_phase(self, phase_id: Optional[int]) -> None:
        self._active_running_phase_id = phase_id
        self._render_tabs()

    def _notify(self) -> None:
        if self._on_phases_changed:
            self._on_phases_changed(self._phases, self._plan_locked)