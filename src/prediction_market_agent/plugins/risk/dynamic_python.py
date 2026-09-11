from __future__ import annotations

import os
import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from prediction_market_agent.plugin_system.config_io import json_file_callbacks
from prediction_market_agent.plugin_system.discovery import (
    PluginConfigField,
    PluginReadiness,
    PluginConfiguration,
    PluginInitializationContext,
    PluginSpec,
)
from prediction_market_agent.core.risk import RuleDecision


@dataclass
class DynamicPythonRuleEngine:
    module_path: Path
    index: int
    _evaluate_function: Any = None

    def __post_init__(self) -> None:
        path = self.module_path if self.module_path.is_absolute() else Path.cwd() / self.module_path
        path = path.resolve()
        if path.suffix.lower() != ".py" or not path.is_file():
            raise ValueError(f"Dynamic risk rule must be an existing .py file: {path}")
        spec = importlib.util.spec_from_file_location(f"prediction_risk_rule_{self.index}", path)
        if spec is None or spec.loader is None:
            raise ValueError(f"Unable to load dynamic risk rule: {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        function = getattr(module, "evaluate", None)
        if not callable(function):
            raise ValueError(f"Dynamic risk rule must export evaluate(operation, context): {path}")
        self.module_path = path
        self._evaluate_function = function

    @property
    def target(self) -> str:
        return f"dynamic:{self.index}:{self.module_path.name}"

    def evaluate(self, operation: str, context: dict[str, Any]) -> RuleDecision:
        try:
            value = self._evaluate_function(operation, dict(context))
            if isinstance(value, RuleDecision):
                decision = value
            elif isinstance(value, dict):
                adjusted = value.get("adjusted_value")
                decision = RuleDecision(
                    str(value["outcome"]).upper(),
                    str(value.get("reason", "dynamic Python risk rule")),
                    None if adjusted is None else float(adjusted),
                )
            else:
                raise TypeError("evaluate() must return RuleDecision or dict")
            outcome = decision.outcome.upper()
        except Exception as error:
            return RuleDecision("REJECT", f"Dynamic Python risk rule failed closed: {error}")
        if outcome not in {"ALLOW", "ADJUST", "REJECT", "HALT"}:
            return RuleDecision("REJECT", f"Risk filter returned invalid outcome: {outcome}")
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


def initialize_plugin(context: PluginInitializationContext) -> PluginSpec:
    load, save, delete, storage = json_file_callbacks(
        context.working_directory / "config" / "plugins" / "risk_dynamic_python.json"
    )
    configuration = PluginConfiguration(
        fields=(
            PluginConfigField("MODULE_PATHS", "Python 规则文件", "string", "受信任 Python 风控规则文件路径列表；多个路径使用系统路径分隔符分隔。留空时本插件不加载任何规则，也就不施加任何限制。", default=""),
        ),
        load_callback=load,
        save_callback=save,
        delete_callback=delete,
        storage=storage,
    )

    def rule_paths() -> list[str]:
        configured = str(configuration.load().get("MODULE_PATHS", "") or "")
        return [item.strip() for item in configured.split(os.pathsep) if item.strip()]

    def factory(config, services):
        del config, services
        return [DynamicPythonRuleEngine(Path(path), index) for index, path in enumerate(rule_paths())]

    def readiness() -> PluginReadiness:
        """Report what this plugin will actually enforce, not merely that it can load.

        A risk control that is enabled but holds no rules imposes nothing. Reporting that as ready
        would put a green badge next to a check that is not being made, which on a trading robot is
        the most misleading state available.
        """
        paths = rule_paths()
        if not paths:
            return PluginReadiness(
                False, ("未指定规则文件，本插件当前不施加任何限制；填写规则文件路径或停用它",)
            )
        missing = tuple(
            f"规则文件不存在或不是 .py：{path}"
            for path in paths
            if not (Path(path) if Path(path).is_absolute() else Path.cwd() / path)
            .resolve()
            .is_file()
            or not path.lower().endswith(".py")
        )
        return PluginReadiness(False, missing) if missing else PluginReadiness(True)

    return PluginSpec(
        "risk",
        "dynamic_python",
        "加载受信任的 Python 风控规则，可针对任意受保护目标拒绝、停止或缩减动作。",
        str(context.module_path),
        factory,
        configuration,
        lambda: None,
        readiness_callback=readiness,
    )
