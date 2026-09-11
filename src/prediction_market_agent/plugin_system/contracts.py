from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..runtime.broker import ExecutionGateway
from ..core.domain import AccountState


@dataclass(frozen=True)
class AccountFunds:
    """What one platform says the trading account can currently spend.

    `source` is the honest part. A platform that exposes a balance endpoint answers "platform" and
    the number is a fact; one that does not answers "declared" and the number is whatever the
    operator configured, which can drift from reality without anyone noticing. Callers that care
    about the difference - and anything deciding how much to risk should care - can tell them apart
    instead of trusting a figure whose origin is hidden.
    """

    available: float
    """Spendable on this prediction market right now, without anything having to be moved first.

    Money the venue holds somewhere that a transfer would have to reach does not count, however
    certain that transfer is. A number that includes it would tell a caller it can trade funds that
    are not there yet, and the order is what would discover the difference.
    """

    currency: str
    source: str
    total: float | None = None
    locked: float | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


FUNDING_STATES = ("satisfied", "pending", "partial", "refused", "failed")

FUNDING_TIMEOUT_SECONDS = 1800
"""How long a request may stay unanswered before the framework stops counting on it.

The framework sets this, not the plugin. A request left open indefinitely is worse than a refused
one: the caller keeps believing money is on the way, and a transfer approved hours later arrives
for a decision that no longer exists. Half an hour is long enough for an operator to notice a
prompt and short enough that a stale approval cannot fund a stale intention.
"""


@dataclass(frozen=True)
class FundingResult:
    """What came of asking for funds - possibly not yet, and possibly not all of it.

    Funding rarely completes inside the call that asks for it: a transfer may need an operator's
    approval, or has to arrive from outside and settle. So the answer carries a state rather than
    a yes/no, and a `request_id` the same request can be identified by afterwards. Without that id
    a later outcome could not be matched to the ask that caused it, and a second ask arriving in
    between would make the pairing a guess.
    """

    request_id: str
    state: str
    requested: float
    currency: str
    available: float
    action: str
    detail: str = ""
    expires_at: int = 0
    """When a pending request stops being valid, as the framework decided. Zero means not pending."""

    operator_note: str = ""
    """What the person answering said, in their words, for the caller to read.

    Approving is not only a yes: "this is the last of it, do not ask again" is a standing
    instruction that the caller has no other way of learning, and which changes what it should do
    next. Refusing is the same in reverse - a reason turns a dead end into something the caller can
    act on instead of asking again identically.
    """

    def __post_init__(self) -> None:
        if self.state not in FUNDING_STATES:
            raise ValueError(f"Unknown funding state: {self.state}")

    @property
    def satisfied(self) -> bool:
        return self.state == "satisfied"

    @property
    def settled(self) -> bool:
        """Whether this request is finished, however it ended. Only `pending` is still in flight."""
        return self.state != "pending"

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "satisfied": self.satisfied, "settled": self.settled}


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

    def sync_time(self) -> None: ...

    def list_topics(self, *, offset: int, limit: int) -> TopicPage: ...

    def get_topic(self, topic_id: str) -> TopicDetail: ...

    def get_order_book(self, market_id: str, outcome_id: str) -> OrderBook: ...

    def get_candles(
        self, reference_symbol: str, interval: str = "1m", limit: int = 120
    ) -> list[Candle]: ...

    def create_write_gateway(self, state: AccountState) -> ExecutionGateway: ...

    def search_market_candidates(self, query: str, limit: int) -> list[MarketCandidate]: ...

    def write_transport(self) -> Any: ...

    def configuration_manifest(self) -> dict[str, Any]: ...

    def cycle_limits(self) -> tuple[int, int]: ...

    def account_funds(self) -> AccountFunds: ...
    """Report what this account can spend right now.

    The plugin owns this because only it knows what its account is and how to look: Spendable is not the same as
    owned: a venue may hold money somewhere that has to be moved first, and only the plugin can
    tell the difference. A plugin that cannot ask still answers, marking the figure as declared.
    """

    def ensure_funds(
        self,
        amount: float,
        currency: str,
        *,
        reason: str = "",
        allow_pending: bool = True,
        timeout_seconds: int = FUNDING_TIMEOUT_SECONDS,
    ) -> FundingResult: ...
    """Make `amount` spendable here, within the terms the caller sets.

    Whether an answer may be deferred, and for how long, is the caller's to decide rather than the
    plugin's: only the caller knows whether anything is still waiting on it. With `allow_pending`
    false a plugin that cannot finish now must say so rather than parking the request, because a
    pending answer nobody will come back for is a request that silently never happens. The timeout
    is the plugin's to honour - a request past it is dead, and reporting it as still pending would
    keep the caller waiting on an approval that can no longer be acted on.

    `reason` is why the money is wanted, in the caller's own words, to be shown to whoever is asked
    to approve it. Being asked to move money with no reason given is being asked to approve on
    trust, and it is the one piece of context the person deciding cannot reconstruct.
    """
    """Make `amount` spendable on this prediction market, by whatever means the venue needs.

    The caller is stating a target for `account_funds().available`, not requesting a transfer of
    that size: a venue already holding enough does nothing and reports satisfied. How the gap gets
    closed - a transfer, an approval, an on-chain deposit, or nothing the plugin can do on its own
    - is the plugin's business. One that cannot close it says so in the result rather than raising,
    because "I could not, and here is what you would have to do" is an answer the caller can act on
    and an exception in the middle of a decision is not.
    """
    """Ask the platform to make `amount` available, and do whatever that takes internally.

    Whether that means a transfer, an approval, or nothing at all is the plugin's business; the
    framework states a need and reads back what actually happened. A plugin that cannot add funds
    says so in the result rather than raising, because "I could not" is an answer the caller can
    act on and an exception in the middle of a decision is not.
    """

    def funding_status(self, request_id: str) -> FundingResult: ...
    """Report where an earlier request got to, identified by the id that request returned.

    Asked rather than announced. A plugin that pushed an outcome would have to find the caller,
    still be running when the outcome arrived, and have somewhere to put a report nobody was
    waiting for; anything lost that way is lost silently. Asking is idempotent, survives a restart,
    and cannot deliver an answer to the wrong request, because the id is the question.
    """

    def topic_page_size(self) -> int: ...

    def outcome_won(
        self, detail: TopicDetail, market: Market, outcome: Outcome
    ) -> bool | None: ...


def platform_state_path(configured: Path, platform: str, multiple: bool) -> Path:
    if not multiple:
        return configured
    suffix = configured.suffix or ".json"
    return configured.with_name(f"{configured.stem}-{platform}{suffix}")
