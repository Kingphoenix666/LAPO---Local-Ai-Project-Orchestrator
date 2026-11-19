"""Atomic session state & journal persistence with strict plan-identity crash recovery."""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from lapo.config import RuntimeConfig
from lapo.core.file_lock_manager import FileLockManager
from lapo.models import PhasePlan, atomic_write_json, read_json_safe


@dataclass
class RecoveryInspection:
    has_existing_session: bool
    can_resume: bool
    plan_id: Optional[str] = None
    active_phase_id: Optional[int] = None
    active_phase_name: str = ""
    completed_phases: int = 0
    total_phases: int = 0
    journal_message_count: int = 0
    lock_integrity_issues: List[Dict[str, str]] = field(default_factory=list)
    reason: str = ""


class RecoveryManager:
    """
    Manages atomic persistence of `state.json` and `journal.json`, verifies plan identity
    before resuming, and safely archives old `.orchestrator/` runs on fresh start.
    """

    def __init__(self, config: RuntimeConfig) -> None:
        self.config = config

    def save_state(self, state_data: Dict[str, Any]) -> None:
        atomic_write_json(self.config.state_file, state_data)

    def load_state(self) -> Optional[Dict[str, Any]]:
        data = read_json_safe(self.config.state_file)
        return data if isinstance(data, dict) else None

    def save_journal(self, messages: List[Dict[str, Any]]) -> None:
        atomic_write_json(self.config.journal_file, messages)

    def load_journal(self) -> Optional[List[Dict[str, Any]]]:
        data = read_json_safe(self.config.journal_file)
        if not isinstance(data, list) or len(data) < 2:
            return None

        # Trim trailing empty assistant stubs left behind by an abrupt crash
        cleaned = list(data)
        while (
            len(cleaned) > 2
            and isinstance(cleaned[-1], dict)
            and cleaned[-1].get("role") == "assistant"
            and not cleaned[-1].get("tool_calls")
            and not (cleaned[-1].get("content") or "").strip()
        ):
            cleaned.pop()

        return cleaned if len(cleaned) >= 2 else None

    def inspect_workspace(
        self,
        expected_plan_id: Optional[str] = None,
    ) -> RecoveryInspection:
        plan_data = read_json_safe(self.config.plan_file)
        if not isinstance(plan_data, dict) or not plan_data.get("phases"):
            return RecoveryInspection(
                has_existing_session=False,
                can_resume=False,
                reason="No existing phase execution plan found in workspace.",
            )

        plan = PhasePlan.from_manifest_dict(plan_data)
        journal = self.load_journal()
        lock_mgr = FileLockManager(self.config)
        integrity_issues = lock_mgr.verify_integrity()

        if expected_plan_id and plan.plan_id != expected_plan_id:
            return RecoveryInspection(
                has_existing_session=True,
                can_resume=False,
                plan_id=plan.plan_id,
                active_phase_id=plan.active_phase_id,
                active_phase_name=plan.active_phase.name if plan.active_phase else "",
                completed_phases=len(plan.completed_phase_ids),
                total_phases=len(plan.phases),
                journal_message_count=len(journal) if journal else 0,
                lock_integrity_issues=integrity_issues,
                reason=(
                    f"Plan identity mismatch: saved plan '{plan.plan_id}' differs from "
                    f"supplied plan '{expected_plan_id}'. Refusing to resume an unrelated plan."
                ),
            )

        if plan.is_finished:
            return RecoveryInspection(
                has_existing_session=True,
                can_resume=False,
                plan_id=plan.plan_id,
                active_phase_id=plan.active_phase_id,
                active_phase_name=plan.active_phase.name if plan.active_phase else "",
                completed_phases=len(plan.completed_phase_ids),
                total_phases=len(plan.phases),
                journal_message_count=len(journal) if journal else 0,
                lock_integrity_issues=integrity_issues,
                reason="Saved session already completed all phases.",
            )

        return RecoveryInspection(
            has_existing_session=True,
            can_resume=True,
            plan_id=plan.plan_id,
            active_phase_id=plan.active_phase_id,
            active_phase_name=plan.active_phase.name if plan.active_phase else "",
            completed_phases=len(plan.completed_phase_ids),
            total_phases=len(plan.phases),
            journal_message_count=len(journal) if journal else 0,
            lock_integrity_issues=integrity_issues,
            reason="Resumable session detected.",
        )

    def archive_previous_session(self) -> Optional[Path]:
        """Archive an old `.orchestrator/` directory cleanly before starting a fresh run."""
        orch_dir = self.config.orchestrator_dir
        if not orch_dir.is_dir():
            return None
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        archive_path = self.config.workspace_dir / f".orchestrator_archive_{stamp}"
        counter = 1
        while archive_path.exists():
            archive_path = self.config.workspace_dir / f".orchestrator_archive_{stamp}_{counter}"
            counter += 1
        shutil.move(str(orch_dir), str(archive_path))
        return archive_path