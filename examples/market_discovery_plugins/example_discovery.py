"""Market-discovery plugin with private instructions and a configurable read budget."""

from dataclasses import dataclass
import hashlib
from pathlib import Path

from prediction_market_agent.agent.market_discovery import DiscoveryBudget
from prediction_market_agent.plugin_system.config_io import json_file_callbacks
from prediction_market_agent.plugin_system.discovery import (
    PluginConfigField,
    PluginConfiguration,
    PluginInitializationContext,
    PluginSpec,
)


@dataclass(frozen=True)
class FileMarketDiscovery:
    """Operator-owned discovery instructions implementing the runtime protocol."""

    instructions: str
    horizon_days: int
    read_budget: DiscoveryBudget
    name: str = "example_discovery"
    evolution_switchable: bool = True

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.instructions.encode("utf-8")).hexdigest()

    def budget(self) -> DiscoveryBudget:
        return self.read_budget

    def to_prompt_payload(self) -> dict[str, object]:
        return {
            "source": "plugin",
            "instructions": self.instructions,
            "budget": self.read_budget.to_dict(),
        }


def initialize_plugin(context: PluginInitializationContext) -> PluginSpec:
    load, save, delete, storage = json_file_callbacks(
        context.working_directory / "examples" / "plugin_configs" / "example_discovery.json"
    )
    configuration = PluginConfiguration(
        fields=(
            PluginConfigField(
                "DISCOVERY_FILE",
                "发现策略文件",
                "string",
                "写入标的发现 Agent 系统上下文的 UTF-8 文本路径。",
                required=True,
            ),
            PluginConfigField("HORIZON_DAYS", "偏好揭标天数", "integer", "该发现策略偏好的最长揭标时间。", default=3),
            PluginConfigField("SURVEY_TOPICS", "宽扫上限", "integer", "每轮最多纳入初始候选池的主题数。", default=200),
            PluginConfigField("SHORTLIST_TOPICS", "搜索上限", "integer", "每次主动搜索最多接收的主题数。", default=24),
            PluginConfigField("DETAIL_LOOKUPS", "详情读取", "integer", "每轮最多读取的主题详情数。", default=12),
            PluginConfigField("BOOK_LOOKUPS", "盘口读取", "integer", "每轮最多读取的盘口数。", default=12),
            PluginConfigField("AGENT_TOOL_STEPS", "工具步数", "integer", "发现 Agent 每轮最多执行的工具步骤数。", default=14),
            PluginConfigField("EXPLORATION_SLOTS", "探索名额", "integer", "为未分析主题保留的最少选择名额。", default=1),
            PluginConfigField("COOLDOWN_SECONDS", "冷却秒数", "integer", "无新变化时重复选择同一主题的降权窗口。", default=900),
        ),
        load_callback=load,
        save_callback=save,
        delete_callback=delete,
        storage=storage,
    )

    def factory(application_config):
        del application_config
        values = configuration.load()
        instruction_path = Path(values["DISCOVERY_FILE"])
        if not instruction_path.is_absolute():
            instruction_path = context.working_directory / instruction_path
        instructions = instruction_path.read_text(encoding="utf-8").strip()
        if not instructions:
            raise ValueError("Discovery strategy file cannot be empty")
        horizon_days = int(values["HORIZON_DAYS"])
        if horizon_days < 1:
            raise ValueError("Discovery horizon must be at least one day")
        return FileMarketDiscovery(
            instructions=instructions,
            horizon_days=horizon_days,
            read_budget=DiscoveryBudget(
                survey_topics=int(values["SURVEY_TOPICS"]),
                shortlist_topics=int(values["SHORTLIST_TOPICS"]),
                detail_lookups=int(values["DETAIL_LOOKUPS"]),
                book_lookups=int(values["BOOK_LOOKUPS"]),
                agent_tool_steps=int(values["AGENT_TOOL_STEPS"]),
                exploration_slots=int(values["EXPLORATION_SLOTS"]),
                cooldown_seconds=int(values["COOLDOWN_SECONDS"]),
            ),
        )

    return PluginSpec(
        "market_discovery",
        "example_discovery",
        "从私有文本与读预算构造标的发现策略的完整示例。",
        str(context.module_path),
        factory,
        configuration,
        lambda: None,
    )
