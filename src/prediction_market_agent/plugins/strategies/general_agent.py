from pathlib import Path

from prediction_market_agent.plugin_system.config_io import json_file_callbacks
from prediction_market_agent.plugin_system.discovery import (
    PluginConfigField,
    PluginConfiguration,
    PluginInitializationContext,
    PluginSpec,
)
from prediction_market_agent.agent.strategy import DecisionStrategyPlugin


def _csv(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item.strip().upper() for item in value.split(",") if item.strip()))


def initialize_plugin(context: PluginInitializationContext) -> PluginSpec:
    load, save, delete, storage = json_file_callbacks(
        context.working_directory / "config" / "plugins" / "strategy_general_agent.json"
    )
    configuration = PluginConfiguration(
        fields=(
            PluginConfigField(
                "STRATEGY_FILE", "策略文本路径", "string",
                "包含系统决策指引的 UTF-8 .md 或 .txt 文件路径；相对路径按机器人工作目录解析。",
                required=True,
            ),
            PluginConfigField("TOPIC_STATUSES", "主题状态", "string", "允许进入本策略的标准化主题状态，英文逗号分隔。", required=True),
            PluginConfigField("MARKET_STATUSES", "市场状态", "string", "允许进入本策略的标准化市场状态，英文逗号分隔。", required=True),
            PluginConfigField("OUTCOME_NAMES", "Outcome 名称", "string", "本策略要求 Agent 分析的标准化 outcome 名称，英文逗号分隔。", required=True),
            PluginConfigField("MIN_TOPIC_LIQUIDITY", "最低主题流动性", "number", "本策略在调用 Agent 前使用的最低标准化流动性条件。", required=True),
        ),
        load_callback=load,
        save_callback=save,
        delete_callback=delete,
        storage=storage,
    )

    def factory(config):
        del config
        values = configuration.load()
        return DecisionStrategyPlugin.load(
            Path(values["STRATEGY_FILE"]),
            topic_statuses=_csv(values["TOPIC_STATUSES"]),
            market_statuses=_csv(values["MARKET_STATUSES"]),
            outcome_names=_csv(values["OUTCOME_NAMES"]),
            minimum_topic_liquidity=float(values["MIN_TOPIC_LIQUIDITY"]),
        )

    return PluginSpec(
        "decision_strategy",
        "general_agent",
        "从插件私有配置指定的文本文件加载通用研究型预测市场策略。",
        str(context.module_path),
        factory,
        configuration,
        lambda: None,
    )
