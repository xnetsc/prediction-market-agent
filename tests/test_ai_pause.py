"""When no model can answer, the robot is paused: nothing collected, nothing recorded, and it says why.

A weekly limit the classifier did not recognise let the robot come back every minute through a
week-long outage, pulling markets and writing a failed record each time - 461 of them. These tests
pin down the pieces that stop that: a limit is recognised, its stated end is waited out, recovery is
confirmed with the client's free answer rather than a round, no round starts meanwhile, the overview
says so, and the operator's own pause or start is what the robot returns to afterwards.
"""
from __future__ import annotations

import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from prediction_market_agent.agent.decision import (
    AgentRunResult,
    DecisionProviderError,
    FallbackDecisionProvider,
)
from prediction_market_agent.agent.provider_health import (
    COOLDOWN_CEILING,
    ProviderHealthRegistry,
    classify_error,
    parse_reset_time,
)
from prediction_market_agent.runtime.memory import SessionMemory
from prediction_market_agent.runtime.provider_quality import ProviderQuality


class Client:
    """A decision provider whose client can say, for free, whether its account has quota."""

    def __init__(self, name: str, *, error: str | None = None, quota: dict | None = None):
        self.name = name
        self.error = error
        self.calls = 0
        self.reading = quota
        self.backend = SimpleNamespace(quota=lambda: self.reading)

    def run(self, payload, **options):
        self.calls += 1
        if self.error:
            raise DecisionProviderError(self.error, "{}")
        return AgentRunResult(value={"ok": True}, raw_output="{}", provider=self.name, research_trace=[])


def utc(*parts: int) -> float:
    return datetime(*parts, tzinfo=timezone.utc).timestamp()


class StatedResetTests(unittest.TestCase):
    NOW = utc(2026, 9, 17, 13, 14)

    def test_the_formats_the_clients_actually_print(self) -> None:
        at = lambda text: parse_reset_time(text, self.NOW)  # noqa: E731
        self.assertEqual(at("You've hit your session limit · resets 5:30pm (UTC)"), utc(2026, 9, 17, 17, 30))
        self.assertEqual(
            at("You've hit your weekly limit · resets 1pm (UTC)"), utc(2026, 9, 18, 13, 0),
            "a time already past today means its next occurrence",
        )
        self.assertEqual(at("resets Sep 21, 1pm (UTC)"), utc(2026, 9, 21, 13, 0))
        self.assertEqual(at("Rate limited, try again in 20 minutes"), self.NOW + 1200)
        self.assertEqual(
            at("ERROR: You've hit your usage limit. ... or try again at Sep 19th, 2026 8:13 AM."),
            datetime(2026, 9, 19, 8, 13).timestamp(),
            "no zone printed: the client runs on this machine, so it is this machine's time",
        )

    def test_what_is_not_a_time_is_not_guessed(self) -> None:
        self.assertIsNone(parse_reset_time("You've hit your usage limit; try again Sep 15th", self.NOW))
        self.assertIsNone(parse_reset_time("HTTP 429 rate limit", self.NOW))
        self.assertIsNone(
            parse_reset_time("resets Sep 1, 1pm (UTC)", self.NOW), "a year away is a misread, not a plan"
        )

    def test_every_metered_window_is_a_limit(self) -> None:
        for message in (
            "Claude failed: success / api_error / You've hit your weekly limit · resets 1pm (UTC)",
            "You've hit your session limit · resets 12:20pm (UTC)",
            "You've hit your Opus limit · resets Sep 20, 3pm (UTC)",
        ):
            with self.subTest(message=message):
                self.assertEqual(classify_error(message), "rate_limit")


