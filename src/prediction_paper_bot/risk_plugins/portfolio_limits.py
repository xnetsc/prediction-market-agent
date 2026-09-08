from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from prediction_paper_bot.models import AccountState
from prediction_paper_bot.plugin_config_io import json_file_callbacks
from prediction_paper_bot.plugins.discovery import (
    PluginConfigField,
    PluginConfiguration,
    PluginInitializationContext,
    PluginSpec,
)
from prediction_paper_bot.risk import RiskRejected, RuleDecision


def _net_result(state: AccountState) -> float:
    return state.equity + state.transferred_out - state.starting_capital


@dataclass(frozen=True)
class PortfolioLimitSettings:
    total_capital: float
    loss_limit: float
    max_position: float
    max_exposure: float
    min_order_notional: float
    allocations: dict[str, float]


class AccountLimitEngine:
    def __init__(self, platform: str, state: AccountState, settings: PortfolioLimitSettings):
        self.target = f"account:{platform}"
        self.state = state
        self.settings = settings
        self.global_refresh: Callable[[], None] | None = None
        self.refresh_metrics()

    def refresh_metrics(self) -> None:
        self.state.risk_metrics["net_result"] = _net_result(self.state)

    def evaluate(self, operation: str, context: dict[str, Any]) -> RuleDecision:
        if operation == "BUY":
            allowed = self.allowed_buy_notional(
                float(context.get("requested", 0)),
                float(context.get("current_position_value", 0)),
                int(context.get("fee_bps", 0)),
                str(context.get("token_id", "")),
            )
            if allowed <= 0:
                return RuleDecision("REJECT", self.state.halt_reason or "No account budget", 0)
            requested = float(context.get("requested", 0))
            return RuleDecision(
                "ALLOW" if allowed >= requested else "ADJUST",
                "Configured account limits applied",
                allowed,
            )
        self.refresh_halt()
        return RuleDecision("HALT" if self.state.halted else "ALLOW", self.state.halt_reason)

    def refresh_halt(self) -> None:
        self.refresh_metrics()
        if self.global_refresh is not None:
            self.global_refresh()

    def require_risk_increase_allowed(self) -> None:
        self.refresh_halt()
        if self.state.halted:
            raise RiskRejected(f"Trading halted: {self.state.halt_reason}")

    def _pending_buys(self, token_id: str) -> tuple[float, float, float]:
        orders = [
            order
            for order in self.state.orders
            if order.status == "OPEN" and order.side == "BUY"
        ]
        pending_notional = sum(order.notional for order in orders)
        pending_debit = sum(order.notional + order.fee for order in orders)
        pending_same = sum(
            order.notional for order in orders if order.token_id == token_id
        )
        return pending_notional, pending_debit, pending_same

    def allowed_buy_notional(
        self, requested: float, current_value: float, fee_bps: int, token_id: str
    ) -> float:
        self.require_risk_increase_allowed()
        pending_notional, pending_debit, pending_same = self._pending_buys(token_id)
        allowed = max(
            0.0,
            min(
                requested,
                self.settings.max_position - current_value - pending_same,
                self.settings.max_exposure - self.state.exposure - pending_notional,
                (self.state.cash - pending_debit) / (1.0 + fee_bps / 10_000.0),
            ),
        )
        return allowed if allowed >= self.settings.min_order_notional else 0.0

    def validate_buy_fill(
        self, notional: float, fee: float, existing_value: float, token_id: str
    ) -> None:
        self.require_risk_increase_allowed()
        pending_notional, pending_debit, pending_same = self._pending_buys(token_id)
        if notional < self.settings.min_order_notional:
            raise RiskRejected("Order is below the configured minimum notional")
        if existing_value + pending_same + notional > self.settings.max_position + 1e-9:
            raise RiskRejected("Configured per-position limit exceeded")
        if self.state.exposure + pending_notional + notional > self.settings.max_exposure + 1e-9:
            raise RiskRejected("Configured total-exposure limit exceeded")
        if pending_debit + notional + fee > self.state.cash + 1e-9:
            raise RiskRejected("Insufficient account cash")

    def validate_inbound_transfer(self, amount: float) -> None:
        if self.state.equity + self.state.transferred_out + amount > self.state.starting_capital:
            raise RiskRejected("Inbound transfer exceeds this account's configured allocation")

    def manifest(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "allocation": self.state.starting_capital,
            "max_position": self.settings.max_position,
            "max_exposure": self.settings.max_exposure,
            "min_order_notional": self.settings.min_order_notional,
            "metrics": dict(self.state.risk_metrics),
        }


