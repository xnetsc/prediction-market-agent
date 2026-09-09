from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from prediction_market_agent.runtime.broker import ExecutionGateway
from prediction_market_agent.core.domain import AccountState
from prediction_market_agent.core.risk import NetworkWriteGate
from prediction_market_agent.plugin_system.contracts import (
    ApiCapabilities,
    Candle,
    Market,
    MarketCandidate,
    OrderBook,
    Outcome,
    PriceLevel,
    Topic,
    TopicDetail,
    TopicPage,
)
from .write import BinancePredictionWriteTransport
from .config import BinancePluginConfig
from .read import BinancePredictionReadClient


def _tokens(value: str) -> set[str]:
    return {
        item
        for item in re.findall(r"[\w]+", value.casefold(), flags=re.UNICODE)
        if len(item) >= 3
    }


class BinancePredictionApiPlugin:
    """Binance production read and write API adapter."""

    name = "binance"
    capabilities = ApiCapabilities(
        realtime_order_book=True,
        candles=True,
        market_search=True,
        settlement_status=True,
        supported_order_types=("MARKET", "LIMIT"),
        write_workflows=("GET_QUOTE", "BUY", "SELL", "CANCEL", "REDEEM", "TRANSFER"),
        data_features=(
            "topic_list",
            "market_detail",
            "top_of_book_and_depth",
            "displayed_probability",
            "liquidity_and_volume",
            "crypto_reference_candles",
            "resolution_metadata",
        ),
        limitations=(
            "Private Prediction endpoints require Binance signed requests and SAS for writes.",
            "Historical prediction-market trades are not exposed by this adapter.",
            "Write requests require the plugin's configured credentials and wallet identifiers.",
        ),
        supported_transfer_directions=("INBOUND", "OUTBOUND"),
    )

    def __init__(self, environment: Mapping[str, str] | None = None):
        if environment is None:
            raise ValueError("Binance plugin configuration mapping is required")
        self.settings = BinancePluginConfig.from_mapping(environment)
        network = self.settings.network_rules
        gate = NetworkWriteGate(
            allowed_hosts=frozenset(str(item) for item in network["hosts"]),
            allowed_schemes=frozenset(str(item) for item in network["schemes"]),
            allowed_methods=frozenset(str(item).upper() for item in network["methods"]),
            allowed_read_paths=frozenset(str(item) for item in network.get("paths", [])),
            allowed_paths_by_method={
                str(method).upper(): frozenset(str(path) for path in paths)
                for method, paths in network.get("paths_by_method", {}).items()
            },
            target_name=f"network:{self.name}",
        )
        self.network_rule_engine = gate
        self._write_transport = BinancePredictionWriteTransport(self.settings, gate)
        self.client = BinancePredictionReadClient(
            self.settings.api_key,
            self.settings.api_secret,
            self.settings.base_url,
            gate=gate,
            http_proxy=self.settings.http_proxy,
        )
        self._topics: list[dict[str, Any]] = []

    def sync_time(self) -> None:
        self.client.sync_time()

    @staticmethod
    def _topic(item: dict[str, Any]) -> Topic:
        return Topic(
            topic_id=str(item.get("marketTopicId", "")),
            title=str(item.get("title", "")),
            question=str(item.get("question", "")),
            description=str(item.get("description", "")),
            category=str(item.get("l1Category", "")),
            status=str(item.get("status", "")),
            liquidity_usdt=float(item.get("liquidity") or 0),
            volume_usdt=float(item.get("tradeVolume") or 0),
            slug=str(item.get("slug", "")),
        )

    @staticmethod
    def _market(item: dict[str, Any]) -> Market:
        def displayed_probability(outcome: dict[str, Any]) -> float | None:
            value = outcome.get("chance")
            if value is None:
                value = outcome.get("price")
            return None if value is None else float(value)

        outcomes = tuple(
            Outcome(
                outcome_id=str(outcome.get("tokenId", "")),
                name=str(outcome.get("name", "")),
                displayed_probability=displayed_probability(outcome),
            )
            for outcome in item.get("outcomes") or []
        )
        return Market(
            market_id=str(item.get("marketId", "")),
            title=str(item.get("title", "")),
            question=str(item.get("question", "")),
            status=str(item.get("tradingStatus", item.get("status", ""))),
            liquidity_usdt=float(item.get("liquidity") or 0),
            volume_usdt=float(item.get("tradeVolume") or 0),
            outcomes=outcomes,
        )

    def list_topics(self, *, offset: int, limit: int) -> TopicPage:
        page = self.client.list_markets(offset=offset, limit=limit)
        if offset == 0:
            self._topics = []
        batch = page.get("marketTopics", [])
        if isinstance(batch, list):
            self._topics.extend(batch)
        topics = tuple(self._topic(item) for item in batch if isinstance(item, dict))
        return TopicPage(
            topics=topics,
            has_more=bool(page.get("hasMore")),
            next_offset=offset + len(topics),
        )

    def get_topic(self, topic_id: str) -> TopicDetail:
        detail = self.client.market_detail(int(topic_id))
        topic = self._topic(detail)
        return TopicDetail(
            topic=topic,
            start_time_ms=(int(detail["startDate"]) if detail.get("startDate") is not None else None),
            end_time_ms=(int(detail["endDate"]) if detail.get("endDate") is not None else None),
            fee_bps=int(detail.get("feeRateBps") or 0),
            markets=tuple(self._market(item) for item in detail.get("markets") or []),
            chart_type=str(detail.get("chartType", "")),
            reference_symbol=str(detail.get("symbol", "")),
            resolution=dict(detail.get("variantData") or {}),
        )

    def get_order_book(self, market_id: str, outcome_id: str) -> OrderBook:
        book = self.client.order_book(int(market_id), outcome_id)

        def levels(name: str) -> tuple[PriceLevel, ...]:
            return tuple(
                PriceLevel(price=float(item["price"]), quantity=float(item.get("quantity") or 0))
                for item in book.get(name) or []
            )

        return OrderBook(
            bids=levels("bids"),
            asks=levels("asks"),
            observed_at_ms=(int(book["timestamp"]) if book.get("timestamp") is not None else None),
        )

    def get_candles(
        self, reference_symbol: str, interval: str = "1m", limit: int = 120
    ) -> list[Candle]:
        rows = self.client.klines(reference_symbol, interval=interval, limit=limit)
        return [
            Candle(
                open_time_ms=int(row[0]),
                close_time_ms=int(row[6]),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
            )
            for row in rows
        ]

    def create_write_gateway(self, state: AccountState, risk) -> ExecutionGateway:
        return ExecutionGateway(
            state,
            risk,
            platform=self.name,
            write_transport=self._write_transport,
        )

    def write_transport(self) -> BinancePredictionWriteTransport:
        return self._write_transport

    def configuration_manifest(self) -> dict[str, Any]:
        return self.settings.manifest()

    def outcome_won(
        self, detail: TopicDetail, market: Market, outcome: Outcome
    ) -> bool | None:
        if detail.resolution.get("endPrice") is None or detail.resolution.get("startPrice") is None:
            return None
        winner = "UP" if float(detail.resolution["endPrice"]) > float(detail.resolution["startPrice"]) else "DOWN"
        direction_won = market.title.upper() == winner
        return direction_won if outcome.name.upper() == "YES" else not direction_won

    def close(self) -> None:
        close = getattr(self._write_transport, "close", None)
        if callable(close):
            close()

    def search_market_candidates(self, query: str, limit: int) -> list[MarketCandidate]:
        query_tokens = _tokens(query)
        ranked: list[tuple[float, dict[str, Any]]] = []
        for topic in self._topics:
            candidate = _tokens(f"{topic.get('title', '')} {topic.get('question', '')}")
            if not query_tokens or not candidate:
                continue
            score = len(query_tokens & candidate) / len(query_tokens | candidate)
            if score > 0:
                ranked.append((score, topic))
        results: list[MarketCandidate] = []
        for score, item in sorted(ranked, key=lambda value: value[0], reverse=True)[:limit]:
            topic = self._topic(item)
            detail: TopicDetail | None = None
            books: dict[str, OrderBook] = {}
            try:
                detail = self.get_topic(topic.topic_id)
                for market in detail.markets[:4]:
                    yes = next(
                        (outcome for outcome in market.outcomes if outcome.name.upper() == "YES"), None
                    )
                    if yes:
                        books[yes.outcome_id] = self.get_order_book(market.market_id, yes.outcome_id)
            except (KeyError, TypeError, ValueError, RuntimeError):
                detail = None
                books = {}
            results.append(
                MarketCandidate(
                    platform=self.name,
                    topic=topic,
                    detail=detail,
                    books=books,
                    retrieval_score=round(score, 4),
                )
            )
        return results
