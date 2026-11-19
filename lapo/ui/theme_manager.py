"""Light/Dark theme styling and persistent UI preference storage for LAPO."""

from __future__ import annotations

import json
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import Any, Dict

PREFS_FILE = Path.home() / ".lapo_ui_prefs.json"

DARK_THEME: Dict[str, str] = {
    "name": "dark",
    "bg": "#0e141b",
    "panel_bg": "#141d26",
    "card_bg": "#18232e",
    "fg": "#e6edf3",
    "muted_fg": "#8b9eb0",
    "accent": "#2f81f7",
    "accent_hover": "#388bfd",
    "success": "#2ea043",
    "warning": "#d29922",
    "danger": "#f85149",
    "border": "#2a394a",
    "entry_bg": "#0b1117",
    "entry_fg": "#e6edf3",
    "text_bg": "#0b1016",
    "text_fg": "#d2deea",
    "thinking_fg": "#e3b341",
    "output_fg": "#79c0ff",
    "select_bg": "#264f78",
    "select_fg": "#ffffff",
    "tab_bg": "#131c25",
    "tab_active_bg": "#233242",
}

LIGHT_THEME: Dict[str, str] = {
    "name": "light",
    "bg": "#f3f5f8",
    "panel_bg": "#ffffff",
    "card_bg": "#eef2f6",
    "fg": "#1c2430",
    "muted_fg": "#576574",
    "accent": "#0969da",
    "accent_hover": "#0550ae",
    "success": "#1a7f37",
    "warning": "#9a6700",
    "danger": "#cf222e",
    "border": "#d0d7de",
    "entry_bg": "#ffffff",
    "entry_fg": "#1c2430",
    "text_bg": "#ffffff",
    "text_fg": "#1c2430",
    "thinking_fg": "#7a4f01",
    "output_fg": "#0550ae",
    "select_bg": "#b6d4fe",
    "select_fg": "#0b1f3a",
    "tab_bg": "#e6ebf1",
    "tab_active_bg": "#ffffff",
}


