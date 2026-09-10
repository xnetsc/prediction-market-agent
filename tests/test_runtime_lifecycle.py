from __future__ import annotations

import threading
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from prediction_market_agent.core.config import Config
from prediction_market_agent.plugin_system.discovery import (
    PluginReadiness,
    PluginRuntime,
    PluginSpec,
)
from prediction_market_agent.plugin_system.managed_config import ManagedRuntimeConfig
from prediction_market_agent.plugins.api._binance.runtime import BinanceEventLoop
from prediction_market_agent.plugins.api._polymarket.runtime import PolymarketEventLoop
from prediction_market_agent.runtime.controller import RobotRuntimeManager


class PlatformOwnedLoopTests(unittest.TestCase):
    def test_builtin_platform_loops_run_immediately_and_stop_without_waiting_interval(self) -> None:
        settings = SimpleNamespace(
            scan_interval_seconds=3600,
            error_backoff_seconds=30,
            error_backoff_max_seconds=900,
            max_topics_per_cycle=3,
            max_decisions_per_cycle=2,
            topic_page_size=100,
        )
        for runtime_type in (BinanceEventLoop, PolymarketEventLoop):
            with self.subTest(runtime=runtime_type.__name__):
                called = threading.Event()
                values: list[tuple[int, int]] = []
                runtime = runtime_type(lambda: settings, lambda topics, page: (topics, page))

                def submit_scan(topics, decisions: int) -> None:
                    values.append((topics, decisions))
                    called.set()

                runtime.start({"submit_scan": submit_scan})
                self.assertTrue(called.wait(1), "first plugin-owned cycle did not start")
                runtime.stop()
                self.assertEqual(values, [((3, 100), 2)])
                self.assertFalse(runtime.status()["running"])


