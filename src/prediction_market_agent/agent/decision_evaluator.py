from __future__ import annotations

from dataclasses import dataclass, field, replace
import time
from typing import Any, Protocol


DEFAULT_SCREENING_CONFIDENCE = 0.90
MIN_SCREENING_CONFIDENCE = 0.80
EVALUATOR_FAILURE_COOLDOWN_SECONDS = 30


def is_high_confidence(value: float | None, threshold: float = DEFAULT_SCREENING_CONFIDENCE) -> bool:
    """Return whether a typed screening answer clears the current adaptive threshold."""

    effective = max(MIN_SCREENING_CONFIDENCE, float(threshold))
    return value is not None and float(value) >= effective


OUTLIER_REVIEW_MINIMUM = 4
"""Fewer answers than this describe no crowd, so there is nothing for one of them to stand out from."""


class DecisionEvaluatorError(RuntimeError):
    """A typed evaluator could not produce a usable answer."""


@dataclass(frozen=True)
class CandidateAssessment:
    candidate_id: str
    action: str
    quality: float
    confidence: float | None = None
    recurring: float | None = None
    """How likely this is one of a series relisted on a schedule, as the screener read it.

    "Ethereum up or down, 3:30-3:45" is followed by 3:45-4:00 and forty more the same day. Taken as
    separate markets they fill a slate with copies of one question; the round needs to know they
    are one thing, and whether they are is a judgement about the title and the terms, not something
    a string rule can be trusted to find.
    """
    probabilities: dict[str, float] = field(default_factory=dict)
    provider: str = ""
    evaluator_name: str = ""

    def __post_init__(self) -> None:
        if not self.candidate_id:
            raise ValueError("candidate_id is required")
        if self.action not in {"PRIORITIZE", "NEEDS_DATA", "DEFER", "REJECT"}:
            raise ValueError(f"unsupported candidate action: {self.action}")
        if not 0.0 <= float(self.quality) <= 1.0:
            raise ValueError("candidate quality must be in [0, 1]")


@dataclass(frozen=True)
class ContinuationAssessment:
    action: str
    marginal_value: float
    confidence: float | None = None
    probabilities: dict[str, float] = field(default_factory=dict)
    provider: str = ""

    def __post_init__(self) -> None:
        if self.action not in {
            "CONTINUE_DISCOVERY", "PROCESS_FRONTIER", "PAUSE_AND_RESUME", "SOURCE_EXHAUSTED"
        }:
            raise ValueError(f"unsupported continuation action: {self.action}")
        if not 0.0 <= float(self.marginal_value) <= 1.0:
            raise ValueError("marginal value must be in [0, 1]")


