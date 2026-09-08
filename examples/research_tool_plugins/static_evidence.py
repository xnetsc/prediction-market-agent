"""Research-tool plugin contributing one deterministic Agent tool."""

from prediction_paper_bot.plugin_config_io import json_file_callbacks
from prediction_paper_bot.plugins.discovery import (
    PluginConfigField,
    PluginConfiguration,
    PluginInitializationContext,
    PluginSpec,
)


class StaticEvidenceExecutor:
    descriptions = {
        "READ_STATIC_EVIDENCE": {
            "purpose": "Return operator-configured evidence for SDK testing.",
            "arguments": {},
        }
    }

    def __init__(self, evidence: str, platform: str):
        self.evidence = evidence
        self.platform = platform

    def execute(self, name, arguments):
        del arguments
        if name != "READ_STATIC_EVIDENCE":
            raise ValueError(f"Unknown tool: {name}")
        return {"platform": self.platform, "evidence": self.evidence}


class StaticEvidenceContribution:
    descriptions = StaticEvidenceExecutor.descriptions

    def __init__(self, evidence: str):
        self.evidence = evidence

    def create(self, context):
        return StaticEvidenceExecutor(self.evidence, context.platform)


def initialize_plugin(context: PluginInitializationContext) -> PluginSpec:
    load, save, storage = json_file_callbacks(
        context.working_directory / "examples" / "plugin_configs" / "static_evidence.json"
    )
    configuration = PluginConfiguration(
        fields=(
            PluginConfigField("EVIDENCE", "示例证据", "string", "READ_STATIC_EVIDENCE 返回给 Agent 的示例文本。", default="No external evidence configured."),
        ),
        load_callback=load,
        save_callback=save,
        storage=storage,
    )

    def factory(config):
        del config
        return StaticEvidenceContribution(configuration.load()["EVIDENCE"])

    return PluginSpec(
        "research_tool",
        "static_evidence",
        "提供一个动态加入 Agent 控制 schema 的只读工具示例。",
        str(context.module_path),
        factory,
        configuration,
        lambda: None,
    )
