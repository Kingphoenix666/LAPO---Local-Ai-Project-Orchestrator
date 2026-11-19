"""Unit tests for RecoveryManager (plan identity & crash resume) and UsageLedger (no double-counting)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lapo.config import RuntimeConfig
from lapo.core.phase_manager import PhaseManager
from lapo.core.recovery_manager import RecoveryManager
from lapo.core.usage_ledger import UsageLedger


class TestRecoveryAndLedger(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        self.cfg = RuntimeConfig(workspace_dir=self.ws, model_name="test-model")
        self.cfg.ensure_orchestrator_dirs()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_recovery_plan_identity_and_journal_trim(self) -> None:
        pm = PhaseManager(self.cfg)
        plan = pm.create_plan([{"name": "P1", "prompt": "Build A"}, {"name": "P2", "prompt": "Build B"}])

        rm = RecoveryManager(self.cfg)
        # Save journal with a trailing empty assistant stub (crash artifact)
        rm.save_journal(
            [
                {"role": "system", "content": "SYS"},
                {"role": "user", "content": "BOOT"},
                {"role": "assistant", "content": ""},
            ]
        )
        loaded_journal = rm.load_journal()
        assert loaded_journal is not None
        self.assertEqual(len(loaded_journal), 2)

        # Matching plan_id is resumable
        insp_ok = rm.inspect_workspace(expected_plan_id=plan.plan_id)
        self.assertTrue(insp_ok.can_resume)

        # Mismatched plan_id MUST NOT resume blindly
        insp_mismatch = rm.inspect_workspace(expected_plan_id="sha256:different")
        self.assertTrue(insp_mismatch.has_existing_session)
        self.assertFalse(insp_mismatch.can_resume)

        # Archive moves `.orchestrator/` cleanly
        archived = rm.archive_previous_session()
        assert archived is not None
        self.assertTrue(archived.is_dir())
        self.assertFalse(self.cfg.orchestrator_dir.exists())

    def test_usage_ledger_does_not_double_count_reasoning_tokens(self) -> None:
        pm = PhaseManager(self.cfg)
        plan = pm.create_plan([{"name": "P1", "prompt": "Task"}])

        ledger = UsageLedger(self.cfg)
        ledger.mark_started(detected_ctx=32768)

        # Case 1: OpenAI/Strata style where reasoning_tokens is INSIDE completion_tokens
        ledger.record_turn(
            usage={
                "prompt_tokens": 1000,
                "completion_tokens": 500,
                "completion_tokens_details": {"reasoning_tokens": 300},
                "total_tokens": 1500,
            },
            timings={},
            reasoning_text="thinking...",
            content_text="answer",
        )
        self.assertEqual(ledger.data["prompt_tokens"], 1000)
        self.assertEqual(ledger.data["completion_tokens"], 500)
        self.assertEqual(ledger.data["reasoning_tokens"], 300)
        # Total MUST be 1500, NOT 1800!
        self.assertEqual(ledger.data["total_tokens"], 1500)

        json_path, md_path, report_obj, md_text = ledger.generate_final_reports(
            plan=plan, locked_files=[], live_requests=[]
        )
        self.assertTrue(json_path.is_file())
        self.assertTrue(md_path.is_file())
        self.assertEqual(report_obj["tokens"]["output_write_tokens_excluding_reasoning"], 200)
        self.assertIn("No Double-Counting", md_text)


if __name__ == "__main__":
    unittest.main()