class RuntimeManagerTests(unittest.TestCase):
    @staticmethod
    def _config() -> Config:
        return Config(
            application_config_file=Path("/tmp/application.json"),
            management_file=Path("/tmp/management.json"),
            decision_providers=("provider",),
            market_api_plugins=("platform",),
            decision_strategy_name="strategy",
        )

    def test_ready_platform_runtime_is_started_by_callback_without_host_loop(self) -> None:
        starts: list[dict[str, object]] = []
        stops: list[bool] = []
        runtime = PluginRuntime(
            lambda services: starts.append(services),
            lambda: stops.append(True),
            lambda: {"running": bool(starts) and not stops},
        )
        specs = {
            ("api", "platform"): PluginSpec(
                "api", "platform", "platform", "/tmp/platform.py", lambda config: None,
                None, lambda: None, lambda: PluginReadiness(True), runtime,
            ),
            ("decision_provider", "provider"): PluginSpec(
                "decision_provider", "provider", "provider", "/tmp/provider.py",
                lambda config: None, None, lambda: None,
            ),
            ("decision_strategy", "strategy"): PluginSpec(
                "decision_strategy", "strategy", "strategy", "/tmp/strategy.py",
                lambda config: None, None, lambda: None,
            ),
        }

        class Catalog:
            def get(self, kind, name):
                return specs[(kind, name)]

            def specs(self, kind):
                return tuple(spec for (item_kind, _), spec in specs.items() if item_kind == kind)

            def shutdown(self):
                return None

        class Engine:
            def __init__(self, config, *, catalog):
                self.config = config
                self.catalog = catalog
                self.calls = []

            def run_platform_once(self, platform, topics, decisions):
                self.calls.append((platform, topics, decisions))
                return {}

            def process_platform_scan(self, platform, topics, decisions):
                self.calls.append((platform, topics, decisions))
                return {}

            def close(self):
                return None

        managed = ManagedRuntimeConfig(Path("/tmp/management.json"))
        manager = RobotRuntimeManager(Path("/tmp/application.json"))
        with patch(
            "prediction_market_agent.runtime.controller.Config.load",
            return_value=self._config(),
        ), patch(
            "prediction_market_agent.runtime.controller.ManagedRuntimeConfig.load",
            return_value=managed,
        ), patch(
            "prediction_market_agent.runtime.controller.load_plugin_catalog",
            return_value=Catalog(),
        ), patch(
            "prediction_market_agent.runtime.controller.TradingEngine", Engine
        ):
            status = manager.reconcile()
            self.assertTrue(status["running"])
            self.assertEqual(len(starts), 1)
            starts[0]["submit_scan"]((), 2)
            self.assertEqual(manager._engine.calls, [("platform", (), 2)])
            self.assertEqual(manager.status()["event_loop"]["processed"], 1)
            manager.stop()
        self.assertTrue(stops)

    def test_unready_or_paused_platform_is_not_started(self) -> None:
        for readiness, paused in (
            (PluginReadiness(False, ("missing token",)), ()),
            (PluginReadiness(True), ("platform",)),
        ):
            with self.subTest(readiness=readiness.ready, paused=bool(paused)):
                starts: list[object] = []
                api = PluginSpec(
                    "api", "platform", "platform", "/tmp/platform.py", lambda config: None,
                    None, lambda: None, lambda: readiness,
                    PluginRuntime(lambda services: starts.append(services), lambda: None, lambda: {}),
                )
                provider = PluginSpec(
                    "decision_provider", "provider", "provider", "/tmp/provider.py",
                    lambda config: None, None, lambda: None,
                )
                strategy = PluginSpec(
                    "decision_strategy", "strategy", "strategy", "/tmp/strategy.py",
                    lambda config: None, None, lambda: None,
                )

                class Catalog:
                    def get(self, kind, name):
                        return {("api", "platform"): api, ("decision_provider", "provider"): provider,
                                ("decision_strategy", "strategy"): strategy}[(kind, name)]

                    def specs(self, kind):
                        return (api,) if kind == "api" else ()

                    def shutdown(self):
                        return None

                managed = ManagedRuntimeConfig(
                    Path("/tmp/management.json"), paused_platforms=paused
                )
                manager = RobotRuntimeManager(Path("/tmp/application.json"))
                with patch("prediction_market_agent.runtime.controller.Config.load", return_value=self._config()), patch(
                    "prediction_market_agent.runtime.controller.ManagedRuntimeConfig.load", return_value=managed
                ), patch("prediction_market_agent.runtime.controller.load_plugin_catalog", return_value=Catalog()):
                    status = manager.reconcile()
                    manager.stop()
                self.assertFalse(status["running"])
                self.assertEqual(starts, [])

    def test_unready_platform_does_not_prevent_another_ready_platform_starting(self) -> None:
        starts: list[str] = []
        ready = PluginSpec(
            "api", "ready", "ready", "/tmp/ready.py", lambda config: None,
            None, lambda: None, lambda: PluginReadiness(True),
            PluginRuntime(
                lambda services: starts.append(str(services["platform"])),
                lambda: None,
                lambda: {"running": bool(starts)},
            ),
        )
        incomplete = PluginSpec(
            "api", "incomplete", "incomplete", "/tmp/incomplete.py", lambda config: None,
            None, lambda: None,
            lambda: PluginReadiness(False, ("missing private setting",)),
            PluginRuntime(lambda services: None, lambda: None, lambda: {"running": False}),
        )
        provider = PluginSpec(
            "decision_provider", "provider", "provider", "/tmp/provider.py",
            lambda config: None, None, lambda: None,
        )
        strategy = PluginSpec(
            "decision_strategy", "strategy", "strategy", "/tmp/strategy.py",
            lambda config: None, None, lambda: None,
        )
        specs = {
            ("api", "ready"): ready,
            ("api", "incomplete"): incomplete,
            ("decision_provider", "provider"): provider,
            ("decision_strategy", "strategy"): strategy,
        }

        class Catalog:
            def get(self, kind, name):
                return specs[(kind, name)]

            def specs(self, kind):
                return (ready, incomplete) if kind == "api" else ()

            def shutdown(self):
                return None

        class Engine:
            def __init__(self, config, *, catalog):
                self.config = config

            def process_platform_scan(self, platform, topics, decisions):
                return {}

            def close(self):
                return None

        config = replace(
            self._config(), market_api_plugins=("ready", "incomplete")
        )
        manager = RobotRuntimeManager(Path("/tmp/application.json"))
        with patch(
            "prediction_market_agent.runtime.controller.Config.load", return_value=config
        ), patch(
            "prediction_market_agent.runtime.controller.ManagedRuntimeConfig.load",
            return_value=ManagedRuntimeConfig(Path("/tmp/management.json")),
        ), patch(
            "prediction_market_agent.runtime.controller.load_plugin_catalog",
            return_value=Catalog(),
        ), patch(
            "prediction_market_agent.runtime.controller.TradingEngine", Engine
        ):
            status = manager.reconcile()
            self.assertEqual(starts, ["ready"])
            self.assertTrue(status["platforms"]["ready"]["running"])
            self.assertFalse(status["platforms"]["incomplete"]["running"])
            manager.stop()


if __name__ == "__main__":
    unittest.main()
