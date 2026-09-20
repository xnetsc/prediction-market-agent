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
            PluginConfigField("SEARCH_RESULT_LIMIT", "单次搜索传输上限", "integer", "单次搜索工具返回的硬资源边界，不是全流程候选配额。", default=100),
            PluginConfigField("DETAIL_READ_SAFETY_LIMIT", "详情读取安全上限", "integer", "单批详情网络读取的硬资源保护。", default=100),
            PluginConfigField("BOOK_READ_SAFETY_LIMIT", "盘口读取安全上限", "integer", "单批盘口网络读取的硬资源保护。", default=100),
            PluginConfigField("AGENT_TOOL_SAFETY_STEPS", "工具安全步数", "integer", "单次发现 Agent 调用的硬工具步数保护。", default=40),
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
                search_result_limit=int(values["SEARCH_RESULT_LIMIT"]),
                detail_read_safety_limit=int(values["DETAIL_READ_SAFETY_LIMIT"]),
                book_read_safety_limit=int(values["BOOK_READ_SAFETY_LIMIT"]),
                agent_tool_steps=int(values["AGENT_TOOL_SAFETY_STEPS"]),
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
