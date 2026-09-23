from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from prediction_market_agent.agent.decision_evaluator import (
    CandidateAssessment,
    ContinuationAssessment,
    DecisionEvaluatorBudgetExhausted,
    DecisionEvaluatorError,
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


OPENROUTER_DECISIONS_BASE_URL = "https://openrouter.ai/api/alpha"
OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODELS_URL = (
    "https://openrouter.ai/api/v1/models?supported_parameters=structured_outputs"
)
NATIVE_JEV_MODELS = ("~typesafe/jev-latest", "typesafe/jev-1.13")


def _decisions_endpoint(base_url: str) -> str:
    value = str(base_url).strip().rstrip("/")
    if not value:
        raise ValueError("自定义 evaluator Base URL 不能为空")
    return value if value.endswith("/decisions") else value + "/decisions"


def _chat_completions_endpoint(base_url: str) -> str:
    value = str(base_url).strip().rstrip("/")
    if not value:
        raise ValueError("自定义模型 Base URL 不能为空")
    return value if value.endswith("/chat/completions") else value + "/chat/completions"


def _answers_schema(questions: dict[str, Any]) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    for name, question in questions.items():
        question_type = str(question.get("type", ""))
        common = {
            "type": {"type": "string", "enum": [question_type]},
            "confidence": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
        }
        if question_type == "choice":
            choices = list((question.get("criteria") or {}).keys())
            properties[name] = {
                "type": "object",
                "properties": {
                    **common,
                    "choice": {"type": "string", "enum": choices},
                    "probabilities": {
                        "type": "object",
                        "additionalProperties": {"type": "number", "minimum": 0, "maximum": 1},
                    },
                },
                "required": ["type", "choice", "confidence", "probabilities"],
                "additionalProperties": False,
            }
        elif question_type == "score":
            maximum = max(0, len(question.get("criteria") or []) - 1)
            properties[name] = {
                "type": "object",
                "properties": {**common, "score": {"type": "number", "minimum": 0, "maximum": maximum}},
                "required": ["type", "confidence", "score"],
                "additionalProperties": False,
            }
        elif question_type == "noul":
            properties[name] = {
                "type": "object",
                "properties": {**common, "noul": {"type": "number", "minimum": 0, "maximum": 1}},
                "required": ["type", "confidence", "noul"],
                "additionalProperties": False,
            }
        else:
            raise DecisionEvaluatorError(f"unsupported typed question: {question_type}")
    return {
        "type": "object",
        "properties": {
            "answers": {
                "type": "object",
                "properties": properties,
                "required": list(properties),
                "additionalProperties": False,
            }
        },
        "required": ["answers"],
        "additionalProperties": False,
    }


def _validate_answers(questions: dict[str, Any], answers: Any) -> dict[str, Any]:
    if not isinstance(answers, dict):
        raise DecisionEvaluatorError("typed evaluator response has no answers object")
    for name, question in questions.items():
        answer = answers.get(name)
        if not isinstance(answer, dict):
            raise DecisionEvaluatorError(f"typed evaluator response is missing answer {name}")
        question_type = str(question.get("type", ""))
        if question_type == "choice":
            choice = answer.get("choice")
            if not isinstance(choice, str) or choice not in (question.get("criteria") or {}):
                raise DecisionEvaluatorError(f"typed evaluator returned an invalid choice for {name}")
        elif question_type == "score":
            if not isinstance(answer.get("score"), (int, float)):
                raise DecisionEvaluatorError(f"typed evaluator returned a non-numeric score for {name}")
        elif question_type == "noul":
            if not isinstance(answer.get("noul"), (int, float)):
                raise DecisionEvaluatorError(f"typed evaluator returned a non-numeric noul for {name}")
        else:
            raise DecisionEvaluatorError(f"unsupported typed question: {question_type}")
    return answers


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _opener(proxy: str) -> urllib.request.OpenerDirector:
    handler = (
        urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        if proxy
        else urllib.request.ProxyHandler({})
    )
    return urllib.request.build_opener(handler, _NoRedirect())


def _probabilities(answer: dict[str, Any]) -> dict[str, float]:
    raw = answer.get("probabilities") or {}
    if not isinstance(raw, dict):
        return {}
    return {str(key): float(value) for key, value in raw.items()}


def openrouter_evaluator_model_choices(
    values: dict[str, Any], *, models_url: str = OPENROUTER_MODELS_URL,
    proxy_settings: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    """List native Jev plus chat models explicitly advertising schema output."""
    choices = {
        model: {"value": model, "label": model + " · native Decisions"}
        for model in NATIVE_JEV_MODELS
    }
    parsed = urllib.parse.urlsplit(models_url)
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    ):
        raise ValueError("OpenRouter 模型接口须使用 HTTPS")
    headers = {"User-Agent": "prediction-market-agent/3"}
    key = str(values.get("OPENROUTER_API_KEY", "")).strip()
    if key:
        headers["Authorization"] = "Bearer " + key
    try:
        with _opener((proxy_settings or {}).get("proxy", "")).open(
            urllib.request.Request(models_url, headers=headers), timeout=15
        ) as response:
            items = json.loads(response.read(8 * 1024 * 1024))["data"]
        if not isinstance(items, list):
            raise ValueError("invalid model list")
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                continue
            parameters = item.get("supported_parameters")
            if not isinstance(parameters, list) or "structured_outputs" not in parameters:
                continue
            model = item["id"]
            choices[model] = {
                "value": model,
                "label": str(item.get("name") or model) + " · schema constrained",
            }
    except (urllib.error.HTTPError, OSError, ValueError, KeyError, TypeError):
        # Native Jev support is known independently of the chat-model directory.  Other models
        # stay hidden until OpenRouter explicitly advertises their schema capability again.
        pass
    return [choices[key] for key in sorted(choices)]


class SchemaDecisionEvaluator:
    """Direct typed JSON adapter; it never delegates evaluation to an Agent."""

    name = "jev"

    def __init__(
        self,
        api_key: str,
        model: str,
        proxy: str,
        timeout: int,
        batch_size: int,
        endpoint: str = _decisions_endpoint(OPENROUTER_DECISIONS_BASE_URL),
        connection_name: str = "openrouter",
        protocol: str = "decisions",
        require_parameters: bool = False,
        allow_http: bool = False,
    ) -> None:
        if not model:
            raise ValueError("请选择或填写 evaluator 模型")
        if not 1 <= timeout <= 300:
            raise ValueError("JEV_TIMEOUT_SECONDS must be in [1, 300]")
        if not 1 <= batch_size <= 100:
            raise ValueError("JEV_BATCH_SIZE must be in [1, 100]")
        parsed = urllib.parse.urlsplit(endpoint)
        if parsed.scheme != "https" and not (allow_http and parsed.scheme == "http") and not (
            parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        ):
            raise ValueError("evaluator endpoint must use HTTPS")
        self.api_key = api_key
        self.model = model
        self.proxy = proxy
        self.timeout = timeout
        self.batch_size = batch_size
        self.endpoint = endpoint
        self.connection_name = connection_name
        self.protocol = protocol
        self.require_parameters = require_parameters

    def _evaluate(self, state: Any, questions: dict[str, Any]) -> dict[str, Any]:
        deadline = None
        if isinstance(state, dict) and "_scan_deadline_monotonic" in state:
            deadline = float(state["_scan_deadline_monotonic"])
            state = {key: value for key, value in state.items() if key != "_scan_deadline_monotonic"}
        elif isinstance(state, dict) and isinstance(state.get("run_state"), dict) and (
            "_scan_deadline_monotonic" in state["run_state"]
        ):
            deadline = float(state["run_state"]["_scan_deadline_monotonic"])
            state = {
                **state,
                "run_state": {
                    key: value for key, value in state["run_state"].items()
                    if key != "_scan_deadline_monotonic"
                },
            }
        request_payload = {"state": state, "model": self.model, "questions": questions}
        active_protocol = self.protocol

        def chat_payload(protocol: str) -> dict[str, Any]:
            schema = _answers_schema(questions)
            request_payload = {
                "model": self.model,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Evaluate only the supplied JSON state and typed questions. Return one "
                            "answer per question matching the response schema. Do not add prose."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {"state": state, "questions": questions}, ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    },
                ],
            }
            if protocol in {"chat_json", "chat_auto"}:
                request_payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "typed_decision_answers",
                        "strict": True,
                        "schema": schema,
                    },
                }
            else:
                request_payload["tools"] = [
                    {
                        "type": "function",
                        "function": {
                            "name": "submit_typed_answers",
                            "description": "Return the complete typed evaluator result.",
                            "parameters": schema,
                            "strict": True,
                        },
                    }
                ]
                request_payload["tool_choice"] = {
                    "type": "function",
                    "function": {"name": "submit_typed_answers"},
                }
            if self.require_parameters:
                request_payload["provider"] = {"require_parameters": True}
            return request_payload

        if self.protocol.startswith("chat_"):
            request_payload = chat_payload(active_protocol)

        def use_compatibility_fallback() -> bool:
            nonlocal active_protocol, request_payload
            if self.protocol != "chat_auto" or active_protocol != "chat_auto":
                return False
            active_protocol = "chat_tool"
            request_payload = chat_payload(active_protocol)
            return True

        last_error = ""
        for attempt in range(3):
            remaining = deadline - time.monotonic() if deadline is not None else None
            if remaining is not None and remaining <= 0:
                raise DecisionEvaluatorBudgetExhausted("scan screening time budget exhausted")
            timeout = min(self.timeout, max(0.1, remaining)) if remaining is not None else self.timeout
            request = urllib.request.Request(
                self.endpoint,
                data=json.dumps(
                    request_payload, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "prediction-market-agent/3",
                    **(
                        {"Authorization": "Bearer " + self.api_key}
                        if self.api_key
                        else {}
                    ),
                },
                method="POST",
            )
            try:
                with _opener(self.proxy).open(request, timeout=timeout) as response:
                    payload = json.loads(response.read(8 * 1024 * 1024))
                if active_protocol.startswith("chat_"):
                    message = payload["choices"][0]["message"]
                    if active_protocol == "chat_tool":
                        calls = message.get("tool_calls") or []
                        call = next(
                            (
                                item
                                for item in calls
                                if (item.get("function") or {}).get("name")
                                == "submit_typed_answers"
                            ),
                            None,
                        )
                        if not isinstance(call, dict):
                            raise DecisionEvaluatorError(
                                "custom chat model did not call submit_typed_answers"
                            )
                        arguments = (call.get("function") or {}).get("arguments")
                        payload = (
                            json.loads(arguments)
                            if isinstance(arguments, str)
                            else arguments
                        )
                    else:
                        content = message["content"]
                        if not isinstance(content, str):
                            raise DecisionEvaluatorError(
                                "custom chat model returned non-text JSON content"
                            )
                        payload = json.loads(content)
                    if not isinstance(payload, dict):
                        raise DecisionEvaluatorError(
                            "custom chat model returned a non-object typed result"
                        )
                answers = _validate_answers(questions, payload.get("answers"))
                return {"answers": answers, "model": str(payload.get("model", self.model)),
                        "usage": payload.get("usage") or {}}
            except urllib.error.HTTPError as error:
                last_error = f"HTTP {error.code}"
                if error.code in {400, 404, 422} and use_compatibility_fallback():
                    continue
                if error.code not in {429, 529} or attempt == 2:
                    raise DecisionEvaluatorError(
                        f"typed evaluator failed: {last_error}"
                    ) from None
            except (DecisionEvaluatorError, ValueError, TypeError, KeyError, IndexError,
                    AttributeError) as error:
                # Some OpenAI-compatible servers accept response_format but silently ignore it.
                # Treat only a verified, schema-valid response as native support; otherwise retry
                # through the same private adapter with a forced function whose arguments carry
                # the exact answers schema.
                if use_compatibility_fallback():
                    continue
                raise DecisionEvaluatorError(f"typed evaluator failed: {error}") from error
            except OSError as error:
                raise DecisionEvaluatorError(f"typed evaluator failed: {error}") from error
            time.sleep(0.25 * (2 ** attempt))
        raise DecisionEvaluatorError(f"typed evaluator failed: {last_error}")

    def answer_questions(self, state: dict[str, Any], questions: dict[str, Any]) -> dict[str, Any]:
        """Answer a bounded Agent-supplied fact check using the same typed protocol."""
        return self._evaluate({"workflow": "agent_fact_check", "facts": state}, questions)

    def evaluate_candidates(
        self, state: dict[str, Any], candidates: list[dict[str, Any]]
    ) -> list[CandidateAssessment]:
        results: list[CandidateAssessment] = []
        for start in range(0, len(candidates), self.batch_size):
            batch = candidates[start:start + self.batch_size]
            questions: dict[str, Any] = {}
            indexed: list[dict[str, Any]] = []
            for index, candidate in enumerate(batch):
                candidate_id = str(candidate.get("candidate_id") or candidate.get("topic_id") or "")
                if not candidate_id:
                    continue
                key = f"c{index}"
                indexed.append({"key": key, **candidate})
                questions[f"route_{key}"] = {
                    "type": "choice",
                    "instructions": (
                        f"Choose the next treatment for candidate {key} from only the supplied facts. "
                        "Missing facts require NEEDS_DATA; low current value is DEFER; use REJECT only "
                        "when the supplied facts establish that it is unusable. When the candidate "
                        "carries `history`, it has been here before: `screened` is what you called "
                        "it, `decided` is what the deciding model concluded each time, and "
                        "`revisit_when` is the condition it named for looking again. A market "
                        "already decided and left alone is worth another expensive look only when "
                        "something it was waiting for has plausibly happened - a price or deadline "
                        "the trigger names, a change in the supplied figures. Otherwise DEFER: "
                        "handing it on again spends the round on an answer that is already known. "
                        "`screening_calibration` in the state says what your own verdicts here have "
                        "led to so far; if what you call PRIORITIZE is almost always held, ask less "
                        "for that kind and more for what actually got traded."
                    ),
                    "criteria": {
                        "PRIORITIZE": "Worth scarce research attention now.",
                        "NEEDS_DATA": "Promising enough to fetch specific missing information.",
                        "DEFER": "Keep for later rather than spend research resources now.",
                        "REJECT": "Supplied facts establish that this candidate is unusable.",
                    },
                }
                questions[f"quality_{key}"] = {
                    "type": "score",
                    "instructions": f"Rate candidate {key}'s current research value.",
                    "criteria": [
                        "No usable opportunity in supplied facts",
                        "Weak or mostly missing",
                        "Plausible but unverified",
                        "Strong candidate for research",
                        "Highest-value candidate in this batch",
                    ],
                }
                questions[f"series_{key}"] = {
                    "type": "noul",
                    "instructions": (
                        f"Is candidate {key} one of a series that is relisted on a schedule - the "
                        "same question for the next fifteen minutes, the next day, the next round "
                        "of a tournament - rather than a one-off event?"
                    ),
                    "criteria": {
                        "true": "One window of a recurring series; more of them will be listed.",
                        "false": "A one-off question that will not be relisted when it settles.",
                    },
                }
                questions[f"evidence_{key}"] = {
                    "type": "noul",
                    "instructions": (
                        f"Does candidate {key} already contain enough supplied evidence to be "
                        "prioritized without another data read?"
                    ),
                    "criteria": {
                        "true": "The supplied facts are sufficient for immediate prioritization.",
                        "false": "A specific missing fact should be fetched first.",
                    },
                }
            if not indexed:
                continue
            response = self._evaluate(
                {"workflow": "candidate_evaluation", "run_state": state, "candidates": indexed},
                questions,
            )
            answers = response["answers"]
            for item in indexed:
                route = answers.get(f"route_{item['key']}") or {}
                quality = answers.get(f"quality_{item['key']}") or {}
                evidence = answers.get(f"evidence_{item['key']}") or {}
                series = answers.get(f"series_{item['key']}") or {}
                action = str(route.get("choice", "DEFER")).upper()
                evidence_sufficient = max(0.0, min(1.0, float(evidence.get("noul", 0.0))))
                if action == "PRIORITIZE" and evidence_sufficient < 0.5:
                    action = "NEEDS_DATA"
                score = max(0.0, min(4.0, float(quality.get("score", 0.0)))) / 4.0
                results.append(
                    CandidateAssessment(
                        candidate_id=str(item.get("candidate_id") or item.get("topic_id")),
                        action=action,
                        quality=score,
                        confidence=(float(route["confidence"]) if route.get("confidence") is not None else None),
                        recurring=(max(0.0, min(1.0, float(series["noul"])))
                                   if series.get("noul") is not None else None),
                        probabilities=_probabilities(route),
                        provider=f"{self.connection_name}:{response['model']}",
                    )
                )
        return results

    def classify_failure(self, message: str) -> str:
        """Which kind of failure a client message describes, from the message alone."""
        response = self._evaluate(
            {"workflow": "failure_classification", "client_message": str(message)[:1200]},
            {
                "kind": {
                    "type": "choice",
                    "instructions": (
                        "A model client failed and printed this. Name what kind of failure it is "
                        "from the words themselves; do not guess at causes the message does not "
                        "state."
                    ),
                    "criteria": {
                        "rate_limit": "An account limit or quota is spent, or the service is "
                                      "asking to slow down; it will work again later.",
                        "auth": "Not signed in, a rejected or expired credential, or no "
                                "permission for this account.",
                        "contract": "The request itself was not acceptable - an unsupported "
                                    "parameter, a model or endpoint that cannot serve it, a reply "
                                    "that did not match what was asked for.",
                        "transient": "A timeout, a dropped connection, an overloaded server or "
                                     "another fault that may simply pass.",
                    },
                }
            },
        )
        answer = (response["answers"].get("kind") or {})
        return str(answer.get("choice") or "").strip().lower()

    def screen_decision(
        self, state: dict[str, Any], candidates: list[dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        """Answer, per candidate, whether anything has changed since it was last settled."""
        verdicts: dict[str, dict[str, Any]] = {}
        for start in range(0, len(candidates), self.batch_size):
            batch = candidates[start:start + self.batch_size]
            questions: dict[str, Any] = {}
            indexed: list[dict[str, Any]] = []
            for index, candidate in enumerate(batch):
                key = f"d{index}"
                indexed.append({"key": key, **candidate})
                questions[f"worth_{key}"] = {
                    "type": "choice",
                    "instructions": (
                        f"Candidate {key} was decided before. `previous_verdict` carries what was "
                        "concluded, and `revisit_when` the condition that round named for looking "
                        "again; the rest of the entry is how the market stands now. Compare the "
                        "two. Do not reason about whether the trade is good - that is the next "
                        "model's work and it costs real money to run."
                    ),
                    "criteria": {
                        "EXAMINE": "Something the last round was waiting for has plausibly "
                                   "happened, or the figures have moved enough that the old "
                                   "conclusion may no longer hold.",
                        "SKIP": "Nothing named in the trigger has happened and the figures are "
                                "materially where they were; the previous conclusion still stands.",
                    },
                }
            if not indexed:
                continue
            response = self._evaluate(
                {"workflow": "decision_worth_screening", "run_state": state,
                 "candidates": indexed},
                questions,
            )
            answers = response["answers"]
            for item in indexed:
                answer = answers.get(f"worth_{item['key']}") or {}
                choice = str(answer.get("choice", "EXAMINE")).upper()
                verdicts[str(item.get("candidate_id"))] = {
                    "examine": choice != "SKIP",
                    "confidence": (float(answer["confidence"])
                                   if answer.get("confidence") is not None else 0.0),
                    "why": str(answer.get("reason") or answer.get("why") or "")[:200],
                    "provider": f"{self.connection_name}:{response['model']}",
                }
        return verdicts

    def screen_outliers(
        self, state: dict[str, Any], assessments: list[dict[str, Any]]
    ) -> dict[str, bool]:
        """Read this round's own answers back and say which confidences stand apart from the rest.

        Only the ones that did not clear the absolute floor are worth asking about - anything above
        it is already trusted - but every answer in the round is supplied, because "stands apart"
        is a statement about the batch and cannot be made from one number.
        """
        under_review = [item for item in assessments if item.get("under_review")]
        if not under_review:
            return {}
        confidences = [item.get("confidence") for item in assessments]
        verdicts: dict[str, bool] = {}
        for start in range(0, len(under_review), self.batch_size):
            batch = under_review[start:start + self.batch_size]
            questions: dict[str, Any] = {}
            indexed: list[dict[str, Any]] = []
            for index, item in enumerate(batch):
                key = f"o{index}"
                indexed.append({"key": key, **item})
                questions[f"stands_apart_{key}"] = {
                    "type": "noul",
                    "instructions": (
                        f"Answer {key} scored {item.get('confidence')} while this round's answers "
                        "are listed in `all_confidences`. Judged against that distribution rather "
                        "than against any fixed number, is this one clearly separated above the "
                        "rest - so that its verdict should be acted on even though it is below the "
                        "absolute confidence floor?"
                    ),
                    "criteria": {
                        "true": "Clearly above the body of this round's answers, not part of it.",
                        "false": "Within the ordinary spread of this round, however that spread sits.",
                    },
                }
            response = self._evaluate(
                {
                    "workflow": "confidence_separation_review",
                    "run_state": state,
                    "all_confidences": confidences,
                    "under_review": indexed,
                },
                questions,
            )
            answers = response["answers"]
            for item in indexed:
                answer = answers.get(f"stands_apart_{item['key']}") or {}
                verdicts[str(item.get("candidate_id"))] = (
                    max(0.0, min(1.0, float(answer.get("noul", 0.0)))) >= 0.5
                )
        return verdicts

    def assess_continuation(
        self, state: dict[str, Any], frontier: list[dict[str, Any]], page: dict[str, Any]
    ) -> ContinuationAssessment:
        response = self._evaluate(
            {"workflow": "continuation", "run_state": state, "frontier": frontier, "last_page": page},
            {
                "next_action": {
                    "type": "choice",
                    "instructions": "Choose the next bounded discovery action from the observed state.",
                    "criteria": {
                        "CONTINUE_DISCOVERY": "Read another page because observed marginal value remains positive.",
                        "PROCESS_FRONTIER": "Stop scanning this batch and process queued candidates.",
                        "PAUSE_AND_RESUME": "Yield execution and preserve work for a later batch.",
                        "SOURCE_EXHAUSTED": "The data source explicitly reports no remaining page.",
                    },
                },
                "marginal_value": {
                    "type": "score",
                    "instructions": "Rate the observed value of one more discovery page.",
                    "criteria": ["No value", "Low value", "Uncertain", "Positive value", "High value"],
                },
            },
        )
        answer = response["answers"].get("next_action") or {}
        score = response["answers"].get("marginal_value") or {}
        return ContinuationAssessment(
            action=str(answer.get("choice", "PAUSE_AND_RESUME")).upper(),
            marginal_value=max(0.0, min(4.0, float(score.get("score", 0.0)))) / 4.0,
            confidence=(float(answer["confidence"]) if answer.get("confidence") is not None else None),
            probabilities=_probabilities(answer),
            provider=f"{self.connection_name}:{response['model']}",
        )

def initialize_plugin(context: PluginInitializationContext) -> PluginSpec:
    load, save, delete, storage = json_file_callbacks(
        context.working_directory / "config" / "plugins" / "jev.json"
    )
    configuration = PluginConfiguration(
        fields=(
            PluginConfigField(
                "JEV_CONNECTION", "连接方式", "enum",
                "OpenRouter 自动使用官方 Decisions Base URL 并默认复用 OpenRouter Key；自定义方式使用下方兼容服务参数。",
                required=True, default="OPENROUTER", options=("OPENROUTER", "CUSTOM"),
            ),
            PluginConfigField(
                "JEV_API_KEY", "独立 / 自定义 API Key（可选）", "secret",
                "OpenRouter 方式留空会复用下方 Provider 的 Key；填写后单独覆盖。自定义服务无需鉴权时可留空。",
            ),
            PluginConfigField(
                "JEV_SHARED_PROVIDER", "共享 Key 的 OpenRouter 配置", "string",
                "Jev 独立 Key 留空时，从该 Provider 的 config/plugins/<名称>.json 复用 OPENROUTER_API_KEY。",
                required=True, default="openrouter",
            ),
            PluginConfigField(
                "JEV_MODEL", "OpenRouter evaluator 模型", "string",
                "Jev 型号走原生 Decisions；其它选项必须由 OpenRouter 明确声明支持 structured_outputs，并由插件强制 answers schema。",
                required=True, default="~typesafe/jev-latest",
                selection_only=True,
            ),
            PluginConfigField(
                "JEV_CUSTOM_BASE_URL", "自定义 Base URL", "string",
                "仅自定义方式使用；填写兼容 OpenAI Chat Completions 的 Base URL。系统先使用原生 strict JSON schema；端点拒绝或忽略时，才在本插件内改用强制函数参数承载同一 answers schema。",
                default="",
            ),
            PluginConfigField(
                "JEV_CUSTOM_MODEL", "自定义模型名", "string",
                "仅自定义方式使用；原样写入 Chat Completions 请求的 model 字段。",
                default="",
            ),
            PluginConfigField(
                "JEV_HTTP_PROXY", "HTTP 代理", "string",
                "只控制 Jev。INHERIT 继承统一代理；也可单独填 DIRECT、HOST、ENVIRONMENT、SYSTEM 或 http(s) URL。不会读取或覆盖 OpenRouter Provider、平台或其它插件的代理。",
                required=True, default="INHERIT",
            ),
            PluginConfigField(
                "JEV_TIMEOUT_SECONDS", "超时秒数", "integer",
                "单次 evaluator 请求的超时。", required=True, default=30,
            ),
            PluginConfigField(
                "JEV_BATCH_SIZE", "候选批大小", "integer",
                "单次请求携带的候选数；这是传输边界，不是全流程候选上限。",
                required=True, default=32,
            ),
        ),
        load_callback=load, save_callback=save, delete_callback=delete, storage=storage,
        choices_callback=lambda field: openrouter_evaluator_model_choices(
            {
                "OPENROUTER_API_KEY": str(configuration.load().get("JEV_API_KEY", "")).strip()
                or str(shared_values(configuration.load()).get("OPENROUTER_API_KEY", "")).strip()
            },
            proxy_settings=proxy_settings(configuration.load()),
        ),
        choice_fields=("JEV_MODEL",),
    )

    def shared_values(values: dict[str, Any]) -> dict[str, Any]:
        name = str(values.get("JEV_SHARED_PROVIDER", "openrouter")).strip().lower()
        if not re.fullmatch(r"openrouter(?:_[a-z0-9_]+)?", name):
            raise ValueError("共享 OpenRouter 配置名必须为 openrouter 或 openrouter_* 格式")
        shared_load, _save, _delete, _storage = json_file_callbacks(
            context.working_directory / "config" / "plugins" / f"{name}.json"
        )
        return shared_load()

    def proxy_settings(values: dict[str, Any]) -> dict[str, str]:
        selector = str(values.get("JEV_HTTP_PROXY", "INHERIT")).strip()
        return context.proxy_settings(selector, field_name="JEV_HTTP_PROXY")

    def factory(config):
        del config
        values = configuration.load()
        connection = str(values.get("JEV_CONNECTION", "OPENROUTER")).strip().upper()
        if connection == "OPENROUTER":
            shared = shared_values(values)
            api_key = str(values.get("JEV_API_KEY", "")).strip() or str(
                shared.get("OPENROUTER_API_KEY", "")
            ).strip()
            if not api_key:
                raise ValueError("OpenRouter 方式需要共享 OpenRouter API Key 或 Jev 独立 Key")
            endpoint = _decisions_endpoint(OPENROUTER_DECISIONS_BASE_URL)
            model = str(values["JEV_MODEL"]).strip()
            connection_name = "openrouter"
            if model in NATIVE_JEV_MODELS:
                protocol = "decisions"
                require_parameters = False
            else:
                supported = {
                    item["value"] for item in openrouter_evaluator_model_choices(
                        {"OPENROUTER_API_KEY": api_key},
                        proxy_settings=proxy_settings(values),
                    )
                }
                if model not in supported:
                    raise ValueError(
                        "所选 OpenRouter evaluator 模型未明确声明 structured_outputs 支持"
                    )
                endpoint = OPENROUTER_CHAT_URL
                protocol = "chat_json"
                require_parameters = True
        elif connection == "CUSTOM":
            api_key = str(values.get("JEV_API_KEY", "")).strip()
            endpoint = _chat_completions_endpoint(str(values.get("JEV_CUSTOM_BASE_URL", "")))
            model = str(values.get("JEV_CUSTOM_MODEL", "")).strip()
            if not model:
                raise ValueError("自定义 evaluator 连接需要填写模型名")
            connection_name = "custom"
            protocol = "chat_auto"
            require_parameters = False
        else:
            raise ValueError("JEV_CONNECTION must be OPENROUTER or CUSTOM")
        return SchemaDecisionEvaluator(
            api_key,
            model,
            proxy_settings(values)["proxy"],
            int(values["JEV_TIMEOUT_SECONDS"]),
            int(values["JEV_BATCH_SIZE"]),
            endpoint=endpoint,
            connection_name=connection_name,
            protocol=protocol,
            require_parameters=require_parameters,
        )

    def readiness() -> PluginReadiness:
        try:
            factory(None)
            return PluginReadiness(True)
        except (KeyError, TypeError, ValueError) as error:
            return PluginReadiness(False, (str(error),))

    return PluginSpec(
        "decision_evaluator", "jev",
        "发现阶段粗筛，也可作为 Agent 按需调用的事实分类工具：默认以 Jev 原生 Decisions 工作，或把支持 structured output 的 OpenRouter/自定义聊天模型约束为同一 state/questions → answers 协议；不代替交易决策。",
        str(context.module_path), factory, configuration, lambda: None,
        readiness_callback=readiness,
        network_routes_callback=lambda: configured_proxy_route(
            load, "JEV_HTTP_PROXY", default="INHERIT",
            resolver=lambda _value: proxy_settings(configuration.load()),
        ),
    )
