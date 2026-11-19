"""Shared typed data models and atomic JSON persistence helpers."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp_path.write_text(text, encoding="utf-8")
    os.replace(tmp_path, path)


def atomic_write_json(path: Path, data: Any) -> None:
    serialized = json.dumps(data, indent=2, ensure_ascii=False)
    atomic_write_text(path, serialized)


def read_json_safe(path: Path, default: Any = None) -> Any:
    if not path.is_file() or path.stat().st_size == 0:
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


class ModelAvailability(str, Enum):
    ACTIVE = "active"
    INSTALLED_NOT_LOADED = "installed_not_loaded"
    API_ONLY = "api_only"


@dataclass
class ModelInfo:
    id: str
    availability: ModelAvailability
    context_length: Optional[int] = None
    source: str = "api"
    details: Dict[str, Any] = field(default_factory=dict)

    @property
    def display_label(self) -> str:
        tag_map = {
            ModelAvailability.ACTIVE: "[ACTIVE]",
            ModelAvailability.INSTALLED_NOT_LOADED: "[INSTALLED - NOT LOADED]",
            ModelAvailability.API_ONLY: "[API ONLY]",
        }
        ctx = f" ({self.context_length // 1024}K)" if self.context_length else ""
        return f"{tag_map[self.availability]} {self.id}{ctx}"


class PhaseStatus(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class PhaseDefinition:
    id: int
    name: str
    prompt: str
    verification_commands: List[str] = field(default_factory=list)
    status: str = PhaseStatus.PENDING.value
    created_or_modified_files: List[str] = field(default_factory=list)
    locked_outputs: List[str] = field(default_factory=list)
    declared_dependencies: Dict[str, List[str]] = field(default_factory=dict)
    verification_history: List[Dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    completed_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PhaseDefinition":
        return cls(
            id=int(data["id"]),
            name=str(data["name"]),
            prompt=str(data["prompt"]),
            verification_commands=list(data.get("verification_commands", [])),
            status=str(data.get("status", PhaseStatus.PENDING.value)),
            created_or_modified_files=list(data.get("created_or_modified_files", [])),
            locked_outputs=list(data.get("locked_outputs", [])),
            declared_dependencies=dict(data.get("declared_dependencies", {})),
            verification_history=list(data.get("verification_history", [])),
            summary=str(data.get("summary", "")),
            completed_at=data.get("completed_at"),
        )


@dataclass
class PhasePlan:
    plan_id: str
    workspace: str
    phases: List[PhaseDefinition]
    active_phase_id: int
    completed_phase_ids: List[int] = field(default_factory=list)
    protocol: str = "LAPO_PHASE_EXECUTION_PROTOCOL"
    protocol_version: int = 1
    transition_authority: str = "ORCHESTRATOR_ONLY"
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)

    def get_phase(self, phase_id: int) -> Optional[PhaseDefinition]:
        for phase in self.phases:
            if phase.id == phase_id:
                return phase
        return None

    @property
    def active_phase(self) -> Optional[PhaseDefinition]:
        return self.get_phase(self.active_phase_id)

    @property
    def is_finished(self) -> bool:
        return len(self.completed_phase_ids) >= len(self.phases) and len(self.phases) > 0

    def to_manifest_dict(self) -> Dict[str, Any]:
        active = self.active_phase
        phase_entries = []
        for p in self.phases:
            entry = p.to_dict()
            entry["is_active"] = (p.id == self.active_phase_id) and (p.id not in self.completed_phase_ids)
            entry["is_completed"] = p.id in self.completed_phase_ids
            entry["is_future"] = (not entry["is_active"]) and (not entry["is_completed"])
            phase_entries.append(entry)

        return {
            "protocol": self.protocol,
            "protocol_version": self.protocol_version,
            "plan_id": self.plan_id,
            "workspace": self.workspace,
            "phase_count": len(self.phases),
            "active_phase_id": self.active_phase_id,
            "active_phase_name": active.name if active else "",
            "completed_phase_ids": list(self.completed_phase_ids),
            "transition_authority": self.transition_authority,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "phases": phase_entries,
        }

    @classmethod
    def from_manifest_dict(cls, data: Dict[str, Any]) -> "PhasePlan":
        phases = [PhaseDefinition.from_dict(item) for item in data.get("phases", [])]
        return cls(
            plan_id=str(data["plan_id"]),
            workspace=str(data["workspace"]),
            phases=phases,
            active_phase_id=int(data["active_phase_id"]),
            completed_phase_ids=[int(i) for i in data.get("completed_phase_ids", [])],
            protocol=str(data.get("protocol", "LAPO_PHASE_EXECUTION_PROTOCOL")),
            protocol_version=int(data.get("protocol_version", 1)),
            transition_authority=str(data.get("transition_authority", "ORCHESTRATOR_ONLY")),
            created_at=str(data.get("created_at", utc_now_iso())),
            updated_at=str(data.get("updated_at", utc_now_iso())),
        )


@dataclass
class LockedFileEntry:
    path: str
    sha256: str
    size: int
    owner_phase_id: int
    verification_status: str = "passed"
    dependents: List[str] = field(default_factory=list)
    locked_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LockedFileEntry":
        return cls(
            path=str(data["path"]),
            sha256=str(data["sha256"]),
            size=int(data["size"]),
            owner_phase_id=int(data["owner_phase_id"]),
            verification_status=str(data.get("verification_status", "passed")),
            dependents=list(data.get("dependents", [])),
            locked_at=str(data.get("locked_at", utc_now_iso())),
        )


@dataclass
class RepairObligation:
    path: str
    owner_phase_id: int
    unlocked_at: str = field(default_factory=utc_now_iso)
    reason: str = "User authorized repair"
    reread_in_session: bool = False
    modified: bool = False
    reverified: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RepairObligation":
        return cls(
            path=str(data["path"]),
            owner_phase_id=int(data["owner_phase_id"]),
            unlocked_at=str(data.get("unlocked_at", utc_now_iso())),
            reason=str(data.get("reason", "User authorized repair")),
            reread_in_session=bool(data.get("reread_in_session", False)),
            modified=bool(data.get("modified", False)),
            reverified=bool(data.get("reverified", False)),
        )


@dataclass
class LiveUserRequest:
    request_id: str
    phase: str  # phase ID as string (e.g. "2") or "active"
    prompt: str
    timestamp: str = field(default_factory=utc_now_iso)
    source_file: str = "user_request.json"
    archived_file: str = ""
    delivered_to_model: bool = False
    resolved: bool = False
    resolved_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LiveUserRequest":
        return cls(
            request_id=str(data["request_id"]),
            phase=str(data.get("phase", "active")),
            prompt=str(data["prompt"]),
            timestamp=str(data.get("timestamp", utc_now_iso())),
            source_file=str(data.get("source_file", "user_request.json")),
            archived_file=str(data.get("archived_file", "")),
            delivered_to_model=bool(data.get("delivered_to_model", False)),
            resolved=bool(data.get("resolved", False)),
            resolved_at=data.get("resolved_at"),
        )


@dataclass
class VerificationCommandResult:
    command: str
    exit_code: int
    passed: bool
    stdout: str
    stderr: str
    duration_s: float
    executed_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class VerificationSuiteResult:
    phase_id: int
    status: str  # "passed" | "failed" | "manual_required"
    worktree_fingerprint: str
    command_results: List[VerificationCommandResult] = field(default_factory=list)
    manual_approved: bool = False
    completed_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "phase_id": self.phase_id,
            "status": self.status,
            "worktree_fingerprint": self.worktree_fingerprint,
            "command_results": [r.to_dict() for r in self.command_results],
            "manual_approved": self.manual_approved,
            "completed_at": self.completed_at,
        }