class WaitingOutALimitTests(unittest.TestCase):
    def test_a_stated_end_is_waited_out_even_past_the_ceiling(self) -> None:
        registry = ProviderHealthRegistry(("claude",))
        later = time.time() + 3 * 86400
        registry.record_failure("claude", "Current week 已用 100%", kind="rate_limit", recovers_at=later)
        state = registry.state("claude")
        self.assertAlmostEqual(state.cooldown_until, later, delta=1)
        self.assertEqual(int(state.recovers_at), int(later))
        self.assertFalse(state.ready(time.time() + COOLDOWN_CEILING + 60))

    def test_the_end_is_read_from_the_error_itself(self) -> None:
        registry = ProviderHealthRegistry(("claude",))
        registry.record_failure("claude", "You've hit your weekly limit · resets Sep 21, 1pm (UTC)")
        self.assertGreater(registry.state("claude").recovers_at, time.time())

    def test_the_operator_can_still_bring_it_back_early(self) -> None:
        registry = ProviderHealthRegistry(("claude",))
        registry.record_failure("claude", "limit", kind="rate_limit", recovers_at=time.time() + 86400)
        registry.recheck("claude")
        state = registry.state("claude")
        self.assertTrue(state.available_at(time.time()))
        self.assertEqual(state.recovers_at, 0.0)

    def test_a_lapsed_cooldown_is_not_a_recovery(self) -> None:
        registry = ProviderHealthRegistry(("claude",))
        registry.record_failure("claude", "HTTP 429 rate limit")
        registry.state("claude").cooldown_until = 0.0
        self.assertFalse(registry.state("claude").ready(time.time()), "only a check says it is back")

    def test_nothing_is_asked_while_no_provider_is_ready(self) -> None:
        a, b = Client("a", error="HTTP 429 rate limit"), Client("b", error="HTTP 429 rate limit")
        provider = FallbackDecisionProvider([a, b], {}, ("a", "b"))
        with self.assertRaises(DecisionProviderError):
            provider.run({}, schema={}, schema_name="s", mission="m")
        self.assertEqual((a.calls, b.calls), (1, 1))
        with self.assertRaises(DecisionProviderError) as caught:
            provider.run({}, schema={}, schema_name="s", mission="m")
        self.assertEqual((a.calls, b.calls), (1, 1), "a known outage must not be asked again")
        self.assertIn("No decision provider is ready", str(caught.exception))


class FreeRecoveryCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.memory = SessionMemory(Path(directory.name) / "s.sqlite3")
        self.addCleanup(self.memory.close)

    def test_recovery_is_confirmed_by_the_client_not_by_a_request(self) -> None:
        client = Client("claude", error="You've hit your weekly limit", quota={"available": False})
        provider = FallbackDecisionProvider([client], {}, ("claude",))
        with self.assertRaises(DecisionProviderError):
            provider.run({}, schema={}, schema_name="s", mission="m")
        provider.health.state("claude").cooldown_until = 0.0
        client.reading = {"available": True}
        quality = ProviderQuality(memory=self.memory, provider=provider)
        self.assertEqual(quality.probe_recovering(), {"claude": "recovered"})
        self.assertEqual(client.calls, 1, "the client answered for free; no request was spent")
        self.assertTrue(provider.health.state("claude").ready(time.time()))
        self.assertGreater(
            provider.health.state("claude").consecutive_failures, 0,
            "only a real answer resets the streak, so a client that misreports is still paced",
        )

    def test_still_exhausted_moves_the_next_check_to_when_it_says(self) -> None:
        later = time.time() + 7200
        client = Client("claude", error="usage limit",
                        quota={"available": False, "kind": "rate_limit", "recovers_at": later, "detail": "本周已用 100%"})
        provider = FallbackDecisionProvider([client], {}, ("claude",))
        with self.assertRaises(DecisionProviderError):
            provider.run({}, schema={}, schema_name="s", mission="m")
        provider.health.state("claude").cooldown_until = 0.0
        ProviderQuality(memory=self.memory, provider=provider).probe_recovering()
        state = provider.health.state("claude")
        self.assertAlmostEqual(state.cooldown_until, later, delta=1)
        self.assertEqual(state.attempts, 1, "asking the client is not an attempt")

    def test_an_exhausted_account_is_known_before_the_first_round(self) -> None:
        """Health is kept in memory; a restart must not relearn an outage by failing a round."""
        later = time.time() + 8100
        client = Client("claude", quota={"available": False, "kind": "rate_limit", "recovers_at": later,
                                         "detail": "Current week 已用 100%"})
        provider = FallbackDecisionProvider([client], {}, ("claude",))
        reading = ProviderQuality(memory=self.memory, provider=provider).capacity()
        self.assertFalse(reading["available"])
        self.assertEqual(reading["providers"]["claude"]["kind"], "rate_limit")
        self.assertEqual(reading["providers"]["claude"]["recovers_at"], int(later))
        self.assertEqual(client.calls, 0)

    def test_a_client_that_cannot_say_leaves_capacity_as_recorded(self) -> None:
        client = Client("compatible")
        client.backend = SimpleNamespace()
        provider = FallbackDecisionProvider([client], {}, ("compatible",))
        self.assertTrue(ProviderQuality(memory=self.memory, provider=provider).capacity()["available"])


