"""Unit tests for PhaseManager manifest creation, isolation protocol, and transitions."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lapo.config import RuntimeConfig
from lapo.core.phase_manager import PhaseManager
from lapo.models import PhaseStatus


class TestPhaseManager(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = RuntimeConfig(workspace_dir=Path(self.tmp.name))
        self.cfg.ensure_orchestrator_dirs()
        self.pm = PhaseManager(self.cfg)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_create_persist_and_advance_phases(self) -> None:
        plan = self.pm.create_plan(
            [
                {"name": "Foundation", "prompt": "Build core module", "verification_commands": ["python -m unittest"]},
                {"name": "Driver", "prompt": "Build CLI entry", "verification_commands": []},
            ]
        )
        self.assertEqual(plan.active_phase_id, 1)
        self.assertTrue(self.cfg.plan_file.is_file())

        # Reload from disk and verify manifest integrity
        reloaded = PhaseManager(self.cfg).load()
        assert reloaded is not None
        self.assertEqual(reloaded.plan_id, plan.plan_id)
        self.assertEqual(len(reloaded.phases), 2)
        self.assertEqual(reloaded.phases[0].status, PhaseStatus.ACTIVE.value)
        self.assertEqual(reloaded.phases[1].status, PhaseStatus.PENDING.value)

        # Advance Phase 1 -> Phase 2
        finished, next_p = self.pm.advance_phase(
            summary="Phase 1 done",
            locked_files=["calc.py"],
            declared_dependencies={"main.py": ["calc.py"]},
        )
        self.assertFalse(finished)
        assert next_p is not None
        self.assertEqual(next_p.id, 2)
        self.assertEqual(self.pm.get_active_phase().id, 2)

        # Advance Phase 2 -> Project Finished
        finished2, next_p2 = self.pm.advance_phase(
            summary="Phase 2 done",
            locked_files=["main.py"],
            declared_dependencies={},
        )
        self.assertTrue(finished2)
        self.assertIsNone(next_p2)
        self.assertTrue(self.pm.plan.is_finished)

    def test_handoff_isolates_active_and_future_phases(self) -> None:
        self.pm.create_plan(
            [
                {"name": "Foundation", "prompt": "ONLY_IMPLEMENT_THIS_IN_P1"},
                {"name": "FutureUI", "prompt": "DO_NOT_IMPLEMENT_YET_IN_P1"},
            ]
        )
        handoff = self.pm.build_phase_handoff_message(
            machine_state={"git": {"worktree_fingerprint": "sha256:abc"}},
            locked_files=[],
            pending_requests=[],
            repair_queue=[],
        )
        self.assertIn("ACTIVE PHASE 1/2: Foundation", handoff)
        self.assertIn("ONLY_IMPLEMENT_THIS_IN_P1", handoff)
        self.assertIn("FUTURE PHASES (FOR ARCHITECTURAL ALIGNMENT ONLY", handoff)


if __name__ == "__main__":
    unittest.main()