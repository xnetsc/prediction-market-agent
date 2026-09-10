from __future__ import annotations

import base64
import importlib.util
import json
import os
from pathlib import Path
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
from types import SimpleNamespace

from prediction_market_agent.plugins.providers._host_proxy import client_proxy
from prediction_market_agent.plugins.providers._shared import subprocess_environment
from prediction_market_agent.plugin_system.config_io import resolve_proxy_settings
from prediction_market_agent.plugin_system.config import PluginDirectoryConfig
from prediction_market_agent.plugin_system.discovery import (
    PluginInitializationContext,
    discover_plugin_catalog,
)
from prediction_market_agent.plugins.api import binance, polymarket
from prediction_market_agent.plugins.research import standard_research

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("host_proxy_detection", ROOT / "deploy/host-proxy.py")
detector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(detector)


class HostProxyTests(unittest.TestCase):
    def test_detection_sources_and_explicit_pac_failure(self):
        cases = [
            ({"platform": "Darwin", "mac": "HTTPSEnable : 1\nHTTPSProxy : 127.0.0.1\nHTTPSPort : 7892"}, "http://host.docker.internal:7892"),
            ({"platform": "Windows", "windows": json.dumps({"enabled": True, "server": "http=localhost:8080;https=localhost:8888"})}, "http://host.docker.internal:8888"),
            ({"platform": "Linux", "environment": "HTTPS_PROXY='http://127.0.0.1:8081'"}, "http://host.proxy.internal:8081"),
            ({"platform": "Linux", "gnome": "org.gnome.system.proxy mode 'manual'\norg.gnome.system.proxy.http host 'proxy.example'\norg.gnome.system.proxy.http port 3128"}, "http://proxy.example:3128"),
            ({"platform": "Linux", "kde_ProxyType": "1", "kde_httpsProxy": "http://proxy.example 1234"}, "http://proxy.example:1234"),
            ({"platform": "Darwin", "env_https": "http://u:p@localhost:9999", "mac": "ProxyAutoConfigEnable : 1"}, "http://u:p@host.docker.internal:9999"),
        ]
        for values, expected in cases:
            with self.subTest(values=values):
                result = detector.snapshot(values, check=False)
                self.assertEqual(result["status"], "proxy")
                self.assertEqual(result["proxy"], expected)
        for values in ({"platform": "Darwin", "mac": "ProxyAutoConfigEnable : 1"},
                       {"platform": "Windows", "windows": '{"pac":"https://example/proxy.pac"}'},
                       {"platform": "Linux", "gnome": "org.gnome.system.proxy mode 'auto'"},
                       {"platform": "Linux", "env_all": "socks5://localhost:1080"}):
            with self.subTest(values=values):
                self.assertEqual(detector.snapshot(values, check=False)["status"], "error")
        self.assertEqual(detector.snapshot({"platform": "Linux"}, check=False)["status"], "direct")

    def test_unreachable_proxy_requests_forwarding_not_direct_fallback(self):
        with patch.object(detector.socket, "create_connection", side_effect=OSError):
            value = detector.snapshot({"platform": "Linux", "env_https": "http://localhost:7892"})
        self.assertEqual(value["status"], "error")
        self.assertTrue(value["needs_forwarding"])
        self.assertEqual(value["host_proxy"], "http://localhost:7892")

    def test_final_proxy_validation_uses_real_https_request_and_can_choose_direct(self):
        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self, size):
                self.size = size
                return b'{"ip":"fixture"}'

        response = Response()
        opener = SimpleNamespace(open=lambda request, timeout: response)
        value = {"status": "proxy", "proxy": "http://proxy.example:8080", "error": ""}
        with patch.object(detector.urllib.request, "build_opener", return_value=opener) as build:
            verified = detector.verify_proxy_request(value.copy())
        self.assertEqual(verified["status"], "proxy")
        self.assertEqual(verified["validation_target"], "api.ipify.org")
        self.assertTrue(verified["validated_at"])
        self.assertEqual(response.size, 256)
        handler = build.call_args.args[0]
        self.assertEqual(handler.proxies["https"], "http://proxy.example:8080")

        with patch.object(detector.urllib.request, "build_opener", side_effect=OSError("private failure")):
            failed = detector.verify_proxy_request(value.copy())
        self.assertEqual(failed["status"], "error")
        self.assertNotIn("private failure", failed["error"])
        direct = detector.force_direct(failed)
        self.assertEqual(direct["status"], "direct")
        self.assertEqual(direct["proxy"], "")
        self.assertEqual(direct["validation_target"], "DIRECT")

    def test_remote_environment_has_no_host_rewrite_or_forwarder(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'snapshot.json'
            with patch('prediction_market_agent.plugin_system.config_io.getproxies', return_value={
                    'https':'http://localhost:8118', 'no':'internal.example'}):
                value = client_proxy('HOST', path)
                self.assertEqual(value['proxy'], 'http://localhost:8118')
                self.assertIn('当前运行环境', value['source'])
                detector.write_snapshot(path, {'status':'proxy','proxy':'http://host.docker.internal:9999'})
                self.assertEqual(client_proxy('ENVIRONMENT',path)['proxy'],'http://localhost:8118')
                self.assertEqual(client_proxy('HOST',path)['proxy'],'http://host.docker.internal:9999')
            with patch('prediction_market_agent.plugin_system.config_io.getproxies',return_value={'all':'socks5://localhost:1080'}):
                with self.assertRaises(ValueError):client_proxy('ENVIRONMENT',path)

    def test_private_snapshot_manual_override_and_scoped_child_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "host-proxy.json"
            value = {"status": "proxy", "proxy": "http://relay:fixture-token@host.docker.internal:8888",
                     "source": "system", "no_proxy": "example.org"}
            detector.write_snapshot(path, value)
            result = client_proxy("HOST", path)
            self.assertNotIn("fixture-token", result["display"])
            self.assertIn("example.org", result["no_proxy"])
            self.assertFalse(client_proxy("DIRECT", path)["proxy"])
            self.assertEqual(client_proxy("http://manual.example:1234", path)["source"], "插件独立设置")
            env = subprocess_environment(result["proxy"], result["no_proxy"])
            for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
                self.assertEqual(env[key], value["proxy"])
            self.assertIn("localhost", env["NO_PROXY"])
            self.assertNotIn("BINANCE_API_KEY", env)
            value["status"] = "error"
            detector.write_snapshot(path, value)
            with self.assertRaises(ValueError):
                client_proxy("HOST", path)
            self.assertFalse(client_proxy("DIRECT", path)["proxy"])

    def test_shared_proxy_is_inherited_but_plugin_override_wins(self):
        inherited = resolve_proxy_settings(
            "INHERIT",
            field_name="PLUGIN_PROXY",
            inherited_value="http://shared.example:8080",
            inherited_no_proxy="shared.internal",
        )
        self.assertEqual(inherited["proxy"], "http://shared.example:8080")
        self.assertIn("shared.internal", inherited["no_proxy"])
        self.assertEqual(inherited["inherited"], "true")

        direct = resolve_proxy_settings(
            "DIRECT",
            field_name="PLUGIN_PROXY",
            inherited_value="http://shared.example:8080",
            inherited_no_proxy="shared.internal",
        )
        self.assertEqual(direct["proxy"], "")
        self.assertNotIn("shared.internal", direct["no_proxy"])
        self.assertEqual(direct["inherited"], "false")

    def test_relative_host_snapshot_is_resolved_from_working_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / ".deployment" / "host-proxy.json"
            detector.write_snapshot(
                snapshot,
                {
                    "status": "proxy",
                    "proxy": "http://shared.example:8080",
                    "source": "fixture",
                    "no_proxy": "",
                },
            )
            package = ROOT / "src" / "prediction_market_agent"
            directories = {kind: () for kind in (
                "api", "decision_provider", "decision_strategy", "research_tool", "risk", "hook"
            )}
            directories["api"] = (package / "plugins" / "api",)
            directory_config = PluginDirectoryConfig(
                root / "dirs.json",
                directories,
                root / "dirs.json",
            )
            catalog = discover_plugin_catalog(
                directory_config,
                enabled={"api": ("binance",)},
                working_directory=root,
                shared_http_proxy="HOST",
                host_proxy_file=Path(".deployment/host-proxy.json"),
            )
            try:
                route = catalog.get("api", "binance").network_routes_callback()[0]
                self.assertEqual(route.proxy, "http://shared.example:8080")
            finally:
                catalog.shutdown()

    def test_platform_and_research_plugins_default_to_shared_proxy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context = lambda kind, name: PluginInitializationContext(
                kind,
                root / f"{name}.py",
                root,
                shared_http_proxy="http://shared.example:8080",
            )
            specs = (
                binance.initialize_plugin(context("api", "binance")),
                polymarket.initialize_plugin(context("api", "polymarket")),
                standard_research.initialize_plugin(
                    context("research_tool", "standard_research")
                ),
            )
            try:
                for spec in specs:
                    with self.subTest(plugin=spec.name):
                        proxy_field = next(
                            field
                            for field in spec.configuration.manifest()["fields"]
                            if field["name"].endswith("HTTP_PROXY")
                        )
                        self.assertEqual(proxy_field["value"], "INHERIT")
                        self.assertEqual(
                            spec.network_routes_callback()[0].proxy,
                            "http://shared.example:8080",
                        )
            finally:
                for spec in specs:
                    spec.teardown()

    def test_framing_preserves_characters_without_shell_evaluation(self):
        value = "http://name:p%24%28cmd%29@proxy.example:8080"
        framed = "env_https\t" + base64.b64encode(value.encode()).decode()
        self.assertEqual(detector.frames(framed)["env_https"], value)

    def test_container_monitor_uses_launcher_docker_permissions(self):
        spec = importlib.util.spec_from_file_location("proxy_forward", ROOT / "deploy/proxy-forward.py")
        relay = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(relay)
        with patch.object(relay.subprocess, "run") as command:
            command.return_value = subprocess.CompletedProcess([], 0, "true\n")
            self.assertTrue(relay.container_running("fixture", docker_sudo=True))
            self.assertEqual(command.call_args.args[0][:3], ["sudo", "-n", "docker"])
            command.return_value = subprocess.CompletedProcess([], 0, "false\n")
            self.assertFalse(relay.container_running("fixture"))
            self.assertEqual(command.call_args.args[0][0], "docker")

    def test_authenticated_forwarder_binary_tunnel_and_shutdown(self):
        received = []
        class Upstream(socketserver.BaseRequestHandler):
            def handle(self):
                header = b""
                self.request.settimeout(3)
                while not header.endswith(b"\r\n\r\n"):
                    data = self.request.recv(1)
                    if not data:
                        return
                    header += data
                received.append(header)
                self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                data = self.request.recv(1000)
                self.request.sendall(data)
        upstream = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Upstream)
        upstream.daemon_threads = True
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        self.addCleanup(upstream.server_close)
        self.addCleanup(upstream.shutdown)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot, ready = root / "host-proxy.json", root / "ready"
            value = {"captured_at": "fixture-capture", "host_proxy": f"http://user:upstream-pass@127.0.0.1:{upstream.server_address[1]}",
                     "source": "fixture", "proxy": "", "status": "error", "needs_forwarding": True, "error": ""}
            detector.write_snapshot(snapshot, value)
            if os.environ.get("TEST_HELPER_SHELL") == "powershell":
                args = [os.environ.get("TEST_HELPER_EXECUTABLE", "powershell"), "-NoProfile", "-File",
                        str(ROOT / "deploy/proxy-forward.ps1"), "-Snapshot", str(snapshot),
                        "-Bind", "127.0.0.1", "-ContainerHost", "127.0.0.1", "-ReadyFile", str(ready)]
            else:
                args = [sys.executable, str(ROOT / "deploy/proxy-forward.py"), str(snapshot),
                        "--bind", "127.0.0.1", "--container-host", "127.0.0.1", "--ready-file", str(ready)]
            process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 15
                while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertTrue(ready.exists(), process.communicate(timeout=3) if process.poll() is not None else "not ready")
                configured = json.loads(snapshot.read_text())
                proxy = urlsplit(configured["proxy"])
                with socket.create_connection((proxy.hostname, proxy.port), timeout=3) as client:
                    client.sendall(b"CONNECT example.test:443 HTTP/1.1\r\nHost: example.test\r\n\r\n")
                    self.assertIn(b"407", client.recv(1000))
                auth = base64.b64encode((proxy.username + ":" + proxy.password).encode())
                with socket.create_connection((proxy.hostname, proxy.port), timeout=3) as client:
                    client.sendall(b"CONNECT example.test:443 HTTP/1.1\r\nHost: example.test\r\nProxy-Authorization: Basic " + auth + b"\r\n\r\n")
                    self.assertIn(b"200", client.recv(1000))
                    client.sendall(b"\x00\xffTLS-like-payload")
                    self.assertEqual(client.recv(1000), b"\x00\xffTLS-like-payload")
                self.assertEqual(len(received), 1)
                self.assertNotIn(auth, received[0])
                self.assertIn(base64.b64encode(b"user:upstream-pass"), received[0])
                configured["captured_at"] = "new-capture"
                detector.write_snapshot(snapshot, configured)
                out, err = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, err.decode(errors="replace"))
                self.assertNotIn(proxy.password.encode(), out + err)
                with socket.socket() as client:
                    self.assertNotEqual(client.connect_ex((proxy.hostname, proxy.port)), 0)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()


if __name__ == "__main__":
    unittest.main()