class GlobalLimitEngine:
    target = "portfolio:global"

    def __init__(self, states: dict[str, AccountState], settings: PortfolioLimitSettings):
        self.states = states
        self.settings = settings
        self.refresh_halt()

    @property
    def net_result(self) -> float:
        return sum(_net_result(state) for state in self.states.values())

    def refresh_halt(self) -> None:
        result = self.net_result
        for state in self.states.values():
            state.risk_metrics["net_result"] = _net_result(state)
        if result <= -self.settings.loss_limit:
            reason = "Configured global loss limit reached"
            for state in self.states.values():
                state.halted = True
                state.halt_reason = reason

    def evaluate(self, operation: str, context: dict[str, Any]) -> RuleDecision:
        del operation, context
        self.refresh_halt()
        halted = any(state.halted for state in self.states.values())
        return RuleDecision("HALT" if halted else "ALLOW", "Configured portfolio policy evaluated")

    def manifest(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "configured_total_capital": self.settings.total_capital,
            "configured_loss_limit": self.settings.loss_limit,
            "metrics": {"global_net_result": self.net_result},
        }


class PortfolioLimitsContribution:
    def __init__(self, settings: PortfolioLimitSettings):
        self.settings = settings
        self._accounts: list[AccountLimitEngine] = []

    def initial_allocations(self, platforms: tuple[str, ...]) -> dict[str, float]:
        if set(self.settings.allocations) != set(platforms):
            raise ValueError("Portfolio allocations must name every enabled API plugin exactly once")
        if any(value <= 0 for value in self.settings.allocations.values()):
            raise ValueError("Portfolio allocations must be positive")
        if sum(self.settings.allocations.values()) > self.settings.total_capital + 1e-9:
            raise ValueError("Portfolio allocations exceed configured total capital")
        return dict(self.settings.allocations)

    def create_account_engine(self, platform: str, state: AccountState) -> AccountLimitEngine:
        engine = AccountLimitEngine(platform, state, self.settings)
        self._accounts.append(engine)
        return engine

    def create_global_engine(self, states: dict[str, AccountState]) -> GlobalLimitEngine:
        engine = GlobalLimitEngine(states, self.settings)
        for account in self._accounts:
            account.global_refresh = engine.refresh_halt
        return engine


def initialize_plugin(context: PluginInitializationContext) -> PluginSpec:
    load, save, storage = json_file_callbacks(
        context.working_directory / "config" / "plugins" / "risk_portfolio_limits.json"
    )
    configuration = PluginConfiguration(
        fields=(
            PluginConfigField("TOTAL_CAPITAL", "总资金", "number", "该风控策略允许机器人管理的总计价资产金额。", required=True),
            PluginConfigField("LOSS_LIMIT", "全局亏损限额", "number", "按本插件净结果公式计算的全局累计亏损触发阈值。", required=True),
            PluginConfigField("MAX_POSITION", "单仓上限", "number", "任一 outcome 持仓允许占用的最大计价资产金额。", required=True),
            PluginConfigField("MAX_EXPOSURE", "账户总敞口上限", "number", "任一平台账户全部持仓允许占用的最大计价资产金额。", required=True),
            PluginConfigField("MIN_ORDER_NOTIONAL", "最小订单金额", "number", "本插件允许新增的最小订单计价金额；更小的调整结果会被拒绝。", required=True),
            PluginConfigField("PLATFORM_ALLOCATIONS_JSON", "平台资金分配", "string", "以 API 插件名为键、正数金额为值的 JSON 对象，必须覆盖全部已启用 API。", required=True),
        ),
        load_callback=load,
        save_callback=save,
        storage=storage,
        required=False,
    )

    def factory(config, services):
        del config, services
        values = configuration.load()
        try:
            allocations = json.loads(values["PLATFORM_ALLOCATIONS_JSON"])
        except json.JSONDecodeError as error:
            raise ValueError("PLATFORM_ALLOCATIONS_JSON must be valid JSON") from error
        if not isinstance(allocations, dict):
            raise ValueError("PLATFORM_ALLOCATIONS_JSON must be an object")
        settings = PortfolioLimitSettings(
            total_capital=float(values["TOTAL_CAPITAL"]),
            loss_limit=float(values["LOSS_LIMIT"]),
            max_position=float(values["MAX_POSITION"]),
            max_exposure=float(values["MAX_EXPOSURE"]),
            min_order_notional=float(values["MIN_ORDER_NOTIONAL"]),
            allocations={str(name): float(amount) for name, amount in allocations.items()},
        )
        if min(
            settings.total_capital,
            settings.loss_limit,
            settings.max_position,
            settings.max_exposure,
            settings.min_order_notional,
        ) <= 0:
            raise ValueError("Portfolio limit numeric fields must be positive")
        if settings.max_position > settings.max_exposure:
            raise ValueError("MAX_POSITION cannot exceed MAX_EXPOSURE")
        return PortfolioLimitsContribution(settings)

    return PluginSpec(
        "risk",
        "portfolio_limits",
        "可配置的资金分配、净结果、全局止损、单仓和账户总敞口策略。",
        str(context.module_path),
        factory,
        configuration,
        lambda: None,
    )