class DecisionEvaluator(Protocol):
    """Typed coarse-screening capability, never a trading decision or execution gateway."""

    name: str

    def evaluate_candidates(
        self, state: dict[str, Any], candidates: list[dict[str, Any]]
    ) -> list[CandidateAssessment]: ...

    def assess_continuation(
        self, state: dict[str, Any], frontier: list[dict[str, Any]], page: dict[str, Any]
    ) -> ContinuationAssessment: ...

    def screen_decision(
        self, state: dict[str, Any], candidates: list[dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        """Is this market worth a full decision right now, on the facts already in hand.

        A trade decision is reasoning: the wording, the book, the news and the position, weighed
        together. Whether one is worth paying for is not - it is a comparison between what was
        concluded last time, the condition that conclusion named, and what the numbers say now.
        That is a question with an answer in the facts, which is where this screener is better than
        a reasoning model rather than merely cheaper: it cannot talk itself into a story.

        Answers per candidate: {"examine": bool, "confidence": float, "why": str}. Saying examine
        is always safe; saying skip is a claim that nothing has changed since it was last settled.
        """
        ...

    def screen_outliers(
        self, state: dict[str, Any], assessments: list[dict[str, Any]]
    ) -> dict[str, bool]:
        """Second look at its own first-pass answers: which verdicts stand apart from the batch.

        An absolute floor asks whether the screener is sure in absolute terms. That is the wrong
        question for a screener that is systematically timid: a batch answering 0.1, 0.15, 0.2 and
        then 0.6 is saying something about that 0.6 which no fixed cut-off can hear. Which ones
        those are is a judgement about this batch, so it is put back to the screener rather than
        settled here by a formula - a rule written into this file would be a constant nobody
        measured, applied to every model and every batch alike.
        """
        ...

def _consistent_upwards(
    trusted: dict[str, bool], assessments: list[dict[str, Any]]
) -> dict[str, bool]:
    """Keep the screener's own answers in an order that "stands apart" has to obey.

    Standing above a batch is monotone by construction: if 0.55 is clear of the crowd, 0.60 in the
    same batch is clear of it too. A real screener will still answer otherwise now and then - asked
    here, one called 0.55 separated and 0.60 not - and that is noise, not a finer judgement. So
    nothing about where the line falls is decided here; only that whatever line the screener drew
    applies the same way to everything above it.
    """
    confidences = {
        str(item.get("candidate_id")): item.get("confidence")
        for item in assessments
        if item.get("confidence") is not None
    }
    lowest = min(
        (confidences[key] for key, value in trusted.items() if value and key in confidences),
        default=None,
    )
    if lowest is None:
        return trusted
    return {
        candidate_id: bool(value or confidences.get(candidate_id, float("-inf")) >= lowest)
        for candidate_id, value in trusted.items()
    }


class DecisionEvaluatorPool:
    """Use one enabled evaluator at a time, falling back only after failure."""

    def __init__(
        self, evaluators: list[DecisionEvaluator], unavailable: dict[str, str] | None = None,
        *, order_mode: str = "QUALITY",
    ):
        self.evaluators = list(evaluators)
        self.unavailable = dict(unavailable or {})
        self.errors: dict[str, str] = {}
        self.fallback_errors: dict[str, str] = {}
        self._last_errors: dict[str, str] = {}
        self._cooldown_until: dict[str, float] = {}
        self._quality: dict[str, float] = {}
        self.set_order_mode(order_mode)

    def set_order_mode(self, mode: str) -> None:
        if mode not in {"QUALITY", "CONFIGURED"}:
            raise ValueError("evaluator order mode must be QUALITY or CONFIGURED")
        self.order_mode = mode

    def set_quality(self, scores: dict[str, float]) -> None:
        self._quality = {str(name): float(score) for name, score in scores.items()}

    def _ordered(self) -> list[DecisionEvaluator]:
        if self.order_mode == "CONFIGURED":
            ranked = list(self.evaluators)
        else:
            ranked = sorted(
                self.evaluators,
                key=lambda item: -self._quality.get(item.name, 1.0),
            )
        now = time.monotonic()
        available = [item for item in ranked if now >= self._cooldown_until.get(item.name, 0)]
        if not available and ranked:
            self.errors = {
                item.name: self._last_errors.get(item.name, "evaluator cooling down")
                for item in ranked
            }
        return available

    def _record_failure(self, name: str, error: Exception) -> None:
        message = str(error)[:500]
        self.errors[name] = message
        self.fallback_errors[name] = message
        self._last_errors[name] = message
        self._cooldown_until[name] = time.monotonic() + EVALUATOR_FAILURE_COOLDOWN_SECONDS

    def _succeeded(self, name: str) -> None:
        self.errors = {}
        self._cooldown_until.pop(name, None)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.evaluators)

    @property
    def available(self) -> bool:
        return bool(self.evaluators)

    def evaluate_candidates(
        self, state: dict[str, Any], candidates: list[dict[str, Any]]
    ) -> list[CandidateAssessment]:
        self.errors = {}
        self.fallback_errors = {}
        for evaluator in self._ordered():
            try:
                answers = evaluator.evaluate_candidates(state, candidates)
                expected = {
                    str(item.get("candidate_id") or item.get("topic_id"))
                    for item in candidates
                    if item.get("candidate_id") or item.get("topic_id")
                }
                received = [answer.candidate_id for answer in answers]
                if set(received) != expected or len(received) != len(set(received)):
                    raise DecisionEvaluatorError("evaluator returned incomplete candidate assessments")
                result = [replace(answer, evaluator_name=evaluator.name) for answer in answers]
            except Exception as error:
                self._record_failure(evaluator.name, error)
                continue
            self._succeeded(evaluator.name)
            return result
        return []

    def classify_failure(self, message: str) -> str:
        """Name a client failure the patterns did not recognise, or say unknown.

        Asked at most once per distinct message, and only when the regexes found nothing. Naming
        what a message is - out of quota, signed out, a network fault, a broken request - is
        reading, not reasoning, and getting it wrong costs a provider that is retried every round
        instead of waited out, or waited out when it would have worked.
        """
        self.errors = {}
        self.fallback_errors = {}
        for evaluator in self._ordered():
            classify = getattr(evaluator, "classify_failure", None)
            if not callable(classify):
                continue
            try:
                answer = str(classify(message) or "").strip().lower()
            except Exception as error:
                self._record_failure(evaluator.name, error)
                continue
            if answer:
                self._succeeded(evaluator.name)
                return answer
        return "unknown"

    def screen_decision(
        self, state: dict[str, Any], candidates: list[dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        """Ask one screener; a failed call falls back without silently skipping a market."""
        self.errors = {}
        self.fallback_errors = {}
        for evaluator in self._ordered():
            screen = getattr(evaluator, "screen_decision", None)
            if not callable(screen):
                continue
            try:
                answer = screen(state, candidates)
                expected = {str(item.get("candidate_id")) for item in candidates if item.get("candidate_id")}
                if not isinstance(answer, dict) or set(answer) != expected or not all(
                    isinstance(value, dict) and isinstance(value.get("examine"), bool)
                    for value in answer.values()
                ):
                    raise DecisionEvaluatorError("evaluator returned incomplete decision screening answers")
            except Exception as error:
                self._record_failure(evaluator.name, error)
                continue
            self._succeeded(evaluator.name)
            return {str(key): value for key, value in answer.items() if isinstance(value, dict)}
        return {}

    def screen_outliers(
        self, state: dict[str, Any], assessments: list[dict[str, Any]]
    ) -> dict[str, bool]:
        """Use one evaluator's separation judgement, falling back after a failed call."""
        self.errors = {}
        self.fallback_errors = {}
        if len(assessments) < OUTLIER_REVIEW_MINIMUM:
            return {}
        for evaluator in self._ordered():
            review = getattr(evaluator, "screen_outliers", None)
            if not callable(review):
                continue
            try:
                answer = review(state, assessments)
                expected = {
                    str(item.get("candidate_id")) for item in assessments
                    if item.get("under_review") and item.get("candidate_id")
                }
                if not isinstance(answer, dict) or not set(answer).issubset(expected):
                    raise DecisionEvaluatorError("evaluator returned invalid outlier answers")
            except Exception as error:
                self._record_failure(evaluator.name, error)
                continue
            self._succeeded(evaluator.name)
            return _consistent_upwards(
                {str(key): bool(value) for key, value in answer.items()}, assessments
            )
        return {}

    def assess_continuation(
        self, state: dict[str, Any], frontier: list[dict[str, Any]], page: dict[str, Any]
    ) -> ContinuationAssessment | None:
        self.errors = {}
        self.fallback_errors = {}
        for evaluator in self._ordered():
            try:
                answer = evaluator.assess_continuation(state, frontier, page)
                if not isinstance(answer, ContinuationAssessment):
                    raise DecisionEvaluatorError("evaluator returned no continuation assessment")
            except Exception as error:
                self._record_failure(evaluator.name, error)
                continue
            self._succeeded(evaluator.name)
            return answer
        return None
