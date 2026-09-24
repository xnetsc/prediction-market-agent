from __future__ import annotations

import logging
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..core.config import Config
from ..plugin_system.discovery import PluginCatalog, PluginReadiness, load_plugin_catalog
from ..plugin_system.managed_config import ManagedRuntimeConfig
from .decision_capacity import DecisionCapacityWatch
from .engine import TradingEngine
from .events import (
    PlatformDiscoveryEvent, PlatformReviewEvent, PlatformScanEvent,
    PlatformScreenEvent, RobotEventLoop,
)


LOGGER = logging.getLogger(__name__)

START_RETRY_SECONDS = 60
"""How soon a robot that should be running, but could not start, looks again.

What stops a start is usually settled somewhere this process is not told about: a model service
signed in through its own page, a quota window reopening. Waiting for the next settings save to
notice left the robot off after the reason was gone.
"""


class RobotRuntimeManager:
    """Reconcile readiness and pause state; schedules remain owned by API plugins."""

    def __init__(self, application_config_file: Path):
        self.application_config_file = application_config_file.expanduser().resolve()
        self._lock = threading.RLock()
        self._catalog: PluginCatalog | None = None
        self._engine: TradingEngine | None = None
        self._events: RobotEventLoop | None = None
        self._capacity: DecisionCapacityWatch | None = None
        self._retry: threading.Timer | None = None
        self._status: dict[str, Any] = {
            "running": False,
            "global_ready": False,
            "global_reasons": ["Runtime has not been evaluated"],
            "robot_paused": False,
            "platforms": {},
        }
        # The last answer status() managed to build, kept so that reading the state never has to
        # wait for whatever is holding the lock. Stopping the robot means waiting for a cycle that
        # may be inside a model call minutes long, and a console that cannot say so until that
        # finishes is a console that looks broken exactly when it has the most to report.
        self._snapshot: dict[str, Any] = dict(self._status)
        self._busy_since = 0.0
        self._reconcile_request_lock = threading.Lock()
        self._reconcile_generation = 0
        self._reconcile_thread: threading.Thread | None = None
        self._pause_requested = threading.Event()

    def record_saved_pause(self, paused: bool) -> None:
        """Close the business-event gate immediately after the pause is saved."""
        if paused:
            self._pause_requested.set()
            events = self._events
            if events is not None:
                events.request_pause()
        else:
            self._pause_requested.clear()

    @staticmethod
    def _safe_readiness(catalog: PluginCatalog, kind: str, name: str) -> PluginReadiness:
        try:
            return catalog.get(kind, name).readiness()
        except Exception as error:
            return PluginReadiness(False, (str(error),))

    def _stop_locked(self) -> None:
        catalog, engine, events = self._catalog, self._engine, self._events
        if events is not None:
            events.request_pause()
        capacity, self._capacity = self._capacity, None
        if capacity is not None:
            capacity.stop()
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
            self._busy_since = time.time()
            self._cancel_retry_locked()
            self._stop_locked()
            self._status["running"] = False

    def _cancel_retry_locked(self) -> None:
        timer, self._retry = self._retry, None
        if timer is not None:
            timer.cancel()
        self._status.pop("start_retry_at", None)

    def _retry_start(self, timer: threading.Timer) -> None:
        with self._lock:
            # Anything done since this was scheduled - a pause, a start, a saved setting - already
            # reconciled against the operator's latest choice, and replaced or cancelled this timer.
            if self._retry is not timer:
                return
            self._retry = None
            if self._status.get("running"):
                return
            try:
                self.reconcile()
            except Exception:
                LOGGER.exception("retrying the robot start failed")

    def reconcile(self, *, start_runtimes: bool = True) -> dict[str, Any]:
        """Bring the robot to the operator's saved choice, as far as conditions allow.

        The choice - paused or running, per platform - is read from where the operator saved it,
        every time. A pause that conditions forced (no model can answer, a service is signed out)
        is never written there, so whatever the operator chose last is what the robot returns to
        once the condition clears.
        """
        with self._lock:
            self._busy_since = time.time()
            self._cancel_retry_locked()
            status = self._reconcile_locked(start_runtimes=start_runtimes)
            wanted = not status.get("robot_paused") and any(
                not platform.get("paused") for platform in status.get("platforms", {}).values()
            )
            if start_runtimes and wanted and not status.get("running"):
                timer = threading.Timer(START_RETRY_SECONDS, lambda: self._retry_start(timer))
                timer.daemon = True
                self._retry = timer
                self._status["start_retry_at"] = int(time.time()) + START_RETRY_SECONDS
                timer.start()
                status = self.status()
            return status

    def reconcile_async(self) -> None:
        """Queue a reconcile without making an HTTP request wait for an active model call.

        Saving pause state is a small durable write.  Stopping the current runtime can take until
        an in-flight CLI invocation returns, so tying the two together made the Save button look
        dead for minutes.  Multiple saves collapse to the newest generation and are reconciled in
        order after whichever operation currently owns the runtime lock finishes.
        """
        with self._reconcile_request_lock:
            self._reconcile_generation += 1
            if self._reconcile_thread is not None and self._reconcile_thread.is_alive():
                return
            thread = threading.Thread(
                target=self._run_requested_reconcile,
                name="robot-runtime-reconcile",
                daemon=True,
            )
            self._reconcile_thread = thread
            thread.start()

    def _run_requested_reconcile(self) -> None:
        while True:
            with self._reconcile_request_lock:
                generation = self._reconcile_generation
            try:
                self.reconcile()
            except Exception:
                LOGGER.exception("background robot reconcile failed")
            with self._reconcile_request_lock:
                if generation == self._reconcile_generation:
                    self._reconcile_thread = None
                    return

    def _reconcile_locked(self, *, start_runtimes: bool = True) -> dict[str, Any]:
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
        }
        ready_strategy = ""
        for kind, names in (
            ("decision_strategy", (config.decision_strategy_name,)),
            ("research_tool", config.research_tool_plugins),
            ("risk", config.risk_plugins),
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
        )
        try:
            engine = TradingEngine(runtime_config, catalog=catalog)
            engine.stop_requested = self._pause_requested.is_set
            self._engine = engine
            if start_runtimes:
                def handle_business_event(event: Any) -> Any:
                    if self._pause_requested.is_set():
                        raise RuntimeError("Robot is pausing; no new business event may start")
                    if isinstance(event, PlatformDiscoveryEvent):
                        return engine.discover_platform_topics(event.platform)
                    if isinstance(event, PlatformReviewEvent):
                        return engine.review_due_platform(event.platform)
                    if isinstance(event, PlatformScreenEvent):
                        return engine.screen_pending_platform(event.platform)
                    return engine.process_platform_scan(
                        event.platform, event.topics
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
                        *,
                        platform: str = name,
                    ) -> dict[str, Any]:
                        return events.submit(
                            PlatformScanEvent(
                                platform=platform,
                                topics=tuple(topics),
                            )
                        )

                    def discover_markets(
                        *, platform: str = name
                    ) -> tuple[Any, ...]:
                        """Framework-owned discovery; the plugin only decides when to ask."""
                        return events.submit(
                            PlatformDiscoveryEvent(
                                platform=platform
                            )
                        )

                    def next_review_at(*, platform: str = name) -> int | None:
                        due_ms = engine.memory.next_market_review_at(platform=platform)
                        return due_ms // 1000 if due_ms is not None else None

                    def review_due(*, platform: str = name) -> dict[str, Any]:
                        return events.submit(PlatformReviewEvent(platform=platform))

                    def next_screening_at(*, platform: str = name) -> int | None:
                        if (not engine.evaluator.available
                                or not engine.discovery.screening_enabled()):
                            return None
                        due_ms = engine.memory.next_market_screening_at(platform=platform)
                        return due_ms // 1000 if due_ms is not None else None

                    def screen_pending(*, platform: str = name) -> dict[str, Any]:
                        return events.submit(PlatformScreenEvent(platform=platform))

                    def next_scan_delay(
                        minimum_seconds: int, *, platform: str = name
                    ) -> dict[str, Any]:
                        """What the agent asked for after its last look at this platform.

                        When to come back is a judgement about this venue right now - how fast
                        its prices move, whether a catalyst is due - and the agent is the only
                        thing here that has just read it. The plugin still owns the decision:
                        this is a request, its own interval is the floor, and a plugin that
                        does not ask never hears about it.
                        """
                        plan = engine.memory.survey_plan(platform)
                        requested = int(plan.get("next_scan_seconds", 0) or 0)
                        return {
                            "seconds": max(int(minimum_seconds), requested),
                            "requested_seconds": requested,
                            "minimum_seconds": int(minimum_seconds),
                            "reason": str(plan.get("reason", "")),
                        }

                    try:
                        spec.runtime.start(
                            {
                                "platform": name,
                                "submit_scan": submit_scan,
                                "discover_markets": discover_markets,
                                "next_scan_delay": next_scan_delay,
                                "next_review_at": next_review_at,
                                "review_due": review_due,
                                "next_screening_at": next_screening_at,
                                "screen_pending": screen_pending,
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
            if start_runtimes and started_platforms:
                # The one loop that has to keep running when nothing can answer, because it is
                # what tells the plugins standing down that they may start again. It watches
                # the robot; it must never be the reason the robot did not start, so a failure
                # here is reported and the run continues without it.
                try:
                    self._capacity = DecisionCapacityWatch(
                        engine.provider,
                        lambda: [
                            (name, catalog.get("api", name).runtime.notify)
                            for name in started_platforms
                            if catalog.get("api", name).runtime is not None
                        ],
                        probe=engine.provider_quality.capacity,
                    )
                    self._capacity.start()
                except Exception as error:
                    self._capacity = None
                    LOGGER.exception("decision capacity watch could not start")
                    self._status["global_reasons"].append(
                        f"AI 可用性监控未启动，平台不会在模型不可用时自动停扫：{error}"
                    )
            self._status["running"] = bool(start_runtimes)
            return self.status()
        except Exception as error:
            self._status["global_ready"] = False
            self._status["global_reasons"].append(str(error))
            self._stop_locked()
            for platform in self._status["platforms"].values():
                platform["running"] = False
            return self.status()

    def recheck_providers(self, name: str = "") -> dict[str, Any]:
        """Put cooled-down decision providers back in line immediately.

        Quota and entitlement change without telling the runtime. The operator is the one who knows
        a plan changed or credits were bought, so this exists for them to say so.
        """
        with self._lock:
            engine = self._engine
            capacity = self._capacity
        if engine is None:
            raise ValueError("机器人未运行，没有可重新检测的模型服务")
        cleared = engine.provider.health.recheck(name.strip())
        probed = engine.provider_quality.probe_recovering()
        # Said at once rather than at the watch's next turn: whoever pressed this is looking at
        # the screen, waiting to see the robot resume.
        reading = capacity.check() if capacity is not None else engine.provider_quality.capacity()
        return {"cleared": cleared, "probed": probed, "capacity": reading,
                "health": engine.provider_quality.manifest()}

    def set_model_selection_mode(self, mode: str) -> None:
        """Apply a saved routing preference without restarting an active cycle."""
        with self._lock:
            engine = self._engine
        if engine is not None:
            engine.provider.health.set_order_mode(mode)
            engine.evaluator.set_order_mode(mode)

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

    STATUS_WAIT_SECONDS = 0.5
    """How long reading the state waits for the lock before answering from the last snapshot.

    Long enough that an ordinary read - nothing else going on - takes the fresh path every time.
    Short enough that a console polling every few seconds never queues behind a restart: the
    requests used to pile up until the server ran out of worker threads and answered nothing at all.
    """

    def status(self) -> dict[str, Any]:
        if not self._lock.acquire(timeout=self.STATUS_WAIT_SECONDS):
            # Something long is holding it. Say what is known, and say that it is stale, rather
            # than joining the queue: the operator asked what the robot is doing, and "busy
            # changing" is an answer where a hung request is not.
            stale = dict(self._snapshot)
            stale["settling"] = True
            stale["settling_seconds"] = int(max(0.0, time.time() - self._busy_since))
            stale["settling_reason"] = (
                "正在按你的设置重启机器人。上一轮可能正卡在一次模型调用里，要等它自己结束；"
                "这里显示的是它开始重启前的状态。"
            )
            return self._with_saved_control(stale)
        try:
            return self._with_saved_control(self._status_locked())
        finally:
            self._lock.release()

    def _with_saved_control(self, status: dict[str, Any]) -> dict[str, Any]:
        """Show the durable operator choice even while a previous reconcile holds the lock."""
        try:
            config = Config.load(self.application_config_file)
            managed = ManagedRuntimeConfig.load(config.management_file)
        except Exception:
            LOGGER.exception("could not read saved runtime control")
            return status
        result = dict(status)
        previous_pause = bool(result.get("robot_paused"))
        result["robot_paused"] = managed.robot_paused
        platforms = {name: dict(value) for name, value in result.get("platforms", {}).items()}
        for name, platform in platforms.items():
            platform["paused"] = name in managed.paused_platforms
        result["platforms"] = platforms
        if previous_pause != managed.robot_paused or (managed.robot_paused and result.get("running")):
            result["settling"] = True
            result["settling_reason"] = (
                "暂停已保存，正在等待当前调用结束；不会接收新的业务事件。"
                if managed.robot_paused else "恢复运行已保存，正在启动已就绪的平台。"
            )
        return result

    def _status_locked(self) -> dict[str, Any]:
        """The fresh answer, built while holding the lock, and kept as the snapshot."""
        engine = self._engine
        result = {
            **self._status,
            "global_reasons": list(self._status.get("global_reasons", [])),
            "platforms": {
                name: dict(value)
                for name, value in self._status.get("platforms", {}).items()
            },
        }
        if self._capacity is not None:
            result["decision_capacity"] = self._capacity.state()
        if engine is not None:
            try:
                result["decision_provider_health"] = engine.provider_quality.manifest()
            except Exception:
                LOGGER.exception("provider health manifest failed")
            evaluator_pool = getattr(engine, "evaluator", None)
            result["evaluator_benchmarks"] = {
                evaluator.name: evaluator.benchmark_status
                for evaluator in getattr(evaluator_pool, "evaluators", ())
                if hasattr(evaluator, "benchmark_status")
            }
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
        self._snapshot = result
        return result
