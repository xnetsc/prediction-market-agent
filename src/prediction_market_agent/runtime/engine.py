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
from .operator_instructions import OperatorInstructions, evidence_for_review
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
        for runtime in self.platforms.values():
            runtime.gateway.pnl_event_sink = self.memory.record_pnl_event
            if not self.memory.has_pnl_events(
                platform=runtime.plugin.name, account_mode=runtime.account_mode
            ):
                state = runtime.state
                legacy_activity = bool(
                    state.orders
                    or state.positions
                    or abs(state.realized_pnl) > 1e-12
                    or abs(state.transferred_out) > 1e-12
                    or abs(state.cash - state.starting_capital) > 1e-12
                )
                self.memory.record_pnl_event(
                    {
                        "created_at": state.created_at,
                        "platform": runtime.plugin.name,
                        "account_mode": runtime.account_mode,
                        "currency": runtime.currency,
                        "event_type": "ACCOUNT_BASELINE",
                        "cash_after": state.cash,
                        "equity_after": state.equity,
                        "realized_pnl_after": state.realized_pnl,
                        "evidence_status": "partial" if legacy_activity else "complete",
                        "metadata": {
                            "starting_capital": state.starting_capital,
                            "open_positions": len(state.positions),
                            "orders_before_event_ledger": len(state.orders),
                            "historical_breakdown_available": not legacy_activity,
                        },
                    }
                )
        self.provider = components.provider
        self.evaluator = components.evaluator
        self.research_contributions = components.research_contributions
        self.discovery_strategy = components.discovery_strategy
        # What the operator attached to their money. Collected by the platform plugins, understood
        # and kept here, obeyed by every round until this says it is finished.
        self.operator_instructions = OperatorInstructions(
            memory=self.memory, provider=components.provider
        )
        self.discovery = DiscoveryEngine(
            memory=self.memory,
            strategy=components.discovery_strategy,
            provider=components.provider,
            evaluator=components.evaluator,
            max_scan_seconds=config.discovery_max_scan_seconds,
            max_scan_pages=config.discovery_max_pages,
            evolution_enabled=components.strategy_evolution,
            cross_platform_search=self.search_market_candidates,
            research_contributions=components.research_contributions,
            operator_instructions=self.operator_instructions.payload,
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
                topics = self._collect_platform_topics(runtime)
                self._process_platform_topics(runtime, topics)
            return self.status()

    def run_platform_once(self, platform: str) -> dict[str, float | int | bool | str]:
        """Execute one platform cycle when invoked by that platform plugin runtime."""
        with self._cycle_lock:
            try:
                runtime = self.platforms[platform]
            except KeyError as error:
                raise ValueError(f"Unknown active platform: {platform}") from error
            topics = self._collect_platform_topics(runtime)
            self._process_platform_topics(runtime, topics)
            return self.status()

    def process_platform_scan(
        self, platform: str, topics: tuple[Topic, ...]
    ) -> dict[str, float | int | bool | str]:
        """Consume a normalized scan event emitted by one platform plugin."""
        with self._cycle_lock:
            try:
                runtime = self.platforms[platform]
            except KeyError as error:
                raise ValueError(f"Unknown active platform: {platform}") from error
            self._process_platform_topics(runtime, list(topics))
            return self.status()

    def discover_platform_topics(self, platform: str) -> tuple[Topic, ...]:
        """Framework-side market discovery, invoked by a platform runtime on its own schedule."""
        with self._cycle_lock:
            try:
                runtime = self.platforms[platform]
            except KeyError as error:
                raise ValueError(f"Unknown active platform: {platform}") from error
            return tuple(self._collect_platform_topics(runtime))

    def can_decide(self, platform: str) -> bool:
        """Whether work that exists to reach a decision should start now.

        Every cycle pulls markets, builds a prompt and opens a ledger record for the sake of one
        answer. With no model able to give it, all of that is spent for nothing and the ledger
        fills with failures that only restate the outage - so the work is not started. The plugins
        are told separately and stand down; this is the check that does not depend on them.
        """
        reading = self.provider_quality.capacity()
        if not reading["available"]:
            LOGGER.info(
                "platform=%s holding: no decision provider can answer (%s)",
                platform,
                ", ".join(f"{name}={kind}" for name, kind in reading["waiting"].items()) or "none",
            )
        return bool(reading["available"])

    def _collect_platform_topics(self, runtime: PlatformRuntime) -> list[Topic]:
        if not self.can_decide(runtime.plugin.name):
            return []
        # Before anything is collected: take what the operator has said since last time, and ask
        # which of the standing instructions this cycle no longer has to carry.
        self._apply_operator_instructions(runtime, moment="before_cycle")
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
            )
        )

    def _settle_open_positions(self, runtime: PlatformRuntime) -> None:
        """Go and settle what is held, rather than waiting for discovery to bring it back.

        Settlement only ever ran on a topic that arrived through discovery and turned out to have
        expired - and a venue that lists open markets does not list the one that just closed, which
        is precisely the one holding an unrealised result. So a position could be opened and never
        resolve: bought, expired, and left sitting at its last mark forever, with the ledger showing
        an outcome that had in fact already happened.

        Held positions are few, and a topic that has not expired costs one read and returns.
        """
        for position in list(runtime.state.positions.values()):
            try:
                detail = runtime.plugin.get_topic(str(position.market_topic_id))
            except Exception as error:
                LOGGER.warning(
                    "could not check %s for settlement: %s", position.market_topic_id, error
                )
                continue
            if detail.end_time_ms and detail.end_time_ms > int(time.time() * 1000):
                continue
            try:
                self._settle_if_possible(runtime, detail)
            except Exception:
                LOGGER.exception("settling %s failed", position.market_topic_id)

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
        partial = str(getattr(answer, "state", "")) == "partial"
        requested = float(getattr(answer, "requested", 0) or 0)
        available = float(getattr(answer, "available", 0) or 0)
        return {
            "continuation_of_decision_id": entry.get("decision_id"),
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
                (
                    f"Only {available:.6f} of the requested {requested:.6f} is available; "
                    f"{max(0.0, requested - available):.6f} remains unfunded. Decide exactly once "
                    "from the current market. Choose one: (1) continue with the available amount "
                    "and ask for nothing more; (2) do not execute now, call ENSURE_FUNDS with the "
                    "exact target balance still required, and wait; or (3) execute a deliberately "
                    "staged amount now and also call ENSURE_FUNDS for that target. For option 3, "
                    "describe the current order as a staged position, not completion; when the "
                    "rest arrives another fresh decision will see the existing position and must "
                    "not mechanically fill the remainder. If the opportunity is gone, HOLD. "
                    "Partial funding is not permission to claim the target is ready."
                ) if partial else (
                    "Given how long that took and what the answer turned out to be, is the thing "
                    "you wanted to do still worth doing? Compare your reason against the prices "
                    "in front of you now, which have been re-read since you asked. A long wait is "
                    "itself evidence: an edge that depended on acting quickly is probably gone. "
                    "If it no longer holds, say so and HOLD rather than completing a trade you "
                    "would not open today."
                )
            ),
        }

    def _process_platform_topics(self, runtime: PlatformRuntime, topics: list[Topic]) -> None:
        self._decisions_this_cycle = 0
        # The core's explicit resource guard is the only hard attempt ceiling; the queue normally ends first
        # because no selected work remains or the bounded cycle time expires.
        self._max_decisions_this_cycle = self.config.decision_max_attempts
        cycle_started = time.monotonic()
        # Settling what is already held needs no model, and money already committed is owed its
        # outcome whether or not anything can decide today.
        if not self.can_decide(runtime.plugin.name):
            self._settle_open_positions(runtime)
            runtime.store.save(runtime.state)
            return
        # Answers to funding requests come back between cycles, so this is where they are read.
        # An unclaimed answer is the same as no answer: nobody is sitting waiting for it.
        self._resume_funded_decisions(runtime)
        self._settle_open_positions(runtime)
        eligible = self.decision_strategy.select_topics(topics)
        queue = self._plan_outcomes(runtime, eligible)
        # One screening pass for the whole cycle, before any expensive round: which of these were
        # already settled and said what they were waiting for.
        self.screen_queue(runtime, queue)
        LOGGER.info(
            "platform=%s scanned=%d candidates=%d outcomes=%d provider=%s safety_limit=%d",
            runtime.plugin.name,
            len(topics),
            len(eligible),
            len(queue),
            self.provider.name,
            self._max_decisions_this_cycle,
        )
        for topic, detail, market, outcome, seconds_remaining in queue:
            if self._decisions_this_cycle >= self._max_decisions_this_cycle:
                break
            if time.monotonic() - cycle_started >= self.config.decision_max_cycle_seconds:
                LOGGER.info("platform=%s yielding queued decisions at the cycle time guard", runtime.plugin.name)
                break
            # A limit reached on one topic is reached for all of them: the rest of the scan
            # would only collect data and write a failed record apiece.
            if not self.can_decide(runtime.plugin.name):
                break
            # A note that arrives mid-cycle has to bind the next decision, not the next cycle. The
            # money it came with is already spendable, and a restriction read an hour later is a
            # restriction read after the trade it was meant to stop. This costs a file read when
            # nobody has written anything, which is almost always.
            self._catch_up_on_notes(runtime)
            try:
                self._evaluate_outcome(
                    runtime, topic, detail, market, outcome, seconds_remaining
                )
            except (KeyError, ValueError, RuntimeError, ExecutionError) as error:
                LOGGER.warning(
                    "skip %s outcome %s in topic %s: %s",
                    runtime.plugin.name,
                    outcome.outcome_id,
                    topic.topic_id,
                    error,
                )
        # The cycle is over and the robot is about to wait: this is when what it just did can be
        # measured against what it was asked for.
        self._apply_operator_instructions(runtime, moment="cycle_finished")
        runtime.store.save(runtime.state)

    def _catch_up_on_notes(self, runtime: PlatformRuntime) -> None:
        """Read anything the operator has said since the last decision, and nothing else.

        The review - asking which instructions are finished - stays at the two moments a cycle can
        be judged from. This is only the reading half, because a new instruction is worth a model
        call the moment it exists, and re-judging the old ones between two topics is not.
        """
        try:
            kept = self.operator_instructions.harvest(runtime.plugin.name, runtime.plugin)
        except Exception:
            LOGGER.exception("could not read what the operator said on %s", runtime.plugin.name)
            return
        if kept:
            LOGGER.info(
                "%s: %d new thing(s) from the operator, binding from this decision on",
                runtime.plugin.name, len(kept),
            )

    def _apply_operator_instructions(self, runtime: PlatformRuntime, *, moment: str) -> None:
        """Collect what was said, and close what has been done - at the two points it can be judged.

        Both halves are quiet when there is nothing to do: no notes means no reading, and no open
        instruction means no review. A round that asked a model twice to be told nothing changed
        would be paying for the feature rather than using it.
        """
        platform = runtime.plugin.name
        try:
            kept = self.operator_instructions.harvest(platform, runtime.plugin)
            if kept:
                LOGGER.info("%s: kept %d thing(s) the operator asked for", platform, len(kept))
        except Exception:
            LOGGER.exception("could not read what the operator said on %s", platform)
        try:
            closed = self.operator_instructions.review(
                platform,
                moment=moment,
                evidence=evidence_for_review(self.memory, platform),
            )
            for item in closed:
                LOGGER.info(
                    "%s: instruction %s is %s - %s",
                    platform, item["id"], item["state"], item["why"],
                )
        except Exception:
            LOGGER.exception("could not review the operator's instructions on %s", platform)

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
            "decision_evaluators": list(self.evaluator.names),
            "unavailable_decision_evaluators": self.evaluator.unavailable,
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
