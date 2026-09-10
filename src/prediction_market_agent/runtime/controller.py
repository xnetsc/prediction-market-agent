from __future__ import annotations

import logging
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..core.config import Config
from ..plugin_system.discovery import PluginCatalog, PluginReadiness, load_plugin_catalog
from ..plugin_system.managed_config import ManagedRuntimeConfig
from .engine import TradingEngine
from .events import PlatformDiscoveryEvent, PlatformScanEvent, RobotEventLoop


LOGGER = logging.getLogger(__name__)


class RobotRuntimeManager:
    """Reconcile readiness and pause state; schedules remain owned by API plugins."""

    def __init__(self, application_config_file: Path):
        self.application_config_file = application_config_file.expanduser().resolve()
        self._lock = threading.RLock()
        self._catalog: PluginCatalog | None = None
        self._engine: TradingEngine | None = None
        self._events: RobotEventLoop | None = None
        self._status: dict[str, Any] = {
            "running": False,
            "global_ready": False,
            "global_reasons": ["Runtime has not been evaluated"],
            "robot_paused": False,
            "platforms": {},
        }

    @staticmethod
    def _safe_readiness(catalog: PluginCatalog, kind: str, name: str) -> PluginReadiness:
        try:
            return catalog.get(kind, name).readiness()
        except Exception as error:
            return PluginReadiness(False, (str(error),))

    def _stop_locked(self) -> None:
        catalog, engine, events = self._catalog, self._engine, self._events
        self._catalog = None
        self._engine = None
        self._events = None
        if catalog is not None:
            for spec in reversed(catalog.specs("api")):
                if spec.runtime is not None:
                    try:
                        spec.runtime.stop()
                    except Exception:
                        LOGGER.exception("failed to stop API plugin runtime %s", spec.name)
        if engine is not None:
            if events is not None:
                events.stop()
            engine.close()
        elif events is not None:
            events.stop()
        if catalog is not None:
            catalog.shutdown()

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()
            self._status["running"] = False

    def reconcile(self, *, start_runtimes: bool = True) -> dict[str, Any]:
        with self._lock:
            self._stop_locked()
            try:
                config = Config.load(self.application_config_file)
                managed = ManagedRuntimeConfig.load(config.management_file)
                catalog = load_plugin_catalog(config)
            except Exception as error:
                self._status = {
                    "running": False,
                    "global_ready": False,
                    "global_reasons": [str(error)],
                    "global_plugins": {},
                    "decision_providers": {},
                    "robot_paused": False,
                    "platforms": {},
                }
                return self.status()
            self._catalog = catalog
            global_reasons = list(config.robot_readiness_errors())

            provider_states = {
                name: self._safe_readiness(catalog, "decision_provider", name)
                for name in config.decision_providers
            }
            if config.decision_providers and not any(
                state.ready for state in provider_states.values()
            ):
                global_reasons.append("No enabled decision provider is ready")

            global_plugins: dict[str, dict[str, Any]] = {}
            ready_optional: dict[str, list[str]] = {
                "research_tool": [],
                "risk": [],
                "hook": [],
            }
            ready_strategy = ""
            for kind, names in (
                ("decision_strategy", (config.decision_strategy_name,)),
                ("research_tool", config.research_tool_plugins),
                ("risk", config.risk_plugins),
                ("hook", config.hook_plugins),
            ):
                for name in names:
                    if not name:
                        continue
                    readiness = self._safe_readiness(catalog, kind, name)
                    global_plugins[f"{kind}:{name}"] = readiness.manifest()
                    if readiness.ready:
                        if kind == "decision_strategy":
                            ready_strategy = name
                        else:
                            ready_optional[kind].append(name)

            platform_states: dict[str, dict[str, Any]] = {}
            configured_platforms: list[str] = []
            active_platforms: list[str] = []
            for name in config.market_api_plugins:
                readiness = self._safe_readiness(catalog, "api", name)
                try:
                    spec = catalog.get("api", name)
                except ValueError:
                    spec = None
                if readiness.ready and (spec is None or spec.runtime is None):
                    readiness = PluginReadiness(
                        False,
                        ("API plugin does not provide a runtime lifecycle",),
                    )
                paused = name in managed.paused_platforms
                platform_states[name] = {
                    "ready": readiness.ready,
                    "startup_reasons": list(readiness.reasons),
                    "paused": paused,
                    "running": False,
                    "runtime": None,
                }
                if readiness.ready:
                    configured_platforms.append(name)
                if readiness.ready and not paused:
                    active_platforms.append(name)

            if config.market_api_plugins and not configured_platforms:
                global_reasons.append("No enabled API platform is ready to start")

            global_ready = not global_reasons
            self._status = {
                "running": False,
                "global_ready": global_ready,
                "global_reasons": global_reasons,
                "global_plugins": global_plugins,
                "decision_providers": {
                    name: state.manifest() for name, state in provider_states.items()
                },
                "robot_paused": managed.robot_paused,
                "platforms": platform_states,
            }
            if managed.robot_paused or not global_ready or not active_platforms:
                return self.status()

            runtime_config = replace(
                config,
                decision_providers=tuple(
                    name for name, state in provider_states.items() if state.ready
                ),
                market_api_plugins=tuple(configured_platforms),
                decision_strategy_name=ready_strategy,
                research_tool_plugins=tuple(ready_optional["research_tool"]),
                risk_plugins=tuple(ready_optional["risk"]),
                hook_plugins=tuple(ready_optional["hook"]),
            )
            try:
                engine = TradingEngine(runtime_config, catalog=catalog)
                self._engine = engine
                if start_runtimes:
                    def handle_business_event(event: Any) -> Any:
                        if isinstance(event, PlatformDiscoveryEvent):
                            return engine.discover_platform_topics(
                                event.platform, event.maximum_topics
                            )
                        return engine.process_platform_scan(
                            event.platform, event.topics, event.maximum_decisions
                        )

                    events = RobotEventLoop(handle_business_event)
                    events.start()
                    self._events = events
                    started_platforms: list[str] = []
                    for name in active_platforms:
                        spec = catalog.get("api", name)
                        if spec.runtime is None:
                            platform_states[name]["startup_reasons"] = [
                                "API plugin does not provide a runtime lifecycle"
                            ]
                            continue

                        def submit_scan(
                            topics: tuple[Any, ...],
                            maximum_decisions: int,
                            *,
                            platform: str = name,
                        ) -> dict[str, Any]:
                            return events.submit(
                                PlatformScanEvent(
                                    platform=platform,
                                    topics=tuple(topics),
                                    maximum_decisions=maximum_decisions,
                                )
                            )

                        def discover_markets(
                            maximum_topics: int, *, platform: str = name
                        ) -> tuple[Any, ...]:
                            """Framework-owned discovery; the plugin only decides when to ask."""
                            return events.submit(
                                PlatformDiscoveryEvent(
                                    platform=platform, maximum_topics=maximum_topics
                                )
                            )

                        try:
                            spec.runtime.start(
                                {
                                    "platform": name,
                                    "submit_scan": submit_scan,
                                    "discover_markets": discover_markets,
                                }
                            )
                            runtime_status = spec.runtime.status()
                            if not runtime_status.get("running", False):
                                raise RuntimeError(
                                    "API plugin runtime returned without entering running state"
                                )
                            platform_states[name]["running"] = True
                            platform_states[name]["runtime"] = runtime_status
                            started_platforms.append(name)
                        except Exception as error:
                            platform_states[name]["startup_reasons"] = [
                                f"Runtime failed to start: {error}"
                            ]
                            try:
                                spec.runtime.stop()
                            except Exception:
                                LOGGER.exception(
                                    "failed to clean up API plugin runtime %s", name
                                )
                    if not started_platforms:
                        self._status["global_ready"] = False
                        self._status["global_reasons"].append(
                            "No enabled API platform runtime started successfully"
                        )
                        if events is not None:
                            events.stop()
                            self._events = None
                        engine.close()
                        self._engine = None
                        return self.status()
                self._status["running"] = bool(start_runtimes)
                return self.status()
            except Exception as error:
                self._status["global_ready"] = False
                self._status["global_reasons"].append(str(error))
                self._stop_locked()
                for platform in self._status["platforms"].values():
                    platform["running"] = False
                return self.status()

    def export_strategies(self, lane: str = "") -> dict[str, Any]:
        """Report what the discovery and decision strategies currently send to the model."""
        from .strategy_export import export_strategies

        with self._lock:
            engine = self._engine
            config = Config.load(self.application_config_file)
        return export_strategies(config, engine=engine, lane=lane)

    def execute_once(self) -> dict[str, Any]:
        status = self.reconcile(start_runtimes=False)
        with self._lock:
            if self._engine is None:
                return {"executed": False, "runtime": status}
            try:
                result = self._engine.run_once()
                return {"executed": True, "result": result, "runtime": self.status()}
            finally:
                self._stop_locked()
                self._status["running"] = False

    def status(self) -> dict[str, Any]:
        with self._lock:
            engine = self._engine
            result = {
                **self._status,
                "global_reasons": list(self._status.get("global_reasons", [])),
                "platforms": {
                    name: dict(value)
                    for name, value in self._status.get("platforms", {}).items()
                },
            }
            if engine is not None:
                try:
                    result["decision_provider_health"] = engine.provider_quality.manifest()
                except Exception:
                    LOGGER.exception("provider health manifest failed")
            if self._catalog is not None:
                for name, platform in result["platforms"].items():
                    try:
                        spec = self._catalog.get("api", name)
                    except ValueError:
                        continue
                    if spec.runtime is not None:
                        platform["runtime"] = spec.runtime.status()
                        platform["running"] = bool(platform["runtime"].get("running"))
                result["running"] = any(
                    item.get("running", False) for item in result["platforms"].values()
                )
            result["event_loop"] = (
                self._events.status() if self._events is not None else {"running": False}
            )
            return result
