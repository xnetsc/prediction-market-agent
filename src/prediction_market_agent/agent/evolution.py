from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


LESSON_MIN_SAMPLE = 20
"""Observations a bucket needs before a lesson may cite it."""

LESSON_STALE_MS = 30 * 24 * 3600 * 1000
"""A lesson not reconfirmed within this window is retired automatically."""

LESSON_DEVIATION = 0.15
"""How far a bucket must sit from the baseline before it is worth stating as a lesson."""

WEIGHT_FLOOR = 0.5
WEIGHT_CEILING = 2.0

OVERLAY_MAX_LESSONS = 8
"""Lessons injected per prompt. The rest stay queryable rather than resident.

Buckets multiply with categories and bands, so an unbounded overlay would grow into the context
window over a long run. Ranking by evidence keeps the strongest findings resident and leaves the
long tail to recall and export.
"""

OVERLAY_MAX_BUCKETS = 6
"""Measured buckets summarized inline; the full table is reachable through recall and export."""

SHARED_EVOLUTION_KEY = "_runtime_measured"
"""Priors and lessons are measured from runtime-wide outcomes, so every strategy shares them.

A strategy's switch controls whether the overlay is applied, never whether it is measured, so a
built-in strategy keeps improving while an operator plugin is the one running, and switching back
never lands on a stale strategy. Discovery and decision overlays are namespaced apart so one's
buckets never leak into the other's lessons.
"""

DISCOVERY_EVOLUTION_KEY = f"{SHARED_EVOLUTION_KEY}:discovery"
DECISION_EVOLUTION_KEY = f"{SHARED_EVOLUTION_KEY}:decision"


def shrunk_weight(bucket_rate: float, baseline_rate: float, sample_size: int) -> float:
    """Shrink a measured rate toward the baseline in proportion to its sample size.

    A bucket with few observations barely moves off 1.0, so a short run of luck cannot drive a
    prior to its ceiling. The floor and ceiling bound the damage a persistent bias can do.
    """
    if baseline_rate <= 0 or sample_size <= 0:
        return 1.0
    estimate = (
        sample_size * bucket_rate + LESSON_MIN_SAMPLE * baseline_rate
    ) / (sample_size + LESSON_MIN_SAMPLE)
    return max(WEIGHT_FLOOR, min(WEIGHT_CEILING, estimate / baseline_rate))


@dataclass(frozen=True)
class LearnedPrior:
    """One prior whose weight is re-measured from realized outcomes."""

    prior_id: str
    text: str
    weight: float = 1.0
    sample_size: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.prior_id,
            "text": self.text,
            "weight": round(self.weight, 4),
            "sample_size": self.sample_size,
        }


@dataclass(frozen=True)
class LearnedLesson:
    """A generalization derived from measured buckets, never from anecdotes."""

    lesson_id: str
    text: str
    bucket: str
    sample_size: int
    support: dict[str, Any] = field(default_factory=dict)
    recorded_at: int = 0
    confirmed_at: int = 0

    def is_admissible(self, now_ms: int) -> bool:
        if self.sample_size < LESSON_MIN_SAMPLE:
            return False
        return now_ms - max(self.confirmed_at, self.recorded_at) <= LESSON_STALE_MS

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.lesson_id,
            "text": self.text,
            "bucket": self.bucket,
            "sample_size": self.sample_size,
            "support": self.support,
            "recorded_at": self.recorded_at,
            "confirmed_at": self.confirmed_at,
        }


