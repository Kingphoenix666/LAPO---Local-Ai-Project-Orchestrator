"""
Full 2-phase End-to-End Integration Test against MockStrataServer.
Deterministically verifies:
1. Exact payloads sent to the model (Phase 1 active first, Phase 2 active only after Phase 1 passes).
2. Inline `<file path="...">` auto-writing with UTF-8/Persian preservation.
3. Recovery from a truncated tool-call JSON without crashing the session.
4. KV-cache-preserving in-stream compaction mid-Phase 1 (TOOLS_SCHEMA retained, context resets, stays in Phase 1).
5. Live `user_request.json` consumed once -> renamed to `readed_user_request_*.json` -> blocks phase close until acknowledged.
6. Automated verification command with `;` (e.g. `python -c "import calc; assert ..."`) succeeds under shell=False.
7. Phase 1 output (`calc.py`) is locked; Phase 2 attempt to overwrite it is rejected; Phase 2 auto-verifies unattended.
8. Final JSON + Markdown reports generated and verified model unload executed after user approval.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict

from lapo.config import RuntimeConfig
from lapo.core.live_request_manager import publish_live_request_file
from lapo.core.orchestrator import ProjectOrchestrator
from tests.mock_strata_server import MockStrataServer


class TestEndToEndIntegration(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_two_phase_project_with_compaction_live_request_locks_and_unload(self) -> None:
        with MockStrataServer(model_id="mock-strata-coder", n_ctx=8192) as srv:
            ws = self.ws

            # Turn 1 (Phase 1): Assert Phase 1 is active, emit truncated tool_call + valid inline <file> + high tokens to trigger compaction
            def turn_1(req: Dict[str, Any]) -> Dict[str, Any]:
                user_text = req["messages"][1]["content"]
                assert "ACTIVE PHASE 1/2: Core Math" in user_text, f"Expected Phase 1 handoff, got: {user_text[:200]}"
                return {
                    "reasoning_content": "تحلیل فاز ۱ — ساخت ماژول calc.py",
                    "content": (
                        'Writing calc.py inline:\n'
                        '<file path="calc.py">\n'
                        '# ماژول محاسبات — فاز ۱\n'
                        'def add(a, b):\n'
                        '    return a + b\n\n'
                        'def multiply(a, b):\n'
                        '    return a * b\n'
                        '</file>\n'
                    ),
                    "tool_calls": [
                        {
                            "id": "call_trunc",
                            "type": "function",
                            "function": {
                                "name": "write_file",
                                "arguments": '{"path": "ignored.py", "content": "unterminated json',
                            },
                        }
                    ],
                    # 6500 tokens >= 75% of 8192 (6144) -> triggers in-stream compaction!
                    "timings": {"prompt_n": 5000, "cache_n": 0, "predicted_n": 1500},
                    "usage": {"prompt_tokens": 5000, "completion_tokens": 1500},
                }

            # Turn 2 (In-Stream Compaction): Assert TOOLS_SCHEMA was NOT stripped (KV-cache preserved!) and return JSON report
            def turn_2_compact(req: Dict[str, Any]) -> Dict[str, Any]:
                assert "tools" in req and len(req["tools"]) > 0, "TOOLS_SCHEMA must stay intact to preserve KV-cache!"
                last_content = req["messages"][-1]["content"]
                assert "IN-STREAM COMPACTION SIGNAL" in last_content
                # Also inject a live user_request.json for Phase 1 right before Turn 3!
                publish_live_request_file(ws, prompt="Ensure add handles floats too", phase="1", request_id="req-live-p1")
                return {
                    "reasoning_content": "Summarizing Phase 1 before context reset.",
                    "content": json.dumps(
                        {
                            "current_objective": "Finish Core Math Phase 1",
                            "current_phase_id": 1,
                            "current_phase_name": "Core Math",
                            "design_decisions_locked": ["Pure Python functions add & multiply"],
                            "active_assumptions": [],
                            "completed_work": ["Wrote calc.py"],
                            "files_created_or_modified": ["calc.py"],
                            "unfinished_work": ["Run checks and complete Phase 1"],
                            "known_failures_and_blockers": [],
                            "next_exact_action": "Address live requests and call complete_phase",
                        }
                    ),
                    "usage": {"prompt_tokens": 6600, "completion_tokens": 200},
                }

            # Turn 3 (Post-Compaction — MUST STILL BE PHASE 1!): Try calling complete_phase WITHOUT acknowledging req-live-p1 -> MUST BE REJECTED
            def turn_3_post_compact(req: Dict[str, Any]) -> Dict[str, Any]:
                assert len(req["messages"]) <= 3, f"Expected freshly compacted messages, got {len(req['messages'])}"
                combined = "\n".join(m.get("content") or "" for m in req["messages"])
                assert "ACTIVE PHASE 1/2: Core Math" in combined, "Compaction must NOT advance phase!"
                assert "req-live-p1" in combined, "Pending live request must be delivered to Phase 1!"
                return {
                    "tool_calls": [
                        {
                            "id": "call_complete_reject",
                            "type": "function",
                            "function": {
                                "name": "complete_phase",
                                "arguments": json.dumps(
                                    {
                                        "summary": "Premature close without request ID",
                                        "files_to_lock": ["calc.py"],
                                        "resolved_request_ids": [],
                                    }
                                ),
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 900, "completion_tokens": 80},
                }

            # Turn 4 (Phase 1 Accepted): Verify rejection message was received, acknowledge req-live-p1, complete Phase 1
            def turn_4_phase1_done(req: Dict[str, Any]) -> Dict[str, Any]:
                tool_msg = req["messages"][-1]["content"]
                assert "UNRESOLVED_LIVE_USER_REQUESTS" in tool_msg
                return {
                    "tool_calls": [
                        {
                            "id": "call_complete_p1",
                            "type": "function",
                            "function": {
                                "name": "complete_phase",
                                "arguments": json.dumps(
                                    {
                                        "summary": "Phase 1 verified and req-live-p1 handled.",
                                        "files_to_lock": ["calc.py"],
                                        "file_dependencies": {"main.py": ["calc.py"]},
                                        "resolved_request_ids": ["req-live-p1"],
                                    }
                                ),
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 1100, "completion_tokens": 90},
                }

            # Turn 5 (Phase 2): Assert Phase 2 is now active, attempt illegal overwrite of locked calc.py, read calc.py, write main.py, complete Phase 2
            def turn_5_phase2(req: Dict[str, Any]) -> Dict[str, Any]:
                handoff = req["messages"][1]["content"]
                assert "ACTIVE PHASE 2/2: App Runner" in handoff, f"Expected Phase 2 handoff, got: {handoff[:200]}"
                return {
                    "tool_calls": [
                        {
                            "id": "call_illegal_lock_edit",
                            "type": "function",
                            "function": {
                                "name": "write_file",
                                "arguments": json.dumps({"path": "calc.py", "content": "BROKEN = True"}),
                            },
                        },
                        {
                            "id": "call_read_locked",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": json.dumps({"path": "calc.py"}),
                            },
                        },
                        {
                            "id": "call_write_main",
                            "type": "function",
                            "function": {
                                "name": "write_file",
                                "arguments": json.dumps(
                                    {
                                        "path": "main.py",
                                        "content": "import calc\n\nif __name__ == '__main__':\n    print(calc.add(2, 3))\n",
                                    }
                                ),
                            },
                        },
                        {
                            "id": "call_complete_p2",
                            "type": "function",
                            "function": {
                                "name": "complete_phase",
                                "arguments": json.dumps(
                                    {
                                        "summary": "Phase 2 done. Run `python main.py` to test output.",
                                        "files_to_lock": ["main.py"],
                                    }
                                ),
                            },
                        },
                    ],
                    "usage": {"prompt_tokens": 1000, "completion_tokens": 160},
                }

            for fn in (turn_1, turn_2_compact, turn_3_post_compact, turn_4_phase1_done, turn_5_phase2):
                srv.queue_reply(fn)

            cfg = RuntimeConfig(
                workspace_dir=self.ws,
                api_base_url=srv.base_url,
                model_name=srv.model_id,
                context_setting="8K",
                compaction_threshold_pct=0.75,
                unattended_auto_verify=True,
                live_request_poll_s=0.1,
            )

            review_called = {"called": False}

            def _approve_and_unload(_report: Dict[str, Any], _md: str):
                review_called["called"] = True
                return True, True

            orch = ProjectOrchestrator(config=cfg, final_review_callback=_approve_and_unload)
            outcome = orch.run(
                raw_phases=[
                    {
                        "name": "Core Math",
                        "prompt": "Create calc.py with add and multiply.",
                        "verification_commands": ['python -c "import calc; assert calc.add(2,3)==5"'],
                    },
                    {
                        "name": "App Runner",
                        "prompt": "Create main.py using calc.py.",
                        "verification_commands": [],  # Unattended auto-verifier must pass this without manual popup!
                    },
                ],
                max_turns=15,
            )

            self.assertTrue(outcome["finished"])
            self.assertTrue(review_called["called"])
            self.assertTrue(outcome["unload_result"]["confirmed"])
            self.assertEqual(len(srv.recorded_unload_requests), 1)

            # Verify UTF-8 content preserved & calc.py was NOT overwritten by illegal Phase 2 write
            calc_text = (self.ws / "calc.py").read_text(encoding="utf-8")
            self.assertIn("ماژول محاسبات — فاز ۱", calc_text)
            self.assertNotIn("BROKEN", calc_text)
            self.assertTrue((self.ws / "main.py").is_file())

            # Verify live request was consumed and renamed to readed_user_request_*.json
            self.assertFalse((self.ws / "user_request.json").exists())
            self.assertEqual(len(list(self.ws.glob("readed_user_request_*.json"))), 1)

            # Verify compaction & token ledger recorded accurately
            report = outcome["report"]
            self.assertEqual(report["counters"]["compaction_count"], 1)
            self.assertEqual(len(report["locked_files"]), 2)
            self.assertTrue(Path(outcome["report_json_path"]).is_file())
            self.assertTrue(Path(outcome["report_md_path"]).is_file())


if __name__ == "__main__":
    unittest.main()