class NoWorkWithoutAModelTests(unittest.TestCase):
    """The plugins stand down when told; this is the check that does not depend on them."""

    def _engine(self, answers):
        from prediction_market_agent.runtime.engine import TradingEngine

        engine = TradingEngine.__new__(TradingEngine)
        replies = iter(answers)
        engine.provider_quality = SimpleNamespace(
            capacity=lambda: {"available": next(replies), "waiting": {"claude": "rate_limit"}},
            review=lambda: None,
        )
        engine.log = []
        engine.config = SimpleNamespace(decision_max_attempts=100, decision_max_cycle_seconds=180)
        engine._plan_outcomes = lambda runtime, topics: [
            (topic, None, None, None, 1.0) for topic in topics
        ]
        engine._evaluate_outcome = lambda runtime, topic, *_args: (
            setattr(engine, "_decisions_this_cycle", engine._decisions_this_cycle + 1),
            engine.log.append(("decide", topic)),
        )
        engine._settle_open_positions = lambda runtime: engine.log.append(("settle",))
        engine._resume_funded_decisions = lambda runtime: engine.log.append(("resume",))
        engine.decision_strategy = SimpleNamespace(select_topics=list)
        engine.provider = SimpleNamespace(name="claude")
        engine.operator_instructions = SimpleNamespace()
        engine._catch_up_on_notes = lambda runtime: None
        engine._apply_operator_instructions = lambda runtime, moment: None
        engine.discovery = SimpleNamespace(
            review=lambda: None, discover=lambda **kw: engine.log.append(("discover",)) or ["t"]
        )
        engine.decision_evolution = SimpleNamespace(review=lambda: None)
        return engine

    @staticmethod
    def _runtime():
        return SimpleNamespace(
            plugin=SimpleNamespace(name="polymarket", sync_time=lambda: None),
            store=SimpleNamespace(save=lambda state: None), state=None,
        )

    def test_no_markets_are_pulled_and_no_round_is_started(self) -> None:
        engine = self._engine([False, False])
        self.assertEqual(engine._collect_platform_topics(self._runtime()), [])
        engine._process_platform_topics(self._runtime(), ["t1", "t2"])
        self.assertEqual(engine.log, [("settle",)], "only what is already held is still settled")

    def test_a_limit_reached_mid_scan_stops_the_rest_of_it(self) -> None:
        engine = self._engine([True, True, False])
        engine._process_platform_topics(self._runtime(), ["t1", "t2", "t3"])
        self.assertEqual(
            [entry for entry in engine.log if entry[0] == "decide"], [("decide", "t1")]
        )


class CapacityWatchTests(unittest.TestCase):
    def _provider(self, names=("claude",)):
        health = ProviderHealthRegistry(names)
        return SimpleNamespace(available_names=names, health=health, unavailable={}), health

    def test_recovery_is_noticed_while_the_platforms_stand_down(self) -> None:
        from prediction_market_agent.runtime.decision_capacity import DecisionCapacityWatch

        provider, health = self._provider()
        health.record_failure("claude", "usage limit")
        told = []
        client_says_back = []

        def probe():
            if client_says_back:
                health.record_probe_success("claude")

        watch = DecisionCapacityWatch(provider, lambda: [("p", told.append)], probe=probe)
        self.assertFalse(watch.check()["available"])
        client_says_back.append(True)
        self.assertTrue(watch.check()["available"])
        self.assertEqual([message["available"] for message in told], [False, True])

    def test_a_person_can_read_why_and_until_when(self) -> None:
        from prediction_market_agent.runtime.decision_capacity import capacity_reading

        provider, health = self._provider()
        later = time.time() + 3600
        health.record_failure("claude", "limit", kind="rate_limit", recovers_at=later)
        reading = capacity_reading(provider)
        self.assertFalse(reading["available"])
        self.assertEqual(reading["waiting"], {"claude": "rate_limit"}, "plugins keep their one-line reason")
        self.assertEqual(reading["providers"]["claude"]["recovers_at"], int(later))
        health.state("claude").cooldown_until = 0.0
        self.assertTrue(capacity_reading(provider)["providers"]["claude"]["confirming"])


