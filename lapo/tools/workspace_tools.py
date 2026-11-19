"""
Confined, lock-aware Workspace & read-only Skills tool executor.
Shared identically by GUI and CLI sessions.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from lapo.config import RuntimeConfig
from lapo.events import EventBus, EventType
from lapo.models import atomic_write_text
from lapo.tools.inline_file_writer import extract_inline_file_blocks


PROTECTED_ROOT_NAMES = {".orchestrator", ".git"}
IGNORED_DIR_NAMES = {"node_modules", ".git", ".orchestrator", "__pycache__", ".venv", "venv", "dist", "build"}
PLACEHOLDER_MAX_CHARS = 100
PLACEHOLDER_MARKERS = ("export {};", "TODO", "Pending phase implementation", "pass")


TOOLS_SCHEMA: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List files and subdirectories inside a relative workspace directory.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Relative path, e.g. '.' or 'src'."}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a workspace file (optionally bounded by 1-based start_line and end_line).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer", "minimum": 1},
                    "end_line": {"type": "integer", "minimum": 1},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_text",
            "description": "Search workspace text files using plain-text or optional regex query.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "path": {"type": "string", "description": "Optional relative subfolder (default '.')."},
                    "is_regex": {"type": "boolean", "description": "Treat query as regular expression."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Create a new file (or overwrite an unlocked file after reading it). "
                "For large multi-line files (>80 lines), you may instead emit "
                '<file path="relative/path.ext">...full code...</file> directly in your assistant response '
                "to avoid JSON escaping truncation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "patch_file",
            "description": "Apply a targeted snippet replacement inside an existing unlocked file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "target_snippet": {"type": "string"},
                    "replacement_snippet": {"type": "string"},
                },
                "required": ["path", "target_snippet", "replacement_snippet"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_skills",
            "description": "List reference skill/guideline files inside the read-only Skills directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Optional relative path inside Skills dir."}
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_skill",
            "description": "Read a reference file from the read-only Skills directory.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lock_file",
            "description": "Register a stable phase output file as a candidate for locking upon phase verification.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_checks",
            "description": "Run the active phase's configured verification commands.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_terminal",
            "description": "Run a single user-allowlisted verification command (strictly shell=False).",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "complete_phase",
            "description": "Request phase completion and verification gate acceptance from the Orchestrator.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "files_to_lock": {"type": "array", "items": {"type": "string"}},
                    "file_dependencies": {
                        "type": "object",
                        "additionalProperties": {"type": "array", "items": {"type": "string"}},
                    },
                    "resolved_request_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["summary"],
            },
        },
    },
]


class WorkspaceTools:
    """
    Executes model tool calls and inline `<file>` writes with strict security,
    backup creation, file-lock enforcement, and dependency read tracking.
    """

    def __init__(
        self,
        config: RuntimeConfig,
        lock_manager: Any,
        verification_manager: Any,
        event_bus: Optional[EventBus] = None,
        get_active_phase_id: Optional[Callable[[], int]] = None,
        get_active_verification_commands: Optional[Callable[[], List[str]]] = None,
        on_file_modified: Optional[Callable[[str], None]] = None,
        on_complete_phase: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
    ) -> None:
        self.config = config
        self.workspace_root = config.workspace_dir.resolve()
        self.skills_root = config.skills_dir.resolve() if config.skills_dir else None
        self.lock_manager = lock_manager
        self.verification_manager = verification_manager
        self.events = event_bus or EventBus()

        self._get_active_phase_id = get_active_phase_id or (lambda: 1)
        self._get_active_verification_commands = get_active_verification_commands or (lambda: [])
        self._on_file_modified = on_file_modified
        self._on_complete_phase = on_complete_phase

        self.compaction_lock: bool = False
        self.compaction_signal_text: str = ""

    # ------------------------------------------------------------------
    # Path Confinement & Security Helpers
    # ------------------------------------------------------------------
    def _check_no_symlink_escape(self, base: Path, candidate: Path) -> None:
        """Walk every ancestor from base to candidate and reject symlinks escaping base."""
        cur = base
        try:
            rel_parts = candidate.relative_to(base).parts
        except ValueError:
            raise PermissionError("Path escapes allowed root directory.")

        for part in rel_parts:
            cur = cur / part
            if cur.is_symlink():
                resolved_link = cur.resolve()
                try:
                    resolved_link.relative_to(base)
                except ValueError:
                    raise PermissionError(f"Symlink escape blocked: '{part}' points outside root.")

    def _resolve_workspace_path(self, rel_path: str, allow_root: bool = False) -> Tuple[Path, str]:
        raw = (rel_path or "").strip()
        if not raw or "\x00" in raw:
            raise PermissionError("Empty or invalid file path.")

        # Normalize Windows/POSIX separators
        candidate = self.workspace_root / raw
        resolved = candidate.resolve()

        try:
            rel = resolved.relative_to(self.workspace_root)
        except ValueError:
            raise PermissionError(f"Path traversal blocked: '{rel_path}' resolves outside workspace.")

        self._check_no_symlink_escape(self.workspace_root, candidate)

        rel_posix = rel.as_posix()
        if rel_posix == ".":
            if allow_root:
                return resolved, "."
            raise PermissionError("Operation requires a file or subdirectory path, not workspace root.")

        top_part = rel.parts[0]
        if top_part in PROTECTED_ROOT_NAMES:
            raise PermissionError(
                f"Access denied: '{top_part}' is a protected internal directory off-limits to model tools."
            )

        if rel.name == "user_request.json" or rel.name.startswith("readed_user_request_"):
            raise PermissionError(
                "Access denied: Live user request control files cannot be accessed directly by model tools."
            )

        return resolved, rel_posix

    def _resolve_skills_path(self, rel_path: str, allow_root: bool = False) -> Tuple[Path, str]:
        if not self.skills_root or not self.skills_root.is_dir():
            raise FileNotFoundError("No Skills directory is configured for this project.")

        raw = (rel_path or ".").strip()
        if "\x00" in raw:
            raise PermissionError("Invalid skills path.")

        candidate = self.skills_root / raw
        resolved = candidate.resolve()

        try:
            rel = resolved.relative_to(self.skills_root)
        except ValueError:
            raise PermissionError(f"Skills path traversal blocked: '{rel_path}'.")

        self._check_no_symlink_escape(self.skills_root, candidate)

        rel_posix = rel.as_posix()
        if rel_posix == "." and not allow_root:
            raise PermissionError("Specify a file inside the Skills directory.")
        return resolved, rel_posix

    def _validate_content_safety(self, content: str) -> None:
        encoded = content.encode("utf-8")
        if len(encoded) > self.config.max_file_size_bytes:
            raise ValueError(
                f"File exceeds configured size limit ({len(encoded)} > {self.config.max_file_size_bytes} bytes)."
            )

    def _is_placeholder_file(self, path: Path) -> bool:
        if not path.is_file():
            return False
        size = path.stat().st_size
        if size == 0:
            return True
        if size <= PLACEHOLDER_MAX_CHARS:
            text = path.read_text(encoding="utf-8", errors="replace").strip()
            if not text or any(marker in text for marker in PLACEHOLDER_MARKERS):
                return True
        return False

    def _backup_existing_file(self, target: Path, rel_posix: str) -> Optional[str]:
        if not target.is_file() or target.stat().st_size == 0:
            return None
        self.config.backups_dir.mkdir(parents=True, exist_ok=True)
        safe_name = rel_posix.replace("/", "__").replace("\\", "__")
        stamp = int(time.time() * 1000)
        backup_path = self.config.backups_dir / f"{stamp}_{safe_name}"
        shutil.copy2(target, backup_path)
        return backup_path.name

    # ------------------------------------------------------------------
    # Unified Safe Write (Used by both `write_file` and Inline `<file>`)
    # ------------------------------------------------------------------
    def _perform_safe_write(self, rel_path: str, content: str, source: str) -> Dict[str, Any]:
        target, rel_posix = self._resolve_workspace_path(rel_path)
        self._validate_content_safety(content)

        is_existing = target.is_file()
        is_placeholder = self._is_placeholder_file(target) if is_existing else False

        allowed, reason = self.lock_manager.check_write_permission(
            rel_posix,
            is_existing=is_existing,
            is_placeholder=is_placeholder,
        )
        if not allowed:
            raise PermissionError(reason)

        backup_name = self._backup_existing_file(target, rel_posix) if (is_existing and not is_placeholder) else None
        atomic_write_text(target, content)

        self.lock_manager.mark_modified(rel_posix)
        self.lock_manager.record_read(rel_posix)
        if self._on_file_modified:
            self._on_file_modified(rel_posix)

        fp = self.verification_manager.compute_worktree_fingerprint()
        result = {
            "status": "ok",
            "source": source,
            "path": rel_posix,
            "bytes_written": len(content.encode("utf-8")),
            "backup": backup_name,
            "worktree_fingerprint": fp,
        }
        return result

    def extract_and_write_inline_files(self, assistant_text: str) -> Tuple[List[str], List[Dict[str, str]]]:
        """
        Extract `<file path="...">...</file>` and fenced file blocks from assistant text
        and write them through the exact same security & lock pipeline.
        Returns (written_paths, errors).
        """
        if self.compaction_lock or not assistant_text:
            return [], []

        blocks = extract_inline_file_blocks(assistant_text)
        written: List[str] = []
        errors: List[Dict[str, str]] = []

        for block in blocks:
            try:
                res = self._perform_safe_write(block.path, block.content, source=block.format_kind)
                written.append(res["path"])
                self.events.emit(
                    EventType.INLINE_FILE_WRITTEN,
                    f"[AUTO-WRITER] Wrote {res['path']} ({res['bytes_written']} bytes)",
                    data=res,
                    level="success",
                )
            except Exception as err:
                err_info = {"path": block.path, "error": str(err)}
                errors.append(err_info)
                self.events.emit(
                    EventType.LOG,
                    f"[AUTO-WRITER BLOCKED] {block.path}: {err}",
                    data=err_info,
                    level="warning",
                )

        return written, errors

    # ------------------------------------------------------------------
    # Tool Dispatcher
    # ------------------------------------------------------------------
    def execute(self, name: str, args: Dict[str, Any]) -> str:
        if self.compaction_lock:
            payload = json.dumps(
                {
                    "error": (
                        "COMPACTION_LOCK_ACTIVE: Context threshold reached. "
                        "Tools are locked until you emit the raw JSON continuation report."
                    )
                },
                ensure_ascii=False,
            )
            return payload + (f"\n{self.compaction_signal_text}" if self.compaction_signal_text else "")

        try:
            if name == "list_dir":
                target, rel_posix = self._resolve_workspace_path(args.get("path", "."), allow_root=True)
                if not target.exists():
                    return json.dumps({"error": f"Directory not found: {rel_posix}"})
                if not target.is_dir():
                    return json.dumps({"error": f"Not a directory: {rel_posix}"})
                entries = []
                for p in sorted(target.iterdir()):
                    if p.name in IGNORED_DIR_NAMES or p.name == "user_request.json" or p.name.startswith("readed_user_request_"):
                        continue
                    entries.append(f"{p.name}/" if p.is_dir() else p.name)
                return json.dumps({"path": rel_posix, "entries": entries}, ensure_ascii=False)

            if name == "read_file":
                target, rel_posix = self._resolve_workspace_path(args["path"])
                if not target.is_file():
                    return json.dumps({"error": f"File not found: {rel_posix}"})
                raw_text = target.read_text(encoding="utf-8", errors="replace")
                self.lock_manager.record_read(rel_posix)

                start_line = args.get("start_line")
                end_line = args.get("end_line")
                if start_line is not None or end_line is not None:
                    lines = raw_text.splitlines(keepends=True)
                    s_idx = max(1, int(start_line or 1)) - 1
                    e_idx = int(end_line) if end_line is not None else len(lines)
                    sliced = "".join(lines[s_idx:e_idx])
                    out = sliced
                else:
                    out = raw_text

                if len(out) > self.config.max_tool_output_chars:
                    out = (
                        out[: self.config.max_tool_output_chars]
                        + f"\n...[TRUNCATED AT {self.config.max_tool_output_chars} CHARS — use start_line/end_line]..."
                    )
                return out

            if name == "search_text":
                query = str(args.get("query", ""))
                if not query:
                    return json.dumps({"error": "Search query cannot be empty."})
                is_regex = bool(args.get("is_regex", False))
                search_root, _ = self._resolve_workspace_path(args.get("path", "."), allow_root=True)
                if not search_root.exists():
                    return json.dumps({"error": f"Search path not found: {args.get('path')}"})

                compiled = re.compile(query) if is_regex else None
                matches: List[str] = []

                for root_dir, dirnames, filenames in os.walk(search_root):
                    dirnames[:] = [d for d in sorted(dirnames) if d not in IGNORED_DIR_NAMES]
                    for fname in sorted(filenames):
                        if fname == "user_request.json" or fname.startswith("readed_user_request_"):
                            continue
                        fpath = Path(root_dir) / fname
                        try:
                            if fpath.stat().st_size > self.config.max_file_size_bytes:
                                continue
                            rel = fpath.resolve().relative_to(self.workspace_root).as_posix()
                            text = fpath.read_text(encoding="utf-8", errors="ignore")
                            for idx, line in enumerate(text.splitlines(), 1):
                                hit = bool(compiled.search(line)) if compiled else (query in line)
                                if hit:
                                    matches.append(f"{rel}:{idx}: {line.strip()[:200]}")
                                    if len(matches) >= 60:
                                        return json.dumps({"matches": matches, "truncated": True}, ensure_ascii=False)
                        except Exception:
                            continue

                return json.dumps({"matches": matches, "truncated": False}, ensure_ascii=False)

            if name == "write_file":
                res = self._perform_safe_write(args["path"], args["content"], source="write_file")
                return json.dumps(res, ensure_ascii=False)

            if name == "patch_file":
                target, rel_posix = self._resolve_workspace_path(args["path"])
                old_str = str(args.get("target_snippet", ""))
                new_str = str(args.get("replacement_snippet", ""))

                is_existing = target.is_file()
                is_placeholder = self._is_placeholder_file(target) if is_existing else False

                allowed, reason = self.lock_manager.check_write_permission(
                    rel_posix,
                    is_existing=is_existing,
                    is_placeholder=is_placeholder,
                )
                if not allowed:
                    raise PermissionError(reason)

                # Pragmatic auto-heal: creating missing file or replacing empty placeholder
                if not is_existing or is_placeholder:
                    self._validate_content_safety(new_str)
                    atomic_write_text(target, new_str)
                    self.lock_manager.mark_modified(rel_posix)
                    self.lock_manager.record_read(rel_posix)
                    if self._on_file_modified:
                        self._on_file_modified(rel_posix)
                    return json.dumps(
                        {
                            "status": "created_from_patch" if not is_existing else "overwrote_placeholder",
                            "path": rel_posix,
                            "worktree_fingerprint": self.verification_manager.compute_worktree_fingerprint(),
                        },
                        ensure_ascii=False,
                    )

                content = target.read_text(encoding="utf-8", errors="replace")
                if not old_str or old_str not in content:
                    return json.dumps(
                        {
                            "error": (
                                f"target_snippet not found in {rel_posix} (length {len(content)} chars). "
                                f"Call read_file('{rel_posix}') first, or output the complete file using "
                                f'<file path="{rel_posix}">...code...</file>.'
                            )
                        },
                        ensure_ascii=False,
                    )

                updated = content.replace(old_str, new_str, 1)
                self._validate_content_safety(updated)
                backup_name = self._backup_existing_file(target, rel_posix)
                atomic_write_text(target, updated)
                self.lock_manager.mark_modified(rel_posix)
                if self._on_file_modified:
                    self._on_file_modified(rel_posix)

                return json.dumps(
                    {
                        "status": "patched",
                        "path": rel_posix,
                        "backup": backup_name,
                        "worktree_fingerprint": self.verification_manager.compute_worktree_fingerprint(),
                    },
                    ensure_ascii=False,
                )

            if name == "list_skills":
                target, rel_posix = self._resolve_skills_path(args.get("path", "."), allow_root=True)
                if not target.is_dir():
                    return json.dumps({"error": f"Skills directory not found: {rel_posix}"})
                entries = [
                    f"{p.name}/" if p.is_dir() else p.name
                    for p in sorted(target.iterdir())
                    if not p.name.startswith(".")
                ]
                return json.dumps({"skills_path": rel_posix, "entries": entries}, ensure_ascii=False)

            if name == "read_skill":
                target, rel_posix = self._resolve_skills_path(args["path"], allow_root=False)
                if not target.is_file():
                    return json.dumps({"error": f"Skill file not found: {rel_posix}"})
                content = target.read_text(encoding="utf-8", errors="replace")
                if len(content) > self.config.max_tool_output_chars:
                    content = content[: self.config.max_tool_output_chars] + "\n...[TRUNCATED]..."
                return content

            if name == "lock_file":
                target, rel_posix = self._resolve_workspace_path(args["path"])
                if not target.is_file():
                    return json.dumps({"error": f"Cannot register lock candidate; file does not exist: {rel_posix}"})
                phase_id = self._get_active_phase_id()
                self.lock_manager.register_lock_candidate(rel_posix, phase_id)
                return json.dumps(
                    {
                        "status": "lock_candidate_registered",
                        "path": rel_posix,
                        "owner_phase_id": phase_id,
                        "note": "File will be locked atomically once phase verification passes.",
                    },
                    ensure_ascii=False,
                )

            if name == "run_checks":
                phase_id = self._get_active_phase_id()
                commands = self._get_active_verification_commands()
                suite = self.verification_manager.run_phase_checks(phase_id, commands)
                return json.dumps(suite.to_dict(), indent=2, ensure_ascii=False)

            if name == "run_terminal":
                cmd_str = str(args.get("command", "")).strip()
                allowlist = list(self.config.allowed_commands) + list(self._get_active_verification_commands())
                res = self.verification_manager.run_single_allowed_command(cmd_str, allowlist)
                return json.dumps(res.to_dict(), indent=2, ensure_ascii=False)

            if name == "complete_phase":
                if self._on_complete_phase is None:
                    return json.dumps({"error": "Phase completion handler is not attached."})
                outcome = self._on_complete_phase(args)
                return json.dumps(outcome, indent=2, ensure_ascii=False)

            return json.dumps({"error": f"Unknown tool: '{name}'"})

        except Exception as err:
            return json.dumps({"error": str(err)}, ensure_ascii=False)