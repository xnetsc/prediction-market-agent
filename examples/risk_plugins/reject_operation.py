"""Risk plugin protecting its own named target with operator-configured operations."""

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
class RejectOperationEngine:
    rejected: frozenset[str]
    target: str = "example:operation_policy"

    def evaluate(self, operation, context):
        del context
        if operation.upper() in self.rejected:
            return RuleDecision("REJECT", "Operation rejected by example plugin configuration")
        return RuleDecision("ALLOW", "Operation accepted by example plugin configuration")

    def manifest(self):
        return {"target": self.target, "rejected_operations": sorted(self.rejected)}


def initialize_plugin(context: PluginInitializationContext) -> PluginSpec:
    load, save, delete, storage = json_file_callbacks(
        context.working_directory / "examples" / "plugin_configs" / "reject_operation.json"
    )
    configuration = PluginConfiguration(
        fields=(
            PluginConfigField("REJECTED_OPERATIONS", "拒绝动作", "string", "此示例规则拒绝的动作名，使用英文逗号分隔。", default=""),
        ),
        load_callback=load,
        save_callback=save,
        delete_callback=delete,
        storage=storage,
    )

    def factory(config, services):
        del config, services
        values = configuration.load()["REJECTED_OPERATIONS"]
        return RejectOperationEngine(
            frozenset(item.strip().upper() for item in values.split(",") if item.strip())
        )

    return PluginSpec(
        "risk",
        "reject_operation",
        "演示标准 RuleDecision 与命名保护目标的完整风控插件。",
        str(context.module_path),
        factory,
        configuration,
        lambda: None,
    )
