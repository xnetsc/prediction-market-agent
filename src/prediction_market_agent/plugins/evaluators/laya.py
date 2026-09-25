"""Local WebGPU evaluator, using the typed state/questions protocol."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from contextlib import contextmanager
from typing import Any

from prediction_market_agent.agent.decision_evaluator import (
    DecisionEvaluatorBudgetExhausted, DecisionEvaluatorError,
)

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

    def __init__(
        self, endpoint: str, proxy: str, timeout: int, *, queue_wait_seconds: int = 120,
        max_questions: int = 6, state_kinds: tuple[str, ...] | None = None,
    ) -> None:
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
        self.health_endpoint = _health_url(endpoint)
        self.queue_wait_seconds = max(1, int(queue_wait_seconds))
        self.max_questions = max(1, int(max_questions))
        self.state_kinds = frozenset(state_kinds or ("text", "json"))
        self._queue = deque()
        self._queue_condition = threading.Condition()
        self._active = False
        self._closed = False

    @property
    def benchmark_status(self) -> dict[str, Any]:
        """Read the service-owned benchmark; never perform inference from the robot."""
        try:
            request = urllib.request.Request(
                self.health_endpoint, headers={"Accept": "application/json"}
            )
            with _opener(self.proxy).open(request, timeout=2) as response:
                payload = json.loads(response.read(64 * 1024))
            benchmark = payload.get("benchmark") if isinstance(payload, dict) else None
            if isinstance(benchmark, dict) and isinstance(benchmark.get("status"), str):
                return benchmark
            return {"status": "unavailable", "error": "Laya 服务未返回测速状态"}
        except (urllib.error.URLError, OSError, ValueError) as error:
            return {"status": "unavailable", "error": str(error)[:300]}

    def answer_questions(self, state: dict[str, Any], questions: dict[str, Any]) -> dict[str, Any]:
        if isinstance(state, dict) and (
            state.get("type") in {"image", "multimodal"} or state.get("image") or state.get("images")
        ):
            raise DecisionEvaluatorError(
                "Laya evaluator is text-only; use a vision-capable application"
            )
        return super().answer_questions(state, questions)

    def close(self) -> None:
        with self._queue_condition:
            self._closed = True
            self._queue_condition.notify_all()

    @contextmanager
    def _exclusive(self, *, deadline: float | None = None):
        """FIFO gate for robot calls; service owns the GPU queue and its benchmark."""
        ticket = object()
        queue_deadline = time.monotonic() + self.queue_wait_seconds
        with self._queue_condition:
            if self._closed:
                raise DecisionEvaluatorError("Laya evaluator is closed")
            if len(self._queue) >= 64:
                raise DecisionEvaluatorError("Laya request queue is full")
            self._queue.append(ticket)
            while self._active or self._queue[0] is not ticket:
                if self._closed:
                    self._queue.remove(ticket)
                    self._queue_condition.notify_all()
                    raise DecisionEvaluatorError("Laya evaluator is closed")
                remaining = queue_deadline - time.monotonic()
                if deadline is not None:
                    remaining = min(remaining, deadline - time.monotonic())
                if remaining <= 0:
                    self._queue.remove(ticket)
                    self._queue_condition.notify_all()
                    if deadline is not None and time.monotonic() >= deadline:
                        raise DecisionEvaluatorBudgetExhausted("scan ended while waiting for Laya")
                    raise DecisionEvaluatorError("Laya queue wait timed out; no GPU request was sent")
                self._queue_condition.wait(remaining)
            self._active = True
        try:
            yield
        finally:
            with self._queue_condition:
                self._active = False
                self._queue.popleft()
                self._queue_condition.notify_all()

    @staticmethod
    def _compact_state(state: Any) -> Any:
        """Keep task facts and deadlines; omit verbose framework bookkeeping and repeated prose."""
        if not isinstance(state, dict):
            return state
        workflow = state.get("workflow")
        if workflow == "candidate_evaluation":
            candidates = []
            for item in state.get("candidates") or []:
                if not isinstance(item, dict):
                    continue
                history = item.get("history") or {}
                candidates.append({
                    "key": item.get("key"),
                    "question": str(item.get("question") or item.get("title") or "")[:260],
                    "description": str(item.get("description") or "")[:160],
                    "category": item.get("category"), "status": item.get("status"),
                    "end_time_ms": item.get("end_time_ms"),
                    "liquidity_usdt": item.get("liquidity_usdt"),
                    "volume_usdt": item.get("volume_usdt"),
                    "prices": [entry.get("displayed_probability")
                               for entry in (item.get("outcomes") or [])[:4]
                               if isinstance(entry, dict)],
                    "history": {
                        "screened": (history.get("screened") or [])[-1:],
                        "decided": [
                            {"action": entry.get("action"),
                             "revisit_when": str(entry.get("revisit_when") or "")[:80]}
                            for entry in (history.get("decided") or [])[-2:]
                            if isinstance(entry, dict)
                        ],
                    } if isinstance(history, dict) else {},
                })
            return {"workflow": workflow, "candidates": candidates}
        if workflow == "continuation":
            scan = state.get("run_state") or {}
            return {
                "workflow": workflow,
                "pages_scanned": scan.get("pages_scanned"),
                "topics_seen": scan.get("topics_seen"),
                "last_page": state.get("last_page"),
                "frontier": [{"id": item.get("candidate_id"), "action": item.get("typed_action"),
                              "quality": item.get("typed_quality")}
                             for item in (state.get("frontier") or [])[:12]
                             if isinstance(item, dict)],
            }
        if workflow == "decision_worth_screening":
            return {
                "workflow": workflow,
                "candidates": [{"key": item.get("key"), "question": str(item.get("question") or "")[:240],
                                "price": item.get("displayed_probability"),
                                "previous_verdict": item.get("previous_verdict"),
                                "history": item.get("history")}
                               for item in (state.get("candidates") or [])[:1]
                               if isinstance(item, dict)],
            }
        if workflow == "agent_fact_check":
            facts = state.get("facts")
            if isinstance(facts, dict) and (
                facts.get("type") in {"image", "multimodal"}
                or "image" in facts or "images" in facts
            ):
                return facts
        return state

    def _evaluate(self, state: Any, questions: dict[str, Any]) -> dict[str, Any]:
        if len(questions) > self.max_questions:
            raise DecisionEvaluatorError(
                f"Laya supports at most {self.max_questions} questions per request"
            )
        for question in questions.values():
            if not isinstance(question, dict):
                continue
            criteria = question.get("criteria")
            if question.get("type") in {"choice", "score"} and isinstance(criteria, (dict, list)):
                if len(criteria) > 20:
                    raise DecisionEvaluatorError(
                        "Laya's default head is unreliable above 20 options in one question"
                    )
        workflow = state.get("workflow") if isinstance(state, dict) else ""
        if workflow == "candidate_evaluation":
            concise = {
                "route_": "From supplied facts only: PRIORITIZE if worth research now; NEEDS_DATA if a key fact is missing; DEFER if low current value; REJECT only if this exact market is proven unusable. A past HOLD is not a ban; revisit only after meaningful change.",
                "quality_": "Rate current research value, not trade profitability.",
                "series_": "Is this one window in a recurring market series?",
                "evidence_": "Are the supplied facts enough to prioritize without another read?",
            }
            questions = {
                name: {
                    **question,
                    "instructions": next(
                        (text for prefix, text in concise.items() if name.startswith(prefix)),
                        question.get("instructions", ""),
                    ),
                } if isinstance(question, dict) else question
                for name, question in questions.items()
            }
        deadline = None
        if isinstance(state, dict):
            deadline = state.get("_scan_deadline_monotonic")
            if deadline is None and isinstance(state.get("run_state"), dict):
                deadline = state["run_state"].get("_scan_deadline_monotonic")
        # Service-owned benchmarks return HTTP 429; no local benchmark joins this queue.
        with self._exclusive(deadline=float(deadline) if deadline is not None else None):
            if deadline is not None and time.monotonic() >= float(deadline):
                raise DecisionEvaluatorBudgetExhausted("scan ended before Laya request started")
            compact = self._compact_state(state)
            if isinstance(compact, dict) and deadline is not None:
                # Preserve the caller's budget for the HTTP transport, but do not send it to Laya.
                if isinstance(compact.get("run_state"), dict):
                    compact["run_state"] = {**compact["run_state"],
                                            "_scan_deadline_monotonic": deadline}
                else:
                    compact["_scan_deadline_monotonic"] = deadline
            try:
                return super()._evaluate(compact, questions)
            except DecisionEvaluatorError as error:
                if "timed out" in str(error).lower() or "timeout" in str(error).lower():
                    if deadline is not None and time.monotonic() >= float(deadline):
                        raise DecisionEvaluatorBudgetExhausted(
                            "scan ended during Laya request"
                        ) from error
                    raise DecisionEvaluatorError(
                        "Laya GPU request timed out; result discarded and the next enabled evaluator may be tried"
                    ) from error
                raise


def _health_url(endpoint: str) -> str:
    parsed = urllib.parse.urlsplit(endpoint.strip().rstrip("/"))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Laya 服务地址须为有效的 HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Laya 服务地址不能包含账号、查询参数或片段")
    if parsed.path not in {"", "/v1"}:
        raise ValueError("Laya 服务地址须指向服务根路径或 /v1")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "/health", "", ""))


def _check_service(endpoint: str, proxy: str) -> dict[str, Any]:
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
    state = ((health.get("surface") or {}).get("takes") or {}).get("state") or {}
    types = questions.get("types") or {}
    max_questions = int(questions.get("max") or 0)
    if not {"choice", "score", "noul"}.issubset(types) or max_questions < 4:
        raise ValueError("Laya 服务未声明粗筛所需的结构化问答协议")
    state_kinds = tuple(str(kind) for kind in (state.get("kinds") or ("text", "json")))
    if set(state_kinds).intersection({"image", "multimodal"}):
        raise ValueError("Laya service must be text-only")
    return {"max_questions": max_questions, "state_kinds": state_kinds}


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
                "从队列开始执行后的 HTTP/GPU 超时；默认留足慢 GPU 的计算时间。超时结果不会用于决策，可回退到下一个已启用评估器。",
                required=True, default=120,
            ),
            PluginConfigField(
                "LAYA_QUEUE_WAIT_SECONDS", "排队最长等待秒数", "integer",
                "Laya 插件内部一次只执行一个请求；超过等待时间会明确失败，不会悄悄丢弃候选。扫描总预算仍优先。",
                required=True, default=120,
            ),
        ),
        load_callback=load, save_callback=save, delete_callback=delete, storage=storage,
    )

    def proxy_settings(values: dict[str, Any]) -> dict[str, str]:
        return context.proxy_settings(
            str(values.get("LAYA_HTTP_PROXY", "DIRECT")), field_name="LAYA_HTTP_PROXY"
        )

    instances: list[LayaDecisionEvaluator] = []

    def factory(_config: Any) -> LayaDecisionEvaluator:
        values = configuration.load()
        endpoint = str(values["LAYA_BASE_URL"]).strip()
        timeout = int(values["LAYA_TIMEOUT_SECONDS"])
        queue_wait = int(values["LAYA_QUEUE_WAIT_SECONDS"])
        proxy = proxy_settings(values)["proxy"]
        capabilities = _check_service(endpoint, proxy)
        evaluator = LayaDecisionEvaluator(
            endpoint, proxy, timeout, queue_wait_seconds=queue_wait,
            max_questions=capabilities["max_questions"],
            state_kinds=capabilities["state_kinds"],
        )
        instances.append(evaluator)
        return evaluator

    def readiness() -> PluginReadiness:
        try:
            values = configuration.load()
            _check_service(str(values["LAYA_BASE_URL"]).strip(), proxy_settings(values)["proxy"])
            return PluginReadiness(True)
        except (KeyError, TypeError, ValueError) as error:
            return PluginReadiness(False, (str(error),))

    def teardown() -> None:
        for evaluator in instances:
            evaluator.close()
        instances.clear()

    return PluginSpec(
        "decision_evaluator", "laya",
        "本地 WebGPU 粗筛评估器，也可作为 Agent 事实分类工具：按 state/questions → answers 协议评估候选与续扫，不代替交易决策；服务不就绪时不启用。",
        str(context.module_path), factory, configuration, teardown,
        readiness_callback=readiness,
        network_routes_callback=lambda: configured_proxy_route(
            load, "LAYA_HTTP_PROXY", default="DIRECT",
            resolver=lambda _value: proxy_settings(configuration.load()),
        ),
    )
