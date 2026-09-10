from __future__ import annotations

import logging
import time
from typing import Any

from ..agent.evolution import (
    DECISION_EVOLUTION_KEY,
    LESSON_DEVIATION,
    LESSON_MIN_SAMPLE,
    LearnedLesson,
    LearnedPrior,
    compose_overlay_payload,
    shrunk_weight,
)
from .memory import SessionMemory

LOGGER = logging.getLogger(__name__)

PROBABILITY_BANDS: tuple[tuple[float, float], ...] = (
    (0.0, 0.1),
    (0.1, 0.3),
    (0.3, 0.5),
    (0.5, 0.7),
    (0.7, 0.9),
    (0.9, 1.0),
)


def probability_band(value: float) -> str:
    for low, high in PROBABILITY_BANDS:
        if value < high:
            return f"estimate:{low:g}-{high:g}"
    return f"estimate:{PROBABILITY_BANDS[-1][0]:g}-{PROBABILITY_BANDS[-1][1]:g}"


def _band_midpoint(bucket: str) -> float:
    low, high = bucket.split(":", 1)[1].split("-")
    return (float(low) + float(high)) / 2.0


def _took_the_right_side(action: str, won: bool) -> bool:
    """Whether the position ended up on the winning side of the settled outcome."""
    if action == "BUY":
        return won
    if action == "SELL":
        return not won
    return False


