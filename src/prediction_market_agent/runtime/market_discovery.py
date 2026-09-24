from __future__ import annotations

import logging
import math
import time
from typing import Any, Callable

from ..agent.decision import DecisionCancelled, DecisionProviderError
from ..agent.decision_evaluator import (
    DEFAULT_SCREENING_CONFIDENCE,
    MIN_SCREENING_CONFIDENCE,
    is_high_confidence,
    screening_feedback,
)
from ..agent.evolution import (
    DISCOVERY_EVOLUTION_KEY,
    LESSON_DEVIATION,
    LESSON_MIN_SAMPLE,
    prompt_json_payload,
    render_overlay_block,
    shrunk_weight,
)
from ..agent.market_discovery import (
    DiscoveryBudget,
    DiscoveryLesson,
    DiscoveryPrior,
    compose_discovery_payload,
)
from ..agent.market_playbook import read_market_playbook
from ..plugin_system.contracts import PredictionMarketApiPlugin, Topic
from .memory import SessionMemory

LOGGER = logging.getLogger(__name__)

DISCOVERY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "selections": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "topic_id": {"type": "string", "minLength": 1},
                    "reason": {"type": "string", "minLength": 1, "maxLength": 400},
                    "priors": {
                        "type": "array",
                        "maxItems": 4,
                        "items": {"type": "string", "maxLength": 80},
                    },
                },
                "required": ["topic_id", "reason", "priors"],
            },
        },
        "skipped_reason": {"type": "string", "maxLength": 400},
        "next_scan_seconds": {"type": "integer", "minimum": 0, "maximum": 86400},
        "next_survey_queries": {
            "type": "array",
            "maxItems": 8,
            "items": {"type": "string", "minLength": 1, "maxLength": 120},
        },
        "pacing_reason": {"type": "string", "maxLength": 300},
        "headline": {"type": "string", "minLength": 1, "maxLength": 90},
    },
    "required": [
        "selections", "skipped_reason", "next_scan_seconds", "next_survey_queries",
        "pacing_reason", "headline",
    ],
}

DISCOVERY_MISSION = (
    "Select the markets on this platform that deserve this cycle's decision slots. Return topic_id "
    "values taken verbatim from the candidate list. Returning fewer than the maximum, or none, is "
    "correct when nothing clears the gates. The headline is the one line a trader reads first: what "
    "this round found, or why it found nothing - for example \"Picked 2 of 24: NATO clash volume up "
    "67%\" or \"Nothing picked: every live book is wider than the edge\"."
)

DISCOVERY_CONTROL_MISSION = (
    "Choose one next action to sharpen the shortlist. Use DECIDE once the ordering is clear."
)

CONTINUATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "action": {
            "type": "string",
            "enum": [
                "CONTINUE_DISCOVERY", "PROCESS_FRONTIER", "PAUSE_AND_RESUME", "SOURCE_EXHAUSTED"
            ],
        },
        "reason": {"type": "string", "minLength": 1, "maxLength": 400},
    },
    "required": ["action", "reason"],
}

REVIEW_SETTLE_MS = 30 * 60 * 1000
"""How long after a selection its downstream decisions are considered final."""


def _provider_incident_details(error: Exception) -> dict[str, Any]:
    raw = str(getattr(error, "raw_output", "") or "")
    return {
        "error_type": type(error).__name__,
        "raw_output": raw[-20_000:],
        "raw_output_truncated": len(raw) > 20_000,
    }



def _band(value: float, edges: tuple[float, ...]) -> str:
    low = 0.0
    for edge in edges:
        if value < edge:
            return f"{low:g}-{edge:g}"
        low = edge
    return f"{low:g}+"


