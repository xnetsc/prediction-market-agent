"""Loading trusted operator Python as a filter engine, shared by both filter categories.

The two categories ask different questions but load rules the same way, and an operator who wants
arbitrary Python should be able to write it against either. The only difference is the target the
resulting engine declares, so that is the one thing this takes as an argument.
"""
from __future__ import annotations

import importlib.util
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from prediction_market_agent.core.risk import RuleDecision
from prediction_market_agent.plugin_system.discovery import (
    PluginConfigField,
    PluginConfiguration,
    PluginReadiness,
)

VALID_OUTCOMES = frozenset({"ALLOW", "ADJUST", "REJECT", "HALT"})


@dataclass
class TrustedRuleEngine:
    """One operator-supplied `.py` file acting as a filter on a named target."""

    module_path: Path
    index: int
    rule_target: str
    namespace: str = "rule"
    _evaluate_function: Any = None

    def __post_init__(self) -> None:
        path = self.module_path if self.module_path.is_absolute() else Path.cwd() / self.module_path
        path = path.resolve()
        if path.suffix.lower() != ".py" or not path.is_file():
            raise ValueError(f"A custom rule must be an existing .py file: {path}")
        spec = importlib.util.spec_from_file_location(
            f"prediction_{self.namespace}_{self.index}", path
        )
        if spec is None or spec.loader is None:
            raise ValueError(f"Unable to load custom rule: {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        function = getattr(module, "evaluate", None)
        if not callable(function):
            raise ValueError(f"A custom rule must export evaluate(operation, context): {path}")
        self.module_path = path
        self._evaluate_function = function

    @property
    def target(self) -> str:
        return self.rule_target

    def evaluate(self, operation: str, context: dict[str, Any]) -> RuleDecision:
        try:
            value = self._evaluate_function(operation, dict(context))
            if isinstance(value, RuleDecision):
                decision = value
            elif isinstance(value, dict):
                adjusted = value.get("adjusted_value")
                decision = RuleDecision(
                    str(value["outcome"]).upper(),
                    str(value.get("reason", "custom rule")),
                    None if adjusted is None else float(adjusted),
                )
            else:
                raise TypeError("evaluate() must return RuleDecision or dict")
            outcome = decision.outcome.upper()
        except Exception as error:
            return RuleDecision("REJECT", f"Custom rule failed closed: {error}")
        if outcome not in VALID_OUTCOMES:
            return RuleDecision("REJECT", f"Custom rule returned invalid outcome: {outcome}")
        return RuleDecision(outcome, decision.reason, decision.adjusted_value)

    def manifest(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "python_module": str(self.module_path),
            "entrypoint": "evaluate(operation, context)",
            "failure_policy": "FAIL_CLOSED",
            "authority": "MAY_ONLY_RESTRICT_OR_REDUCE",
            "trust_boundary": "TRUSTED_IN_PROCESS_PYTHON",
        }


def rule_path_field(description: str) -> PluginConfigField:
    return PluginConfigField("MODULE_PATHS", "Python 规则文件", "string", description, default="")


def configured_paths(configuration: PluginConfiguration) -> list[str]:
    configured = str(configuration.load().get("MODULE_PATHS", "") or "")
    return [item.strip() for item in configured.split(os.pathsep) if item.strip()]


def rule_readiness(paths: list[str]) -> PluginReadiness:
    """Report what this plugin will actually enforce, not merely that it can load.

    A filter that is enabled but holds no rules imposes nothing. Reporting that as ready would put
    a green badge next to a check that is not being made, which on a trading robot is the most
    misleading state available.
    """
    if not paths:
        return PluginReadiness(
            False, ("未指定规则文件，本插件当前不施加任何限制；填写规则文件路径或停用它",)
        )
    missing = tuple(
        f"规则文件不存在或不是 .py：{path}"
        for path in paths
        if not (Path(path) if Path(path).is_absolute() else Path.cwd() / path).resolve().is_file()
        or not path.lower().endswith(".py")
    )
    return PluginReadiness(False, missing) if missing else PluginReadiness(True)
