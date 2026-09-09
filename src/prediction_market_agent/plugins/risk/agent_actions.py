from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from prediction_market_agent.plugin_system.config_io import json_file_callbacks
from prediction_market_agent.plugin_system.discovery import (
    PluginConfigField,
    PluginConfiguration,
    PluginInitializationContext,
    PluginSpec,
)
from prediction_market_agent.core.risk import RuleDecision


@dataclass(frozen=True)
class AgentActionRuleEngine:
    allowed_tools: tuple[str, ...]
    allowed_trade_actions: tuple[str, ...]
    target: str = "agent:actions"

    def evaluate(self, operation: str, context: dict[str, Any]) -> RuleDecision:
        kind = str(context.get("kind", "tool"))
        allowed = self.allowed_tools if kind == "tool" else self.allowed_trade_actions
        if operation not in allowed:
            return RuleDecision("REJECT", f"{operation} is not on the {kind} allowlist")
        return RuleDecision("ALLOW", f"{operation} is on the {kind} allowlist")

    def manifest(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "allowed_tools": list(self.allowed_tools),
            "allowed_trade_actions": list(self.allowed_trade_actions),
            "policy": "ALLOWLIST",
        }


def _items(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item.strip().upper() for item in value.split(",") if item.strip()))


def initialize_plugin(context: PluginInitializationContext) -> PluginSpec:
    load, save, delete, storage = json_file_callbacks(
        context.working_directory / "config" / "plugins" / "risk_agent_actions.json"
    )
    configuration = PluginConfiguration(
        fields=(
            PluginConfigField("ALLOWED_TOOLS", "Agent 工具白名单", "string", "允许 Agent 调用的工具名，使用英文逗号分隔；未列出的工具会被拒绝。", required=True),
            PluginConfigField("ALLOWED_TRADE_ACTIONS", "交易动作白名单", "string", "允许 Agent 给出的交易动作，使用英文逗号分隔；未列出的动作会被拒绝。", required=True),
        ),
        load_callback=load,
        save_callback=save,
        delete_callback=delete,
        storage=storage,
    )

    def factory(config, services):
        del config, services
        values = configuration.load()
        return AgentActionRuleEngine(
            _items(values["ALLOWED_TOOLS"]),
            _items(values["ALLOWED_TRADE_ACTIONS"]),
        )

    return PluginSpec(
        "risk",
        "agent_actions",
        "对 Agent 可调用工具及可输出交易动作实施白名单控制。",
        str(context.module_path),
        factory,
        configuration,
        lambda: None,
    )
from dataclasses import dataclass
from typing import Any
