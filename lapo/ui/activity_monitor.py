"""
Thread-safe, bounded-buffer Live Execution Monitor with 3 tabs:
- Activity Log
- Thinking Stream (Reasoning deltas)
- Generated Output (Content deltas)
Features: bottom-aware auto-scroll, preserved reading position, Ctrl+Wheel font zoom,
right-click menu, Copy/Select All, and safe middle-mouse handling on Linux.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional

from lapo.ui.theme_manager import ThemeManager


class BoundedStreamPane(ttk.Frame):
    """Read-only scrollable text monitor with bounded line retention and bottom-aware auto-scroll."""

    def __init__(
        self,
        parent: tk.Widget,
        theme_manager: ThemeManager,
        fg_key: str = "text_fg",
        font_size: int = 10,
        max_lines: int = 4000,
        on_font_zoom: Optional[Callable[[int], None]] = None,
    ) -> None:
        super().__init__(parent, style="Panel.TFrame")
        self.theme = theme_manager
        self.fg_key = fg_key
        self.font_size = font_size
        self.max_lines = max_lines
        self._on_font_zoom = on_font_zoom

        # Keep requested height modest so expand=True fills available space without pushing footer off-screen
        self.text = tk.Text(
            self,
            height=10,
            width=40,
            wrap="word",
            font=("Consolas", self.font_size),
            state="disabled",
            cursor="xterm",
        )
        self.vbar = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=self.vbar.set)

        self.text.pack(side="left", fill="both", expand=True)
        self.vbar.pack(side="right", fill="y")

        self._bind_interactions()
        self.apply_theme()

    def _bind_interactions(self) -> None:
        # Prevent accidental X11 middle-click PRIMARY-selection paste on Linux
        self.text.bind("<Button-2>", lambda _e: "break")
        self.text.bind("<ButtonRelease-2>", lambda _e: "break")

        # Ctrl + Mouse Wheel font zoom (Windows & Linux)
        self.text.bind("<Control-MouseWheel>", self._on_ctrl_wheel_win)
        self.text.bind("<Control-Button-4>", lambda _e: self._zoom(1))
        self.text.bind("<Control-Button-5>", lambda _e: self._zoom(-1))

        # Copy shortcut
        self.text.bind("<Control-c>", lambda _e: self._copy_selection())
        self.text.bind("<Control-a>", lambda _e: self._select_all())

        # Right-click context menu
        self._menu = tk.Menu(self.text, tearoff=0)
        self._menu.add_command(label="Copy", command=self._copy_selection)
        self._menu.add_command(label="Select All", command=self._select_all)
        self._menu.add_separator()
        self._menu.add_command(label="Zoom In (Ctrl+Wheel Up)", command=lambda: self._zoom(1))
        self._menu.add_command(label="Zoom Out (Ctrl+Wheel Down)", command=lambda: self._zoom(-1))
        self._menu.add_separator()
        self._menu.add_command(label="Clear Pane", command=self.clear)
        self.text.bind("<Button-3>", self._show_menu)

    def _show_menu(self, event: tk.Event) -> None:
        try:
            self._menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._menu.grab_release()

    def _copy_selection(self) -> str:
        try:
            sel = self.text.get("sel.first", "sel.last")
            if sel:
                self.clipboard_clear()
                self.clipboard_append(sel)
        except tk.TclError:
            pass
        return "break"

    def _select_all(self) -> str:
        self.text.tag_add("sel", "1.0", "end-1c")
        return "break"

    def _on_ctrl_wheel_win(self, event: tk.Event) -> str:
        delta = 1 if getattr(event, "delta", 0) > 0 else -1
        self._zoom(delta)
        return "break"

    def _zoom(self, step: int) -> str:
        new_size = max(7, min(22, self.font_size + step))
        if new_size != self.font_size:
            self.set_font_size(new_size)
            if self._on_font_zoom:
                self._on_font_zoom(new_size)
        return "break"

    def set_font_size(self, size: int) -> None:
        self.font_size = max(7, min(22, size))
        self.text.configure(font=("Consolas", self.font_size))

    def apply_theme(self) -> None:
        self.theme.style_text_widget(self.text, fg_key=self.fg_key)
        p = self.theme.current
        self.text.tag_configure("info", foreground=p["text_fg"])
        self.text.tag_configure("success", foreground=p["success"])
        self.text.tag_configure("warning", foreground=p["warning"])
        self.text.tag_configure("error", foreground=p["danger"])

    def append(self, chunk: str, tag: Optional[str] = None) -> None:
        if not chunk:
            return
        _, bottom_frac = self.text.yview()
        at_bottom = bottom_frac >= 0.98

        self.text.configure(state="normal")
        if tag:
            self.text.insert("end", chunk, (tag,))
        else:
            self.text.insert("end", chunk)

        total_lines = int(self.text.index("end-1c").split(".")[0])
        if total_lines > self.max_lines:
            excess = total_lines - self.max_lines
            self.text.delete("1.0", f"{excess + 1}.0")

        self.text.configure(state="disabled")
        if at_bottom:
            self.text.see("end")

    def clear(self) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")


class ActivityMonitor(ttk.LabelFrame):
    """Fixed-viewport 3-tab live monitor: Activity, Thinking Stream, Generated Output."""

    def __init__(
        self,
        parent: tk.Widget,
        theme_manager: ThemeManager,
        initial_font_size: int = 10,
        on_font_size_changed: Optional[Callable[[int], None]] = None,
    ) -> None:
        super().__init__(parent, text=" Live Execution Monitor ", padding=6)
        self.theme = theme_manager
        self._on_font_size_changed = on_font_size_changed

        header = ttk.Frame(self, style="Panel.TFrame")
        header.pack(fill="x", pady=(0, 6))

        self.lbl_context = ttk.Label(header, text="Context: Idle", style="Muted.TLabel")
        self.lbl_context.pack(side="left")

        self.ctx_progress = ttk.Progressbar(header, orient="horizontal", length=180, mode="determinate", maximum=100)
        self.ctx_progress.pack(side="right", padx=(8, 0))

        self.btn_clear_all = ttk.Button(header, text="Clear Logs", command=self.clear_all)
        self.btn_clear_all.pack(side="right")

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True)

        self.activity_pane = BoundedStreamPane(
            self.notebook,
            theme_manager,
            fg_key="text_fg",
            font_size=initial_font_size,
            on_font_zoom=self._sync_font_size,
        )
        self.thinking_pane = BoundedStreamPane(
            self.notebook,
            theme_manager,
            fg_key="thinking_fg",
            font_size=initial_font_size,
            on_font_zoom=self._sync_font_size,
        )
        self.output_pane = BoundedStreamPane(
            self.notebook,
            theme_manager,
            fg_key="output_fg",
            font_size=initial_font_size,
            on_font_zoom=self._sync_font_size,
        )

        self.notebook.add(self.activity_pane, text=" Activity ")
        self.notebook.add(self.thinking_pane, text=" Thinking Stream ")
        self.notebook.add(self.output_pane, text=" Generated Output ")

    def _sync_font_size(self, new_size: int) -> None:
        self.activity_pane.set_font_size(new_size)
        self.thinking_pane.set_font_size(new_size)
        self.output_pane.set_font_size(new_size)
        if self._on_font_size_changed:
            self._on_font_size_changed(new_size)

    def apply_theme(self) -> None:
        self.activity_pane.apply_theme()
        self.thinking_pane.apply_theme()
        self.output_pane.apply_theme()

    def append_activity(self, line: str, level: str = "info") -> None:
        if not line.endswith("\n"):
            line += "\n"
        self.activity_pane.append(line, tag=level)

    def append_thinking(self, delta: str) -> None:
        self.thinking_pane.append(delta)

    def append_output(self, delta: str) -> None:
        self.output_pane.append(delta)

    def update_context_display(self, summary: str, pct: float) -> None:
        self.lbl_context.configure(text=summary)
        self.ctx_progress["value"] = max(0.0, min(100.0, pct))

    def clear_all(self) -> None:
        self.activity_pane.clear()
        self.thinking_pane.clear()
        self.output_pane.clear()