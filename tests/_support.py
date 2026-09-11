from __future__ import annotations

import os
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from prediction_market_agent.runtime.broker import ExecutionError, ExecutionGateway
from prediction_market_agent.agent.decision import (
    AgentDecisionProvider,
    Decision,
    DecisionProviderError,
    FallbackDecisionProvider,
    StructuredResult,
)
from prediction_market_agent.runtime.dashboard import AuditData
from prediction_market_agent.runtime.engine import TradingEngine
from prediction_market_agent.runtime.memory import SessionMemory
from prediction_market_agent.core.config import Config
from prediction_market_agent.core.domain import AccountState
from prediction_market_agent.core.state import StateStore
from prediction_market_agent.plugins.api._binance.adapter import BinancePredictionApiPlugin
from prediction_market_agent.plugins.api._binance.config import BinancePluginConfig
from prediction_market_agent.plugins.api._binance.write import BinancePredictionWriteTransport
from prediction_market_agent.plugins.api._polymarket.adapter import PolymarketApiPlugin
from prediction_market_agent.plugins.api._polymarket.config import PolymarketPluginConfig
from prediction_market_agent.plugin_system.registry import ApiPluginRegistry
from prediction_market_agent.plugin_system.contracts import (
    ApiCapabilities,
    Market,
    OrderBook,
    Outcome,
    PriceLevel,
    Topic,
    TopicDetail,
    TopicPage,
)
from prediction_market_agent.core.risk import (
    NetworkGateError,
    NetworkWriteGate,
    RiskCoordinator,
)
from prediction_market_agent.runtime.market_guard import GuardedMarketApi, MarketActionRejected
from prediction_market_agent.plugins.agent_policy.agent_actions import AgentActionRuleEngine
from prediction_market_agent.plugins.risk.custom_rules import BusinessRuleEngine
from prediction_market_agent.plugins.risk.portfolio_limits import (
    AccountLimitEngine,
    PortfolioLimitSettings,
    PortfolioLimitsContribution,
)
from prediction_market_agent.agent.strategy import DecisionStrategyPlugin


def config(path: Path) -> Config:
    return Config(state_file=path)


def load_strategy(path: Path) -> DecisionStrategyPlugin:
    return DecisionStrategyPlugin.load(
        path,
        topic_statuses=("OPEN",),
        market_statuses=("OPEN",),
        outcome_names=("YES",),
        minimum_topic_liquidity=0,
    )


BINANCE_ENV = {
    "BINANCE_API_BASE_URL": "https://api.binance.com",
    "BINANCE_PREDICTION_ACCOUNT_TYPE": "SPOT",
    "BINANCE_PREDICTION_SLIPPAGE_BPS": "100",
    "BINANCE_HTTP_PROXY": "DIRECT",
    "BINANCE_NETWORK_RULES_JSON": '{"schemes":["https"],"hosts":["api.binance.com"],"methods":["GET","POST"],"paths_by_method":{"GET":["/*"],"POST":["/*"]}}',
}

POLYMARKET_ENV = {
    "POLYMARKET_GAMMA_URL": "https://gamma-api.polymarket.com",
    "POLYMARKET_CLOB_URL": "https://clob.polymarket.com",
    "POLYMARKET_DATA_URL": "https://data-api.polymarket.com",
    "POLYMARKET_RELAYER_URL": "https://relayer-v2.polymarket.com",
    "POLYMARKET_RPC_URL": "https://polygon.drpc.org",
    "POLYMARKET_CHAIN_ID": "137",
    "POLYMARKET_HTTP_PROXY": "DIRECT",
    "POLYMARKET_NETWORK_RULES_JSON": '{"schemes":["https"],"hosts":["clob.polymarket.com"],"methods":["GET","POST","DELETE"],"paths_by_method":{"GET":["/*"],"POST":["/*"],"DELETE":["/*"]}}',
}


def configured_read_gate() -> NetworkWriteGate:
    return NetworkWriteGate(
        allowed_hosts=frozenset({"api.binance.com"}),
        allowed_schemes=frozenset({"https"}),
        allowed_methods=frozenset({"GET"}),
        allowed_read_paths=frozenset({"/api/v3/time"}),
        target_name="network:test",
    )


def account_risk(state: AccountState, platform: str = "test") -> AccountLimitEngine:
    return AccountLimitEngine(
        platform,
        state,
        PortfolioLimitSettings(
            total_capital=80,
            loss_limit=7,
            max_position=5.25,
            max_exposure=20,
            min_order_notional=1.25,
            allocations={platform: state.starting_capital},
        ),
    )
