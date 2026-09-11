from __future__ import annotations
from prediction_market_agent.plugin_system.network_diagnostics import configured_proxy_route

import html
import json
import re
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
from typing import Any

from prediction_market_agent.plugin_system.config_io import json_file_callbacks
from prediction_market_agent.plugin_system.discovery import PluginConfigField, PluginConfiguration, PluginInitializationContext, PluginSpec
from prediction_market_agent.agent.research import ResearchToolContext, ResearchToolError


class _SearchParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, str]] = []
        self._current: dict[str, str] | None = None
        self._capture = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = set((values.get("class") or "").split())
        if tag == "a" and "result__a" in classes:
            url = values.get("href") or ""
            parsed = urllib.parse.urlparse(url)
            if parsed.hostname and parsed.hostname.endswith("duckduckgo.com"):
                url = urllib.parse.parse_qs(parsed.query).get("uddg", [url])[0]
            self._current = {"title": "", "url": urllib.parse.unquote(url), "snippet": ""}
            self.results.append(self._current)
            self._capture = "title"
        elif self._current is not None and "result__snippet" in classes:
            self._capture = "snippet"

    def handle_endtag(self, tag: str) -> None:
        if tag in {"a", "div"}:
            self._capture = ""

    def handle_data(self, data: str) -> None:
        if self._current is not None and self._capture:
            self._current[self._capture] += data


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._ignored = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in {"script", "style", "noscript", "svg"}:
            self._ignored += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self._ignored:
            self._ignored -= 1

    def handle_data(self, data: str) -> None:
        if not self._ignored and data.strip():
            self.parts.append(data.strip())


def _csv(value: str, *, upper: bool = False) -> tuple[str, ...]:
    items = (item.strip() for item in value.split(","))
    return tuple(dict.fromkeys((item.upper() if upper else item) for item in items if item))


