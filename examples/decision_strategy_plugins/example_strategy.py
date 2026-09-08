"""Decision-strategy plugin whose instructions and discovery policy are private config."""

from pathlib import Path

from prediction_paper_bot.plugin_config_io import json_file_callbacks
from prediction_paper_bot.plugins.discovery import (
    PluginConfigField,
    PluginConfiguration,
    PluginInitializationContext,
    PluginSpec,
)
from prediction_paper_bot.strategy_plugin import DecisionStrategyPlugin


def _csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip().upper() for item in value.split(",") if item.strip())


def initialize_plugin(context: PluginInitializationContext) -> PluginSpec:
    load, save, storage = json_file_callbacks(
        context.working_directory / "examples" / "plugin_configs" / "example_strategy.json"
    )
    configuration = PluginConfiguration(
        fields=(
            PluginConfigField("STRATEGY_FILE", "策略文件", "string", "写入 Agent 系统上下文的 UTF-8 策略文本路径。", required=True),
            PluginConfigField("TOPIC_STATUSES", "主题状态", "string", "允许扫描的标准化主题状态，逗号分隔。", required=True),
            PluginConfigField("MARKET_STATUSES", "市场状态", "string", "允许扫描的标准化市场状态，逗号分隔。", required=True),
            PluginConfigField("OUTCOME_NAMES", "Outcome 名称", "string", "允许分析的标准化 outcome 名称，逗号分隔。", required=True),
            PluginConfigField("MIN_TOPIC_LIQUIDITY", "最低流动性", "number", "该示例策略自己的主题流动性下限。", default=0),
        ),
        load_callback=load,
        save_callback=save,
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
        "example_strategy",
        "策略文本与候选筛选都由插件私有 JSON 决定的完整示例。",
        str(context.module_path),
        factory,
        configuration,
        lambda: None,
    )
