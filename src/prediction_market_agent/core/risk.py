from __future__ import annotations

import fnmatch
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

class RiskRejected(RuntimeError):
    """A configured risk engine rejected an operation."""


class NetworkGateError(RiskRejected):
    """A network request did not match the configured allowlist."""


@dataclass(frozen=True)
class RuleDecision:
    outcome: str
    reason: str
    adjusted_value: float | None = None


class TargetRuleEngine(Protocol):
    target: str

    def evaluate(self, operation: str, context: dict[str, Any]) -> RuleDecision: ...

    def manifest(self) -> dict[str, Any]: ...


@runtime_checkable
class PortfolioRiskContribution(Protocol):
    """Standard risk-plugin contribution needed to construct trading accounts."""

    def initial_allocations(self, platforms: tuple[str, ...]) -> dict[str, float]: ...
    def create_account_engine(self, platform: str, state: Any) -> TargetRuleEngine: ...
    def create_global_engine(self, states: dict[str, Any]) -> TargetRuleEngine: ...


@dataclass(frozen=True)
class NetworkWriteGate:
    allowed_hosts: frozenset[str]
    allowed_schemes: frozenset[str]
    allowed_methods: frozenset[str]
    allowed_read_paths: frozenset[str]
    allowed_paths_by_method: dict[str, frozenset[str]] = field(default_factory=dict)
    target_name: str = ""

    @property
    def target(self) -> str:
        return self.target_name or "network:" + ",".join(sorted(self.allowed_hosts))

    def evaluate(self, operation: str, context: dict[str, Any]) -> RuleDecision:
        try:
            self.check(operation, str(context.get("url", "")))
        except NetworkGateError as error:
            return RuleDecision("REJECT", str(error))
        return RuleDecision("ALLOW", "Request is on the configured network allowlist")

    def manifest(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "allowed_methods": sorted(self.allowed_methods),
            "allowed_schemes": sorted(self.allowed_schemes),
            "allowed_hosts": sorted(self.allowed_hosts),
            "allowed_read_paths": sorted(self.allowed_read_paths),
            "allowed_paths_by_method": {
                method: sorted(paths)
                for method, paths in sorted(self.allowed_paths_by_method.items())
            },
            "policy": "CONFIGURED_ALLOWLIST",
        }

    def check(self, method: str, url: str) -> None:
        parsed = urllib.parse.urlparse(url)
        normalized_method = method.upper()
        if normalized_method not in self.allowed_methods:
            raise NetworkGateError(f"Method is not allowed by network rules: {normalized_method}")
        if parsed.scheme not in self.allowed_schemes or parsed.hostname not in self.allowed_hosts:
            raise NetworkGateError(f"Host is not allowed: {parsed.scheme}://{parsed.hostname}")
        patterns = self.allowed_paths_by_method.get(
            normalized_method, self.allowed_read_paths
        )
        if not any(fnmatch.fnmatchcase(parsed.path, pattern) for pattern in patterns):
            raise NetworkGateError(
                f"Endpoint is not on the configured path allowlist: {parsed.path}"
            )


_OUTCOMES = frozenset({"ALLOW", "ADJUST", "REJECT", "HALT"})


class RiskCoordinator:
    """Run every enabled filter plugin that applies to an action, in the order they were enabled.

    Both filter categories stack: an operator may enable several business-risk plugins and several
    agent-policy plugins at once, and each enabled plugin gets to see the action. There is no
    priority and no override - the chain is a conjunction, so one refusal is enough to fail the
    action. That is what lets a broad house rule and a narrow per-desk rule coexist without either
    having to know the other exists.

    An engine's ``target`` names what it applies to, and may be a glob: ``market:*`` covers every
    platform, ``market:binance`` covers one. Engines whose target matches nothing that is ever
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


class UnrestrictedExecutionRiskControl:
    """Neutral gateway adapter used when no account-risk plugin is enabled."""

    def __init__(self, platform: str, state: Any):
        self.target = f"account:{platform}"
        self.state = state

    def refresh_halt(self) -> None:
        return None

    def require_risk_increase_allowed(self) -> None:
        return None

    def allowed_buy_notional(
        self,
        requested: float,
        current_position_value: float,
        fee_bps: int,
        token_id: str,
    ) -> float:
        del current_position_value, fee_bps, token_id
        return requested

    def validate_buy_fill(
        self,
        notional: float,
        fee: float,
        existing_position_value: float,
        token_id: str,
    ) -> None:
        del notional, fee, existing_position_value, token_id

    def validate_inbound_transfer(self, amount: float) -> None:
        del amount

    def evaluate(self, operation: str, context: dict[str, Any]) -> RuleDecision:
        del operation, context
        return RuleDecision("ALLOW", "No enabled account-risk plugin applies")

    def manifest(self) -> dict[str, Any]:
        return {"target": self.target, "policy": "NO_ENABLED_ACCOUNT_RISK_PLUGIN"}


class UnrestrictedGlobalRiskControl:
    """Neutral global adapter used when no portfolio-risk plugin is enabled."""

    target = "portfolio:global"

    def refresh_halt(self) -> None:
        return None

    def evaluate(self, operation: str, context: dict[str, Any]) -> RuleDecision:
        del operation, context
        return RuleDecision("ALLOW", "No enabled portfolio-risk plugin applies")

    def manifest(self) -> dict[str, Any]:
        return {"target": self.target, "policy": "NO_ENABLED_PORTFOLIO_RISK_PLUGIN"}
