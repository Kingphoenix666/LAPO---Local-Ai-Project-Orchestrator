"""Unit tests for proxy normalization, private LAN/MikroTik routing, failover, and npm bootstrap."""

from __future__ import annotations

import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from lapo.adapters.strata_adapter import StrataAdapter
from lapo.config import (
    LOCAL_BYPASS_PROXIES,
    RuntimeConfig,
    is_private_network_url,
    normalize_proxy_url,
    resolve_active_proxy,
)
from lapo.core.verification_manager import VerificationManager


class TestProxyAndBootstrap(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_proxy_normalization_and_private_lan_detection(self) -> None:
        self.assertEqual(normalize_proxy_url("192.168.1.3:38861"), "http://192.168.1.3:38861")
        self.assertEqual(normalize_proxy_url("http://192.168.1.6:33877/"), "http://192.168.1.6:33877")
        self.assertIsNone(normalize_proxy_url("   "))

        # Loopback and RFC1918 private LAN IPs (e.g. MikroTik / local GPU servers)
        self.assertTrue(is_private_network_url("http://127.0.0.1:47831/v1"))
        self.assertTrue(is_private_network_url("http://192.168.1.50:47831/v1"))
        self.assertTrue(is_private_network_url("http://10.0.0.25:8080/v1"))
        self.assertTrue(is_private_network_url("http://172.16.5.10:8000/v1"))
        self.assertTrue(is_private_network_url("http://gpu-box.local:11434/v1"))
        self.assertFalse(is_private_network_url("https://api.openai.com/v1"))

    def test_primary_to_fallback_proxy_failover(self) -> None:
        # Bind an ephemeral local TCP socket to emulate a live fallback proxy
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(2)
        live_port = srv.getsockname()[1]

        # Reserve a closed port to emulate a dead primary proxy
        dead = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        dead.bind(("127.0.0.1", 0))
        dead_port = dead.getsockname()[1]
        dead.close()

        try:
            active, label = resolve_active_proxy(
                f"127.0.0.1:{dead_port}",
                f"127.0.0.1:{live_port}",
                timeout=0.5,
            )
            self.assertEqual(label, "fallback")
            self.assertEqual(active, f"http://127.0.0.1:{live_port}")
        finally:
            srv.close()

    def test_private_lan_direct_vs_explicit_api_via_proxy(self) -> None:
        # Default: Private LAN (MikroTik/local server) bypasses OS proxy directly
        cfg_direct = RuntimeConfig(
            workspace_dir=self.ws,
            api_base_url="http://192.168.1.50:47831/v1",
            primary_proxy="192.168.1.3:38861",
            route_api_via_proxy=False,
        )
        adapter_direct = StrataAdapter(cfg_direct)
        self.assertEqual(adapter_direct._proxies, LOCAL_BYPASS_PROXIES)
        self.assertIn("private LAN", adapter_direct.api_route_summary)

        # Explicit route_api_via_proxy=True routes through active proxy
        with patch("lapo.adapters.strata_adapter.resolve_active_proxy", return_value=("http://192.168.1.3:38861", "primary")):
            cfg_proxied = RuntimeConfig(
                workspace_dir=self.ws,
                api_base_url="http://192.168.1.50:47831/v1",
                primary_proxy="192.168.1.3:38861",
                route_api_via_proxy=True,
            )
            adapter_proxied = StrataAdapter(cfg_proxied)
            self.assertEqual(
                adapter_proxied._proxies,
                {"http": "http://192.168.1.3:38861", "https": "http://192.168.1.3:38861"},
            )

    def test_config_roundtrip_preserves_proxy_and_bootstrap_fields(self) -> None:
        cfg = RuntimeConfig(
            workspace_dir=self.ws,
            primary_proxy="192.168.1.3:38861",
            fallback_proxy="192.168.1.6:33877",
            route_api_via_proxy=True,
            auto_install_deps=False,
        )
        safe = cfg.to_safe_dict()
        restored = RuntimeConfig.from_dict(safe)
        self.assertEqual(restored.primary_proxy, "http://192.168.1.3:38861")
        self.assertEqual(restored.fallback_proxy, "http://192.168.1.6:33877")
        self.assertTrue(restored.route_api_via_proxy)
        self.assertFalse(restored.auto_install_deps)

    def test_bootstrap_installs_missing_node_modules_via_active_proxy(self) -> None:
        (self.ws / "package.json").write_text('{"name":"demo","version":"1.0.0"}', encoding="utf-8")
        cfg = RuntimeConfig(
            workspace_dir=self.ws,
            primary_proxy="192.168.1.3:38861",
            fallback_proxy="192.168.1.6:33877",
        )
        vm = VerificationManager(cfg)

        fake_proc = MagicMock(returncode=0, stdout="added 10 packages", stderr="")
        with (
            patch("lapo.core.verification_manager.resolve_active_proxy", return_value=("http://192.168.1.3:38861", "primary")),
            patch("lapo.core.verification_manager.resolve_executable", return_value="npm"),
            patch("lapo.core.verification_manager.subprocess.run", return_value=fake_proc) as mock_run,
        ):
            res = vm.bootstrap_workspace_dependencies()

        assert res is not None
        self.assertEqual(res["status"], "installed")
        called_argv = mock_run.call_args.args[0]
        self.assertIn("--proxy", called_argv)
        self.assertIn("http://192.168.1.3:38861", called_argv)


if __name__ == "__main__":
    unittest.main()