def load_ui_prefs() -> Dict[str, Any]:
    defaults: Dict[str, Any] = {
        "theme": "dark",
        "font_size": 10,
        "last_api_url": "http://127.0.0.1:47831/v1",
        "last_workspace": "",
        "last_skills": "",
    }
    if not PREFS_FILE.is_file():
        return defaults
    try:
        loaded = json.loads(PREFS_FILE.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            defaults.update(loaded)
    except Exception:
        pass
    return defaults


def save_ui_prefs(prefs: Dict[str, Any]) -> None:
    try:
        # Never persist secrets/API keys
        safe = {k: v for k, v in prefs.items() if "key" not in k.lower() and "secret" not in k.lower()}
        PREFS_FILE.write_text(json.dumps(safe, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


class ThemeManager:
    """Applies cohesive Light or Dark palettes across ttk and classic tk widgets."""

    def __init__(self, root: tk.Tk, initial_theme: str = "dark") -> None:
        self.root = root
        self.style = ttk.Style(root)
        try:
            self.style.theme_use("clam")
        except tk.TclError:
            pass
        self.current: Dict[str, str] = DARK_THEME if initial_theme != "light" else LIGHT_THEME

    def apply(self, theme_name: str) -> Dict[str, str]:
        palette = LIGHT_THEME if theme_name == "light" else DARK_THEME
        self.current = palette
        s = self.style

        self.root.configure(bg=palette["bg"])
        self.root.option_add("*TCombobox*Listbox.background", palette["entry_bg"])
        self.root.option_add("*TCombobox*Listbox.foreground", palette["entry_fg"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", palette["select_bg"])
        self.root.option_add("*TCombobox*Listbox.selectForeground", palette["select_fg"])

        s.configure(".", background=palette["bg"], foreground=palette["fg"], font=("Segoe UI", 9))
        s.configure("TFrame", background=palette["bg"])
        s.configure("Panel.TFrame", background=palette["panel_bg"])
        s.configure("Card.TFrame", background=palette["card_bg"])

        s.configure(
            "TLabelframe",
            background=palette["panel_bg"],
            foreground=palette["accent"],
            bordercolor=palette["border"],
            lightcolor=palette["border"],
            darkcolor=palette["border"],
        )
        s.configure(
            "TLabelframe.Label",
            background=palette["panel_bg"],
            foreground=palette["accent"],
            font=("Segoe UI", 9, "bold"),
        )

        s.configure("TLabel", background=palette["panel_bg"], foreground=palette["fg"])
        s.configure("Bg.TLabel", background=palette["bg"], foreground=palette["fg"])
        s.configure("Muted.TLabel", background=palette["panel_bg"], foreground=palette["muted_fg"])
        s.configure("Title.TLabel", background=palette["bg"], foreground=palette["fg"], font=("Segoe UI", 12, "bold"))

        s.configure(
            "TEntry",
            fieldbackground=palette["entry_bg"],
            foreground=palette["entry_fg"],
            insertcolor=palette["fg"],
            bordercolor=palette["border"],
        )
        s.configure(
            "TCombobox",
            fieldbackground=palette["entry_bg"],
            background=palette["card_bg"],
            foreground=palette["entry_fg"],
            arrowcolor=palette["fg"],
            bordercolor=palette["border"],
        )
        s.map(
            "TCombobox",
            fieldbackground=[("readonly", palette["entry_bg"])],
            foreground=[("readonly", palette["entry_fg"])],
        )

        s.configure(
            "TButton",
            background=palette["card_bg"],
            foreground=palette["fg"],
            bordercolor=palette["border"],
            padding=(8, 4),
        )
        s.map(
            "TButton",
            background=[("active", palette["tab_active_bg"]), ("disabled", palette["bg"])],
            foreground=[("disabled", palette["muted_fg"])],
        )

        s.configure(
            "Accent.TButton",
            background=palette["accent"],
            foreground="#ffffff",
            font=("Segoe UI", 9, "bold"),
            padding=(10, 5),
        )
        s.map(
            "Accent.TButton",
            background=[("active", palette["accent_hover"]), ("disabled", palette["border"])],
        )

        s.configure(
            "Success.TButton",
            background=palette["success"],
            foreground="#ffffff",
            font=("Segoe UI", 9, "bold"),
            padding=(12, 6),
        )
        s.map("Success.TButton", background=[("disabled", palette["border"])])

        s.configure(
            "Warning.TButton",
            background=palette["warning"],
            foreground="#0e141b",
            font=("Segoe UI", 9, "bold"),
            padding=(10, 5),
        )
        s.map("Warning.TButton", background=[("disabled", palette["border"])])

        s.configure(
            "Danger.TButton",
            background=palette["danger"],
            foreground="#ffffff",
            font=("Segoe UI", 9, "bold"),
            padding=(10, 5),
        )
        s.map("Danger.TButton", background=[("disabled", palette["border"])])

        s.configure("TNotebook", background=palette["panel_bg"], bordercolor=palette["border"])
        s.configure(
            "TNotebook.Tab",
            background=palette["tab_bg"],
            foreground=palette["muted_fg"],
            padding=(10, 4),
        )
        s.map(
            "TNotebook.Tab",
            background=[("selected", palette["tab_active_bg"])],
            foreground=[("selected", palette["fg"])],
        )

        s.configure(
            "Treeview",
            background=palette["text_bg"],
            fieldbackground=palette["text_bg"],
            foreground=palette["text_fg"],
            bordercolor=palette["border"],
            rowheight=24,
        )
        s.configure(
            "Treeview.Heading",
            background=palette["card_bg"],
            foreground=palette["fg"],
            font=("Segoe UI", 9, "bold"),
        )
        s.map("Treeview", background=[("selected", palette["select_bg"])])

        s.configure(
            "Horizontal.TProgressbar",
            troughcolor=palette["entry_bg"],
            background=palette["accent"],
            bordercolor=palette["border"],
        )
        s.configure("TPanedwindow", background=palette["border"])

        return palette

    def style_text_widget(self, widget: tk.Text, fg_key: str = "text_fg") -> None:
        p = self.current
        widget.configure(
            bg=p["text_bg"],
            fg=p.get(fg_key, p["text_fg"]),
            insertbackground=p["fg"],
            selectbackground=p["select_bg"],
            selectforeground=p["select_fg"],
            highlightbackground=p["border"],
            highlightcolor=p["accent"],
            highlightthickness=1,
            relief="flat",
        )