class DecisionEvolution:
    """Measured overlay for the decision strategy, mirroring market discovery.

    Discovery measures whether a slot produced an action. Decisions are measured against settled
    truth: how well the stated probabilities matched what actually happened. The mechanism, the
    sample gates, the shrinkage and the on/off semantics are shared; only the metric differs.
    """

    def __init__(
        self, *, memory: SessionMemory, strategy: Any, evolution_enabled: bool
    ) -> None:
        self.memory = memory
        self.strategy = strategy
        self.evolution_enabled = evolution_enabled

    def _priors(self) -> tuple[LearnedPrior, ...]:
        seeds = getattr(self.strategy, "seed_priors", None)
        base = tuple(seeds()) if callable(seeds) else ()
        if not self.evolution_enabled:
            return base
        stored = {
            item["prior_id"]: item
            for item in self.memory.load_discovery_priors(DECISION_EVOLUTION_KEY)
        }
        return tuple(
            LearnedPrior(
                prior.prior_id,
                prior.text,
                float(stored.get(prior.prior_id, {}).get("weight", prior.weight)),
                int(stored.get(prior.prior_id, {}).get("sample_size", 0)),
            )
            for prior in base
        )

    def _lessons(self) -> tuple[LearnedLesson, ...]:
        if not self.evolution_enabled:
            return ()
        return tuple(
            LearnedLesson(
                lesson_id=item["lesson_id"],
                text=item["text"],
                bucket=item["bucket"],
                sample_size=item["sample_size"],
                support=item["support"],
                recorded_at=item["recorded_at"],
                confirmed_at=item["confirmed_at"],
            )
            for item in self.memory.load_discovery_lessons(DECISION_EVOLUTION_KEY)
        )

    def payload(self, *, now_ms: int | None = None) -> dict[str, Any]:
        """The decision strategy as the Agent sees it: operator text plus the measured overlay."""
        return compose_overlay_payload(
            self.strategy.to_prompt_payload(),
            strategy=self.strategy,
            priors=self._priors(),
            lessons=self._lessons(),
            measurements=self.measurements(),
            evolution_enabled=self.evolution_enabled,
            now_ms=int(time.time() * 1000) if now_ms is None else int(now_ms),
        )

    def measurements(self) -> dict[str, Any]:
        """Calibration by stated-probability band, measured against settled outcomes."""
        rows = self.memory.settled_decisions()
        if not rows:
            return {"settled": 0, "bands": {}, "right_side_rate": 0.0}
        bands: dict[str, dict[str, float]] = {}
        for row in rows:
            bucket = probability_band(row["estimated_probability"])
            entry = bands.setdefault(bucket, {"decisions": 0, "won": 0})
            entry["decisions"] += 1
            if row["won"]:
                entry["won"] += 1
        right_side = sum(
            1 for row in rows if _took_the_right_side(row["action"], row["won"])
        )
        return {
            "settled": len(rows),
            "right_side_rate": round(right_side / len(rows), 4),
            "bands": {
                name: {
                    "decisions": int(entry["decisions"]),
                    "realized_yes_rate": round(entry["won"] / entry["decisions"], 4),
                }
                for name, entry in sorted(bands.items())
            },
        }

    def review(self) -> dict[str, int]:
        """Re-weight cited priors and restate calibration lessons from settled outcomes."""
        rows = self.memory.settled_decisions()
        if not rows:
            return {"settled": 0, "priors": 0, "lessons": 0, "retired": 0}
        baseline = sum(
            1 for row in rows if _took_the_right_side(row["action"], row["won"])
        ) / len(rows)
        priors = self._reweight_priors(rows, baseline)
        written, retired = self._rewrite_lessons()
        return {
            "settled": len(rows),
            "priors": priors,
            "lessons": written,
            "retired": retired,
        }

    def _reweight_priors(self, rows: list[dict[str, Any]], baseline: float) -> int:
        tally: dict[str, dict[str, int]] = {}
        for row in rows:
            for prior_id in row.get("priors", ()):
                entry = tally.setdefault(str(prior_id), {"decisions": 0, "right": 0})
                entry["decisions"] += 1
                if _took_the_right_side(row["action"], row["won"]):
                    entry["right"] += 1
        seeds = getattr(self.strategy, "seed_priors", None)
        texts = {prior.prior_id: prior.text for prior in (seeds() if callable(seeds) else ())}
        for prior_id, entry in tally.items():
            rate = entry["right"] / entry["decisions"]
            self.memory.save_discovery_prior(
                strategy=DECISION_EVOLUTION_KEY,
                prior_id=prior_id,
                text=texts.get(prior_id, ""),
                weight=shrunk_weight(rate, baseline, entry["decisions"]),
                sample_size=entry["decisions"],
                support={
                    "right_side_rate": round(rate, 4),
                    "baseline_right_side_rate": round(baseline, 4),
                    "right": entry["right"],
                },
            )
        return len(tally)

    def _rewrite_lessons(self) -> tuple[int, int]:
        """State measured calibration drift. Templates, not model prose.

        A band is compared against its own midpoint rather than a global baseline: the question is
        whether "70%" actually meant 70%, which is the one thing a forecaster can fix about itself.
        """
        bands = self.measurements()["bands"]
        active = {
            item["lesson_id"]
            for item in self.memory.load_discovery_lessons(DECISION_EVOLUTION_KEY)
        }
        qualifying: set[str] = set()
        written = 0
        for bucket, entry in bands.items():
            samples = int(entry["decisions"])
            realized = float(entry["realized_yes_rate"])
            expected = _band_midpoint(bucket)
            if samples < LESSON_MIN_SAMPLE or abs(realized - expected) < LESSON_DEVIATION:
                continue
            qualifying.add(bucket)
            drift = "overconfident" if realized < expected else "underconfident"
            self.memory.save_discovery_lesson(
                strategy=DECISION_EVOLUTION_KEY,
                lesson_id=bucket,
                text=(
                    f"Outcomes you priced in the {bucket.split(':', 1)[1]} band resolved YES "
                    f"{realized:.0%} of the time across {samples} settled markets, against the "
                    f"{expected:.0%} the band implies, so your estimates there have been "
                    f"{drift}."
                ),
                bucket=bucket,
                sample_size=samples,
                support={
                    "realized_yes_rate": realized,
                    "band_midpoint": expected,
                    "deviation": round(realized - expected, 4),
                },
            )
            written += 1
        retired = 0
        for lesson_id in active - qualifying:
            self.memory.retire_discovery_lesson(
                strategy=DECISION_EVOLUTION_KEY,
                lesson_id=lesson_id,
                reason="band no longer deviates from its implied probability",
            )
            retired += 1
        return written, retired
