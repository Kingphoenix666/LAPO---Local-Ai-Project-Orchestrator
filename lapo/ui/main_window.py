"""
Main Desktop GUI window for Local AI Project Orchestrator (LAPO).
Uses 100% of the shared core engine (`ProjectOrchestrator`) via thread-safe EventBus polling.
Never touches Tkinter widgets directly from background threads.
"""

from __future__ import annotations

import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Dict, List, Optional, Tuple

from lapo import __app_name__, __version__
from lapo.adapters.strata_adapter import StrataAdapter
from lapo.config import (
    DEFAULT_API_BASE_URL,
    DEFAULT_API_KEY,
    SUPPORTED_TOOL_PROTOCOLS,
    RuntimeConfig,
)
from lapo.core.live_request_manager import publish_live_request_file
from lapo.core.orchestrator import ProjectOrchestrator
from lapo.core.phase_manager import compute_plan_id
from lapo.core.recovery_manager import RecoveryManager
from lapo.events import EventBus, EventType
from lapo.models import (
    ModelAvailability,
    ModelInfo,
    PhaseDefinition,
    VerificationSuiteResult,
    atomic_write_json,
    read_json_safe,
)
from lapo.ui.activity_monitor import ActivityMonitor
from lapo.ui.dialogs import FinalReviewDialog, ManualApprovalDialog, RecoveryDialog
from lapo.ui.lock_panel import LockPanel
from lapo.ui.phase_editor import PhaseEditor
from lapo.ui.theme_manager import ThemeManager, load_ui_prefs, save_ui_prefs


CONTEXT_PRESETS = ("auto", "32K", "64K", "128K", "256K", "512K", "1M")


