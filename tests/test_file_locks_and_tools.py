"""Unit tests for WorkspaceTools security, inline <file> writer, locks, dependencies, and repair."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from lapo.config import RuntimeConfig
from lapo.core.file_lock_manager import FileLockManager
from lapo.core.verification_manager import VerificationManager
from lapo.tools.inline_file_writer import extract_inline_file_blocks, extract_text_tool_calls
from lapo.tools.workspace_tools import WorkspaceTools


class TestFileLocksAndTools(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ws = self.root / "workspace"
        self.skills = self.root / "skills"
        self.ws.mkdir()
        self.skills.mkdir()
        (self.skills / "guide.md").write_text("# Coding Guide\nAlways write tests.", encoding="utf-8")

        self.cfg = RuntimeConfig(workspace_dir=self.ws, skills_dir=self.skills)
        self.cfg.ensure_orchestrator_dirs()
        self.lm = FileLockManager(self.cfg)
        self.vm = VerificationManager(self.cfg)
        self.tools = WorkspaceTools(self.cfg, self.lm, self.vm)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_path_traversal_and_protected_dirs_blocked(self) -> None:
        for bad_path in ("../escape.txt", ".orchestrator/state.json", "user_request.json"):
            out = json.loads(self.tools.execute("write_file", {"path": bad_path, "content": "x"}))
            self.assertIn("error", out)

    def test_symlink_escape_blocked(self) -> None:
        outside = self.root / "secret.txt"
        outside.write_text("top-secret", encoding="utf-8")
        link_path = self.ws / "link_secret.txt"
        try:
            os.symlink(outside, link_path)
        except OSError:
            self.skipTest("OS does not permit creating symlinks without elevated privileges.")

        out = json.loads(self.tools.execute("read_file", {"path": "link_secret.txt"}))
        self.assertIn("error", out)

    def test_read_before_edit_and_locked_file_rejection(self) -> None:
        # Create a non-placeholder file externally
        (self.ws / "existing.py").write_text("def hello():\n    return 'world' * 20\n", encoding="utf-8")

        # Overwrite without reading first must be rejected
        res1 = json.loads(self.tools.execute("write_file", {"path": "existing.py", "content": "x = 1\n"}))
        self.assertIn("error", res1)
        self.assertIn("READ-BEFORE-EDIT", res1["error"])

        # After reading, overwrite succeeds and creates a backup
        content = self.tools.execute("read_file", {"path": "existing.py"})
        self.assertIn("def hello", content)
        res2 = json.loads(self.tools.execute("write_file", {"path": "existing.py", "content": "def hello():\n    return 'updated'\n"}))
        self.assertEqual(res2["status"], "ok")
        self.assertIsNotNone(res2["backup"])

        # Commit lock for Phase 1
        self.lm.commit_phase_locks(1, ["existing.py"], file_dependencies={"consumer.py": ["existing.py"]})

        # Editing locked file via write_file OR inline <file> must fail
        res3 = json.loads(self.tools.execute("write_file", {"path": "existing.py", "content": "hacked"}))
        self.assertIn("LOCKED FILE REJECTION", res3["error"])

        written, errs = self.tools.extract_and_write_inline_files('<file path="existing.py">\nhacked\n</file>')
        self.assertEqual(written, [])
        self.assertEqual(len(errs), 1)

        # Reset session reads (simulating Phase 2 start) -> editing dependent `consumer.py` requires reading `existing.py` first!
        self.lm.reset_session_reads()
        res_dep = json.loads(self.tools.execute("write_file", {"path": "consumer.py", "content": "import existing\n"}))
        self.assertIn("DEPENDENCY SYNC REQUIRED", res_dep["error"])

        # Read locked reference -> dependent write succeeds
        self.tools.execute("read_file", {"path": "existing.py"})
        res_dep_ok = json.loads(self.tools.execute("write_file", {"path": "consumer.py", "content": "import existing\n"}))
        self.assertEqual(res_dep_ok["status"], "ok")

    def test_unlock_repair_and_external_tamper_detection(self) -> None:
        (self.ws / "api.py").write_text("VERSION = 1\n", encoding="utf-8")
        self.lm.commit_phase_locks(1, ["api.py"])
        self.assertEqual(self.lm.verify_integrity(), [])

        # External modification detected via SHA-256
        (self.ws / "api.py").write_text("VERSION = 999\n", encoding="utf-8")
        issues = self.lm.verify_integrity()
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["issue"], "external_modification_detected")

        # Controlled unlock requires reread before edit
        self.lm.unlock_for_repair("api.py", reason="Fix version")
        res_block = json.loads(self.tools.execute("write_file", {"path": "api.py", "content": "VERSION = 2\n"}))
        self.assertIn("REPAIR PROTOCOL", res_block["error"])

        self.tools.execute("read_file", {"path": "api.py"})
        res_ok = json.loads(self.tools.execute("write_file", {"path": "api.py", "content": "VERSION = 2\n"}))
        self.assertEqual(res_ok["status"], "ok")

        # Re-lock after repair
        self.lm.relock_repaired_file("api.py")
        self.assertTrue(self.lm.is_locked("api.py"))
        self.assertEqual(self.lm.verify_integrity(), [])

    def test_skills_read_only_and_inline_extractors(self) -> None:
        listed = json.loads(self.tools.execute("list_skills", {}))
        self.assertIn("guide.md", listed["entries"])
        skill_text = self.tools.execute("read_skill", {"path": "guide.md"})
        self.assertIn("Coding Guide", skill_text)

        blocks = extract_inline_file_blocks(
            'Here is code:\n<file path="a.py">\nx = 1\n</file>\n```python file="b.py"\ny = 2\n```'
        )
        self.assertEqual([b.path for b in blocks], ["a.py", "b.py"])

        calls = extract_text_tool_calls('<tool_call>{"name": "list_dir", "arguments": {"path": "."}}</tool_call>')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].name, "list_dir")


if __name__ == "__main__":
    unittest.main()