def topic_features(
    topic: Topic,
    *,
    previous: dict[str, Any] | None,
    attention: dict[str, Any] | None,
    now_ms: int,
    verdict: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Cheap, list-payload-only features. Buckets are what outcome attribution aggregates on."""
    buckets: list[str] = [
        f"liquidity:{_band(float(topic.liquidity_usdt or 0.0), (1_000, 10_000, 100_000))}",
        f"volume:{_band(float(topic.volume_usdt or 0.0), (10_000, 100_000, 1_000_000))}",
        f"status:{str(topic.status).upper() or 'UNKNOWN'}",
    ]
    category = str(topic.category or "").strip().lower()[:40]
    if category:
        buckets.append(f"category:{category}")

    features: dict[str, Any] = {"liquidity_usdt": float(topic.liquidity_usdt or 0.0)}
    if previous is None:
        buckets.append("listing:new")
        features["first_seen"] = True
    else:
        buckets.append("listing:known")
        previous_volume = float(previous.get("volume_usdt") or 0.0)
        volume_change = float(topic.volume_usdt or 0.0) - previous_volume
        features["volume_change"] = volume_change
        features["seconds_since_observed"] = max(
            0, (now_ms - int(previous.get("observed_at", now_ms))) // 1000
        )
        moved = previous_volume > 0 and abs(volume_change) / previous_volume > 0.01
        buckets.append("flow:moving" if moved else "flow:quiet")

    if attention:
        last = int(attention.get("last_selected_at", 0))
        features["seconds_since_selected"] = max(0, (now_ms - last) // 1000)
        features["previous_selections"] = int(attention.get("selections", 0))
    else:
        features["never_selected"] = True
        buckets.append("attention:never")

    # What was decided about this market before, so a round can tell "already looked at and found
    # fairly priced" from "nobody has looked". Most binary markets are priced about right and
    # answer HOLD; without this the same ones come back every round, take the same slots and get
    # the same answer, and the robot spends everything it has on questions already settled.
    if verdict:
        features["previous_verdict"] = {
            "action": verdict.get("last_action", ""),
            "said": verdict.get("last_headline", ""),
            "revisit_when": verdict.get("revisit_when", ""),
            "times_decided": int(verdict.get("decisions", 0)),
            "times_held": int(verdict.get("holds", 0)),
            "seconds_since": max(0, (now_ms - int(verdict.get("last_at", now_ms))) // 1000),
        }
        buckets.append(f"verdict:{str(verdict.get('last_action', '')).lower() or 'none'}")
        if int(verdict.get("holds", 0)) >= 2:
            buckets.append("verdict:held-repeatedly")
    elif attention:
        features["decided_before"] = False

    features["buckets"] = buckets
    return features


class DiscoveryEngine:
    """Framework-owned market discovery: the platform says when, this decides what."""

    def __init__(
        self,
        *,
        memory: SessionMemory,
        strategy: Any,
        provider: Any,
        evaluator: Any = None,
        max_scan_seconds: int = 45,
        max_scan_pages: int = 20,
        evolution_enabled: bool,
        cross_platform_search: Callable[[str, int], dict[str, Any]] | None = None,
        research_contributions: list[Any] | None = None,
        operator_instructions: Callable[[str], list[dict[str, Any]]] | None = None,
    ) -> None:
        self.memory = memory
        self.strategy = strategy
        self.provider = provider
        self.evaluator = evaluator
        self.max_scan_seconds = max(1, int(max_scan_seconds))
        self.max_scan_pages = max(1, int(max_scan_pages))
        self._scan_audit: list[dict[str, Any]] = []
        self._evaluator_assessments: dict[str, Any] = {}
        self._evaluator_candidate_errors: dict[str, str] = {}
        # Read once per round and handed to the screener with every candidate: what has already
        # been screened and decided here. Empty until a round loads it.
        self._screening_history: dict[str, dict[str, Any]] = {}
        self._screening_calibration: dict[str, Any] = {}
        self._screening_threshold = DEFAULT_SCREENING_CONFIDENCE
        self._selection_ids: dict[tuple[str, str], int] = {}
        self.evolution_enabled = evolution_enabled
        self.cross_platform_search = cross_platform_search
        # What the operator attached to their money, asked for per round: a slot spent on something
        # they ruled out is a slot spent against them.
        self.operator_instructions = operator_instructions
        # The gates ask about resolution wording and deadlines, and a candidate that does not carry
        # them was being rejected as unverifiable - while the tools that could have gone and found
        # them were offered only at the decision stage. Discovery could see that something was
        # missing and had no way to go and get it.
        self.research_contributions = list(research_contributions or [])

    # ------------------------------------------------------------------ survey

    operator_instructions: Any = None
    """Asked for the open instructions each round; the runtime sets it, tests may leave it out."""

    def _evaluation_candidate(self, topic: Topic) -> dict[str, Any]:
        candidate = {
            "candidate_id": topic.topic_id,
            "topic_id": topic.topic_id,
            "title": topic.title,
            "question": topic.question,
            "description": topic.description[:600],
            "category": topic.category,
            "status": topic.status,
            "liquidity_usdt": topic.liquidity_usdt,
            "volume_usdt": topic.volume_usdt,
        }
        # Whatever is already known about this market: how it has been screened before, and what
        # the deciding model concluded each time. Screening it without this is screening it as if
        # for the first time, every time - which is how the same markets kept being handed on and
        # kept coming back held.
        known = self._screening_history.get(str(topic.topic_id))
        if known:
            candidate["history"] = known
        return candidate

    def _llm_continuation(
        self, *, platform: str, scan_state: dict[str, Any], frontier: list[dict[str, Any]],
        last_page: dict[str, Any], evaluator_answer: Any,
    ) -> dict[str, Any] | None:
        continuation_input = {
            "platform": platform,
            "scan_state": scan_state,
            "frontier": frontier[:40],
            "last_page": last_page,
            "typed_evaluator": (
                {
                    "action": evaluator_answer.action,
                    "marginal_value": evaluator_answer.marginal_value,
                    "confidence": evaluator_answer.confidence,
                    "provider": evaluator_answer.provider,
                }
                if evaluator_answer is not None else None
            ),
        }
        try:
            result = self.provider.run(
                continuation_input,
                schema=CONTINUATION_SCHEMA,
                schema_name="discovery_continuation",
                mission=(
                    "Choose whether to read another bounded page, process the current frontier, "
                    "or yield and resume later. Base the answer on observed coverage and evidence "
                    "gaps; do not guess the quality of unread markets."
                ),
                max_tool_steps=0,
            )
            self.memory.record_turn(
                platform=platform, provider=result.provider, market_topic_id="", token_id="",
                input_payload=continuation_input, raw_output=result.raw_output,
                decision=result.value, status="OK",
            )
            return dict(result.value)
        except (DecisionProviderError, AttributeError, KeyError, TypeError, ValueError) as error:
            self.memory.record_turn(
                platform=platform, provider=getattr(self.provider, "name", ""),
                market_topic_id="", token_id="", input_payload=continuation_input,
                raw_output=getattr(error, "raw_output", ""), decision=None,
                status="ERROR", error=str(error),
            )
            LOGGER.warning("discovery continuation provider unavailable on %s: %s", platform, error)
            self.memory.record_runtime_incident(
                platform=platform,
                stage="discovery_continuation",
                severity="warning",
                message=str(error),
                fallback="使用有界扫描规则继续",
                details=_provider_incident_details(error),
            )
            return None

    @staticmethod
    def _typed_continuation_is_decisive(
        answer: Any, errors: dict[str, str], threshold: float
    ) -> bool:
        """Accept only strong, internally consistent typed routing without an Agent review."""

        if answer is None or errors or not is_high_confidence(answer.confidence, threshold):
            return False
        if answer.action == "CONTINUE_DISCOVERY":
            return answer.marginal_value >= 0.5
        if answer.action in {"PROCESS_FRONTIER", "SOURCE_EXHAUSTED"}:
            return answer.marginal_value <= 0.5
        return answer.action == "PAUSE_AND_RESUME"

    def _separated_verdicts(self) -> set[str]:
        """Which sub-floor screening verdicts this round's screener says stand apart from the rest.

        The floor is one way to be sure and the shape of the batch is another; both are allowed to
        drop a candidate, and neither is decided here. This asks only about the answers that failed
        the floor, and only when the screener answered cleanly in the first place - a round whose
        first pass errored is not one to ask a follow-up question of.
        """
        if self.evaluator is None or not self.evaluator.available or self._evaluator_candidate_errors:
            return set()
        assessments = [
            {
                "candidate_id": candidate_id,
                "action": item.action,
                "quality": round(float(item.quality), 4),
                "confidence": item.confidence,
                "under_review": (
                    item.action in {"DEFER", "REJECT"}
                    and not is_high_confidence(item.confidence, self._screening_threshold)
                    and item.confidence is not None
                ),
            }
            for candidate_id, item in self._evaluator_assessments.items()
        ]
        if not any(item["under_review"] for item in assessments):
            return set()
        verdicts = self.evaluator.screen_outliers(
            {"absolute_floor": self._screening_threshold},
            assessments,
        )
        return {candidate_id for candidate_id, trusted in verdicts.items() if trusted}

    def _adaptive_survey(
        self, plugin: PredictionMarketApiPlugin, budget: DiscoveryBudget
    ) -> tuple[list[Topic], set[str]]:
        started = time.monotonic()
        horizon_days = int(getattr(self.strategy, "horizon_days", 0) or 0)
        now_ms = int(time.time() * 1000)
        # Loaded before the first candidate is screened, because the screener is asked about every
        # page and each of those calls has to see the same history.
        self._screening_history = self.memory.screening_history(platform=plugin.name)
        self._screening_calibration = self.memory.screening_calibration(platform=plugin.name)
        if callable(getattr(self.evaluator, "set_quality", None)):
            self.evaluator.set_quality(self.memory.evaluator_quality(platform=plugin.name))
        deadline_listing = getattr(plugin, "list_topics_by_deadline", None)
        sources: list[tuple[str, Any, dict[str, Any]]] = []
        if horizon_days > 0 and callable(deadline_listing):
            sources.append(("deadline", deadline_listing, {
                "after_ms": now_ms, "before_ms": now_ms + horizon_days * 86400 * 1000
            }))
        sources.append(("catalog", plugin.list_topics, {}))
        source_names = {name for name, _listing, _extra in sources}
        stored_resume = self.memory.survey_plan(plugin.name).get("resume") or {}
        stored_cursors = stored_resume.get("cursors") if isinstance(stored_resume, dict) else {}
        stored_active = stored_resume.get("active") if isinstance(stored_resume, dict) else []
        cursors = {
            name: max(0, int((stored_cursors or {}).get(name, 0)))
            for name in source_names
        }
        active = (
            {str(name) for name in stored_active if str(name) in source_names}
            if stored_active else set(source_names)
        )
        if not active:
            active = set(source_names)
        seen: dict[str, Topic] = {}
        near_dated: set[str] = set()
        assessments: dict[str, Any] = {}
        self._evaluator_candidate_errors = {}
        self._scan_audit = []
        page_number = 0
        disagreement_probe_used = False
        resume_saved = False
        time_exhausted = False

        def save_resume(reason: str) -> None:
            nonlocal resume_saved
            if not active:
                self.memory.clear_survey_resume(plugin.name)
                resume_saved = True
                return
            self.memory.save_survey_resume(plugin.name, {
                "cursors": dict(cursors),
                "active": sorted(active),
                "reason": reason,
                "saved_at": int(time.time() * 1000),
            })
            resume_saved = True

        while active and page_number < self.max_scan_pages:
            if time.monotonic() - started >= self.max_scan_seconds:
                self._scan_audit.append({"stop": "resource_time_limit"})
                save_resume("resource_time_limit")
                break
            progressed = False
            for source, listing, extra in sources:
                if source not in active or page_number >= self.max_scan_pages:
                    continue
                if time.monotonic() - started >= self.max_scan_seconds:
                    self._scan_audit.append({"stop": "resource_time_limit"})
                    save_resume("resource_time_limit")
                    time_exhausted = True
                    break
                try:
                    page = listing(
                        offset=cursors[source], limit=max(1, int(plugin.topic_page_size())), **extra
                    )
                except Exception as error:
                    LOGGER.warning("%s adaptive %s listing failed: %s", plugin.name, source, error)
                    active.discard(source)
                    self._scan_audit.append({"source": source, "stop": "source_error", "error": str(error)[:200]})
                    continue
                progressed = True
                page_number += 1
                fresh = [topic for topic in page.topics if topic.topic_id not in seen]
                for topic in fresh:
                    seen[topic.topic_id] = topic
                    if source == "deadline":
                        near_dated.add(topic.topic_id)
                if not page.has_more or not page.topics:
                    active.discard(source)
                else:
                    cursors[source] = page.next_offset
                # A local evaluator may need one request per candidate. Sending an entire venue
                # page (often 100 topics) before checking the clock can hold a scan open for many
                # minutes, preventing even its observation record from being written. The venue's
                # ordered page remains in the survey; spend the remaining screening budget on its
                # leading candidates and leave the rest for ordinary agent selection.
                remaining = max(0.0, self.max_scan_seconds - (time.monotonic() - started))
                per_candidate = bool(getattr(self.evaluator, "per_candidate_requests", False))
                screening_limit = (
                    min(len(fresh), 24, max(0, int(remaining / 2)))
                    if per_candidate
                    else len(fresh)
                )
                candidates = [self._evaluation_candidate(topic) for topic in fresh[:screening_limit]]
                answers: list[Any] = []
                attempted_items = 0
                if self.evaluator is not None and self.evaluator.available:
                    screening_state = {
                        "platform": plugin.name,
                        "pages_scanned": page_number,
                        "topics_seen": len(seen),
                        "source": source,
                        # How this screener's own verdicts have turned out here so far, so a
                        # round can be better calibrated than the one before it.
                        "screening_calibration": self._screening_calibration,
                        "screening_feedback": screening_feedback(self._screening_calibration),
                    }
                    screening_state["_scan_deadline_monotonic"] = started + self.max_scan_seconds
                    chunk_size = 4 if per_candidate else max(1, len(candidates))
                    for offset in range(0, len(candidates), chunk_size):
                        if per_candidate and time.monotonic() >= started + self.max_scan_seconds:
                            break
                        chunk = candidates[offset:offset + chunk_size]
                        attempted_items += len(chunk)
                        chunk_answers = self.evaluator.evaluate_candidates(screening_state, chunk)
                        answers.extend(chunk_answers)
                        self._evaluator_candidate_errors.update(self.evaluator.errors)
                        for name, error in getattr(self.evaluator, "fallback_errors", {}).items():
                            self.memory.record_runtime_incident(
                                platform=plugin.name, stage="evaluator_fallback",
                                severity="warning", message=f"{name}: {error}",
                                fallback="本次粗筛改用下一个已启用评估器",
                            )
                        if not chunk_answers and self.evaluator.errors:
                            break
                assessments.update({answer.candidate_id: answer for answer in answers})
                if time.monotonic() - started >= self.max_scan_seconds:
                    self._scan_audit.append({
                        "source": source, "items": len(page.topics),
                        "screened_items": len(answers), "attempted_items": attempted_items,
                        "stop": "resource_time_limit",
                    })
                    save_resume("resource_time_limit")
                    time_exhausted = True
                    break
                frontier = sorted(
                    [
                        {**self._evaluation_candidate(topic),
                         "typed_quality": assessments.get(topic.topic_id).quality if topic.topic_id in assessments else None,
                         "typed_action": assessments.get(topic.topic_id).action if topic.topic_id in assessments else None}
                        for topic in seen.values()
                    ],
                    key=lambda item: (
                        item.get("typed_action") != "PRIORITIZE",
                        -(item.get("typed_quality") or 0.0),
                        -float(item.get("liquidity_usdt") or 0.0),
                    ),
                )
                page_summary = {
                    "source": source,
                    "items": len(page.topics),
                    "new_items": len(fresh),
                    "screened_items": len(answers),
                    "attempted_items": attempted_items,
                    "has_more": bool(page.has_more),
                    "next_offset": page.next_offset,
                }
                scan_state = {
                    "pages_scanned": page_number,
                    "topics_seen": len(seen),
                    "sources_remaining": sorted(active),
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                    "resource_page_limit": self.max_scan_pages,
                    "resource_time_limit_seconds": self.max_scan_seconds,
                }
                scan_state["_scan_deadline_monotonic"] = started + self.max_scan_seconds
                typed = (
                    self.evaluator.assess_continuation(scan_state, frontier[:40], page_summary)
                    if self.evaluator is not None and self.evaluator.available
                    else None
                )
                evaluator_errors = (
                    dict(self.evaluator.errors)
                    if self.evaluator is not None and self.evaluator.available else {}
                )
                if self.evaluator is not None:
                    for name, error in getattr(self.evaluator, "fallback_errors", {}).items():
                        self.memory.record_runtime_incident(
                            platform=plugin.name, stage="evaluator_fallback",
                            severity="warning", message=f"{name}: {error}",
                            fallback="本次续扫评估改用下一个已启用评估器",
                        )
                typed_is_decisive = self._typed_continuation_is_decisive(
                    typed, evaluator_errors, self._screening_threshold
                )
                # An optional Agent continuation can run through several provider timeouts
                # before the first observation is saved. Only ask when all configured attempts
                # can fit in the remaining scan budget. Unknown test/third-party timeouts keep
                # their existing behavior; the final candidate choice always stays with Agent.
                provider_items = getattr(self.provider, "providers", None) or [self.provider]
                provider_timeouts = [
                    getattr(getattr(item, "backend", None), "timeout", None)
                    for item in provider_items
                ]
                remaining_scan = max(0.0, started + self.max_scan_seconds - time.monotonic())
                provider_budgeted = bool(
                    provider_timeouts
                    and all(isinstance(timeout, (int, float)) and timeout > 0
                            for timeout in provider_timeouts)
                    and sum(provider_timeouts) + 5 >= remaining_scan
                )
                continuation_budgeted = bool(
                    provider_budgeted or (
                        self.evaluator is not None
                        and getattr(self.evaluator, "per_candidate_requests", False)
                    )
                )
                llm = (
                    self._llm_continuation(
                        platform=plugin.name, scan_state=scan_state, frontier=frontier,
                        last_page=page_summary, evaluator_answer=typed,
                    )
                    if self.evaluator is not None and self.evaluator.available
                    and not typed_is_decisive and not continuation_budgeted
                    else None
                )
                audit = {
                    **page_summary,
                    "typed": (
                        {"action": typed.action, "marginal_value": typed.marginal_value,
                         "confidence": typed.confidence, "provider": typed.provider}
                        if typed else None
                    ),
                    "llm": llm,
                    "llm_skipped": (
                        "decisive_typed_evaluation" if typed_is_decisive
                        else "single_candidate_screening_budget" if per_candidate
                        else "provider_timeout_exceeds_scan_budget" if continuation_budgeted
                        else None
                    ),
                    "evaluator_errors": evaluator_errors,
                }
                self._scan_audit.append(audit)
                typed_stop = typed is not None and typed.action in {
                    "PROCESS_FRONTIER", "PAUSE_AND_RESUME", "SOURCE_EXHAUSTED"
                }
                llm_stop = llm is not None and llm.get("action") in {
                    "PROCESS_FRONTIER", "PAUSE_AND_RESUME", "SOURCE_EXHAUSTED"
                }
                if typed is not None and llm is not None:
                    if typed_stop and llm_stop:
                        actions = {typed.action, str(llm.get("action", ""))}
                        if "PAUSE_AND_RESUME" in actions:
                            save_resume("joint_pause")
                        else:
                            self.memory.clear_survey_resume(plugin.name)
                        active.clear()
                        break
                    if typed_stop != llm_stop:
                        if disagreement_probe_used:
                            save_resume("bounded_disagreement_probe_complete")
                            active.clear()
                            audit["stop"] = "bounded_disagreement_probe_complete"
                            break
                        disagreement_probe_used = True
                        audit["probe"] = "one_more_page"
                elif typed_stop or llm_stop:
                    action = typed.action if typed_stop else str(llm.get("action", ""))
                    if action == "PAUSE_AND_RESUME":
                        save_resume("evaluator_pause")
                    else:
                        self.memory.clear_survey_resume(plugin.name)
                    active.clear()
                    break
            if time_exhausted or not progressed:
                break
        if active and not resume_saved and page_number >= self.max_scan_pages:
            self._scan_audit.append({"stop": "resource_page_limit"})
            save_resume("resource_page_limit")
        elif not active and not resume_saved:
            self.memory.clear_survey_resume(plugin.name)
        self._evaluator_assessments = assessments
        ordered = sorted(
            seen.values(),
            key=lambda topic: (
                topic.topic_id not in near_dated,
                assessments.get(topic.topic_id).action != "PRIORITIZE" if topic.topic_id in assessments else True,
                -(assessments.get(topic.topic_id).quality if topic.topic_id in assessments else 0.0),
            ),
        )
        return self._with_requested(
            plugin, ordered, budget, deadline_monotonic=started + self.max_scan_seconds
        ), near_dated

    def _survey(
        self, plugin: PredictionMarketApiPlugin, budget: DiscoveryBudget
    ) -> tuple[list[Topic], set[str]]:
        return self._adaptive_survey(plugin, budget)

    def _with_requested(
        self,
        plugin: PredictionMarketApiPlugin,
        topics: list[Topic],
        budget: DiscoveryBudget,
        *,
        deadline_monotonic: float,
    ) -> list[Topic]:
        """Add what the agent asked to look for, alongside what the venue happens to list first.

        The listing is one fixed opinion - most traded first - and a robot that only ever sees that
        can only ever find something there. What is worth looking at is a judgement about the
        moment: a catalyst due this week, a category that moved, a question it saw quoted elsewhere.
        Built-in venues can match the already-fetched listing without extra network requests.
        A third-party venue without that capability is only asked while scan time remains.

        These are added to the listing, never instead of it. A query that finds nothing leaves the
        round exactly as it was.
        """
        queries = self.memory.survey_plan(plugin.name).get("queries") or []
        if not queries:
            return topics
        seen = {topic.topic_id for topic in topics}
        lightweight = bool(getattr(plugin, "supports_lightweight_search", False))
        for query in queries[:8]:
            if not lightweight and time.monotonic() >= deadline_monotonic:
                self._scan_audit.append({"requested_search": "skipped_scan_budget"})
                break
            try:
                if lightweight:
                    found = plugin.search_market_candidates(
                        query, budget.search_result_limit, lightweight=True
                    )
                else:
                    found = plugin.search_market_candidates(
                        query, min(5, budget.search_result_limit)
                    )
            except Exception as error:
                LOGGER.warning("requested survey %r failed on %s: %s", query, plugin.name, error)
                continue
            for candidate in found:
                topic = getattr(candidate, "topic", None)
                if topic is None or topic.topic_id in seen:
                    continue
                seen.add(topic.topic_id)
                topics.append(topic)
        return topics

    def _prior_weights(self) -> dict[str, float]:
        if not self.evolution_enabled:
            return {}
        return {
            item["prior_id"]: float(item["weight"])
            for item in self.memory.load_discovery_priors(DISCOVERY_EVOLUTION_KEY)
        }

    def _priors(self) -> tuple[DiscoveryPrior, ...]:
        seeds = getattr(self.strategy, "seed_priors", None)
        base = tuple(seeds()) if callable(seeds) else ()
        if not self.evolution_enabled:
            return base
        stored = {item["prior_id"]: item for item in self.memory.load_discovery_priors(DISCOVERY_EVOLUTION_KEY)}
        merged = [
            DiscoveryPrior(
                prior.prior_id,
                prior.text,
                float(stored.get(prior.prior_id, {}).get("weight", prior.weight)),
                int(stored.get(prior.prior_id, {}).get("sample_size", 0)),
            )
            for prior in base
        ]
        return tuple(merged)

    def _lessons(self) -> tuple[DiscoveryLesson, ...]:
        if not self.evolution_enabled:
            return ()
        return tuple(
            DiscoveryLesson(
                lesson_id=item["lesson_id"],
                text=item["text"],
                bucket=item["bucket"],
                sample_size=item["sample_size"],
                support=item["support"],
                recorded_at=item["recorded_at"],
                confirmed_at=item["confirmed_at"],
            )
            for item in self.memory.load_discovery_lessons(DISCOVERY_EVOLUTION_KEY)
        )

    def _suggested_rank(
        self, topic_id: str, features_by_topic: dict[str, dict[str, Any]],
        weights: dict[str, float], budget: DiscoveryBudget,
    ) -> float:
        """Where the framework would start reading, preferring the screener's own ordering.

        The formula below is a set of constants nobody measured - fresh listing plus one, mid-tier
        liquidity plus a half, recently selected times a quarter - applied to every market on every
        platform alike. The screener has already read each candidate and said what it is worth, on
        the facts rather than on a table of weights, so that is the order used when it answered.
        The formula remains for the rounds where no screener is configured or it failed.
        """
        typed = self._evaluator_assessments.get(topic_id)
        if typed is not None and not self._evaluator_candidate_errors:
            priority = {"PRIORITIZE": 3.0, "NEEDS_DATA": 2.0, "DEFER": 1.0, "REJECT": 0.0}
            return priority.get(str(typed.action), 1.0) + float(typed.quality or 0.0)
        return self._prescore(features_by_topic[topic_id], weights, budget)

    def _prescore(
        self, features: dict[str, Any], weights: dict[str, float], budget: DiscoveryBudget
    ) -> float:
        """Rank candidates cheaply so the Agent reads a shortlist, not the whole platform.

        This orders what the Agent looks at. It never decides what is selected.
        """
        buckets = set(features.get("buckets", ()))
        score = 1.0
        if "listing:new" in buckets:
            score += weights.get("fresh_listing", 1.0)
        if "flow:quiet" in buckets and not features.get("first_seen"):
            score += 0.5 * weights.get("stale_price_moving_reality", 1.0)
        if "flow:moving" in buckets:
            score += weights.get("stale_price_moving_reality", 1.0)
        if "attention:never" in buckets:
            score += weights.get("mid_tier_liquidity", 1.0)
        liquidity = float(features.get("liquidity_usdt", 0.0))
        if 1_000 <= liquidity <= 100_000:
            score += 0.5 * weights.get("mid_tier_liquidity", 1.0)
        seconds_since_selected = features.get("seconds_since_selected")
        if seconds_since_selected is not None and seconds_since_selected < budget.cooldown_seconds:
            score *= 0.25
        return score

    # ------------------------------------------------------------- discovery

    def discover(
        self, *, platform: str, plugin: PredictionMarketApiPlugin
    ) -> tuple[Topic, ...]:
        budget = self.strategy.budget()
        now_ms = int(time.time() * 1000)
        self._screening_threshold = self._adaptive_screening_threshold()
        surveyed, near_dated = self._survey(plugin, budget)
        if not surveyed:
            return ()

        previous = self.memory.previous_topic_observations(platform=platform, before_ms=now_ms)
        attention = self.memory.topic_attention_history(platform=platform)
        verdicts = self.memory.topic_verdict_history(platform=platform)
        weights = self._prior_weights()

        features_by_topic: dict[str, dict[str, Any]] = {}
        observations: list[dict[str, Any]] = []
        for topic in surveyed:
            features = topic_features(
                topic,
                previous=previous.get(topic.topic_id),
                attention=attention.get(topic.topic_id),
                now_ms=now_ms,
                verdict=verdicts.get(topic.topic_id),
            )
            typed = self._evaluator_assessments.get(topic.topic_id)
            if typed is not None:
                features["typed_evaluation"] = {
                    "action": typed.action,
                    "quality": typed.quality,
                    "confidence": typed.confidence,
                    "provider": typed.provider,
                    "evaluator_name": typed.evaluator_name,
                    # Whether this is one window of a series that will be relisted. A slate of
                    # copies of one question is a slate that decides one question several times.
                    **({"recurring_series": typed.recurring}
                       if typed.recurring is not None else {}),
                }
            features_by_topic[topic.topic_id] = features
            observations.append(
                {
                    "market_topic_id": topic.topic_id,
                    "title": topic.title,
                    "status": topic.status,
                    "liquidity_usdt": topic.liquidity_usdt,
                    "volume_usdt": topic.volume_usdt,
                    "features": features,
                }
            )
        self.memory.record_topic_observations(platform=platform, observations=observations)

        # An order, offered as an opinion. What settles soon leads it, because those markets are
        # small - a five-minute crypto market has almost no volume next to an election months out -
        # and ranking them together buries exactly the ones this runtime prefers. Which of them to
        # read, in what order and how many, is the round's own call; this is the framework saying
        # where it would start, not where the round must.
        suggested = sorted(
            surveyed,
            key=lambda item: (
                item.topic_id not in near_dated,
                -self._suggested_rank(item.topic_id, features_by_topic, weights, budget),
            ),
        )
        by_id = {topic.topic_id: topic for topic in surveyed}

        selection_capacity = len(surveyed)
        selections, mode, skipped_reason, fetched = self._select(
            platform=platform,
            plugin=plugin,
            pool=suggested,
            near_dated=near_dated,
            features_by_topic=features_by_topic,
            budget=budget,
            selection_capacity=selection_capacity,
            now_ms=now_ms,
        )
        strategy_name = self.strategy.name if mode == "agent" else f"{self.strategy.name}:{mode}"
        # A market the round went and found for itself is selectable like any other.
        by_id.update(fetched)

        chosen: list[Topic] = []
        for position, item in enumerate(selections, start=1):
            topic = by_id.get(str(item.get("topic_id", "")))
            if topic is None or topic in chosen:
                continue
            selection_id = self.memory.record_discovery_selection(
                platform=platform,
                strategy=strategy_name,
                market_topic_id=topic.topic_id,
                position=position,
                reason=str(item.get("reason", "")),
                priors=[str(name) for name in item.get("priors", [])][:4],
                features=features_by_topic.get(topic.topic_id, {}),
            )
            self._selection_ids[(platform, str(topic.topic_id))] = selection_id
            chosen.append(topic)
            if len(chosen) >= selection_capacity:
                break
        if not chosen:
            # The model is asked for this and was handing it back all along; dropping it turned a
            # stated judgement into a silent zero, which reads as a broken cycle rather than as a
            # round where nothing was worth a decision slot.
            LOGGER.info(
                "platform=%s selected nothing from %d surveyed: %s",
                platform,
                len(surveyed),
                skipped_reason or "no reason was given",
            )
        LOGGER.info(
            "platform=%s surveyed=%d near_dated=%d selected=%d strategy=%s evolution=%s",
            platform,
            len(surveyed),
            len(near_dated),
            len(chosen),
            strategy_name,
            self.evolution_enabled,
        )
        return tuple(chosen)

    def selection_id(self, platform: str, topic_id: str) -> int | None:
        return self._selection_ids.get((platform, str(topic_id)))

    def _adaptive_screening_threshold(self) -> float:
        """Calibrate coarse screening from safety-sample false negatives.

        The prior starts at 0.90. Candidates that screening wanted to defer/reject but the
        quality guard retained form an observable audit sample: if those later prove useful the
        threshold rises, while repeated harmless exclusions let it fall gradually. The evaluator
        never gets a lower threshold than 0.80.
        """

        false_negatives = 3.0
        observations = 6.0
        for row in self.memory.reviewed_selection_outcomes():
            features = row.get("features") or {}
            typed = features.get("typed_evaluation") if isinstance(features, dict) else None
            if not isinstance(typed, dict) or typed.get("action") not in {"DEFER", "REJECT"}:
                continue
            confidence = typed.get("confidence")
            if confidence is None or float(confidence) < MIN_SCREENING_CONFIDENCE:
                continue
            observations += 1.0
            false_negatives += float(bool((row.get("outcome") or {}).get("useful")))
        posterior_false_negative_rate = false_negatives / observations
        return round(
            max(MIN_SCREENING_CONFIDENCE, min(0.99, 0.80 + 0.20 * posterior_false_negative_rate)),
            4,
        )

    def _select(
        self,
        *,
        platform: str,
        plugin: PredictionMarketApiPlugin,
        pool: list[Topic],
        near_dated: set[str],
        features_by_topic: dict[str, dict[str, Any]],
        budget: DiscoveryBudget,
        selection_capacity: int,
        now_ms: int,
    ) -> tuple[list[dict[str, Any]], str, str, dict[str, Topic]]:
        payload = compose_discovery_payload(
            self.strategy,
            priors=self._priors(),
            lessons=self._lessons(),
            measurements=self.measurements(),
            evolution_enabled=self.evolution_enabled,
            now_ms=now_ms,
        )
        toolbox = _DiscoveryToolbox(
            plugin=plugin,
            memory=self.memory,
            platform=platform,
            budget=budget,
            cross_platform_search=self.cross_platform_search,
            measurements=self.measurements,
            research=self.research_contributions,
        )
        horizon_days = int(getattr(self.strategy, "horizon_days", 0) or 0)
        trusted_outliers = self._separated_verdicts()
        excluded_topics: list[tuple[Topic, Any]] = []
        eligible_pool: list[Topic] = []
        for topic in pool:
            assessment = self._evaluator_assessments.get(topic.topic_id)
            if (
                assessment is not None
                and not self._evaluator_candidate_errors
                and assessment.action in {"DEFER", "REJECT"}
                and (
                    is_high_confidence(assessment.confidence, self._screening_threshold)
                    or topic.topic_id in trusted_outliers
                )
            ):
                excluded_topics.append((topic, assessment))
            else:
                eligible_pool.append(topic)
        minimum_frontier = max(1, math.ceil(math.sqrt(len(pool))))
        restore_count = min(
            len(excluded_topics), max(0, minimum_frontier - len(eligible_pool))
        )
        restored_ids = {
            topic.topic_id
            for topic, _assessment in sorted(
                excluded_topics,
                key=lambda item: (
                    float(item[1].confidence or 0.0),
                    -float(item[1].quality),
                ),
            )[:restore_count]
        }
        eligible_pool.extend(
            topic for topic, _assessment in excluded_topics if topic.topic_id in restored_ids
        )
        order = {topic.topic_id: index for index, topic in enumerate(pool)}
        eligible_pool.sort(key=lambda topic: order[topic.topic_id])
        excluded = [
            {
                "topic_id": topic.topic_id,
                "action": assessment.action,
                "confidence": assessment.confidence,
                "quality": assessment.quality,
                "provider": assessment.provider,
            }
            for topic, assessment in excluded_topics
            if topic.topic_id not in restored_ids
        ]
        pool = eligible_pool
        selection_capacity = len(pool)
        filter_summary = {
            "excluded_count": len(excluded),
            "retained_count": len(pool),
            "restored_for_quality_audit_count": len(restored_ids),
            "confidence_threshold": self._screening_threshold,
            "minimum_threshold": MIN_SCREENING_CONFIDENCE,
            "evaluator_errors": dict(self._evaluator_candidate_errors),
            "policy": "coarse token-saving filter only; adaptive safety sample cannot be filtered",
        }
        filter_audit = {
            **filter_summary,
            "excluded": excluded,
            "restored_for_quality_audit": sorted(restored_ids),
        }
        # Everything surveyed, described with what a listing already tells you and nothing that
        # costs a read: the round decides which of these are worth reading, in what order, and how
        # many. `suggested_rank` is where the framework would start, offered as an opinion - what
        # settles inside the operator's preferred window leads it, because those markets are small
        # and get buried when ranked against months-out elections.
        candidates = [
            {
                "topic_id": topic.topic_id,
                "rank_suggestion": position,
                "title": topic.title,
                "question": topic.question,
                "description": topic.description[:400],
                "category": topic.category,
                "status": topic.status,
                "liquidity_usdt": topic.liquidity_usdt,
                "volume_usdt": topic.volume_usdt,
                "settles_inside_preferred_window": topic.topic_id in near_dated or None,
                "signals": {
                    key: value
                    for key, value in features_by_topic.get(topic.topic_id, {}).items()
                    if key != "buckets"
                },
            }
            for position, topic in enumerate(pool, start=1)
        ]
        instructions = self.operator_instructions(platform) if self.operator_instructions else {}
        request = {
            "platform": platform,
            "platform_capabilities": plugin.capabilities.to_dict(),
            "available_selection_count": selection_capacity,
            "discovery_strategy": prompt_json_payload(payload),
            "candidates": candidates,
            "scan_audit": list(self._scan_audit),
            # Only the compact summary reaches the model; excluded candidate details stay in the
            # ledger, otherwise the audit data would erase the token saving from coarse screening.
            "typed_frontier_filter": filter_summary,
            # Preferences, not gates. The operator's are about how the money is made, and the
            # round may go outside them when it can say what makes that worth doing.
            "operator_preferences": {
                "settles_within_days": horizon_days,
                "why": "short-dated, small and often: money back soon, mistakes cheap, evidence fast",
                "may_be_exceeded": "with a concrete reason about this opportunity, stated in the selection",
            },
            "nothing_here_is_priced_yet": "VERIFY_TOPICS reads deadlines, resolutions and spreads for the ids you name",
            # Conditions the operator attached to their money. A slot spent on something they ruled
            # out is a slot spent against them, so the round that picks the slates sees them too.
            **({"operator_instructions": instructions} if instructions.get("count") else {}),
        }
        request["history_handoff"] = {
            "storage": "shared SQLite history exposed to the active CLI",
            "previous_round": self.memory.latest_decision_reference(
                platform=platform,
                market_topic_id="",
                token_id="",
            ),
            "instruction": (
                "This is a new discovery round. The previous round is only a retrieval pointer, "
                "not a conclusion to copy. Query any additional history with the CLI's own tools."
            ),
        }
        # Every call to a model leaves a row, this one included. It used to leave none: a discovery
        # round that failed, or that judged nothing worth a slot, produced no ledger entry at all,
        # so the only visible trace was a cycle that quietly did nothing. An operator reading the
        # ledger could not tell "the model was asked and declined" from "nothing ran".
        decision_id = self.memory.begin_decision(
            platform=platform,
            market_topic_id="",
            market_id="",
            token_id="",
            strategy_name=f"{self.strategy.name}:discovery",
            strategy_sha256=self.strategy.sha256,
            # What the round was working from. What it went on to read about these is merged in
            # when the round ends, so the record shows the same picture the round had.
            context={
                "stage": "discovery",
                "candidates": candidates,
                "available_selection_count": selection_capacity,
                "preferred_window_days": horizon_days,
                "scan_audit": list(self._scan_audit),
                "typed_frontier_filter": filter_audit,
                # Share the same object so begin_decision's exact current_round_id also reaches
                # the first CLI input without the framework reading or summarising old records.
                "history_handoff": request["history_handoff"],
            },
        )
        def record_step(**values: Any) -> None:
            # A discovery round reads books, verifies resolutions and searches other platforms, and
            # none of it was written down: only the decision path passed a recorder, so the console
            # showed an empty research trail for every round and there was no way to tell a round
            # that went and looked from one that answered off the list.
            self.memory.record_agent_step(
                platform=platform,
                market_topic_id="",
                token_id="",
                decision_id=decision_id,
                **values,
            )

        try:
            result = self.provider.run(
                request,
                step_recorder=record_step,
                schema=DISCOVERY_SCHEMA,
                schema_name="market_discovery",
                mission=DISCOVERY_MISSION,
                control_mission=DISCOVERY_CONTROL_MISSION,
                instructions=render_overlay_block(payload, "MARKET_DISCOVERY_STRATEGY"),
                max_tool_steps=budget.agent_tool_steps,
                final_step_name="FINAL_SELECTION",
                tool_executor=toolbox.execute,
                tool_descriptions=toolbox.descriptions,
                should_stop=lambda: self.memory.is_cancelled(decision_id),
            )
        except DecisionCancelled:
            # The round's record was deleted while the agent was still choosing. Its choices have
            # nowhere to be recorded, so this round selects nothing rather than acting on them.
            LOGGER.info("discovery round %s on %s was deleted; stopped", decision_id, platform)
            return [], "cancelled", "deleted while running", dict(toolbox.found)
        except (DecisionProviderError, AttributeError, TypeError) as error:
            # Nothing is selected without a model having chosen it. Ordering the pool by a formula
            # and trading it anyway produced rounds whose selections were never reasoned about,
            # attributed to a strategy that had not run - and it did that precisely when something
            # was already wrong. A round that cannot ask a model selects nothing and says why; the
            # candidates keep, and the next round asks again.
            LOGGER.error("market discovery agent unavailable on %s; selecting nothing: %s",
                         platform, error)
            self.memory.record_turn(
                platform=platform, provider=getattr(self.provider, "name", ""),
                market_topic_id="", token_id="", input_payload=request,
                raw_output=getattr(error, "raw_output", ""), decision=None,
                status="ERROR", error=str(error), decision_id=decision_id,
            )
            self.memory.record_runtime_incident(
                platform=platform,
                stage="market_selection",
                severity="error",
                message=str(error),
                fallback="本轮不选任何标的；不按公式代替模型判断",
                details={
                    **_provider_incident_details(error),
                    "decision_id": decision_id,
                    "candidates_held": len(pool),
                },
            )
            self.memory.complete_decision(
                decision_id,
                provider=getattr(self.provider, "name", ""),
                model_raw_output=getattr(error, "raw_output", ""),
                status="PROVIDER_ERROR",
                error=str(error),
            )
            return [], "unavailable", str(error)[:400], dict(toolbox.found)
        if self.memory.is_cancelled(decision_id):
            LOGGER.info("discovery round %s on %s was deleted after answering", decision_id, platform)
            return [], "cancelled", "deleted while running", dict(toolbox.found)
        self.memory.record_turn(
            platform=platform, provider=result.provider, market_topic_id="", token_id="",
            input_payload=request, raw_output=result.raw_output, decision=result.value,
            status="OK", decision_id=decision_id,
        )
        if toolbox.verified:
            # The round's own reading of the candidates, kept where the candidates are.
            self.memory.merge_decision_context(
                decision_id,
                {"candidates": [
                    {**candidate, **toolbox.verified.get(str(candidate.get("topic_id")), {})}
                    for candidate in candidates
                ],
                 "verified_count": len(toolbox.verified)},
            )
        selections = [item for item in result.value.get("selections", []) if isinstance(item, dict)]
        skipped_reason = str(result.value.get("skipped_reason", ""))
        # How soon to come back and what to go looking for are judgements about this platform right
        # now - how fast its prices move, what catalyst is due - and the only thing here that has
        # looked at that is the agent that just read it.
        self.memory.save_survey_plan(
            platform=platform,
            queries=[
                str(item) for item in result.value.get("next_survey_queries", [])
                if str(item).strip()
            ],
            next_scan_seconds=int(result.value.get("next_scan_seconds", 0) or 0),
            reason=str(result.value.get("pacing_reason", "")),
        )
        self.memory.complete_decision(
            decision_id,
            provider=result.provider,
            research=result.research_trace,
            model_raw_output=result.raw_output,
            final_decision={
                "selections": selections,
                "skipped_reason": skipped_reason,
                "headline": str(result.value.get("headline", "")).strip()[:90],
                "next_scan_seconds": int(result.value.get("next_scan_seconds", 0) or 0),
                "next_survey_queries": list(result.value.get("next_survey_queries", []) or []),
                "pacing_reason": str(result.value.get("pacing_reason", "")),
            },
            status="OK" if selections else "NO_ACTION",
            error="" if selections else skipped_reason,
        )
        return selections, "agent", skipped_reason, dict(toolbox.found)

    # -------------------------------------------------------------- evolution

    def measurements(self) -> dict[str, Any]:
        """Per-bucket outcome rates measured from this runtime's own completed decisions."""
        rows = [
            row for row in self.memory.reviewed_selection_outcomes()
            if "decisions" not in row["outcome"] or int(row["outcome"].get("decisions", 0)) > 0
        ]
        if not rows:
            return {"samples": 0, "baseline_effective_activity_rate": 0.0,
                    "per_platform": {}, "buckets": {}}
        useful_total = sum(1 for row in rows if row["outcome"].get("useful"))
        baseline = useful_total / len(rows)
        per_platform: dict[str, dict[str, float]] = {}
        buckets: dict[str, dict[str, float]] = {}
        for row in rows:
            platform = row["platform"]
            platform_entry = per_platform.setdefault(
                platform, {"evaluated": 0, "effective_activity": 0, "resolved": 0, "net_pnl": 0.0}
            )
            platform_entry["evaluated"] += 1
            platform_entry["effective_activity"] += int(bool(row["outcome"].get("useful")))
            if row["outcome"].get("net_pnl_known"):
                platform_entry["resolved"] += 1
                platform_entry["net_pnl"] += float(row["outcome"].get("realized_pnl", 0.0))
            for bucket in row["features"].get("buckets", ()):
                entry = buckets.setdefault(
                    str(bucket), {"selections": 0, "useful": 0, "resolved": 0, "net_pnl": 0.0}
                )
                entry["selections"] += 1
                if row["outcome"].get("useful"):
                    entry["useful"] += 1
                if row["outcome"].get("net_pnl_known"):
                    entry["resolved"] += 1
                    entry["net_pnl"] += float(row["outcome"].get("realized_pnl", 0.0))
        return {
            "samples": len(rows),
            "baseline_effective_activity_rate": round(baseline, 4),
            "baseline_useful_rate": round(baseline, 4),
            "per_platform": {
                name: {
                    **entry,
                    "effective_activity_rate": round(entry["effective_activity"] / entry["evaluated"], 4),
                    "net_profit_per_evaluated_selection": round(entry["net_pnl"] / entry["evaluated"], 6),
                    "net_profit_per_resolved_selection": (
                        round(entry["net_pnl"] / entry["resolved"], 6) if entry["resolved"] else None
                    ),
                }
                for name, entry in per_platform.items()
            },
            "buckets": {
                name: {
                    "selections": int(entry["selections"]),
                    "effective_activity_rate": round(entry["useful"] / entry["selections"], 4),
                    "useful_rate": round(entry["useful"] / entry["selections"], 4),
                    "resolved": int(entry["resolved"]),
                    "net_pnl": round(entry["net_pnl"], 6),
                    "net_profit_per_selection": round(entry["net_pnl"] / entry["selections"], 6),
                }
                for name, entry in sorted(buckets.items())
                if entry["selections"] > 0
            },
        }

    def review(self, *, now_ms: int | None = None) -> dict[str, int]:
        """Close out settled selections, re-weight priors, and rewrite measured lessons.

        This runs regardless of which strategy is selecting markets and regardless of whether any
        strategy currently applies the result, so the measured overlay is never stale when it is
        switched back on.
        """
        now = int(time.time() * 1000) if now_ms is None else int(now_ms)
        pending = self.memory.unreviewed_selections(settled_before_ms=now - REVIEW_SETTLE_MS)
        for item in pending:
            outcome = self.memory.selection_downstream(
                platform=item["platform"],
                market_topic_id=item["market_topic_id"],
                since_ms=item["selected_at"],
                until_ms=now,
                selection_id=item["id"],
            )
            self.memory.mark_selection_reviewed(
                item["id"], outcome, final=bool(outcome.get("outcome_complete"))
            )
        rows = self.memory.reviewed_selection_outcomes()
        if not rows:
            return {"reviewed": len(pending), "priors": 0, "lessons": 0, "retired": 0}
        baseline = sum(1 for row in rows if row["outcome"].get("useful")) / len(rows)
        priors = self._reweight_priors(rows, baseline)
        written, retired = self._rewrite_lessons(baseline)
        return {
            "reviewed": len(pending),
            "priors": priors,
            "lessons": written,
            "retired": retired,
        }

    def _reweight_priors(self, rows: list[dict[str, Any]], baseline: float) -> int:
        """Measure each prior by what happened to the selections that cited it."""
        known_pnl = [
            abs(float(row["outcome"].get("realized_pnl", 0.0)))
            for row in rows if row["outcome"].get("net_pnl_known")
        ]
        pnl_scale = max(1e-9, sorted(known_pnl)[len(known_pnl) // 2]) if known_pnl else 1.0
        row_values: list[float] = []
        for row in rows:
            if "decisions" in row["outcome"] and int(row["outcome"].get("decisions", 0)) <= 0:
                continue
            activity = 1.0 if row["outcome"].get("useful") else 0.0
            value = activity
            if row["outcome"].get("net_pnl_known"):
                value = 0.5 * activity + 0.5 * (
                    0.5 + 0.5 * math.tanh(
                        float(row["outcome"].get("realized_pnl", 0.0)) / pnl_scale
                    )
                )
            row_values.append(value)
        dual_baseline = sum(row_values) / len(row_values) if row_values else baseline
        tally: dict[str, dict[str, float]] = {}
        for row in rows:
            if "decisions" in row["outcome"] and int(row["outcome"].get("decisions", 0)) <= 0:
                continue
            activity = 1.0 if row["outcome"].get("useful") else 0.0
            value = activity
            if row["outcome"].get("net_pnl_known"):
                pnl = float(row["outcome"].get("realized_pnl", 0.0))
                profit_score = 0.5 + 0.5 * math.tanh(pnl / pnl_scale)
                value = 0.5 * activity + 0.5 * profit_score
            for prior_id in row.get("priors", ()):
                entry = tally.setdefault(str(prior_id), {"selections": 0.0, "value": 0.0,
                                                         "useful": 0.0, "net_pnl": 0.0})
                entry["selections"] += 1
                entry["value"] += value
                if row["outcome"].get("useful"):
                    entry["useful"] += 1
                if row["outcome"].get("net_pnl_known"):
                    entry["net_pnl"] += float(row["outcome"].get("realized_pnl", 0.0))
        seeds = getattr(self.strategy, "seed_priors", None)
        texts = {prior.prior_id: prior.text for prior in (seeds() if callable(seeds) else ())}
        written = 0
        for prior_id, entry in tally.items():
            rate = entry["value"] / entry["selections"]
            weight = shrunk_weight(rate, dual_baseline, int(entry["selections"]))
            self.memory.save_discovery_prior(
                strategy=DISCOVERY_EVOLUTION_KEY,
                prior_id=prior_id,
                text=texts.get(prior_id, ""),
                weight=weight,
                sample_size=int(entry["selections"]),
                support={
                    "dual_objective_value": round(rate, 4),
                    "effective_activity_rate": round(entry["useful"] / entry["selections"], 4),
                    "net_pnl": round(entry["net_pnl"], 6),
                    "baseline_dual_objective_value": round(dual_baseline, 4),
                    "useful": int(entry["useful"]),
                },
            )
            written += 1
        return written

    def _rewrite_lessons(self, baseline: float) -> tuple[int, int]:
        """State what the buckets actually measured. Templates, not model prose.

        A lesson generated from a template can always be traced back to its sample; a model asked to
        summarize a handful of trades will write a confident generalization out of noise.
        """
        measured = self.measurements()["buckets"]
        active = {item["lesson_id"]: item for item in self.memory.load_discovery_lessons(DISCOVERY_EVOLUTION_KEY)}
        written = 0
        qualifying: set[str] = set()
        for bucket, entry in measured.items():
            samples = int(entry["selections"])
            rate = float(entry["useful_rate"])
            if samples < LESSON_MIN_SAMPLE or abs(rate - baseline) < LESSON_DEVIATION:
                continue
            qualifying.add(bucket)
            direction = "more" if rate > baseline else "less"
            self.memory.save_discovery_lesson(
                strategy=DISCOVERY_EVOLUTION_KEY,
                lesson_id=bucket,
                text=(
                    f"Candidates in {bucket} produced an actionable decision "
                    f"{rate:.0%} of the time against a {baseline:.0%} baseline over {samples} "
                    f"selections, so treat this bucket as {direction} promising than average."
                ),
                bucket=bucket,
                sample_size=samples,
                support={
                    "useful_rate": rate,
                    "baseline_useful_rate": round(baseline, 4),
                    "deviation": round(rate - baseline, 4),
                },
            )
            written += 1
        retired = 0
        for lesson_id in active:
            if lesson_id not in qualifying:
                self.memory.retire_discovery_lesson(
                    strategy=DISCOVERY_EVOLUTION_KEY,
                    lesson_id=lesson_id,
                    reason="bucket no longer deviates from the baseline",
                )
                retired += 1
        return written, retired


class _DiscoveryToolbox:
    """Read-only tools the discovery Agent drives, all through plugin standard interfaces."""

    def __init__(
        self,
        *,
        plugin: PredictionMarketApiPlugin,
        memory: SessionMemory,
        platform: str,
        budget: DiscoveryBudget,
        cross_platform_search: Callable[[str, int], dict[str, Any]] | None,
        measurements: Callable[[], dict[str, Any]] | None = None,
        research: list[Any] | None = None,
    ) -> None:
        self.measurements = measurements
        self.plugin = plugin
        self.memory = memory
        self.platform = platform
        self.budget = budget
        self.cross_platform_search = cross_platform_search
        self._detail_calls = 0
        self._book_calls = 0
        # Topics the round went and fetched for itself, which the selection step must be able to
        # resolve: a market it found and chose is no different from one it was handed.
        self.found: dict[str, Topic] = {}
        # What it read about candidates this round, merged into the record afterwards.
        self.verified: dict[str, dict[str, Any]] = {}
        self.descriptions: dict[str, Any] = {
            "READ_MARKET_PLAYBOOK": {
                "purpose": (
                    "Read one prediction-market guide only when needed: selection, execution, "
                    "resolution, structure, feedback, or field_notes (first-person reports)."
                ),
                "arguments": {"section": "required section name"},
            },
            "TOPIC_DETAIL": {
                "purpose": (
                    "Read one page of a topic's markets, outcomes, resolution data and end time. "
                    "Use next_offset until has_more is false when later submarkets matter."
                ),
                "arguments": {
                    "topic_id": "required string from the candidate list",
                    "offset": "optional non-negative market offset",
                    "limit": "optional page size from 1 to 50",
                },
            },
            "OUTCOME_BOOK": {
                "purpose": (
                    "Read the order book, touch size and nearest depth for a specific outcome. "
                    "A different contract in the same event can have a different book."
                ),
                "arguments": {"market_id": "required string", "outcome_id": "required string"},
            },
            "TOPIC_HISTORY": {
                "purpose": "Recall when this topic was last selected and what came of it.",
                "arguments": {"topic_id": "required string"},
            },
            "RECALL_MEASUREMENTS": {
                "purpose": (
                    "Read the complete measured outcome table; the prompt carries only the "
                    "largest buckets."
                ),
                "arguments": {"prefix": "optional bucket name prefix filter"},
            },
            # The shortlist you were handed is one reading of this venue, taken before you looked
            # at anything. When it is the wrong reading - everything settles too far out, nothing
            # in the category that moved today - go and get a better one now rather than asking for
            # it next round. Anything these return can be selected like any other candidate.
            "VERIFY_TOPICS": {
                "purpose": (
                    "Read event deadline and resolution, list the first open contracts, and sample "
                    "ONE contract's book per named event. A bad sample does NOT reject other "
                    "contracts or outcomes in the event: use TOPIC_DETAIL and OUTCOME_BOOK for "
                    "the actual thesis. Nothing in the "
                    "list arrives priced, because which ones are worth reading is your call: name "
                    "the few you would actually give a slot to. Costs one platform read per "
                    "candidate, inside this round's read allowance."
                ),
                "arguments": {"topic_ids": "required list of candidate ids"},
            },
            "FIND_TOPICS": {
                "purpose": (
                    "Search this platform's own catalogue for markets the shortlist did not "
                    "include. The shortlist is the venue's busiest, which is not the same as what "
                    "is worth looking at now."
                ),
                "arguments": {"query": "required string", "limit": "optional integer"},
            },
            "TOPICS_BY_DEADLINE": {
                "purpose": (
                    "List this platform's markets by when they settle, soonest first, inside a "
                    "window you name. The ordinary listing is ordered by how busy a market is, and "
                    "the busiest are usually the ones settling months out - so this is how what "
                    "resolves soon gets found at all. A platform that cannot answer by date says so."
                ),
                "arguments": {
                    "within_hours": "required number", "limit": "optional integer",
                },
            },
        }
        if cross_platform_search is not None:
            self.descriptions["SEARCH_OTHER_PLATFORMS"] = {
                "purpose": "Find the same question quoted on other registered platforms.",
                "arguments": {"query": "required string", "max_results_per_platform": "optional integer"},
            }
        # Whatever the research plugins contribute - searching the web, reading a page - offered
        # here as well as at the decision stage. "I could not verify the resolution wording" is a
        # statement about what was reachable, and this is what makes it reachable.
        self._research: dict[str, Any] = {}
        for contribution in research or []:
            executor = contribution.create(None)
            for name, description in executor.descriptions.items():
                key = str(name).strip().upper()
                if key in self.descriptions or key in self._research:
                    continue
                self.descriptions[key] = description
                self._research[key] = executor

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        handler = {
            "READ_MARKET_PLAYBOOK": read_market_playbook,
            "TOPIC_DETAIL": self._topic_detail,
            "OUTCOME_BOOK": self._outcome_book,
            "TOPIC_HISTORY": self._topic_history,
            "RECALL_MEASUREMENTS": self._recall_measurements,
            "SEARCH_OTHER_PLATFORMS": self._search,
            "VERIFY_TOPICS": self._verify_topics,
            "FIND_TOPICS": self._find_topics,
            "TOPICS_BY_DEADLINE": self._topics_by_deadline,
        }.get(name.strip().upper())
        if handler is None:
            executor = self._research.get(name.strip().upper())
            if executor is not None:
                return executor.execute(name.strip().upper(), arguments)
            raise ValueError(f"Unknown discovery tool: {name}")
        return handler(arguments)

    def _offer(self, topics: Any) -> list[dict[str, Any]]:
        """Put fetched topics into the round's pool so they can be chosen, and describe them."""
        listed: list[dict[str, Any]] = []
        for topic in topics or ():
            if topic is None:
                continue
            self.found[topic.topic_id] = topic
            listed.append({
                "topic_id": topic.topic_id,
                "title": topic.title,
                "status": topic.status,
                "liquidity_usdt": topic.liquidity_usdt,
                "volume_usdt": topic.volume_usdt,
            })
        return listed

    def _verify_topics(self, arguments: dict[str, Any]) -> dict[str, Any]:
        ids = arguments.get("topic_ids") or []
        if isinstance(ids, str):
            ids = [ids]
        if not isinstance(ids, list) or not ids:
            raise ValueError("VERIFY_TOPICS needs topic_ids")
        answer = self.verify([str(item) for item in ids])
        # Kept so the round's record shows what it was working from, not only what it chose.
        self.verified.update(answer)
        return {"ok": True, "verified": answer}

    def _find_topics(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = str(arguments.get("query", "")).strip()
        if not query:
            raise ValueError("FIND_TOPICS needs a query")
        limit = max(1, min(int(arguments.get("limit", 10) or 10), self.budget.search_result_limit))
        try:
            candidates = self.plugin.search_market_candidates(query, limit)
        except Exception as error:
            return {"ok": False, "error": str(error)[:200]}
        topics = [getattr(candidate, "topic", None) for candidate in candidates]
        return {"ok": True, "query": query, "topics": self._offer(topics)}

    def _topics_by_deadline(self, arguments: dict[str, Any]) -> dict[str, Any]:
        listing = getattr(self.plugin, "list_topics_by_deadline", None)
        if not callable(listing):
            return {"ok": False, "supported": False,
                    "error": "this platform cannot list by settlement time; use FIND_TOPICS"}
        hours = float(arguments.get("within_hours", 0) or 0)
        if hours <= 0:
            raise ValueError("TOPICS_BY_DEADLINE needs within_hours above zero")
        limit = max(1, min(int(arguments.get("limit", 20) or 20), self.budget.search_result_limit))
        now_ms = int(time.time() * 1000)
        try:
            page = listing(offset=0, limit=limit, after_ms=now_ms,
                           before_ms=now_ms + int(hours * 3600 * 1000))
        except Exception as error:
            return {"ok": False, "supported": True, "error": str(error)[:200]}
        return {"ok": True, "supported": True, "within_hours": hours,
                "topics": self._offer(page.topics)}

    def verify(self, topic_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Read the deadline, the resolution and the real spread for the named candidates.

        One tool step, many candidates: what the gates ask about costs a platform read each, and
        making the round spend a model call per candidate to learn them is how a shortlist of
        twenty-four turns into "unverifiable" by construction. Which ones are worth it, and how
        many, is the round's own call - this only does as it is told, within the read allowance.
        """
        verified: dict[str, dict[str, Any]] = {}
        topic_ids = [str(item) for item in topic_ids][: self.budget.detail_read_safety_limit]
        now_ms = int(time.time() * 1000)
        for topic_id in topic_ids:
            # A candidate the venue will not describe is one candidate the agent has to judge
            # without help. It is not a reason to abandon the round - which is exactly what it
            # became when one topic on a shortlist of twenty-four had no order book and the 404
            # took the whole cycle down with it.
            try:
                detail = self.execute("TOPIC_DETAIL", {"topic_id": topic_id})
            except Exception as error:
                verified[topic_id] = {
                    "verified": False, "lookup_error": str(error)[:200]
                }
                continue
            if not detail.get("ok"):
                # The allowance is gone; the rest of what was asked for is honestly unexamined.
                verified[topic_id] = {"verified": False,
                                      "lookup_error": str(detail.get("error", "budget exhausted"))}
                break
            entry: dict[str, Any] = {"verified": True, "resolution": detail.get("resolution")}
            end_time = detail.get("end_time_ms")
            if end_time:
                entry["event_seconds_remaining"] = max(0, (int(end_time) - now_ms) // 1000)
            markets = detail.get("markets") or []
            entry["markets"] = int(detail.get("markets_total", len(markets)))
            entry["markets_open"] = int(detail.get("markets_open", 0))
            entry["market_options"] = [
                {
                    "market_id": market.get("market_id"),
                    "question": market.get("question"),
                    "liquidity_usdt": market.get("liquidity_usdt"),
                    "fees_enabled": market.get("fees_enabled"),
                    "fee_schedule": market.get("fee_schedule"),
                    "outcomes": market.get("outcomes"),
                }
                for market in markets
                if str(market.get("status", "")).upper() == "OPEN"
            ]
            entry["market_options_has_more"] = bool(detail.get("has_more"))
            entry["sampled_market_count"] = 0
            # Only an open market has a book. Asking for one on a closed market is a guaranteed 404
            # that spends the allowance and tells the agent nothing - whereas "nothing here is open"
            # is itself the fact a status gate needs.
            priced = next(
                (market for market in markets if str(market.get("status", "")).upper() == "OPEN"),
                None,
            )
            if priced is None:
                entry["tradeable"] = False
                entry["why_not_priced"] = "no market in this event is open for trading"
            outcomes = (priced.get("outcomes") or []) if priced else []
            if outcomes:
                entry["tradeable"] = True
                entry["sampled_market_count"] = 1
                entry["unverified_open_markets"] = max(0, entry["markets_open"] - 1)
                # Which market the price belongs to matters: in a ladder of deadlines, a spread on
                # "by December" says nothing about "by June".
                entry["priced_market"] = str(priced.get("question", ""))[:160]
                entry["priced_market_id"] = str(priced.get("market_id", ""))
                market_end = priced.get("end_time_ms") or end_time
                if market_end:
                    entry["seconds_remaining"] = max(0, (int(market_end) - now_ms) // 1000)
                entry["priced_outcome"] = outcomes[0].get("name")
                entry["priced_outcome_id"] = outcomes[0].get("outcome_id")
                try:
                    book = self.execute("OUTCOME_BOOK", {
                        "market_id": priced.get("market_id"),
                        "outcome_id": outcomes[0].get("outcome_id"),
                    })
                except Exception as error:
                    book = {"ok": False, "error": str(error)[:200]}
                if book.get("ok"):
                    entry.update({
                        key: book[key] for key in (
                            "best_bid", "best_ask", "best_bid_size", "best_ask_size",
                            "spread", "top_bids", "top_asks",
                        ) if key in book
                    })
                    bid, ask = book.get("best_bid"), book.get("best_ask")
                    if bid and ask:
                        mid = (float(bid) + float(ask)) / 2
                        entry["spread_pct_of_mid"] = (
                            round((float(ask) - float(bid)) / mid * 100, 2) if mid else None
                        )
                    else:
                        # A book with one side has no spread to report, and saying nothing made it
                        # look like a candidate nobody had checked. It is a finding: there is no
                        # price to buy at, or none to sell at.
                        entry["book_one_sided"] = (
                            "nobody is selling" if not ask else "nobody is buying"
                        ) if (bid or ask) else "the book is empty"
                else:
                    entry["book_error"] = book.get("error", "")
            verified[topic_id] = entry
        return verified

    def _topic_detail(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if self._detail_calls >= self.budget.detail_read_safety_limit:
            return {"ok": False, "error": "detail lookup budget exhausted for this cycle"}
        self._detail_calls += 1
        detail = self.plugin.get_topic(str(arguments["topic_id"]))
        # Tradeable markets first, most liquid first, and only then cut to eight. Events that ladder
        # one question across deadlines - "by June", "by September", "by December" - list the
        # earliest first, and the earliest has usually already closed: reading them in the venue's
        # order put dead markets at the front and, past eight, dropped the live ones entirely.
        markets = sorted(
            detail.markets,
            key=lambda market: (
                str(market.status).upper() != "OPEN",
                -float(market.liquidity_usdt or 0.0),
            ),
        )
        offset = max(0, int(arguments.get("offset", 0) or 0))
        limit = max(1, min(50, int(arguments.get("limit", 8) or 8)))
        market_page = markets[offset:offset + limit]
        return {
            "ok": True,
            "end_time_ms": detail.end_time_ms,
            "fee_bps": detail.fee_bps,
            "resolution": detail.resolution,
            "reference_symbol": detail.reference_symbol,
            "markets_total": len(detail.markets),
            "markets_open": sum(1 for market in detail.markets if str(market.status).upper() == "OPEN"),
            "market_offset": offset,
            "has_more": offset + len(market_page) < len(markets),
            "next_offset": offset + len(market_page),
            "markets": [
                {
                    "market_id": market.market_id,
                    "question": market.question[:300],
                    "status": market.status,
                    "liquidity_usdt": market.liquidity_usdt,
                    "end_time_ms": market.end_time_ms,
                    "fees_enabled": market.fees_enabled,
                    "fee_schedule": market.fee_schedule,
                    "outcomes": [
                        {
                            "outcome_id": outcome.outcome_id,
                            "name": outcome.name,
                            "displayed_probability": outcome.displayed_probability,
                        }
                        for outcome in market.outcomes
                    ],
                }
                for market in market_page
            ],
        }

    def _outcome_book(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if self._book_calls >= self.budget.book_read_safety_limit:
            return {"ok": False, "error": "order book budget exhausted for this cycle"}
        self._book_calls += 1
        book = self.plugin.get_order_book(
            str(arguments["market_id"]), str(arguments["outcome_id"])
        )
        bids = sorted(book.bids, key=lambda level: level.price, reverse=True)
        asks = sorted(book.asks, key=lambda level: level.price)
        answer: dict[str, Any] = {
            "ok": True,
            "two_sided": bool(bids and asks),
            "top_bids": [
                {"price": level.price, "quantity": level.quantity} for level in bids[:5]
            ],
            "top_asks": [
                {"price": level.price, "quantity": level.quantity} for level in asks[:5]
            ],
        }
        if bids:
            answer.update(best_bid=bids[0].price, best_bid_size=bids[0].quantity)
        if asks:
            answer.update(best_ask=asks[0].price, best_ask_size=asks[0].quantity)
        if bids and asks:
            answer["spread"] = round(asks[0].price - bids[0].price, 6)
        return answer

    def _topic_history(self, arguments: dict[str, Any]) -> dict[str, Any]:
        topic_id = str(arguments["topic_id"])
        attention = self.memory.topic_attention_history(platform=self.platform).get(topic_id, {})
        return {
            "ok": True,
            "topic_id": topic_id,
            "attention": attention,
            "downstream": self.memory.selection_downstream(
                platform=self.platform,
                market_topic_id=topic_id,
                since_ms=0,
                until_ms=int(time.time() * 1000),
            ),
        }

    def _recall_measurements(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if self.measurements is None:
            return {"ok": False, "error": "no measurements are available"}
        measured = self.measurements()
        prefix = str(arguments.get("prefix", "") or "")
        buckets = measured.get("buckets", {})
        if prefix:
            buckets = {name: value for name, value in buckets.items() if name.startswith(prefix)}
        return {"ok": True, **{k: v for k, v in measured.items() if k != "buckets"}, "buckets": buckets}

    def _search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if self.cross_platform_search is None:
            return {"ok": False, "error": "cross-platform search is not available"}
        maximum = int(arguments.get("max_results_per_platform", 5) or 5)
        return self.cross_platform_search(str(arguments["query"]), max(1, min(maximum, 20)))
