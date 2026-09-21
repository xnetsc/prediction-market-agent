from __future__ import annotations

import io
import json
import logging
import hashlib
import os
import secrets
import subprocess
import tempfile
import threading
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

from prediction_market_agent.agent.decision import (
    DecisionProviderError,
    StructuredResult,
)
from prediction_market_agent.plugin_system.config_io import (
    json_file_callbacks,
)
from prediction_market_agent.plugin_system.discovery import (
    PluginConfigField,
    PluginConfiguration,
    PluginControls,
    PluginInitializationContext,
    PluginReadiness,
    PluginSpec,
)
from prediction_market_agent.plugin_system.network_diagnostics import configured_proxy_route
from prediction_market_agent.plugins.providers._shared import (
    cli_failure_detail,
    client_subprocess_environment,
    configured_client_homes,
    resolve_executable,
    shared_agent_history_instruction,
    subprocess_output_text,
)


OPENROUTER_API_BASE = "https://openrouter.ai/api/v1"
OPENROUTER_MODELS_URL = (
    OPENROUTER_API_BASE + "/models?supported_parameters=structured_outputs"
)
OPENROUTER_KEY_URL = OPENROUTER_API_BASE + "/key"
OPENROUTER_CREDITS_URL = OPENROUTER_API_BASE + "/credits"
REQUIRED_MODEL_PARAMETERS = frozenset({"structured_outputs", "tools"})
CODEX_REQUIRED_MODEL_PARAMETERS = REQUIRED_MODEL_PARAMETERS
AGENT_CLI_OPTIONS = ("AUTO", "CODEX", "CLAUDE")


def _openrouter_schema_prompt(prompt: str, schema: dict[str, Any]) -> str:
    """Make the Responses compatibility boundary explicit to the selected model.

    Codex correctly sends ``text.format=json_schema`` to the private bridge, but OpenRouter's
    Responses compatibility route can acknowledge that field while a routed model still answers
    as ordinary prose.  The native schema remains on the API request; this duplicated instruction
    prevents the model from treating the final contract as an unnamed Codex-only detail.
    """
    return (
        prompt
        + "\n\nFINAL RESPONSE CONTRACT (MANDATORY): Return exactly one JSON object and no "
        "explanation. It must validate against this complete JSON Schema:\n"
        + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    )


def _schema_error(value: Any, schema: dict[str, Any], path: str = "$") -> str:
    """Validate the JSON-Schema subset used by runtime contracts without coercion."""
    expected = schema.get("type")
    allowed = expected if isinstance(expected, list) else [expected] if expected else []

    def is_type(name: str) -> bool:
        return {
            "null": value is None,
            "object": isinstance(value, dict),
            "array": isinstance(value, list),
            "string": isinstance(value, str),
            "boolean": isinstance(value, bool),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        }.get(name, False)

    if allowed and not any(is_type(str(name)) for name in allowed):
        return f"{path} must be {' or '.join(map(str, allowed))}"
    if "enum" in schema and value not in schema["enum"]:
        return f"{path} is not one of the allowed values"
    if isinstance(value, dict):
        properties = schema.get("properties") or {}
        missing = [name for name in schema.get("required") or [] if name not in value]
        if missing:
            return f"{path} is missing {', '.join(missing)}"
        additional = schema.get("additionalProperties", True)
        for name, child in value.items():
            child_schema = properties.get(name)
            if child_schema is None:
                if additional is False:
                    return f"{path}.{name} is not allowed"
                child_schema = additional if isinstance(additional, dict) else None
            if isinstance(child_schema, dict):
                error = _schema_error(child, child_schema, f"{path}.{name}")
                if error:
                    return error
    elif isinstance(value, list):
        if "minItems" in schema and len(value) < int(schema["minItems"]):
            return f"{path} has too few items"
        if "maxItems" in schema and len(value) > int(schema["maxItems"]):
            return f"{path} has too many items"
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, child in enumerate(value):
                error = _schema_error(child, item_schema, f"{path}[{index}]")
                if error:
                    return error
    elif isinstance(value, str):
        if "minLength" in schema and len(value) < int(schema["minLength"]):
            return f"{path} is too short"
        if "maxLength" in schema and len(value) > int(schema["maxLength"]):
            return f"{path} is too long"
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            return f"{path} is below the minimum"
        if "maximum" in schema and value > schema["maximum"]:
            return f"{path} is above the maximum"
    return ""


def _strict_schema_object(text: str, schema: dict[str, Any]) -> dict[str, Any]:
    """Accept plain JSON or one bare JSON code fence, then enforce the whole schema."""
    candidate = text.strip()
    if candidate.startswith("```json\n") and candidate.endswith("```"):
        candidate = candidate[8:-3].strip()
    elif candidate.startswith("```\n") and candidate.endswith("```"):
        candidate = candidate[4:-3].strip()
    value = json.loads(candidate)
    if not isinstance(value, dict):
        raise ValueError("root is not an object")
    error = _schema_error(value, schema)
    if error:
        raise ValueError(error)
    return value


def _server_tool_parameters(tool: dict[str, Any]) -> dict[str, Any]:
    """Translate official CLI server-tool options to OpenRouter's equivalent shape."""
    parameters = dict(tool.get("parameters") or {})
    for name in (
        "engine",
        "max_results",
        "max_total_results",
        "max_uses",
        "search_context_size",
        "max_characters",
        "user_location",
        "allowed_domains",
        "excluded_domains",
    ):
        if name in tool:
            parameters[name] = tool[name]
    filters = tool.get("filters")
    if isinstance(filters, dict) and isinstance(filters.get("allowed_domains"), list):
        parameters["allowed_domains"] = filters["allowed_domains"]
    if "blocked_domains" in tool:
        parameters["excluded_domains"] = tool["blocked_domains"]
    return parameters


