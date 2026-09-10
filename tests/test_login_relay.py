from __future__ import annotations

import base64
import json
import socket
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from prediction_market_agent.core.config import Config
from prediction_market_agent.plugins.providers._login_relay import BrowserLoginRelay, seal, unseal
from prediction_market_agent.plugins.providers._login_helper import run
from prediction_market_agent.runtime.dashboard import create_app


class LoginRelayTests(unittest.TestCase):
    def callback_server(self, location="/success", completion_status=200):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                received.append(self.path)
                if self.path.startswith("/auth/callback?"):
                    self.send_response(302)
                    if location is not None:
                        self.send_header("Location", location)
                else:
                    self.send_response(completion_status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        relay = BrowserLoginRelay(60, completion_paths=("/success",))
        relay.configure(f"https://auth.openai.com/authorize?state=fixture&redirect_uri="
                        f"http://127.0.0.1:{server.server_port}/auth/callback")
        relay.ready = True
        self.addCleanup(relay.close)
        return relay, received

    def test_callback_follows_cli_completion_before_reporting_delivery(self):
        for mode in ("direct", "helper"):
            with self.subTest(mode=mode):
                relay, received = self.callback_server("/success?view=complete")
                if mode == "direct":
                    relay.forward_callback("state=fixture&code=test-approval")
                else:
                    result = relay.handle(relay.secret, seal(relay.secret, {
                        "action": "callback", "query": "state=fixture&code=test-approval"
                    }, "helper-request"))
                    self.assertTrue(unseal(relay.secret, result, "helper-response")["ok"])
                self.assertEqual(received, ["/auth/callback?state=fixture&code=test-approval",
                                            "/success?view=complete"])
                self.assertTrue(relay.consumed)
                with self.assertRaises(ValueError):
                    relay.forward_callback("state=fixture&code=test-approval")

    def test_completion_redirect_cannot_leave_original_listener_or_allowed_path(self):
        for target in ("https://external.invalid/success", "http://127.0.0.1:1/success",
                       "http://localhost:1/success", "/cancel", "/auth/callback", "/success#fragment",
                       "http://user:password@127.0.0.1:1/success"):
            with self.subTest(target=target):
                relay, received = self.callback_server(target)
                with self.assertRaises(ValueError):
                    relay.forward_callback("state=fixture&code=test-approval")
                self.assertEqual(len(received), 1)
                self.assertFalse(relay.consumed)

    def test_incomplete_redirect_and_failed_completion_are_not_success(self):
        for location, status in ((None, 200), ("/success", 500), ("/success", 302)):
            with self.subTest(location=location, status=status):
                relay, _ = self.callback_server(location, status)
                with self.assertRaises(ValueError):
                    relay.forward_callback("state=fixture&code=test-approval")
                self.assertFalse(relay.consumed)

    def test_capability_nonce_expiry_and_close(self):
        relay = BrowserLoginRelay(60)
        envelope = seal(relay.secret, {"action": "poll"}, "helper-request")
        with self.assertRaises(ValueError):
            relay.handle("wrong-secret", envelope)
        answer = relay.handle(relay.secret, envelope)
        self.assertFalse(unseal(relay.secret, answer, "helper-response")["completed"])
        with self.assertRaisesRegex(ValueError, "Repeated"):
            relay.handle(relay.secret, envelope)
        with patch("prediction_market_agent.plugins.providers._login_relay.time.time", return_value=relay.expires_at + 1):
            with self.assertRaises(ValueError):
                relay.handle(relay.secret, seal(relay.secret, {"action": "poll"}, "helper-request"))
        relay.close()
        with self.assertRaises(ValueError):
            relay.handle(relay.secret, seal(relay.secret, {"action": "poll"}, "helper-request"))

    def test_callback_target_and_oauth_state_cannot_be_replaced(self):
        relay = BrowserLoginRelay(60)
        self.assertFalse(relay.configure("https://auth.openai.com/authorize?state=x&redirect_uri=https://evil.example/callback"))
        self.assertFalse(relay.configure("https://auth.openai.com/authorize?state=x&redirect_uri=http://localhost:1234/admin"))
        self.assertTrue(relay.configure("https://auth.openai.com/authorize?state=x&redirect_uri=http://localhost:1234/auth/callback"))
        self.assertFalse(relay.status()["authorization_url"])
        relay.handle(relay.secret, seal(relay.secret, {"action": "ready", "redirect_uri": relay.redirect_uri}, "helper-request"))
        with self.assertRaisesRegex(ValueError, "state mismatch"):
            relay.handle(relay.secret, seal(relay.secret, {"action": "callback", "query": "state=wrong&code=anything"}, "helper-request"))

    def test_script_capability_is_single_use_bound_to_shell_origin_and_flow(self):
        relay = BrowserLoginRelay(60, origin="https://robot.example")
        for platform in ("bash", "powershell"):
            command = relay.script_command(relay.origin, platform, "codex")["command"]
            ticket = next(iter(relay.script_tickets))
            self.assertIn(ticket, command)
            self.assertNotIn(relay.secret, command)
            with self.assertRaises(ValueError):
                relay.script(ticket, "wrong-shell")
            script = relay.script(ticket, platform)["script"]
            self.assertNotIn("@@CONFIG@@", script)
            with self.assertRaises(ValueError):
                relay.script(ticket, platform)
        for origin, shell in (("http://public.example", "bash"), ("https://other.example", "bash"),
                              (relay.origin, "../bash")):
            with self.assertRaises(ValueError):
                relay.script_command(origin, shell, "codex")
        relay.script_command(relay.origin, "bash", "codex")
        ticket = next(iter(relay.script_tickets))
        relay.close()
        with self.assertRaises(ValueError):
            relay.script(ticket, "bash")
        self.assertNotIn(relay.secret, json.dumps(relay.status()))

    def test_standalone_helper_forwards_browser_callback_without_code_entry(self):
        reserve = socket.socket()
        reserve.bind(("127.0.0.1", 0))
        callback_port = reserve.getsockname()[1]
        reserve.close()
        ready = threading.Event()
        received = []
        secret = "test-pairing-capability"
        redirect = f"http://localhost:{callback_port}/callback"
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                envelope = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                message = unseal(secret, envelope, "helper-request")
                if message["action"] == "poll":
                    response = {"redirect_uri": redirect, "completed": False}
                else:
                    response = {"ok": True}
                    if message["action"] == "ready":
                        ready.set()
                    if message["action"] == "callback":
                        received.append(message["query"])
                raw = json.dumps(seal(secret, response, "helper-response")).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            def log_message(self, *_):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        config = {"endpoint": f"http://127.0.0.1:{server.server_port}/api/plugin-helper/decision_provider/test",
                  "secret": secret, "expires_at": time.time() + 10}
        errors = []
        def helper():
            try:
                run(config)
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=helper, daemon=True)
        try:
            thread.start()
            self.assertTrue(ready.wait(5))
            with urllib.request.urlopen(redirect + "?state=fixture&code=fixture-approval", timeout=5) as response:
                self.assertEqual(response.status, 200)
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertFalse(errors)
            self.assertEqual(received, ["state=fixture&code=fixture-approval"])
        finally:
            server.shutdown()
            server.server_close()

    def test_helper_releases_listener_when_server_flow_ends_without_a_callback(self):
        with socket.socket() as reserve:
            reserve.bind(("127.0.0.1", 0))
            port = reserve.getsockname()[1]
        redirect = f"http://localhost:{port}/callback"
        config = {"endpoint": "https://robot.example/api/plugin-helper/decision_provider/test",
                  "secret": "fixture-capability", "expires_at": time.time() + 60}
        replies = [{"redirect_uri": redirect, "completed": False}, {"ok": True},
                   {"redirect_uri": redirect, "completed": True}]
        with patch("prediction_market_agent.plugins.providers._login_helper.exchange", side_effect=replies):
            with self.assertRaisesRegex(ValueError, "flow ended"):
                run(config)
        with socket.socket() as connection:
            connection.settimeout(.2)
            self.assertNotEqual(connection.connect_ex(("127.0.0.1", port)), 0)

    def test_web_helper_endpoint_is_not_a_general_authentication_bypass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = Config(working_directory=root, session_db=root / "sessions.sqlite3", auth_db=root / "auth.sqlite3",
                            management_file=root / "selection.json", plugin_directories_file=root / "dirs.json")
            app = create_app(config, start_robot=False)
            with TestClient(app, base_url="https://robot.example") as client:
                self.assertEqual(client.post("/api/plugin-helper/decision_provider/codex", json={}).status_code, 401)
                self.assertEqual(client.post("/api/secure", json={}).status_code, 401)
                self.assertEqual(client.post("/api/local", json={"url": "/api/plugins/controls"}).status_code, 403)
                relay = BrowserLoginRelay(60, origin="https://robot.example")
                relay.script_command(relay.origin, "bash", "codex")
                ticket = next(iter(relay.script_tickets))
                spec = SimpleNamespace(controls=SimpleNamespace(
                    helper_callback=lambda token, message: relay.script(token, message["script_platform"])))
                with patch("prediction_market_agent.plugin_system.discovery.PluginCatalog.get", return_value=spec):
                    url = "/api/plugin-helper/decision_provider/codex/script/bash"
                    result = client.get(url, headers={"Authorization": "Bearer " + ticket})
                    self.assertEqual(result.status_code, 200)
                    self.assertEqual(result.headers["Cache-Control"], "no-store")
                    self.assertEqual(result.headers["X-Content-Type-Options"], "nosniff")
                    self.assertIn("prediction_login_main", result.text)
                    self.assertEqual(client.get(url, headers={"Authorization": "Bearer " + ticket}).status_code, 401)
                relay.close()
            with TestClient(create_app(config, start_robot=False), base_url="http://localhost") as client:
                page = client.get("/").text
                self.assertIn("客户端网页登录", page)
                self.assertIn("复制本次登录命令", page)
                self.assertIn("copyHelperCommand", page)
                self.assertNotIn("下载助手 ZIP", page)
                self.assertEqual(client.get("/api/plugin-helper/decision_provider/codex/script/bash").status_code, 401)
                self.assertNotIn("提交官方页面授权码", page)
                result = client.post("/api/local", json={"url": "/api/plugins/controls", "body": None})
                self.assertEqual(result.status_code, 200)
                self.assertNotIn("secret", result.text)
