from __future__ import annotations
from prediction_market_agent.plugin_system.network_diagnostics import configured_proxy_route

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from prediction_market_agent.agent.decision import DecisionProviderError, StructuredResult, SYSTEM_INSTRUCTIONS
from prediction_market_agent.plugin_system.config_io import json_file_callbacks, resolve_plugin_proxy
from prediction_market_agent.plugin_system.discovery import PluginConfigField, PluginConfiguration, PluginInitializationContext, PluginReadiness, PluginSpec


PRESETS = (
    {"name": "openrouter", "label": "OpenRouter", "values": {
        "COMPATIBLE_API_BASE": "https://openrouter.ai/api/v1", "COMPATIBLE_API_KEY": "",
        "COMPATIBLE_MODEL": "", "COMPATIBLE_EXTRA_HEADERS_JSON": "{}",
        "COMPATIBLE_HTTP_PROXY": "DIRECT", "COMPATIBLE_TIMEOUT_SECONDS": 120}},
    {"name": "openai", "label": "OpenAI", "values": {
        "COMPATIBLE_API_BASE": "https://api.openai.com/v1", "COMPATIBLE_API_KEY": "",
        "COMPATIBLE_MODEL": "", "COMPATIBLE_EXTRA_HEADERS_JSON": "{}",
        "COMPATIBLE_HTTP_PROXY": "DIRECT", "COMPATIBLE_TIMEOUT_SECONDS": 120}},
)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward an account's Authorization header to a redirect destination.
        return None


def model_choices(values: dict[str, Any]) -> list[dict[str, str]]:
    url = values["COMPATIBLE_API_BASE"].rstrip("/") + "/models"
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}):
        raise ValueError("模型接口须使用 HTTPS，本地回环地址除外")
    if parsed.username or parsed.password:
        raise ValueError("请在密钥字段配置凭据，不要放在 URL 中")
    headers = json.loads(values["COMPATIBLE_EXTRA_HEADERS_JSON"])
    if not isinstance(headers, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items()):
        raise ValueError("附加请求头必须是字符串键值对象")
    key = values.get("COMPATIBLE_API_KEY", "").strip()
    if key:
        headers["Authorization"] = "Bearer " + key
    proxy = resolve_plugin_proxy(values["COMPATIBLE_HTTP_PROXY"], field_name="COMPATIBLE_HTTP_PROXY")
    opener = urllib.request.build_opener(NoRedirect(), urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {}))
    try:
        with opener.open(urllib.request.Request(url, headers=headers), timeout=15) as response:
            data = json.loads(response.read(8 * 1024 * 1024))["data"]
        if not isinstance(data, list):
            raise ValueError("Invalid model list")
        return sorted([{"value": item["id"], "label": str(item.get("name") or item["id"])}
                       for item in data if isinstance(item, dict) and isinstance(item.get("id"), str)],
                      key=lambda item: item["value"])
    except urllib.error.HTTPError as error:
        raise ValueError(f"获取模型列表失败：HTTP {error.code}，请检查 URL、Key 和代理") from None
    except (OSError, ValueError, KeyError, TypeError):
        raise ValueError("获取模型列表失败：请检查网络及服务商是否支持 /models；仍可手动填写模型 ID") from None


class OpenAICompatibleBackend:
    name = "openai_compatible"

    def __init__(self, base_url: str, api_key: str, model: str, headers_json: str, proxy: str, timeout: int):
        if not api_key or not model:
            raise DecisionProviderError("请填写 API Key 和模型 ID")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        try:
            self.extra_headers = json.loads(headers_json)
        except json.JSONDecodeError as error:
            raise DecisionProviderError("COMPATIBLE_EXTRA_HEADERS_JSON is invalid") from error
        if not isinstance(self.extra_headers, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in self.extra_headers.items()):
            raise DecisionProviderError("Compatible extra headers must be a string-to-string object")
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy}) if proxy else urllib.request.ProxyHandler({})
        self.opener = urllib.request.build_opener(handler, NoRedirect())

    def complete(self, prompt: str, schema: dict[str, Any], schema_name: str) -> StructuredResult:
        url = f"{self.base_url}/chat/completions"
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise DecisionProviderError("Compatible API must use HTTPS unless it is localhost")
        body = {"model": self.model, "messages": [{"role": "system", "content": SYSTEM_INSTRUCTIONS}, {"role": "user", "content": prompt}], "response_format": {"type": "json_schema", "json_schema": {"name": schema_name, "strict": True, "schema": schema}}, "max_tokens": 1200, "temperature": 0}
        request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json", "User-Agent": "prediction-market-agent/1", **self.extra_headers, "Authorization": f"Bearer {self.api_key}"}, method="POST")
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise DecisionProviderError(f"Compatible API HTTP {error.code}: {detail[:1000]}", detail) from error
        except urllib.error.URLError as error:
            raise DecisionProviderError(f"Compatible API connection failed: {error.reason}") from error
        try:
            content = json.loads(raw)["choices"][0]["message"]["content"]
            value = content if isinstance(content, dict) else json.loads(content)
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as error:
            raise DecisionProviderError("Compatible API returned invalid structured output", raw) from error
        if not isinstance(value, dict):
            raise DecisionProviderError("Compatible API returned non-object JSON", raw)
        return StructuredResult(value, raw)


