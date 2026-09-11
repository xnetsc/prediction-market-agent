from __future__ import annotations

import logging
from typing import Any

from ..core.risk import RiskCoordinator, RuleDecision

LOGGER = logging.getLogger(__name__)


READ_OPERATIONS = (
    "sync_time",
    "list_topics",
    "get_topic",
    "get_order_book",
    "get_candles",
    "search_market_candidates",
)
"""Contract calls that only read. They still reach the platform and still cost its rate budget."""


class MarketActionRejected(RuntimeError):
    """A business-risk rule refused a market API call."""


def business_target(platform: str) -> str:
    return f"market:{platform}"


class GuardedMarketApi:
    """Put every market API call past business risk, whoever asked for it.

    Business risk is about what happens on the platform, not about who wanted it: the same order is
    just as consequential whether the model proposed it, a settlement sweep produced it, or an
    operator triggered a manual cycle. Gating it inside the Agent path would have covered only one
    of those, and would have missed reads entirely even though reads spend the platform's rate
    budget and decide what the rest of the cycle can see.

    Anything the guard does not recognise is delegated untouched, so a plugin that offers more than
    the contract keeps working and simply is not gated by this layer.
    """

    def __init__(self, plugin: Any, risk: RiskCoordinator):
        self._plugin = plugin
        self._risk = risk
        self._target = business_target(plugin.name)

    # The framework reads these directly and they must not look different through the guard.
    @property
    def name(self) -> str:
        return self._plugin.name

    @property
    def capabilities(self) -> Any:
        return self._plugin.capabilities

    def __getattr__(self, item: str) -> Any:
        return getattr(self._plugin, item)

    def _check(self, operation: str, context: dict[str, Any]) -> RuleDecision:
        decision = self._risk.evaluate(
            self._target, operation, {"kind": "market_api", "platform": self.name, **context}
        )
        if decision.outcome == "ADJUST":
            # A market API call cannot be made smaller here. The size is already bound to a quote
            # the platform issued, and a read has no size at all. Letting it through would leave the
            # rule author believing a cap applied while the full order went out, so this refuses and
            # says where reducing does work.
            raise MarketActionRejected(
                f"{operation} refused: business risk cannot reduce a market API call "
                f"({decision.reason}). Reduce the size from an agent_policy rule instead, which "
                f"runs before the platform is quoted."
            )
        if decision.outcome in {"REJECT", "HALT"}:
            LOGGER.warning(
                "business risk %s %s on %s: %s",
                decision.outcome.lower(),
                operation,
                self.name,
                decision.reason,
            )
            raise MarketActionRejected(
                f"{operation} refused by business risk: {decision.reason}"
            )
        return decision

    def sync_time(self) -> None:
        self._check("sync_time", {})
        return self._plugin.sync_time()

    def list_topics(self, *, offset: int, limit: int) -> Any:
        self._check("list_topics", {"offset": offset, "limit": limit})
        return self._plugin.list_topics(offset=offset, limit=limit)

    def get_topic(self, topic_id: str) -> Any:
        self._check("get_topic", {"market_topic_id": topic_id})
        return self._plugin.get_topic(topic_id)

    def get_order_book(self, market_id: str, outcome_id: str) -> Any:
        self._check("get_order_book", {"market_id": market_id, "token_id": outcome_id})
        return self._plugin.get_order_book(market_id, outcome_id)

    def get_candles(self, reference_symbol: str, interval: str = "1m", limit: int = 120) -> Any:
        self._check(
            "get_candles",
            {"symbol": reference_symbol, "interval": interval, "limit": limit},
        )
        return self._plugin.get_candles(reference_symbol, interval=interval, limit=limit)

    def search_market_candidates(self, query: str, limit: int) -> Any:
        self._check("search_market_candidates", {"query": query, "limit": limit})
        return self._plugin.search_market_candidates(query, limit)

    def create_write_gateway(self, state: Any, risk: Any) -> Any:
        """Hand the gateway the same guard, so writes are checked on the same target."""
        gateway = self._plugin.create_write_gateway(state, risk)
        gateway.business_risk = self._check
        return gateway
