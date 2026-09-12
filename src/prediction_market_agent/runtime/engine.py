from __future__ import annotations

import time

import logging
import threading
from typing import Any

from ..core.config import Config
from ..plugin_system.contracts import Topic
from ..plugin_system.discovery import PluginCatalog
from .actions import ExecutionActionsMixin
from .bootstrap import PlatformRuntime, bootstrap_engine
from .broker import ExecutionError
from .evaluation import MarketEvaluationMixin
from .decision_strategy import DecisionEvolution
from .market_discovery import DiscoveryEngine
from .provider_quality import ProviderQuality
from .memory import SessionMemory

LOGGER = logging.getLogger(__name__)


class TradingEngine(MarketEvaluationMixin, ExecutionActionsMixin):
    """Coordinate one decision cycle across all enabled market plugins."""

    def __init__(self, config: Config, *, catalog: PluginCatalog | None = None):
        self.config = config
        self._cycle_lock = threading.RLock()
        self._owns_catalog = catalog is None
        self.memory = SessionMemory(config.session_db)
        try:
            components = bootstrap_engine(config, catalog=catalog)
        except Exception:
            self.memory.close()
            raise
        self.plugin_catalog = components.plugin_catalog
        self.decision_strategy = components.decision_strategy
        self.risk = components.risk
        self.api_registry = components.api_registry
        self.platforms = components.platforms
        self.provider = components.provider
        self.research_contributions = components.research_contributions
        self.discovery_strategy = components.discovery_strategy
        self.discovery = DiscoveryEngine(
            memory=self.memory,
            strategy=components.discovery_strategy,
            provider=components.provider,
            evolution_enabled=components.strategy_evolution,
            cross_platform_search=self.search_market_candidates,
        )
        self.decision_evolution = DecisionEvolution(
            memory=self.memory,
            strategy=components.decision_strategy,
            evolution_enabled=components.strategy_evolution,
        )
        self.provider_quality = ProviderQuality(memory=self.memory, provider=self.provider)
        self._decisions_this_cycle = 0
        self._max_decisions_this_cycle = 0

    def run_once(self) -> dict[str, float | int | bool | str]:
        with self._cycle_lock:
            for runtime in self.platforms.values():
                maximum_topics, maximum_decisions = runtime.plugin.cycle_limits()
                topics = self._collect_platform_topics(runtime, maximum_topics)
                self._process_platform_topics(runtime, topics, maximum_decisions)
            return self.status()

    def run_platform_once(
        self, platform: str, maximum_topics: int, maximum_decisions: int
    ) -> dict[str, float | int | bool | str]:
        """Execute one platform cycle when invoked by that platform plugin runtime."""
        if maximum_topics <= 0 or maximum_decisions <= 0:
            raise ValueError("Platform cycle limits must be positive")
        with self._cycle_lock:
            try:
                runtime = self.platforms[platform]
            except KeyError as error:
                raise ValueError(f"Unknown active platform: {platform}") from error
            topics = self._collect_platform_topics(runtime, maximum_topics)
            self._process_platform_topics(runtime, topics, maximum_decisions)
            return self.status()

    def process_platform_scan(
        self, platform: str, topics: tuple[Topic, ...], maximum_decisions: int
    ) -> dict[str, float | int | bool | str]:
        """Consume a normalized scan event emitted by one platform plugin."""
        if maximum_decisions <= 0:
            raise ValueError("Platform decision limit must be positive")
        with self._cycle_lock:
            try:
                runtime = self.platforms[platform]
            except KeyError as error:
                raise ValueError(f"Unknown active platform: {platform}") from error
            self._process_platform_topics(runtime, list(topics), maximum_decisions)
            return self.status()

    def discover_platform_topics(
        self, platform: str, maximum_topics: int
    ) -> tuple[Topic, ...]:
        """Framework-side market discovery, invoked by a platform runtime on its own schedule."""
        if maximum_topics <= 0:
            raise ValueError("Platform topic limit must be positive")
        with self._cycle_lock:
            try:
                runtime = self.platforms[platform]
            except KeyError as error:
                raise ValueError(f"Unknown active platform: {platform}") from error
            return tuple(self._collect_platform_topics(runtime, maximum_topics))

    def _collect_platform_topics(
        self, runtime: PlatformRuntime, maximum_topics: int
    ) -> list[Topic]:
        runtime.plugin.sync_time()
        for name, review in (
            ("discovery", self.discovery.review),
            ("decision", self.decision_evolution.review),
            ("provider quality", self.provider_quality.review),
        ):
            try:
                review()
            except Exception:
                LOGGER.exception(
                    "%s strategy review failed; continuing with stored measurements", name
                )
        return list(
            self.discovery.discover(
                platform=runtime.plugin.name,
                plugin=runtime.plugin,
                maximum_topics=maximum_topics,
            )
        )

    def _resume_funded_decisions(self, runtime: PlatformRuntime) -> None:
        """Pick up decisions that stopped to wait for money, once the money question is settled.

        Nothing else will: the round that asked ended when it asked, and the plugin only answers
        when asked. Telling the model "the funds arrived" on its own would be worse than useless -
        it would be an instruction to act on a conclusion nobody has re-examined. So the market is
        re-read and the old reasoning is handed back as something to check, not to resume from.
        """
        try:
            waiting = self.memory.open_funding_continuations(runtime.plugin.name)
        except Exception:
            LOGGER.exception("could not read pending funding continuations")
            return
        for entry in waiting:
            try:
                answer = runtime.plugin.funding_status(str(entry["request_id"]))
            except Exception:
                LOGGER.exception("could not read funding status for %s", entry["request_id"])
                continue
            if answer.state == "pending":
                continue
            self.memory.close_funding_continuation(str(entry["request_id"]))
            if self._decisions_this_cycle >= self._max_decisions_this_cycle:
                LOGGER.info(
                    "%s funding %s settled as %s but this cycle has no decision budget left",
                    runtime.plugin.name, entry["request_id"], answer.state,
                )
                continue
            try:
                self._reconsider_after_funding(runtime, entry, answer)
            except (KeyError, ValueError, RuntimeError, ExecutionError):
                LOGGER.exception(
                    "could not reconsider %s after funding settled", entry["market_topic_id"]
                )

    def _reconsider_after_funding(
        self, runtime: PlatformRuntime, entry: dict[str, Any], answer: Any
    ) -> None:
        """Re-run the decision on fresh market data, with the funding answer as context."""
        detail = runtime.plugin.get_topic(str(entry["market_topic_id"]))
        market = next(
            (
                item
                for item in detail.markets
                for outcome in item.outcomes
                if str(outcome.outcome_id) == str(entry["token_id"])
            ),
            None,
        )
        if market is None:
            LOGGER.info(
                "%s no longer lists %s; the funded intention has nothing to act on",
                runtime.plugin.name, entry["token_id"],
            )
            return
        outcome = next(
            item for item in market.outcomes if str(item.outcome_id) == str(entry["token_id"])
        )
        self._evaluate_outcome(
            runtime,
            detail.topic,
            detail,
            market,
            outcome,
            max(0, int(detail.topic.end_time_ms / 1000 - time.time())),
            funding_followup=self._funding_reminder(entry, answer, detail, market, outcome),
        )

    @staticmethod
    def _elapsed_phrase(milliseconds: int) -> str:
        seconds = max(0, int(milliseconds / 1000))
        if seconds < 90:
            return f"{seconds} seconds"
        if seconds < 5400:
            return f"{seconds // 60} minutes"
        if seconds < 172800:
            return f"{seconds // 3600} hours"
        return f"{seconds // 86400} days"

    def _funding_reminder(
        self, entry: dict[str, Any], answer: Any, detail: Any, market: Any, outcome: Any
    ) -> dict[str, Any]:
        """A note to someone who walked away, not a conversation being picked up mid-sentence.

        Nothing carried over from the round that asked: each decision is a fresh call, and between
        then and now this agent has looked at other markets and reached other conclusions. It does
        not remember asking. So the note has to say what was going on, what was asked for and why,
        how long ago that was, and what the answer turned out to be - and then ask whether the
        thing is still worth doing, rather than assuming it is.
        """
        waited_ms = max(0, int(time.time() * 1000) - int(entry.get("asked_at", 0) or 0))
        remaining = max(0, int(detail.topic.end_time_ms / 1000 - time.time()))
        return {
            "notice": (
                "A reminder, not a new opportunity. Earlier you stopped short on this market and "
                "asked for funds. You have looked at other things since and do not remember this; "
                "everything you need is below."
            ),
            "what_you_were_looking_at": {
                "market": getattr(detail.topic, "title", ""),
                "outcome": getattr(outcome, "name", ""),
                **(entry.get("conclusion") or {}),
            },
            "what_you_asked_for": {
                "amount": entry["asked_for"],
                "currency": entry["currency"],
                "your_reason": entry["reason"],
            },
            "how_long_ago": self._elapsed_phrase(waited_ms),
            "how_long_this_market_still_has": self._elapsed_phrase(remaining * 1000),
            "the_answer": answer.to_dict(),
            "what_to_decide": (
                "Given how long that took and what the answer turned out to be, is the thing you "
                "wanted to do still worth doing? Compare your reason against the prices in front "
                "of you now, which have been re-read since you asked. A long wait is itself "
                "evidence: an edge that depended on acting quickly is probably gone. If it no "
                "longer holds, say so and HOLD rather than completing a trade you would not open "
                "today."
            ),
        }

    def _process_platform_topics(
        self, runtime: PlatformRuntime, topics: list[Topic], maximum_decisions: int
    ) -> None:
        self._decisions_this_cycle = 0
        self._max_decisions_this_cycle = maximum_decisions
        # Answers to funding requests come back between cycles, so this is where they are read.
        # An unclaimed answer is the same as no answer: nobody is sitting waiting for it.
        self._resume_funded_decisions(runtime)
        eligible = self.decision_strategy.select_topics(topics)
        LOGGER.info(
            "platform=%s scanned=%d candidates=%d provider=%s",
            runtime.plugin.name,
            len(topics),
            len(eligible),
            self.provider.name,
        )
        for topic in eligible:
            if self._decisions_this_cycle >= maximum_decisions:
                break
            try:
                self._evaluate_topic(runtime, topic)
            except (KeyError, ValueError, RuntimeError, ExecutionError) as error:
                LOGGER.warning(
                    "skip %s topic %s: %s",
                    runtime.plugin.name,
                    topic.topic_id,
                    error,
                )
        runtime.store.save(runtime.state)

    @staticmethod
    def _platform_status(runtime: PlatformRuntime) -> dict[str, Any]:
        state = runtime.state
        return {
            "platform": runtime.plugin.name,
            "cash": round(state.cash, 6),
            "exposure": round(state.exposure, 6),
            "equity": round(state.equity, 6),
            # The profit-and-loss picture, so whoever is deciding can apply its own stop: there is
            # no built-in stop loss or take profit anywhere, by design. A decision strategy states
            # its thresholds in its own text and reads these numbers, or a business-risk plugin
            # enforces them on the chain. Nothing here judges them.
            "starting_capital": round(state.starting_capital, 6),
            "realized_pnl": round(state.realized_pnl, 6),
            "transferred_out": round(state.transferred_out, 6),
            "net_result": round(
                state.equity + state.transferred_out - state.starting_capital, 6
            ),
            "positions": len(state.positions),
            "orders": len(state.orders),
            "capabilities": runtime.plugin.capabilities.to_dict(),
        }

    def status(self, include_memory: bool = True) -> dict[str, Any]:
        accounts = {
            name: self._platform_status(runtime)
            for name, runtime in self.platforms.items()
        }
        result: dict[str, Any] = {
            "platforms": accounts,
            "aggregate_equity": round(
                sum(runtime.state.equity for runtime in self.platforms.values()), 6
            ),
            "decision_provider": self.provider.name,
            "decision_strategy": {
                "path": str(self.decision_strategy.path),
                "sha256": self.decision_strategy.sha256,
            },
            "available_decision_providers": list(self.provider.available_names),
            "unavailable_decision_providers": self.provider.unavailable,
            "decision_provider_health": self.provider_quality.manifest(),
        }
        if include_memory:
            result.update(self.memory.stats())
        return result

    def close(self) -> None:
        """Release session and plugin-owned resources for this engine instance."""
        try:
            self.memory.close()
        finally:
            if self._owns_catalog:
                self.plugin_catalog.shutdown()
