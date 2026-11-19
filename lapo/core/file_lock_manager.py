"""Application-level SHA-256 file locks, dependency sync, and controlled repair queue."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from lapo.config import RuntimeConfig
from lapo.events import EventBus, EventType
from lapo.models import (
    LockedFileEntry,
    RepairObligation,
    atomic_write_json,
    read_json_safe,
    sha256_file,
)


class FileLockManager:
    """
    Enforces:
    - Immutability of finalized/locked phase files through model tools.
    - Read-before-edit protection for existing non-placeholder files.
    - Dependency synchronization (must read locked reference in current session before editing dependent).
    - User-authorized Unlock & controlled repair tracking.
    - SHA-256 integrity checks across restarts.
    """

    def __init__(self, config: RuntimeConfig, event_bus: Optional[EventBus] = None) -> None:
        self.config = config
        self.workspace_root = config.workspace_dir.resolve()
        self.events = event_bus or EventBus()

        self._locks: Dict[str, LockedFileEntry] = {}
        self._repair_queue: Dict[str, RepairObligation] = {}
        self._lock_candidates: Dict[int, Set[str]] = {}  # phase_id -> set of rel paths
        self._read_in_session: Set[str] = set()

        self.load()

    @property
    def _candidates_file(self) -> Path:
        return self.config.orchestrator_dir / "phase_lock_requests.json"

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def load(self) -> None:
        raw_locks = read_json_safe(self.config.locks_file, default={})
        self._locks = {}
        if isinstance(raw_locks, dict):
            for path_key, entry in raw_locks.items():
                if isinstance(entry, dict) and entry.get("path"):
                    self._locks[path_key] = LockedFileEntry.from_dict(entry)

        raw_repairs = read_json_safe(self.config.repair_queue_file, default={})
        self._repair_queue = {}
        if isinstance(raw_repairs, dict):
            for path_key, entry in raw_repairs.items():
                if isinstance(entry, dict) and entry.get("path"):
                    self._repair_queue[path_key] = RepairObligation.from_dict(entry)

        raw_cands = read_json_safe(self._candidates_file, default={})
        self._lock_candidates = {}
        if isinstance(raw_cands, dict):
            for pid_str, paths in raw_cands.items():
                try:
                    self._lock_candidates[int(pid_str)] = set(paths or [])
                except (TypeError, ValueError):
                    pass

    def save(self) -> None:
        atomic_write_json(
            self.config.locks_file,
            {k: v.to_dict() for k, v in sorted(self._locks.items())},
        )
        atomic_write_json(
            self.config.repair_queue_file,
            {k: v.to_dict() for k, v in sorted(self._repair_queue.items())},
        )
        atomic_write_json(
            self._candidates_file,
            {str(k): sorted(v) for k, v in sorted(self._lock_candidates.items())},
        )

    # ------------------------------------------------------------------
    # Session Read Tracking (Reset at Phase Boundaries & Compaction)
    # ------------------------------------------------------------------
    def record_read(self, rel_posix: str) -> None:
        self._read_in_session.add(rel_posix)
        if rel_posix in self._repair_queue:
            self._repair_queue[rel_posix].reread_in_session = True
            self.save()

    def reset_session_reads(self) -> None:
        self._read_in_session.clear()
        for repair in self._repair_queue.values():
            repair.reread_in_session = False
        if self._repair_queue:
            self.save()

    def has_read_in_session(self, rel_posix: str) -> bool:
        return rel_posix in self._read_in_session

    def mark_modified(self, rel_posix: str) -> None:
        if rel_posix in self._repair_queue:
            self._repair_queue[rel_posix].modified = True
            self._repair_queue[rel_posix].reverified = False
            self.save()

    # ------------------------------------------------------------------
    # Write Permission & Dependency Enforcement
    # ------------------------------------------------------------------
    def is_locked(self, rel_posix: str) -> bool:
        return rel_posix in self._locks

    def get_locked_dependencies_of(self, dependent_rel_path: str) -> List[LockedFileEntry]:
        refs: List[LockedFileEntry] = []
        for entry in self._locks.values():
            if dependent_rel_path in entry.dependents:
                refs.append(entry)
        return refs

    def check_write_permission(
        self,
        rel_posix: str,
        is_existing: bool,
        is_placeholder: bool = False,
    ) -> Tuple[bool, str]:
        # 1. Locked file guard
        if rel_posix in self._locks:
            entry = self._locks[rel_posix]
            return (
                False,
                f"LOCKED FILE REJECTION: '{rel_posix}' was finalized in Phase {entry.owner_phase_id} "
                f"(sha256:{entry.sha256[:12]}). Do NOT modify locked files. "
                "If a genuine fix is needed, report the blocker and ask the user to authorize Unlock.",
            )

        # 2. Repair queue guard: unlocked file MUST be reread in current session before editing
        if rel_posix in self._repair_queue and rel_posix not in self._read_in_session:
            return (
                False,
                f"REPAIR PROTOCOL: '{rel_posix}' is unlocked for controlled repair. "
                f"You must call read_file('{rel_posix}') in this session before modifying it.",
            )

        # 3. Read-before-edit guard for existing non-placeholder files
        if is_existing and not is_placeholder and rel_posix not in self._read_in_session:
            return (
                False,
                f"READ-BEFORE-EDIT PROTECTION: '{rel_posix}' already exists. "
                f"Call read_file('{rel_posix}') first in this session before overwriting or patching it.",
            )

        # 4. Dependency synchronization guard
        locked_refs = self.get_locked_dependencies_of(rel_posix)
        unread_refs = [ref.path for ref in locked_refs if ref.path not in self._read_in_session]
        if unread_refs:
            return (
                False,
                f"DEPENDENCY SYNC REQUIRED: '{rel_posix}' depends on locked file(s) {unread_refs}. "
                "Call read_file on each locked reference in this session before editing the dependent file.",
            )

        return True, "ok"

    # ------------------------------------------------------------------
    # Candidate Registration & Phase Lock Commits
    # ------------------------------------------------------------------
    def register_lock_candidate(self, rel_posix: str, phase_id: int) -> None:
        self._lock_candidates.setdefault(phase_id, set()).add(rel_posix)
        self.save()

    def get_lock_candidates(self, phase_id: int) -> List[str]:
        return sorted(self._lock_candidates.get(phase_id, set()))

    def register_dependency(self, locked_ref_path: str, dependent_path: str) -> None:
        """Declare that `dependent_path` depends on `locked_ref_path`."""
        if locked_ref_path in self._locks:
            entry = self._locks[locked_ref_path]
            if dependent_path not in entry.dependents:
                entry.dependents.append(dependent_path)
                entry.dependents.sort()
                self.save()

    def commit_phase_locks(
        self,
        phase_id: int,
        explicit_files: List[str],
        file_dependencies: Optional[Dict[str, List[str]]] = None,
        verification_status: str = "passed",
    ) -> List[str]:
        """
        Atomically compute SHA-256 hashes and lock all verified output files for a phase.
        `file_dependencies` maps: dependent_file -> [locked_reference_files] OR
        locked_file -> [dependent_files] (both directions handled intelligently).
        """
        candidates = set(self._lock_candidates.get(phase_id, set())) | set(explicit_files)
        newly_locked: List[str] = []

        for raw_path in sorted(candidates):
            rel_posix = raw_path.strip().replace("\\", "/").lstrip("./")
            if not rel_posix:
                continue
            full_path = (self.workspace_root / rel_posix).resolve()
            if not full_path.is_file():
                continue
            try:
                full_path.relative_to(self.workspace_root)
            except ValueError:
                continue

            existing_deps = list(self._locks[rel_posix].dependents) if rel_posix in self._locks else []
            self._locks[rel_posix] = LockedFileEntry(
                path=rel_posix,
                sha256=sha256_file(full_path),
                size=full_path.stat().st_size,
                owner_phase_id=phase_id,
                verification_status=verification_status,
                dependents=existing_deps,
            )
            newly_locked.append(rel_posix)

        # Wire dependencies if provided
        if isinstance(file_dependencies, dict):
            for key_path, values in file_dependencies.items():
                norm_key = key_path.strip().replace("\\", "/").lstrip("./")
                for val in values or []:
                    norm_val = str(val).strip().replace("\\", "/").lstrip("./")
                    if norm_key in self._locks and norm_val != norm_key:
                        self.register_dependency(norm_key, norm_val)
                    elif norm_val in self._locks and norm_key != norm_val:
                        self.register_dependency(norm_val, norm_key)

        self._lock_candidates.pop(phase_id, None)
        self.save()

        self.events.emit(
            EventType.LOCK_UPDATED,
            f"Locked {len(newly_locked)} file(s) for Phase {phase_id}.",
            data={"locked": newly_locked, "all_locks": self.list_locks_dicts()},
            level="success",
        )
        return newly_locked

    # ------------------------------------------------------------------
    # Controlled User Unlock & Re-Lock After Repair Verification
    # ------------------------------------------------------------------
    def unlock_for_repair(self, rel_path: str, reason: str = "User authorized repair") -> RepairObligation:
        rel_posix = rel_path.strip().replace("\\", "/").lstrip("./")
        if rel_posix not in self._locks:
            raise KeyError(f"File '{rel_posix}' is not currently locked.")

        entry = self._locks.pop(rel_posix)
        obligation = RepairObligation(
            path=rel_posix,
            owner_phase_id=entry.owner_phase_id,
            reason=reason,
            reread_in_session=False,
            modified=False,
            reverified=False,
        )
        self._repair_queue[rel_posix] = obligation
        self._read_in_session.discard(rel_posix)
        self.save()

        self.events.emit(
            EventType.LOCK_UPDATED,
            f"User unlocked '{rel_posix}' (owner Phase {entry.owner_phase_id}) for controlled repair.",
            data={"unlocked": rel_posix, "all_locks": self.list_locks_dicts()},
            level="warning",
        )
        return obligation

    def relock_repaired_file(self, rel_path: str, verification_status: str = "passed") -> LockedFileEntry:
        rel_posix = rel_path.strip().replace("\\", "/").lstrip("./")
        if rel_posix not in self._repair_queue:
            raise KeyError(f"File '{rel_posix}' is not in the repair queue.")

        obligation = self._repair_queue.pop(rel_posix)
        full_path = (self.workspace_root / rel_posix).resolve()
        if not full_path.is_file():
            raise FileNotFoundError(f"Repaired file missing on disk: '{rel_posix}'.")

        entry = LockedFileEntry(
            path=rel_posix,
            sha256=sha256_file(full_path),
            size=full_path.stat().st_size,
            owner_phase_id=obligation.owner_phase_id,
            verification_status=verification_status,
            dependents=[],
        )
        self._locks[rel_posix] = entry
        self.save()

        self.events.emit(
            EventType.LOCK_UPDATED,
            f"Re-locked repaired file '{rel_posix}' after Phase {obligation.owner_phase_id} verification passed.",
            data={"relocked": rel_posix, "all_locks": self.list_locks_dicts()},
            level="success",
        )
        return entry

    def lock_existing_file_manual(self, rel_path: str, owner_phase_id: int) -> LockedFileEntry:
        """Allow the user to manually lock a file from GUI/CLI."""
        rel_posix = rel_path.strip().replace("\\", "/").lstrip("./")
        full_path = (self.workspace_root / rel_posix).resolve()
        full_path.relative_to(self.workspace_root)
        if not full_path.is_file():
            raise FileNotFoundError(f"File not found in workspace: '{rel_posix}'.")

        entry = LockedFileEntry(
            path=rel_posix,
            sha256=sha256_file(full_path),
            size=full_path.stat().st_size,
            owner_phase_id=owner_phase_id,
            verification_status="manual_locked",
        )
        self._locks[rel_posix] = entry
        self._repair_queue.pop(rel_posix, None)
        self.save()
        self.events.emit(
            EventType.LOCK_UPDATED,
            f"Manually locked '{rel_posix}'.",
            data={"all_locks": self.list_locks_dicts()},
            level="info",
        )
        return entry

    # ------------------------------------------------------------------
    # Integrity Inspection & Serialization
    # ------------------------------------------------------------------
    def verify_integrity(self) -> List[Dict[str, str]]:
        """
        Detect missing or externally modified locked files by comparing on-disk SHA-256
        against locks.json.
        """
        issues: List[Dict[str, str]] = []
        for rel_posix, entry in sorted(self._locks.items()):
            full_path = self.workspace_root / rel_posix
            if not full_path.is_file():
                issues.append({"path": rel_posix, "issue": "missing_on_disk"})
                continue
            current_hash = sha256_file(full_path)
            if current_hash != entry.sha256:
                issues.append(
                    {
                        "path": rel_posix,
                        "issue": "external_modification_detected",
                        "expected_sha256": entry.sha256[:16],
                        "actual_sha256": current_hash[:16],
                    }
                )
        return issues

    def list_locks(self) -> List[LockedFileEntry]:
        return [self._locks[k] for k in sorted(self._locks.keys())]

    def list_locks_dicts(self) -> List[Dict[str, Any]]:
        return [entry.to_dict() for entry in self.list_locks()]

    def list_repair_queue(self) -> List[RepairObligation]:
        return [self._repair_queue[k] for k in sorted(self._repair_queue.keys())]

    def list_repair_queue_dicts(self) -> List[Dict[str, Any]]:
        return [item.to_dict() for item in self.list_repair_queue()]