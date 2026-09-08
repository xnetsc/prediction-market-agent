from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..broker import ExecutionGateway, ExecutionRiskControl
from ..models import AccountState
from ..risk import NetworkWriteGate


@dataclass(frozen=True)
class ApiCapabilities:
    realtime_order_book: bool
    candles: bool
    market_search: bool
    settlement_status: bool
    supported_order_types: tuple[str, ...]
    write_workflows: tuple[str, ...]
    data_features: tuple[str, ...]
    limitations: tuple[str, ...] = ()
    supported_transfer_directions: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Outcome:
    outcome_id: str
    name: str
    displayed_probability: float | None = None


@dataclass(frozen=True)
class Market:
    market_id: str
    title: str
    question: str
    status: str
    liquidity_usdt: float
    volume_usdt: float
    outcomes: tuple[Outcome, ...]


@dataclass(frozen=True)
class Topic:
    topic_id: str
    title: str
    question: str
    description: str
    category: str
    status: str
    liquidity_usdt: float
    volume_usdt: float
    slug: str = ""


@dataclass(frozen=True)
class TopicPage:
    topics: tuple[Topic, ...]
    has_more: bool
    next_offset: int


@dataclass(frozen=True)
class TopicDetail:
    topic: Topic
    start_time_ms: int | None
    end_time_ms: int | None
    fee_bps: int
    markets: tuple[Market, ...]
    chart_type: str = ""
    reference_symbol: str = ""
    resolution: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PriceLevel:
    price: float
    quantity: float


@dataclass(frozen=True)
class OrderBook:
    bids: tuple[PriceLevel, ...]
    asks: tuple[PriceLevel, ...]
    observed_at_ms: int | None = None


@dataclass(frozen=True)
class Candle:
    open_time_ms: int
    close_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class MarketCandidate:
    platform: str
    topic: Topic
    detail: TopicDetail | None
    books: dict[str, OrderBook]
    retrieval_score: float | None
    warning: str = "Retrieval candidate only; the Agent must judge semantic relevance."

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PredictionMarketApiPlugin(Protocol):
    """Only API surface visible to the engine; plugins normalize platform differences."""

    name: str
    capabilities: ApiCapabilities
    network_rule_engine: NetworkWriteGate

    def sync_time(self) -> None: ...

    def list_topics(self, *, offset: int, limit: int) -> TopicPage: ...

    def get_topic(self, topic_id: str) -> TopicDetail: ...

    def get_order_book(self, market_id: str, outcome_id: str) -> OrderBook: ...

    def get_candles(
        self, reference_symbol: str, interval: str = "1m", limit: int = 120
    ) -> list[Candle]: ...

    def create_write_gateway(
        self, state: AccountState, risk: ExecutionRiskControl
    ) -> ExecutionGateway: ...

    def search_market_candidates(self, query: str, limit: int) -> list[MarketCandidate]: ...

    def write_transport(self) -> Any: ...

    def configuration_manifest(self) -> dict[str, Any]: ...

    def outcome_won(
        self, detail: TopicDetail, market: Market, outcome: Outcome
    ) -> bool | None: ...


def platform_state_path(configured: Path, platform: str, multiple: bool) -> Path:
    if not multiple:
        return configured
    suffix = configured.suffix or ".json"
    return configured.with_name(f"{configured.stem}-{platform}{suffix}")
