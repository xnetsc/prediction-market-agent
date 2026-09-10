from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, replace
from typing import Any

from ..agent.decision import DecisionProviderError
from ..agent.research import ResearchToolContext, ResearchToolbox
from ..plugin_system.contracts import Market, OrderBook, Outcome, Topic, TopicDetail
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
        "end_date_ms": detail.end_time_ms,
        "liquidity_usdt": market.liquidity_usdt or detail.topic.liquidity_usdt,
        "volume_usdt": market.volume_usdt or detail.topic.volume_usdt,
        "fee_rate_bps": detail.fee_bps,
        "resolution_data": detail.resolution,
    }


class MarketEvaluationMixin:
    """Build explainable contexts, invoke the Agent, and apply risk decisions."""

    def _evaluate_topic(self, runtime: PlatformRuntime, topic: Topic) -> None:
        detail = runtime.plugin.get_topic(topic.topic_id)
        if detail.end_time_ms is None:
            raise ValueError("Normalized topic has no end_time_ms")
        seconds_remaining = max(
            (detail.end_time_ms - int(time.time() * 1000)) / 1000.0, 0.0
        )
        if seconds_remaining <= 0:
            self._settle_if_possible(runtime, detail)
            return
        for market in self.decision_strategy.select_markets(detail.markets):
            if self._decisions_this_cycle >= self._max_decisions_this_cycle:
                break
            for outcome in self.decision_strategy.select_outcomes(market.outcomes):
                self._evaluate_outcome(
                    runtime, topic, detail, market, outcome, seconds_remaining
                )

    def _evaluate_outcome(
        self,
        runtime: PlatformRuntime,
        topic: Topic,
        detail: TopicDetail,
        market: Market,
        outcome: Outcome,
        seconds_remaining: float,
    ) -> None:
        if self._decisions_this_cycle >= self._max_decisions_this_cycle:
            return
        self._decisions_this_cycle += 1
        platform = runtime.plugin.name
        market_topic_id = detail.topic.topic_id
        market_id = market.market_id
        token_id = outcome.outcome_id
        book = runtime.plugin.get_order_book(market_id, token_id)
        bid, ask = best_prices(book)
        runtime.gateway.mark(token_id, (bid + ask) / 2.0)
        position = runtime.state.positions.get(token_id)
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
                "top_bids": [asdict(level) for level in book.bids[:5]],
                "top_asks": [asdict(level) for level in book.asks[:5]],
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
            "registered_platform_capabilities": {
                name: item.plugin.capabilities.to_dict()
                for name, item in self.platforms.items()
            },
            "active_api_plugin": runtime.plugin.configuration_manifest(),
            "decision_strategy_plugin": self.decision_strategy.to_prompt_payload(),
            "execution_risk_manifest": runtime.gateway.risk.manifest(),
            "risk_rule_engines": self.risk.manifests(),
        }
        current_chars = len(json.dumps(current, ensure_ascii=False))
        current["recalled_history"] = self.memory.recalled_context(
            market_topic_id=market_topic_id,
            token_id=token_id,
            history_limit=self.config.history_per_market,
            char_budget=max(1000, self.config.context_window_chars - current_chars),
            platform=platform,
        )
        decision_id = self.memory.begin_decision(
            platform=platform,
            market_topic_id=market_topic_id,
            market_id=market_id,
            token_id=token_id,
            strategy_name=self.config.decision_strategy_name,
            strategy_sha256=self.decision_strategy.sha256,
            context=current,
        )

        def search_markets(query: str, maximum: int) -> dict[str, Any]:
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
                        "candidates": [
                            candidate.to_dict() for candidate in candidates
                        ],
                    }
                except Exception as error:
                    results[name] = {"available": False, "reason": str(error)}
            return {
                "query": query,
                "platforms": results,
                "warning": (
                    "The Agent, not retrieval scores, must judge semantic equivalence."
                ),
            }

        toolbox = ResearchToolbox(
            self.research_contributions,
            ResearchToolContext(
                client=runtime.plugin,
                memory=self.memory,
                platform=platform,
                market_topic_id=market_topic_id,
                market_id=market_id,
                token_id=token_id,
                symbol=detail.reference_symbol,
                history_limit=self.config.history_per_market,
                market_search=search_markets,
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

        try:
            result = self.provider.decide(
                current,
                tool_executor=lambda name, arguments: self._execute_agent_tool(
                    platform, toolbox, name, arguments
                ),
                tool_descriptions=toolbox.descriptions,
                step_recorder=record_agent_step,
            )
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
        self.hooks.emit(
            "before_trade_decision",
            {"platform": platform, "decision": result.decision.to_dict()},
        )
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
        self.hooks.emit(
            "after_trade_decision",
            {"platform": platform, "decision": decision.to_dict()},
        )

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
        self.hooks.emit(
            "before_agent_tool", {"platform": platform, "tool": name, **context}
        )
        result = toolbox.execute(name, arguments)
        self.hooks.emit(
            "after_agent_tool",
            {"platform": platform, "tool": name, "result": result},
        )
        return result
