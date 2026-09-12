from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol

from .consultation import AgentConsultation
from ..runtime.memory import SessionMemory
from ..plugin_system.contracts import PredictionMarketApiPlugin


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

    consultation: AgentConsultation | None = None
    """The way back to whoever is asking, for any tool plugin that hits a fork it cannot settle.

    Tools normally answer questions; occasionally one has to ask one. A plugin holding two
    instructions that contradict each other, or an argument that only the asker can disambiguate,
    can either pick a rule and be quietly wrong sometimes or ask and be right. This is how it asks,
    and it carries the round's own market, trace and strategy text with it, so the question is
    answered in the situation that produced it rather than in isolation.

    Absent whenever nothing is driving a model - a panel action, a settlement sweep, a test - so a
    plugin must always have an answer it can give without it.
    """


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
