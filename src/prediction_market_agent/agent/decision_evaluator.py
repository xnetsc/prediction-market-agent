from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


DEFAULT_SCREENING_CONFIDENCE = 0.90
MIN_SCREENING_CONFIDENCE = 0.80


def is_high_confidence(value: float | None, threshold: float = DEFAULT_SCREENING_CONFIDENCE) -> bool:
    """Return whether a typed screening answer clears the current adaptive threshold."""

    effective = max(MIN_SCREENING_CONFIDENCE, float(threshold))
    return value is not None and float(value) >= effective


class DecisionEvaluatorError(RuntimeError):
    """A typed evaluator could not produce a usable answer."""


@dataclass(frozen=True)
class CandidateAssessment:
    candidate_id: str
    action: str
    quality: float
    confidence: float | None = None
    probabilities: dict[str, float] = field(default_factory=dict)
    provider: str = ""

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

class DecisionEvaluatorPool:
    """Run all configured evaluators; failures are errors, never implicit agreement."""

    def __init__(self, evaluators: list[DecisionEvaluator], unavailable: dict[str, str] | None = None):
        self.evaluators = list(evaluators)
        self.unavailable = dict(unavailable or {})
        self.errors: dict[str, str] = {}

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.evaluators)

    @property
    def available(self) -> bool:
        return bool(self.evaluators)

    @staticmethod
    def _confidence(items: list[Any]) -> float | None:
        values = [float(item.confidence) for item in items if item.confidence is not None]
        return sum(values) / len(values) if values else None

    def evaluate_candidates(
        self, state: dict[str, Any], candidates: list[dict[str, Any]]
    ) -> list[CandidateAssessment]:
        grouped: dict[str, list[CandidateAssessment]] = {}
        self.errors = {}
        for evaluator in self.evaluators:
            try:
                answers = evaluator.evaluate_candidates(state, candidates)
            except Exception as error:
                self.errors[evaluator.name] = str(error)[:500]
                continue
            for answer in answers:
                grouped.setdefault(answer.candidate_id, []).append(answer)
        priority = {"PRIORITIZE": 3, "NEEDS_DATA": 2, "DEFER": 1, "REJECT": 0}
        return [
            CandidateAssessment(
                candidate_id=candidate_id,
                action=max(answers, key=lambda item: priority[item.action]).action,
                quality=sum(item.quality for item in answers) / len(answers),
                confidence=self._confidence(answers),
                provider="+".join(item.provider or "unknown" for item in answers),
            )
            for candidate_id, answers in grouped.items()
        ]

    def assess_continuation(
        self, state: dict[str, Any], frontier: list[dict[str, Any]], page: dict[str, Any]
    ) -> ContinuationAssessment | None:
        answers: list[ContinuationAssessment] = []
        self.errors = {}
        for evaluator in self.evaluators:
            try:
                answers.append(evaluator.assess_continuation(state, frontier, page))
            except Exception as error:
                self.errors[evaluator.name] = str(error)[:500]
        if not answers:
            return None
        order = {"CONTINUE_DISCOVERY": 3, "PROCESS_FRONTIER": 2,
                 "PAUSE_AND_RESUME": 1, "SOURCE_EXHAUSTED": 0}
        selected = max(answers, key=lambda item: order[item.action])
        return ContinuationAssessment(
            action=selected.action,
            marginal_value=sum(item.marginal_value for item in answers) / len(answers),
            confidence=self._confidence(answers),
            provider="+".join(item.provider or "unknown" for item in answers),
        )
