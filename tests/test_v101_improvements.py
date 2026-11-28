"""Unit & integration tests for LAPO v1.0.1 improvements: ANSI stripping & anti-loop JSON recovery."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict

import lapo
from lapo.config import RuntimeConfig
from lapo.core.orchestrator import ProjectOrchestrator
from lapo.core.verification_manager import strip_ansi
from tests.mock_strata_server import MockStrataServer


class TestV101Improvements(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_version_is_1_0_2(self) -> None:
        self.assertEqual(lapo.__version__, "1.1.1")

    def test_strip_ansi_cleans_vitest_output(self) -> None:
        dirty = "\x1b[31m\x1b[1mAssertionError\x1b[22m: expected 565 to be 4661\x1b[39m\n\x1b[2m > \x1b[22mtests/hardware.test.ts"
        cleaned = strip_ansi(dirty)
        self.assertEqual(cleaned, "AssertionError: expected 565 to be 4661\n > tests/hardware.test.ts")
        self.assertNotIn("\x1b", cleaned)

    def test_tailored_recovery_messages_and_anti_loop_escalation(self) -> None:
        err = json.JSONDecodeError("Unterminated string", "{", 10)
        msg_patch_1 = ProjectOrchestrator._build_truncated_json_recovery_message("patch_file", err, 4200, streak=1)
        self.assertIn("5 to 15 lines", msg_patch_1)
        self.assertNotIn("CRITICAL ANTI-LOOP", msg_patch_1)

        msg_patch_2 = ProjectOrchestrator._build_truncated_json_recovery_message("patch_file", err, 5000, streak=2)
        self.assertIn("CRITICAL ANTI-LOOP WARNING", msg_patch_2)
        self.assertIn("streak=2", msg_patch_2.replace("consecutive truncated JSON #2", "streak=2"))

        msg_write_1 = ProjectOrchestrator._build_truncated_json_recovery_message("write_file", err, 9000, streak=1)
        self.assertIn('<file path="relative/path.ext">', msg_write_1)

    def test_consecutive_truncated_patch_triggers_anti_loop_and_recovers(self) -> None:
        with MockStrataServer(model_id="mock-loop-test", n_ctx=8192) as srv:
            # Turn 1: first truncated patch_file
            srv.queue_reply(
                {
                    "tool_calls": [
                        {
                            "id": "call_bad_1",
                            "type": "function",
                            "function": {
                                "name": "patch_file",
                                "arguments": '{"path": "app.py", "target_snippet": "broken...',
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 300, "completion_tokens": 50},
                }
            )
            # Turn 2: second consecutive truncated patch_file
            srv.queue_reply(
                {
                    "tool_calls": [
                        {
                            "id": "call_bad_2",
                            "type": "function",
                            "function": {
                                "name": "patch_file",
                                "arguments": '{"path": "app.py", "target_snippet": "still broken...',
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 400, "completion_tokens": 50},
                }
            )

            # Turn 3: verify escalated CRITICAL ANTI-LOOP warning reached the model, then recover via <file> + complete_phase
            def turn_3_recover(req: Dict[str, Any]) -> Dict[str, Any]:
                last_tool_msg = req["messages"][-1]["content"]
                assert "CRITICAL ANTI-LOOP WARNING" in last_tool_msg, f"Expected escalation, got: {last_tool_msg}"
                assert "5 to 15 lines" in last_tool_msg
                return {
                    "content": '<file path="app.py">\nx = 42\n</file>\n',
                    "tool_calls": [
                        {
                            "id": "call_done",
                            "type": "function",
                            "function": {
                                "name": "complete_phase",
                                "arguments": json.dumps({"summary": "Recovered cleanly", "files_to_lock": ["app.py"]}),
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 500, "completion_tokens": 80},
                }

            srv.queue_reply(turn_3_recover)

            cfg = RuntimeConfig(
                workspace_dir=self.ws,
                api_base_url=srv.base_url,
                model_name=srv.model_id,
                unattended_auto_verify=True,
            )
            orch = ProjectOrchestrator(config=cfg)
            outcome = orch.run(
                raw_phases=[{"name": "Phase 1", "prompt": "Create app.py", "verification_commands": []}],
                max_turns=6,
            )
            self.assertTrue(outcome["finished"])
            self.assertEqual((self.ws / "app.py").read_text(encoding="utf-8").strip(), "x = 42")


if __name__ == "__main__":
    unittest.main()