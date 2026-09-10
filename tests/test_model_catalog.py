from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from prediction_market_agent.plugin_system.discovery import PluginInitializationContext
from prediction_market_agent.plugins.providers.openai_compatible import initialize_plugin, model_choices


class ModelCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.spec = initialize_plugin(PluginInitializationContext("decision_provider", root / "openai_compatible.py", root))
        self.seen = []
        seen = self.seen
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append((self.path, self.headers.get("Authorization")))
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"choices": [{"message": {"content": '{"action":"HOLD"}'}}]}).encode())
            def do_GET(self):
                seen.append((self.path, self.headers.get("Authorization")))
                if self.path.startswith("/redirect"):
                    self.send_response(302)
                    self.send_header("Location", "/stolen")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"data": [{"id": "vendor/model-a", "name": "Model A"}, {"id": "model-b"}]}).encode())
            def log_message(self, *_):
                pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def test_openrouter_default_has_no_key_or_model_and_is_not_ready(self):
        values = self.spec.configuration.load()
        self.assertEqual(values["COMPATIBLE_API_BASE"], "https://openrouter.ai/api/v1")
        self.assertFalse(values.get("COMPATIBLE_API_KEY"))
        self.assertFalse(values["COMPATIBLE_MODEL"])
        self.assertEqual(values["COMPATIBLE_HTTP_PROXY"], "DIRECT")
        self.assertEqual(self.spec.network_routes_callback()[0].proxy, "")
        self.assertFalse(self.spec.readiness().ready)
        field = next(x for x in self.spec.configuration.manifest()["fields"] if x["name"] == "COMPATIBLE_MODEL")
        self.assertTrue(field["dynamic_choices"])

    def test_models_use_saved_url_key_and_allow_public_list(self):
        values = {**self.spec.configuration.load(), "COMPATIBLE_API_BASE": f"http://127.0.0.1:{self.server.server_port}/v1"}
        self.spec.configuration.save(values)
        choices = self.spec.configuration.choices_callback("COMPATIBLE_MODEL")
        self.assertEqual({x["value"] for x in choices}, {"vendor/model-a", "model-b"})
        self.assertEqual(self.seen[-1], ("/v1/models", None))
        self.spec.configuration.save({**values, "COMPATIBLE_API_KEY": "fixture-key", "COMPATIBLE_MODEL": "vendor/model-a"})
        self.spec.configuration.choices_callback("COMPATIBLE_MODEL")
        self.assertEqual(self.seen[-1], ("/v1/models", "Bearer fixture-key"))
        self.assertEqual(self.spec.factory(None).model, "vendor/model-a")
        self.assertNotIn("fixture-key", json.dumps(self.spec.manifest()))

    def test_model_redirect_never_forwards_key_and_errors_are_sanitized(self):
        values = {**self.spec.configuration.load(), "COMPATIBLE_API_BASE": f"http://127.0.0.1:{self.server.server_port}/redirect",
                  "COMPATIBLE_API_KEY": "fixture-key"}
        with self.assertRaisesRegex(ValueError, "HTTP 302"):
            model_choices(values)
        self.assertEqual(len(self.seen), 1)

    def test_switching_preset_clears_previous_secret_atomically(self):
        configuration = self.spec.configuration
        configuration.save({**configuration.load(), "COMPATIBLE_API_KEY": "old-service-key"})
        preset = configuration.presets[0]["values"]
        configuration.save(preset, clear_secrets=["COMPATIBLE_API_KEY"])
        self.assertFalse(configuration.load().get("COMPATIBLE_API_KEY"))
        configuration.save({**preset, "COMPATIBLE_API_KEY": "new-service-key"}, clear_secrets=["COMPATIBLE_API_KEY"])
        self.assertEqual(configuration.load()["COMPATIBLE_API_KEY"], "new-service-key")
        with self.assertRaises(ValueError):
            configuration.save(preset, clear_secrets=["COMPATIBLE_MODEL"])
