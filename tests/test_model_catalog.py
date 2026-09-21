from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from prediction_market_agent.plugin_system.discovery import PluginInitializationContext
from prediction_market_agent.plugins.providers.openrouter import (
    _BridgeServer,
    _adapt_claude_parameters,
    _adapt_server_tools,
    _opener,
    _openrouter_schema_prompt,
    _restore_namespace_calls,
    _strict_schema_object,
    OpenRouterBackend,
    OpenRouterAccountControl,
    initialize_openrouter_plugin,
    initialize_plugin,
    model_choices,
)


class ModelCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.context = PluginInitializationContext(
            "decision_provider",
            self.root / "openrouter.py",
            self.root,
            shared_http_proxy="DIRECT",
        )
        self.spec = initialize_plugin(self.context)
        self.seen: list[tuple[str, str | None, dict | None]] = []
        seen = self.seen

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                seen.append((self.path, self.headers.get("Authorization"), body))
                payload = json.dumps(
                    {"upstream": "ok", "request_path": self.path}
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
                if self.path == "/key":
                    body = {
                        "data": {
                            "usage": 25.5,
                            "usage_daily": 1.5,
                            "usage_weekly": 7.5,
                            "usage_monthly": 20.5,
                            "limit": 100,
                            "limit_remaining": 74.5,
                            "limit_reset": "monthly",
                            "is_free_tier": False,
                            "expires_at": "2027-12-31T23:59:59Z",
                        }
                    }
                elif self.path == "/credits":
                    body = {"data": {"total_credits": 100.5, "total_usage": 25.75}}
                else:
                    body = {
                        "data": [
                            {
                                "id": "vendor/model-a",
                                "name": "Model A",
                                "supported_parameters": [
                                    "structured_outputs",
                                    "tools",
                                    "web_search_options",
                                ],
                            },
                            {
                                "id": "anthropic/model-a",
                                "name": "Anthropic Model A",
                                "supported_parameters": ["structured_outputs", "tools"],
                            },
                            {
                                "id": "vendor/plain-model",
                                "name": "Plain model",
                                "supported_parameters": ["tools"],
                            },
                        ]
                    }
                payload = json.dumps(body).encode()
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
        self.assertFalse(values.get("OPENROUTER_MANAGEMENT_API_KEY"))
        self.assertFalse(values["OPENROUTER_MODEL"])
        self.assertEqual(values["OPENROUTER_AGENT_CLI"], "AUTO")
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

    def test_openrouter_controls_report_key_usage_and_optional_account_balance(self):
        self.spec.configuration.save(
            {
                "OPENROUTER_API_KEY": "fixture-inference-key",
                "OPENROUTER_MANAGEMENT_API_KEY": "fixture-management-key",
                "OPENROUTER_MODEL": "vendor/model-a",
            }
        )
        control = OpenRouterAccountControl(
            self.spec.configuration,
            self.context,
            key_url=f"http://127.0.0.1:{self.server.server_port}/key",
            credits_url=f"http://127.0.0.1:{self.server.server_port}/credits",
        )
        with patch(
            "prediction_market_agent.plugins.providers.openrouter.resolve_executable",
            return_value="/fixture/agent-cli",
        ):
            initial = control.snapshot()
        self.assertEqual(initial["control_type"], "openrouter")
        self.assertEqual(initial["state"], "configured")
        self.assertNotIn("fixture-inference-key", json.dumps(initial))
        self.assertEqual(initial["agent_cli"]["selected"], "CODEX")
        result = control.action("refresh_usage", {})
        self.assertEqual(result["usage"]["key"]["limit_remaining"], 74.5)
        self.assertEqual(result["usage"]["account"]["balance"], 74.75)
        self.assertEqual(
            [(path, authorization) for path, authorization, _ in self.seen],
            [
                ("/key", "Bearer fixture-inference-key"),
                ("/credits", "Bearer fixture-management-key"),
            ],
        )

    def test_openrouter_account_balance_is_skipped_without_management_key(self):
        self.spec.configuration.save(
            {
                "OPENROUTER_API_KEY": "fixture-inference-key",
                "OPENROUTER_MODEL": "vendor/model-a",
            }
        )
        control = OpenRouterAccountControl(
            self.spec.configuration,
            self.context,
            key_url=f"http://127.0.0.1:{self.server.server_port}/key",
            credits_url=f"http://127.0.0.1:{self.server.server_port}/credits",
        )
        result = control.action("refresh_usage", {})
        self.assertIn("Management Key", result["usage"]["account_note"])
        self.assertEqual([entry[0] for entry in self.seen], ["/key"])

    def test_models_require_explicit_structured_output_support(self):
        values = {
            **self.spec.configuration.load(),
            "OPENROUTER_API_KEY": "fixture-key",
        }
        url = f"http://127.0.0.1:{self.server.server_port}/models?supported_parameters=structured_outputs"
        choices = model_choices(values, models_url=url, proxy_settings={"proxy": ""})
        self.assertEqual(
            choices,
            [
                {"value": "anthropic/model-a", "label": "Anthropic Model A"},
                {"value": "vendor/model-a", "label": "Model A"},
            ],
        )
        self.assertEqual(self.seen[-1][0], "/models?supported_parameters=structured_outputs")
        self.assertEqual(self.seen[-1][1], "Bearer fixture-key")

    def test_models_without_schema_tool_input_are_hidden(self):
        values = {
            **self.spec.configuration.load(),
            "OPENROUTER_API_KEY": "fixture-key",
        }
        url = f"http://127.0.0.1:{self.server.server_port}/models?supported_parameters=structured_outputs"
        choices = model_choices(values, models_url=url, proxy_settings={"proxy": ""})
        self.assertNotIn("vendor/plain-model", {item["value"] for item in choices})

    def test_model_choices_follow_the_selected_agent_cli(self):
        url = f"http://127.0.0.1:{self.server.server_port}/models?supported_parameters=structured_outputs"
        codex = model_choices(
            {"OPENROUTER_AGENT_CLI": "CODEX"},
            models_url=url,
            proxy_settings={"proxy": ""},
        )
        claude = model_choices(
            {"OPENROUTER_AGENT_CLI": "CLAUDE"},
            models_url=url,
            proxy_settings={"proxy": ""},
        )
        expected = ["anthropic/model-a", "vendor/model-a"]
        self.assertEqual([item["value"] for item in codex], expected)
        self.assertEqual([item["value"] for item in claude], expected)

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

    def test_generated_provider_reuses_primary_key_unless_overridden(self):
        self.spec.configuration.save({
            "OPENROUTER_API_KEY": "shared-key",
            "OPENROUTER_MODEL": "vendor/model-a",
        })
        plugin = initialize_openrouter_plugin(
            PluginInitializationContext(
                "decision_provider",
                self.root / "plugins" / "decision_provider" / "openrouter_second.py",
                self.root,
                shared_http_proxy="DIRECT",
            )
        )
        plugin.configuration.save({
            "OPENROUTER_API_KEY": "",
            "OPENROUTER_MODEL": "vendor/model-b",
            "OPENROUTER_HTTP_PROXY": "DIRECT",
        })

        with patch(
            "prediction_market_agent.plugins.providers.openrouter.resolve_executable",
            side_effect=lambda value: "/fixture/" + value,
        ):
            shared = plugin.factory(None)
        self.assertEqual(shared.api_key, "shared-key")
        self.assertEqual(shared.model, "vendor/model-b")

        plugin.configuration.save({
            "OPENROUTER_API_KEY": "own-key",
            "OPENROUTER_MODEL": "vendor/model-b",
            "OPENROUTER_HTTP_PROXY": "DIRECT",
        })
        with patch(
            "prediction_market_agent.plugins.providers.openrouter.resolve_executable",
            side_effect=lambda value: "/fixture/" + value,
        ):
            self.assertEqual(plugin.factory(None).api_key, "own-key")

    def test_codex_agent_keeps_cli_tooling_and_uses_transparent_responses_proxy(self):
        seen = {}

        def run(command, **kwargs):
            # Patching subprocess.run changes the shared module object, so an unrelated background
            # host-proxy refresh may also pass through here during the full suite. Record only the
            # CLI invocation this test owns.
            if "--output-last-message" in command:
                seen.update(command=command, kwargs=kwargs)
                output = Path(command[command.index("--output-last-message") + 1])
                output.write_text('{"answer":"ok"}', encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")

        with patch(
            "prediction_market_agent.plugins.providers.openrouter.resolve_executable",
            side_effect=lambda value: "/fixture/" + value,
        ), patch(
            "prediction_market_agent.plugins.providers.openrouter.subprocess.run",
            side_effect=run,
        ):
            backend = OpenRouterBackend(
                "openrouter",
                "codex",
                "fixture-key",
                "vendor/model-a",
                {"proxy": "", "no_proxy": "localhost,127.0.0.1,::1"},
                10,
                4096,
                agent_cli="CODEX",
                cli_paths={"CODEX": "codex", "CLAUDE": "claude"},
                client_homes={
                    "CODEX": self.root / "codex-auth" / ".codex",
                    "CLAUDE": self.root / "claude-auth" / ".claude",
                },
            )
            result = backend.complete(
                "Use the official tools when useful.",
                {
                    "type": "object",
                    "properties": {"answer": {"type": "string"}},
                    "required": ["answer"],
                    "additionalProperties": False,
                },
                "answer",
            )
        self.assertEqual(result.value, {"answer": "ok"})
        self.assertIn("--search", seen["command"])
        self.assertIn("workspace-write", seen["command"])
        self.assertNotIn("--ignore-rules", seen["command"])
        self.assertEqual(
            seen["command"][seen["command"].index("-m") + 1],
            "vendor/model-a",
        )
        self.assertIn('model_providers.openrouter_bridge.wire_api="responses"', seen["command"])
        self.assertEqual(
            seen["kwargs"]["env"].get("HOME"),
            str((self.root / "codex-auth").resolve()),
        )
        self.assertEqual(
            seen["kwargs"]["env"].get("CODEX_HOME"),
            str((self.root / "codex-auth" / ".codex").resolve()),
        )

    def test_auto_agent_falls_back_to_claude_and_preserves_cli_configuration(self):
        seen = {}

        def resolve(value):
            if value == "codex-missing":
                from prediction_market_agent.agent.decision import DecisionProviderError
                raise DecisionProviderError("missing codex")
            return "/fixture/claude"

        def run(command, **kwargs):
            seen.update(command=command, kwargs=kwargs)
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps({"structured_output": {"answer": "ok"}}),
                "",
            )

        with patch(
            "prediction_market_agent.plugins.providers.openrouter.resolve_executable",
            side_effect=resolve,
        ), patch(
            "prediction_market_agent.plugins.providers.openrouter.subprocess.run",
            side_effect=run,
        ):
            backend = OpenRouterBackend(
                "openrouter",
                "codex-missing",
                "fixture-key",
                "anthropic/model-a",
                {"proxy": "", "no_proxy": "localhost,127.0.0.1,::1"},
                10,
                4096,
                agent_cli="AUTO",
                cli_paths={"CODEX": "codex-missing", "CLAUDE": "claude"},
                client_homes={
                    "CODEX": self.root / "codex-auth" / ".codex",
                    "CLAUDE": self.root / "claude-auth" / ".claude",
                },
            )
            self.assertEqual(backend.agent_cli, "CLAUDE")
            result = backend.complete(
                "Use the official tools when useful.",
                {"type": "object"},
                "answer",
            )
        self.assertEqual(result.value, {"answer": "ok"})
        self.assertIn("prediction-openrouter-claude-", str(seen["kwargs"]["cwd"]))
        self.assertTrue(seen["kwargs"]["env"]["ANTHROPIC_BASE_URL"].startswith("http://127.0.0.1:"))
        self.assertEqual(
            seen["kwargs"]["env"].get("HOME"),
            str((self.root / "claude-auth").resolve()),
        )
        self.assertEqual(
            seen["kwargs"]["env"].get("CLAUDE_CONFIG_DIR"),
            str((self.root / "claude-auth" / ".claude").resolve()),
        )
        self.assertEqual(seen["kwargs"]["env"]["ANTHROPIC_API_KEY"], "")
        self.assertEqual(
            seen["command"][seen["command"].index("--model") + 1],
            "anthropic/model-a",
        )

    def test_auto_agent_keeps_a_non_anthropic_user_model_through_claude(self):
        def resolve(value):
            if value == "codex-missing":
                from prediction_market_agent.agent.decision import DecisionProviderError

                raise DecisionProviderError("missing codex")
            return "/fixture/claude"

        with patch(
            "prediction_market_agent.plugins.providers.openrouter.resolve_executable",
            side_effect=resolve,
        ):
            backend = OpenRouterBackend(
                "openrouter",
                "codex-missing",
                "fixture-key",
                "vendor/model-a",
                {"proxy": "", "no_proxy": "localhost,127.0.0.1,::1"},
                10,
                4096,
                agent_cli="AUTO",
                cli_paths={"CODEX": "codex-missing", "CLAUDE": "claude"},
            )
        self.assertEqual(backend.agent_cli, "CLAUDE")
        self.assertEqual(backend.model, "vendor/model-a")

    def test_openrouter_codex_contract_names_the_complete_schema(self):
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
        }
        prompt = _openrouter_schema_prompt("Decide.", schema)
        self.assertIn("FINAL RESPONSE CONTRACT", prompt)
        self.assertIn('"required":["answer"]', prompt)

    def test_openrouter_accepts_only_plain_or_single_fenced_schema_json(self):
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "action": {"type": "string", "enum": ["KEEP", "DROP"]},
                "score": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "required": ["action", "score"],
        }
        expected = {"action": "KEEP", "score": 0.8}
        self.assertEqual(
            _strict_schema_object(json.dumps(expected), schema), expected
        )
        self.assertEqual(
            _strict_schema_object(
                "```json\n" + json.dumps(expected) + "\n```", schema
            ),
            expected,
        )
        for invalid in (
            'Here is the answer: {"action":"KEEP","score":0.8}',
            '{"action":"KEEP","score":2}',
            '{"action":"KEEP","score":0.8,"extra":true}',
        ):
            with self.subTest(invalid=invalid), self.assertRaises(
                (json.JSONDecodeError, ValueError)
            ):
                _strict_schema_object(invalid, schema)

    def test_private_bridge_preserves_complete_responses_tool_protocol(self):
        bridge = _BridgeServer(
            "local-token",
            "fixture-key",
            "vendor/model-a",
            _opener(""),
            10,
            4096,
            f"http://127.0.0.1:{self.server.server_port}/responses",
            f"http://127.0.0.1:{self.server.server_port}/messages",
        )
        thread = threading.Thread(target=bridge.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(bridge.server_close)
        self.addCleanup(bridge.shutdown)
        body = {
            "model": "ignored/model",
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
            "tools": [
                {
                    "type": "web_search",
                    "search_context_size": "medium",
                    "filters": {"allowed_domains": ["example.com"]},
                },
                {
                    "type": "function",
                    "name": "search",
                    "description": "Search the web",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                },
                {
                    "type": "namespace",
                    "name": "mcp__market",
                    "description": "Market tools",
                    "tools": [
                        {
                            "type": "function",
                            "name": "lookup",
                            "description": "Look up a market",
                            "strict": False,
                            "parameters": {"type": "object"},
                        }
                    ],
                },
            ],
            "client_metadata": {"source": "codex"},
            "parallel_tool_calls": True,
            "prompt_cache_key": "cache-key",
            "store": False,
            "previous_response_id": "resp_previous",
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
            result = json.load(response)
        sent = self.seen[-1][2]
        self.assertEqual(sent["model"], "vendor/model-a")
        self.assertEqual(sent["provider"], {"require_parameters": True})
        self.assertEqual(sent["text"], body["text"])
        self.assertEqual(
            sent["tools"][0],
            {
                "type": "openrouter:web_search",
                "parameters": {
                    "search_context_size": "medium",
                    "allowed_domains": ["example.com"],
                },
            },
        )
        self.assertEqual(sent["tools"][1], body["tools"][1])
        self.assertEqual(sent["tools"][2]["type"], "function")
        self.assertEqual(sent["tools"][2]["name"], "mcp__market__lookup")
        self.assertNotIn("client_metadata", sent)
        self.assertNotIn("parallel_tool_calls", sent)
        self.assertNotIn("prompt_cache_key", sent)
        self.assertNotIn("store", sent)
        self.assertEqual(sent["input"], body["input"])
        self.assertEqual(sent["previous_response_id"], "resp_previous")
        self.assertEqual(sent["max_output_tokens"], 4096)
        self.assertEqual(result, {"upstream": "ok", "request_path": "/responses"})

    def test_private_bridge_preserves_native_search_when_model_advertises_it(self):
        bridge = _BridgeServer(
            "local-token",
            "fixture-key",
            "vendor/model-a",
            _opener(""),
            10,
            4096,
            f"http://127.0.0.1:{self.server.server_port}/responses",
            f"http://127.0.0.1:{self.server.server_port}/messages",
            native_model_parameters=frozenset(
                {"structured_outputs", "tools", "web_search_options"}
            ),
        )
        thread = threading.Thread(target=bridge.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(bridge.server_close)
        self.addCleanup(bridge.shutdown)
        tool = {"type": "web_search", "search_context_size": "low"}
        body = {
            "input": "search",
            "text": {"format": {"type": "json_schema", "schema": {"type": "object"}}},
            "tools": [tool],
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
        with urllib.request.urlopen(request, timeout=10):
            pass
        self.assertEqual(self.seen[-1][2]["tools"], [tool])

    def test_claude_versioned_search_does_not_reuse_openai_native_flag(self):
        body = {"tools": [{"type": "web_search_20250305", "name": "web_search"}]}

        _adapt_server_tools(
            body, frozenset({"structured_outputs", "tools", "web_search_options"})
        )

        self.assertEqual(body["tools"][0]["type"], "openrouter:web_search")

    def test_namespace_tool_calls_are_restored_for_codex_json_and_sse(self):
        names = {"mcp__market__lookup": ("mcp__market", "lookup")}
        item = {
            "type": "function_call",
            "name": "mcp__market__lookup",
            "arguments": "{}",
        }
        restored = json.loads(
            _restore_namespace_calls(
                json.dumps({"output": [item]}).encode(), "application/json", names
            )
        )
        self.assertEqual(restored["output"][0]["name"], "lookup")
        self.assertEqual(restored["output"][0]["namespace"], "mcp__market")
        event_item = {
            "type": "function_call",
            "name": "mcp__market__lookup",
            "arguments": "{}",
        }
        event = b"data: " + json.dumps({"item": event_item}).encode() + b"\n\n"
        stream = _restore_namespace_calls(event, "text/event-stream", names)
        event_payload = json.loads(stream.splitlines()[0].removeprefix(b"data: "))
        self.assertEqual(event_payload["item"]["name"], "lookup")
        self.assertEqual(event_payload["item"]["namespace"], "mcp__market")

    def test_private_bridge_forwards_codex_model_catalog_request(self):
        bridge = _BridgeServer(
            "local-token",
            "fixture-key",
            "vendor/model-a",
            _opener(""),
            10,
            4096,
            f"http://127.0.0.1:{self.server.server_port}/responses",
            f"http://127.0.0.1:{self.server.server_port}/messages",
        )
        thread = threading.Thread(target=bridge.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(bridge.server_close)
        self.addCleanup(bridge.shutdown)
        request = urllib.request.Request(
            f"http://127.0.0.1:{bridge.server_port}/v1/models?client_version=test",
            headers={"Authorization": "Bearer local-token"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.load(response)
        self.assertEqual([item["id"] for item in payload["models"]], ["vendor/model-a"])
        self.assertEqual(self.seen[-1][0], "/models?client_version=test")
        self.assertEqual(self.seen[-1][1], "Bearer fixture-key")

    def test_private_bridge_preserves_complete_claude_messages_tool_protocol(self):
        bridge = _BridgeServer(
            "local-token",
            "fixture-key",
            "vendor/model-a",
            _opener(""),
            10,
            4096,
            f"http://127.0.0.1:{self.server.server_port}/responses",
            f"http://127.0.0.1:{self.server.server_port}/messages",
        )
        thread = threading.Thread(target=bridge.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(bridge.server_close)
        self.addCleanup(bridge.shutdown)
        body = {
            "model": "ignored/model",
            "max_tokens": 1024,
            "context_management": {"edits": []},
            "thinking": {"type": "adaptive", "display": "omitted"},
            "output_config": {
                "effort": "high",
                "format": {"type": "json_schema", "schema": {"type": "object"}},
            },
            "metadata": {"user_id": "fixture"},
            "messages": [{"role": "user", "content": "inspect the workspace"}],
            "tools": [
                {"name": "Read", "input_schema": {"type": "object"}},
                {
                    "type": "web_search_20250305",
                    "name": "web_search",
                    "max_uses": 3,
                    "blocked_domains": ["blocked.example"],
                },
                {
                    "type": "web_fetch_20250910",
                    "name": "web_fetch",
                    "max_uses": 2,
                },
            ],
        }
        request = urllib.request.Request(
            f"http://127.0.0.1:{bridge.server_port}/api/v1/messages",
            data=json.dumps(body).encode(),
            headers={"x-api-key": "local-token", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            self.assertEqual(json.load(response)["request_path"], "/messages")
        sent = self.seen[-1][2]
        self.assertEqual(sent["model"], "vendor/model-a")
        self.assertEqual(sent["messages"], body["messages"])
        self.assertEqual(sent["tools"][0], body["tools"][0])
        self.assertEqual(
            sent["tools"][1],
            {
                "type": "openrouter:web_search",
                "parameters": {
                    "max_uses": 3,
                    "excluded_domains": ["blocked.example"],
                },
            },
        )
        self.assertEqual(
            sent["tools"][2],
            {"type": "openrouter:web_fetch", "parameters": {"max_uses": 2}},
        )
        self.assertEqual(sent["max_tokens"], 1024)
        self.assertEqual(sent["context_management"], body["context_management"])
        self.assertEqual(sent["thinking"], body["thinking"])
        self.assertEqual(sent["metadata"], body["metadata"])
        self.assertNotIn("effort", sent["output_config"])
        self.assertEqual(sent["output_config"]["format"], body["output_config"]["format"])
        self.assertEqual(sent["provider"], {"require_parameters": True})

    def test_claude_effort_is_preserved_for_anthropic_native_routes(self):
        body = {"output_config": {"effort": "high"}}

        _adapt_claude_parameters(
            body, "anthropic/claude-sonnet-4.6", frozenset({"reasoning"})
        )

        self.assertEqual(body["output_config"], {"effort": "high"})

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
            "http://openrouter.invalid/responses",
            "http://openrouter.invalid/messages",
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

        self.assertEqual(self.seen[-1][0], "http://openrouter.invalid/responses")
        self.assertEqual(self.seen[-1][1], "Bearer fixture-key")


if __name__ == "__main__":
    unittest.main()


class RoutingRefusalTests(unittest.TestCase):
    """OpenRouter refusing to route is not the model answering badly, and must not end the round.

    `require_parameters` asks for an endpoint that supports everything in the request at once. When
    none does, OpenRouter answers 404 "No endpoints found" and the round used to die there, with a
    ledger row reading `[]` and a message about a schema nobody had violated. The schema guarantee
    never came from that flag - the returned object is checked here either way - so the request is
    sent once more without it before anything is called a failure.
    """

    def _upstream(self, refusals: int):
        state = {"calls": []}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                state["calls"].append(body)
                if len(state["calls"]) <= refusals:
                    payload = json.dumps({"error": {
                        "message": "No endpoints found that can handle the requested parameters. "
                                   "To learn more about provider routing, visit: "
                                   "https://openrouter.ai/docs/guides/",
                        "code": 404,
                    }}).encode()
                    self.send_response(404)
                else:
                    payload = json.dumps({"upstream": "ok"}).encode()
                    self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server, state

    def _bridge(self, upstream_port: int):
        bridge = _BridgeServer(
            "local-token", "fixture-key", "vendor/model-a", _opener(""), 10, 4096,
            f"http://127.0.0.1:{upstream_port}/responses",
            f"http://127.0.0.1:{upstream_port}/messages",
        )
        thread = threading.Thread(target=bridge.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(bridge.server_close)
        self.addCleanup(bridge.shutdown)
        return bridge

    REQUEST = {
        "model": "ignored/model",
        "input": [{"type": "message", "role": "user",
                   "content": [{"type": "input_text", "text": "Return ok"}]}],
        "text": {"format": {"type": "json_schema", "name": "probe", "strict": True,
                            "schema": {"type": "object", "properties": {"word": {"type": "string"}},
                                       "required": ["word"], "additionalProperties": False}}},
    }

    def _send(self, bridge) -> tuple[int, dict]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{bridge.server_port}/v1/responses",
            data=json.dumps(self.REQUEST).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer local-token"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"{}")

    def test_a_refused_route_is_retried_without_the_requirement(self):
        server, state = self._upstream(refusals=1)
        status, body = self._send(self._bridge(server.server_port))
        self.assertEqual(status, 200, body)
        self.assertEqual(len(state["calls"]), 2, "refused once, sent again")
        self.assertTrue(state["calls"][0]["provider"]["require_parameters"])
        self.assertNotIn("require_parameters", state["calls"][1].get("provider", {}))
        self.assertEqual(
            state["calls"][1]["text"]["format"]["type"], "json_schema",
            "the schema is still requested; only the routing ultimatum was dropped",
        )

    def test_a_refusal_that_survives_the_retry_is_reported_as_itself(self):
        server, state = self._upstream(refusals=2)
        status, body = self._send(self._bridge(server.server_port))
        self.assertEqual(status, 404)
        self.assertEqual(len(state["calls"]), 2, "tried twice, then stopped")
        self.assertIn("No endpoints found", json.dumps(body))

    def test_an_empty_answer_is_not_called_a_schema_violation(self):
        from prediction_market_agent.plugins.providers.openrouter import _routing_refusal

        self.assertIn("没有端点", _routing_refusal("", "No endpoints found that can handle the requested parameters"))
        self.assertEqual(_routing_refusal("", "some other trouble"), "")
        source = Path("src/prediction_market_agent/plugins/providers/openrouter.py").read_text()
        self.assertIn("OpenRouter returned no content at all", source)
