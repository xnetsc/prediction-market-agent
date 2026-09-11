from __future__ import annotations

import json
import logging
import time
from typing import Any

from ..agent.decision import DecisionProviderError
from .memory import SessionMemory

LOGGER = logging.getLogger(__name__)


REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "grounded": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
        },
        "consistent": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
        },
        "cost_aware": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
        },
        "overall": {"type": "number", "minimum": 0, "maximum": 1},
        "notes": {"type": "string", "minLength": 1, "maxLength": 400},
    },
    "required": ["grounded", "consistent", "cost_aware", "overall", "notes"],
}

REVIEW_MISSION = (
    "Grade another model's trade decision on the evidence it was given. Do not restate the trade "
    "or propose your own. Score only what can be checked against the supplied context: whether "
    "every number in the rationale appears in the inputs (grounded), whether the rationale and the "
    "chosen action agree (consistent), and whether the decision compared against the executable "
    "side of the book plus fees rather than the midpoint (cost_aware). A confident rationale built "
    "on numbers that are not in the inputs scores low on grounded regardless of how it reads."
)

LIVENESS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
}

LIVENESS_MISSION = (
    "Answer with ok set to true. This is a liveness check for the runtime, not a task."
)

MINIMUM_SAMPLE = 10
"""Observations a component needs before it moves a provider's score off neutral."""

SCORE_FLOOR = 0.5
SCORE_CEILING = 1.5


def _shrink(rate: float, sample: int, *, neutral: float = 1.0) -> float:
    """Pull a component toward neutral until it rests on enough observations."""
    if sample <= 0:
        return neutral
    return (sample * rate + MINIMUM_SAMPLE * neutral) / (sample + MINIMUM_SAMPLE)


