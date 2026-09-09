from __future__ import annotations

import logging
import time
from typing import Any

from ..core.config import Config
from ..plugin_system.contracts import PredictionMarketApiPlugin, Topic
from .actions import ExecutionActionsMixin
from .bootstrap import PlatformRuntime, bootstrap_engine
from .broker import ExecutionError
from .evaluation import MarketEvaluationMixin
from .memory import SessionMemory

LOGGER = logging.getLogger(__name__)


class TradingEngine(MarketEvaluationMixin, ExecutionActionsMixin):
    """Coordinate one decision cycle across all enabled market plugins."""

    def __init__(self, config: Config):
        self.config = config
        self.memory = SessionMemory(config.session_db)
        try:
            components = bootstrap_engine(config)
        except Exception:
            self.memory.close()
            raise
        self.plugin_catalog = components.plugin_catalog
        self.decision_strategy = components.decision_strategy
        self.hooks = components.hooks
        self.hook_plugin_status = components.hook_plugin_status
        self.risk = components.risk
        self.api_registry = components.api_registry
        self.platforms = components.platforms
        self.global_risk = components.global_risk
        self.provider = components.provider
        self.research_contributions = components.research_contributions
        self._decisions_this_cycle = 0

    @staticmethod
    def _all_topics(
        plugin: PredictionMarketApiPlugin, maximum: int = 500
    ) -> list[Topic]:
        topics: list[Topic] = []
        offset = 0
        while len(topics) < maximum:
            page = plugin.list_topics(
                offset=offset, limit=min(100, maximum - len(topics))
            )
            topics.extend(page.topics)
            if not page.has_more or not page.topics:
                break
            offset = page.next_offset
        return topics

    def run_once(self) -> dict[str, float | int | bool | str]:
        self.global_risk.refresh_halt()
        topics_by_platform: dict[str, list[Topic]] = {}
        for runtime in self.platforms.values():
            if runtime.state.halted:
                LOGGER.warning(
                    "%s account halted: %s",
                    runtime.plugin.name,
                    runtime.state.halt_reason,
                )
                continue
            runtime.plugin.sync_time()
            topics_by_platform[runtime.plugin.name] = self._all_topics(runtime.plugin)
        for runtime in self.platforms.values():
            if runtime.plugin.name not in topics_by_platform:
                continue
            self._decisions_this_cycle = 0
            topics = topics_by_platform[runtime.plugin.name]
            eligible = self.decision_strategy.select_topics(topics)[
                : self.config.max_topics_per_cycle
            ]
            LOGGER.info(
                "platform=%s scanned=%d candidates=%d provider=%s",
                runtime.plugin.name,
                len(topics),
                len(eligible),
                self.provider.name,
            )
            for topic in eligible:
                if self._decisions_this_cycle >= self.config.max_decisions_per_cycle:
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
        return self.status()

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

    def run_forever(self) -> None:
        while True:
            if self.config.run_until_epoch and time.time() >= self.config.run_until_epoch:
                LOGGER.info("configured run-until time reached")
                break
            try:
                status = self.run_once()
                LOGGER.info("status=%s", status)
            except KeyboardInterrupt:
                raise
            except Exception:
                LOGGER.exception("cycle failed; state was not discarded")
            sleep_for = self.config.interval_seconds
            if self.config.run_until_epoch:
                sleep_for = min(
                    sleep_for,
                    max(0, self.config.run_until_epoch - int(time.time())),
                )
            if sleep_for > 0:
                time.sleep(sleep_for)

    def close(self) -> None:
        """Release session and plugin-owned resources for this engine instance."""
        try:
            self.memory.close()
        finally:
            self.plugin_catalog.shutdown()
