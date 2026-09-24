from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, replace
from typing import Any

from ..agent.consultation import AgentConsultation
from ..agent.decision import DecisionCancelled, DecisionProviderError
from ..agent.evolution import prompt_json_payload, render_overlay_block
from ..agent.research import ResearchToolContext, ResearchToolbox
from ..plugin_system.contracts import Market, OrderBook, Outcome, Topic, TopicDetail
from .market_tools import MarketToolset
from .bootstrap import PlatformRuntime
from .broker import ExecutionError

LOGGER = logging.getLogger(__name__)


def best_prices(book: OrderBook) -> tuple[float, float]:
    bids = [level.price for level in book.bids]
    asks = [level.price for level in book.asks]
    if not bids or not asks:
        raise ValueError("Order book has no two-sided liquidity")
    bid, ask = max(bids), min(asks)
    if not 0 < bid <= ask < 1:
        raise ValueError(f"Invalid order book: bid={bid}, ask={ask}")
    return bid, ask


def near_touch_levels(book: OrderBook, count: int = 5) -> dict[str, list[dict[str, float]]]:
    """Normalize venue-specific book ordering before showing depth to a decision agent."""
    return {
        "top_bids": [asdict(level) for level in sorted(
            book.bids, key=lambda level: level.price, reverse=True
        )[:count]],
        "top_asks": [asdict(level) for level in sorted(
            book.asks, key=lambda level: level.price
        )[:count]],
    }


def compact_market(
    platform: str, topic: Topic, detail: TopicDetail, market: Market
) -> dict[str, Any]:
    return {
        "platform": platform,
        "market_topic_id": detail.topic.topic_id,
        "market_id": market.market_id,
        "slug": detail.topic.slug or topic.slug,
        "title": detail.topic.title or topic.title,
        "question": (detail.topic.question or topic.question)[:2000],
        "description": (detail.topic.description or topic.description)[:3000],
        "market_title": market.title,
        "market_question": market.question[:2000],
        "category": topic.category,
        "chart_type": detail.chart_type,
        "symbol": detail.reference_symbol,
        "start_date_ms": detail.start_time_ms,
        "end_date_ms": market.end_time_ms or detail.end_time_ms,
        "liquidity_usdt": market.liquidity_usdt or detail.topic.liquidity_usdt,
        "volume_usdt": market.volume_usdt or detail.topic.volume_usdt,
        "fee_rate_bps": (
            detail.fee_bps if market.fees_enabled is False or detail.fee_bps else None
        ),
        "fees_enabled": market.fees_enabled,
        "fee_schedule": market.fee_schedule,
        "resolution_data": detail.resolution,
    }


