from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from prediction_market_agent.agent.decision import DecisionProviderError, StructuredResult, SYSTEM_INSTRUCTIONS
from prediction_market_agent.sdk.config_io import json_file_callbacks, resolve_plugin_proxy
from prediction_market_agent.sdk.discovery import PluginConfigField, PluginConfiguration, PluginInitializationContext, PluginSpec


class OpenAICompatibleBackend:
    name = "openai_compatible"

    def __init__(self, base_url: str, api_key: str, model: str, headers_json: str, proxy: str, timeout: int):
        if not api_key or not model:
            raise DecisionProviderError("Compatible API key and model are required")
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
        self.opener = urllib.request.build_opener(handler)

    def complete(self, prompt: str, schema: dict[str, Any], schema_name: str) -> StructuredResult:
        url = f"{self.base_url}/chat/completions"
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise DecisionProviderError("Compatible API must use HTTPS unless it is localhost")
        body = {"model": self.model, "messages": [{"role": "system", "content": SYSTEM_INSTRUCTIONS}, {"role": "user", "content": prompt}], "response_format": {"type": "json_schema", "json_schema": {"name": schema_name, "strict": True, "schema": schema}}, "max_tokens": 1200, "temperature": 0}
        request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json", "User-Agent": "prediction-market-agent/1", **self.extra_headers}, method="POST")
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
    load, save, storage = json_file_callbacks(context.working_directory / "config" / "plugins" / "openai_compatible.json")
    configuration = PluginConfiguration(fields=(
        PluginConfigField("COMPATIBLE_API_BASE", "API URL", "string", "OpenAI Chat Completions 兼容服务的 HTTPS 根地址。", required=True),
        PluginConfigField("COMPATIBLE_API_KEY", "API Key", "secret", "OpenAI 官方、OpenRouter 或其他兼容服务的访问密钥。"),
        PluginConfigField("COMPATIBLE_MODEL", "模型", "string", "兼容 API 请求使用的模型标识。"),
        PluginConfigField("COMPATIBLE_EXTRA_HEADERS_JSON", "附加请求头 JSON", "string", "服务商要求的字符串到字符串 HTTP 请求头 JSON 对象。", required=True),
        PluginConfigField("COMPATIBLE_HTTP_PROXY", "HTTP 代理", "string", "兼容 API 独立使用的代理；DIRECT、SYSTEM 或 http(s) URL。", required=True),
        PluginConfigField("COMPATIBLE_TIMEOUT_SECONDS", "超时秒数", "integer", "每次兼容 API 调用的超时时间。", required=True),
    ), load_callback=load, save_callback=save, storage=storage)

    def factory(config):
        del config
        values = configuration.load()
        timeout = int(values["COMPATIBLE_TIMEOUT_SECONDS"])
        if timeout < 10:
            raise ValueError("COMPATIBLE_TIMEOUT_SECONDS must be at least 10")
        return OpenAICompatibleBackend(values["COMPATIBLE_API_BASE"].strip(), values.get("COMPATIBLE_API_KEY", "").strip(), values["COMPATIBLE_MODEL"].strip(), values["COMPATIBLE_EXTRA_HEADERS_JSON"].strip(), resolve_plugin_proxy(values["COMPATIBLE_HTTP_PROXY"], field_name="COMPATIBLE_HTTP_PROXY"), timeout)

    return PluginSpec("decision_provider", "openai_compatible", "通过 OpenAI Chat Completions 兼容 API（含 OpenAI/OpenRouter）进行决策。", str(context.module_path), factory, configuration, lambda: None)
