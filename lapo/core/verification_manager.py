"""
Safe allowlisted command execution (shell=False), built-in unattended auto-verification
(Python/JSON/Node/TS/Go/Rust), ANSI output sanitization, SHA-256 worktree fingerprinting,
Git inspection, and language-agnostic file contract extraction.
"""

from __future__ import annotations

import hashlib
import json
import os
import py_compile
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from lapo.config import NO_PROXY_HOSTS, RuntimeConfig
from lapo.events import EventBus, EventType
from lapo.models import (
    VerificationCommandResult,
    VerificationSuiteResult,
    utc_now_iso,
)


FORBIDDEN_CONTROL_CHARS = set("\n\r\x00")
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[@-Z\\-_]")

IGNORED_WALK_DIRS = {
    ".git",
    ".orchestrator",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "dist",
    "build",
    ".pytest_cache",
    ".mypy_cache",
}

BUILTIN_SAFE_TEST_PREFIXES: List[Tuple[str, ...]] = [
    ("python", "-m", "unittest"),
    ("python3", "-m", "unittest"),
    ("python", "-m", "pytest"),
    ("python3", "-m", "pytest"),
    ("python", "-m", "py_compile"),
    ("python3", "-m", "py_compile"),
    ("pytest",),
    ("npx", "tsc", "--noEmit"),
    ("npx", "vitest", "run"),
    ("npx", "vite", "build"),
    ("npm", "test"),
    ("npm", "run", "build"),
    ("npm", "run", "test"),
    ("node", "--check"),
    ("go", "test"),
    ("go", "vet"),
    ("cargo", "check"),
    ("cargo", "test"),
]

_CONTRACT_PATTERNS: Dict[str, re.Pattern[str]] = {
    ".py": re.compile(r"^\s*(?:async\s+)?(?:def|class)\s+([A-Za-z0-9_]+[^\n:]*)", re.MULTILINE),
    ".ts": re.compile(
        r"^\s*export\s+(?:default\s+)?(?:async\s+)?(?:function|interface|type|class|const|enum)\s+([A-Za-z0-9_]+[^\n\{;]*)",
        re.MULTILINE,
    ),
    ".tsx": re.compile(
        r"^\s*export\s+(?:default\s+)?(?:async\s+)?(?:function|interface|type|class|const|enum)\s+([A-Za-z0-9_]+[^\n\{;]*)",
        re.MULTILINE,
    ),
    ".js": re.compile(
        r"^\s*export\s+(?:default\s+)?(?:async\s+)?(?:function|class|const)\s+([A-Za-z0-9_]+[^\n\{;]*)",
        re.MULTILINE,
    ),
    ".go": re.compile(r"^(?:func|type)\s+([A-Z][A-Za-z0-9_]*[^\n\{]*)", re.MULTILINE),
    ".rs": re.compile(
        r"^\s*pub\s+(?:async\s+)?(?:fn|struct|enum|trait|type)\s+([A-Za-z0-9_]+[^\n\{;]*)",
        re.MULTILINE,
    ),
}


def strip_ansi(text: str) -> str:
    """Remove ANSI terminal color/cursor escape codes from subprocess output."""
    return _ANSI_ESCAPE_RE.sub("", text or "")


def _probe_tcp(proxy_url: str, timeout: float = 1.2) -> bool:
    try:
        clean = proxy_url.split("://")[-1].strip("/")
        host, port_str = clean.split(":")
        with socket.create_connection((host, int(port_str)), timeout=timeout):
            return True
    except Exception:
        return False


def resolve_executable(bin_name: str) -> str:
    stem = Path(bin_name).stem.lower()
    if stem in ("python", "python3"):
        return sys.executable
    found = shutil.which(bin_name)
    if not found and os.name == "nt":
        found = shutil.which(f"{bin_name}.exe") or shutil.which(f"{bin_name}.cmd")
    if not found:
        raise FileNotFoundError(f"Executable '{bin_name}' not found in PATH.")
    return found


def _resolve_native_node_cli(argv: List[str], cwd: Path) -> Optional[List[str]]:
    if len(argv) >= 2 and Path(argv[0]).stem.lower() == "npx":
        tool = argv[1]
        rest = argv[2:]
        local_bins = {
            "tsc": cwd / "node_modules" / "typescript" / "bin" / "tsc",
            "vitest": cwd / "node_modules" / "vitest" / "vitest.mjs",
            "vite": cwd / "node_modules" / "vite" / "bin" / "vite.js",
        }
        if tool in local_bins and local_bins[tool].is_file():
            return [resolve_executable("node"), str(local_bins[tool])] + rest
    return None


