from __future__ import annotations

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
        self.global_risk = components.global_risk
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
        self.global_risk.refresh_halt()
        if runtime.state.halted:
            LOGGER.warning(
                "%s account halted: %s",
                runtime.plugin.name,
                runtime.state.halt_reason,
            )
            return []
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

    def _process_platform_topics(
        self, runtime: PlatformRuntime, topics: list[Topic], maximum_decisions: int
    ) -> None:
        self.global_risk.refresh_halt()
        if runtime.state.halted:
            LOGGER.warning(
                "%s account halted before scan processing: %s",
                runtime.plugin.name,
                runtime.state.halt_reason,
            )
            return
        self._decisions_this_cycle = 0
        self._max_decisions_this_cycle = maximum_decisions
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
        return {
            "platform": runtime.plugin.name,
            "cash": round(runtime.state.cash, 6),
            "exposure": round(runtime.state.exposure, 6),
            "equity": round(runtime.state.equity, 6),
            "risk_metrics": {
                key: round(value, 6)
                for key, value in sorted(runtime.state.risk_metrics.items())
            },
            "positions": len(runtime.state.positions),
            "orders": len(runtime.state.orders),
            "halted": runtime.state.halted,
            "halt_reason": runtime.state.halt_reason,
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
            "aggregate_risk_metrics": self._aggregate_risk_metrics(),
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

    def _aggregate_risk_metrics(self) -> dict[str, float]:
        totals: dict[str, float] = {}
        for runtime in self.platforms.values():
            for key, value in runtime.state.risk_metrics.items():
                totals[key] = totals.get(key, 0.0) + float(value)
        return {
            key: round(value, 6) for key, value in sorted(totals.items())
        }

    def close(self) -> None:
        """Release session and plugin-owned resources for this engine instance."""
        try:
            self.memory.close()
        finally:
            if self._owns_catalog:
                self.plugin_catalog.shutdown()
