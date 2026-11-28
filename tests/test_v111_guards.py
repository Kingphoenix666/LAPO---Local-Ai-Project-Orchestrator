import json
import tempfile
import unittest
from pathlib import Path

from lapo.config import RuntimeConfig
from lapo.core.compaction_manager import build_workspace_ast_map
from lapo.core.file_lock_manager import FileLockManager
from lapo.core.orchestrator import ProjectOrchestrator
from lapo.core.verification_manager import VerificationManager
from lapo.tools.inline_file_writer import extract_inline_file_blocks
from lapo.tools.workspace_tools import WorkspaceTools


class TestV111GuardsAndTools(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.ws = self.root / "workspace"
        self.skills = self.root / "skills"
        self.ws.mkdir(parents=True, exist_ok=True)
        self.skills.mkdir(parents=True, exist_ok=True)

        self.cfg = RuntimeConfig(
            workspace_dir=self.ws,
            skills_dir=self.skills,
            api_base_url="http://127.0.0.1:47831/v1",
            max_tool_output_chars=120,
        )
        self.cfg.ensure_orchestrator_dirs()
        self.locks = FileLockManager(self.cfg)
        self.verif = VerificationManager(self.cfg)
        self.tools = WorkspaceTools(
            config=self.cfg,
            lock_manager=self.locks,
            verification_manager=self.verif,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_shrink_guard_and_backup_restore_roundtrip(self) -> None:
        original = "\n".join(f"export const LINE_{i} = {i};" for i in range(250)) + "\n"
        self.assertGreaterEqual(len(original.encode("utf-8")), 4096)

        res1 = json.loads(self.tools.execute("write_file", {"path": "src/big.ts", "content": original}))
        self.assertEqual(res1["status"], "ok")
        self.assertEqual(res1["lines_written"], 250)

        # Read before overwrite
        self.tools.execute("read_file", {"path": "src/big.ts"})

        # 1) Accidental shrink via write_file must be blocked
        blocked = json.loads(self.tools.execute("write_file", {"path": "src/big.ts", "content": "export {};\n"}))
        self.assertIn("SHRINK_GUARD_BLOCKED", blocked.get("error", ""))
        self.assertEqual((self.ws / "src/big.ts").read_text(encoding="utf-8"), original)

        # 2) Accidental shrink via inline <file> must also be blocked
        written, errors = self.tools.extract_and_write_inline_files('<file path="src/big.ts">\nshort\n</file>')
        self.assertEqual(written, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("SHRINK_GUARD_BLOCKED", errors[0]["error"])

        # 3) Explicit allow_shrink succeeds and creates backup
        allowed = json.loads(
            self.tools.execute("write_file", {"path": "src/big.ts", "content": "export const OK = 1;\n", "allow_shrink": True})
        )
        self.assertEqual(allowed["status"], "ok")
        backup_name = allowed["backup"]
        self.assertTrue(backup_name)
        self.assertIn("restore_backup", allowed.get("restore_hint", ""))

        # 4) list_backups finds it
        listed = json.loads(self.tools.execute("list_backups", {"path": "src/big.ts"}))
        names = [b["backup_name"] for b in listed["backups"]]
        self.assertIn(backup_name, names)

        # 5) Mismatched target path is rejected
        mismatch = json.loads(
            self.tools.execute("restore_backup", {"path": "src/other.ts", "backup_name": backup_name})
        )
        self.assertIn("does not match target path", mismatch.get("error", ""))

        # 6) restore_backup restores original bytes intact
        restored = json.loads(
            self.tools.execute("restore_backup", {"path": "src/big.ts", "backup_name": backup_name})
        )
        self.assertEqual(restored["status"], "restored")
        self.assertEqual((self.ws / "src/big.ts").read_text(encoding="utf-8"), original)

    def test_inline_allow_shrink_attribute_parsing(self) -> None:
        blocks = extract_inline_file_blocks('<file path="a.ts" allow_shrink="true">\nconst x = 1;\n</file>')
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].path, "a.ts")
        self.assertTrue(blocks[0].allow_shrink)

    def test_read_skill_line_ranges_and_total_lines(self) -> None:
        ref = self.skills / "ref.ts"
        ref.write_text("".join(f"line {i:02d} content here\n" for i in range(1, 21)), encoding="utf-8")

        chunk = self.tools.execute("read_skill", {"path": "ref.ts", "start_line": 5, "end_line": 7})
        self.assertEqual(chunk.splitlines(), ["line 05 content here", "line 06 content here", "line 07 content here"])

        truncated = self.tools.execute("read_skill", {"path": "ref.ts"})
        self.assertIn("total_lines=20", truncated)
        self.assertIn("start_line", truncated)

    def test_ast_map_supports_typescript_and_python(self) -> None:
        (self.ws / "src").mkdir(exist_ok=True)
        (self.ws / "src/app.ts").write_text(
            "export const MAX_RETRY = 3;\nexport class Runner {}\nexport function runJob() {}\n",
            encoding="utf-8",
        )
        (self.ws / "helper.py").write_text("def ping():\n    return True\n", encoding="utf-8")

        ast_map = build_workspace_ast_map(self.ws)
        self.assertIn("src/app.ts", ast_map)
        self.assertIn("helper.py", ast_map)
        ts_syms = ast_map["src/app.ts"]["symbols"]
        self.assertTrue(any("export const MAX_RETRY" in s for s in ts_syms))
        self.assertTrue(any("export class Runner" in s for s in ts_syms))
        self.assertTrue(any("export function runJob" in s for s in ts_syms))

    def test_thrashing_guard_and_compaction_stub(self) -> None:
        orch = ProjectOrchestrator(self.cfg)
        streak = {}
        for _ in range(3):
            self.assertIsNone(orch._record_edit_and_check_thrashing(streak, ["src/menu.tsx"]))
        warn = orch._record_edit_and_check_thrashing(streak, ["src/menu.tsx"])
        self.assertIsNotNone(warn)
        self.assertIn("ANTI-THRASHING GUARD", warn)

        big_body = "x = 1\n" * 300
        raw_msg = {
            "role": "assistant",
            "tool_calls": [
                {"id": "1", "function": {"name": "write_file", "arguments": json.dumps({"path": "a.py", "content": big_body})}}
            ],
        }
        sanitized, saved = ProjectOrchestrator._sanitize_assistant_message_with_metrics(raw_msg)
        self.assertGreater(saved, 0)
        args_str = sanitized["tool_calls"][0]["function"]["arguments"]
        self.assertIn("[compacted from history:", args_str)
        self.assertIn("ALREADY sent & written to disk", args_str)


if __name__ == "__main__":
    unittest.main()