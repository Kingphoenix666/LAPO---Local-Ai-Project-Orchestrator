"""Unit tests for LiveRequestManager: atomic publish, one-time consumption, readed_ rename, routing."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lapo.config import RuntimeConfig
from lapo.core.live_request_manager import LiveRequestManager, publish_live_request_file


class TestLiveRequests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        self.cfg = RuntimeConfig(workspace_dir=self.ws, live_request_poll_s=0.2)
        self.cfg.ensure_orchestrator_dirs()
        self.mgr = LiveRequestManager(self.cfg)

    def tearDown(self) -> None:
        self.mgr.stop_watcher()
        self.tmp.cleanup()

    def test_publish_consume_rename_and_never_overwrite_unconsumed(self) -> None:
        req = publish_live_request_file(self.ws, prompt="Add input validation", phase="1", request_id="req-001")
        self.assertTrue((self.ws / "user_request.json").is_file())

        # Publishing again while unconsumed must raise FileExistsError
        with self.assertRaises(FileExistsError):
            publish_live_request_file(self.ws, prompt="Second request", phase="1", wait_if_busy_s=0.0)

        consumed = self.mgr.poll_once()
        assert consumed is not None
        self.assertEqual(consumed.request_id, req.request_id)
        self.assertFalse((self.ws / "user_request.json").exists())

        # Verify renamed file starts with `readed_user_request_`
        readed_files = list(self.ws.glob("readed_user_request_*.json"))
        self.assertEqual(len(readed_files), 1)

        # Polling again must ignore `readed_` files completely
        self.assertIsNone(self.mgr.poll_once())

    def test_phase_routing_and_acknowledgment_gate(self) -> None:
        publish_live_request_file(self.ws, prompt="Fix phase 1", phase="1", request_id="req-p1")
        self.mgr.poll_once()

        publish_live_request_file(self.ws, prompt="Enhance phase 2", phase="2", request_id="req-p2")
        self.mgr.poll_once()

        # Phase 1 only sees req-p1; req-p2 remains queued for Phase 2
        p1_reqs = self.mgr.get_unresolved_for_phase(1)
        self.assertEqual([r.request_id for r in p1_reqs], ["req-p1"])

        # Acknowledging wrong ID keeps req-p1 unresolved
        accepted, remaining = self.mgr.acknowledge_requests(1, ["wrong-id"])
        self.assertEqual(accepted, [])
        self.assertEqual([r.request_id for r in remaining], ["req-p1"])

        # Acknowledging exact ID resolves Phase 1 request while Phase 2 remains intact
        accepted2, remaining2 = self.mgr.acknowledge_requests(1, ["req-p1"])
        self.assertEqual(accepted2, ["req-p1"])
        self.assertEqual(remaining2, [])
        self.assertEqual([r.request_id for r in self.mgr.get_unresolved_for_phase(2)], ["req-p2"])


if __name__ == "__main__":
    unittest.main()