def initialize_plugin(context: PluginInitializationContext) -> PluginSpec:
    load, save, delete, storage = json_file_callbacks(context.working_directory / "config" / "plugins" / "openai_compatible.json")
    configuration = PluginConfiguration(fields=(
        PluginConfigField("COMPATIBLE_API_BASE", "API URL", "string", "OpenAI Chat Completions 兼容服务的根地址；默认 OpenRouter，可修改。模型列表从此地址的 /models 获取。", required=True, default="https://openrouter.ai/api/v1"),
        PluginConfigField("COMPATIBLE_API_KEY", "API Key", "secret", "OpenAI 官方、OpenRouter 或其他兼容服务的访问密钥。"),
        PluginConfigField("COMPATIBLE_MODEL", "API 模型 ID", "string", "打开配置后自动获取已保存服务的模型，可按名称或 ID 搜索，也可手动填写。修改 URL、Key 或本插件代理后先保存，再刷新列表；选择模型后保存生效。留空不启动此后端。", default=""),
        PluginConfigField("COMPATIBLE_EXTRA_HEADERS_JSON", "附加请求头 JSON", "string", "服务商要求的字符串到字符串 HTTP 请求头 JSON 对象。", required=True, default="{}"),
        PluginConfigField("COMPATIBLE_HTTP_PROXY", "HTTP 代理", "string", "只控制此兼容 API 插件，不读取 Claude/Codex 的代理。默认 DIRECT（直连）；只有显式填写 SYSTEM 或完整 http(s):// 地址才使用代理。", required=True, default="DIRECT"),
        PluginConfigField("COMPATIBLE_TIMEOUT_SECONDS", "超时秒数", "integer", "每次兼容 API 调用的超时时间。", required=True, default=120),
    ), load_callback=load, save_callback=save, delete_callback=delete, storage=storage,
        choices_callback=lambda field: model_choices(configuration.load()),
        choice_fields=("COMPATIBLE_MODEL",), presets=PRESETS)

    def factory(config):
        del config
        values = configuration.load()
        timeout = int(values["COMPATIBLE_TIMEOUT_SECONDS"])
        if timeout < 10:
            raise ValueError("COMPATIBLE_TIMEOUT_SECONDS must be at least 10")
        return OpenAICompatibleBackend(values["COMPATIBLE_API_BASE"].strip(), values.get("COMPATIBLE_API_KEY", "").strip(), values["COMPATIBLE_MODEL"].strip(), values["COMPATIBLE_EXTRA_HEADERS_JSON"].strip(), resolve_plugin_proxy(values["COMPATIBLE_HTTP_PROXY"], field_name="COMPATIBLE_HTTP_PROXY"), timeout)

    def readiness():
        try:
            factory(None)
            return PluginReadiness(True)
        except (ValueError, KeyError, DecisionProviderError) as error:
            return PluginReadiness(False, (str(error),))

    return PluginSpec("decision_provider", "openai_compatible", "通过 OpenAI Chat Completions 兼容 API（含 OpenAI/OpenRouter）进行决策。", str(context.module_path), factory, configuration, lambda: None, readiness_callback=readiness, network_routes_callback=lambda: configured_proxy_route(load, "COMPATIBLE_HTTP_PROXY", default="DIRECT"))
