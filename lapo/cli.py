"""
Headless-capable Terminal CLI (`lapo`) sharing 100% of the core orchestration engine
with the Desktop GUI. Works on Windows PowerShell/CMD and Linux servers without display.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from lapo import __version__
from lapo.adapters.strata_adapter import StrataAdapter
from lapo.config import (
    DEFAULT_API_BASE_URL,
    DEFAULT_API_KEY,
    SUPPORTED_TOOL_PROTOCOLS,
    RuntimeConfig,
)
from lapo.core.file_lock_manager import FileLockManager
from lapo.core.live_request_manager import publish_live_request_file
from lapo.core.orchestrator import ProjectOrchestrator
from lapo.core.recovery_manager import RecoveryManager
from lapo.events import EventBus, EventType, OrchestratorEvent
from lapo.models import PhaseDefinition, VerificationSuiteResult, atomic_write_json, read_json_safe


def _cli_event_printer(evt: OrchestratorEvent) -> None:
    if evt.type in (EventType.THINKING_DELTA, EventType.CONTENT_DELTA):
        sys.stdout.write(evt.message)
        sys.stdout.flush()
    else:
        print(f"\n[{evt.type.value.upper()}] {evt.message}", flush=True)


def _interactive_phase_wizard() -> List[Dict[str, Any]]:
    print("\n=== LAPO Interactive Phase Builder ===")
    phases: List[Dict[str, Any]] = []
    idx = 1

    while True:
        print(f"\n--- Phase {idx} ---")
        name = input(f"Phase {idx} name [Phase {idx}]: ").strip() or f"Phase {idx}"

        print("Enter Phase prompt (type a single line with `END` to finish this prompt):")
        prompt_lines: List[str] = []
        while True:
            line = input()
            if line.strip() == "END":
                break
            prompt_lines.append(line)
        prompt = "\n".join(prompt_lines).strip()
        if not prompt:
            print("Prompt cannot be empty. Please re-enter this phase.")
            continue

        cmds_raw = input("Verification commands (optional, separate multiple with ';;'): ").strip()
        cmds = [c.strip() for c in cmds_raw.split(";;") if c.strip()] if cmds_raw else []

        phases.append(
            {
                "name": name,
                "prompt": prompt,
                "verification_commands": cmds,
            }
        )

        while True:
            choice = input("\n[A] Add another phase | [F] Finish phases input: ").strip().lower()
            if choice in ("a", "add"):
                idx += 1
                break
            if choice in ("f", "finish"):
                print(f"\nLocked in {len(phases)} phase(s).")
                return phases
            print("Please enter 'A' or 'F'.")


def _cli_manual_approval(phase: PhaseDefinition, suite: VerificationSuiteResult) -> Tuple[bool, str]:
    print(f"\n================ MANUAL APPROVAL REQUIRED (Phase {phase.id}: {phase.name}) ================")
    print(f"Modified files: {phase.created_or_modified_files}")
    ans = input("Approve and lock this phase? [y/N]: ").strip().lower()
    if ans in ("y", "yes"):
        return True, "Approved via CLI"
    note = input("Enter revision feedback for the model: ").strip()
    return False, note or "User requested revisions."


def _cli_final_review(report: Dict[str, Any], md_text: str) -> Tuple[bool, bool]:
    print("\n" + "=" * 72)
    print(md_text)
    print("=" * 72)
    approved = input("Do you approve the final project outputs? [y/N]: ").strip().lower() in ("y", "yes")
    if not approved:
        return False, False
    unload = input("Send UNLOAD request to Strata/model server now? [y/N]: ").strip().lower() in ("y", "yes")
    return True, unload


def _load_config_from_args(args: argparse.Namespace) -> Tuple[RuntimeConfig, Optional[List[Dict[str, Any]]]]:
    file_data: Dict[str, Any] = {}
    phases: Optional[List[Dict[str, Any]]] = None

    if getattr(args, "config", None):
        cfg_path = Path(args.config).expanduser().resolve()
        raw = read_json_safe(cfg_path)
        if not isinstance(raw, dict):
            raise RuntimeError(f"Could not read JSON config file: {cfg_path}")
        file_data = dict(raw)
        if isinstance(raw.get("phases"), list):
            phases = raw["phases"]

    if getattr(args, "phases_file", None):
        p_path = Path(args.phases_file).expanduser().resolve()
        raw_p = read_json_safe(p_path)
        if isinstance(raw_p, list):
            phases = raw_p
        elif isinstance(raw_p, dict) and isinstance(raw_p.get("phases"), list):
            phases = raw_p["phases"]
        else:
            raise RuntimeError(f"Invalid phases JSON file: {p_path}")

    if getattr(args, "workspace", None):
        file_data["workspace_dir"] = args.workspace
    if getattr(args, "skills", None):
        file_data["skills_dir"] = args.skills
    if getattr(args, "url", None):
        file_data["api_base_url"] = args.url
    if getattr(args, "api_key", None):
        file_data["api_key"] = args.api_key
    if getattr(args, "model", None):
        file_data["model_name"] = args.model
    if getattr(args, "context", None):
        file_data["context_setting"] = args.context
    if getattr(args, "compaction_pct", None) is not None:
        file_data["compaction_threshold_pct"] = args.compaction_pct
    if getattr(args, "max_gen_tokens", None) is not None:
        file_data["max_gen_tokens"] = args.max_gen_tokens
    if getattr(args, "tool_protocol", None):
        file_data["tool_protocol"] = args.tool_protocol
    if getattr(args, "manual_approval", False):
        file_data["unattended_auto_verify"] = False

    cfg = RuntimeConfig.from_dict(file_data)
    return cfg, phases


def cmd_models(args: argparse.Namespace) -> int:
    cfg = RuntimeConfig(
        workspace_dir=Path.cwd(),
        api_base_url=args.url,
        api_key=args.api_key,
    )
    adapter = StrataAdapter(cfg)
    models, err = adapter.discover_models()
    if err:
        print(f"[WARNING] API query issue: {err}")
    if not models:
        print("No models discovered.")
        return 1 if err else 0

    print(f"Discovered {len(models)} model(s) for {cfg.api_base_url}:")
    for m in models:
        print(f"  - {m.display_label}")
    return 0


def cmd_init_config(args: argparse.Namespace) -> int:
    target_dir = Path(args.directory).expanduser().resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = target_dir / "project.json"
    sample = {
        "workspace_dir": str(target_dir / "workspace"),
        "skills_dir": None,
        "api_base_url": DEFAULT_API_BASE_URL,
        "model_name": "",
        "context_setting": "auto",
        "compaction_threshold_pct": 0.75,
        "max_gen_tokens": 32768,
        "unattended_auto_verify": True,
        "tool_protocol": "auto",
        "allowed_commands": [],
        "phases": [
            {
                "name": "Phase 1: Architecture",
                "prompt": "Define core interfaces, project layout, and automated unit tests.",
                "verification_commands": [],
            },
            {
                "name": "Phase 2: Implementation & Validation",
                "prompt": "Implement core modules, run automated tests, and provide instructions to run the final result.",
                "verification_commands": [],
            },
        ],
    }
    atomic_write_json(cfg_path, sample)
    print(f"Initialized example configuration at: {cfg_path}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = RuntimeConfig(workspace_dir=Path(args.workspace))
    recovery = RecoveryManager(cfg)
    insp = recovery.inspect_workspace()
    lock_mgr = FileLockManager(cfg)

    print(f"Workspace: {cfg.workspace_dir}")
    print(f"Existing session: {insp.has_existing_session} | Resumable: {insp.can_resume}")
    if insp.plan_id:
        print(f"Plan ID: {insp.plan_id}")
        print(f"Active Phase: {insp.active_phase_id} ({insp.active_phase_name})")
        print(f"Completed: {insp.completed_phases}/{insp.total_phases}")
        print(f"Journal Messages: {insp.journal_message_count}")
    print(f"Locked Files: {len(lock_mgr.list_locks())} | Repair Queue: {len(lock_mgr.list_repair_queue())}")
    if insp.lock_integrity_issues:
        print("Integrity Issues:")
        for issue in insp.lock_integrity_issues:
            print(f"  - {issue}")
    if insp.reason:
        print(f"Note: {insp.reason}")
    return 0


def cmd_request(args: argparse.Namespace) -> int:
    req = publish_live_request_file(
        workspace_dir=Path(args.workspace),
        prompt=args.prompt,
        phase=args.phase,
    )
    print(f"Published live request '{req.request_id}' targeting phase '{req.phase}' -> user_request.json")
    return 0


def cmd_unlock(args: argparse.Namespace) -> int:
    cfg = RuntimeConfig(workspace_dir=Path(args.workspace))
    lock_mgr = FileLockManager(cfg)
    obligation = lock_mgr.unlock_for_repair(args.file, reason=args.reason)
    print(
        f"Unlocked '{obligation.path}' (owner Phase {obligation.owner_phase_id}). "
        "It has been added to repair_queue.json and will be re-verified before phase closure."
    )
    return 0


def cmd_run_or_resume(args: argparse.Namespace, is_resume: bool) -> int:
    cfg, phases = _load_config_from_args(args)
    bus = EventBus()
    bus.subscribe(_cli_event_printer)

    orch = ProjectOrchestrator(
        config=cfg,
        event_bus=bus,
        approval_callback=_cli_manual_approval if not getattr(args, "non_interactive", False) else None,
        final_review_callback=_cli_final_review if not getattr(args, "non_interactive", False) else None,
    )

    if getattr(args, "smoke_test", False):
        res = orch.run_smoke_test()
        print("\n=== REAL-SERVER SMOKE TEST PASSED ===")
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0

    def _sig_handler(_signum: int, _frame: Any) -> None:
        print("\n[CLI] Interrupt received; saving state cleanly...")
        orch.stop()

    signal.signal(signal.SIGINT, _sig_handler)

    if not is_resume:
        if getattr(args, "interactive_phases", False) or not phases:
            phases = _interactive_phase_wizard()
        recovery = RecoveryManager(cfg)
        insp = recovery.inspect_workspace()
        if insp.has_existing_session:
            if getattr(args, "fresh", False):
                archived = recovery.archive_previous_session()
                print(f"[CLI] Archived previous session to {archived}")
            elif not getattr(args, "non_interactive", False):
                ans = input(
                    f"\nExisting session detected (Phase {insp.active_phase_id}/{insp.total_phases}). "
                    "[R] Resume | [F] Archive & Start Fresh | [Q] Quit: "
                ).strip().lower()
                if ans in ("r", "resume"):
                    is_resume = True
                elif ans in ("f", "fresh"):
                    archived = recovery.archive_previous_session()
                    print(f"[CLI] Archived previous session to {archived}")
                else:
                    return 0

    outcome = orch.run(raw_phases=None if is_resume else phases, resume=is_resume)
    print(f"\nFinal JSON Report: {outcome['report_json_path']}")
    print(f"Final Markdown Report: {outcome['report_md_path']}")
    return 0 if outcome.get("finished") else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lapo",
        description="Local AI Project Orchestrator (LAPO) — Multi-phase supervisory engine for local LLMs.",
    )
    parser.add_argument("--version", action="version", version=f"lapo {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_models = sub.add_parser("models", help="List installed Strata models and active API models.")
    p_models.add_argument("--url", default=DEFAULT_API_BASE_URL, help="API Base URL (e.g. http://127.0.0.1:5678/v1)")
    p_models.add_argument("--api-key", default=DEFAULT_API_KEY)

    p_init = sub.add_parser("init-config", help="Generate a starter project.json template.")
    p_init.add_argument("--directory", required=True, help="Target directory for project.json")

    p_status = sub.add_parser("status", help="Inspect workspace orchestration state and lock integrity.")
    p_status.add_argument("--workspace", required=True)

    p_req = sub.add_parser("request", help="Submit a live mid-flight instruction (user_request.json).")
    p_req.add_argument("--workspace", required=True)
    p_req.add_argument("--phase", default="active", help="Target phase ID (e.g. '2') or 'active'")
    p_req.add_argument("--prompt", required=True, help="Instruction text")

    p_unlock = sub.add_parser("unlock", help="Authorize controlled repair by unlocking a finalized file.")
    p_unlock.add_argument("--workspace", required=True)
    p_unlock.add_argument("--file", required=True, help="Relative path inside workspace")
    p_unlock.add_argument("--reason", default="User authorized repair via CLI")

    def _add_exec_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--config", help="Path to project.json")
        p.add_argument("--workspace", help="Workspace directory path")
        p.add_argument("--skills", help="Optional read-only Skills directory path")
        p.add_argument("--url", help="API Base URL (e.g. http://127.0.0.1:47831/v1)")
        p.add_argument("--api-key", help="API Key")
        p.add_argument("--model", help="Target active model ID")
        p.add_argument("--context", help="Context ceiling: auto, 32K, 128K, 256K, 512K, 1M, or integer")
        p.add_argument("--compaction-pct", type=float, help="Compaction threshold ratio (e.g. 0.75)")
        p.add_argument("--max-gen-tokens", type=int, help="Max generation tokens per turn")
        p.add_argument("--tool-protocol", choices=SUPPORTED_TOOL_PROTOCOLS, help="Tool protocol mode")
        p.add_argument(
            "--manual-approval",
            action="store_true",
            help="Require manual approval between phases when no verification commands are configured",
        )
        p.add_argument("--non-interactive", action="store_true", help="Disable interactive CLI prompts")

    p_run = sub.add_parser("run", help="Start a new multi-phase orchestrated project run.")
    _add_exec_args(p_run)
    p_run.add_argument("--phases-file", help="Path to JSON file containing phases list")
    p_run.add_argument("--interactive-phases", action="store_true", help="Launch interactive phase builder")
    p_run.add_argument("--fresh", action="store_true", help="Archive any existing .orchestrator session")
    p_run.add_argument("--smoke-test", action="store_true", help="Run a real-server smoke test and exit")

    p_resume = sub.add_parser("resume", help="Resume an interrupted session cleanly.")
    _add_exec_args(p_resume)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "models":
        return cmd_models(args)
    if args.command == "init-config":
        return cmd_init_config(args)
    if args.command == "status":
        return cmd_status(args)
    if args.command == "request":
        return cmd_request(args)
    if args.command == "unlock":
        return cmd_unlock(args)
    if args.command == "run":
        return cmd_run_or_resume(args, is_resume=False)
    if args.command == "resume":
        return cmd_run_or_resume(args, is_resume=True)

    return 1


if __name__ == "__main__":
    raise SystemExit(main())