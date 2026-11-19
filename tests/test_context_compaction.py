"""Unit tests for ContextManager scaling, K/M parsing, and CompactionManager fallback & phase lock."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from lapo.adapters.strata_adapter import StrataAdapter
from lapo.config import RuntimeConfig, parse_context_size
from lapo.core.compaction_manager import CompactionManager, parse_continuation_report
from lapo.core.context_manager import ContextManager
from lapo.core.file_lock_manager import FileLockManager
from lapo.core.live_request_manager import LiveRequestManager
from lapo.core.phase_manager import PhaseManager
from lapo.core.recovery_manager import RecoveryManager
from lapo.core.usage_ledger import UsageLedger
from lapo.core.verification_manager import VerificationManager
from lapo.tools.workspace_tools import WorkspaceTools


class TestContextAndCompaction(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        self.cfg = RuntimeConfig(workspace_dir=self.ws, context_setting="64K", compaction_threshold_pct=0.75)
        self.cfg.ensure_orchestrator_dirs()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_context_size_parsing(self) -> None:
        self.assertEqual(parse_context_size("auto"), "auto")
        self.assertEqual(parse_context_size("32K"), 32768)
        self.assertEqual(parse_context_size("256k"), 262144)
        self.assertEqual(parse_context_size("1M"), 1048576)
        self.assertEqual(parse_context_size(16384), 16384)
        with self.assertRaises(ValueError):
            parse_context_size("512")
        with self.assertRaises(ValueError):
            parse_context_size("invalid")

    def test_context_manager_thresholds_and_ceiling_cap(self) -> None:
        adapter = StrataAdapter(self.cfg)
        # Server reports smaller actual capacity (32K) than manual ceiling (64K) -> cap to 32K
        cm = ContextManager(self.cfg, adapter, detected_capacity=32768)
        self.assertEqual(cm.effective_ceiling, 32768)
        self.assertEqual(cm.compaction_trigger, int(32768 * 0.75))
        self.assertFalse(cm.should_trigger_compaction(10000))
        self.assertTrue(cm.should_trigger_compaction(25000))

    def test_compaction_valid_json_and_deterministic_fallback_preserve_phase(self) -> None:
        pm = PhaseManager(self.cfg)
        pm.create_plan(
            [
                {"name": "Core", "prompt": "Build core engine"},
                {"name": "UI", "prompt": "Build UI"},
            ]
        )
        lm = FileLockManager(self.cfg)
        vm = VerificationManager(self.cfg)
        lrm = LiveRequestManager(self.cfg)
        rm = RecoveryManager(self.cfg)
        ul = UsageLedger(self.cfg)
        tools = WorkspaceTools(self.cfg, lm, vm)

        cm = CompactionManager(pm, lm, vm, lrm, rm, ul)

        # 1. Valid JSON wrapped in <think> + fences; model even tries to claim Phase 2, must be forced to Phase 1!
        model_reply = (
            "<think>summarizing state</think>\n```json\n"
            + json.dumps(
                {
                    "current_objective": "Finish Core",
                    "current_phase_id": 2,
                    "current_phase_name": "UI",
                    "design_decisions_locked": ["Use pure stdlib"],
                    "active_assumptions": [],
                    "completed_work": ["Created stub"],
                    "files_created_or_modified": ["core.py"],
                    "unfinished_work": ["Add unit tests"],
                    "known_failures_and_blockers": [],
                    "next_exact_action": "Write tests",
                }
            )
            + "\n```"
        )
        fresh_msgs, state, used_fb = cm.finalize_compaction(model_reply, tools, "SYS", previous_tokens=50000)
        self.assertFalse(used_fb)
        self.assertFalse(tools.compaction_lock)
        self.assertEqual(len(fresh_msgs), 2)
        self.assertEqual(state["active_phase_id"], 1)
        self.assertEqual(state["continuation_report"]["current_phase_id"], 1)
        self.assertEqual(state["continuation_report"]["current_phase_name"], "Core")

        # 2. Invalid/corrupted JSON must trigger deterministic fallback without crashing
        fresh_msgs2, state2, used_fb2 = cm.finalize_compaction("NOT VALID JSON AT ALL", tools, "SYS", previous_tokens=50000)
        self.assertTrue(used_fb2)
        self.assertFalse(tools.compaction_lock)
        self.assertEqual(len(fresh_msgs2), 2)
        self.assertEqual(state2["active_phase_id"], 1)
        self.assertTrue(self.cfg.state_file.is_file())
        self.assertTrue(self.cfg.journal_file.is_file())

    def test_parse_continuation_report_normalizes_scalar_fields(self) -> None:
        pm = PhaseManager(self.cfg)
        pm.create_plan([{"name": "P1", "prompt": "Work"}])
        raw = json.dumps(
            {
                "current_objective": "Do work",
                "design_decisions_locked": "Single string instead of list",
                "active_assumptions": None,
                "completed_work": ["Step A"],
                "files_created_or_modified": [],
                "unfinished_work": "Step B",
                "known_failures_and_blockers": [],
                "next_exact_action": "Do Step B",
            }
        )
        parsed = parse_continuation_report(raw, pm.get_active_phase())
        self.assertEqual(parsed["design_decisions_locked"], ["Single string instead of list"])
        self.assertEqual(parsed["unfinished_work"], ["Step B"])


if __name__ == "__main__":
    unittest.main()