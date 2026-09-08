from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol

from .memory import SessionMemory
from .plugins.base import PredictionMarketApiPlugin


class ResearchToolError(RuntimeError):
    """A research-tool plugin rejected or failed a request."""


@dataclass(frozen=True)
class ResearchToolContext:
    client: PredictionMarketApiPlugin
    memory: SessionMemory
    platform: str
    market_topic_id: str
    market_id: str
    token_id: str
    symbol: str
    history_limit: int
    market_search: Callable[[str, int], dict[str, Any]] | None = None


class ResearchToolExecutor(Protocol):
    descriptions: dict[str, Any]

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


class ResearchToolContribution(Protocol):
    descriptions: dict[str, Any]

    def create(self, context: ResearchToolContext) -> ResearchToolExecutor: ...


class ResearchToolbox:
    """Generic dispatcher combining enabled research-tool plugin contributions."""

    def __init__(
        self,
        contributions: list[ResearchToolContribution],
        context: ResearchToolContext,
    ) -> None:
        self.descriptions: dict[str, Any] = {}
        self._executors: dict[str, ResearchToolExecutor] = {}
        for contribution in contributions:
            executor = contribution.create(context)
            for name, description in executor.descriptions.items():
                normalized = str(name).strip().upper()
                if not normalized:
                    raise ValueError("Research tool plugin returned an empty tool name")
                if normalized in self._executors:
                    raise ValueError(f"Duplicate research tool name: {normalized}")
                self.descriptions[normalized] = description
                self._executors[normalized] = executor

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        normalized = name.strip().upper()
        executor = self._executors.get(normalized)
        if executor is None:
            raise ResearchToolError(f"Unknown or disabled research tool: {normalized}")
        return executor.execute(normalized, arguments)