def _adapt_server_tools(
    body: dict[str, Any], native_model_parameters: frozenset[str] = frozenset()
) -> dict[str, tuple[str, str]]:
    """Keep CLI protocol I/O while replacing provider-specific hosted tool declarations.

    OpenRouter executes these tools inside the same Responses or Messages request and returns the
    result using the protocol the caller used.  The child CLI therefore retains its native event
    stream and Agent loop; only the declaration at this private forwarding boundary changes.
    """
    tools = body.get("tools")
    if not isinstance(tools, list):
        return {}
    adapted: list[Any] = []
    namespace_names: dict[str, tuple[str, str]] = {}
    for tool in tools:
        if not isinstance(tool, dict):
            adapted.append(tool)
            continue
        tool_type = str(tool.get("type", ""))
        if tool_type == "namespace" and "namespace_tools" not in native_model_parameters:
            namespace = str(tool.get("name", ""))
            for child in tool.get("tools") or []:
                if not isinstance(child, dict) or child.get("type") != "function":
                    continue
                flattened = dict(child)
                child_name = str(child.get("name", ""))
                if not child_name:
                    continue
                if namespace and namespace != "functions":
                    candidate = namespace + "__" + child_name
                    if len(candidate) > 64:
                        digest = hashlib.sha256(candidate.encode()).hexdigest()[:12]
                        candidate = candidate[: 51] + "_" + digest
                    flattened["name"] = candidate
                    namespace_names[candidate] = (namespace, child_name)
                adapted.append(flattened)
            continue
        if tool_type in {"web_search", "web_search_preview"} or tool_type.startswith(
            "web_search_"
        ):
            native_search = tool_type in native_model_parameters or (
                tool_type in {"web_search", "web_search_preview"}
                and "web_search_options" in native_model_parameters
            )
            if native_search:
                adapted.append(tool)
                continue
            replacement: dict[str, Any] = {"type": "openrouter:web_search"}
            parameters = _server_tool_parameters(tool)
            if parameters:
                replacement["parameters"] = parameters
            adapted.append(replacement)
            continue
        if tool_type == "web_fetch" or tool_type.startswith("web_fetch_"):
            if tool_type in native_model_parameters:
                adapted.append(tool)
                continue
            replacement = {"type": "openrouter:web_fetch"}
            parameters = _server_tool_parameters(tool)
            if parameters:
                replacement["parameters"] = parameters
            adapted.append(replacement)
            continue
        adapted.append(tool)
    body["tools"] = adapted
    return namespace_names


LOGGER = logging.getLogger(__name__)

NO_ENDPOINTS = "No endpoints found that can handle the requested parameters"


def _routing_refusal(stdout: str, stderr: str) -> str:
    """OpenRouter's own words when it declined to route, which name the fix.

    "No endpoints found that can handle the requested parameters" is not a model failing to answer:
    it is this model having no provider that supports everything the request asked for at once -
    a schema, tools, reasoning. The operator can act on that sentence and cannot act on "returned
    no content", so it is carried up when the client printed it.
    """
    for text in (stderr, stdout):
        if NO_ENDPOINTS in (text or ""):
            return (
                "OpenRouter 说这个模型没有端点能同时满足本次请求的全部参数"
                "（结构化输出、工具等）。换一个支持这些参数的模型，或在模型配置里降低要求。"
            )
    return ""


def _adapt_optional_parameters(
    body: dict[str, Any], native_model_parameters: frozenset[str]
) -> None:
    """Drop only Codex transport optimizations a selected model did not advertise."""
    for field in ("client_metadata", "parallel_tool_calls", "prompt_cache_key", "store"):
        if field not in native_model_parameters:
            body.pop(field, None)
    reasoning = body.get("reasoning")
    if isinstance(reasoning, dict) and "reasoning_summary" not in native_model_parameters:
        reasoning.pop("summary", None)
    if "include_reasoning" not in native_model_parameters:
        body.pop("include", None)


def _adapt_claude_parameters(
    body: dict[str, Any],
    model: str,
    native_model_parameters: frozenset[str],
) -> None:
    """Remove only Claude transport hints the selected route cannot honor.

    Claude Code 2.1 sends output_config.effort even when the user did not configure an
    OpenRouter reasoning effort.  Anthropic first-party routes accept that native field, but a
    non-Anthropic model can advertise generic reasoning support while rejecting this
    Anthropic-specific nesting under provider.require_parameters.  Structured output format is
    retained independently.
    """
    output_config = body.get("output_config")
    if not isinstance(output_config, dict):
        return
    native_effort = (
        model.startswith("anthropic/")
        or model.startswith("~anthropic/")
        or "output_config" in native_model_parameters
        or "effort" in native_model_parameters
    )
    if not native_effort:
        output_config.pop("effort", None)
    if not output_config:
        body.pop("output_config", None)


def _restore_namespace_calls(
    payload: bytes, content_type: str, names: dict[str, tuple[str, str]]
) -> bytes:
    if not names:
        return payload

    def restore(value: Any) -> Any:
        if isinstance(value, dict):
            name = value.get("name")
            if isinstance(name, str) and name in names:
                namespace, child = names[name]
                value["name"] = child
                value["namespace"] = namespace
            for child_value in value.values():
                restore(child_value)
        elif isinstance(value, list):
            for child_value in value:
                restore(child_value)
        return value

    if "text/event-stream" not in content_type.lower():
        try:
            return json.dumps(restore(json.loads(payload))).encode("utf-8")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return payload
    output: list[bytes] = []
    for line in payload.splitlines(keepends=True):
        prefix, separator, data = line.partition(b"data:")
        if not separator:
            output.append(line)
            continue
        suffix = b"\n" if data.endswith(b"\n") else b""
        raw = data.strip()
        if not raw or raw == b"[DONE]":
            output.append(line)
            continue
        try:
            rewritten = json.dumps(restore(json.loads(raw))).encode("utf-8")
            output.append(prefix + separator + b" " + rewritten + suffix)
        except (json.JSONDecodeError, UnicodeDecodeError):
            output.append(line)
    return b"".join(output)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward an account's Authorization header to a redirect destination.
        return None


def _opener(proxy: str) -> urllib.request.OpenerDirector:
    handler = (
        urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        if proxy
        else urllib.request.ProxyHandler({})
    )
    return urllib.request.build_opener(handler, NoRedirect())