class OverviewTests(unittest.TestCase):
    def _state(self, runtime):
        from prediction_market_agent.runtime.setup_guide import setup_guide

        return setup_guide(runtime, {"plugins": {}}, automatic_start=True)

    def test_holding_platforms_are_shown_as_a_pause_with_its_reasons(self) -> None:
        capacity = {"available": False, "providers": {"claude": {"ready": False, "kind": "rate_limit"}}}
        guide = self._state({"running": True, "platforms": {"polymarket": {"running": True}},
                             "decision_capacity": capacity})
        self.assertEqual(guide["state"], "ai_paused")
        self.assertEqual(guide["decision_capacity"], capacity)

    def test_the_operators_pause_is_what_is_shown_when_they_paused(self) -> None:
        guide = self._state({"running": False, "robot_paused": True, "platforms": {},
                             "decision_capacity": {"available": False}})
        self.assertEqual(guide["state"], "paused")


class OperatorsLatestChoiceTests(unittest.TestCase):
    """A pause that conditions forced is never saved as the operator's choice."""

    def _manager(self, statuses):
        from prediction_market_agent.runtime.controller import RobotRuntimeManager

        manager = RobotRuntimeManager.__new__(RobotRuntimeManager)
        manager._lock = threading.RLock()
        manager._retry = None
        manager._status = {}
        manager._capacity = manager._engine = manager._events = manager._catalog = None
        calls: list[dict] = []

        def reconcile_once(*, start_runtimes=True):
            status = dict(statuses[min(len(calls), len(statuses) - 1)])
            calls.append(status)
            manager._status = dict(status)
            return status

        manager._reconcile_locked = reconcile_once
        manager.status = lambda: dict(manager._status)
        self.addCleanup(manager.stop)
        return manager, calls

    @staticmethod
    def _until(condition, seconds=3.0):
        deadline = time.time() + seconds
        while time.time() < deadline and not condition():
            time.sleep(0.02)
        return condition()

    WANTED_BUT_DOWN = {"robot_paused": False, "running": False, "platforms": {"polymarket": {"paused": False}}}
    RUNNING = {"robot_paused": False, "running": True, "platforms": {"polymarket": {"paused": False}}}
    PAUSED = {"robot_paused": True, "running": False, "platforms": {"polymarket": {"paused": False}}}

    def test_a_robot_that_could_not_start_starts_once_it_can(self) -> None:
        from prediction_market_agent.runtime import controller

        with patch.object(controller, "START_RETRY_SECONDS", 0.05):
            manager, calls = self._manager([self.WANTED_BUT_DOWN, self.RUNNING])
            self.assertIn("start_retry_at", manager.reconcile())
            self.assertTrue(self._until(lambda: len(calls) == 2))
            self.assertTrue(manager._status["running"])
            self.assertIsNone(manager._retry)

    def test_a_pause_saved_meanwhile_is_what_it_stays_in(self) -> None:
        from prediction_market_agent.runtime import controller

        with patch.object(controller, "START_RETRY_SECONDS", 0.2):
            manager, calls = self._manager([self.WANTED_BUT_DOWN, self.PAUSED, self.RUNNING])
            manager.reconcile()
            manager.stop()          # what saving the pause switch does first
            manager.reconcile()     # and then reads the saved choice
            time.sleep(0.5)
            self.assertEqual(len(calls), 2, "nothing restarts a robot the operator paused")
            self.assertIsNone(manager._retry)

    def test_nothing_retries_when_every_platform_is_paused_by_hand(self) -> None:
        manager, _ = self._manager([{"robot_paused": False, "running": False,
                                     "platforms": {"polymarket": {"paused": True}}}])
        manager.reconcile()
        self.assertIsNone(manager._retry)


