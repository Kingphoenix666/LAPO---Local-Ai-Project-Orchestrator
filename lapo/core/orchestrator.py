"""
Shared autonomous orchestration engine used identically by Desktop GUI and Terminal CLI.
Coordinates multi-phase execution, tool execution, `<file>` auto-writing, KV-cache-preserving
compaction, live user requests, unattended self-verification, file lock repair, crash recovery, and final reports.
"""

from __future__ import annotations

import copy
import json
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from lapo.adapters.strata_adapter import (
    ENGINE_TERMINATED,
    EngineTerminatedError,
    StrataAdapter,
)
from lapo.config import RuntimeConfig, format_token_count
from lapo.core.compaction_manager import (
    CompactionManager,
    build_in_stream_compaction_signal,
    build_workspace_ast_map,
)
from lapo.core.context_manager import ContextManager
from lapo.core.file_lock_manager import FileLockManager
from lapo.core.live_request_manager import LiveRequestManager
from lapo.core.phase_manager import PhaseManager
from lapo.core.recovery_manager import RecoveryManager
from lapo.core.usage_ledger import UsageLedger
from lapo.core.verification_manager import VerificationManager
from lapo.events import EventBus, EventType
from lapo.models import PhaseDefinition, PhaseStatus, VerificationSuiteResult, utc_now_iso
from lapo.tools.inline_file_writer import extract_text_tool_calls
from lapo.tools.workspace_tools import TOOLS_SCHEMA, WorkspaceTools


ManualApprovalCallback = Callable[[PhaseDefinition, VerificationSuiteResult], Tuple[bool, str]]
FinalReviewCallback = Callable[[Dict[str, Any], str], Tuple[bool, bool]]


