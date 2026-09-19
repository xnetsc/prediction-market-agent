from __future__ import annotations

import json
import secrets
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

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
    resolve_executable,
    subprocess_environment,
)


OPENROUTER_API_BASE = "https://openrouter.ai/api/v1"
OPENROUTER_MODELS_URL = (
    OPENROUTER_API_BASE + "/models?supported_parameters=structured_outputs"
)
OPENROUTER_KEY_URL = OPENROUTER_API_BASE + "/key"
OPENROUTER_CREDITS_URL = OPENROUTER_API_BASE + "/credits"
STRUCTURED_OUTPUT_PARAMETER = "structured_outputs"


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
    """Return only models that OpenRouter advertises for structured output."""
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
        supported = []
        for item in data:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                continue
            parameters = item.get("supported_parameters")
            if not isinstance(parameters, list) or STRUCTURED_OUTPUT_PARAMETER not in parameters:
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
        raise ValueError("获取 OpenRouter 结构化输出模型列表失败，请检查网络、Key 和代理") from None


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
    ):
        self.configuration = configuration
        self.context = context
        self.key_url = key_url
        self.credits_url = credits_url
        self.lock = threading.RLock()
        self.usage: dict[str, Any] = {}
        self.controls = PluginControls(self.snapshot, self.action)

    def proxy_settings(self) -> dict[str, str]:
        values = self.configuration.load()
        return self.context.proxy_settings(
            values["OPENROUTER_HTTP_PROXY"], field_name="OPENROUTER_HTTP_PROXY"
        )

    def snapshot(self) -> dict[str, Any]:
        values = self.configuration.load()
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
        return {
            "control_type": "openrouter",
            "state": "configured" if configured else "incomplete",
            "message": (
                "推理 Key 与结构化输出模型均已配置"
                if configured
                else "请填写推理 API Key 并选择结构化输出模型"
            ),
            "model": model,
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
        configured = self.configuration.load()
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


def _codex_messages(body: dict[str, Any]) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        messages.append({"role": "system", "content": instructions})
    for item in body.get("input") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        text = "\n".join(
            str(part.get("text", ""))
            for part in item.get("content") or []
            if isinstance(part, dict)
            and part.get("type") in {"input_text", "output_text"}
        )
        role = str(item.get("role") or "user")
        if role == "developer":
            role = "system"
        if role not in {"system", "user", "assistant"}:
            continue
        messages.append({"role": role, "content": text})
    return messages


def _responses_event_stream(answer: str, usage: dict[str, Any]) -> bytes:
    response_id = "resp_" + secrets.token_hex(12)
    item_id = "msg_" + secrets.token_hex(12)
    content = {"type": "output_text", "text": answer, "annotations": []}
    item = {
        "id": item_id,
        "type": "message",
        "status": "completed",
        "role": "assistant",
        "content": [content],
    }
    response_usage = {
        "input_tokens": int(usage.get("prompt_tokens") or 0),
        "output_tokens": int(usage.get("completion_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
    }
    events = (
        {
            "type": "response.created",
            "response": {
                "id": response_id,
                "object": "response",
                "status": "in_progress",
                "output": [],
            },
        },
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {**item, "status": "in_progress", "content": []},
        },
        {
            "type": "response.content_part.added",
            "item_id": item_id,
            "output_index": 0,
            "content_index": 0,
            "part": {**content, "text": ""},
        },
        {
            "type": "response.output_text.delta",
            "item_id": item_id,
            "output_index": 0,
            "content_index": 0,
            "delta": answer,
        },
        {
            "type": "response.output_text.done",
            "item_id": item_id,
            "output_index": 0,
            "content_index": 0,
            "text": answer,
        },
        {
            "type": "response.content_part.done",
            "item_id": item_id,
            "output_index": 0,
            "content_index": 0,
            "part": content,
        },
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        {
            "type": "response.completed",
            "response": {
                "id": response_id,
                "object": "response",
                "status": "completed",
                "output": [item],
                "usage": response_usage,
            },
        },
    )
    return (
        "".join(
            "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
            for event in events
        )
        + "data: [DONE]\n\n"
    ).encode("utf-8")


class _BridgeServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        token: str,
        api_key: str,
        model: str,
        opener: urllib.request.OpenerDirector,
        timeout: int,
        max_output_tokens: int,
        chat_url: str,
    ):
        self.token = token
        self.api_key = api_key
        self.model = model
        self.opener = opener
        self.timeout = timeout
        self.max_output_tokens = max_output_tokens
        self.chat_url = chat_url
        super().__init__(("127.0.0.1", 0), _BridgeHandler)


class _BridgeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def _write(self, status: int, content_type: str, payload: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:
        server: _BridgeServer = self.server  # type: ignore[assignment]
        if self.path != "/v1/responses" or self.headers.get("Authorization") != "Bearer " + server.token:
            self._write(404, "application/json", b'{"error":{"message":"not found"}}')
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 32 * 1024 * 1024:
                raise ValueError("invalid request size")
            body = json.loads(self.rfile.read(length))
            output_format = body["text"]["format"]
            if output_format.get("type") != "json_schema":
                raise ValueError("Codex request did not include a JSON schema")
            request_body = {
                "model": server.model,
                "messages": _codex_messages(body),
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": output_format.get("name", "codex_output_schema"),
                        "strict": bool(output_format.get("strict", True)),
                        "schema": output_format["schema"],
                    },
                },
                "provider": {"require_parameters": True},
                "max_tokens": server.max_output_tokens,
                "temperature": 0,
            }
            request = urllib.request.Request(
                server.chat_url,
                data=json.dumps(request_body).encode("utf-8"),
                headers={
                    "Authorization": "Bearer " + server.api_key,
                    "Content-Type": "application/json",
                    "User-Agent": "prediction-market-agent/1",
                },
                method="POST",
            )
            with server.opener.open(request, timeout=server.timeout) as response:
                result = json.load(response)
            answer = result["choices"][0]["message"]["content"]
            if isinstance(answer, dict):
                answer = json.dumps(answer, ensure_ascii=False)
            if not isinstance(answer, str):
                raise ValueError("OpenRouter returned no text response")
            payload = _responses_event_stream(answer, result.get("usage") or {})
            self._write(200, "text/event-stream", payload)
        except urllib.error.HTTPError as error:
            detail = error.read(1024 * 1024)
            self._write(error.code, "application/json", detail)
        except Exception as error:
            payload = json.dumps(
                {"error": {"message": str(error)[:1000], "type": "openrouter_bridge"}}
            ).encode("utf-8")
            self._write(502, "application/json", payload)


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
        chat_url: str = OPENROUTER_API_BASE + "/chat/completions",
    ):
        if not api_key or not model:
            raise DecisionProviderError("请填写 OpenRouter API Key 和模型 ID")
        self.name = name
        self.executable = resolve_executable(executable)
        self.api_key = api_key
        self.model = model
        self.proxy_settings = dict(proxy_settings)
        self.timeout = timeout
        self.max_output_tokens = max_output_tokens
        self.chat_url = chat_url
        self.opener = _opener(self.proxy_settings["proxy"])

    def complete(
        self, prompt: str, schema: dict[str, Any], schema_name: str
    ) -> StructuredResult:
        token = secrets.token_urlsafe(32)
        bridge = _BridgeServer(
            token,
            self.api_key,
            self.model,
            self.opener,
            self.timeout,
            self.max_output_tokens,
            self.chat_url,
        )
        thread = threading.Thread(target=bridge.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(prefix="prediction-openrouter-") as directory:
                root = Path(directory)
                home = root / "home"
                codex_home = home / ".codex"
                codex_home.mkdir(parents=True)
                schema_path = root / f"{schema_name}.schema.json"
                output_path = root / f"{schema_name}.json"
                schema_path.write_text(json.dumps(schema), encoding="utf-8")
                command = [
                    self.executable,
                    "exec",
                    "--ephemeral",
                    "--skip-git-repo-check",
                    "--ignore-rules",
                    "--sandbox",
                    "read-only",
                    "--color",
                    "never",
                    "--output-schema",
                    str(schema_path),
                    "--output-last-message",
                    str(output_path),
                    "-C",
                    str(root),
                    "-m",
                    self.model,
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
                    "-",
                ]
                env = subprocess_environment("", self.proxy_settings["no_proxy"])
                env.update(
                    HOME=str(home),
                    CODEX_HOME=str(codex_home),
                    OPENROUTER_BRIDGE_TOKEN=token,
                    NO_COLOR="1",
                )
                try:
                    completed = subprocess.run(
                        command,
                        input=prompt,
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
                            "stdout": error.stdout or "",
                            "stderr": error.stderr or "",
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
                    raise DecisionProviderError(
                        f"OpenRouter runtime failed: {completed.stderr[-1000:]}", raw
                    )
                try:
                    value = json.loads(output_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError as error:
                    raise DecisionProviderError(
                        "OpenRouter returned invalid JSON", raw
                    ) from error
                if not isinstance(value, dict):
                    raise DecisionProviderError(
                        "OpenRouter returned non-object JSON", raw
                    )
                return StructuredResult(value, raw)
        finally:
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
                "用于模型目录、推理和查询当前 Key 用量；仅发送给 OpenRouter，保存值不会在界面回显。",
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
                "只列出 OpenRouter 当前声明支持结构化输出的模型；仍会在每次请求时要求具体供应端点支持该参数。",
                default="",
                selection_only=True,
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
            configuration.load(),
            proxy_settings=context.proxy_settings(
                configuration.load()["OPENROUTER_HTTP_PROXY"],
                field_name="OPENROUTER_HTTP_PROXY",
            ),
        ),
        choice_fields=("OPENROUTER_MODEL",),
    )

    def factory(config):
        del config
        values = configuration.load()
        timeout = int(values["OPENROUTER_TIMEOUT_SECONDS"])
        max_output_tokens = int(values["OPENROUTER_MAX_OUTPUT_TOKENS"])
        if timeout < 10:
            raise ValueError("OPENROUTER_TIMEOUT_SECONDS must be at least 10")
        if max_output_tokens < 512:
            raise ValueError("OPENROUTER_MAX_OUTPUT_TOKENS must be at least 512")
        return OpenRouterBackend(
            name,
            "codex",
            values.get("OPENROUTER_API_KEY", "").strip(),
            values["OPENROUTER_MODEL"].strip(),
            context.proxy_settings(
                values["OPENROUTER_HTTP_PROXY"],
                field_name="OPENROUTER_HTTP_PROXY",
            ),
            timeout,
            max_output_tokens,
        )

    def readiness():
        try:
            factory(None)
            return PluginReadiness(True)
        except (ValueError, KeyError, DecisionProviderError) as error:
            return PluginReadiness(False, (str(error),))

    account_control = OpenRouterAccountControl(configuration, context)

    return PluginSpec(
        "decision_provider",
        name,
        "通过插件私有的本机 schema 桥调用 OpenRouter 严格结构化输出。每个插件文件保存一份独立配置。",
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