class ProviderQuality:
    """Measure how well each decision provider actually performs, and rank them by it.

    Three independent signals, none of which requires trusting a model's opinion of itself:
    whether it returns a usable answer at all, how well its stated probabilities matched settled
    outcomes, and how a different provider grades its reasoning against the inputs it was given.
    """

    def __init__(self, *, memory: SessionMemory, provider: Any) -> None:
        self.memory = memory
        self.provider = provider

    def measure(self) -> dict[str, dict[str, Any]]:
        delivery = self.memory.provider_delivery()
        effort = self.memory.provider_tool_effort()
        peer = self.memory.provider_review_scores()
        settled = self.memory.settled_decisions()

        calibration: dict[str, dict[str, float]] = {}
        for row in settled:
            name = row.get("provider") or ""
            if not name:
                continue
            entry = calibration.setdefault(name, {"decisions": 0.0, "brier": 0.0})
            entry["decisions"] += 1
            # Brier score of the stated probability against what actually settled.
            entry["brier"] += (
                row["estimated_probability"] - (1.0 if row["won"] else 0.0)
            ) ** 2

        names = set(delivery) | set(calibration) | set(peer) | set(effort)
        measured: dict[str, dict[str, Any]] = {}
        for name in sorted(names):
            turns = delivery.get(name, {})
            total = int(turns.get("turns", 0))
            ok_rate = (int(turns.get("ok", 0)) / total) if total else 1.0
            calibrated = calibration.get(name)
            brier = (
                calibrated["brier"] / calibrated["decisions"] if calibrated else None
            )
            steps = effort.get(name, {})
            measured[name] = {
                "turns": total,
                "delivery_rate": round(ok_rate, 4),
                "settled_decisions": int(calibrated["decisions"]) if calibrated else 0,
                "brier_score": None if brier is None else round(brier, 4),
                "peer_reviews": int(peer.get(name, {}).get("reviews", 0)),
                "peer_average": peer.get(name, {}).get("average"),
                "tool_steps_per_decision": (
                    round(steps["steps"] / steps["decisions"], 2)
                    if steps.get("decisions")
                    else None
                ),
            }
        return measured

    def scores(self, measured: dict[str, dict[str, Any]] | None = None) -> dict[str, float]:
        """Combine the measured signals into one bounded ranking weight per provider."""
        readings = self.measure() if measured is None else measured
        result: dict[str, float] = {}
        for name, item in readings.items():
            delivery = _shrink(float(item["delivery_rate"]), int(item["turns"]))
            # A Brier score of 0.25 is what always saying 50% earns, so treat it as neutral.
            if item["brier_score"] is None:
                calibration = 1.0
            else:
                calibration = _shrink(
                    max(0.0, 1.0 + (0.25 - float(item["brier_score"])) * 2.0),
                    int(item["settled_decisions"]),
                )
            if item["peer_average"] is None:
                peer = 1.0
            else:
                peer = _shrink(
                    0.5 + float(item["peer_average"]), int(item["peer_reviews"])
                )
            combined = (delivery + calibration + peer) / 3.0
            result[name] = round(max(SCORE_FLOOR, min(SCORE_CEILING, combined)), 4)
        return result

    def cross_evaluate(self, *, sample: int = 2) -> int:
        """Have one provider grade another's finished decisions, never its own.

        Self-assessment is worth little: a model that reasoned its way into a mistake will grade
        that reasoning highly. Peer review only counts when the grader is a different provider,
        and it is capped per pass because every grade costs a call.
        """
        providers = list(getattr(self.provider, "providers", []) or [])
        if len(providers) < 2:
            return 0
        health = getattr(self.provider, "health", None)
        graded = 0
        for item in self.memory.recent_decisions_for_review(limit=max(1, sample)):
            reviewer = next(
                (
                    candidate
                    for candidate in providers
                    if candidate.name != item["provider"]
                    and (health is None or health.state(candidate.name).available_at(time.time()))
                ),
                None,
            )
            if reviewer is None:
                continue
            context = item["context"]
            request = {
                "decision_under_review": item["decision"],
                "inputs_the_decision_had": {
                    key: context.get(key)
                    for key in ("market", "outcome", "order_book", "seconds_remaining")
                },
            }
            try:
                result = reviewer.run(
                    request,
                    schema=REVIEW_SCHEMA,
                    schema_name="provider_review",
                    mission=REVIEW_MISSION,
                    final_step_name="FINAL_REVIEW",
                )
            except DecisionProviderError as error:
                LOGGER.warning(
                    "peer review by %s failed: %s", reviewer.name, error
                )
                continue
            self.memory.record_provider_review(
                decision_id=item["decision_id"],
                subject_provider=item["provider"],
                reviewer_provider=reviewer.name,
                scores=result.value,
            )
            graded += 1
        return graded

    def probe_recovering(self) -> dict[str, str]:
        """Check providers that have not proved themselves since their last failure.

        This runs away from the decision path on purpose. A refusal can be slow - a client may
        spend half a minute retrying its own transport before reporting it - and a decision should
        never wait on a provider that is only being tested. Entitlement also changes out of band:
        a plan changes, credits are bought, an account is swapped, and none of that announces
        itself, so a provider is retried on a bounded schedule rather than on what it said its
        limit was.
        """
        health = getattr(self.provider, "health", None)
        backends = {item.name: item for item in getattr(self.provider, "providers", []) or []}
        if health is None or not backends:
            return {}
        results: dict[str, str] = {}
        for name in health.due_for_probe(tuple(backends)):
            health.record_probe(name)
            # Both questions this probe asks - is the session still valid, is there quota left -
            # are ones the client can answer for free. Spending a request to discover whether
            # requests are still possible is the expensive way to learn it, and tells you nothing
            # at all once the account is already exhausted.
            entitlement = getattr(backends[name], "entitlement", None)
            answer = entitlement() if callable(entitlement) else None
            if answer is False:
                results[name] = health.record_failure(
                    name, "client reports no usable quota or session"
                )
                continue
            if answer is True:
                health.record_success(name)
                results[name] = "recovered"
                LOGGER.info("decision provider %s reports it can serve requests again", name)
                continue
            started = time.monotonic()
            try:
                backends[name].run(
                    {},
                    schema=LIVENESS_SCHEMA,
                    schema_name="liveness",
                    mission=LIVENESS_MISSION,
                    final_step_name="LIVENESS",
                )
            except DecisionProviderError as error:
                results[name] = health.record_failure(name, str(error))
            except Exception as error:  # a client that cannot even start is still a failure
                results[name] = health.record_failure(name, str(error))
            else:
                health.record_success(name, latency_seconds=time.monotonic() - started)
                results[name] = "recovered"
                LOGGER.info("decision provider %s is answering again", name)
        return results

    def review(self, *, cross_evaluate: bool = False, sample: int = 2) -> dict[str, Any]:
        """Refresh measured quality and publish it as the provider ranking weights."""
        try:
            self.probe_recovering()
        except Exception:
            LOGGER.exception("provider liveness probe failed; keeping the current health view")
        if cross_evaluate:
            try:
                self.cross_evaluate(sample=sample)
            except Exception:
                LOGGER.exception("peer review pass failed; keeping earlier scores")
        measured = self.measure()
        scores = self.scores(measured)
        health = getattr(self.provider, "health", None)
        if health is not None:
            health.set_quality(scores)
        return {"measured": measured, "scores": scores}

    def manifest(self) -> dict[str, Any]:
        health = getattr(self.provider, "health", None)
        return {
            "providers": health.manifest() if health is not None else [],
            "measured": self.measure(),
            "unavailable": dict(getattr(self.provider, "unavailable", {}) or {}),
        }


def summarize(manifest: dict[str, Any]) -> str:
    """One line per provider for logs and status text."""
    return json.dumps(manifest.get("providers", []), ensure_ascii=False, sort_keys=True)
