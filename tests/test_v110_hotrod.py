"""Comprehensive tests for LAPO v1.1.0 HotRod & Strata 0.1.67 integration."""
from __future__ import annotations
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
import lapo
from lapo.adapters.strata_adapter import (
    ENGINE_READY,
    ENGINE_TERMINATED,
    EngineTerminatedError,
    StrataAdapter,
)
from lapo.config import STRATA_HOTROD_PROFILES, RuntimeConfig
from lapo.core.compaction_manager import (
    build_workspace_ast_map,
    enforce_continuation_report_budget,
    parse_continuation_report,
)
from lapo.core.file_lock_manager import FileLockManager
from lapo.core.orchestrator import ProjectOrchestrator
from lapo.core.usage_ledger import UsageLedger
from lapo.core.verification_manager import VerificationManager
from lapo.models import PhaseDefinition
from lapo.tools.workspace_tools import TOOLS_SCHEMA, WorkspaceTools
class _FakeSSEResponse:
    def __init__(self, chunks: list[dict]) -> None:
        self._chunks = chunks
        self.closed = False
    def iter_lines(self, decode_unicode: bool = False):
        for ch in self._chunks:
            if self.closed:
                break
            line = "data: " + json.dumps(ch)
            yield line if decode_unicode else line.encode("utf-8")
        if not self.closed:
            yield "data: [DONE]" if decode_unicode else b"data: [DONE]"
    def close(self) -> None:
        self.closed = True
