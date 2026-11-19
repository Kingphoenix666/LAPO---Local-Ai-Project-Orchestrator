"""Unit tests for StrataAdapter: discovery, UTF-8 SSE streaming, retries, tokenize, unload."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lapo.adapters.strata_adapter import StrataAdapter, is_loopback_url
from lapo.config import RuntimeConfig
from lapo.models import ModelAvailability
from tests.mock_strata_server import MockStrataServer


class TestStrataAdapter(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_loopback_detection(self) -> None:
        self.assertTrue(is_loopback_url("http://127.0.0.1:47831/v1"))
        self.assertTrue(is_loopback_url("http://localhost:5678/v1"))
        self.assertFalse(is_loopback_url("http://192.168.1.10:8080/v1"))

    def test_discover_models_distinguishes_active_installed_and_api_only(self) -> None:
        with MockStrataServer(model_id="active-model", n_ctx=32768) as srv:
            cfg = RuntimeConfig(workspace_dir=self.ws, api_base_url=srv.base_url, model_name="active-model")
            adapter = StrataAdapter(cfg)

            fake_installed = {
                "active-model": {"id": "active-model", "context_length": 32768},
                "dormant-model": {"id": "dormant-model", "context_length": 16384},
            }
            with patch.object(adapter, "_query_strata_cli_installed_models", return_value=fake_installed):
                models, err = adapter.discover_models()

            self.assertIsNone(err)
            by_id = {m.id: m for m in models}
            self.assertEqual(by_id["active-model"].availability, ModelAvailability.ACTIVE)
            self.assertEqual(by_id["dormant-model"].availability, ModelAvailability.INSTALLED_NOT_LOADED)

            # Without CLI inventory, API model is tagged API_ONLY
            with patch.object(adapter, "_query_strata_cli_installed_models", return_value={}):
                models2, _ = adapter.discover_models()
            self.assertEqual(models2[0].availability, ModelAvailability.API_ONLY)

    def test_sse_utf8_reasoning_content_and_fragmented_tool_calls(self) -> None:
        with MockStrataServer(model_id="utf8-model", n_ctx=16384) as srv:
            srv.queue_reply(
                {
                    "reasoning_content": "بررسی دقیق — step 1",
                    "content": "خروجی نهایی — آماده است",
                    "tool_calls": [
                        {
                            "id": "call_abc",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": json.dumps({"path": "src/app.py"}),
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 200, "completion_tokens": 50},
                }
            )
            cfg = RuntimeConfig(workspace_dir=self.ws, api_base_url=srv.base_url, model_name="utf8-model")
            adapter = StrataAdapter(cfg)
            res = adapter.chat_completion(
                messages=[{"role": "user", "content": "hi"}],
                max_tokens=512,
                stream=True,
            )

            self.assertEqual(res.message["reasoning_content"], "بررسی دقیق — step 1")
            self.assertEqual(res.message["content"], "خروجی نهایی — آماده است")
            self.assertEqual(len(res.message["tool_calls"]), 1)
            tc = res.message["tool_calls"][0]
            self.assertEqual(tc["function"]["name"], "read_file")
            self.assertEqual(json.loads(tc["function"]["arguments"]), {"path": "src/app.py"})

    def test_post_tokenize_exact_and_fallback_estimate(self) -> None:
        with MockStrataServer(enable_tokenize=True) as srv:
            cfg = RuntimeConfig(workspace_dir=self.ws, api_base_url=srv.base_url)
            adapter = StrataAdapter(cfg)
            count, is_exact = adapter.count_tokens("hello world " * 20)
            self.assertTrue(is_exact)
            self.assertGreater(count, 32)
            self.assertEqual(len(srv.recorded_tokenize_requests), 1)

        with MockStrataServer(enable_tokenize=False) as srv:
            cfg = RuntimeConfig(workspace_dir=self.ws, api_base_url=srv.base_url)
            adapter = StrataAdapter(cfg)
            count, is_exact = adapter.count_tokens("hello world " * 20)
            self.assertFalse(is_exact)
            self.assertGreater(count, 100)

    def test_retry_recovers_after_transient_http_error(self) -> None:
        with MockStrataServer() as srv:
            srv.queue_reply({"http_error": 503, "error_msg": "loading experts"})
            srv.queue_reply({"content": "recovered ok", "non_stream": True})

            cfg = RuntimeConfig(
                workspace_dir=self.ws,
                api_base_url=srv.base_url,
                model_name=srv.model_id,
                max_api_retries=3,
                retry_backoff_s=0,
            )
            adapter = StrataAdapter(cfg)
            res = adapter.chat_completion(messages=[{"role": "user", "content": "ping"}], max_tokens=64, stream=False)
            self.assertEqual(res.message["content"], "recovered ok")
            self.assertEqual(res.retries_used, 1)

    def test_unload_verification(self) -> None:
        with MockStrataServer(unload_succeeds=True) as srv:
            cfg = RuntimeConfig(workspace_dir=self.ws, api_base_url=srv.base_url, model_name=srv.model_id)
            ok, msg = StrataAdapter(cfg).unload_model()
            self.assertTrue(ok)
            self.assertIn("unloaded", msg.lower())

        with MockStrataServer(unload_succeeds=False) as srv:
            cfg = RuntimeConfig(workspace_dir=self.ws, api_base_url=srv.base_url, model_name=srv.model_id)
            adapter = StrataAdapter(cfg)
            with patch("lapo.adapters.strata_adapter._resolve_cli_binary", return_value=None):
                ok, _ = adapter.unload_model()
            self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()