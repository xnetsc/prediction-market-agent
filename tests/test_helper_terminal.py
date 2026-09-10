from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from prediction_market_agent.plugins.providers._helper_scripts import command_for, render_script
from prediction_market_agent.plugins.providers._login_helper import exchange as helper_exchange
from prediction_market_agent.plugins.providers._login_relay import seal, unseal


class HelperTerminalTests(unittest.TestCase):
    """Select Bash or PowerShell explicitly; CI runs each OS, never skips a missing runtime."""

    def setUp(self):
        self.platform = os.environ.get("TEST_HELPER_SHELL", "bash")
        self.executable = os.environ.get("TEST_HELPER_EXECUTABLE", self.platform)
        self.assertIsNotNone(shutil.which(self.executable), "Required shell is missing")
        self.body = b""
        self.ready = threading.Event()
        self.completed = False
        self.queries = []
        self.errors = []
        self.secret = "fixture-not-a-real-login"
        self.tamper_response = False
        self.response_status = 200
        test = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.headers.get("Authorization") != "Bearer fixture-ticket":
                    self.send_error(401)
                    return
                self.send_response(test.response_status)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(test.body)))
                self.end_headers()
                self.wfile.write(test.body)

            def do_POST(self):
                try:
                    envelope = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    test.assertEqual(self.headers.get("Authorization"), "Bearer " + test.secret)
                    payload = unseal(test.secret, envelope, "helper-request")
                    if payload["action"] == "poll":
                        result = {"redirect_uri": test.redirect, "completed": test.completed}
                    else:
                        result = {"ok": True}
                        if payload["action"] == "ready":
                            test.ready.set()
                        if payload["action"] == "callback":
                            test.queries.append(payload["query"])
                    response = seal(test.secret, result, "helper-response",
                                    algorithm=envelope.get("algorithm", "AES-GCM"))
                    if test.tamper_response:
                        response["ciphertext"] = base64.b64encode(b"invalid").decode()
                    raw = json.dumps(response).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except Exception as error:
                    test.errors.append(error)
                    self.send_error(400)

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        with socket.socket() as reserve:
            reserve.bind(("127.0.0.1", 0))
            self.port = reserve.getsockname()[1]
        self.redirect = f"http://localhost:{self.port}/callback"
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}/api/plugin-helper/decision_provider/fixture"

    def launch(self, script, *, body=None):
        self.body = script if body is None else body
        command = command_for(self.endpoint + "/script/" + self.platform, "fixture-ticket",
                              self.platform, hashlib.sha256(script).hexdigest())
        args = ([self.executable, "-c", command] if self.platform == "bash" else
                [self.executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
                 "[Console]::OutputEncoding = [Text.Encoding]::UTF8; " + command])
        env = {**os.environ, "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]}
        process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            start_new_session=os.name == "posix",
        )

        def cleanup():
            if process.poll() is None:
                self.stop_process(process)
            process.communicate()
        self.addCleanup(cleanup)
        return process

    @staticmethod
    def stop_process(process):
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()

    def helper(self, expires=90):
        config = {"endpoint": self.endpoint, "secret": self.secret, "expires_at": time.time() + expires}
        return self.launch(render_script(self.platform, config).encode("utf-8"))

    def wait_ready(self, process):
        if self.ready.wait(60):
            return
        if process.poll() is None:
            self.stop_process(process)
        out, err = process.communicate(timeout=5)
        self.fail(
            "helper not ready; stdout=" + out.decode(errors="replace")[-1000:]
            + "; stderr=" + err.decode(errors="replace")[-1000:]
        )

    def test_pipeline_preserves_quotes_unicode_backslashes_and_line_endings(self):
        literal = """引号 ' " $HOME $(printf BAD) `printf BAD` \\ \t"""
        if self.platform == "bash":
            source = ("printf '%s\\n' 'BEGIN'\ncat <<'LITERAL'\n" + literal + "\nLITERAL\n").encode()
        else:
            source = ("[Console]::WriteLine('BEGIN')\r\n[Console]::WriteLine(@'\r\n" + literal + "\r\n'@)\r\n").encode()
        process = self.launch(source)
        out, err = process.communicate(timeout=15)
        self.assertEqual(process.returncode, 0, err.decode(errors="replace"))
        self.assertIn(literal, out.decode("utf-8").replace("\r\n", "\n"))
        self.assertIn("BEGIN", out.decode("utf-8"))

    def test_loopback_management_endpoint_ignores_unreachable_system_proxy(self):
        config = {
            "endpoint": self.endpoint,
            "secret": self.secret,
            "expires_at": time.time() + 20,
        }
        poisoned_proxy = {
            "HTTP_PROXY": "http://127.0.0.1:1",
            "HTTPS_PROXY": "http://127.0.0.1:1",
            "http_proxy": "http://127.0.0.1:1",
            "https_proxy": "http://127.0.0.1:1",
            "NO_PROXY": "",
            "no_proxy": "",
        }
        with patch.dict(os.environ, poisoned_proxy, clear=False):
            result = helper_exchange(config, {"action": "poll"})
        self.assertEqual(result["redirect_uri"], self.redirect)
        self.assertIn("$request.Proxy = $null", render_script("powershell", config))

    def test_corruption_truncation_and_http_failure_never_execute(self):
        script = b"echo SHOULD_NEVER_EXECUTE\n"
        for body, status in ((script + b"\n", 200), (script[:8], 200), (script, 500)):
            with self.subTest(status=status, length=len(body)):
                self.response_status = status
                process = self.launch(script, body=body)
                out, _ = process.communicate(timeout=15)
                self.assertNotEqual(process.returncode, 0)
                self.assertNotIn(b"SHOULD_NEVER_EXECUTE", out)

    def test_terminal_script_forwards_encrypted_callback_and_releases_port(self):
        process = self.helper()
        self.wait_ready(process)
        with urllib.request.urlopen(self.redirect + "?state=fixture&code=fixture-only", timeout=10) as response:
            self.assertEqual(response.status, 200)
        out, err = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, err.decode(errors="replace"))
        self.assertEqual(self.queries, ["state=fixture&code=fixture-only"])
        self.assertFalse(self.errors)
        self.assertNotIn(self.secret.encode(), out + err)
        self.assertNotIn(b"fixture-only", out + err)
        self.assert_port_closed()

    def test_cancel_releases_listener_without_a_callback(self):
        process = self.helper()
        self.wait_ready(process)
        self.completed = True
        process.communicate(timeout=10)
        self.assert_port_closed()

    def test_expiry_occupied_port_and_modified_encryption_fail(self):
        expired = self.helper(expires=-1)
        expired.communicate(timeout=10)
        self.assertNotEqual(expired.returncode, 0)
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", self.port))
            occupied.listen()
            process = self.helper()
            process.communicate(timeout=10)
            self.assertNotEqual(process.returncode, 0)
            self.assertFalse(self.ready.is_set())
        self.tamper_response = True
        process = self.helper()
        process.communicate(timeout=10)
        self.assertNotEqual(process.returncode, 0)
        self.assert_port_closed()

    def assert_port_closed(self):
        with socket.socket() as client:
            client.settimeout(.2)
            self.assertNotEqual(client.connect_ex(("127.0.0.1", self.port)), 0)

    def test_cbc_authentication_rejects_tampering_and_wrong_direction(self):
        envelope = seal(self.secret, {"action": "poll"}, "helper-request", algorithm="AES-CBC-HMAC-SHA256")
        self.assertEqual(unseal(self.secret, envelope, "helper-request"), {"action": "poll"})
        for field in ("nonce", "ciphertext", "mac"):
            with self.subTest(field=field):
                changed = dict(envelope)
                raw = bytearray(base64.b64decode(changed[field]))
                raw[0] ^= 1
                changed[field] = base64.b64encode(raw).decode()
                with self.assertRaises(ValueError):
                    unseal(self.secret, changed, "helper-request")
        with self.assertRaises(ValueError):
            unseal(self.secret, envelope, "helper-response")


if __name__ == "__main__":
    unittest.main()
