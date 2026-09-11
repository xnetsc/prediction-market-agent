from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from prediction_market_agent.runtime.broker import ExecutionGateway
from prediction_market_agent.core.domain import AccountState
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
from .read import PolymarketReadClient
from .write import PolymarketWriteTransport
from .config import PolymarketPluginConfig


def _tokens(value: str) -> set[str]:
    return {
        item
        for item in re.findall(r"[\w]+", value.casefold(), flags=re.UNICODE)
        if len(item) >= 3
    }


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _milliseconds(value: Any) -> int | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return int(number if number > 10_000_000_000 else number * 1000)
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


class PolymarketApiPlugin:
    """Polymarket Gamma discovery + CLOB V2 market data and trading adapter."""

    name = "polymarket"
    capabilities = ApiCapabilities(
        realtime_order_book=True,
        candles=True,
        market_search=True,
        settlement_status=False,
        supported_order_types=("MARKET", "LIMIT"),
        write_workflows=("BUY", "SELL", "CANCEL", "REDEEM", "TRANSFER_OUT"),
        data_features=(
            "event_list_and_detail",
            "market_and_outcome_normalization",
            "top_of_book_and_depth",
            "displayed_probability",
            "liquidity_and_volume",
            "outcome_price_history",
            "resolution_source_and_rules",
            "negative_risk_metadata",
        ),
        limitations=(
            "Production trading requires a Polygon wallet and CLOB L2 credentials.",
            "CLOB V2 has dynamic taker fees; Gamma fee fields are not treated as a fixed fee quote.",
            "Price-history points are normalized to flat OHLC candles; they are not exchange trade bars.",
            "Write requests require the plugin's configured Polygon wallet and CLOB credentials.",
        ),
        supported_transfer_directions=("OUTBOUND",),
    )

    def __init__(self, environment: Mapping[str, str] | None = None):
        if environment is None:
            raise ValueError("Polymarket plugin configuration mapping is required")
        self.settings = PolymarketPluginConfig.from_mapping(environment)
        self.client = PolymarketReadClient(self.settings)
        self._write_transport = PolymarketWriteTransport(self.settings)
        self._events: list[dict[str, Any]] = []

    def sync_time(self) -> None:
        self.client.sync_time()

    def cycle_limits(self) -> tuple[int, int]:
        return (
            self.settings.max_topics_per_cycle,
            self.settings.max_decisions_per_cycle,
        )

    def topic_page_size(self) -> int:
        return self.settings.topic_page_size

    @staticmethod
    def _topic(item: dict[str, Any]) -> Topic:
        status = "CLOSED" if item.get("closed") else "OPEN" if item.get("active") else "INACTIVE"
        return Topic(
            topic_id=str(item.get("id", "")),
            title=str(item.get("title", "")),
            question=str(item.get("title", "")),
            description=str(item.get("description", "")),
            category=str(item.get("category", "")),
            status=status,
            liquidity_usdt=float(item.get("liquidity") or 0),
            volume_usdt=float(item.get("volume") or 0),
            slug=str(item.get("slug", "")),
        )

    @staticmethod
    def _market(item: dict[str, Any]) -> Market:
        names = _json_list(item.get("outcomes"))
        prices = _json_list(item.get("outcomePrices"))
        token_ids = _json_list(item.get("clobTokenIds"))
        outcomes: list[Outcome] = []
        for index, name in enumerate(names):
            price: float | None = None
            if index < len(prices) and prices[index] not in {None, ""}:
                try:
                    price = float(prices[index])
                except (TypeError, ValueError):
                    price = None
            outcome_id = str(token_ids[index]) if index < len(token_ids) else ""
            outcomes.append(Outcome(outcome_id, str(name), price))
        accepting = bool(item.get("acceptingOrders")) and bool(item.get("active", True))
        status = "OPEN" if accepting and not item.get("closed") else "CLOSED"
        question = str(item.get("question", ""))
        return Market(
            market_id=str(item.get("conditionId") or item.get("id", "")),
            title=question,
            question=question,
            status=status,
            liquidity_usdt=float(item.get("liquidity") or 0),
            volume_usdt=float(item.get("volume") or 0),
            outcomes=tuple(outcomes),
        )

    def list_topics(self, *, offset: int, limit: int) -> TopicPage:
        batch = self.client.list_events(offset=offset, limit=limit)
        if offset == 0:
            self._events = []
        self._events.extend(batch)
        topics = tuple(self._topic(item) for item in batch)
        return TopicPage(topics, len(batch) == limit, offset + len(batch))

    def get_topic(self, topic_id: str) -> TopicDetail:
        raw = self.client.get_event(topic_id)
        topic = self._topic(raw)
        markets = tuple(
            self._market(item) for item in (raw.get("markets") or []) if isinstance(item, dict)
        )
        first_yes = next(
            (
                outcome.outcome_id
                for market in markets
                for outcome in market.outcomes
                if outcome.name.upper() == "YES" and outcome.outcome_id
            ),
            "",
        )
        return TopicDetail(
            topic=topic,
            start_time_ms=_milliseconds(raw.get("startDate")),
            end_time_ms=_milliseconds(raw.get("endDate")),
            fee_bps=0,
            markets=markets,
            chart_type="EVENT",
            reference_symbol=first_yes,
            resolution={
                "resolutionSource": raw.get("resolutionSource"),
                "negRisk": raw.get("negRisk", raw.get("enableNegRisk")),
                "restricted": raw.get("restricted"),
            },
        )

    def get_order_book(self, market_id: str, outcome_id: str) -> OrderBook:
        del market_id
        raw = self.client.get_order_book(outcome_id)

        def levels(name: str) -> tuple[PriceLevel, ...]:
            return tuple(
                PriceLevel(float(item["price"]), float(item.get("size") or 0))
                for item in (raw.get(name) or [])
                if isinstance(item, dict)
            )

        return OrderBook(
            bids=levels("bids"),
            asks=levels("asks"),
            observed_at_ms=_milliseconds(raw.get("timestamp")),
        )

    def get_candles(
        self, reference_symbol: str, interval: str = "1m", limit: int = 120
    ) -> list[Candle]:
        fidelity = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}.get(
            interval, 5
        )
        api_interval = "1h" if fidelity <= 5 else "1d" if fidelity <= 60 else "1w"
        rows = self.client.get_price_history(
            reference_symbol, interval=api_interval, fidelity=fidelity
        )[-limit:]
        duration = fidelity * 60_000
        return [
            Candle(
                open_time_ms=int(float(item["t"]) * 1000),
                close_time_ms=int(float(item["t"]) * 1000) + duration,
                open=float(item["p"]),
                high=float(item["p"]),
                low=float(item["p"]),
                close=float(item["p"]),
                volume=0.0,
            )
            for item in rows
            if item.get("t") is not None and item.get("p") is not None
        ]

    def create_write_gateway(self, state: AccountState, risk) -> ExecutionGateway:
        return ExecutionGateway(
            state,
            risk,
            platform=self.name,
            write_transport=self._write_transport,
        )

    def write_transport(self) -> PolymarketWriteTransport:
        return self._write_transport

    def configuration_manifest(self) -> dict[str, Any]:
        return self.settings.manifest()

    def outcome_won(
        self, detail: TopicDetail, market: Market, outcome: Outcome
    ) -> bool | None:
        del detail, market, outcome
        return None

    def close(self) -> None:
        self._write_transport.close()

    def search_market_candidates(self, query: str, limit: int) -> list[MarketCandidate]:
        query_tokens = _tokens(query)
        ranked: list[tuple[float, dict[str, Any]]] = []
        for event in self._events:
            candidate_tokens = _tokens(
                f"{event.get('title', '')} {event.get('description', '')}"
            )
            if query_tokens and candidate_tokens:
                score = len(query_tokens & candidate_tokens) / len(query_tokens | candidate_tokens)
                if score > 0:
                    ranked.append((score, event))
        results: list[MarketCandidate] = []
        for score, raw in sorted(ranked, key=lambda item: item[0], reverse=True)[:limit]:
            topic = self._topic(raw)
            detail: TopicDetail | None = None
            books: dict[str, OrderBook] = {}
            try:
                detail = self.get_topic(topic.topic_id)
                for market in detail.markets[:4]:
                    yes = next(
                        (outcome for outcome in market.outcomes if outcome.name.upper() == "YES"),
                        None,
                    )
                    if yes and yes.outcome_id:
                        books[yes.outcome_id] = self.get_order_book(
                            market.market_id, yes.outcome_id
                        )
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