@dataclass(frozen=True)
class StandardResearchSettings:
    search_url: str
    proxy: str
    timeout_seconds: int
    user_agent: str
    search_response_bytes: int
    fetch_response_bytes: int
    fetch_text_chars: int
    default_search_results: int
    max_search_results: int
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
        "SEARCH_WEB": {"purpose": "Search the configured web-search endpoint.", "arguments": {"query": "required string", "max_results": "optional integer"}},
        "SEARCH_MARKETS": {"purpose": "Search all registered prediction-market plugins for candidates.", "arguments": {"query": "required string", "max_results_per_platform": "optional integer"}},
        "FETCH_URL": {"purpose": "Read one URL permitted by this plugin's network policy.", "arguments": {"url": "required URL"}},
        "REFRESH_MARKET": {"purpose": "Refresh the current normalized market and order book.", "arguments": {}},
        "GET_KLINES": {"purpose": "Read normalized price history from the current API plugin.", "arguments": {"interval": "optional configured interval", "limit": "optional integer"}},
        "RECALL_HISTORY": {"purpose": "Recall earlier decisions and actions for this market.", "arguments": {"limit": "optional integer"}},
    }

    def __init__(self, settings: StandardResearchSettings, context: ResearchToolContext):
        self.settings = settings
        self.context = context
        handler = urllib.request.ProxyHandler({"http": settings.proxy, "https": settings.proxy}) if settings.proxy else urllib.request.ProxyHandler({})
        self.opener = urllib.request.build_opener(handler)

    @staticmethod
    def _integer(value: Any, default: int, minimum: int, maximum: int) -> int:
        try:
            result = int(value)
        except (TypeError, ValueError):
            result = default
        return max(minimum, min(maximum, result))

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "SEARCH_WEB": self._search_web,
            "SEARCH_MARKETS": self._search_markets,
            "FETCH_URL": self._fetch_url,
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

    def _search_web(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = self._query(arguments)
        maximum = self._integer(arguments.get("max_results"), self.settings.default_search_results, 1, self.settings.max_search_results)
        separator = "&" if "?" in self.settings.search_url else "?"
        url = self.settings.search_url + separator + urllib.parse.urlencode({"q": query})
        request = urllib.request.Request(url, headers={"User-Agent": self.settings.user_agent, "Accept": "text/html"}, method="GET")
        with self.opener.open(request, timeout=self.settings.timeout_seconds) as response:
            parser = _SearchParser()
            parser.feed(response.read(self.settings.search_response_bytes).decode("utf-8", errors="replace"))
        results = [{"title": html.unescape(re.sub(r"\s+", " ", item["title"])).strip(), "url": item["url"], "snippet": html.unescape(re.sub(r"\s+", " ", item["snippet"])).strip()} for item in parser.results[:maximum]]
        return {"query": query, "results": results, "source": self.settings.search_url}

    def _fetch_url(self, arguments: dict[str, Any]) -> dict[str, Any]:
        url = str(arguments.get("url", "")).strip()
        request = urllib.request.Request(url, headers={"User-Agent": self.settings.user_agent, "Accept": "text/html,text/plain,application/json"}, method="GET")
        with self.opener.open(request, timeout=self.settings.timeout_seconds) as response:
            final_url = response.geturl()
            content_type = response.headers.get_content_type()
            raw = response.read(self.settings.fetch_response_bytes).decode("utf-8", errors="replace")
        if content_type == "application/json":
            try:
                text = json.dumps(json.loads(raw), ensure_ascii=False)
            except json.JSONDecodeError:
                text = raw
        elif content_type.startswith("text/"):
            parser = _TextParser()
            parser.feed(raw)
            text = "\n".join(parser.parts)
        else:
            raise ResearchToolError(f"Unsupported fetched content type: {content_type}")
        return {"url": final_url, "content_type": content_type, "text": text[:self.settings.fetch_text_chars]}

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
        return self.context.memory.recalled_context(market_topic_id=self.context.market_topic_id, token_id=self.context.token_id, history_limit=limit, char_budget=self.settings.fetch_text_chars, platform=self.context.platform)


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
        PluginConfigField("SEARCH_URL", "搜索 URL", "string", "网页搜索 GET 端点；插件追加 q 查询参数。", required=True),
        PluginConfigField("HTTP_PROXY", "代理使用方式", "string", "默认 INHERIT，使用程序设置里的统一代理。也可单独填 DIRECT、HOST、ENVIRONMENT、SYSTEM（仅原生 macOS）或完整 http(s) URL。", required=True, default="INHERIT"),
        PluginConfigField("HTTP_TIMEOUT_SECONDS", "HTTP 超时", "integer", "研究网络请求的超时秒数。", required=True),
        PluginConfigField("USER_AGENT", "User-Agent", "string", "研究网络请求发送的 User-Agent。", required=True),
        PluginConfigField("SEARCH_RESPONSE_BYTES", "搜索响应字节上限", "integer", "单次搜索最多读取的响应字节数。", required=True),
        PluginConfigField("FETCH_RESPONSE_BYTES", "网页响应字节上限", "integer", "单次读取网页最多接收的字节数。", required=True),
        PluginConfigField("FETCH_TEXT_CHARS", "网页文本字符上限", "integer", "解析后返回给 Agent 的网页文本字符上限。", required=True),
        PluginConfigField("DEFAULT_SEARCH_RESULTS", "默认搜索条数", "integer", "Agent 未指定时返回的搜索结果数。", required=True),
        PluginConfigField("MAX_SEARCH_RESULTS", "最大搜索条数", "integer", "单次网页搜索允许返回的最大结果数。", required=True),
        PluginConfigField("QUERY_MAX_CHARS", "查询字符上限", "integer", "网页与市场搜索查询字符串的最大字符数。", required=True),
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
        retired_fields=("ALLOWED_URL_SCHEMES", "BLOCK_NON_PUBLIC_ADDRESSES"),
    )

    def factory(config):
        del config
        values = configuration.load()
        settings = StandardResearchSettings(
            search_url=values["SEARCH_URL"], proxy=context.proxy_settings(str(values["HTTP_PROXY"]), field_name="HTTP_PROXY")["proxy"], timeout_seconds=values["HTTP_TIMEOUT_SECONDS"], user_agent=values["USER_AGENT"], search_response_bytes=values["SEARCH_RESPONSE_BYTES"], fetch_response_bytes=values["FETCH_RESPONSE_BYTES"], fetch_text_chars=values["FETCH_TEXT_CHARS"], default_search_results=values["DEFAULT_SEARCH_RESULTS"], max_search_results=values["MAX_SEARCH_RESULTS"], query_max_chars=values["QUERY_MAX_CHARS"], kline_intervals=_csv(values["KLINE_INTERVALS"]), default_kline_interval=values["DEFAULT_KLINE_INTERVAL"], kline_default_limit=values["KLINE_DEFAULT_LIMIT"], kline_min_limit=values["KLINE_MIN_LIMIT"], kline_max_limit=values["KLINE_MAX_LIMIT"], market_default_results=values["MARKET_DEFAULT_RESULTS"], market_max_results=values["MARKET_MAX_RESULTS"], history_max_results=values["HISTORY_MAX_RESULTS"],
        )
        integers = [settings.timeout_seconds, settings.search_response_bytes, settings.fetch_response_bytes, settings.fetch_text_chars, settings.default_search_results, settings.max_search_results, settings.query_max_chars, settings.kline_default_limit, settings.kline_min_limit, settings.kline_max_limit, settings.market_default_results, settings.market_max_results, settings.history_max_results]
        if any(value <= 0 for value in integers):
            raise ValueError("Standard research numeric limits must be positive")
        if settings.default_search_results > settings.max_search_results:
            raise ValueError("DEFAULT_SEARCH_RESULTS cannot exceed MAX_SEARCH_RESULTS")
        if not settings.kline_intervals:
            raise ValueError("KLINE_INTERVALS cannot be empty")
        if not settings.kline_min_limit <= settings.kline_default_limit <= settings.kline_max_limit:
            raise ValueError("KLINE_DEFAULT_LIMIT must be within the configured min/max range")
        if settings.market_default_results > settings.market_max_results:
            raise ValueError("MARKET_DEFAULT_RESULTS cannot exceed MARKET_MAX_RESULTS")
        if settings.default_kline_interval not in settings.kline_intervals:
            raise ValueError("DEFAULT_KLINE_INTERVAL must be listed in KLINE_INTERVALS")
        return StandardResearchContribution(settings)

    return PluginSpec("research_tool", "standard_research", "可配置的网页、跨市场、行情刷新、K线和会话召回工具集。", str(context.module_path), factory, configuration, lambda: None, network_routes_callback=lambda: configured_proxy_route(load, "HTTP_PROXY", default="INHERIT", resolver=lambda value: context.proxy_settings(value, field_name="HTTP_PROXY")))