class ProjectOrchestrator:
    """Single source of truth for LAPO project execution across GUI and CLI."""

    def __init__(
        self,
        config: RuntimeConfig,
        event_bus: Optional[EventBus] = None,
        approval_callback: Optional[ManualApprovalCallback] = None,
        final_review_callback: Optional[FinalReviewCallback] = None,
    ) -> None:
        self.config = config
        self.events = event_bus or EventBus()
        self.approval_callback = approval_callback
        self.final_review_callback = final_review_callback

        self.config.ensure_orchestrator_dirs()

        self.adapter = StrataAdapter(config, event_bus=self.events)
        self.phase_manager = PhaseManager(config)
        self.lock_manager = FileLockManager(config, event_bus=self.events)
        self.verification_manager = VerificationManager(config, event_bus=self.events)
        self.live_request_manager = LiveRequestManager(config, event_bus=self.events)
        self.recovery_manager = RecoveryManager(config)
        self.usage_ledger = UsageLedger(config)
        self.context_manager = ContextManager(config, self.adapter)
        self.compaction_manager = CompactionManager(
            phase_manager=self.phase_manager,
            lock_manager=self.lock_manager,
            verification_manager=self.verification_manager,
            live_request_manager=self.live_request_manager,
            recovery_manager=self.recovery_manager,
            usage_ledger=self.usage_ledger,
            event_bus=self.events,
        )

        self.tools = WorkspaceTools(
            config=config,
            lock_manager=self.lock_manager,
            verification_manager=self.verification_manager,
            event_bus=self.events,
            get_active_phase_id=lambda: self.phase_manager.get_active_phase().id,
            get_active_verification_commands=lambda: list(self.phase_manager.get_active_phase().verification_commands),
            on_file_modified=self.phase_manager.record_modified_file,
            on_complete_phase=self._handle_complete_phase_gate,
        )

        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()
        self._running = False

        self._phase_transitioned_this_turn = False
        self._project_finished = False
        self._messages: List[Dict[str, Any]] = []
        self._system_prompt: str = ""

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_paused(self) -> bool:
        return not self._pause_event.is_set()

    def pause(self) -> None:
        self._pause_event.clear()
        self.events.emit(EventType.STATUS, "Paused by user.", level="warning")

    def resume(self) -> None:
        self._pause_event.set()
        self.events.emit(EventType.STATUS, "Resumed execution.", level="info")

    def stop(self) -> None:
        self._stop_event.set()
        self._pause_event.set()
        self.events.emit(EventType.STATUS, "Stopping and saving state...", level="warning")

    def _wait_if_paused(self) -> bool:
        while not self._pause_event.is_set():
            if self._stop_event.is_set():
                return False
            time.sleep(0.15)
        return not self._stop_event.is_set()

    # ------------------------------------------------------------------
    # Authoritative Phase Completion Gate
    # ------------------------------------------------------------------
    def _handle_complete_phase_gate(self, args: Dict[str, Any]) -> Dict[str, Any]:
        active = self.phase_manager.get_active_phase()
        summary = str(args.get("summary") or "").strip()
        files_to_lock = [str(f).strip() for f in (args.get("files_to_lock") or []) if str(f).strip()]
        raw_deps = args.get("file_dependencies") or {}
        resolved_ids = [str(rid).strip() for rid in (args.get("resolved_request_ids") or []) if str(rid).strip()]

        # 1. Check live user requests targeting this phase BEFORE allowing closure
        accepted_ids, remaining_reqs = self.live_request_manager.acknowledge_requests(active.id, resolved_ids)
        if remaining_reqs:
            unaddressed = [{"request_id": r.request_id, "prompt": r.prompt} for r in remaining_reqs]
            self.events.emit(
                EventType.LOG,
                f"[GATE REJECTED Phase {active.id}] {len(remaining_reqs)} unresolved live user request(s): "
                f"{[r.request_id for r in remaining_reqs]}",
                level="warning",
            )
            return {
                "accepted": False,
                "reason": "UNRESOLVED_LIVE_USER_REQUESTS",
                "message": (
                    "Phase cannot close while live user requests for this phase remain unresolved. "
                    "Address each request below, verify your changes, and call `complete_phase` again "
                    "with their exact `request_id`s in `resolved_request_ids`."
                ),
                "unresolved_requests": unaddressed,
                "acknowledged_ids": accepted_ids,
            }

        # 2. Check previous-phase repair obligations
        repair_obligations = self.lock_manager.list_repair_queue()
        for obligation in repair_obligations:
            owner_phase = self.phase_manager.get_phase_by_id(obligation.owner_phase_id)
            owner_cmds = list(owner_phase.verification_commands) if owner_phase else []

            self.events.emit(
                EventType.LOG,
                f"[REPAIR GATE] Verifying repaired file '{obligation.path}' against Phase {obligation.owner_phase_id}...",
            )
            repair_suite = self.verification_manager.run_phase_checks(
                obligation.owner_phase_id,
                owner_cmds,
                modified_files=[obligation.path],
            )
            self.phase_manager.record_verification(obligation.owner_phase_id, repair_suite.to_dict())

            if repair_suite.status == "failed":
                self.events.emit(
                    EventType.LOG,
                    f"[GATE REJECTED Phase {active.id}] Repair verification failed for '{obligation.path}'.",
                    level="error",
                )
                return {
                    "accepted": False,
                    "reason": "REPAIR_VERIFICATION_FAILED",
                    "message": (
                        f"Repaired file '{obligation.path}' failed owning Phase {obligation.owner_phase_id} "
                        "verification checks. Fix the issue and try again."
                    ),
                    "verification": repair_suite.to_dict(),
                }

            if repair_suite.status == "manual_required" and self.approval_callback and owner_phase:
                approved, note = self.approval_callback(owner_phase, repair_suite)
                if not approved:
                    return {
                        "accepted": False,
                        "reason": "REPAIR_MANUAL_APPROVAL_REJECTED",
                        "message": f"User rejected repair of '{obligation.path}': {note}",
                    }
                self.lock_manager.relock_repaired_file(obligation.path, verification_status="manual_approved")
            else:
                self.lock_manager.relock_repaired_file(obligation.path, verification_status="passed")

        # 3. Run active phase verification suite (custom commands OR unattended smart auto-verifier)
        suite = self.verification_manager.run_phase_checks(
            active.id,
            active.verification_commands,
            modified_files=active.created_or_modified_files,
        )
        if suite.status == "failed":
            self.phase_manager.record_verification(active.id, suite.to_dict())
            failed_details = [
                f"{r.command} (exit {r.exit_code}): {(r.stderr or r.stdout).strip()[:160]}"
                for r in suite.command_results
                if not r.passed
            ]
            self.events.emit(
                EventType.LOG,
                f"[GATE REJECTED Phase {active.id}] Verification failed: {'; '.join(failed_details)}",
                level="error",
            )
            return {
                "accepted": False,
                "reason": "VERIFICATION_FAILED",
                "message": "Phase verification failed. Fix the reported errors before calling `complete_phase` again.",
                "verification": suite.to_dict(),
            }

        verification_label = "passed" if active.verification_commands else "auto_verified"
        if suite.status == "manual_required":
            self.phase_manager.set_phase_status(active.id, PhaseStatus.AWAITING_APPROVAL)
            if self.approval_callback is not None:
                self.events.emit(
                    EventType.STATUS,
                    f"Phase {active.id} awaiting manual user approval...",
                    level="warning",
                )
                approved, note = self.approval_callback(active, suite)
                if not approved:
                    self.phase_manager.set_phase_status(active.id, PhaseStatus.ACTIVE)
                    return {
                        "accepted": False,
                        "reason": "MANUAL_APPROVAL_REJECTED",
                        "message": f"User did not approve Phase {active.id} yet. Feedback: {note or 'Revise outputs.'}",
                    }
                suite.manual_approved = True
                suite.status = "manual_approved"
                verification_label = "manual_approved"
            else:
                self.usage_ledger.record_unverified_item(
                    f"Phase {active.id} ({active.name}) had no automated verification commands and no interactive approver."
                )
                verification_label = "unverified_no_commands"

        self.phase_manager.record_verification(active.id, suite.to_dict())

        # 4. Commit file locks atomically
        explicit_lock_list = list(set(files_to_lock) | set(active.created_or_modified_files))
        dep_map: Dict[str, List[str]] = {}
        if isinstance(raw_deps, dict):
            for k, v in raw_deps.items():
                if isinstance(v, list):
                    dep_map[str(k)] = [str(x) for x in v]

        newly_locked = self.lock_manager.commit_phase_locks(
            phase_id=active.id,
            explicit_files=explicit_lock_list,
            file_dependencies=dep_map,
            verification_status=verification_label,
        )

        # 5. Advance phase manifest
        finished, next_phase = self.phase_manager.advance_phase(
            summary=summary or f"Completed Phase {active.id}",
            locked_files=newly_locked,
            declared_dependencies=dep_map,
        )
        self.lock_manager.reset_session_reads()
        self._phase_transitioned_this_turn = True
        self._project_finished = finished

        if finished:
            self.events.emit(
                EventType.PHASE_CHANGED,
                f"Phase {active.id} accepted ({verification_label}). All phases completed!",
                data={"completed_phase_id": active.id, "finished": True},
                level="success",
            )
            return {
                "accepted": True,
                "project_finished": True,
                "locked_files": newly_locked,
                "verification_status": verification_label,
                "message": "All phases completed and accepted by Orchestrator.",
            }

        assert next_phase is not None
        self.events.emit(
            EventType.PHASE_CHANGED,
            f"Phase {active.id} accepted ({verification_label}) -> Transitioned to Phase {next_phase.id}: {next_phase.name}",
            data={
                "completed_phase_id": active.id,
                "active_phase_id": next_phase.id,
                "active_phase_name": next_phase.name,
                "finished": False,
            },
            level="success",
        )
        return {
            "accepted": True,
            "project_finished": False,
            "locked_files": newly_locked,
            "verification_status": verification_label,
            "next_phase_id": next_phase.id,
            "next_phase_name": next_phase.name,
            "message": f"Phase {active.id} completed. Orchestrator is now handing off Phase {next_phase.id}.",
        }

    # ------------------------------------------------------------------
    # Message Preparation & Recovery Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _build_truncated_json_recovery_message(
        fn_name: str,
        err: Exception,
        raw_len: int,
        streak: int,
    ) -> str:
        prefix = ""
        if streak >= 2:
            prefix = (
                f"CRITICAL ANTI-LOOP WARNING (consecutive truncated JSON #{streak}): "
                f"STOP sending oversized JSON payloads to `{fn_name}`. "
            )

        if fn_name == "patch_file":
            return (
                f"{prefix}Truncated JSON arguments in `patch_file` (received {raw_len} chars; {err}). "
                "Keep `target_snippet` and `replacement_snippet` short and focused (5 to 15 lines max per call), "
                'and split multiple edits across small patch calls. If you need to rewrite a large file, emit '
                '<file path="relative/path.ext">...complete file...</file> directly in plain assistant text instead.'
            )

        if fn_name == "write_file":
            return (
                f"{prefix}Truncated JSON arguments in `write_file` (received {raw_len} chars; {err}). "
                'Do NOT stuff large source files into JSON tool arguments. Output the complete file directly '
                'in your plain assistant text using <file path="relative/path.ext">...</file>.'
            )

        return (
            f"{prefix}Truncated JSON arguments in `{fn_name}` (received {raw_len} chars; {err}). "
            'Keep JSON tool arguments concise and valid, or use <file path="relative/path.ext">...</file> '
            "in plain assistant text for file writes."
        )

    def _build_fresh_phase_messages(
        self,
        continuation_report: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        active = self.phase_manager.get_active_phase()
        machine_state = self.verification_manager.build_machine_verified_snapshot()
        if getattr(self.config, "inject_ast_map_on_compaction", True):
            locked_paths = {d["path"] for d in self.lock_manager.list_locks_dicts() if d.get("path")}
            ast_map = build_workspace_ast_map(self.config.workspace_dir, locked_paths=locked_paths)
            if ast_map:
                machine_state["python_ast_map"] = ast_map
        pending_dicts = [
            r.to_dict() for r in self.live_request_manager.pop_undelivered_for_phase(active.id)
        ]

        handoff = self.phase_manager.build_phase_handoff_message(
            machine_state=machine_state,
            locked_files=self.lock_manager.list_locks_dicts(),
            pending_requests=pending_dicts,
            repair_queue=self.lock_manager.list_repair_queue_dicts(),
            continuation_report=continuation_report,
        )
        msgs = [
            {"role": "system", "content": self._system_prompt},
            {"role": "user", "content": handoff},
        ]

        plan = self.phase_manager.plan
        assert plan is not None
        self.recovery_manager.save_state(
            {
                "timestamp": utc_now_iso(),
                "plan_id": plan.plan_id,
                "active_phase_id": active.id,
                "active_phase_name": active.name,
                "machine_verified": machine_state,
                "continuation_report": continuation_report or {},
            }
        )
        self.recovery_manager.save_journal(msgs)
        self.context_manager.reset_tracking()
        return msgs

    @staticmethod
    def _sanitize_assistant_message_with_metrics(
        raw_msg: Dict[str, Any],
        strip_reasoning: bool = True,
        compact_file_writes: bool = True,
        inline_written: Optional[List[str]] = None,
        is_truncated_empty: bool = False,
    ) -> Tuple[Dict[str, Any], int]:
        """
        Append-only single-pass history hygiene applied ONCE before inserting into history.
        Preserves earlier turns byte-identically for Strata 0.1.67 Conversation Cache reuse.
        """
        history_msg = copy.deepcopy(raw_msg)
        chars_stripped = 0

        if strip_reasoning and history_msg.get("reasoning_content"):
            chars_stripped += len(str(history_msg.get("reasoning_content") or ""))
            history_msg.pop("reasoning_content", None)

        if history_msg.get("tool_calls"):
            for tc in history_msg["tool_calls"]:
                fn = tc.get("function") or {}
                fn_name = str(fn.get("name") or "")
                raw_args = str(fn.get("arguments") or "{}")
                try:
                    parsed_args = json.loads(raw_args, strict=False)
                except Exception:
                    replacement = json.dumps({"_truncated_json_recovered": True})
                    chars_stripped += max(0, len(raw_args) - len(replacement))
                    fn["arguments"] = replacement
                    continue

                if (
                    compact_file_writes
                    and fn_name == "write_file"
                    and isinstance(parsed_args, dict)
                    and isinstance(parsed_args.get("content"), str)
                    and len(parsed_args["content"]) > 1200
                ):
                    c_body = parsed_args["content"]
                    path_lbl = str(parsed_args.get("path") or "unknown")
                    receipt = (
                        f"[compacted from history: {len(c_body)} chars, "
                        f"{c_body.count(chr(10)) + 1} lines for {path_lbl}; see tool result]"
                    )
                    compact_args = json.dumps({"path": path_lbl, "content": receipt}, ensure_ascii=False)
                    chars_stripped += max(0, len(raw_args) - len(compact_args))
                    fn["arguments"] = compact_args

        content_val = history_msg.get("content")
        if isinstance(content_val, str) and content_val:
            if compact_file_writes and inline_written:
                def _compact_inline(match: re.Match) -> str:
                    nonlocal chars_stripped
                    fpath = match.group(1)
                    body = match.group(2)
                    if len(body) <= 1200:
                        return match.group(0)
                    rep = (
                        f'<file path="{fpath}">[compacted from history: {len(body)} chars, '
                        f'{body.count(chr(10)) + 1} lines written to disk]</file>'
                    )
                    chars_stripped += max(0, len(match.group(0)) - len(rep))
                    return rep
                content_val = re.sub(
                    r'<file\s+path=["\']([^"\']+)["\']\s*>(.*?)</file>',
                    _compact_inline,
                    content_val,
                    flags=re.DOTALL,
                )
                history_msg["content"] = content_val

            if is_truncated_empty and len(content_val) > 1200:
                short_notice = (
                    f"[truncated response omitted from history ({len(content_val)} chars; finish_reason=length). "
                    "Emit concise tool calls or one file per turn.]"
                )
                chars_stripped += max(0, len(content_val) - len(short_notice))
                history_msg["content"] = short_notice

        return history_msg, chars_stripped

    @staticmethod
    def _sanitize_assistant_message_for_history(raw_msg: Dict[str, Any]) -> Dict[str, Any]:
        msg, _ = ProjectOrchestrator._sanitize_assistant_message_with_metrics(
            raw_msg, strip_reasoning=True, compact_file_writes=True
        )
        return msg

    def _inject_live_requests_if_any(self, messages: List[Dict[str, Any]]) -> None:
        active = self.phase_manager.get_active_phase()
        incoming = self.live_request_manager.pop_undelivered_for_phase(active.id)
        if not incoming:
            return
        lines = [
            "========================================================================",
            f"[LIVE USER REQUEST(S) FOR ACTIVE PHASE {active.id}]",
            "Address these instructions before calling `complete_phase`, and include each "
            "`request_id` in `resolved_request_ids`:",
        ]
        for req in incoming:
            lines.append(f"- request_id: `{req.request_id}` | prompt: {req.prompt}")
        lines.append("========================================================================")
        messages.append({"role": "user", "content": "\n".join(lines)})
        self.recovery_manager.save_journal(messages)

    def _arm_in_stream_compaction(self, active_tokens: int, origin: str) -> None:
        active_phase = self.phase_manager.get_active_phase()
        trigger = self.context_manager.compaction_trigger
        pct = (active_tokens / float(max(1, trigger))) * 100.0
        signal = build_in_stream_compaction_signal(active_phase, active_tokens, trigger)

        self.tools.compaction_lock = True
        self.tools.compaction_signal_text = signal

        if self._messages and self._messages[-1].get("role") in ("tool", "user"):
            self._messages[-1]["content"] = (self._messages[-1].get("content") or "") + signal
        else:
            self._messages.append({"role": "user", "content": signal})

        self.events.emit(
            EventType.COMPACTION_STARTED,
            (
                "====================================================================\n"
                f"[ORCHESTRATOR] >>> IN-STREAM COMPACTION TRIGGERED ({origin})!\n"
                f"Active Context: {format_token_count(active_tokens)} >= Trigger "
                f"{format_token_count(trigger)} ({pct:.1f}%).\n"
                f"Tools LOCKED. Requesting continuation summary JSON from model before resetting context to 0 "
                f"(staying in Phase {active_phase.id}: {active_phase.name})...\n"
                "===================================================================="
            ),
            data={
                "active_tokens": active_tokens,
                "trigger_tokens": trigger,
                "phase_id": active_phase.id,
            },
            level="warning",
        )

    # ------------------------------------------------------------------
    # Main Autonomous Execution Entrypoint
    # ------------------------------------------------------------------
    def run(
        self,
        raw_phases: Optional[List[Dict[str, Any]]] = None,
        resume: bool = False,
        max_turns: int = 500,
    ) -> Dict[str, Any]:
        self._stop_event.clear()
        self._pause_event.set()
        self._running = True
        self._project_finished = False

        try:
            if resume:
                plan = self.phase_manager.load()
                if plan is None:
                    raise RuntimeError("Cannot resume: no saved phase_execution_plan.json found.")
                if plan.is_finished:
                    raise RuntimeError("Saved session is already finished.")
                integrity_issues = self.lock_manager.verify_integrity()
                if integrity_issues:
                    self.events.emit(
                        EventType.LOG,
                        f"[RECOVERY WARNING] Locked file integrity issues detected: {integrity_issues}",
                        level="warning",
                    )
            else:
                if not raw_phases:
                    raise ValueError("Cannot start a new run without phase definitions.")
                plan = self.phase_manager.create_plan(raw_phases)

            self.events.emit(
                EventType.LOG,
                f"[NETWORK] API route: {self.adapter.api_route_summary} | "
                f"Subprocess proxies: primary={self.config.primary_proxy or 'none'}, "
                f"fallback={self.config.fallback_proxy or 'none'}",
                level="info",
            )
            if self.config.auto_install_deps:
                self.verification_manager.bootstrap_workspace_dependencies()

            inspection = self.adapter.inspect_server()
            if inspection.engine_state == ENGINE_TERMINATED:
                raise EngineTerminatedError(f"Inference engine is {ENGINE_TERMINATED}: {inspection.error}")
            if not inspection.reachable:
                raise RuntimeError(f"Inference server unreachable at {self.config.api_base_url}: {inspection.error}")

            if not self.config.model_name and inspection.active_model_ids:
                self.config.model_name = inspection.active_model_ids[0]
            if not self.config.model_name:
                raise RuntimeError("No target model selected and server returned no active models.")

            self.context_manager.update_detected_capacity(inspection.detected_context_tokens)
            strata_metrics_start = self.adapter.fetch_strata_metrics()
            self.usage_ledger.mark_started(
                detected_ctx=inspection.detected_context_tokens,
                engine_version=inspection.engine_version,
                strata_metrics_snapshot=strata_metrics_start,
            )
            self.events.emit(
                EventType.LOG,
                f"[ENGINE] Version: {inspection.engine_version or 'unknown'} | "
                f"State: {inspection.engine_state} | Detected Context: {inspection.detected_context_tokens}",
                level="info",
            )

            self._system_prompt = self.phase_manager.build_system_prompt(self.config.tool_protocol)

            saved_journal = self.recovery_manager.load_journal() if resume else None
            if saved_journal is not None:
                self._messages = saved_journal
                active = self.phase_manager.get_active_phase()
                self.lock_manager.reset_session_reads()
                self.live_request_manager.reset_delivery_flags_for_phase(active.id)
                handoff = self.phase_manager.build_phase_handoff_message(
                    machine_state=self.verification_manager.build_machine_verified_snapshot(),
                    locked_files=self.lock_manager.list_locks_dicts(),
                    pending_requests=[
                        r.to_dict() for r in self.live_request_manager.pop_undelivered_for_phase(active.id)
                    ],
                    repair_queue=self.lock_manager.list_repair_queue_dicts(),
                )
                self._messages.append({"role": "user", "content": f"[SESSION RESUMED]\n{handoff}"})
                self.recovery_manager.save_journal(self._messages)
                self.events.emit(
                    EventType.LOG,
                    f"[ORCHESTRATOR] Resumed session on Phase {active.id}: {active.name} ({len(self._messages)} messages).",
                    level="success",
                )
            else:
                self._messages = self._build_fresh_phase_messages()

            active = self.phase_manager.get_active_phase()
            self.events.emit(
                EventType.PHASE_CHANGED,
                f"Active Phase {active.id}/{len(plan.phases)}: {active.name}",
                data={"active_phase_id": active.id, "active_phase_name": active.name, "total_phases": len(plan.phases)},
                level="info",
            )

            self.live_request_manager.start_watcher()

            awaiting_compaction = False
            compaction_prev_tokens = 0
            consecutive_json_errors = 0
            consecutive_watchdog_hits = 0
            turns = 0

            while not self._stop_event.is_set() and not self._project_finished and turns < max_turns:
                if not self._wait_if_paused():
                    break

                turns += 1
                self._phase_transitioned_this_turn = False

                if not awaiting_compaction:
                    self._inject_live_requests_if_any(self._messages)

                est_tokens, is_exact = self.context_manager.count_messages_tokens(
                    self._messages,
                    tools_schema=TOOLS_SCHEMA,
                )
                active_tokens = max(est_tokens, self.context_manager.last_engine_tokens)
                snap = self.context_manager.snapshot(active_tokens, is_exact)
                self.events.emit(
                    EventType.CONTEXT_UPDATE,
                    snap.summary_line(),
                    data=snap.to_dict(),
                )

                if not awaiting_compaction and self.context_manager.needs_preflight_compaction(active_tokens):
                    self._arm_in_stream_compaction(active_tokens, origin="PRE-FLIGHT")
                    awaiting_compaction = True
                    compaction_prev_tokens = active_tokens

                if awaiting_compaction:
                    self.events.emit(
                        EventType.LOG,
                        "[ORCHESTRATOR] >>> Waiting for model to emit compaction JSON summary (context reset pending)...",
                        level="warning",
                    )

                max_gen = self.context_manager.compute_dynamic_max_tokens(
                    active_tokens, for_compaction=awaiting_compaction
                )

                result = self.adapter.chat_completion(
                    messages=self._messages,
                    max_tokens=max_gen,
                    tools_schema=TOOLS_SCHEMA,
                    stream=True,
                    stop_event=self._stop_event,
                    allow_thought_watchdog=not awaiting_compaction,
                )

                if result.was_stopped:
                    self.events.emit(EventType.STATUS, "Execution stopped cleanly by user.", level="warning")
                    break

                raw_msg = result.message
                reasoning_str = raw_msg.get("reasoning_content") or ""
                content_str = raw_msg.get("content") or ""

                self.usage_ledger.record_turn(
                    usage=result.usage,
                    timings=result.timings,
                    reasoning_text=reasoning_str,
                    content_text=content_str,
                    retries_used=result.retries_used,
                )

                post_tokens = self.context_manager.extract_post_flight_tokens(result, fallback_estimate=est_tokens)
                post_snap = self.context_manager.snapshot(post_tokens, is_exact=True)
                self.events.emit(
                    EventType.CONTEXT_UPDATE,
                    f"[POST-FLIGHT] {post_snap.summary_line()} | Finish: {result.finish_reason}",
                    data=post_snap.to_dict(),
                )

                if awaiting_compaction:
                    raw_summary = content_str or reasoning_str
                    self._messages, _, _ = self.compaction_manager.finalize_compaction(
                        raw_model_output=raw_summary,
                        tools=self.tools,
                        system_prompt=self._system_prompt,
                        previous_tokens=max(compaction_prev_tokens, post_tokens),
                    )
                    awaiting_compaction = False
                    compaction_prev_tokens = 0
                    consecutive_json_errors = 0
                    self.context_manager.reset_tracking()

                    fresh_tok, fresh_exact = self.context_manager.count_messages_tokens(
                        self._messages, tools_schema=TOOLS_SCHEMA
                    )
                    fresh_snap = self.context_manager.snapshot(fresh_tok, fresh_exact)
                    self.events.emit(
                        EventType.CONTEXT_UPDATE,
                        f"[POST-RESET] {fresh_snap.summary_line()}",
                        data=fresh_snap.to_dict(),
                    )
                    continue

                if result.watchdog_triggered:
                    consecutive_watchdog_hits += 1
                    self.usage_ledger.record_watchdog_trigger()
                    stripped_r = len(reasoning_str)
                    if stripped_r > 0:
                        self.usage_ledger.record_history_compaction_savings(stripped_r)
                    self.context_manager.last_engine_tokens = est_tokens
                    self._messages.append(
                        {
                            "role": "assistant",
                            "content": "[Thought watchdog interrupted overlong reasoning before any output or tool call.]",
                        }
                    )
                    urgency = f"CRITICAL (watchdog #{consecutive_watchdog_hits}): " if consecutive_watchdog_hits >= 2 else ""
                    self._messages.append(
                        {
                            "role": "user",
                            "content": (
                                f"[ORCHESTRATOR WATCHDOG] {urgency}Your previous turn spent {stripped_r:,} chars "
                                "in reasoning without producing output or a tool call. Do NOT draft full code "
                                "inside <think>. Keep reasoning to a brief 5-line outline and immediately emit "
                                'the file via <file path="relative/path.ext">...</file> or call a tool now.'
                            ),
                        }
                    )
                    self.recovery_manager.save_journal(self._messages)
                    continue
                consecutive_watchdog_hits = 0

                inline_written, inline_errors = self.tools.extract_and_write_inline_files(content_str)
                if inline_written:
                    self.usage_ledger.record_inline_files(len(inline_written))
                    consecutive_json_errors = 0

                native_tool_calls = raw_msg.get("tool_calls") or []
                text_tool_calls = []
                if not native_tool_calls and self.config.tool_protocol in ("text", "auto"):
                    text_tool_calls = extract_text_tool_calls(content_str)

                is_truncated_empty = (
                    result.finish_reason == "length"
                    and not inline_written
                    and not native_tool_calls
                    and not text_tool_calls
                )
                history_msg, chars_stripped = self._sanitize_assistant_message_with_metrics(
                    raw_msg,
                    strip_reasoning=getattr(self.config, "strip_history_reasoning", True),
                    compact_file_writes=getattr(self.config, "compact_history_file_writes", True),
                    inline_written=inline_written,
                    is_truncated_empty=is_truncated_empty,
                )
                if chars_stripped > 0:
                    self.usage_ledger.record_history_compaction_savings(chars_stripped)
                self._messages.append(history_msg)
                self.recovery_manager.save_journal(self._messages)

                wrote_or_tested = len(inline_written) > 0

                if native_tool_calls:
                    self.usage_ledger.record_tool_calls(len(native_tool_calls))
                    for tc in native_tool_calls:
                        fn_name = (tc.get("function") or {}).get("name") or ""
                        raw_args = (tc.get("function") or {}).get("arguments") or "{}"

                        if fn_name in ("write_file", "patch_file", "run_checks", "run_terminal", "complete_phase"):
                            wrote_or_tested = True

                        try:
                            fn_args = json.loads(raw_args, strict=False)
                            consecutive_json_errors = 0
                            self.events.emit(
                                EventType.TOOL_CALL,
                                f"[TOOL] {fn_name}({json.dumps(fn_args, ensure_ascii=False)[:140]})",
                                data={"name": fn_name, "arguments": fn_args},
                            )
                            tool_output = self.tools.execute(fn_name, fn_args)
                        except json.JSONDecodeError as je:
                            consecutive_json_errors += 1
                            raw_len = len(raw_args)
                            recovery_msg = self._build_truncated_json_recovery_message(
                                fn_name, je, raw_len, consecutive_json_errors
                            )
                            self.events.emit(
                                EventType.LOG,
                                f"[TOOL RECOVERY] Truncated JSON in `{fn_name}` "
                                f"(received {raw_len} chars; streak={consecutive_json_errors}; {je}).",
                                level="warning",
                            )
                            tool_output = json.dumps({"error": recovery_msg}, ensure_ascii=False)

                        self.events.emit(
                            EventType.TOOL_RESULT,
                            f"[TOOL RESULT] {fn_name}: {tool_output[:240]}",
                            data={"name": fn_name, "output_preview": tool_output[:500]},
                        )
                        self._messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc.get("id") or f"call_{int(time.time())}",
                                "name": fn_name,
                                "content": tool_output,
                            }
                        )

                        if self._phase_transitioned_this_turn:
                            break

                    if self._project_finished:
                        break

                    if self._phase_transitioned_this_turn:
                        self._messages = self._build_fresh_phase_messages()
                        continue

                    if inline_written or inline_errors:
                        feedback_lines = []
                        if inline_written:
                            feedback_lines.append(f"[AUTO-WRITER] Wrote inline files: {inline_written}")
                        if inline_errors:
                            feedback_lines.append(f"[AUTO-WRITER ERRORS] {json.dumps(inline_errors, ensure_ascii=False)}")
                        self._messages.append({"role": "user", "content": "\n".join(feedback_lines)})

                elif text_tool_calls:
                    consecutive_json_errors = 0
                    self.usage_ledger.record_tool_calls(len(text_tool_calls))
                    results_summary: List[str] = []
                    for tc in text_tool_calls:
                        if tc.name in ("write_file", "patch_file", "run_checks", "run_terminal", "complete_phase"):
                            wrote_or_tested = True
                        self.events.emit(
                            EventType.TOOL_CALL,
                            f"[TEXT-TOOL] {tc.name}({json.dumps(tc.arguments, ensure_ascii=False)[:140]})",
                            data={"name": tc.name, "arguments": tc.arguments},
                        )
                        out = self.tools.execute(tc.name, tc.arguments)
                        results_summary.append(f'<tool_result name="{tc.name}">\n{out}\n</tool_result>')
                        if self._phase_transitioned_this_turn:
                            break

                    if self._project_finished:
                        break
                    if self._phase_transitioned_this_turn:
                        self._messages = self._build_fresh_phase_messages()
                        continue

                    self._messages.append({"role": "user", "content": "\n\n".join(results_summary)})

                else:
                    if inline_written or inline_errors:
                        active_phase = self.phase_manager.get_active_phase()
                        parts = []
                        if inline_written:
                            parts.append(f"[AUTO-WRITER] Successfully wrote {inline_written} atomically to disk.")
                        if inline_errors:
                            parts.append(f"[AUTO-WRITER BLOCKED] {json.dumps(inline_errors, ensure_ascii=False)}")
                        if inline_written:
                            suite = self.verification_manager.run_phase_checks(
                                active_phase.id,
                                active_phase.verification_commands,
                                modified_files=active_phase.created_or_modified_files,
                            )
                            parts.append(
                                f"[AUTO-CHECKS Phase {active_phase.id}] status={suite.status}\n"
                                f"{json.dumps(suite.to_dict(), indent=2, ensure_ascii=False)}"
                            )
                        parts.append(
                            f"Continue Phase {active_phase.id} ('{active_phase.name}') or call `complete_phase` when finished."
                        )
                        self._messages.append({"role": "user", "content": "\n\n".join(parts)})
                    else:
                        active_phase = self.phase_manager.get_active_phase()
                        self._messages.append(
                            {
                                "role": "user",
                                "content": (
                                    f"Continue working on active Phase {active_phase.id} ('{active_phase.name}'). "
                                    'Use workspace tools or `<file path="...">...</file>` blocks, and call '
                                    "`complete_phase` once this phase is complete and verified."
                                ),
                            }
                        )

                effective_post_tokens = post_tokens
                if chars_stripped > 0:
                    turn_comp_tok = int(
                        (result.usage or {}).get("completion_tokens")
                        or (result.timings or {}).get("predicted_n")
                        or 0
                    )
                    est_saved = int(chars_stripped / 3.5)
                    if turn_comp_tok > 0:
                        est_saved = min(est_saved, max(0, turn_comp_tok - 64))
                    effective_post_tokens = max(est_tokens, post_tokens - est_saved)
                    self.context_manager.last_engine_tokens = effective_post_tokens

                if not awaiting_compaction and self.context_manager.should_trigger_compaction(
                    effective_post_tokens, wrote_or_tested=wrote_or_tested
                ):
                    self._arm_in_stream_compaction(effective_post_tokens, origin="POST-FLIGHT")
                    awaiting_compaction = True
                    compaction_prev_tokens = effective_post_tokens

                self.recovery_manager.save_journal(self._messages)

            if not self._project_finished and turns >= max_turns:
                self.events.emit(
                    EventType.LOG,
                    f"[ORCHESTRATOR] Reached max_turns limit ({max_turns}). Saving state cleanly for resume.",
                    level="warning",
                )

            self.usage_ledger.record_strata_metrics_end(self.adapter.fetch_strata_metrics())
            assert self.phase_manager.plan is not None
            json_path, md_path, report_obj, md_text = self.usage_ledger.generate_final_reports(
                plan=self.phase_manager.plan,
                locked_files=self.lock_manager.list_locks_dicts(),
                live_requests=self.live_request_manager.list_all_dicts(),
            )

            unload_result: Optional[Dict[str, Any]] = None
            if self._project_finished and self.final_review_callback is not None:
                approved, should_unload = self.final_review_callback(report_obj, md_text)
                if approved and should_unload:
                    ok, msg = self.adapter.unload_model()
                    unload_result = {"confirmed": ok, "message": msg}
                    self.events.emit(
                        EventType.LOG,
                        f"[UNLOAD] {msg}",
                        level="success" if ok else "warning",
                    )

            outcome = {
                "finished": self._project_finished,
                "stopped": self._stop_event.is_set(),
                "report_json_path": str(json_path),
                "report_md_path": str(md_path),
                "report": report_obj,
                "report_markdown": md_text,
                "unload_result": unload_result,
            }
            self.events.emit(
                EventType.SESSION_COMPLETED,
                "All phases completed! Final report generated."
                if self._project_finished
                else "Session paused/stopped. State saved for resume.",
                data=outcome,
                level="success" if self._project_finished else "warning",
            )
            return outcome

        except Exception as err:
            self.usage_ledger.record_error(str(err))
            self.events.emit(EventType.ERROR, f"[ORCHESTRATOR ERROR] {err}", level="error")
            raise
        finally:
            self.live_request_manager.stop_watcher()
            self._running = False

    def run_smoke_test(self) -> Dict[str, Any]:
        inspection = self.adapter.inspect_server()
        if not inspection.reachable:
            raise RuntimeError(f"Smoke test failed: server unreachable at {self.config.api_base_url}: {inspection.error}")

        if not self.config.model_name and inspection.active_model_ids:
            self.config.model_name = inspection.active_model_ids[0]

        tok_count, is_exact = self.adapter.count_tokens("LAPO smoke test tokenization check.")
        resp = self.adapter.chat_completion(
            messages=[
                {"role": "system", "content": "Reply with the exact word OK."},
                {"role": "user", "content": "Ping"},
            ],
            max_tokens=64,
            tools_schema=None,
            stream=True,
        )
        return {
            "reachable": True,
            "model": self.config.model_name,
            "detected_context_tokens": inspection.detected_context_tokens,
            "tokenize_exact": is_exact,
            "sample_tokens": tok_count,
            "reply_preview": (resp.message.get("content") or resp.message.get("reasoning_content") or "")[:120],
            "elapsed_s": round(resp.elapsed_s, 2),
        }