def model_choices(
    values: dict[str, Any],
    *,
    models_url: str = OPENROUTER_MODELS_URL,
    proxy_settings: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    """Return only models advertising schema output and schema-defined tool input."""
    parsed = urllib.parse.urlsplit(models_url)
    if parsed.scheme != "https" and not (
        parsed.scheme == "http"
        and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    ):
        raise ValueError("OpenRouter 模型接口须使用 HTTPS")
    headers: dict[str, str] = {"User-Agent": "prediction-market-agent/1"}
    key = values.get("OPENROUTER_API_KEY", "").strip()
    if key:
        headers["Authorization"] = "Bearer " + key
    proxy = (proxy_settings or {}).get("proxy", "")
    try:
        with _opener(proxy).open(
            urllib.request.Request(models_url, headers=headers), timeout=15
        ) as response:
            data = json.loads(response.read(8 * 1024 * 1024))["data"]
        if not isinstance(data, list):
            raise ValueError("Invalid model list")
        agent_cli = str(
            values.get("_RESOLVED_AGENT_CLI")
            or values.get("OPENROUTER_AGENT_CLI")
            or "CODEX"
        ).upper()
        if agent_cli == "AUTO":
            agent_cli = "CODEX"
        required_parameters = (
            CODEX_REQUIRED_MODEL_PARAMETERS
            if agent_cli == "CODEX"
            else REQUIRED_MODEL_PARAMETERS
        )
        supported = []
        for item in data:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                continue
            parameters = item.get("supported_parameters")
            if not isinstance(parameters, list) or not required_parameters.issubset(
                parameters
            ):
                continue
            supported.append(
                {
                    "value": item["id"],
                    "label": str(item.get("name") or item["id"]),
                }
            )
        return sorted(supported, key=lambda item: item["value"])
    except urllib.error.HTTPError as error:
        raise ValueError(
            f"获取 OpenRouter 模型列表失败：HTTP {error.code}，请检查 Key 和代理"
        ) from None
    except (OSError, ValueError, KeyError, TypeError):
        raise ValueError("获取 OpenRouter 结构化输入/输出模型列表失败，请检查网络、Key 和代理") from None


def _account_json(
    url: str,
    key: str,
    proxy_settings: dict[str, str],
) -> dict[str, Any]:
    """Read one fixed OpenRouter account endpoint without exposing its credential."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" and not (
        parsed.scheme == "http"
        and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    ):
        raise ValueError("OpenRouter 账号接口须使用 HTTPS")
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": "Bearer " + key,
            "User-Agent": "prediction-market-agent/1",
        },
    )
    try:
        with _opener(proxy_settings.get("proxy", "")).open(request, timeout=15) as response:
            payload = json.loads(response.read(1024 * 1024))
        data = payload["data"]
        if not isinstance(data, dict):
            raise ValueError("Invalid account response")
        return data
    except urllib.error.HTTPError as error:
        raise ValueError(f"OpenRouter 账号查询失败：HTTP {error.code}，请检查对应 Key 和代理") from None
    except (OSError, ValueError, KeyError, TypeError):
        raise ValueError("OpenRouter 账号查询失败，请检查对应 Key 和代理") from None


class OpenRouterAccountControl:
    """OpenRouter-owned balance reader; it is not a framework-wide API facility."""

    def __init__(
        self,
        configuration: PluginConfiguration,
        context: PluginInitializationContext,
        *,
        key_url: str = OPENROUTER_KEY_URL,
        credits_url: str = OPENROUTER_CREDITS_URL,
        values_loader: Callable[[], dict[str, Any]] | None = None,
    ):
        self.configuration = configuration
        self.context = context
        self.key_url = key_url
        self.credits_url = credits_url
        self.values_loader = values_loader or configuration.load
        self.lock = threading.RLock()
        self.usage: dict[str, Any] = {}
        self.controls = PluginControls(self.snapshot, self.action)

    def proxy_settings(self) -> dict[str, str]:
        values = self.values_loader()
        return self.context.proxy_settings(
            values["OPENROUTER_HTTP_PROXY"], field_name="OPENROUTER_HTTP_PROXY"
        )

    def snapshot(self) -> dict[str, Any]:
        values = self.values_loader()
        api_key = str(values.get("OPENROUTER_API_KEY", "")).strip()
        model = str(values.get("OPENROUTER_MODEL", "")).strip()
        configured = bool(api_key and model)
        try:
            proxy = self.proxy_settings()
            proxy_message = proxy["display"] + " — " + proxy["source"]
        except ValueError as error:
            proxy_message = str(error)
        with self.lock:
            usage = json.loads(json.dumps(self.usage))
        requested_cli = str(values.get("OPENROUTER_AGENT_CLI", "AUTO") or "AUTO").upper()
        available_clis: list[str] = []
        unavailable_clis: dict[str, str] = {}
        for cli_name, field_name, default in (
            ("CODEX", "CODEX_CLI_PATH", "codex"),
            ("CLAUDE", "CLAUDE_CLI_PATH", "claude"),
        ):
            cli_load, _save, _delete, _storage = json_file_callbacks(
                self.context.working_directory
                / "config"
                / "plugins"
                / f"{cli_name.lower()}.json"
            )
            configured_path = str(cli_load().get(field_name, default) or default).strip()
            try:
                resolve_executable(configured_path)
                available_clis.append(cli_name)
            except DecisionProviderError as error:
                unavailable_clis[cli_name] = str(error)
        selected_cli = (
            next((item for item in ("CODEX", "CLAUDE") if item in available_clis), "")
            if requested_cli == "AUTO"
            else requested_cli if requested_cli in available_clis else ""
        )
        account_configured = configured
        configured = account_configured and bool(selected_cli)
        if not account_configured:
            message = "请填写推理 API Key 并选择结构化输出模型"
        elif not selected_cli:
            message = "OpenRouter 配置完整，但没有可用的 Codex 或 Claude CLI"
        else:
            message = "推理 Key、结构化输出模型与 Agent CLI 均已就绪"
        return {
            "control_type": "openrouter",
            "state": "configured" if configured else "incomplete",
            "message": message,
            "model": model,
            "agent_cli": {
                "requested": requested_cli,
                "selected": selected_cli,
                "available": available_clis,
                "unavailable": unavailable_clis,
            },
            "proxy_message": proxy_message,
            "usage": usage,
            "actions": [
                {
                    "id": "refresh_usage",
                    "label": "刷新余额与用量",
                    "group": "账号与额度",
                    "group_note": (
                        "普通推理 Key 查询本 Key 用量；可选 Management Key 只查询账户充值余额。"
                        "查询不发起模型调用。"
                    ),
                    "disabled": not bool(api_key),
                }
            ],
        }

    def action(self, action: str, values: dict[str, Any]) -> dict[str, Any]:
        del values
        if action != "refresh_usage":
            raise ValueError("Unknown OpenRouter control action")
        checked_at = int(time.time())
        configured = self.values_loader()
        api_key = str(configured.get("OPENROUTER_API_KEY", "")).strip()
        management_key = str(
            configured.get("OPENROUTER_MANAGEMENT_API_KEY", "")
        ).strip()
        if not api_key:
            reading = {
                "checked_at": checked_at,
                "error": "尚未配置 OpenRouter 推理 API Key",
                "source": "OpenRouter API",
            }
        else:
            reading = self._read(api_key, management_key, checked_at)
        with self.lock:
            self.usage = reading
        return self.snapshot()

    def _read(
        self, api_key: str, management_key: str, checked_at: int
    ) -> dict[str, Any]:
        try:
            key = _account_json(self.key_url, api_key, self.proxy_settings())
        except ValueError as error:
            return {
                "checked_at": checked_at,
                "error": str(error),
                "source": "OpenRouter · /api/v1/key",
            }
        reading: dict[str, Any] = {
            "checked_at": checked_at,
            "error": "",
            "source": "OpenRouter · /api/v1/key",
            "key": {
                field: key.get(field)
                for field in (
                    "usage",
                    "usage_daily",
                    "usage_weekly",
                    "usage_monthly",
                    "limit",
                    "limit_remaining",
                    "limit_reset",
                    "is_free_tier",
                    "expires_at",
                )
            },
        }
        remaining = key.get("limit_remaining")
        reading["available"] = not isinstance(remaining, (int, float)) or remaining > 0
        if management_key:
            try:
                credits = _account_json(
                    self.credits_url, management_key, self.proxy_settings()
                )
                total = credits.get("total_credits")
                used = credits.get("total_usage")
                reading["account"] = {
                    "total_credits": total,
                    "total_usage": used,
                    "balance": total - used
                    if isinstance(total, (int, float))
                    and not isinstance(total, bool)
                    and isinstance(used, (int, float))
                    and not isinstance(used, bool)
                    else None,
                }
                reading["source"] += " · /api/v1/credits"
            except ValueError as error:
                reading["account_error"] = str(error)
        else:
            reading["account_note"] = "未配置 Management Key，未查询账户充值余额"
        return reading


class _BridgeServer(ThreadingHTTPServer):
    """OpenRouter-only transparent guard in front of official CLI protocols.

    It keeps messages, tool calls/results, and streaming events in the CLI's native protocol.
    Provider-specific hosted tool declarations are adapted to OpenRouter server tools at this
    boundary, so models need tool support but not a provider's private web-search parameter.
    """

    daemon_threads = True

    def __init__(
        self,
        token: str,
        api_key: str,
        model: str,
        opener: urllib.request.OpenerDirector,
        timeout: int,
        max_output_tokens: int,
        responses_url: str,
        messages_url: str | None = None,
        model_catalog: bytes | None = None,
        native_model_parameters: frozenset[str] = frozenset(),
    ):
        self.token = token
        self.api_key = api_key
        self.model = model
        self.opener = opener
        self.timeout = timeout
        self.max_output_tokens = max_output_tokens
        self.responses_url = responses_url
        self.models_url = responses_url.rsplit("/responses", 1)[0] + "/models"
        self.messages_url = messages_url or responses_url.rsplit("/responses", 1)[0] + "/messages"
        self.model_catalog = model_catalog
        self.native_model_parameters = native_model_parameters
        super().__init__(("127.0.0.1", 0), _BridgeHandler)


class _BridgeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    _HOP_BY_HOP_HEADERS = frozenset(
        {
            "authorization",
            "accept-encoding",
            "connection",
            "content-length",
            "host",
            "keep-alive",
            "proxy-authorization",
            "proxy-connection",
            "te",
            "trailer",
            "transfer-encoding",
            "upgrade",
            "x-api-key",
        }
    )

    def log_message(self, *_):
        pass

    def _write(self, status: int, content_type: str, payload: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _authorized(self, server: _BridgeServer) -> bool:
        authorization = self.headers.get("Authorization")
        api_key = self.headers.get("x-api-key")
        return authorization == "Bearer " + server.token or api_key == server.token

    def _upstream_headers(
        self, server: _BridgeServer, *, content_type: str | None = None
    ) -> dict[str, str]:
        # Keep CLI protocol/version/feature headers intact. Only transport headers and the
        # child-facing one-time credential are replaced at this provider boundary.
        headers = {
            name: value
            for name, value in self.headers.items()
            if name.lower() not in self._HOP_BY_HOP_HEADERS
        }
        headers["Authorization"] = "Bearer " + server.api_key
        headers.setdefault("User-Agent", "prediction-market-agent/1")
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def do_GET(self) -> None:
        server: _BridgeServer = self.server  # type: ignore[assignment]
        parsed_path = urllib.parse.urlsplit(self.path)
        if not self._authorized(server) or parsed_path.path != "/v1/models":
            self._write(404, "application/json", b'{"error":{"message":"not found"}}')
            return
        if server.model_catalog is not None:
            self._write(200, "application/json", server.model_catalog)
            return
        upstream = server.models_url
        if parsed_path.query:
            upstream += "?" + parsed_path.query
        request = urllib.request.Request(
            upstream,
            headers=self._upstream_headers(server),
        )
        try:
            with server.opener.open(request, timeout=server.timeout) as response:
                payload = response.read(16 * 1024 * 1024)
                status = response.status
            document = json.loads(payload)
            candidates = document.get("models") or document.get("data") or []
            if not isinstance(candidates, list):
                raise ValueError("OpenRouter model catalog must contain a model list")
            selected = [
                item
                for item in candidates
                if isinstance(item, dict)
                and str(item.get("slug") or item.get("id") or "") == server.model
            ]
            # Codex receives metadata for exactly the model selected in this plugin. The UI owns
            # model selection; the CLI catalog refresh may describe that choice but cannot replace
            # it with another OpenRouter model.
            self._write(
                status,
                "application/json",
                json.dumps({"models": selected}).encode("utf-8"),
            )
        except urllib.error.HTTPError as error:
            self._write(error.code, "application/json", error.read(1024 * 1024))
        except Exception as error:
            payload = json.dumps(
                {"error": {"message": str(error)[:1000], "type": "openrouter_bridge"}}
            ).encode("utf-8")
            self._write(502, "application/json", payload)

    def do_POST(self) -> None:
        server: _BridgeServer = self.server  # type: ignore[assignment]
        parsed_path = urllib.parse.urlsplit(self.path)
        path = parsed_path.path
        if not self._authorized(server):
            self._write(404, "application/json", b'{"error":{"message":"not found"}}')
            return
        if path == "/v1/responses" or path.startswith("/v1/responses/"):
            upstream = server.responses_url + path.removeprefix("/v1/responses")
            output_limit_field = "max_output_tokens"
            require_response_schema = path == "/v1/responses"
            claude_messages = False
        elif path == "/api/v1/messages" or path.startswith("/api/v1/messages/"):
            upstream = server.messages_url + path.removeprefix("/api/v1/messages")
            output_limit_field = "max_tokens"
            require_response_schema = False
            claude_messages = True
        else:
            self._write(404, "application/json", b'{"error":{"message":"not found"}}')
            return
        if parsed_path.query:
            upstream += "?" + parsed_path.query
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 32 * 1024 * 1024:
                raise ValueError("invalid request size")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("request body must be an object")
            output_format = (body.get("text") or {}).get("format")
            if require_response_schema and (
                not isinstance(output_format, dict)
                or output_format.get("type") != "json_schema"
            ):
                raise ValueError("Codex request did not include a JSON schema")
            provider = body.get("provider") or {}
            if not isinstance(provider, dict):
                raise ValueError("provider routing options must be an object")
            body["provider"] = {**provider, "require_parameters": True}
            body["model"] = server.model
            body.setdefault(output_limit_field, server.max_output_tokens)
            _adapt_optional_parameters(body, server.native_model_parameters)
            if claude_messages:
                _adapt_claude_parameters(
                    body, server.model, server.native_model_parameters
                )
            namespace_names = _adapt_server_tools(
                body, server.native_model_parameters
            )
            payload, content_type, status = self._ask_upstream(server, upstream, body)
            payload = _restore_namespace_calls(
                payload, content_type, namespace_names
            )
            self._write(status, content_type, payload)
        except urllib.error.HTTPError as error:
            detail = error.read(1024 * 1024)
            self._write(error.code, "application/json", detail)

        except Exception as error:
            payload = json.dumps(
                {"error": {"message": str(error)[:1000], "type": "openrouter_bridge"}}
            ).encode("utf-8")
            self._write(502, "application/json", payload)

    def _ask_upstream(self, server, upstream: str, body: dict[str, Any]):
        """Send the round, and when routing refuses the parameters, send it once more without them.

        `require_parameters` asks OpenRouter to route only to a provider supporting everything in
        the request. It is a good default and a bad ultimatum: a model whose endpoints support the
        schema but not, say, tool calls answers 404 "No endpoints found", and the round dies having
        never been attempted. Dropping the flag costs nothing here, because the schema is checked
        against the returned object on the way out either way - the guarantee comes from that check,
        not from the routing hint.
        """
        attempts = [body]
        relaxed = {key: value for key, value in body.items() if key != "provider"}
        routing = {key: value for key, value in (body.get("provider") or {}).items()
                   if key != "require_parameters"}
        if routing:
            relaxed["provider"] = routing
        attempts.append(relaxed)
        for index, attempt in enumerate(attempts):
            request = urllib.request.Request(
                upstream,
                data=json.dumps(attempt).encode("utf-8"),
                headers=self._upstream_headers(server, content_type="application/json"),
                method="POST",
            )
            try:
                with server.opener.open(request, timeout=server.timeout) as response:
                    return (
                        response.read(64 * 1024 * 1024),
                        response.headers.get("Content-Type", "application/json"),
                        response.status,
                    )
            except urllib.error.HTTPError as error:
                detail = error.read(1024 * 1024)
                text = detail.decode("utf-8", errors="replace")
                if index + 1 < len(attempts) and error.code == 404 and NO_ENDPOINTS in text:
                    LOGGER.warning(
                        "OpenRouter refused to route %s with require_parameters; retrying without it",
                        server.model,
                    )
                    continue
                raise urllib.error.HTTPError(
                    error.url, error.code, error.reason, error.headers, io.BytesIO(detail)
                ) from error
        raise RuntimeError("unreachable")


class OpenRouterBackend:
    def __init__(
        self,
        name: str,
        executable: str,
        api_key: str,
        model: str,
        proxy_settings: dict[str, str],
        timeout: int,
        max_output_tokens: int,
        *,
        responses_url: str = OPENROUTER_API_BASE + "/responses",
        messages_url: str = OPENROUTER_API_BASE + "/messages",
        agent_cli: str = "AUTO",
        cli_paths: dict[str, str] | None = None,
        history_database: Path | None = None,
        client_homes: dict[str, Path] | None = None,
    ):
        if not api_key or not model:
            raise DecisionProviderError("请填写 OpenRouter API Key 和模型 ID")
        self.name = name
        requested_cli = str(agent_cli or "AUTO").strip().upper()
        if requested_cli not in AGENT_CLI_OPTIONS:
            raise DecisionProviderError(
                "OpenRouter Agent CLI 必须是 AUTO、CODEX 或 CLAUDE"
            )
        configured_paths = {
            "CODEX": executable,
            "CLAUDE": "claude",
            **{str(key).upper(): str(value) for key, value in (cli_paths or {}).items()},
        }
        available: dict[str, str] = {}
        unavailable: dict[str, str] = {}
        for candidate in ("CODEX", "CLAUDE"):
            try:
                available[candidate] = resolve_executable(configured_paths[candidate])
            except DecisionProviderError as error:
                unavailable[candidate] = str(error)
        if requested_cli == "AUTO":
            selected_cli = next((item for item in ("CODEX", "CLAUDE") if item in available), "")
        else:
            selected_cli = requested_cli if requested_cli in available else ""
        if not selected_cli:
            detail = "；".join(
                f"{item}: {unavailable.get(item, '不可用')}" for item in ("CODEX", "CLAUDE")
                if requested_cli == "AUTO" or item == requested_cli
            )
            raise DecisionProviderError(
                "OpenRouter 只提供 LLM，必须有可用的 Codex 或 Claude CLI 作为 Agent；" + detail
            )
        self.agent_cli = selected_cli
        self.available_agent_clis = tuple(item for item in ("CODEX", "CLAUDE") if item in available)
        self.executable = available[selected_cli]
        self.api_key = api_key
        self.model = model
        self.proxy_settings = dict(proxy_settings)
        self.timeout = timeout
        self.max_output_tokens = max_output_tokens
        self.responses_url = responses_url
        self.messages_url = messages_url
        self.opener = _opener(self.proxy_settings["proxy"])
        self.history_database = history_database.resolve() if history_database else None
        self.client_homes = client_homes or {
            "CODEX": Path(
                os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
            ).resolve(),
            "CLAUDE": Path(
                os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))
            ).resolve(),
        }
        self._sessions = threading.local()
        self._catalog_lock = threading.RLock()
        self._codex_model_catalog: bytes | None = None
        self._model_parameters: frozenset[str] | None = None

    def _selected_model_parameters(self) -> frozenset[str]:
        """Read the chosen model's advertised native features once per backend instance."""
        if self._model_parameters is not None:
            return self._model_parameters
        if "/" in self.executable and not Path(self.executable).exists():
            self._model_parameters = REQUIRED_MODEL_PARAMETERS
            return self._model_parameters
        parsed = urllib.parse.urlsplit(self.responses_url)
        if parsed.hostname != "openrouter.ai":
            self._model_parameters = REQUIRED_MODEL_PARAMETERS
            return self._model_parameters
        model_url = (
            self.responses_url.rsplit("/responses", 1)[0]
            + "/model/"
            + urllib.parse.quote(self.model, safe="/")
        )
        request = urllib.request.Request(
            model_url,
            headers={
                "Authorization": "Bearer " + self.api_key,
                "User-Agent": "prediction-market-agent/1",
            },
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                model_data = json.loads(response.read(1024 * 1024)).get("data") or {}
            parameters = model_data.get("supported_parameters") or []
            supported = frozenset(str(item) for item in parameters)
            missing = sorted(REQUIRED_MODEL_PARAMETERS - supported)
            if missing:
                raise DecisionProviderError(
                    f"所选 OpenRouter 模型 {self.model} 不支持 Agent CLI 所需参数："
                    + "、".join(missing)
                )
            self._model_parameters = supported
            return supported
        except DecisionProviderError:
            raise
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise DecisionProviderError(
                "无法读取所选 OpenRouter 模型的能力元数据"
            ) from error

    def _selected_codex_catalog(self) -> bytes | None:
        """Cache OpenRouter's Codex metadata for exactly the configured model."""
        if self.agent_cli != "CODEX":
            return None
        if "/" in self.executable and not Path(self.executable).exists():
            # Test doubles may use a synthetic executable path; production paths were already
            # validated by resolve_executable during construction.
            return None
        parsed = urllib.parse.urlsplit(self.responses_url)
        if parsed.hostname != "openrouter.ai":
            return None
        with self._catalog_lock:
            if self._codex_model_catalog is not None:
                return self._codex_model_catalog
            try:
                self._selected_model_parameters()
                version_output = subprocess.check_output(
                    [self.executable, "--version"],
                    text=True,
                    timeout=5,
                    env=client_subprocess_environment(
                        "CODEX", self.client_homes["CODEX"], "",
                        self.proxy_settings["no_proxy"],
                    ),
                )
                version = version_output.strip().rsplit(" ", 1)[-1]
                catalog_url = (
                    self.responses_url.rsplit("/responses", 1)[0]
                    + "/models?client_version="
                    + urllib.parse.quote(version)
                )
                request = urllib.request.Request(
                    catalog_url,
                    headers={
                        "Authorization": "Bearer " + self.api_key,
                        "User-Agent": "codex_cli_rs/" + version,
                    },
                )
                with self.opener.open(request, timeout=self.timeout) as response:
                    document = json.loads(response.read(16 * 1024 * 1024))
                models = document.get("models") or []
                selected = [
                    item
                    for item in models
                    if isinstance(item, dict) and item.get("slug") == self.model
                ]
                if not selected:
                    raise DecisionProviderError(
                        f"OpenRouter no longer advertises the selected model to Codex: {self.model}"
                    )
                self._codex_model_catalog = json.dumps(
                    {"models": selected}, separators=(",", ":")
                ).encode("utf-8")
                return self._codex_model_catalog
            except DecisionProviderError:
                raise
            except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
                raise DecisionProviderError(
                    "无法读取所选 OpenRouter 模型的 Codex 元数据"
                ) from error

    @contextmanager
    def session(self):
        """One new Codex/Claude conversation for one application round."""
        if getattr(self._sessions, "root", None) is not None:
            yield
            return
        with tempfile.TemporaryDirectory(prefix="prediction-openrouter-round-") as directory:
            self._sessions.root = Path(directory)
            self._sessions.started = False
            self._sessions.session_id = str(uuid.uuid4())
            self._sessions.thread_id = ""
            try:
                yield
            finally:
                self._sessions.root = None
                self._sessions.started = False
                self._sessions.session_id = ""
                self._sessions.thread_id = ""

    def _with_history(self, prompt: str) -> str:
        return prompt + shared_agent_history_instruction(
            self.history_database, self.client_homes
        )

    @staticmethod
    def _thread_id(stdout: str) -> str:
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "thread.started" and event.get("thread_id"):
                return str(event["thread_id"])
        return ""

    def complete(
        self, prompt: str, schema: dict[str, Any], schema_name: str
    ) -> StructuredResult:
        if self.agent_cli == "CLAUDE":
            return self._complete_claude(prompt, schema, schema_name)
        return self._complete_codex(prompt, schema, schema_name)

    def _complete_codex(
        self, prompt: str, schema: dict[str, Any], schema_name: str
    ) -> StructuredResult:
        token = secrets.token_urlsafe(32)
        selected_catalog = self._selected_codex_catalog()
        bridge = _BridgeServer(
            token,
            self.api_key,
            self.model,
            self.opener,
            self.timeout,
            self.max_output_tokens,
            self.responses_url,
            self.messages_url,
            selected_catalog,
            self._selected_model_parameters(),
        )
        thread = threading.Thread(target=bridge.serve_forever, daemon=True)
        thread.start()
        active_root = getattr(self._sessions, "root", None)
        temporary = (
            None
            if active_root is not None
            else tempfile.TemporaryDirectory(prefix="prediction-openrouter-")
        )
        try:
            root = active_root or Path(temporary.name)
            schema_path = root / f"{schema_name}.schema.json"
            output_path = root / f"{schema_name}.json"
            schema_path.write_text(json.dumps(schema), encoding="utf-8")
            output_path.unlink(missing_ok=True)
            thread_id = str(getattr(self._sessions, "thread_id", "") or "")
            provider_options = [
                "-c",
                'model_provider="openrouter_bridge"',
                "-c",
                'model_providers.openrouter_bridge.name="OpenRouter"',
                "-c",
                f'model_providers.openrouter_bridge.base_url="http://127.0.0.1:{bridge.server_port}/v1"',
                "-c",
                'model_providers.openrouter_bridge.env_key="OPENROUTER_BRIDGE_TOKEN"',
                "-c",
                'model_providers.openrouter_bridge.wire_api="responses"',
                "-c",
                "model_providers.openrouter_bridge.request_max_retries=0",
            ]
            if active_root is not None and thread_id:
                command = [
                    self.executable,
                    "--search",
                    "exec",
                    "resume",
                    thread_id,
                    "--skip-git-repo-check",
                    "--output-schema",
                    str(schema_path),
                    "--output-last-message",
                    str(output_path),
                    "--json",
                    "-m",
                    self.model,
                    *provider_options,
                    "-",
                ]
            else:
                command = [
                    self.executable,
                    "--search",
                    "exec",
                    *([] if active_root is not None else ["--ephemeral"]),
                    "--skip-git-repo-check",
                    "--sandbox",
                    "workspace-write",
                    "--color",
                    "never",
                    "--output-schema",
                    str(schema_path),
                    "--output-last-message",
                    str(output_path),
                    "-C",
                    str(root),
                    *(["--json"] if active_root is not None else []),
                    "-m",
                    self.model,
                    *provider_options,
                    "-",
                ]
            env = client_subprocess_environment(
                "CODEX", self.client_homes["CODEX"], "",
                self.proxy_settings["no_proxy"],
            )
            env.update(
                OPENROUTER_BRIDGE_TOKEN=token,
                NO_COLOR="1",
            )
            try:
                completed = subprocess.run(
                    command,
                    input=self._with_history(_openrouter_schema_prompt(prompt, schema)),
                    text=True,
                    capture_output=True,
                    timeout=self.timeout + 15,
                    env=env,
                    check=False,
                )
            except subprocess.TimeoutExpired as error:
                raw = json.dumps(
                    {
                        "timeout": True,
                        "stdout": subprocess_output_text(error.stdout),
                        "stderr": subprocess_output_text(error.stderr),
                    },
                    ensure_ascii=False,
                )
                raise DecisionProviderError(
                    f"OpenRouter timed out after {self.timeout}s", raw
                ) from error
            raw = json.dumps(
                {
                    "returncode": completed.returncode,
                    "stdout": completed.stdout,
                    "stderr": completed.stderr,
                    "final": output_path.read_text(encoding="utf-8")
                    if output_path.exists()
                    else "",
                },
                ensure_ascii=False,
            )
            if completed.returncode != 0 or not output_path.exists():
                detail = cli_failure_detail(
                    returncode=completed.returncode,
                    stdout=completed.stdout,
                    stderr=completed.stderr,
                    output_exists=output_path.exists(),
                )
                raise DecisionProviderError(
                    f"OpenRouter runtime failed: {detail}", raw
                )
            if active_root is not None and not thread_id:
                started = self._thread_id(completed.stdout)
                if not started:
                    raise DecisionProviderError(
                        "Codex did not report the new OpenRouter round session id", raw
                    )
                self._sessions.thread_id = started
            written = output_path.read_text(encoding="utf-8") if output_path.exists() else ""
            if not written.strip():
                # Nothing came back at all. Reported as a schema violation this read as "the model
                # answered badly" and sent everyone looking at prompts and schemas; the ledger row
                # said `[]`. An empty answer is a failed call, and the routing refusal that usually
                # causes it is printed by the client rather than returned here.
                raise DecisionProviderError(
                    "OpenRouter returned no content at all"
                    + (f"：{_routing_refusal(completed.stdout, completed.stderr)}"
                       if _routing_refusal(completed.stdout, completed.stderr) else ""),
                    raw,
                )
            try:
                value = _strict_schema_object(written, schema)
            except (json.JSONDecodeError, ValueError) as error:
                raise DecisionProviderError(
                    f"OpenRouter returned output that violates the required schema: {error}", raw
                ) from error
            return StructuredResult(value, raw)
        finally:
            if temporary is not None:
                temporary.cleanup()
            bridge.shutdown()
            bridge.server_close()
            thread.join(timeout=2)

    def _complete_claude(
        self, prompt: str, schema: dict[str, Any], schema_name: str
    ) -> StructuredResult:
        del schema_name
        token = secrets.token_urlsafe(32)
        model_parameters = self._selected_model_parameters()
        bridge = _BridgeServer(
            token,
            self.api_key,
            self.model,
            self.opener,
            self.timeout,
            self.max_output_tokens,
            self.responses_url,
            self.messages_url,
            None,
            model_parameters,
        )
        thread = threading.Thread(target=bridge.serve_forever, daemon=True)
        thread.start()
        active_root = getattr(self._sessions, "root", None)
        temporary = (
            None
            if active_root is not None
            else tempfile.TemporaryDirectory(prefix="prediction-openrouter-claude-")
        )
        try:
            root = active_root or Path(temporary.name)
            started = bool(getattr(self._sessions, "started", False))
            session_id = str(getattr(self._sessions, "session_id", "") or "")
            command = [
                self.executable,
                "-p",
            ]
            if active_root is None:
                command.append("--no-session-persistence")
            elif started:
                command.extend(["--resume", session_id])
            else:
                command.extend(["--session-id", session_id])
            command.extend(
                [
                    "--permission-mode",
                    "acceptEdits",
                    "--max-turns",
                    "6",
                    "--output-format",
                    "json",
                    "--json-schema",
                    json.dumps(schema, separators=(",", ":")),
                    "--model",
                    self.model,
                ]
            )
            env = client_subprocess_environment(
                "CLAUDE", self.client_homes["CLAUDE"], "",
                self.proxy_settings["no_proxy"],
            )
            env.update(
                ANTHROPIC_BASE_URL=f"http://127.0.0.1:{bridge.server_port}/api",
                ANTHROPIC_AUTH_TOKEN=token,
                ANTHROPIC_API_KEY="",
                ANTHROPIC_DEFAULT_OPUS_MODEL=self.model,
                ANTHROPIC_DEFAULT_SONNET_MODEL=self.model,
                ANTHROPIC_DEFAULT_HAIKU_MODEL=self.model,
                CLAUDE_CODE_SUBAGENT_MODEL=self.model,
                NO_COLOR="1",
            )
            try:
                completed = subprocess.run(
                    command,
                    input=self._with_history(prompt),
                    text=True,
                    capture_output=True,
                    timeout=self.timeout + 15,
                    env=env,
                    cwd=root,
                    check=False,
                )
            except subprocess.TimeoutExpired as error:
                raw = json.dumps(
                    {
                        "timeout": True,
                        "stdout": subprocess_output_text(error.stdout),
                        "stderr": subprocess_output_text(error.stderr),
                    },
                    ensure_ascii=False,
                )
                raise DecisionProviderError(
                    f"OpenRouter through Claude timed out after {self.timeout}s", raw
                ) from error
            raw = json.dumps(
                {
                    "returncode": completed.returncode,
                    "stdout": completed.stdout,
                    "stderr": completed.stderr,
                },
                ensure_ascii=False,
            )
            if completed.returncode != 0:
                detail = cli_failure_detail(
                    returncode=completed.returncode,
                    stdout=completed.stdout,
                    stderr=completed.stderr,
                    output_exists=True,
                )
                raise DecisionProviderError(
                    f"OpenRouter Claude Agent failed: {detail}", raw
                )
            try:
                envelope = json.loads(completed.stdout)
                value = envelope["structured_output"]
            except (json.JSONDecodeError, KeyError, TypeError) as error:
                raise DecisionProviderError(
                    "OpenRouter Claude Agent returned invalid structured output", raw
                ) from error
            if not isinstance(value, dict):
                raise DecisionProviderError(
                    "OpenRouter Claude Agent returned non-object structured output", raw
                )
            schema_error = _schema_error(value, schema)
            if schema_error:
                raise DecisionProviderError(
                    "OpenRouter Claude Agent returned output that violates the required schema: "
                    + schema_error,
                    raw,
                )
            if active_root is not None:
                self._sessions.started = True
            return StructuredResult(value, raw)
        finally:
            if temporary is not None:
                temporary.cleanup()
            bridge.shutdown()
            bridge.server_close()
            thread.join(timeout=2)


def initialize_openrouter_plugin(context: PluginInitializationContext) -> PluginSpec:
    """Initialize one OpenRouter configuration; the file name is its provider identity."""
    name = context.module_path.stem.lower()
    load, save, delete, storage = json_file_callbacks(
        context.working_directory / "config" / "plugins" / f"{name}.json"
    )
    configuration = PluginConfiguration(
        fields=(
            PluginConfigField(
                "OPENROUTER_API_KEY",
                "OpenRouter API Key",
                "secret",
                "用于模型目录、推理和查询当前 Key 用量；openrouter_* 配置留空时默认复用主 openrouter 配置的 Key，填写后单独覆盖。",
            ),
            PluginConfigField(
                "OPENROUTER_MANAGEMENT_API_KEY",
                "OpenRouter Management Key（可选）",
                "secret",
                "只用于官方 credits 接口查询账户充值总额、累计消费和余额；绝不用于模型推理。",
            ),
            PluginConfigField(
                "OPENROUTER_MODEL",
                "OpenRouter 模型",
                "string",
                "只列出同时支持 tools 与 structured_outputs 的模型。OpenRouter 插件私有守卫会把 Codex/Claude 的版本化 Web Search/Fetch 声明转为等价 OpenRouter server tool，但仍保持各自的 Responses/Messages 输入输出协议和用户选定模型。",
                default="",
                selection_only=True,
                choices_depend_on=("OPENROUTER_AGENT_CLI",),
            ),
            PluginConfigField(
                "OPENROUTER_AGENT_CLI",
                "Agent CLI",
                "enum",
                "OpenRouter 只提供背后的 LLM。AUTO 优先使用可用的 Codex CLI、否则使用 Claude CLI；也可固定选择其中一个。两个 CLI 都不可用时本 Provider 不会启动。CLI 保留自己的 Agent 工具、配置、规则与已安装 skills，模型请求和计费仍只走本 OpenRouter Key。",
                required=True,
                default="AUTO",
                options=AGENT_CLI_OPTIONS,
            ),
            PluginConfigField(
                "OPENROUTER_HTTP_PROXY",
                "HTTP 代理",
                "string",
                "只控制这一份 OpenRouter 配置。INHERIT 继承统一代理；也可选 DIRECT、HOST、ENVIRONMENT、SYSTEM 或填写完整 http(s):// 地址。",
                required=True,
                default="INHERIT",
            ),
            PluginConfigField(
                "OPENROUTER_TIMEOUT_SECONDS",
                "超时秒数",
                "integer",
                "每次 OpenRouter 结构化调用的超时时间。",
                required=True,
                default=180,
            ),
            PluginConfigField(
                "OPENROUTER_MAX_OUTPUT_TOKENS",
                "最大输出 token",
                "integer",
                "工具选择和最终决策共用的输出上限；过小会截断结构化结果。",
                required=True,
                default=8192,
            ),
        ),
        load_callback=load,
        save_callback=save,
        delete_callback=delete,
        storage=storage,
        choices_callback=lambda field: model_choices(
            choice_values(),
            proxy_settings=context.proxy_settings(
                configuration.load()["OPENROUTER_HTTP_PROXY"],
                field_name="OPENROUTER_HTTP_PROXY",
            ),
        ),
        choice_fields=("OPENROUTER_MODEL",),
    )

    def effective_values() -> dict[str, Any]:
        values = configuration.load()
        if name == "openrouter" or str(values.get("OPENROUTER_API_KEY", "")).strip():
            return values
        shared_load, _save, _delete, _storage = json_file_callbacks(
            context.working_directory / "config" / "plugins" / "openrouter.json"
        )
        shared_key = str(shared_load().get("OPENROUTER_API_KEY", "")).strip()
        return {**values, "OPENROUTER_API_KEY": shared_key}

    def choice_values() -> dict[str, Any]:
        values = effective_values()
        requested = str(values.get("OPENROUTER_AGENT_CLI", "AUTO") or "AUTO").upper()
        available: list[str] = []
        for cli_name, field_name, default in (
            ("CODEX", "CODEX_CLI_PATH", "codex"),
            ("CLAUDE", "CLAUDE_CLI_PATH", "claude"),
        ):
            cli_load, _save, _delete, _storage = json_file_callbacks(
                context.working_directory
                / "config"
                / "plugins"
                / f"{cli_name.lower()}.json"
            )
            try:
                resolve_executable(
                    str(cli_load().get(field_name, default) or default).strip()
                )
                available.append(cli_name)
            except DecisionProviderError:
                pass
        resolved = (
            next((item for item in ("CODEX", "CLAUDE") if item in available), "CODEX")
            if requested == "AUTO"
            else requested
        )
        return {**values, "_RESOLVED_AGENT_CLI": resolved}

    def factory(config):
        history_database = getattr(config, "session_db", None)
        values = effective_values()
        timeout = int(values["OPENROUTER_TIMEOUT_SECONDS"])
        max_output_tokens = int(values["OPENROUTER_MAX_OUTPUT_TOKENS"])
        if timeout < 10:
            raise ValueError("OPENROUTER_TIMEOUT_SECONDS must be at least 10")
        if max_output_tokens < 512:
            raise ValueError("OPENROUTER_MAX_OUTPUT_TOKENS must be at least 512")
        cli_paths: dict[str, str] = {}
        for cli_name, field_name, default in (
            ("CODEX", "CODEX_CLI_PATH", "codex"),
            ("CLAUDE", "CLAUDE_CLI_PATH", "claude"),
        ):
            cli_load, _save, _delete, _storage = json_file_callbacks(
                context.working_directory / "config" / "plugins" / f"{cli_name.lower()}.json"
            )
            cli_paths[cli_name] = str(cli_load().get(field_name, default) or default).strip()
        return OpenRouterBackend(
            name,
            cli_paths["CODEX"],
            values.get("OPENROUTER_API_KEY", "").strip(),
            values["OPENROUTER_MODEL"].strip(),
            context.proxy_settings(
                values["OPENROUTER_HTTP_PROXY"],
                field_name="OPENROUTER_HTTP_PROXY",
            ),
            timeout,
            max_output_tokens,
            agent_cli=values["OPENROUTER_AGENT_CLI"],
            cli_paths=cli_paths,
            history_database=history_database,
            client_homes=configured_client_homes(context.working_directory),
        )

    def readiness():
        try:
            factory(None)
            return PluginReadiness(True)
        except (ValueError, KeyError, DecisionProviderError) as error:
            return PluginReadiness(False, (str(error),))

    account_control = OpenRouterAccountControl(
        configuration, context, values_loader=effective_values
    )

    return PluginSpec(
        "decision_provider",
        name,
        "用 Codex 或 Claude CLI 提供 Agent 能力，背后的 LLM 通过 OpenRouter 严格结构化接口调用；Key 默认复用主 OpenRouter 配置并可单独覆盖。",
        str(context.module_path),
        factory,
        configuration,
        lambda: None,
        readiness_callback=readiness,
        controls=account_control.controls,
        network_routes_callback=lambda: configured_proxy_route(
            load,
            "OPENROUTER_HTTP_PROXY",
            default="INHERIT",
            resolver=lambda value: context.proxy_settings(
                value, field_name="OPENROUTER_HTTP_PROXY"
            ),
        ),
    )


def initialize_plugin(context: PluginInitializationContext) -> PluginSpec:
    return initialize_openrouter_plugin(context)