class RestatingNeedsAModelNotARobotTests(unittest.TestCase):
    def _audit(self):
        from prediction_market_agent.core.config import Config
        from prediction_market_agent.runtime.dashboard import AuditData

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        config = Config(
            working_directory=root, session_db=root / "s.sqlite3", auth_db=root / "a.sqlite3",
            management_file=root / "m.json", plugin_directories_file=root / "d.json",
            application_config_file=root / "app.json",
        )
        memory = SessionMemory(config.session_db)
        decision_id = memory.begin_decision(
            platform="p", market_topic_id="t", market_id="m", token_id="o",
            strategy_name="built_in", strategy_sha256="x", context={"market": {"title": "BTC"}},
        )
        memory.complete_decision(decision_id, provider="claude", status="NO_ACTION",
                                 final_decision={"action": "HOLD", "rationale": "r"},
                                 execution={"action": "HOLD", "status": "NO_ACTION"})
        memory.close()
        data = AuditData(config, runtime=SimpleNamespace(_engine=None))
        self.addCleanup(data.management.shutdown)
        return data, decision_id

    def test_a_stopped_robot_does_not_stop_a_record_being_restated(self) -> None:
        data, decision_id = self._audit()
        model = SimpleNamespace(run=lambda payload, **options: SimpleNamespace(
            value={"headline": "观望", "found": "BTC", "analysis": "估计相近"}))
        with patch.object(type(data), "_restater", lambda self: (model, None)):
            answer = data.readable(decision_id)
        self.assertTrue(answer["available"])
        self.assertEqual(answer["headline"], "观望")

    def test_out_of_quota_says_so_and_when_it_is_back(self) -> None:
        data, decision_id = self._audit()
        quality = SimpleNamespace(capacity=lambda: {
            "available": False, "waiting": {"claude": "rate_limit"},
            "providers": {"claude": {"ready": False, "kind": "rate_limit", "recovers_at": time.time() + 7300}},
        })
        with patch.object(type(data), "_restater", lambda self: (SimpleNamespace(), quality)):
            answer = data.readable(decision_id)
        self.assertFalse(answer["available"])
        self.assertIn("AI 额度用完", answer["reason"])
        self.assertIn("预计 2 小时", answer["reason"])
        self.assertNotIn("机器人", answer["reason"])

    def test_no_service_at_all_names_the_service_not_the_robot(self) -> None:
        data, decision_id = self._audit()
        answer = data.readable(decision_id)
        self.assertFalse(answer["available"])
        self.assertIn("没有可用的 AI 模型服务", answer["reason"])
        self.assertNotIn("机器人", answer["reason"])


class StaleTabTests(unittest.TestCase):
    def test_the_version_follows_what_a_tab_loads(self) -> None:
        from prediction_market_agent.runtime import dashboard

        first = dashboard.console_version()
        self.assertEqual(dashboard.console_version(), first, "same files, same version")
        with patch.object(dashboard, "HTML", dashboard.HTML + "<!-- changed -->"):
            self.assertNotEqual(dashboard.console_version(), first)

    def test_assets_carry_it_and_the_runtime_reports_it(self) -> None:
        from prediction_market_agent.runtime import dashboard

        for name in dashboard.CONSOLE_ASSETS:
            self.assertIn(f"/assets/{name}?v=__CONSOLE_VERSION__", dashboard.HTML)
        self.assertIn("const CONSOLE_VERSION='__CONSOLE_VERSION__'", dashboard.HTML)
        self.assertIn('status["console_version"]=console_version()', Path(dashboard.__file__).read_text())
        views = Path("src/prediction_market_agent/runtime/static/dashboard-views.js").read_text()
        self.assertIn("noticeConsoleUpdate(r.console_version)", views)


class StrategyNameTests(unittest.TestCase):
    def test_the_row_names_the_strategy_that_actually_ran(self) -> None:
        source = Path("src/prediction_market_agent/runtime/evaluation.py").read_text()
        self.assertIn('getattr(self.decision_strategy, "name", "") or self.config.decision_strategy_name', source)
        from prediction_market_agent.agent.strategy import BuiltInDecisionStrategy

        self.assertEqual(BuiltInDecisionStrategy().name, "built_in")


if __name__ == "__main__":
    unittest.main()