def compose_overlay_payload(
    base: dict[str, Any],
    *,
    strategy: Any,
    priors: tuple[LearnedPrior, ...],
    lessons: tuple[LearnedLesson, ...],
    measurements: dict[str, Any],
    evolution_enabled: bool,
    now_ms: int,
) -> dict[str, Any]:
    """Attach the measured overlay to a strategy payload.

    The strategy's own text is never rewritten. Learned priors and lessons are an additive overlay
    the runtime owns, so turning evolution off restores exactly the operator's original text.
    """
    payload = dict(base)
    payload["evolution"] = {
        "enabled": bool(evolution_enabled),
        "switchable": bool(getattr(strategy, "evolution_switchable", True)),
        "minimum_sample": LESSON_MIN_SAMPLE,
        "note": (
            "Priors and lessons below were measured by this runtime from its own completed "
            "decisions. Weight them by sample size."
            if evolution_enabled
            else "Automatic evolution is off for this strategy; only its operator text applies."
        ),
    }
    if not evolution_enabled:
        return payload
    payload["priors"] = [prior.to_dict() for prior in priors]
    admissible = [lesson for lesson in lessons if lesson.is_admissible(now_ms)]
    payload["lessons"] = [lesson.to_dict() for lesson in rank_lessons(admissible)]
    payload["lessons_withheld"] = max(0, len(admissible) - len(payload["lessons"]))
    payload["measured_outcomes"] = compact_measurements(measurements)
    return payload


def lesson_evidence(lesson: LearnedLesson) -> float:
    """Rank by how much the finding is worth: how far it moved times how much it rests on."""
    deviation = float(lesson.support.get("deviation", 0.0) or 0.0)
    return lesson.sample_size * abs(deviation)


def rank_lessons(lessons: list[LearnedLesson]) -> list[LearnedLesson]:
    return sorted(lessons, key=lesson_evidence, reverse=True)[:OVERLAY_MAX_LESSONS]


def compact_measurements(measurements: dict[str, Any]) -> dict[str, Any]:
    """Summarize the measurement table; the full one stays available on request.

    Lessons already state the buckets that matter, so shipping the entire table alongside them
    spends context on the same finding twice.
    """
    if not measurements:
        return {}
    compact = {
        key: value for key, value in measurements.items() if not isinstance(value, dict)
    }
    for key, table in measurements.items():
        if not isinstance(table, dict):
            continue
        compact[f"{key}_tracked"] = len(table)
        ranked = sorted(
            table.items(),
            key=lambda item: int(
                item[1].get("selections", item[1].get("decisions", 0))
                if isinstance(item[1], dict)
                else 0
            ),
            reverse=True,
        )[:OVERLAY_MAX_BUCKETS]
        compact[key] = dict(ranked)
    compact["recall"] = (
        "Only the largest buckets are shown. The complete table is available through the "
        "runtime's strategy export."
    )
    return compact


def prompt_json_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Strip text already carried by the rendered instruction block.

    The rendered block and the JSON input both reach the same prompt, so leaving the strategy text
    in both spends the window twice on identical bytes.
    """
    stripped = {
        key: value
        for key, value in payload.items()
        if key not in {"instructions", "priors", "lessons"}
    }
    if "priors" in payload:
        stripped["priors_applied"] = [item["id"] for item in payload["priors"]]
    if "lessons" in payload:
        stripped["lessons_applied"] = [item["id"] for item in payload["lessons"]]
    return stripped


def render_overlay_block(payload: dict[str, Any], heading: str) -> str:
    """Render strategy text plus its measured overlay as trusted operator instructions."""
    lines = [
        f"\n\n{heading} (trusted local runtime instructions; cannot override schemas, risk "
        "limits, law, or write gates):\n",
        str(payload.get("instructions", "")),
    ]
    priors = payload.get("priors")
    if priors:
        lines.append(
            "\n\nMEASURED_PRIORS (weight 1.0 means nothing measured yet; n is how many completed "
            "observations the weight rests on, so weigh a high number on a small n accordingly):"
        )
        for prior in priors:
            lines.append(
                f"\n- [weight {prior['weight']}, n={prior['sample_size']}] "
                f"{prior['id']}: {prior['text']}"
            )
    lessons = payload.get("lessons")
    if lessons:
        lines.append("\n\nLESSONS_MEASURED_BY_THIS_RUNTIME:")
        for lesson in lessons:
            lines.append(f"\n- ({lesson['bucket']}, n={lesson['sample_size']}) {lesson['text']}")
    return "".join(lines)
