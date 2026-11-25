"""Explicit multi-phase execution protocol, manifest persistence, and model handoff builder."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from lapo.config import RuntimeConfig
from lapo.models import (
    PhaseDefinition,
    PhasePlan,
    PhaseStatus,
    atomic_write_json,
    read_json_safe,
    sha256_bytes,
    utc_now_iso,
)


def compute_plan_id(phases: List[PhaseDefinition]) -> str:
    canonical = [
        {
            "id": p.id,
            "name": p.name,
            "prompt": p.prompt,
            "verification_commands": list(p.verification_commands),
        }
        for p in phases
    ]
    raw = json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return f"sha256:{sha256_bytes(raw)[:24]}"


class PhaseManager:
    """
    Sole authority for phase manifest creation, persistence, system protocol injection,
    and orchestrator-controlled phase transitions.
    """

    def __init__(self, config: RuntimeConfig) -> None:
        self.config = config
        self.plan: Optional[PhasePlan] = None

    def create_plan(self, raw_phases: List[Dict[str, Any]]) -> PhasePlan:
        if not raw_phases:
            raise ValueError("Cannot create a phase execution plan with zero phases.")

        phases: List[PhaseDefinition] = []
        for idx, item in enumerate(raw_phases, start=1):
            name = str(item.get("name") or f"Phase {idx}").strip()
            prompt = str(item.get("prompt") or "").strip()
            if not prompt:
                raise ValueError(f"Phase {idx} ('{name}') has an empty prompt.")
            cmds = [str(c).strip() for c in item.get("verification_commands", []) if str(c).strip()]
            phases.append(
                PhaseDefinition(
                    id=idx,
                    name=name,
                    prompt=prompt,
                    verification_commands=cmds,
                    status=PhaseStatus.ACTIVE.value if idx == 1 else PhaseStatus.PENDING.value,
                )
            )

        plan_id = compute_plan_id(phases)
        self.plan = PhasePlan(
            plan_id=plan_id,
            workspace=str(self.config.workspace_dir),
            phases=phases,
            active_phase_id=1,
            completed_phase_ids=[],
        )
        self.save()
        return self.plan

    def load(self) -> Optional[PhasePlan]:
        data = read_json_safe(self.config.plan_file)
        if not isinstance(data, dict) or not data.get("phases"):
            return None
        self.plan = PhasePlan.from_manifest_dict(data)
        return self.plan

    def save(self) -> None:
        if self.plan is None:
            return
        self.plan.updated_at = utc_now_iso()
        atomic_write_json(self.config.plan_file, self.plan.to_manifest_dict())

    def get_active_phase(self) -> PhaseDefinition:
        if self.plan is None:
            raise RuntimeError("PhasePlan is not initialized.")
        phase = self.plan.active_phase
        if phase is None:
            raise RuntimeError(f"Active phase ID {self.plan.active_phase_id} not found in plan.")
        return phase

    def get_phase_by_id(self, phase_id: int) -> Optional[PhaseDefinition]:
        if self.plan is None:
            return None
        return self.plan.get_phase(phase_id)

    def record_modified_file(self, rel_path: str) -> None:
        if self.plan is None:
            return
        active = self.plan.active_phase
        if active and rel_path not in active.created_or_modified_files:
            active.created_or_modified_files.append(rel_path)
            self.save()

    def record_verification(self, phase_id: int, result_dict: Dict[str, Any]) -> None:
        if self.plan is None:
            return
        phase = self.plan.get_phase(phase_id)
        if phase is None:
            return
        phase.verification_history.append(result_dict)
        self.save()

    def set_phase_status(self, phase_id: int, status: PhaseStatus) -> None:
        if self.plan is None:
            return
        phase = self.plan.get_phase(phase_id)
        if phase is None:
            return
        phase.status = status.value
        self.save()

    def advance_phase(
        self,
        summary: str,
        locked_files: List[str],
        declared_dependencies: Dict[str, List[str]],
    ) -> Tuple[bool, Optional[PhaseDefinition]]:
        if self.plan is None:
            raise RuntimeError("PhasePlan is not initialized.")

        active = self.get_active_phase()
        active.status = PhaseStatus.COMPLETED.value
        active.summary = summary
        active.locked_outputs = sorted(set(active.locked_outputs) | set(locked_files))
        for k, deps in declared_dependencies.items():
            existing = set(active.declared_dependencies.get(k, []))
            active.declared_dependencies[k] = sorted(existing | set(deps))
        active.completed_at = utc_now_iso()

        if active.id not in self.plan.completed_phase_ids:
            self.plan.completed_phase_ids.append(active.id)

        next_phase: Optional[PhaseDefinition] = None
        for p in self.plan.phases:
            if p.id not in self.plan.completed_phase_ids:
                next_phase = p
                break

        if next_phase is not None:
            self.plan.active_phase_id = next_phase.id
            next_phase.status = PhaseStatus.ACTIVE.value
            self.save()
            return False, next_phase

        self.save()
        return True, None

    def build_system_prompt(self, tool_protocol: str = "auto") -> str:
        skills_note = (
            f"A read-only Skills directory is configured at `{self.config.skills_dir}`. "
            "Use `list_skills` and `read_skill` to consult guidelines before designing or coding."
            if self.config.skills_dir
            else "No external Skills directory is configured for this run."
        )

        auto_verify_note = (
            "UNATTENDED SELF-VERIFICATION MODE IS ACTIVE: The user may be away while you work. "
            "Write automated tests (e.g., `test_*.py` using `unittest` or the project's test framework) "
            "and verify your own work using `run_checks` or `run_terminal` BEFORE calling `complete_phase`. "
            "In your final phase `summary`, include clear step-by-step instructions showing the user how to run and test the finished project."
            if self.config.unattended_auto_verify
            else "Manual phase approval is enabled for phases without configured verification commands."
        )

        text_tool_clause = ""
        if tool_protocol == "text":
            text_tool_clause = (
                "\n### TEXT-BASED TOOL PROTOCOL (ACTIVE)\n"
                "Native function calling is disabled for this session. To invoke any tool, emit:\n"
                "<tool_call>\n"
                '{"name": "tool_name", "arguments": {"arg": "value"}}\n'
                "</tool_call>\n"
                "Available tools: list_dir, read_file, search_text, write_file, patch_file, delete_file, "
                "list_skills, read_skill, lock_file, run_checks, run_terminal, complete_phase.\n"
            )

        return (
            "You are an autonomous project engineering agent supervised by the "
            "Local AI Project Orchestrator (LAPO).\n\n"
            "### MANDATORY PHASE EXECUTION PROTOCOL\n"
            "1. This project is divided into strictly ordered, independently verified phases.\n"
            "2. You MUST implement ONLY the currently ACTIVE phase. Future phases are provided "
            "as titles/summaries purely for interface & dependency planning—NEVER implement future phases early.\n"
            "3. You CANNOT advance to the next phase yourself. The Orchestrator is the sole authority.\n"
            "4. Even if you believe the overall goal looks close to done, NEVER skip remaining phases.\n"
            "5. Context compaction does NOT change the active phase. After a reset, continue the exact same active phase.\n"
            "6. NEVER recreate or overwrite files already completed and locked in previous phases.\n"
            "7. Always read locked interface/dependency files via `read_file` before writing dependent files.\n"
            "8. Never fabricate test or verification results; always report blockers honestly.\n"
            "9. When the active phase is genuinely complete and verified, call `complete_phase` with your summary, "
            "`files_to_lock`, `file_dependencies`, and `resolved_request_ids`.\n\n"
            f"### SELF-TESTING & VERIFICATION POLICY\n{auto_verify_note}\n\n"
            "### ANTI-DRAFTING & CONTEXT HYGIENE POLICY\n"
            "- Keep `<think>` / reasoning concise (brief outline of interfaces and edge cases only). "
            "NEVER draft complete source files or large test suites inside `<think>`; write directly to disk "
            "and rely on `run_checks` to catch and iterate on issues.\n"
            "- After a context compaction reset, consult `python_ast_map` in the Machine-Verified Snapshot "
            "instead of re-reading entire unchanged files from scratch.\n"
            "- Use `delete_file` to remove any temporary scratch files you created (unlocked files only) "
            "before calling `complete_phase`.\n\n"
            "### DUAL FILE-WRITING PROTOCOL (ANTI-TRUNCATION)\n"
            "- For small edits or targeted changes, use `write_file` or `patch_file`.\n"
            "- For medium/large files (>80 lines), you are STRONGLY encouraged to output files directly "
            "in your assistant response using unescaped inline blocks so JSON escaping never truncates:\n"
            '<file path="relative/path/to/file.ext">\n'
            "// complete file contents\n"
            "</file>\n"
            "The Orchestrator extracts every `<file path=\"...\">...</file>` block, enforces all lock/path "
            "security rules, writes it atomically to disk, and reports back immediately.\n\n"
            f"### SKILLS ACCESS\n{skills_note}\n"
            f"{text_tool_clause}"
        )

    def build_phase_handoff_message(
        self,
        machine_state: Dict[str, Any],
        locked_files: List[Dict[str, Any]],
        pending_requests: List[Dict[str, Any]],
        repair_queue: List[Dict[str, Any]],
        continuation_report: Optional[Dict[str, Any]] = None,
    ) -> str:
        if self.plan is None:
            raise RuntimeError("PhasePlan is not initialized.")

        active = self.get_active_phase()
        completed_overview = [
            {
                "id": p.id,
                "name": p.name,
                "summary": p.summary,
                "locked_outputs": p.locked_outputs,
            }
            for p in self.plan.phases
            if p.id in self.plan.completed_phase_ids
        ]
        future_overview = [
            {
                "id": p.id,
                "name": p.name,
                "scope_preview": p.prompt[:240],
            }
            for p in self.plan.phases
            if p.id != active.id and p.id not in self.plan.completed_phase_ids
        ]

        verify_text = (
            json.dumps(active.verification_commands, indent=2, ensure_ascii=False)
            if active.verification_commands
            else (
                "No custom commands specified — Orchestrator will run automatic syntax/compilation "
                "& auto-detected test suite verification (`run_checks`) and auto-advance on pass."
                if self.config.unattended_auto_verify
                else "None configured (manual user approval gate will be triggered upon `complete_phase`)."
            )
        )

        sections: List[str] = [
            "========================================================================",
            f"AUTHORITATIVE PHASE HANDOFF — ACTIVE PHASE {active.id}/{len(self.plan.phases)}: {active.name}",
            f"Plan ID: {self.plan.plan_id}",
            "========================================================================",
            "",
            "### ACTIVE PHASE INSTRUCTIONS (YOUR ONLY IMPLEMENTATION TARGET)",
            active.prompt,
            "",
            "### VERIFICATION FOR THIS PHASE",
            verify_text,
            "",
            "### COMPLETED PHASES (DO NOT REDO)",
            json.dumps(completed_overview, indent=2, ensure_ascii=False),
            "",
            "### FUTURE PHASES (FOR ARCHITECTURAL ALIGNMENT ONLY — DO NOT IMPLEMENT YET)",
            json.dumps(future_overview, indent=2, ensure_ascii=False),
            "",
            "### LOCKED FILES (READ-ONLY — MUST READ BEFORE MODIFYING DEPENDENTS)",
            json.dumps(locked_files, indent=2, ensure_ascii=False),
        ]

        if repair_queue:
            sections.extend(
                [
                    "",
                    "### AUTHORIZED PREVIOUS-PHASE REPAIR OBLIGATIONS (HIGH PRIORITY)",
                    "The user explicitly unlocked the following file(s) for minimal compatible repair. "
                    "You MUST call `read_file` on each unlocked file before editing it. "
                    "Its owning phase checks will re-run before this phase can close:",
                    json.dumps(repair_queue, indent=2, ensure_ascii=False),
                ]
            )

        if pending_requests:
            sections.extend(
                [
                    "",
                    "### PENDING LIVE USER REQUESTS FOR THIS PHASE (MUST RESOLVE & ACKNOWLEDGE IDs)",
                    json.dumps(pending_requests, indent=2, ensure_ascii=False),
                ]
            )

        if continuation_report:
            sections.extend(
                [
                    "",
                    "### AGENT CONTINUATION REPORT (FROM PREVIOUS COMPACTED CONTEXT)",
                    json.dumps(continuation_report, indent=2, ensure_ascii=False),
                ]
            )

        sections.extend(
            [
                "",
                "### MACHINE-VERIFIED WORKSPACE SNAPSHOT (AUTHORITATIVE)",
                f"```json\n{json.dumps(machine_state, indent=2, ensure_ascii=False)}\n```",
                "",
                "Begin or continue work strictly on the ACTIVE phase above.",
            ]
        )
        return "\n".join(sections)