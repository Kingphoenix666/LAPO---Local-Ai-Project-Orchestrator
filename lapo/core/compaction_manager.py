"""
KV-cache-preserving in-stream context compaction manager.
Preserves TOOLS_SCHEMA in the API payload while locking tools Python-side,
validates the model's continuation JSON (with deterministic fallback),
atomically saves state, and rebuilds a fresh session on the EXACT active phase.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from jsonschema import ValidationError, validate

from lapo.config import format_token_count
from lapo.core.file_lock_manager import FileLockManager
from lapo.core.live_request_manager import LiveRequestManager
from lapo.core.phase_manager import PhaseManager
from lapo.core.recovery_manager import RecoveryManager
from lapo.core.usage_ledger import UsageLedger
from lapo.core.verification_manager import VerificationManager
from lapo.events import EventBus, EventType
from lapo.models import PhaseDefinition, utc_now_iso
from lapo.tools.workspace_tools import WorkspaceTools


CONTINUATION_REPORT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": [
        "current_objective",
        "current_phase_id",
        "current_phase_name",
        "design_decisions_locked",
        "active_assumptions",
        "completed_work",
        "files_created_or_modified",
        "unfinished_work",
        "known_failures_and_blockers",
        "next_exact_action",
    ],
    "properties": {
        "current_objective": {"type": "string"},
        "current_phase_id": {"type": "integer"},
        "current_phase_name": {"type": "string"},
        "design_decisions_locked": {"type": "array", "items": {"type": "string"}},
        "active_assumptions": {"type": "array", "items": {"type": "string"}},
        "completed_work": {"type": "array", "items": {"type": "string"}},
        "files_created_or_modified": {"type": "array", "items": {"type": "string"}},
        "verified_phase_history": {"type": "array"},
        "locked_files_and_dependencies": {"type": "array"},
        "unfinished_work": {"type": "array", "items": {"type": "string"}},
        "known_failures_and_blockers": {"type": "array", "items": {"type": "string"}},
        "pending_user_requests": {"type": "array"},
        "next_exact_action": {"type": "string"},
    },
}


def build_in_stream_compaction_signal(
    active_phase: PhaseDefinition,
    active_tokens: int,
    trigger_tokens: int,
) -> str:
    return (
        "\n\n================================================================================\n"
        f"[ORCHESTRATOR IN-STREAM COMPACTION SIGNAL — CONTEXT REACHED {format_token_count(active_tokens)} "
        f"(THRESHOLD {format_token_count(trigger_tokens)})]\n"
        "Your previous action has been executed and persisted on disk.\n"
        f"Context compaction does NOT change your active phase. You remain strictly in "
        f"Phase {active_phase.id}: '{active_phase.name}'.\n\n"
        "1. Do NOT call any tools and do NOT output any `<file>` blocks in your next response.\n"
        "2. Synthesize everything learned and decided so far, then output ONLY a valid raw JSON object "
        "(no markdown code fences) matching this exact structure:\n"
        "{\n"
        f'  "current_objective": "Goal for Phase {active_phase.id}",\n'
        f'  "current_phase_id": {active_phase.id},\n'
        f'  "current_phase_name": "{active_phase.name}",\n'
        '  "design_decisions_locked": ["Key architectural/interface decisions"],\n'
        '  "active_assumptions": ["Current working assumptions"],\n'
        '  "completed_work": ["Completed tasks inside this phase"],\n'
        '  "files_created_or_modified": ["relative/path.ext"],\n'
        '  "unfinished_work": ["Exact remaining tasks in this phase"],\n'
        '  "known_failures_and_blockers": ["Open errors or blockers"],\n'
        '  "next_exact_action": "Exact first action to take immediately after context reset"\n'
        "}\n"
        "================================================================================\n"
    )


def parse_continuation_report(raw_text: Optional[str], active_phase: PhaseDefinition) -> Dict[str, Any]:
    text = raw_text or ""
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("No JSON object found in compaction output.")

    parsed = json.loads(cleaned[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("Compaction output root is not a JSON object.")

    parsed["current_phase_id"] = active_phase.id
    parsed["current_phase_name"] = active_phase.name

    array_keys = (
        "design_decisions_locked",
        "active_assumptions",
        "completed_work",
        "files_created_or_modified",
        "unfinished_work",
        "known_failures_and_blockers",
    )
    for key in array_keys:
        val = parsed.get(key)
        if val is None:
            parsed[key] = []
        elif isinstance(val, str):
            parsed[key] = [val]
        elif isinstance(val, list):
            parsed[key] = [str(item) for item in val]

    parsed.setdefault("current_objective", active_phase.prompt[:300])
    parsed.setdefault("next_exact_action", f"Continue unfinished work in Phase {active_phase.id}: {active_phase.name}")

    validate(instance=parsed, schema=CONTINUATION_REPORT_SCHEMA)
    return parsed


class CompactionManager:
    """Coordinates authoritative state snapshotting and deterministic context resets."""

    def __init__(
        self,
        phase_manager: PhaseManager,
        lock_manager: FileLockManager,
        verification_manager: VerificationManager,
        live_request_manager: LiveRequestManager,
        recovery_manager: RecoveryManager,
        usage_ledger: UsageLedger,
        event_bus: Optional[EventBus] = None,
    ) -> None:
        self.phase_manager = phase_manager
        self.lock_manager = lock_manager
        self.verification_manager = verification_manager
        self.live_request_manager = live_request_manager
        self.recovery_manager = recovery_manager
        self.usage_ledger = usage_ledger
        self.events = event_bus or EventBus()

    def build_deterministic_fallback_report(
        self,
        active_phase: PhaseDefinition,
        reason: str,
        machine_state: Dict[str, Any],
    ) -> Dict[str, Any]:
        prev_state = self.recovery_manager.load_state() or {}
        prev_report = prev_state.get("continuation_report") if isinstance(prev_state, dict) else {}
        prev_decisions = (
            list(prev_report.get("design_decisions_locked", []))
            if isinstance(prev_report, dict)
            else []
        )

        contracts = [c.get("path", "") for c in machine_state.get("file_contracts", []) if c.get("path")]
        pending_reqs = [
            f"{r.request_id}: {r.prompt}"
            for r in self.live_request_manager.get_unresolved_for_phase(active_phase.id)
        ]

        return {
            "current_objective": active_phase.prompt[:400],
            "current_phase_id": active_phase.id,
            "current_phase_name": active_phase.name,
            "design_decisions_locked": prev_decisions,
            "active_assumptions": ["Fallback summary generated from authoritative machine state."],
            "completed_work": [
                f"Files recorded in Phase {active_phase.id}: {', '.join(active_phase.created_or_modified_files) or 'None'}"
            ],
            "files_created_or_modified": sorted(set(active_phase.created_or_modified_files) | set(contracts[:20])),
            "verified_phase_history": [
                {"id": p.id, "name": p.name, "status": p.status}
                for p in (self.phase_manager.plan.phases if self.phase_manager.plan else [])
                if p.id in (self.phase_manager.plan.completed_phase_ids if self.phase_manager.plan else [])
            ],
            "locked_files_and_dependencies": self.lock_manager.list_locks_dicts(),
            "unfinished_work": [
                f"Inspect workspace files and complete remaining requirements for Phase {active_phase.id}: {active_phase.name}"
            ],
            "known_failures_and_blockers": [f"Compaction JSON fallback triggered: {reason[:220]}"],
            "pending_user_requests": pending_reqs,
            "next_exact_action": f"Inspect current files with list_dir/read_file and continue Phase {active_phase.id}.",
        }

    def finalize_compaction(
        self,
        raw_model_output: str,
        tools: WorkspaceTools,
        system_prompt: str,
        previous_tokens: int = 0,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any], bool]:
        tools.compaction_lock = True
        used_fallback = False

        try:
            active_phase = self.phase_manager.get_active_phase()
            plan = self.phase_manager.plan
            assert plan is not None

            machine_state = self.verification_manager.build_machine_verified_snapshot()

            try:
                report = parse_continuation_report(raw_model_output, active_phase)
            except (json.JSONDecodeError, ValidationError, ValueError, Exception) as err:
                used_fallback = True
                self.events.emit(
                    EventType.LOG,
                    f"[ORCHESTRATOR] >>> COMPACTION JSON FALLBACK TRIGGERED ({err}). "
                    "Using deterministic machine-verified state summary.",
                    level="warning",
                )
                report = self.build_deterministic_fallback_report(active_phase, str(err), machine_state)

            report["verified_phase_history"] = [
                {"id": p.id, "name": p.name, "status": p.status, "summary": p.summary}
                for p in plan.phases
                if p.id in plan.completed_phase_ids
            ]
            report["locked_files_and_dependencies"] = self.lock_manager.list_locks_dicts()
            report["pending_user_requests"] = [
                r.to_dict() for r in self.live_request_manager.get_unresolved_for_phase(active_phase.id)
            ]

            full_state: Dict[str, Any] = {
                "timestamp": utc_now_iso(),
                "plan_id": plan.plan_id,
                "active_phase_id": active_phase.id,
                "active_phase_name": active_phase.name,
                "machine_verified": machine_state,
                "continuation_report": report,
            }
            self.recovery_manager.save_state(full_state)

            self.lock_manager.reset_session_reads()
            self.live_request_manager.reset_delivery_flags_for_phase(active_phase.id)
            pending_dicts = [
                r.to_dict() for r in self.live_request_manager.pop_undelivered_for_phase(active_phase.id)
            ]

            handoff_msg = self.phase_manager.build_phase_handoff_message(
                machine_state=machine_state,
                locked_files=self.lock_manager.list_locks_dicts(),
                pending_requests=pending_dicts,
                repair_queue=self.lock_manager.list_repair_queue_dicts(),
                continuation_report=report,
            )

            fresh_messages: List[Dict[str, Any]] = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": handoff_msg},
            ]
            self.recovery_manager.save_journal(fresh_messages)
            self.usage_ledger.record_compaction()

            prev_lbl = format_token_count(previous_tokens) if previous_tokens > 0 else "full"
            mode_lbl = "deterministic fallback" if used_fallback else "model summary JSON"
            self.events.emit(
                EventType.COMPACTION_COMPLETED,
                (
                    "====================================================================\n"
                    f"[ORCHESTRATOR] >>> COMPACTION COMPLETE! STATE.json saved & Context RESET to 0 "
                    f"(was {prev_lbl} | source: {mode_lbl}).\n"
                    f"Continuing Phase {active_phase.id}: {active_phase.name} | "
                    f"Next Action: {report.get('next_exact_action', '')[:140]}\n"
                    "===================================================================="
                ),
                data={
                    "phase_id": active_phase.id,
                    "phase_name": active_phase.name,
                    "used_fallback": used_fallback,
                    "previous_tokens": previous_tokens,
                },
                level="success",
            )
            return fresh_messages, full_state, used_fallback
        finally:
            tools.compaction_lock = False
            tools.compaction_signal_text = ""