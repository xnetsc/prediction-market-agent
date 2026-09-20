from __future__ import annotations
from dataclasses import asdict, dataclass, field
from typing import Any

from prediction_market_agent.plugin_system.config_io import json_file_callbacks
from prediction_market_agent.plugin_system.discovery import PluginConfigField, PluginConfiguration, PluginInitializationContext, PluginSpec
from prediction_market_agent.agent.research import ResearchToolContext, ResearchToolError


def _csv(value: str, *, upper: bool = False) -> tuple[str, ...]:
    items = (item.strip() for item in value.split(","))
    return tuple(dict.fromkeys((item.upper() if upper else item) for item in items if item))


@dataclass(frozen=True)
class StandardResearchSettings:
    history_text_chars: int
    query_max_chars: int
    kline_intervals: tuple[str, ...]
    default_kline_interval: str
    kline_default_limit: int
    kline_min_limit: int
    kline_max_limit: int
    market_default_results: int
    market_max_results: int
    history_max_results: int


class StandardResearchExecutor:
    descriptions = {
        "SEARCH_MARKETS": {"purpose": "Search all registered prediction-market plugins for candidates.", "arguments": {"query": "required string", "max_results_per_platform": "optional integer"}},
        "REFRESH_MARKET": {"purpose": "Refresh the current normalized market and order book.", "arguments": {}},
        "GET_KLINES": {"purpose": "Read normalized price history from the current API plugin.", "arguments": {"interval": "optional configured interval", "limit": "optional integer"}},
        "RECALL_HISTORY": {"purpose": "Recall earlier decisions and actions for this market.", "arguments": {"limit": "optional integer"}},
    }

    def __init__(self, settings: StandardResearchSettings, context: ResearchToolContext):
        self.settings = settings
        self.context = context

    @staticmethod
    def _integer(value: Any, default: int, minimum: int, maximum: int) -> int:
        try:
            result = int(value)
        except (TypeError, ValueError):
            result = default
        return max(minimum, min(maximum, result))

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "SEARCH_MARKETS": self._search_markets,
            "REFRESH_MARKET": lambda _: self._refresh_market(),
            "GET_KLINES": self._get_klines,
            "RECALL_HISTORY": self._recall_history,
        }[name](arguments)

    def _query(self, arguments: dict[str, Any]) -> str:
        query = str(arguments.get("query", "")).strip()
        if not query or len(query) > self.settings.query_max_chars:
            raise ResearchToolError("Query length is outside the plugin-configured range")
        return query

    def _search_markets(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if self.context.market_search is None:
            raise ResearchToolError("Cross-platform market search is unavailable")
        maximum = self._integer(arguments.get("max_results_per_platform"), self.settings.market_default_results, 1, self.settings.market_max_results)
        return self.context.market_search(self._query(arguments), maximum)

    def _refresh_market(self) -> dict[str, Any]:
        detail = self.context.client.get_topic(self.context.market_topic_id)
        book = self.context.client.get_order_book(self.context.market_id, self.context.token_id)
        return {"platform": self.context.platform, "market_topic_id": self.context.market_topic_id, "market_id": self.context.market_id, "token_id": self.context.token_id, "detail": asdict(detail), "bids": [asdict(item) for item in book.bids[:10]], "asks": [asdict(item) for item in book.asks[:10]]}

    def _get_klines(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if not self.context.symbol:
            raise ResearchToolError("This API plugin supplied no price-history reference symbol")
        interval = str(arguments.get("interval", self.settings.default_kline_interval))
        if interval not in self.settings.kline_intervals:
            raise ResearchToolError("Kline interval is not enabled in plugin configuration")
        limit = self._integer(arguments.get("limit"), self.settings.kline_default_limit, self.settings.kline_min_limit, self.settings.kline_max_limit)
        rows = self.context.client.get_candles(self.context.symbol, interval=interval, limit=limit)
        return {"symbol": self.context.symbol, "interval": interval, "candles": [asdict(row) for row in rows]}

    def _recall_history(self, arguments: dict[str, Any]) -> dict[str, Any]:
        limit = self._integer(arguments.get("limit"), self.context.history_limit, 1, self.settings.history_max_results)
        return self.context.memory.recalled_context(market_topic_id=self.context.market_topic_id, token_id=self.context.token_id, history_limit=limit, char_budget=self.settings.history_text_chars, platform=self.context.platform)


@dataclass(frozen=True)
class StandardResearchContribution:
    settings: StandardResearchSettings
    descriptions: dict[str, Any] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "descriptions", StandardResearchExecutor.descriptions)

    def create(self, context: ResearchToolContext) -> StandardResearchExecutor:
        return StandardResearchExecutor(self.settings, context)


def initialize_plugin(context: PluginInitializationContext) -> PluginSpec:
    load, save, delete, storage = json_file_callbacks(context.working_directory / "config" / "plugins" / "research_standard.json")
    fields = (
        PluginConfigField("HISTORY_TEXT_CHARS", "历史查询字符上限", "integer", "业务历史查询工具一次返回的最大字符数。", required=True, default=12000),
        PluginConfigField("QUERY_MAX_CHARS", "查询字符上限", "integer", "跨预测市场查询字符串的最大字符数。", required=True),
        PluginConfigField("KLINE_INTERVALS", "K线周期白名单", "string", "允许 Agent 请求的 K 线周期，逗号分隔。", required=True),
        PluginConfigField("DEFAULT_KLINE_INTERVAL", "默认K线周期", "string", "Agent 未指定时使用的 K 线周期。", required=True),
        PluginConfigField("KLINE_DEFAULT_LIMIT", "默认K线条数", "integer", "Agent 未指定时请求的 K 线条数。", required=True),
        PluginConfigField("KLINE_MIN_LIMIT", "最小K线条数", "integer", "单次 K 线请求允许的最小条数。", required=True),
        PluginConfigField("KLINE_MAX_LIMIT", "最大K线条数", "integer", "单次 K 线请求允许的最大条数。", required=True),
        PluginConfigField("MARKET_DEFAULT_RESULTS", "默认跨市场条数", "integer", "每个平台默认返回的跨市场候选数。", required=True),
        PluginConfigField("MARKET_MAX_RESULTS", "最大跨市场条数", "integer", "每个平台单次允许返回的最大候选数。", required=True),
        PluginConfigField("HISTORY_MAX_RESULTS", "历史召回上限", "integer", "单次历史召回允许的最大记录数。", required=True),
    )
    configuration = PluginConfiguration(
        fields, load, save, delete, storage,
        retired_fields=(
            "ALLOWED_URL_SCHEMES", "BLOCK_NON_PUBLIC_ADDRESSES", "SEARCH_URL",
            "HTTP_PROXY", "HTTP_TIMEOUT_SECONDS", "USER_AGENT", "SEARCH_RESPONSE_BYTES",
            "FETCH_RESPONSE_BYTES", "FETCH_TEXT_CHARS", "DEFAULT_SEARCH_RESULTS",
            "MAX_SEARCH_RESULTS",
        ),
    )

    def factory(config):
        del config
        values = configuration.load()
        settings = StandardResearchSettings(
            history_text_chars=values["HISTORY_TEXT_CHARS"], query_max_chars=values["QUERY_MAX_CHARS"], kline_intervals=_csv(values["KLINE_INTERVALS"]), default_kline_interval=values["DEFAULT_KLINE_INTERVAL"], kline_default_limit=values["KLINE_DEFAULT_LIMIT"], kline_min_limit=values["KLINE_MIN_LIMIT"], kline_max_limit=values["KLINE_MAX_LIMIT"], market_default_results=values["MARKET_DEFAULT_RESULTS"], market_max_results=values["MARKET_MAX_RESULTS"], history_max_results=values["HISTORY_MAX_RESULTS"],
        )
        integers = [settings.history_text_chars, settings.query_max_chars, settings.kline_default_limit, settings.kline_min_limit, settings.kline_max_limit, settings.market_default_results, settings.market_max_results, settings.history_max_results]
        if any(value <= 0 for value in integers):
            raise ValueError("Standard research numeric limits must be positive")
        if not settings.kline_intervals:
            raise ValueError("KLINE_INTERVALS cannot be empty")
        if not settings.kline_min_limit <= settings.kline_default_limit <= settings.kline_max_limit:
            raise ValueError("KLINE_DEFAULT_LIMIT must be within the configured min/max range")
        if settings.market_default_results > settings.market_max_results:
            raise ValueError("MARKET_DEFAULT_RESULTS cannot exceed MARKET_MAX_RESULTS")
        if settings.default_kline_interval not in settings.kline_intervals:
            raise ValueError("DEFAULT_KLINE_INTERVAL must be listed in KLINE_INTERVALS")
        return StandardResearchContribution(settings)

    return PluginSpec("research_tool", "standard_research", "预测市场专用的跨市场、行情刷新、K线和业务历史查询工具集；通用搜索和网页读取由官方 Agent CLI 自己完成。", str(context.module_path), factory, configuration, lambda: None)
