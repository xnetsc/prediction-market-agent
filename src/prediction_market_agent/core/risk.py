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


class RiskCoordinator:
    def __init__(self) -> None:
        self.engines: dict[str, TargetRuleEngine] = {}

    def register(self, engine: TargetRuleEngine) -> None:
        if engine.target in self.engines:
            raise ValueError(f"Risk engine already registered for {engine.target}")
        self.engines[engine.target] = engine

    def manifests(self) -> dict[str, dict[str, Any]]:
        return {target: engine.manifest() for target, engine in self.engines.items()}

    def evaluate(
        self, target: str, operation: str, context: dict[str, Any]
    ) -> RuleDecision:
        engine = self.engines.get(target)
        decisions = [] if engine is None else [engine.evaluate(operation, context)]
        decisions.extend(
            item.evaluate(operation, {**context, "protected_target": target})
            for name, item in self.engines.items()
            if name.startswith("dynamic:")
        )
        if not decisions:
            return RuleDecision(
                "ALLOW",
                f"No enabled rule plugin applies to target {target}",
            )
        for outcome in ("HALT", "REJECT"):
            match = next((item for item in decisions if item.outcome == outcome), None)
            if match:
                return match
        adjustments = [
            item.adjusted_value
            for item in decisions
            if item.outcome == "ADJUST" and item.adjusted_value is not None
        ]
        if adjustments:
            requested = context.get("requested_value")
            values = adjustments + ([float(requested)] if requested is not None else [])
            return RuleDecision("ADJUST", "Most restrictive adjustment applied", min(values))
        return decisions[0]


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
