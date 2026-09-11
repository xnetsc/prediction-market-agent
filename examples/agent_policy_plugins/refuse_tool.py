"""Agent-policy plugin refusing operator-named tool calls.

Agent policy sees every tool the model invokes - market API calls, web search, quote lookups - plus
the trade it finally proposes. `context["kind"]` says which: "tool" for a tool call, "trade" for the
proposal. The target must be `agent:actions`; a plugin that invents its own target name loads,
appears enabled, and is silently never consulted.
"""

from dataclasses import dataclass

from prediction_market_agent.plugin_system.config_io import json_file_callbacks
from prediction_market_agent.plugin_system.discovery import (
    PluginConfigField,
    PluginConfiguration,
    PluginInitializationContext,
    PluginSpec,
)
from prediction_market_agent.core.risk import RuleDecision


@dataclass(frozen=True)
class RefuseToolEngine:
    refused: frozenset[str]
    target: str = "agent:actions"

    def evaluate(self, operation, context):
        del context
        if operation.upper() in self.refused:
            return RuleDecision("REJECT", f"{operation} is refused by example agent policy")
        return RuleDecision("ALLOW", f"{operation} is not on the example refusal list")

    def manifest(self):
        return {"target": self.target, "refused_operations": sorted(self.refused)}


def initialize_plugin(context: PluginInitializationContext) -> PluginSpec:
    load, save, delete, storage = json_file_callbacks(
        context.working_directory / "examples" / "plugin_configs" / "refuse_tool.json"
    )
    configuration = PluginConfiguration(
        fields=(
            PluginConfigField("REFUSED_OPERATIONS", "拒绝的工具或动作", "string", "此示例拒绝的工具名或交易动作，使用英文逗号分隔，例如 SEARCH_WEB,BUY。与白名单插件不同，这里是黑名单：没列出的一律放行。", default=""),
        ),
        load_callback=load,
        save_callback=save,
        delete_callback=delete,
        storage=storage,
    )

    def factory(config, services):
        del config, services
        values = configuration.load()["REFUSED_OPERATIONS"]
        return RefuseToolEngine(
            frozenset(item.strip().upper() for item in values.split(",") if item.strip())
        )

    return PluginSpec(
        "agent_policy",
        "refuse_tool",
        "演示 Agent 行为风控目标与标准 RuleDecision 的完整插件。",
        str(context.module_path),
        factory,
        configuration,
        lambda: None,
    )
