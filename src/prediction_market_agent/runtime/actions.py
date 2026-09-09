from __future__ import annotations

from dataclasses import asdict
from typing import Any

from ..agent.decision import Decision
from ..plugin_system.contracts import Market, TopicDetail
from .bootstrap import PlatformRuntime


class ExecutionActionsMixin:
    """Translate normalized Agent decisions into platform gateway operations."""

    memory: Any

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
                platform,
                market_topic_id,
                token_id,
                "HOLD",
                request,
                {"status": "NO_ACTION"},
                decision_id,
            )
        if decision.action == "CANCEL":
            order_ids = [
                order.order_id
                for order in runtime.state.orders
                if order.token_id == token_id and order.status == "OPEN"
            ]
            result = runtime.gateway.cancel_orders(order_ids)
            return self._record_no_action(
                platform,
                market_topic_id,
                token_id,
                "CANCEL",
                request,
                result,
                decision_id,
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

    def _settle_if_possible(
        self, runtime: PlatformRuntime, detail: TopicDetail
    ) -> None:
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