class TestV110HotRod(unittest.TestCase):
    def test_version_and_profiles_exist(self) -> None:
        self.assertEqual(lapo.__version__, "1.1.0")
        self.assertIn("gyro_m_256k_native", STRATA_HOTROD_PROFILES)
        self.assertIn("coder_iq1_m_128k", STRATA_HOTROD_PROFILES)
    def test_delete_file_unlocked_and_locked_guards(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = RuntimeConfig(workspace_dir=Path(tmp), model_name="test-model")
            cfg.ensure_orchestrator_dirs()
            lm = FileLockManager(cfg)
            vm = VerificationManager(cfg)
            tools = WorkspaceTools(cfg, lm, vm)
            # Schema exposes delete_file
            tool_names = [t["function"]["name"] for t in TOOLS_SCHEMA]
            self.assertIn("delete_file", tool_names)
            # Create unlocked scratch file & locked module
            scratch = Path(tmp) / "scratch_tmp.txt"
            scratch.write_text("temp data", encoding="utf-8")
            core = Path(tmp) / "core.py"
            core.write_text("VALUE = 1\n", encoding="utf-8")
            lm.commit_phase_locks(phase_id=1, explicit_files=["core.py"])
            # Deleting unlocked file succeeds and creates a backup
            res = json.loads(tools.execute("delete_file", {"path": "scratch_tmp.txt", "reason": "cleanup"}))
            self.assertEqual(res["status"], "deleted")
            self.assertFalse(scratch.exists())
            self.assertTrue(res["backup"])
            self.assertTrue((cfg.backups_dir / res["backup"]).is_file())
            # Deleting locked file is rejected
            locked_res = json.loads(tools.execute("delete_file", {"path": "core.py"}))
            self.assertIn("error", locked_res)
            self.assertIn("LOCKED FILE REJECTION", locked_res["error"])
            self.assertTrue(core.exists())
            # Deleting a directory is rejected
            sub = Path(tmp) / "sub"
            sub.mkdir()
            dir_res = json.loads(tools.execute("delete_file", {"path": "sub"}))
            self.assertIn("error", dir_res)
            self.assertTrue(sub.is_dir())
    def test_append_only_history_hygiene_strips_thought_and_compacts_writes(self) -> None:
        big_code = "\n".join(f"def fn_{i}(x): return x + {i}" for i in range(120))
        big_thought = "Drafting full implementation... " * 400
        raw_msg = {
            "role": "assistant",
            "reasoning_content": big_thought,
            "content": f'Writing now.\n<file path="pkg/mod.py">\n{big_code}\n</file>',
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "write_file",
                        "arguments": json.dumps({"path": "pkg/other.py", "content": big_code}),
                    },
                }
            ],
        }
        sanitized, stripped = ProjectOrchestrator._sanitize_assistant_message_with_metrics(
            raw_msg,
            strip_reasoning=True,
            compact_file_writes=True,
            inline_written=["pkg/mod.py"],
        )
        self.assertNotIn("reasoning_content", sanitized)
        self.assertGreater(stripped, len(big_thought) + len(big_code))
        self.assertIn("[compacted from history:", sanitized["content"])
        args_after = json.loads(sanitized["tool_calls"][0]["function"]["arguments"])
        self.assertEqual(args_after["path"], "pkg/other.py")
        self.assertIn("[compacted from history:", args_after["content"])
        # Original raw_msg must remain untouched
        self.assertEqual(raw_msg["reasoning_content"], big_thought)
    def test_thought_watchdog_interrupts_only_when_no_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = RuntimeConfig(
                workspace_dir=Path(tmp),
                model_name="coder",
                thought_watchdog_enabled=True,
                thought_watchdog_max_chars=500,
            )
            adapter = StrataAdapter(cfg)
            # Case 1: Only reasoning chunks exceed 500 chars -> interrupted
            chunks_runaway = [
                {"choices": [{"delta": {"reasoning_content": "A" * 260}}]},
                {"choices": [{"delta": {"reasoning_content": "B" * 260}}]},
                {"choices": [{"delta": {"reasoning_content": "C" * 260}}]},
            ]
            resp1 = _FakeSSEResponse(chunks_runaway)
            msg1, finish1, _, _, stopped1 = adapter._consume_sse_stream(
                resp1, start_ts=0.0, thought_watchdog_max_chars=500
            )
            self.assertEqual(finish1, "thought_watchdog")
            self.assertFalse(stopped1)
            self.assertTrue(resp1.closed)
            self.assertGreaterEqual(len(msg1.get("reasoning_content", "")), 500)
            # Case 2: Content already started -> watchdog must NOT interrupt
            chunks_normal = [
                {"choices": [{"delta": {"reasoning_content": "A" * 300}}]},
                {"choices": [{"delta": {"content": "Hello output"}}]},
                {"choices": [{"delta": {"reasoning_content": "B" * 300}, "finish_reason": "stop"}]},
            ]
            resp2 = _FakeSSEResponse(chunks_normal)
            msg2, finish2, _, _, _ = adapter._consume_sse_stream(
                resp2, start_ts=0.0, thought_watchdog_max_chars=500
            )
            self.assertEqual(finish2, "stop")
            self.assertIn("Hello output", msg2.get("content", ""))
    def test_compaction_budget_and_ast_map_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            pkg = ws / "mini_kv"
            pkg.mkdir()
            (pkg / "query.py").write_text(
                "DEFAULT_LIMIT = 10\n\n"
                "class QueryEngine:\n"
                "    def __init__(self, store): pass\n"
                "    def filter(self, prefix=None): pass\n\n"
                "def sort_records(records, reverse=False):\n"
                "    return records\n",
                encoding="utf-8",
            )
            ast_map = build_workspace_ast_map(ws, locked_paths={"mini_kv/store.py"})
            self.assertIn("mini_kv/query.py", ast_map)
            syms = ast_map["mini_kv/query.py"]["symbols"]
            self.assertTrue(any("class QueryEngine:" in s and "filter(self, prefix)" in s for s in syms))
            self.assertTrue(any("def sort_records(records, reverse)" in s for s in syms))
            # Huge continuation report gets cleanly bounded
            phase = PhaseDefinition(id=2, name="Query", prompt="Build query engine")
            bloated = {
                "current_objective": "O" * 800,
                "current_phase_id": 2,
                "current_phase_name": "Query",
                "design_decisions_locked": ["D" * 600 for _ in range(15)],
                "active_assumptions": ["A" * 400 for _ in range(10)],
                "completed_work": ["C" * 400 for _ in range(10)],
                "files_created_or_modified": ["mini_kv/query.py"],
                "unfinished_work": ["U" * 500 for _ in range(10)],
                "known_failures_and_blockers": [],
                "next_exact_action": "N" * 700,
            }
            parsed = parse_continuation_report(json.dumps(bloated), phase, max_chars=3500)
            self.assertLessEqual(len(json.dumps(parsed, ensure_ascii=False)), 3500)
            _, was_trimmed = enforce_continuation_report_budget(bloated, max_chars=3500)
            self.assertTrue(was_trimmed)
    def test_strata_0167_status_metrics_and_terminated_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = RuntimeConfig(workspace_dir=Path(tmp), model_name="qwen3.8-flash-next-gyro-gyro-m")
            cfg.ensure_orchestrator_dirs()
            adapter = StrataAdapter(cfg)
            # Inspect /v1/status detecting engine 0.1.67 and cache_max_tokens=262144
            def fake_get(url: str, timeout: float = 8.0):
                if url.endswith("/v1/status"):
                    return {"engine": "0.1.67", "cache_max_tokens": 262144, "loaded": True, "activity": "idle"}
                return None
            with patch.object(adapter, "_safe_get_json", side_effect=fake_get),                  patch.object(adapter, "_query_api_models", return_value=(True, {"qwen3.8-flash-next-gyro-gyro-m": {}}, None)),                  patch.object(adapter, "count_tokens", return_value=(10, True)):
                insp = adapter.inspect_server()
                self.assertTrue(insp.reachable)
                self.assertEqual(insp.engine_version, "0.1.67")
                self.assertEqual(insp.detected_context_tokens, 262144)
                self.assertEqual(insp.engine_state, ENGINE_READY)
            # Detect ENGINE_TERMINATED state
            state, detail = adapter._classify_engine_state(
                {"loaded": False, "error": "engine terminated: stopped restarting after 3 crashes"},
                reachable=True,
            )
            self.assertEqual(state, ENGINE_TERMINATED)
            self.assertIn("3 crashes", detail or "")
            # UsageLedger computes Strata /metrics delta (read vs reused + drafts)
            ledger = UsageLedger(cfg)
            ledger.mark_started(
                detected_ctx=262144,
                engine_version="0.1.67",
                strata_metrics_snapshot={
                    "totals": {
                        "since": 100.0,
                        "requests": 1,
                        "prompt_tokens": 100,
                        "reused": 0,
                        "output_tokens": 20,
                        "decode_ms": 1000.0,
                        "drafts_offered": 10,
                        "drafts_accepted": 8,
                    }
                },
            )
            ledger.record_strata_metrics_end(
                {
                    "totals": {
                        "since": 100.0,
                        "requests": 5,
                        "prompt_tokens": 1100,
                        "reused": 900,
                        "output_tokens": 220,
                        "decode_ms": 11000.0,
                        "drafts_offered": 110,
                        "drafts_accepted": 93,
                    }
                }
            )
            delta = ledger.data["strata_telemetry_delta"]
            self.assertEqual(delta["prompt_tokens"], 1000)
            self.assertEqual(delta["prompt_tokens_reused"], 900)
            self.assertEqual(delta["prompt_tokens_read"], 100)
            self.assertEqual(delta["prompt_reuse_pct"], 90.0)
            self.assertEqual(delta["draft_accept_pct"], 85.0)
            self.assertEqual(delta["weighted_decode_tok_s"], 20.0)
    def test_cli_profiles_and_override_precedence(self) -> None:
        from lapo.cli import _load_config_from_args, build_parser, main
        self.assertEqual(main(["profiles"]), 0)
        parser = build_parser()
        with tempfile.TemporaryDirectory() as tmp:
            # Profile applies defaults
            args1 = parser.parse_args(["run", "--workspace", tmp, "--profile", "coder_iq1_m_128k"])
            cfg1, _ = _load_config_from_args(args1)
            self.assertEqual(cfg1.context_setting, 131072)
            self.assertEqual(cfg1.thought_watchdog_max_chars, 24000)
            self.assertTrue(cfg1.thought_watchdog_enabled)
            # Explicit CLI flags override profile defaults
            args2 = parser.parse_args([
                "run", "--workspace", tmp, "--profile", "coder_iq1_m_128k",
                "--context", "64K", "--thought-watchdog-max-chars", "15000",
                "--no-thought-watchdog", "--keep-history-reasoning", "--no-ast-map",
            ])
            cfg2, _ = _load_config_from_args(args2)
            self.assertEqual(cfg2.context_setting, 65536)
            self.assertEqual(cfg2.thought_watchdog_max_chars, 15000)
            self.assertFalse(cfg2.thought_watchdog_enabled)
            self.assertFalse(cfg2.strip_history_reasoning)
            self.assertFalse(cfg2.inject_ast_map_on_compaction)
    def test_cli_init_config_includes_v110_fields(self) -> None:
        from lapo.cli import main
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(main(["init-config", "--directory", tmp]), 0)
            data = json.loads((Path(tmp) / "project.json").read_text(encoding="utf-8"))
            self.assertTrue(data["strip_history_reasoning"])
            self.assertTrue(data["thought_watchdog_enabled"])
            self.assertEqual(data["thought_watchdog_max_chars"], 32000)
            self.assertTrue(data["inject_ast_map_on_compaction"])

if __name__ == "__main__":
    unittest.main()