def _strip_matching_outer_quotes(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in ('"', "'"):
        return token[1:-1]
    return token


class VerificationManager:
    """
    Runs strictly allowlisted commands AND unattended smart auto-verification
    (syntax checks + auto-detected test suites) with shell=False.
    """

    def __init__(self, config: RuntimeConfig, event_bus: Optional[EventBus] = None) -> None:
        self.config = config
        self.workspace_root = config.workspace_dir.resolve()
        self.events = event_bus or EventBus()
        self.last_suite_result: Optional[VerificationSuiteResult] = None

    def _build_subprocess_env(self) -> Dict[str, str]:
        env = os.environ.copy()
        env["NO_PROXY"] = NO_PROXY_HOSTS
        env["no_proxy"] = NO_PROXY_HOSTS
        env["NO_COLOR"] = "1"

        active_proxy: Optional[str] = None
        if self.config.primary_proxy and _probe_tcp(self.config.primary_proxy):
            active_proxy = self.config.primary_proxy
        elif self.config.fallback_proxy and _probe_tcp(self.config.fallback_proxy):
            active_proxy = self.config.fallback_proxy

        if active_proxy:
            env["HTTP_PROXY"] = active_proxy
            env["HTTPS_PROXY"] = active_proxy
            env["http_proxy"] = active_proxy
            env["https_proxy"] = active_proxy
        return env

    @staticmethod
    def _parse_safe_tokens(cmd_str: str) -> List[str]:
        cleaned = (cmd_str or "").strip()
        if not cleaned:
            raise PermissionError("Empty command rejected.")
        if any(ch in cleaned for ch in FORBIDDEN_CONTROL_CHARS):
            raise PermissionError("Control characters (newline, carriage return, NUL) are forbidden in commands.")

        is_posix = os.name != "nt"
        tokens = shlex.split(cleaned, posix=is_posix)
        if not is_posix:
            tokens = [_strip_matching_outer_quotes(t) for t in tokens]
        if not tokens:
            raise PermissionError("Empty command after parsing.")
        return tokens

    @staticmethod
    def _normalize_command(tokens: List[str]) -> Tuple[str, ...]:
        exe = Path(tokens[0]).stem.lower()
        return (exe,) + tuple(tokens[1:])

    def _is_builtin_safe_test_command(self, norm_tokens: Tuple[str, ...]) -> bool:
        if not self.config.unattended_auto_verify:
            return False
        for prefix in BUILTIN_SAFE_TEST_PREFIXES:
            if len(norm_tokens) >= len(prefix) and norm_tokens[: len(prefix)] == prefix:
                for arg in norm_tokens[len(prefix) :]:
                    if ".." in arg.replace("\\", "/").split("/"):
                        return False
                return True
        return False

    def _run_tokens(self, tokens: List[str], timeout_s: int = 120) -> subprocess.CompletedProcess[str]:
        native = _resolve_native_node_cli(tokens, self.workspace_root)
        if native is not None:
            full_argv = native
        else:
            full_argv = [resolve_executable(tokens[0])] + tokens[1:]

        return subprocess.run(
            full_argv,
            cwd=self.workspace_root,
            env=self._build_subprocess_env(),
            shell=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            encoding="utf-8",
            errors="replace",
        )

    def run_single_allowed_command(
        self,
        cmd_str: str,
        allowlist: List[str],
        timeout_s: int = 120,
    ) -> VerificationCommandResult:
        tokens = self._parse_safe_tokens(cmd_str)
        norm_target = self._normalize_command(tokens)

        allowed_norms: List[Tuple[str, ...]] = []
        for entry in allowlist:
            try:
                allowed_norms.append(self._normalize_command(self._parse_safe_tokens(entry)))
            except Exception:
                continue

        if norm_target not in allowed_norms and not self._is_builtin_safe_test_command(norm_target):
            raise PermissionError(
                f"Command '{cmd_str}' is NOT in the approved verification allowlist. "
                f"User allowlist: {allowlist} | Built-in test runners (when unattended auto-verify is on): "
                "python -m unittest, python -m py_compile, pytest, npx tsc --noEmit, npx vitest run, npm test, go test, cargo test."
            )

        start = time.time()
        try:
            proc = self._run_tokens(tokens, timeout_s=timeout_s)
            dur = round(time.time() - start, 3)
            clean_stdout = strip_ansi(proc.stdout or "")[-4000:]
            clean_stderr = strip_ansi(proc.stderr or "")[-4000:]
            return VerificationCommandResult(
                command=cmd_str,
                exit_code=proc.returncode,
                passed=(proc.returncode == 0),
                stdout=clean_stdout,
                stderr=clean_stderr,
                duration_s=dur,
            )
        except Exception as err:
            dur = round(time.time() - start, 3)
            return VerificationCommandResult(
                command=cmd_str,
                exit_code=-1,
                passed=False,
                stdout="",
                stderr=strip_ansi(str(err)),
                duration_s=dur,
            )

    def _has_python_test_files(self) -> bool:
        for root_dir, dirnames, filenames in os.walk(self.workspace_root):
            dirnames[:] = [d for d in sorted(dirnames) if d not in IGNORED_WALK_DIRS]
            for fname in filenames:
                if (fname.startswith("test_") or fname.endswith("_test.py")) and fname.endswith(".py"):
                    return True
        return False

    def run_auto_verification(
        self,
        phase_id: int,
        modified_files: Optional[List[str]] = None,
    ) -> VerificationSuiteResult:
        fp = self.compute_worktree_fingerprint()
        results: List[VerificationCommandResult] = []
        all_passed = True

        targets: List[Path] = []
        if modified_files:
            for rel in modified_files:
                p = (self.workspace_root / rel).resolve()
                if p.is_file():
                    targets.append(p)
        if not targets:
            for root_dir, dirnames, filenames in os.walk(self.workspace_root):
                dirnames[:] = [d for d in sorted(dirnames) if d not in IGNORED_WALK_DIRS]
                for fname in sorted(filenames):
                    if fname == "user_request.json" or fname.startswith("readed_user_request_"):
                        continue
                    targets.append(Path(root_dir) / fname)

        py_files = [p for p in targets if p.suffix.lower() == ".py"]
        for py_file in py_files:
            rel = py_file.relative_to(self.workspace_root).as_posix()
            t0 = time.time()
            try:
                py_compile.compile(str(py_file), doraise=True)
                results.append(
                    VerificationCommandResult(
                        command=f"auto:py_compile {rel}",
                        exit_code=0,
                        passed=True,
                        stdout=f"Syntax OK: {rel}",
                        stderr="",
                        duration_s=round(time.time() - t0, 3),
                    )
                )
            except py_compile.PyCompileError as err:
                all_passed = False
                results.append(
                    VerificationCommandResult(
                        command=f"auto:py_compile {rel}",
                        exit_code=1,
                        passed=False,
                        stdout="",
                        stderr=strip_ansi(str(err)),
                        duration_s=round(time.time() - t0, 3),
                    )
                )

        json_files = [p for p in targets if p.suffix.lower() == ".json"]
        for jf in json_files:
            rel = jf.relative_to(self.workspace_root).as_posix()
            t0 = time.time()
            try:
                json.loads(jf.read_text(encoding="utf-8"))
                results.append(
                    VerificationCommandResult(
                        command=f"auto:json_validate {rel}",
                        exit_code=0,
                        passed=True,
                        stdout=f"Valid JSON: {rel}",
                        stderr="",
                        duration_s=round(time.time() - t0, 3),
                    )
                )
            except Exception as err:
                all_passed = False
                results.append(
                    VerificationCommandResult(
                        command=f"auto:json_validate {rel}",
                        exit_code=1,
                        passed=False,
                        stdout="",
                        stderr=strip_ansi(str(err)),
                        duration_s=round(time.time() - t0, 3),
                    )
                )

        if self._has_python_test_files():
            cmd = "python -m unittest discover -v"
            self.events.emit(EventType.LOG, f"[AUTO-VERIFY Phase {phase_id}] Running detected Python tests: {cmd}")
            res = self.run_single_allowed_command(cmd, [cmd], timeout_s=120)
            results.append(res)
            if not res.passed:
                all_passed = False

        if (self.workspace_root / "tsconfig.json").is_file() and (
            self.workspace_root / "node_modules" / "typescript" / "bin" / "tsc"
        ).is_file():
            cmd = "npx tsc --noEmit"
            self.events.emit(EventType.LOG, f"[AUTO-VERIFY Phase {phase_id}] Running: {cmd}")
            res = self.run_single_allowed_command(cmd, [cmd], timeout_s=120)
            results.append(res)
            if not res.passed:
                all_passed = False

        if (self.workspace_root / "node_modules" / "vitest" / "vitest.mjs").is_file():
            cmd = "npx vitest run"
            self.events.emit(EventType.LOG, f"[AUTO-VERIFY Phase {phase_id}] Running: {cmd}")
            res = self.run_single_allowed_command(cmd, [cmd], timeout_s=120)
            results.append(res)
            if not res.passed:
                all_passed = False

        if (self.workspace_root / "go.mod").is_file() and shutil.which("go"):
            cmd = "go test ./..."
            self.events.emit(EventType.LOG, f"[AUTO-VERIFY Phase {phase_id}] Running: {cmd}")
            res = self.run_single_allowed_command(cmd, [cmd], timeout_s=120)
            results.append(res)
            if not res.passed:
                all_passed = False

        if (self.workspace_root / "Cargo.toml").is_file() and shutil.which("cargo"):
            cmd = "cargo check"
            self.events.emit(EventType.LOG, f"[AUTO-VERIFY Phase {phase_id}] Running: {cmd}")
            res = self.run_single_allowed_command(cmd, [cmd], timeout_s=180)
            results.append(res)
            if not res.passed:
                all_passed = False

        if not results:
            results.append(
                VerificationCommandResult(
                    command="auto:workspace_sanity",
                    exit_code=0,
                    passed=True,
                    stdout=f"Verified {len(targets)} workspace file(s) exist and are UTF-8 readable.",
                    stderr="",
                    duration_s=0.0,
                )
            )

        suite = VerificationSuiteResult(
            phase_id=phase_id,
            status="passed" if all_passed else "failed",
            worktree_fingerprint=fp,
            command_results=results,
            manual_approved=False,
        )
        self.last_suite_result = suite
        self.events.emit(
            EventType.VERIFICATION_RESULT,
            f"[AUTO-VERIFY] Phase {phase_id} {'PASSED' if all_passed else 'FAILED'} ({len(results)} check(s)).",
            data=suite.to_dict(),
            level="success" if all_passed else "error",
        )
        return suite

    def run_phase_checks(
        self,
        phase_id: int,
        commands: List[str],
        modified_files: Optional[List[str]] = None,
    ) -> VerificationSuiteResult:
        fp = self.compute_worktree_fingerprint()
        clean_cmds = [c.strip() for c in commands if c and c.strip()]

        if not clean_cmds:
            if self.config.unattended_auto_verify:
                return self.run_auto_verification(phase_id, modified_files=modified_files)

            suite = VerificationSuiteResult(
                phase_id=phase_id,
                status="manual_required",
                worktree_fingerprint=fp,
                command_results=[],
                manual_approved=False,
            )
            self.last_suite_result = suite
            self.events.emit(
                EventType.VERIFICATION_RESULT,
                f"Phase {phase_id}: No automated verification commands configured (manual approval required).",
                data=suite.to_dict(),
                level="warning",
            )
            return suite

        results: List[VerificationCommandResult] = []
        all_passed = True
        allowlist = list(self.config.allowed_commands) + clean_cmds

        for cmd in clean_cmds:
            self.events.emit(EventType.LOG, f"[VERIFY Phase {phase_id}] Running: {cmd}")
            try:
                res = self.run_single_allowed_command(cmd, allowlist)
            except Exception as err:
                res = VerificationCommandResult(
                    command=cmd,
                    exit_code=-1,
                    passed=False,
                    stdout="",
                    stderr=strip_ansi(str(err)),
                    duration_s=0.0,
                )
            results.append(res)
            if not res.passed:
                all_passed = False
                err_preview = strip_ansi(res.stderr or res.stdout or f"exit_code={res.exit_code}").strip()[:300]
                self.events.emit(
                    EventType.LOG,
                    f"[VERIFY FAILED Phase {phase_id}] `{cmd}` -> {err_preview}",
                    level="error",
                )

        suite = VerificationSuiteResult(
            phase_id=phase_id,
            status="passed" if all_passed else "failed",
            worktree_fingerprint=fp,
            command_results=results,
            manual_approved=False,
        )
        self.last_suite_result = suite
        self.events.emit(
            EventType.VERIFICATION_RESULT,
            f"Phase {phase_id} verification {'PASSED' if all_passed else 'FAILED'} ({len(results)} command(s)).",
            data=suite.to_dict(),
            level="success" if all_passed else "error",
        )
        return suite

    def compute_worktree_fingerprint(self) -> str:
        hasher = hashlib.sha256()
        collected: List[Path] = []

        if self.workspace_root.is_dir():
            for root_dir, dirnames, filenames in os.walk(self.workspace_root):
                dirnames[:] = [d for d in sorted(dirnames) if d not in IGNORED_WALK_DIRS]
                for fname in sorted(filenames):
                    if (
                        fname.endswith(".tmp")
                        or fname == "user_request.json"
                        or fname.startswith("readed_user_request_")
                    ):
                        continue
                    collected.append(Path(root_dir) / fname)

        for p in sorted(collected):
            try:
                rel = p.resolve().relative_to(self.workspace_root).as_posix()
                hasher.update(rel.encode("utf-8"))
                hasher.update(b"\0")
                hasher.update(p.read_bytes())
                hasher.update(b"\0")
            except Exception:
                continue

        return f"sha256:{hasher.hexdigest()}"

    def get_git_state(self) -> Dict[str, Any]:
        fp = self.compute_worktree_fingerprint()
        info: Dict[str, Any] = {
            "is_git_repo": False,
            "head_commit": "none",
            "branch": "none",
            "staged_files": [],
            "unstaged_files": [],
            "untracked_files": [],
            "worktree_fingerprint": fp,
        }

        if not (self.workspace_root / ".git").exists():
            return info

        try:
            git_exe = resolve_executable("git")
            info["is_git_repo"] = True

            head_proc = subprocess.run(
                [git_exe, "rev-parse", "--short", "HEAD"],
                cwd=self.workspace_root,
                shell=False,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if head_proc.returncode == 0 and head_proc.stdout.strip():
                info["head_commit"] = head_proc.stdout.strip()

            br_proc = subprocess.run(
                [git_exe, "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=self.workspace_root,
                shell=False,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if br_proc.returncode == 0 and br_proc.stdout.strip():
                info["branch"] = br_proc.stdout.strip()

            st_proc = subprocess.run(
                [git_exe, "status", "--porcelain=v1", "-z"],
                cwd=self.workspace_root,
                shell=False,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=10,
            )
            if st_proc.returncode == 0 and st_proc.stdout:
                entries = st_proc.stdout.decode("utf-8", errors="replace").split("\0")
                for entry in entries:
                    if len(entry) < 4:
                        continue
                    xy, fpath = entry[:2], entry[3:]
                    if fpath.startswith(".orchestrator/") or fpath == "user_request.json":
                        continue
                    if xy == "??":
                        info["untracked_files"].append(fpath)
                    else:
                        if xy[0] in ("M", "A", "R", "C"):
                            info["staged_files"].append(fpath)
                        if xy[1] == "M":
                            info["unstaged_files"].append(fpath)
        except Exception:
            pass

        return info

    def extract_file_contracts(self, max_files: int = 80) -> List[Dict[str, Any]]:
        contracts: List[Dict[str, Any]] = []
        if not self.workspace_root.is_dir():
            return contracts

        for root_dir, dirnames, filenames in os.walk(self.workspace_root):
            dirnames[:] = [d for d in sorted(dirnames) if d not in IGNORED_WALK_DIRS]
            for fname in sorted(filenames):
                if (
                    fname.endswith(".tmp")
                    or fname == "user_request.json"
                    or fname.startswith("readed_user_request_")
                ):
                    continue
                fpath = Path(root_dir) / fname
                try:
                    raw = fpath.read_bytes()
                    rel = fpath.resolve().relative_to(self.workspace_root).as_posix()
                    f_hash = hashlib.sha256(raw).hexdigest()[:16]
                    pattern = _CONTRACT_PATTERNS.get(fpath.suffix.lower())
                    symbols: List[str] = []
                    if pattern and len(raw) <= 512 * 1024:
                        text = raw.decode("utf-8", errors="ignore")
                        symbols = [m.group(0).strip()[:120] for m in pattern.finditer(text)][:25]
                    contracts.append(
                        {
                            "path": rel,
                            "sha256": f_hash,
                            "size": len(raw),
                            "symbols": symbols,
                        }
                    )
                    if len(contracts) >= max_files:
                        return contracts
                except Exception:
                    continue

        return contracts

    def build_machine_verified_snapshot(self) -> Dict[str, Any]:
        git_state = self.get_git_state()
        current_fp = git_state["worktree_fingerprint"]
        last_v = self.last_suite_result.to_dict() if self.last_suite_result else None
        if last_v is not None:
            last_v["is_stale"] = last_v.get("worktree_fingerprint") != current_fp

        return {
            "timestamp": utc_now_iso(),
            "git": git_state,
            "last_verification": last_v,
            "file_contracts": self.extract_file_contracts(),
        }