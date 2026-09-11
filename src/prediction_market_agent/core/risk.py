from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

@dataclass(frozen=True)
class RuleDecision:
    outcome: str
    reason: str
    adjusted_value: float | None = None


class TargetRuleEngine(Protocol):
    target: str

    def evaluate(self, operation: str, context: dict[str, Any]) -> RuleDecision: ...

    def manifest(self) -> dict[str, Any]: ...


_OUTCOMES = frozenset({"ALLOW", "ADJUST", "REJECT", "HALT"})


class RiskCoordinator:
    """Run every enabled filter plugin that applies to an action, in the order they were enabled.

    Both filter categories stack: an operator may enable several business-risk plugins and several
    agent-policy plugins at once, and each enabled plugin gets to see the action. There is no
    priority and no override - the chain is a conjunction, so one refusal is enough to fail the
    action. That is what lets a broad house rule and a narrow per-desk rule coexist without either
    having to know the other exists.

    An engine's ``target`` names what it applies to, and may be a glob: ``market:*`` covers every
    platform, ``market:<platform>`` covers one. Engines whose target matches nothing that is ever
    dispatched still register, so they appear in the manifest the dashboard renders.
    """

    def __init__(self) -> None:
        self.engines: list[TargetRuleEngine] = []

    def register(self, engine: TargetRuleEngine) -> None:
        """Append an engine to the chain.

        Duplicate targets are expected rather than rejected: two plugins of the same category
        legitimately guard the same actions.
        """
        self.engines.append(engine)

    def chain(self, target: str) -> list[TargetRuleEngine]:
        """Engines that apply to this target, in enablement order."""
        return [
            engine
            for engine in self.engines
            if engine.target == target or fnmatch.fnmatchcase(target, engine.target)
        ]

    def manifests(self) -> dict[str, dict[str, Any]]:
        """Manifests keyed by target, suffixed when several engines share one target."""
        grouped: dict[str, list[TargetRuleEngine]] = {}
        for engine in self.engines:
            grouped.setdefault(engine.target, []).append(engine)
        manifests: dict[str, dict[str, Any]] = {}
        for target, engines in grouped.items():
            for index, engine in enumerate(engines):
                key = target if len(engines) == 1 else f"{target}#{index + 1}"
                manifests[key] = engine.manifest()
        return manifests

    def evaluate(
        self, target: str, operation: str, context: dict[str, Any]
    ) -> RuleDecision:
        chain = self.chain(target)
        if not chain:
            return RuleDecision(
                "ALLOW",
                f"No enabled filter plugin applies to target {target}",
            )
        adjustments: list[float] = []
        for engine in chain:
            try:
                decision = engine.evaluate(operation, {**context, "protected_target": target})
                outcome = str(decision.outcome).upper()
                if outcome not in _OUTCOMES:
                    raise ValueError(f"returned an unknown outcome {decision.outcome!r}")
            except Exception as error:
                # A filter that crashes has not allowed anything - it failed to answer. Letting the
                # action through because the check broke is the one failure mode that turns a
                # missing guard into real money moving, so a raising engine refuses.
                return RuleDecision(
                    "REJECT",
                    f"Filter plugin {engine.target} failed closed on {operation}: {error}",
                )
            decision = RuleDecision(outcome, decision.reason, decision.adjusted_value)
            if outcome in ("HALT", "REJECT"):
                # First refusal ends it. Every caller treats HALT and REJECT the same way, and
                # continuing would run later plugins - including operator-supplied Python, with
                # whatever side effects it has - for a verdict that is already settled.
                return decision
            if outcome == "ADJUST":
                if decision.adjusted_value is None:
                    # "Reduce it" without saying to what is not an answer, and treating it as
                    # permission would pass the action at full size.
                    return RuleDecision(
                        "REJECT",
                        f"Filter plugin {engine.target} asked to adjust {operation} "
                        f"without an adjusted_value: {decision.reason}",
                    )
                adjustments.append(decision.adjusted_value)
        if adjustments:
            requested = context.get("requested_value")
            values = adjustments + ([float(requested)] if requested is not None else [])
            return RuleDecision("ADJUST", "Most restrictive adjustment applied", min(values))
        return RuleDecision(
            "ALLOW", f"{len(chain)} enabled filter plugin(s) allowed {operation}"
        )


