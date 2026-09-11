from __future__ import annotations

from pathlib import Path

from prediction_market_agent.plugin_system.config_io import json_file_callbacks
from prediction_market_agent.plugin_system.discovery import (
    PluginConfiguration,
    PluginInitializationContext,
    PluginSpec,
)
from prediction_market_agent.plugins._rules import (
    TrustedRuleEngine,
    configured_paths,
    rule_path_field,
    rule_readiness,
)

AGENT_TARGET = "agent:actions"
"""Every tool call the model makes, plus the trade it proposes.

Unlike business risk this runs before the platform is quoted, so a rule here can still reduce a
requested size with ADJUST rather than only refusing it.
"""


def AgentRuleEngine(module_path: Path, index: int) -> TrustedRuleEngine:
    return TrustedRuleEngine(module_path, index, AGENT_TARGET, namespace="agent_rule")


def initialize_plugin(context: PluginInitializationContext) -> PluginSpec:
    load, save, delete, storage = json_file_callbacks(
        context.working_directory / "config" / "plugins" / "agent_policy_custom_rules.json"
    )
    configuration = PluginConfiguration(
        fields=(
            rule_path_field(
                "受信任 Python Agent 行为规则文件路径列表；多个路径使用系统路径分隔符分隔。"
                "规则收到的是工具名或交易动作（BUY、SELL、HOLD 及各研究工具名），"
                "context 的 kind 为 tool 或 trade，protected_target 为 agent:actions。"
                "留空时本插件不加载任何规则，也就不施加任何限制。"
            ),
        ),
        load_callback=load,
        save_callback=save,
        delete_callback=delete,
        storage=storage,
    )

    def factory(config, services):
        del config, services
        return [
            AgentRuleEngine(Path(path), index)
            for index, path in enumerate(configured_paths(configuration))
        ]

    return PluginSpec(
        "agent_policy",
        "custom_rules",
        "用你自己的 Python 规则约束模型发起的工具调用与交易动作，可拒绝、停止或缩减。",
        str(context.module_path),
        factory,
        configuration,
        lambda: None,
        readiness_callback=lambda: rule_readiness(configured_paths(configuration)),
    )