class MarketEvaluationMixin:
    """Build explainable contexts, invoke the Agent, and apply risk decisions."""

    def search_market_candidates(self, query: str, maximum: int) -> dict[str, Any]:
        """Ask every registered platform for candidates; the Agent judges relevance."""
        results: dict[str, Any] = {}
        for name, item in self.platforms.items():
            if not item.plugin.capabilities.market_search:
                results[name] = {
                    "available": False,
                    "reason": "plugin has no market search",
                }
                continue
            try:
                candidates = item.plugin.search_market_candidates(query, maximum)
                results[name] = {
                    "available": True,
                    "capabilities": item.plugin.capabilities.to_dict(),
                    "candidates": [candidate.to_dict() for candidate in candidates],
                }
            except Exception as error:
                results[name] = {"available": False, "reason": str(error)}
        return {
            "query": query,
            "platforms": results,
            "warning": "The Agent, not retrieval scores, must judge semantic equivalence.",
        }

    def _evaluate_topic(self, runtime: PlatformRuntime, topic: Topic) -> None:
        detail = runtime.plugin.get_topic(topic.topic_id)
        now_ms = int(time.time() * 1000)
        any_open_time = False
        for market in self.decision_strategy.select_markets(detail.markets):
            if self._decisions_this_cycle >= self._max_decisions_this_cycle:
                break
            market_end = market.end_time_ms or detail.end_time_ms
            if market_end is None or market_end <= now_ms:
                continue
            any_open_time = True
            seconds_remaining = (market_end - now_ms) / 1000.0
            for outcome in self.decision_strategy.select_outcomes(market.outcomes):
                self._evaluate_outcome(
                    runtime, topic, detail, market, outcome, seconds_remaining
                )
        if not any_open_time and detail.end_time_ms is not None and detail.end_time_ms <= now_ms:
            self._settle_if_possible(runtime, detail)

    def screen_queue(
        self, runtime: PlatformRuntime,
        queue: list[tuple[Topic, TopicDetail, Market, Outcome, float]],
    ) -> None:
        """Ask the screener, once for the whole cycle, which of these are already settled.

        Most binary markets are priced about right and answer HOLD, and they answer it again the
        next time; paying a reasoning round to be told so is the largest avoidable cost here. What
        makes that answer reusable is the trigger the last round named - so this is a comparison
        between that trigger and how the market stands now, which is what a screener answers from
        the facts rather than something to reason about.

        Asked as one batch, because how sure it is only means something next to the others: a skip
        is acted on when the screener itself says that answer stands apart from the rest of this
        round, not when it clears some number written here. Nothing is skipped for a market with an
        open position, one that has never been decided, or one whose last round named no trigger.
        """
        self._screened_out = {}
        evaluator = getattr(self, "evaluator", None)
        if evaluator is None or not getattr(evaluator, "available", False) or not queue:
            return
        platform = runtime.plugin.name
        history_by_topic = self.memory.topic_verdict_history(platform=platform)
        candidates: list[dict[str, Any]] = []
        for topic, detail, market, outcome, seconds_remaining in queue:
            if runtime.state.positions.get(outcome.outcome_id) is not None:
                continue
            history = history_by_topic.get(str(topic.topic_id))
            if not history or not str(history.get("revisit_when") or "").strip():
                continue
            candidates.append({
                "candidate_id": f"{topic.topic_id}:{outcome.outcome_id}",
                "title": topic.title,
                "outcome": outcome.name,
                "seconds_remaining": int(seconds_remaining),
                "liquidity_usdt": topic.liquidity_usdt,
                "volume_usdt": topic.volume_usdt,
                "previous_verdict": {
                    "action": history.get("last_action"),
                    "said": history.get("last_headline"),
                    "revisit_when": history.get("revisit_when"),
                    "times_held": history.get("holds"),
                    "seconds_since": max(
                        0, int(time.time()) - int(history.get("last_at", 0)) // 1000
                    ),
                },
            })
        if not candidates:
            return
        if callable(getattr(evaluator, "set_quality", None)):
            evaluator.set_quality(self.memory.evaluator_quality(platform=platform))
        state = {"platform": platform, "why": "decide which of these still need a full decision"}
        try:
            answers = evaluator.screen_decision(state, candidates) or {}
        except Exception as error:
            LOGGER.warning("decision screening failed on %s: %s", platform, error)
            return
        for name, error in getattr(evaluator, "fallback_errors", {}).items():
            self.memory.record_runtime_incident(
                platform=platform, stage="evaluator_fallback", severity="warning",
                message=f"{name}: {error}",
                fallback="决策前粗筛改用下一个已启用评估器",
            )
        skips = {
            key: value for key, value in answers.items()
            if not value.get("examine", True)
        }
        if not skips:
            return
        # Which of those answers are sure enough to act on is the screener's own judgement about
        # this batch, not a threshold: most rounds cluster, and the ones worth trusting are the
        # ones standing clear of the cluster.
        trusted = evaluator.screen_outliers(state, [
            {
                "candidate_id": key,
                "confidence": value.get("confidence"),
                "under_review": key in skips,
            }
            for key, value in answers.items()
        ])
        self._screened_out = {
            key: {**value, "previous_verdict": next(
                (item["previous_verdict"] for item in candidates
                 if item["candidate_id"] == key), {}
            )}
            for key, value in skips.items()
            if trusted.get(key)
        }
        if self._screened_out:
            LOGGER.info(
                "%s: %d of %d queued outcomes are already settled and will not be re-examined",
                platform, len(self._screened_out), len(queue),
            )

    def _plan_outcomes(
        self, runtime: PlatformRuntime, topics: list[Topic]
    ) -> list[tuple[Topic, TopicDetail, Market, Outcome, float]]:
        """Build a cross-topic queue so one event cannot consume the whole cycle by position."""
        by_topic: list[list[tuple[Topic, TopicDetail, Market, Outcome, float]]] = []
        for topic in topics:
            try:
                detail = runtime.plugin.get_topic(topic.topic_id)
            except Exception as error:
                LOGGER.warning("could not expand topic %s: %s", topic.topic_id, error)
                continue
            now_ms = int(time.time() * 1000)
            entries = []
            for market in self.decision_strategy.select_markets(detail.markets):
                market_end = market.end_time_ms or detail.end_time_ms
                if market_end is None or market_end <= now_ms:
                    continue
                entries.extend(
                    (topic, detail, market, outcome, (market_end - now_ms) / 1000.0)
                    for outcome in self.decision_strategy.select_outcomes(market.outcomes)
                )
            if not entries and detail.end_time_ms is not None and detail.end_time_ms <= now_ms:
                self._settle_if_possible(runtime, detail)
            if entries:
                by_topic.append(entries)
        queue: list[tuple[Topic, TopicDetail, Market, Outcome, float]] = []
        # Round-robin is the neutral core schedule. A strategy may order topics, markets and
        # outcomes, while the core prevents one large topic winning merely by being first.
        index = 0
        while any(index < len(entries) for entries in by_topic):
            for entries in by_topic:
                if index < len(entries):
                    queue.append(entries[index])
            index += 1
        return queue

    def _evaluate_outcome(
        self,
        runtime: PlatformRuntime,
        topic: Topic,
        detail: TopicDetail,
        market: Market,
        outcome: Outcome,
        seconds_remaining: float,
        funding_followup: dict[str, Any] | None = None,
        scheduled_review: dict[str, Any] | None = None,
    ) -> None:
        if getattr(self, "stop_requested", lambda: False)():
            return
        if self._decisions_this_cycle >= self._max_decisions_this_cycle:
            return
        platform = runtime.plugin.name
        market_topic_id = detail.topic.topic_id
        market_id = market.market_id
        token_id = outcome.outcome_id
        book = runtime.plugin.get_order_book(market_id, token_id)
        # An order-book failure is a platform read failure, not a completed decision attempt.
        self._decisions_this_cycle += 1
        bid, ask = best_prices(book)
        runtime.gateway.mark(token_id, (bid + ask) / 2.0)
        position = runtime.state.positions.get(token_id)
        skipped = (
            None if (position is not None or funding_followup is not None)
            else getattr(self, "_screened_out", {}).get(f"{market_topic_id}:{token_id}")
        )
        if skipped is not None:
            self.memory.complete_decision(
                self.memory.begin_decision(
                    platform=platform, market_topic_id=market_topic_id, market_id=market_id,
                    token_id=token_id, strategy_name="screened",
                    strategy_sha256=getattr(self.decision_strategy, "sha256", ""),
                    context={
                        "market": compact_market(platform, topic, detail, market),
                        "outcome": {"name": outcome.name, "token_id": token_id},
                        "order_book": {"best_bid": bid, "best_ask": ask},
                        "screened_out": skipped,
                    },
                ),
                provider=str(skipped.get("provider", "")),
                status=SessionMemory.SCREENED_OUT,
            )
            return
        # The strategy text reaches the model once, through the instruction block; the context
        # JSON carries only which priors and lessons were applied.
        strategy_payload = self.decision_evolution.payload()
        keeper = getattr(self, "operator_instructions", None)
        instructions = keeper.payload(platform) if keeper is not None else {}
        current = {
            "market": compact_market(platform, topic, detail, market),
            "outcome": {
                "name": outcome.name,
                "token_id": token_id,
                "displayed_probability": outcome.displayed_probability,
            },
            "order_book": {
                "best_bid": bid,
                "best_ask": ask,
                "spread": ask - bid,
                **near_touch_levels(book),
            },
            "seconds_remaining": seconds_remaining,
            "portfolio": {
                **self._platform_status(runtime),
                "position": asdict(position) if position else None,
                "open_orders_for_token": [
                    asdict(order)
                    for order in runtime.state.orders
                    if order.token_id == token_id and order.status == "OPEN"
                ],
            },
            # Present only when this round exists because an earlier one stopped for money. It
            # carries the old reasoning as something to re-check, never as a conclusion to resume.
            **({"delayed_funding_answer": funding_followup} if funding_followup else {}),
            **({"scheduled_review": scheduled_review} if scheduled_review else {}),
            # Conditions the operator attached to the money. They are not preferences: obeying
            # them comes before anything the strategy text would otherwise choose.
            **({"operator_instructions": instructions} if instructions.get("count") else {}),
            "registered_platform_capabilities": {
                name: item.plugin.capabilities.to_dict()
                for name, item in self.platforms.items()
            },
            "active_api_plugin": runtime.plugin.configuration_manifest(),
            "decision_strategy_plugin": prompt_json_payload(strategy_payload),
            "risk_rule_engines": self.risk.manifests(),
        }
        previous_round = self.memory.latest_decision_reference(
            market_topic_id=market_topic_id,
            token_id=token_id,
            platform=platform,
        )
        continuation_id = (
            funding_followup.get("continuation_of_decision_id")
            if isinstance(funding_followup, dict)
            else None
        )
        current["history_handoff"] = {
            "storage": "shared SQLite history exposed to the active CLI",
            "previous_round": previous_round,
            "continue_round_id": continuation_id,
            "instruction": (
                "Use the exact continue_round_id when present; otherwise previous_round is only "
                "the most relevant prior round, not a conclusion to copy. Query any additional "
                "history with the CLI's own tools."
            ),
        }
        decision_id = self.memory.begin_decision(
            platform=platform,
            market_topic_id=market_topic_id,
            market_id=market_id,
            token_id=token_id,
            # The strategy that is actually running, not the one configured. They differ whenever a
            # chosen strategy plugin is not ready and the built-in one stands in - and recording the
            # configured name left every such row with an empty strategy.
            strategy_name=(
                getattr(self.decision_strategy, "name", "") or self.config.decision_strategy_name
            ),
            strategy_sha256=self.decision_strategy.sha256,
            context=current,
            discovery_selection_id=(
                self.discovery.selection_id(platform, str(market_topic_id))
                if hasattr(self, "discovery") else None
            ),
        )

        # One handle for this round, given to the tools now and bound to a backend by the provider
        # below. A plugin reached through a tool can put a question back to the model this way, and
        # it arrives carrying the same market, trace and strategy text the round is working from.
        consultation = AgentConsultation()
        toolbox = ResearchToolbox(
            # The market contract and the account book are always present. A model asked to
            # apply a stop from its strategy text cannot apply it while blind to its own balance,
            # and one that cannot reach a second platform cannot compare a price against it.
            [*self.research_contributions, MarketToolset(self.platforms, platform)],
            ResearchToolContext(
                client=runtime.plugin,
                memory=self.memory,
                platform=platform,
                market_topic_id=market_topic_id,
                market_id=market_id,
                token_id=token_id,
                symbol=detail.reference_symbol,
                history_limit=self.config.history_per_market,
                market_search=self.search_market_candidates,
                consultation=consultation,
            ),
        )

        def record_agent_step(**values: Any) -> None:
            self.memory.record_agent_step(
                platform=platform,
                market_topic_id=market_topic_id,
                token_id=token_id,
                decision_id=decision_id,
                **values,
            )

        # What this round is about, so a funding request made mid-way can be read back against it.
        self._funding_context = {
            "market_topic_id": str(market_topic_id),
            "token_id": str(token_id),
            "decision_id": decision_id,
            "conclusion": {
                "market": current.get("market"),
                "order_book": current.get("order_book"),
                "seconds_remaining": current.get("seconds_remaining"),
                "portfolio": current.get("portfolio"),
            },
        }
        try:
            result = self.provider.decide(
                current,
                tool_executor=lambda name, arguments: self._execute_agent_tool(
                    platform, toolbox, name, arguments
                ),
                tool_descriptions=toolbox.descriptions,
                step_recorder=record_agent_step,
                instructions=render_overlay_block(
                    strategy_payload, "CONFIGURED_DECISION_STRATEGY"
                ),
                consultation=consultation,
                should_stop=lambda: self.memory.is_cancelled(decision_id),
            )
        except DecisionCancelled:
            # The row is gone; there is nothing to record against and nothing to act on.
            LOGGER.info("decision %s was deleted while being made; stopped", decision_id)
            return
        except DecisionProviderError as error:
            self.memory.record_turn(
                platform=platform,
                provider=self.provider.name,
                market_topic_id=market_topic_id,
                token_id=token_id,
                input_payload=current,
                raw_output=error.raw_output,
                decision=None,
                status="ERROR",
                error=str(error),
                decision_id=decision_id,
            )
            self.memory.complete_decision(
                decision_id,
                provider=self.provider.name,
                model_raw_output=error.raw_output,
                status="PROVIDER_ERROR",
                error=str(error),
            )
            LOGGER.error("decision provider failed for token %s: %s", token_id, error)
            return

        self.memory.record_turn(
            platform=platform,
            provider=result.provider,
            market_topic_id=market_topic_id,
            token_id=token_id,
            input_payload=current,
            raw_output=result.raw_output,
            decision=result.decision.to_dict(),
            status="OK",
            decision_id=decision_id,
        )
        if self.memory.is_cancelled(decision_id):
            # Deleted after the model answered but before anything was done about it. Acting on it
            # now would place an order for a decision the operator has already thrown away.
            LOGGER.info("decision %s was deleted before execution; not acted on", decision_id)
            return
        action_rule = self.risk.evaluate(
            "agent:actions",
            result.decision.action,
            {
                "kind": "trade",
                "platform": platform,
                "requested_value": result.decision.notional_usdt,
                "decision": result.decision.to_dict(),
            },
        )
        if action_rule.outcome in {"REJECT", "HALT"}:
            execution = self._record_no_action(
                platform,
                market_topic_id,
                token_id,
                "RISK_REJECTED",
                result.decision.to_dict(),
                {"status": action_rule.outcome, "reason": action_rule.reason},
                decision_id,
            )
            self.memory.complete_decision(
                decision_id,
                provider=result.provider,
                research=result.research_trace,
                model_raw_output=result.raw_output,
                proposed_decision=result.decision.to_dict(),
                risk_decision=asdict(action_rule),
                execution=execution,
                status="RISK_REJECTED",
            )
            return

        decision = result.decision
        if action_rule.outcome == "ADJUST" and action_rule.adjusted_value is not None:
            decision = replace(
                decision,
                notional_usdt=min(
                    decision.notional_usdt, action_rule.adjusted_value
                ),
            )
        if getattr(self, "stop_requested", lambda: False)():
            execution = self._record_no_action(
                platform, market_topic_id, token_id, "PAUSED", decision.to_dict(),
                {"status": "NO_ACTION", "reason": "Pause saved before execution"}, decision_id,
            )
            self.memory.complete_decision(
                decision_id, provider=result.provider, research=result.research_trace,
                model_raw_output=result.raw_output,
                proposed_decision=result.decision.to_dict(),
                risk_decision=asdict(action_rule), execution=execution,
                status="NO_ACTION",
            )
            return
        try:
            execution = self._execute_decision(
                runtime,
                decision,
                detail=detail,
                market=market,
                token_id=token_id,
                bid=bid,
                ask=ask,
                decision_id=decision_id,
            )
        except (ExecutionError, RuntimeError, ValueError) as error:
            self.memory.record_action(
                platform=platform,
                market_topic_id=market_topic_id,
                token_id=token_id,
                action=f"{decision.action}_FAILED",
                request=decision.to_dict(),
                result={"status": "EXECUTION_ERROR", "error": str(error)[:2000]},
                decision_id=decision_id,
            )
            self.memory.complete_decision(
                decision_id,
                provider=result.provider,
                research=result.research_trace,
                model_raw_output=result.raw_output,
                proposed_decision=result.decision.to_dict(),
                risk_decision=asdict(action_rule),
                final_decision=decision.to_dict(),
                execution={"status": "EXECUTION_ERROR", "error": str(error)[:2000]},
                status="EXECUTION_ERROR",
                error=str(error),
            )
            LOGGER.warning(
                "%s execution failed for token %s: %s", platform, token_id, error
            )
            return

        self.memory.complete_decision(
            decision_id,
            provider=result.provider,
            research=result.research_trace,
            model_raw_output=result.raw_output,
            proposed_decision=result.decision.to_dict(),
            risk_decision=asdict(action_rule),
            final_decision=decision.to_dict(),
            execution=execution,
            status=str(execution.get("status", "COMPLETED")),
        )

    _funding_context: dict[str, Any] | None = None

    def _execute_agent_tool(
        self,
        platform: str,
        toolbox: ResearchToolbox,
        name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        context = {"kind": "tool", "platform": platform, "arguments": arguments}
        decision = self.risk.evaluate("agent:actions", name, context)
        if decision.outcome in {"REJECT", "HALT"}:
            raise RuntimeError(
                f"Agent tool blocked by risk rules: {decision.reason}"
            )
        result = toolbox.execute(name, arguments)
        self._remember_funding_wait(platform, name, arguments, result)
        return result

    def _remember_funding_wait(
        self, platform: str, name: str, arguments: dict[str, Any], result: dict[str, Any]
    ) -> None:
        """File the reasoning behind a funding request that has to wait for a person.

        The decision ends here - the model cannot hold a position on an answer that has not come.
        What must not end with it is why the money was wanted, because that is the only thing the
        eventual answer can be judged against.
        """
        if name.strip().upper() != "ENSURE_FUNDS" or not isinstance(result, dict):
            return
        if str(result.get("state")) != "pending" or not result.get("request_id"):
            return
        context = self._funding_context
        if context is None:
            return
        try:
            self.memory.open_funding_continuation(
                request_id=str(result["request_id"]),
                platform=str(result.get("platform") or platform),
                market_topic_id=context["market_topic_id"],
                token_id=context["token_id"],
                decision_id=context.get("decision_id"),
                asked_for=float(arguments.get("amount", 0) or 0),
                currency=str(result.get("currency", "")),
                reason=str(arguments.get("reason", "")),
                conclusion=context["conclusion"],
            )
        except Exception:
            LOGGER.exception("could not record why %s was asked for funds", platform)