class LapoMainWindow:
    """Responsive, cross-platform Desktop GUI for LAPO."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.prefs = load_ui_prefs()

        self.root.title(f"{__app_name__} v{__version__}")

        # Fit cleanly inside the visible screen area above the taskbar on any DPI scaling
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        win_w = min(1280, max(940, screen_w - 60))
        win_h = min(820, max(620, screen_h - 100))
        pos_x = max(10, (screen_w - win_w) // 2)
        pos_y = max(10, (screen_h - win_h) // 2 - 15)
        self.root.geometry(f"{win_w}x{win_h}+{pos_x}+{pos_y}")
        self.root.minsize(920, 600)

        self.theme = ThemeManager(root, initial_theme=self.prefs.get("theme", "dark"))
        self.theme.apply(self.prefs.get("theme", "dark"))

        self._event_bus = EventBus()
        self._orchestrator: Optional[ProjectOrchestrator] = None
        self._worker_thread: Optional[threading.Thread] = None
        self._discovered_models: Dict[str, ModelInfo] = {}

        self.var_api_url = tk.StringVar(value=self.prefs.get("last_api_url") or DEFAULT_API_BASE_URL)
        self.var_api_key = tk.StringVar(value=DEFAULT_API_KEY)
        self.var_model_display = tk.StringVar(value="")
        self.var_conn_status = tk.StringVar(value="Not checked")

        self.var_workspace = tk.StringVar(value=self.prefs.get("last_workspace", ""))
        self.var_skills = tk.StringVar(value=self.prefs.get("last_skills", ""))

        self.var_context = tk.StringVar(value="auto")
        self.var_compaction_pct = tk.StringVar(value="75")
        self.var_max_gen = tk.StringVar(value="32768")
        self.var_read_timeout = tk.StringVar(value="5400")
        self.var_tool_protocol = tk.StringVar(value="auto")
        self.var_unattended_auto_verify = tk.BooleanVar(value=True)

        self.var_live_phase = tk.StringVar(value="active")
        self.var_status = tk.StringVar(value="Status: Ready")
        self.var_phase_progress = tk.StringVar(value="Phase: -/-")

        self._build_layout()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.root.after(50, self._poll_events)
        self.root.after(250, self.fetch_models_async)

    def _build_layout(self) -> None:
        # 1. Top Title & Theme Bar
        top_bar = ttk.Frame(self.root, padding=(12, 8, 12, 4))
        top_bar.pack(fill="x", side="top")

        ttk.Label(top_bar, text=f"LAPO — Local AI Project Orchestrator v{__version__}", style="Title.TLabel").pack(
            side="left"
        )

        ttk.Button(top_bar, text="🌓 Toggle Theme", command=self.toggle_theme).pack(side="right", padx=(6, 0))
        ttk.Button(top_bar, text="💾 Save Config", command=self.export_project_config).pack(side="right", padx=(6, 0))
        ttk.Button(top_bar, text="📂 Load Config", command=self.import_project_config).pack(side="right")

        # 2. Compact Configuration Strip (Server + Paths + Context)
        cfg_strip = ttk.Frame(self.root, padding=(12, 2, 12, 6))
        cfg_strip.pack(fill="x", side="top")

        f_srv = ttk.LabelFrame(cfg_strip, text=" 1. Strata / Local API ", padding=8)
        f_srv.pack(side="left", fill="both", expand=True, padx=(0, 6))

        r0 = ttk.Frame(f_srv, style="Panel.TFrame")
        r0.pack(fill="x", pady=(0, 4))
        ttk.Label(r0, text="API URL:", width=8).pack(side="left")
        ttk.Entry(r0, textvariable=self.var_api_url, width=28).pack(side="left", fill="x", expand=True, padx=(0, 6))
        ttk.Label(r0, text="Key:").pack(side="left")
        ttk.Entry(r0, textvariable=self.var_api_key, width=12, show="*").pack(side="left", padx=(4, 0))

        r1 = ttk.Frame(f_srv, style="Panel.TFrame")
        r1.pack(fill="x")
        self.btn_fetch = ttk.Button(r1, text="🔄 Fetch Models", style="Accent.TButton", command=self.fetch_models_async)
        self.btn_fetch.pack(side="left", padx=(0, 6))
        self.combo_models = ttk.Combobox(r1, textvariable=self.var_model_display, state="readonly", width=32)
        self.combo_models.pack(side="left", fill="x", expand=True)

        self.lbl_conn = ttk.Label(f_srv, textvariable=self.var_conn_status, style="Muted.TLabel")
        self.lbl_conn.pack(anchor="w", pady=(4, 0))

        f_paths = ttk.LabelFrame(cfg_strip, text=" 2. Workspace, Skills & Context Safety ", padding=8)
        f_paths.pack(side="left", fill="both", expand=True)

        p0 = ttk.Frame(f_paths, style="Panel.TFrame")
        p0.pack(fill="x", pady=(0, 4))
        ttk.Label(p0, text="Workspace:", width=10).pack(side="left")
        ttk.Entry(p0, textvariable=self.var_workspace).pack(side="left", fill="x", expand=True, padx=(0, 4))
        ttk.Button(p0, text="Browse", command=self._browse_workspace).pack(side="left")

        p1 = ttk.Frame(f_paths, style="Panel.TFrame")
        p1.pack(fill="x", pady=(0, 4))
        ttk.Label(p1, text="Skills (opt):", width=10).pack(side="left")
        ttk.Entry(p1, textvariable=self.var_skills).pack(side="left", fill="x", expand=True, padx=(0, 4))
        ttk.Button(p1, text="Browse", command=self._browse_skills).pack(side="left")

        p2 = ttk.Frame(f_paths, style="Panel.TFrame")
        p2.pack(fill="x", pady=(0, 4))
        ttk.Label(p2, text="Context:").pack(side="left")
        ttk.Combobox(p2, textvariable=self.var_context, values=CONTEXT_PRESETS, width=7).pack(side="left", padx=(3, 6))

        ttk.Label(p2, text="Compact %:").pack(side="left")
        ttk.Entry(p2, textvariable=self.var_compaction_pct, width=4).pack(side="left", padx=(3, 6))

        ttk.Label(p2, text="MaxGen:").pack(side="left")
        ttk.Entry(p2, textvariable=self.var_max_gen, width=6).pack(side="left", padx=(3, 6))

        ttk.Label(p2, text="Timeout:").pack(side="left")
        ttk.Entry(p2, textvariable=self.var_read_timeout, width=5).pack(side="left", padx=(3, 6))

        ttk.Label(p2, text="Tools:").pack(side="left")
        ttk.Combobox(
            p2,
            textvariable=self.var_tool_protocol,
            values=SUPPORTED_TOOL_PROTOCOLS,
            state="readonly",
            width=6,
        ).pack(side="left", padx=(3, 0))

        p3 = ttk.Frame(f_paths, style="Panel.TFrame")
        p3.pack(fill="x")
        ttk.Checkbutton(
            p3,
            text="Auto-Verify & Continue Between Phases (Unattended Mode — no manual approval popups)",
            variable=self.var_unattended_auto_verify,
        ).pack(side="left")

        # 3. Persistent Footer Execution Controls — packed BEFORE `paned` so it is NEVER clipped!
        footer = ttk.Frame(self.root, padding=(12, 8, 12, 10))
        footer.pack(fill="x", side="bottom")
        ttk.Separator(self.root, orient="horizontal").pack(fill="x", side="bottom")

        self.btn_start = ttk.Button(footer, text="🚀 Start", style="Success.TButton", command=self.start_execution)
        self.btn_start.pack(side="left", padx=(0, 6))

        self.btn_resume_session = ttk.Button(
            footer,
            text="↺ Recover / Resume",
            style="Accent.TButton",
            command=self.resume_saved_session,
        )
        self.btn_resume_session.pack(side="left", padx=(0, 6))

        self.btn_pause = ttk.Button(footer, text="⏸ Pause", command=self.pause_execution, state="disabled")
        self.btn_pause.pack(side="left", padx=(0, 6))

        self.btn_resume_run = ttk.Button(footer, text="▶ Resume", command=self.resume_execution, state="disabled")
        self.btn_resume_run.pack(side="left", padx=(0, 6))

        self.btn_stop = ttk.Button(
            footer,
            text="⏹ Stop & Save",
            style="Danger.TButton",
            command=self.stop_execution,
            state="disabled",
        )
        self.btn_stop.pack(side="left", padx=(0, 12))

        ttk.Label(footer, textvariable=self.var_phase_progress, style="Bg.TLabel", font=("Segoe UI", 9, "bold")).pack(
            side="left", padx=(4, 12)
        )
        ttk.Label(footer, textvariable=self.var_status, style="Bg.TLabel").pack(side="right")

        # 4. Main Split Workspace (packed LAST so it strictly fills remaining middle space)
        paned = ttk.PanedWindow(self.root, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=12, pady=4)

        left_pane = ttk.Frame(paned)
        paned.add(left_pane, weight=45)

        self.left_notebook = ttk.Notebook(left_pane)
        self.left_notebook.pack(fill="both", expand=True)

        self.phase_editor = PhaseEditor(
            self.left_notebook,
            theme_manager=self.theme,
            on_phases_changed=self._on_phases_updated,
        )
        self.lock_panel = LockPanel(
            self.left_notebook,
            get_workspace_dir=self._get_workspace_path_safe,
            get_active_lock_manager=lambda: self._orchestrator.lock_manager if self._orchestrator else None,
            on_log=lambda msg, lvl: self.monitor.append_activity(msg, lvl),
        )
        self.left_notebook.add(self.phase_editor, text=" Phase Builder ")
        self.left_notebook.add(self.lock_panel, text=" Final / Locked Files ")

        right_pane = ttk.Frame(paned)
        paned.add(right_pane, weight=55)

        # Pack Live Request bar BEFORE monitor so it is also never clipped on compact screens
        f_live = ttk.LabelFrame(right_pane, text=" Live Mid-Flight User Request (user_request.json -> readed_) ", padding=6)
        f_live.pack(side="bottom", fill="x", pady=(6, 0))

        live_top = ttk.Frame(f_live, style="Panel.TFrame")
        live_top.pack(fill="x", pady=(0, 4))
        ttk.Label(live_top, text="Target Phase:").pack(side="left")
        self.combo_live_phase = ttk.Combobox(
            live_top,
            textvariable=self.var_live_phase,
            values=("active", "1"),
            state="readonly",
            width=12,
        )
        self.combo_live_phase.pack(side="left", padx=(4, 8))
        ttk.Button(
            live_top,
            text="⚡ Send Live Request",
            style="Warning.TButton",
            command=self.send_live_request,
        ).pack(side="right")

        self.live_prompt_text = tk.Text(f_live, height=2, wrap="word", font=("Consolas", 9))
        self.theme.style_text_widget(self.live_prompt_text)
        self.live_prompt_text.pack(fill="x")

        self.monitor = ActivityMonitor(
            right_pane,
            theme_manager=self.theme,
            initial_font_size=int(self.prefs.get("font_size", 10)),
            on_font_size_changed=self._on_font_size_changed,
        )
        self.monitor.pack(fill="both", expand=True)

        self._on_phases_updated(self.phase_editor.get_phases(), self.phase_editor.is_plan_locked)

    def toggle_theme(self) -> None:
        next_name = "light" if self.theme.current["name"] == "dark" else "dark"
        self.theme.apply(next_name)
        self.phase_editor.apply_theme()
        self.monitor.apply_theme()
        self.theme.style_text_widget(self.live_prompt_text)
        self.prefs["theme"] = next_name
        save_ui_prefs(self.prefs)

    def _on_font_size_changed(self, size: int) -> None:
        self.prefs["font_size"] = size
        save_ui_prefs(self.prefs)

    def _persist_basic_prefs(self) -> None:
        self.prefs["last_api_url"] = self.var_api_url.get().strip()
        self.prefs["last_workspace"] = self.var_workspace.get().strip()
        self.prefs["last_skills"] = self.var_skills.get().strip()
        save_ui_prefs(self.prefs)

    def _browse_workspace(self) -> None:
        path = filedialog.askdirectory(title="Select Project Workspace", parent=self.root)
        if path:
            self.var_workspace.set(path)
            self._persist_basic_prefs()
            self.lock_panel.refresh_from_disk()

    def _browse_skills(self) -> None:
        path = filedialog.askdirectory(title="Select Read-Only Skills Directory", parent=self.root)
        if path:
            self.var_skills.set(path)
            self._persist_basic_prefs()

    def _get_workspace_path_safe(self) -> Optional[Path]:
        raw = self.var_workspace.get().strip()
        return Path(raw).expanduser().resolve() if raw else None

    def _on_phases_updated(self, phases: List[Dict[str, Any]], _locked: bool) -> None:
        values = ["active"] + [str(i + 1) for i in range(len(phases))]
        self.combo_live_phase.configure(values=values)
        if self.var_live_phase.get() not in values:
            self.var_live_phase.set("active")

    def _build_runtime_config(self, require_model: bool = True) -> RuntimeConfig:
        ws = self.var_workspace.get().strip()
        if not ws:
            raise ValueError("Please select a Workspace directory.")

        selected_label = self.var_model_display.get().strip()
        selected_info = self._discovered_models.get(selected_label)
        model_id = selected_info.id if selected_info else selected_label

        if require_model:
            if not model_id:
                raise ValueError("Please click 'Fetch Models' and select a model first.")
            if selected_info and selected_info.availability == ModelAvailability.INSTALLED_NOT_LOADED:
                proceed = messagebox.askyesno(
                    "Model Not Currently Loaded",
                    f"Model '{model_id}' is installed in Strata but NOT currently served by {self.var_api_url.get()}.\n\n"
                    "Start anyway? (Recommended: load the model in Strata first and click Fetch Models.)",
                    parent=self.root,
                )
                if not proceed:
                    raise ValueError("Canceled: selected model is not currently loaded.")

        pct_val = float(self.var_compaction_pct.get().strip())
        if pct_val > 1.0:
            pct_val = pct_val / 100.0

        return RuntimeConfig(
            workspace_dir=Path(ws),
            skills_dir=Path(self.var_skills.get().strip()) if self.var_skills.get().strip() else None,
            api_base_url=self.var_api_url.get().strip(),
            api_key=self.var_api_key.get().strip(),
            model_name=model_id,
            context_setting=self.var_context.get().strip() or "auto",
            compaction_threshold_pct=pct_val,
            max_gen_tokens=int(self.var_max_gen.get().strip() or "32768"),
            unattended_auto_verify=bool(self.var_unattended_auto_verify.get()),
            read_timeout_s=int(self.var_read_timeout.get().strip() or "5400"),
            tool_protocol=self.var_tool_protocol.get().strip() or "auto",
        )

    def export_project_config(self) -> None:
        try:
            cfg = self._build_runtime_config(require_model=False)
            data = cfg.to_safe_dict()
            data["phases"] = self.phase_editor.get_phases()
            path = filedialog.asksaveasfilename(
                title="Save LAPO Project Config",
                defaultextension=".json",
                filetypes=[("JSON files", "*.json")],
                parent=self.root,
            )
            if path:
                atomic_write_json(Path(path), data)
                self.monitor.append_activity(f"Saved project config to {path}", "success")
        except Exception as err:
            messagebox.showerror("Save Config Error", str(err), parent=self.root)

    def import_project_config(self) -> None:
        path = filedialog.askopenfilename(
            title="Load LAPO Project Config",
            filetypes=[("JSON files", "*.json")],
            parent=self.root,
        )
        if not path:
            return
        data = read_json_safe(Path(path))
        if not isinstance(data, dict):
            messagebox.showerror("Load Error", "Selected file is not valid JSON.", parent=self.root)
            return

        if data.get("api_base_url"):
            self.var_api_url.set(str(data["api_base_url"]))
        if data.get("workspace_dir"):
            self.var_workspace.set(str(data["workspace_dir"]))
        if data.get("skills_dir"):
            self.var_skills.set(str(data["skills_dir"]))
        if data.get("context_setting") is not None:
            self.var_context.set(str(data["context_setting"]))
        if data.get("compaction_threshold_pct") is not None:
            pct = float(data["compaction_threshold_pct"])
            self.var_compaction_pct.set(str(int(pct * 100) if pct <= 1.0 else int(pct)))
        if data.get("max_gen_tokens") is not None:
            self.var_max_gen.set(str(data["max_gen_tokens"]))
        if data.get("unattended_auto_verify") is not None:
            self.var_unattended_auto_verify.set(bool(data["unattended_auto_verify"]))
        if data.get("read_timeout_s") is not None:
            self.var_read_timeout.set(str(data["read_timeout_s"]))
        if data.get("tool_protocol"):
            self.var_tool_protocol.set(str(data["tool_protocol"]))
        if isinstance(data.get("phases"), list) and data["phases"]:
            self.phase_editor.set_phases(data["phases"], locked=False)

        self.lock_panel.refresh_from_disk()
        self.fetch_models_async(preferred_model_id=data.get("model_name"))
        self.monitor.append_activity(f"Loaded project config from {path}", "success")

    def fetch_models_async(self, preferred_model_id: Optional[str] = None) -> None:
        url = self.var_api_url.get().strip()
        key = self.var_api_key.get().strip()
        if not url:
            messagebox.showerror("Invalid URL", "Please enter an API Base URL (e.g. http://127.0.0.1:47831/v1).")
            return

        self._persist_basic_prefs()
        self.var_conn_status.set(f"Querying {url}/models ...")
        self.btn_fetch.configure(state="disabled")

        def _worker() -> None:
            try:
                tmp_cfg = RuntimeConfig(workspace_dir=Path.cwd(), api_base_url=url, api_key=key)
                adapter = StrataAdapter(tmp_cfg)
                models, err = adapter.discover_models()
                self.root.after(0, lambda: self._apply_discovered_models(models, err, preferred_model_id))
            except Exception as exc:
                self.root.after(0, lambda: self._apply_discovered_models([], str(exc), preferred_model_id))

        threading.Thread(target=_worker, name="LAPO-ModelDiscovery", daemon=True).start()

    def _apply_discovered_models(
        self,
        models: List[ModelInfo],
        error: Optional[str],
        preferred_model_id: Optional[str],
    ) -> None:
        self.btn_fetch.configure(state="normal")
        self._discovered_models.clear()
        labels: List[str] = []

        for m in models:
            lbl = m.display_label
            self._discovered_models[lbl] = m
            labels.append(lbl)

        self.combo_models.configure(values=labels)

        if labels:
            chosen_idx = 0
            if preferred_model_id:
                for i, lbl in enumerate(labels):
                    if self._discovered_models[lbl].id == preferred_model_id:
                        chosen_idx = i
                        break
            self.combo_models.current(chosen_idx)
            active_count = sum(1 for m in models if m.availability != ModelAvailability.INSTALLED_NOT_LOADED)
            status = f"Connected — {active_count} active, {len(models)} total discovered"
            if error:
                status += f" (API note: {error[:80]})"
            self.var_conn_status.set(status)
            self.monitor.append_activity(f"[MODELS] {status}", "success" if not error else "warning")
        else:
            self.var_model_display.set("")
            msg = f"Unreachable / no models: {error or 'empty list'}"
            self.var_conn_status.set(msg[:110])
            self.monitor.append_activity(f"[MODELS] {msg}", "warning")

    def send_live_request(self) -> None:
        ws = self._get_workspace_path_safe()
        if ws is None or not ws.is_dir():
            messagebox.showerror("Workspace Required", "Select a valid Workspace directory first.", parent=self.root)
            return

        prompt = self.live_prompt_text.get("1.0", "end-1c").strip()
        if not prompt:
            messagebox.showwarning("Empty Request", "Enter an instruction before sending.", parent=self.root)
            return

        phase_target = self.var_live_phase.get().strip() or "active"
        try:
            req = publish_live_request_file(workspace_dir=ws, prompt=prompt, phase=phase_target)
            self.live_prompt_text.delete("1.0", "end")
            self.monitor.append_activity(
                f"[LIVE REQUEST SENT] id={req.request_id} (phase={req.phase}) written to user_request.json",
                "warning",
            )
        except Exception as err:
            messagebox.showerror("Live Request Error", str(err), parent=self.root)

    def _bridge_manual_approval(
        self,
        phase: PhaseDefinition,
        suite: VerificationSuiteResult,
    ) -> Tuple[bool, str]:
        done = threading.Event()
        holder: Dict[str, Tuple[bool, str]] = {"res": (False, "Canceled")}

        def _open() -> None:
            dlg = ManualApprovalDialog(self.root, self.theme, phase, suite)
            self.root.wait_window(dlg)
            holder["res"] = dlg.result
            done.set()

        self.root.after(0, _open)
        done.wait()
        return holder["res"]

    def _bridge_final_review(
        self,
        report_dict: Dict[str, Any],
        report_md: str,
    ) -> Tuple[bool, bool]:
        done = threading.Event()
        holder: Dict[str, Tuple[bool, bool]] = {"res": (False, False)}

        def _open() -> None:
            dlg = FinalReviewDialog(self.root, self.theme, report_dict, report_md)
            self.root.wait_window(dlg)
            holder["res"] = dlg.result
            done.set()

        self.root.after(0, _open)
        done.wait()
        return holder["res"]

    def start_execution(self) -> None:
        if self._worker_thread and self._worker_thread.is_alive():
            return

        if not self.phase_editor.is_plan_locked:
            messagebox.showinfo(
                "Finish Phases First",
                "Please click 'Finish Phases Input' in the Phase Builder to lock your phase plan before starting.",
                parent=self.root,
            )
            return

        try:
            cfg = self._build_runtime_config(require_model=True)
        except Exception as err:
            messagebox.showerror("Configuration Error", str(err), parent=self.root)
            return

        phases = self.phase_editor.get_phases()
        temp_defs = [
            PhaseDefinition(
                id=i + 1,
                name=p["name"],
                prompt=p["prompt"],
                verification_commands=p.get("verification_commands", []),
            )
            for i, p in enumerate(phases)
        ]
        expected_plan_id = compute_plan_id(temp_defs)

        recovery = RecoveryManager(cfg)
        insp = recovery.inspect_workspace(expected_plan_id=expected_plan_id)
        resume_mode = False

        if insp.has_existing_session:
            dlg = RecoveryDialog(self.root, self.theme, insp)
            self.root.wait_window(dlg)
            if dlg.choice == "cancel":
                return
            if dlg.choice == "resume":
                resume_mode = True
            elif dlg.choice == "fresh":
                archived = recovery.archive_previous_session()
                self.monitor.append_activity(f"[RECOVERY] Archived previous session to {archived}", "warning")

        self._launch_orchestrator_thread(cfg, phases=None if resume_mode else phases, resume=resume_mode)

    def resume_saved_session(self) -> None:
        if self._worker_thread and self._worker_thread.is_alive():
            return

        try:
            cfg = self._build_runtime_config(require_model=True)
        except Exception as err:
            messagebox.showerror("Configuration Error", str(err), parent=self.root)
            return

        recovery = RecoveryManager(cfg)
        insp = recovery.inspect_workspace()
        if not insp.can_resume:
            messagebox.showinfo("No Resumable Session", insp.reason, parent=self.root)
            return

        saved_plan = read_json_safe(cfg.plan_file)
        if isinstance(saved_plan, dict) and isinstance(saved_plan.get("phases"), list):
            self.phase_editor.set_phases(saved_plan["phases"], locked=True)

        self._launch_orchestrator_thread(cfg, phases=None, resume=True)

    def _launch_orchestrator_thread(
        self,
        cfg: RuntimeConfig,
        phases: Optional[List[Dict[str, Any]]],
        resume: bool,
    ) -> None:
        self._persist_basic_prefs()
        self._set_running_ui_state(True)
        self.var_status.set("Status: Running...")

        self._orchestrator = ProjectOrchestrator(
            config=cfg,
            event_bus=self._event_bus,
            approval_callback=self._bridge_manual_approval,
            final_review_callback=self._bridge_final_review,
        )

        def _worker() -> None:
            try:
                assert self._orchestrator is not None
                self._orchestrator.run(raw_phases=phases, resume=resume)
            except Exception as exc:
                self._event_bus.emit(EventType.ERROR, f"Fatal error: {exc}", level="error")
            finally:
                self.root.after(0, lambda: self._set_running_ui_state(False))

        self._worker_thread = threading.Thread(target=_worker, name="LAPO-OrchestratorWorker", daemon=True)
        self._worker_thread.start()

    def pause_execution(self) -> None:
        if self._orchestrator and self._orchestrator.is_running:
            self._orchestrator.pause()
            self.btn_pause.configure(state="disabled")
            self.btn_resume_run.configure(state="normal")

    def resume_execution(self) -> None:
        if self._orchestrator and self._orchestrator.is_running:
            self._orchestrator.resume()
            self.btn_pause.configure(state="normal")
            self.btn_resume_run.configure(state="disabled")

    def stop_execution(self) -> None:
        if self._orchestrator and self._orchestrator.is_running:
            self._orchestrator.stop()
            self.btn_stop.configure(state="disabled")

    def _set_running_ui_state(self, running: bool) -> None:
        self.btn_start.configure(state="disabled" if running else "normal")
        self.btn_resume_session.configure(state="disabled" if running else "normal")
        self.btn_pause.configure(state="normal" if running else "disabled")
        self.btn_resume_run.configure(state="disabled")
        self.btn_stop.configure(state="normal" if running else "disabled")
        if not running:
            self.phase_editor.highlight_active_phase(None)
            self.lock_panel.refresh_from_disk()

    def _poll_events(self) -> None:
        events = self._event_bus.drain(max_items=250)
        if events:
            thinking_buf: List[str] = []
            output_buf: List[str] = []

            for evt in events:
                if evt.type == EventType.THINKING_DELTA:
                    thinking_buf.append(evt.message)
                elif evt.type == EventType.CONTENT_DELTA:
                    output_buf.append(evt.message)
                elif evt.type == EventType.CONTEXT_UPDATE:
                    pct = float(evt.data.get("trigger_progress_pct", 0.0))
                    self.monitor.update_context_display(evt.message, pct)
                    self.monitor.append_activity(evt.message, "info")
                elif evt.type == EventType.COMPACTION_STARTED:
                    self.var_status.set("Status: Compacting Context (Summarizing before Reset to 0)...")
                    self.monitor.append_activity(evt.message, "warning")
                elif evt.type == EventType.COMPACTION_COMPLETED:
                    pid = evt.data.get("phase_id", "?")
                    self.var_status.set(f"Status: Context Reset -> Continuing Phase {pid}...")
                    self.monitor.append_activity(evt.message, "success")
                elif evt.type == EventType.ERROR:
                    self.var_status.set("Status: STOPPED WITH ERROR (check Activity tab)")
                    self.monitor.append_activity(evt.message, "error")
                elif evt.type == EventType.STATUS:
                    self.var_status.set(f"Status: {evt.message}")
                    self.monitor.append_activity(f"[STATUS] {evt.message}", evt.level)
                elif evt.type == EventType.PHASE_CHANGED:
                    active_id = evt.data.get("active_phase_id")
                    total = evt.data.get("total_phases") or len(self.phase_editor.get_phases())
                    if evt.data.get("finished"):
                        self.var_phase_progress.set(f"Phase: {total}/{total} (Done)")
                        self.phase_editor.highlight_active_phase(None)
                    elif active_id:
                        self.var_phase_progress.set(f"Phase: {active_id}/{total}")
                        self.phase_editor.highlight_active_phase(int(active_id))
                    self.monitor.append_activity(evt.message, evt.level)
                elif evt.type == EventType.LOCK_UPDATED:
                    self.lock_panel.refresh_from_disk()
                    self.monitor.append_activity(evt.message, evt.level)
                elif evt.type == EventType.SESSION_COMPLETED:
                    self.var_status.set(f"Status: {evt.message}")
                    self.monitor.append_activity(f"[DONE] {evt.message}", evt.level)
                    self.lock_panel.refresh_from_disk()
                else:
                    self.monitor.append_activity(evt.message, evt.level)

            if thinking_buf:
                self.monitor.append_thinking("".join(thinking_buf))
            if output_buf:
                self.monitor.append_output("".join(output_buf))

        self.root.after(50, self._poll_events)

    def _on_close(self) -> None:
        self._persist_basic_prefs()
        if self._orchestrator and self._orchestrator.is_running:
            if not messagebox.askyesno(
                "Orchestrator Running",
                "An execution session is active. Stop, save state, and exit?",
                parent=self.root,
            ):
                return
            self._orchestrator.stop()
            if self._worker_thread and self._worker_thread.is_alive():
                self._worker_thread.join(timeout=3.0)
        self.root.destroy()


def launch_gui() -> int:
    root = tk.Tk()
    LapoMainWindow(root)
    root.mainloop()
    return 0