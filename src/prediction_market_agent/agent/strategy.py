from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PassThroughDecisionStrategy:
    """Neutral selection used when no optional decision-strategy plugin is active."""

    path: Path = Path("")
    sha256: str = ""
    instructions: str = ""

    def to_prompt_payload(self) -> dict[str, Any]:
        return {
            "configured": False,
            "instructions": "",
            "discovery": {"mode": "all platform candidates"},
        }

    def select_topics(self, topics: list[Any]) -> list[Any]:
        return list(topics)

    def select_markets(self, markets: tuple[Any, ...]) -> list[Any]:
        return list(markets)

    def select_outcomes(self, outcomes: tuple[Any, ...]) -> list[Any]:
        return list(outcomes)


@dataclass(frozen=True)
class DecisionStrategyPlugin:
    path: Path
    sha256: str
    instructions: str
    topic_statuses: tuple[str, ...]
    market_statuses: tuple[str, ...]
    outcome_names: tuple[str, ...]
    minimum_topic_liquidity: float

    @classmethod
    def load(
        cls,
        configured: Path,
        *,
        topic_statuses: tuple[str, ...],
        market_statuses: tuple[str, ...],
        outcome_names: tuple[str, ...],
        minimum_topic_liquidity: float,
    ) -> "DecisionStrategyPlugin":
        path = configured if configured.is_absolute() else (Path.cwd() / configured)
        path = path.resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Decision strategy file does not exist: {path}")
        raw = path.read_bytes()
        if len(raw) > 64_000:
            raise ValueError("Decision strategy file exceeds 64 KB")
        try:
            text = raw.decode("utf-8-sig").strip()
        except UnicodeDecodeError as error:
            raise ValueError("Decision strategy file must be UTF-8 text") from error
        if not text:
            raise ValueError("Decision strategy file is empty")
        if not topic_statuses or not market_statuses or not outcome_names:
            raise ValueError("Decision strategy discovery lists cannot be empty")
        if minimum_topic_liquidity < 0:
            raise ValueError("Decision strategy minimum liquidity cannot be negative")
        return cls(
            path=path,
            sha256=hashlib.sha256(raw).hexdigest(),
            instructions=text,
            topic_statuses=topic_statuses,
            market_statuses=market_statuses,
            outcome_names=outcome_names,
            minimum_topic_liquidity=minimum_topic_liquidity,
        )

    def to_prompt_payload(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "sha256": self.sha256,
            "instructions": self.instructions,
            "discovery": {
                "topic_statuses": list(self.topic_statuses),
                "market_statuses": list(self.market_statuses),
                "outcome_names": list(self.outcome_names),
                "minimum_topic_liquidity": self.minimum_topic_liquidity,
            },
        }

    def select_topics(self, topics: list[Any]) -> list[Any]:
        return [
            topic for topic in topics
            if str(topic.status).upper() in self.topic_statuses
            and float(topic.liquidity_usdt) >= self.minimum_topic_liquidity
        ]

    def select_markets(self, markets: tuple[Any, ...]) -> list[Any]:
        return [market for market in markets if str(market.status).upper() in self.market_statuses]

    def select_outcomes(self, outcomes: tuple[Any, ...]) -> list[Any]:
        return [outcome for outcome in outcomes if str(outcome.name).upper() in self.outcome_names]
