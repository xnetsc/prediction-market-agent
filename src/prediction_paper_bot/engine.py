from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, replace
from typing import Any

from .broker import ExecutionError, ExecutionGateway
from .decision import Decision, DecisionProviderError, make_provider
from .memory import SessionMemory
from .models import AccountState, Config, StateStore
from .hooks import HookManager
from .plugins import load_api_plugins
from .plugins.discovery import load_plugin_catalog
from .plugins.base import (
    Market,
    OrderBook,
    Outcome,
    PredictionMarketApiPlugin,
    Topic,
    TopicDetail,
    platform_state_path,
)
from .research import ResearchToolContext, ResearchToolbox
from .risk import (
    PortfolioRiskContribution,
    RiskCoordinator,
)

LOGGER = logging.getLogger(__name__)


def _best_prices(book: OrderBook) -> tuple[float, float]:
    bids = [level.price for level in book.bids]
    asks = [level.price for level in book.asks]
    if not bids or not asks:
        raise ValueError("Order book has no two-sided liquidity")
    bid, ask = max(bids), min(asks)
    if not 0 < bid <= ask < 1:
        raise ValueError(f"Invalid order book: bid={bid}, ask={ask}")
    return bid, ask


def _compact_market(
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


@dataclass
class PlatformRuntime:
    plugin: PredictionMarketApiPlugin
    store: StateStore
    state: AccountState
    gateway: ExecutionGateway


class TradingEngine:
    def __init__(self, config: Config):
        self.config = config
        self.memory = SessionMemory(config.session_db)
        self.plugin_catalog = load_plugin_catalog(config)
        strategy_name = config.decision_strategy_name.strip().lower()
        if not strategy_name:
            raise ValueError("An enabled decision strategy plugin must be selected")
        self.decision_strategy = self.plugin_catalog.get(
            "decision_strategy", strategy_name
        ).factory(config)
        self.hooks = HookManager()
        self.hook_plugin_status: dict[str, Any] = {}
        for name in config.hook_plugins:
            self.hook_plugin_status[name] = self.plugin_catalog.get("hook", name).factory(
                config, {"hooks": self.hooks}
            )
        self.risk = RiskCoordinator()
        self.api_registry, plugins = load_api_plugins(config, self.plugin_catalog)
        risk_services: dict[str, Any] = {
            "phase": "bootstrap",
            "api_names": tuple(plugin.name for plugin in plugins),
        }
        risk_products: list[tuple[str, Any]] = []
        portfolio_contributions: list[Any] = []
        for name in config.risk_plugins:
            created = self.plugin_catalog.get("risk", name).factory(config, risk_services)
            risk_products.append((name, created))
            if isinstance(created, PortfolioRiskContribution):
                portfolio_contributions.append(created)
        if len(portfolio_contributions) != 1:
            raise ValueError(
                "Exactly one enabled risk plugin must provide account allocation and "
                "account/global execution risk controls"
            )
        portfolio_contribution = portfolio_contributions[0]
        multiple = len(plugins) > 1
        allocations = portfolio_contribution.initial_allocations(
            tuple(plugin.name for plugin in plugins)
        )
        self.platforms: dict[str, PlatformRuntime] = {}
        for plugin in plugins:
            state_path = platform_state_path(config.state_file, plugin.name, multiple)
            store = StateStore(state_path, allocations[plugin.name])
            state = store.load()
            account_risk = portfolio_contribution.create_account_engine(plugin.name, state)
            gateway = plugin.create_write_gateway(state, account_risk)
            gateway.hooks = self.hooks
            self.platforms[plugin.name] = PlatformRuntime(
                plugin=plugin,
                store=store,
                state=state,
                gateway=gateway,
            )
            self.risk.register(plugin.network_rule_engine)
            self.risk.register(gateway.risk)
        self.global_risk = portfolio_contribution.create_global_engine(
            {name: item.state for name, item in self.platforms.items()}
        )
        self.risk.register(self.global_risk)
        risk_services.update({
            "phase": "running",
            "states": {name: item.state for name, item in self.platforms.items()},
            "platforms": self.platforms,
        })
        for _name, created in risk_products:
            if created is portfolio_contribution:
                continue
            engines = created if isinstance(created, (list, tuple)) else (created,)
            for engine in engines:
                self.risk.register(engine)
        self.provider = make_provider(config, self.plugin_catalog)
        self.research_contributions = [
            self.plugin_catalog.get("research_tool", name).factory(config)
            for name in config.research_tool_plugins
        ]
        self._decisions_this_cycle = 0

    def _all_topics(self, plugin: PredictionMarketApiPlugin, maximum: int = 500) -> list[Topic]:
        topics: list[Topic] = []
        offset = 0
        while len(topics) < maximum:
            page = plugin.list_topics(offset=offset, limit=min(100, maximum - len(topics)))
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
                LOGGER.warning("%s account halted: %s", runtime.plugin.name, runtime.state.halt_reason)
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
                    LOGGER.warning("skip %s topic %s: %s", runtime.plugin.name, topic.topic_id, error)
            runtime.store.save(runtime.state)
        return self.status()

    def _evaluate_topic(self, runtime: PlatformRuntime, topic: Topic) -> None:
        detail = runtime.plugin.get_topic(topic.topic_id)
        if detail.end_time_ms is None:
            raise ValueError("Normalized topic has no end_time_ms")
        end_time_ms = detail.end_time_ms
        seconds_remaining = max((end_time_ms - int(time.time() * 1000)) / 1000.0, 0.0)
        if seconds_remaining <= 0:
            self._settle_if_possible(runtime, detail)
            return
        for market in self.decision_strategy.select_markets(detail.markets):
            if self._decisions_this_cycle >= self.config.max_decisions_per_cycle:
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
        if self._decisions_this_cycle >= self.config.max_decisions_per_cycle:
            return
        self._decisions_this_cycle += 1
        platform = runtime.plugin.name
        market_topic_id = detail.topic.topic_id
        market_id = market.market_id
        token_id = outcome.outcome_id
        book = runtime.plugin.get_order_book(market_id, token_id)
        bid, ask = _best_prices(book)
        runtime.gateway.mark(token_id, (bid + ask) / 2.0)
        position = runtime.state.positions.get(token_id)
        current = {
            "market": _compact_market(platform, topic, detail, market),
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
                    results[name] = {"available": False, "reason": "plugin has no market search"}
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
                notional_usdt=min(decision.notional_usdt, action_rule.adjusted_value),
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
                result={
                    "status": "EXECUTION_ERROR",
                    "error": str(error)[:2000],
                },
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
            LOGGER.warning("%s execution failed for token %s: %s", platform, token_id, error)
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
            raise RuntimeError(f"Agent tool blocked by risk rules: {decision.reason}")
        self.hooks.emit("before_agent_tool", {"platform": platform, "tool": name, **context})
        result = toolbox.execute(name, arguments)
        self.hooks.emit(
            "after_agent_tool", {"platform": platform, "tool": name, "result": result}
        )
        return result

    def _record_no_action(
        self,
        platform: str,
        market_topic_id: str,
        token_id: str,
        action: str,
        request: dict[str, Any],
        result: dict[str, Any],
        decision_id: int | None = None,
    ) -> dict[str, Any]:
        recorded = dict(result)
        self.memory.record_action(
            platform=platform,
            market_topic_id=market_topic_id,
            token_id=token_id,
            action=action,
            request=request,
            result=recorded,
            decision_id=decision_id,
        )
        return {"action": action, **recorded}

    def _execute_decision(
        self,
        runtime: PlatformRuntime,
        decision: Decision,
        *,
        detail: TopicDetail,
        market: Market,
        token_id: str,
        bid: float,
        ask: float,
        decision_id: int,
    ) -> dict[str, Any]:
        platform = runtime.plugin.name
        market_topic_id = detail.topic.topic_id
        market_id = market.market_id
        request = decision.to_dict()
        if decision.action == "HOLD":
            return self._record_no_action(
                platform, market_topic_id, token_id, "HOLD", request,
                {"status": "NO_ACTION"}, decision_id
            )
        if decision.action == "CANCEL":
            order_ids = [
                order.order_id
                for order in runtime.state.orders
                if order.token_id == token_id and order.status == "OPEN"
            ]
            result = runtime.gateway.cancel_orders(order_ids)
            return self._record_no_action(
                platform, market_topic_id, token_id, "CANCEL", request, result, decision_id
            )

        position = runtime.state.positions.get(token_id)
        fee_bps = detail.fee_bps
        if decision.action == "BUY":
            current_value = position.market_value if position else 0.0
            notional = runtime.gateway.allowed_buy_notional(
                decision.notional_usdt, current_value, fee_bps, token_id
            )
            if notional <= 0:
                return self._record_no_action(
                    platform,
                    market_topic_id,
                    token_id,
                    "BUY_REJECTED",
                    request,
                    {"status": "RISK_REJECTED", "allowed_notional": notional},
                    decision_id,
                )
            price = decision.limit_price if decision.order_type == "LIMIT" else ask
            assert price is not None
            quote = runtime.gateway.get_quote(
                token_id=token_id,
                side="BUY",
                price=price,
                quantity=notional / price,
                fee_bps=fee_bps,
                market_topic_id=market_topic_id,
                market_id=market_id,
                symbol=detail.reference_symbol,
                direction=market.title,
                order_type=decision.order_type,
            )
        else:
            if not position or decision.quantity_fraction <= 0:
                return self._record_no_action(
                    platform,
                    market_topic_id,
                    token_id,
                    "SELL_REJECTED",
                    request,
                    {"status": "NO_POSITION"},
                    decision_id,
                )
            price = decision.limit_price if decision.order_type == "LIMIT" else bid
            assert price is not None
            quote = runtime.gateway.get_quote(
                token_id=token_id,
                side="SELL",
                price=price,
                quantity=position.quantity * decision.quantity_fraction,
                fee_bps=fee_bps,
                market_topic_id=market_topic_id,
                market_id=market_id,
                symbol=position.symbol,
                direction=position.direction,
                order_type=decision.order_type,
            )
        order = runtime.gateway.place_order(quote, reason=decision.rationale)
        self.memory.record_action(
            platform=platform,
            market_topic_id=market_topic_id,
            token_id=token_id,
            action=decision.action,
            request={**request, "quote": asdict(quote)},
            result=asdict(order),
            decision_id=decision_id,
        )
        return {
            "action": decision.action,
            "status": order.status,
            "order_id": order.order_id,
            "order": asdict(order),
        }

    def _settle_if_possible(self, runtime: PlatformRuntime, detail: TopicDetail) -> None:
        if not runtime.plugin.capabilities.settlement_status:
            return
        for market in detail.markets:
            for outcome in market.outcomes:
                token_id = outcome.outcome_id
                if token_id not in runtime.state.positions:
                    continue
                winning = runtime.plugin.outcome_won(detail, market, outcome)
                if winning is None:
                    continue
                result = runtime.gateway.redeem(token_id, winning=winning)
                self.memory.record_action(
                    platform=runtime.plugin.name,
                    market_topic_id=detail.topic.topic_id,
                    token_id=token_id,
                    action="REDEEM",
                    request={"winning": winning, "outcome": outcome.name},
                    result=result,
                )

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
            name: self._platform_status(runtime) for name, runtime in self.platforms.items()
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
        return {key: round(value, 6) for key, value in sorted(totals.items())}

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
                sleep_for = min(sleep_for, max(0, self.config.run_until_epoch - int(time.time())))
            if sleep_for > 0:
                time.sleep(sleep_for)

    def close(self) -> None:
        """Release session and plugin-owned resources for this engine instance."""
        try:
            self.memory.close()
        finally:
            self.plugin_catalog.shutdown()
