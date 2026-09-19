from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from prediction_market_agent.plugin_system.discovery import PluginInitializationContext
from prediction_market_agent.plugins.providers.openrouter import (
    _BridgeServer,
    _opener,
    initialize_openrouter_plugin,
    initialize_plugin,
    model_choices,
)


class ModelCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.spec = initialize_plugin(
            PluginInitializationContext(
                "decision_provider",
                self.root / "openrouter.py",
                self.root,
                shared_http_proxy="DIRECT",
            )
        )
        self.seen: list[tuple[str, str | None, dict | None]] = []
        seen = self.seen

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                seen.append((self.path, self.headers.get("Authorization"), body))
                payload = json.dumps(
                    {
                        "choices": [
                            {"message": {"content": '{"word":"ok","number":7}'}}
                        ],
                        "usage": {
                            "prompt_tokens": 11,
                            "completion_tokens": 8,
                            "total_tokens": 19,
                        },
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                seen.append((self.path, self.headers.get("Authorization"), None))
                if self.path.startswith("/redirect"):
                    self.send_response(302)
                    self.send_header("Location", "/stolen")
                    self.end_headers()
                    return
                payload = json.dumps(
                    {
                        "data": [
                            {
                                "id": "vendor/model-a",
                                "name": "Model A",
                                "supported_parameters": ["structured_outputs", "tools"],
                            },
                            {
                                "id": "vendor/plain-model",
                                "name": "Plain model",
                                "supported_parameters": ["tools"],
                            },
                        ]
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def test_openrouter_default_has_no_key_or_model_and_is_not_ready(self):
        values = self.spec.configuration.load()
        self.assertFalse(values.get("OPENROUTER_API_KEY"))
        self.assertFalse(values["OPENROUTER_MODEL"])
        self.assertEqual(values["OPENROUTER_HTTP_PROXY"], "INHERIT")
        self.assertEqual(values["OPENROUTER_MAX_OUTPUT_TOKENS"], 8192)
        self.assertFalse(self.spec.readiness().ready)
        field = next(
            x
            for x in self.spec.configuration.manifest()["fields"]
            if x["name"] == "OPENROUTER_MODEL"
        )
        self.assertTrue(field["dynamic_choices"])
        self.assertTrue(field["selection_only"])

    def test_models_require_explicit_structured_output_support(self):
        values = {
            **self.spec.configuration.load(),
            "OPENROUTER_API_KEY": "fixture-key",
        }
        url = f"http://127.0.0.1:{self.server.server_port}/models?supported_parameters=structured_outputs"
        choices = model_choices(values, models_url=url, proxy_settings={"proxy": ""})
        self.assertEqual(choices, [{"value": "vendor/model-a", "label": "Model A"}])
        self.assertEqual(self.seen[-1][0], "/models?supported_parameters=structured_outputs")
        self.assertEqual(self.seen[-1][1], "Bearer fixture-key")

    def test_model_redirect_never_forwards_key(self):
        values = {
            **self.spec.configuration.load(),
            "OPENROUTER_API_KEY": "fixture-key",
        }
        url = f"http://127.0.0.1:{self.server.server_port}/redirect"
        with self.assertRaisesRegex(ValueError, "HTTP 302"):
            model_choices(values, models_url=url, proxy_settings={"proxy": ""})
        self.assertEqual(len(self.seen), 1)

    def test_openrouter_configuration_has_no_custom_endpoint_fields(self):
        names = {
            field["name"] for field in self.spec.configuration.manifest()["fields"]
        }
        self.assertNotIn("OPENROUTER_API_BASE", names)
        self.assertNotIn("OPENROUTER_EXTRA_HEADERS_JSON", names)

    def test_generated_file_name_creates_an_independent_provider(self):
        plugin = initialize_openrouter_plugin(
            PluginInitializationContext(
                "decision_provider",
                self.root / "plugins" / "decision_provider" / "openrouter_second.py",
                self.root,
                shared_http_proxy="DIRECT",
            )
        )
        self.assertEqual(plugin.name, "openrouter_second")
        self.assertTrue(
            plugin.configuration.storage["location"].endswith("openrouter_second.json")
        )

    def test_private_bridge_translates_schema_and_requires_capable_endpoint(self):
        bridge = _BridgeServer(
            "local-token",
            "fixture-key",
            "vendor/model-a",
            _opener(""),
            10,
            4096,
            f"http://127.0.0.1:{self.server.server_port}/chat/completions",
        )
        thread = threading.Thread(target=bridge.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(bridge.server_close)
        self.addCleanup(bridge.shutdown)
        body = {
            "model": "vendor/model-a",
            "stream": True,
            "instructions": "Follow the requested schema.",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Return ok and 7"}],
                }
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "probe",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "word": {"type": "string"},
                            "number": {"type": "integer"},
                        },
                        "required": ["word", "number"],
                        "additionalProperties": False,
                    },
                }
            },
        }
        request = urllib.request.Request(
            f"http://127.0.0.1:{bridge.server_port}/v1/responses",
            data=json.dumps(body).encode(),
            headers={
                "Authorization": "Bearer local-token",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            stream = response.read().decode()
        sent = self.seen[-1][2]
        self.assertEqual(sent["provider"], {"require_parameters": True})
        self.assertEqual(sent["response_format"]["type"], "json_schema")
        self.assertTrue(sent["response_format"]["json_schema"]["strict"])
        self.assertNotIn("tools", sent)
        delta = next(
            json.loads(line.removeprefix("data: "))["delta"]
            for line in stream.splitlines()
            if line.startswith("data: ")
            and "response.output_text.delta" in line
        )
        self.assertEqual(json.loads(delta), {"word": "ok", "number": 7})
        self.assertIn("response.completed", stream)

    def test_private_bridge_preserves_inherited_proxy_and_bypasses_it_for_loopback(self):
        context = PluginInitializationContext(
            "decision_provider",
            self.root / "openrouter_proxy.py",
            self.root,
            shared_http_proxy=f"http://127.0.0.1:{self.server.server_port}",
            shared_no_proxy="internal.example",
        )
        settings = context.proxy_settings(
            "INHERIT", field_name="OPENROUTER_HTTP_PROXY"
        )
        self.assertEqual(
            settings["proxy"], f"http://127.0.0.1:{self.server.server_port}"
        )
        self.assertEqual(settings["inherited"], "true")
        self.assertIn("internal.example", settings["no_proxy"])
        self.assertIn("127.0.0.1", settings["no_proxy"])

        bridge = _BridgeServer(
            "local-token",
            "fixture-key",
            "vendor/model-a",
            _opener(settings["proxy"]),
            10,
            4096,
            "http://openrouter.invalid/chat/completions",
        )
        thread = threading.Thread(target=bridge.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(bridge.server_close)
        self.addCleanup(bridge.shutdown)
        body = {
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Return JSON"}],
                }
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "strict": True,
                    "schema": {"type": "object"},
                }
            },
        }
        request = urllib.request.Request(
            f"http://127.0.0.1:{bridge.server_port}/v1/responses",
            data=json.dumps(body).encode(),
            headers={
                "Authorization": "Bearer local-token",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            self.assertEqual(response.status, 200)

        self.assertEqual(self.seen[-1][0], "http://openrouter.invalid/chat/completions")
        self.assertEqual(self.seen[-1][1], "Bearer fixture-key")


if __name__ == "__main__":
    unittest.main()
