"""Local WebGPU evaluator, using the typed state/questions protocol."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from prediction_market_agent.plugin_system.config_io import json_file_callbacks
from prediction_market_agent.plugin_system.discovery import (
    PluginConfigField,
    PluginConfiguration,
    PluginInitializationContext,
    PluginReadiness,
    PluginSpec,
)
from prediction_market_agent.plugin_system.network_diagnostics import configured_proxy_route

from prediction_market_agent.plugins.evaluators.jev import (
    SchemaDecisionEvaluator, _chat_completions_endpoint, _opener,
)


MODEL = "convaiinnovations/laya"
DEFAULT_ENDPOINT = "http://host.proxy.internal:8899/v1"


class LayaDecisionEvaluator(SchemaDecisionEvaluator):
    """One candidate per call keeps its four typed questions below Laya's six-question limit."""

    name = "laya"
    per_candidate_requests = True

    def __init__(self, endpoint: str, proxy: str, timeout: int) -> None:
        super().__init__(
            api_key="",
            model=MODEL,
            proxy=proxy,
            timeout=timeout,
            batch_size=1,
            endpoint=_chat_completions_endpoint(endpoint),
            connection_name="laya",
            protocol="chat_json",
            allow_http=True,
        )


def _health_url(endpoint: str) -> str:
    parsed = urllib.parse.urlsplit(endpoint.strip().rstrip("/"))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Laya 服务地址须为有效的 HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Laya 服务地址不能包含账号、查询参数或片段")
    if parsed.path not in {"", "/v1"}:
        raise ValueError("Laya 服务地址须指向服务根路径或 /v1")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "/health", "", ""))


def _check_service(endpoint: str, proxy: str) -> None:
    request = urllib.request.Request(_health_url(endpoint), headers={"Accept": "application/json"})
    try:
        with _opener(proxy).open(request, timeout=5) as response:
            health: Any = json.loads(response.read(64 * 1024))
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise ValueError(f"无法连接 Laya 服务：{error}") from error
    if not isinstance(health, dict) or not health.get("ready"):
        raise ValueError("Laya 模型尚未就绪")
    if health.get("backend") != "webgpu":
        raise ValueError("Laya 当前未使用 WebGPU，不能作为粗筛评估器")
    if health.get("model") != MODEL:
        raise ValueError("Laya 服务返回的模型不是 convaiinnovations/laya")
    questions = ((health.get("surface") or {}).get("takes") or {}).get("questions") or {}
    types = questions.get("types") or {}
    if not {"choice", "score", "noul"}.issubset(types) or int(questions.get("max") or 0) < 4:
        raise ValueError("Laya 服务未声明粗筛所需的结构化问答协议")


def initialize_plugin(context: PluginInitializationContext) -> PluginSpec:
    load, save, delete, storage = json_file_callbacks(
        context.working_directory / "config" / "plugins" / "laya.json"
    )
    configuration = PluginConfiguration(
        fields=(
            PluginConfigField(
                "LAYA_BASE_URL", "Laya 服务地址", "string",
                "填写机器人能够访问的本地 WebGPU 服务地址；默认 Compose 使用 host.proxy.internal，其他部署须以实测可达地址为准。仅连接测试不会启用本插件。",
                required=True, default=DEFAULT_ENDPOINT,
            ),
            PluginConfigField(
                "LAYA_HTTP_PROXY", "HTTP 代理", "string",
                "只控制 Laya 插件。直连本机服务选 DIRECT；远端服务可选 INHERIT 或独立代理，不影响其他层级代理。",
                required=True, default="DIRECT",
            ),
            PluginConfigField(
                "LAYA_TIMEOUT_SECONDS", "单次请求超时秒数", "integer",
                "Laya 每次最多处理一个候选的四个结构化问题；超时会放弃本次粗筛，不会代替交易决策。",
                required=True, default=30,
            ),
        ),
        load_callback=load, save_callback=save, delete_callback=delete, storage=storage,
    )

    def proxy_settings(values: dict[str, Any]) -> dict[str, str]:
        return context.proxy_settings(
            str(values.get("LAYA_HTTP_PROXY", "DIRECT")), field_name="LAYA_HTTP_PROXY"
        )

    def factory(_config: Any) -> LayaDecisionEvaluator:
        values = configuration.load()
        endpoint = str(values["LAYA_BASE_URL"]).strip()
        timeout = int(values["LAYA_TIMEOUT_SECONDS"])
        proxy = proxy_settings(values)["proxy"]
        evaluator = LayaDecisionEvaluator(endpoint, proxy, timeout)
        _check_service(endpoint, proxy)
        return evaluator

    def readiness() -> PluginReadiness:
        try:
            factory(None)
            return PluginReadiness(True)
        except (KeyError, TypeError, ValueError) as error:
            return PluginReadiness(False, (str(error),))

    return PluginSpec(
        "decision_evaluator", "laya",
        "本地 WebGPU 粗筛评估器，也可作为 Agent 事实分类工具：按 state/questions → answers 协议评估候选与续扫，不代替交易决策；服务不就绪时不启用。",
        str(context.module_path), factory, configuration, lambda: None,
        readiness_callback=readiness,
        network_routes_callback=lambda: configured_proxy_route(
            load, "LAYA_HTTP_PROXY", default="DIRECT",
            resolver=lambda _value: proxy_settings(configuration.load()),
        ),
    )
