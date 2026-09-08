from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Position:
    token_id: str
    market_topic_id: str | int
    market_id: str | int
    symbol: str
    direction: str
    quantity: float
    average_price: float
    mark_price: float
    opened_at: int

    @property
    def market_value(self) -> float:
        return self.quantity * self.mark_price

    @property
    def cost_basis(self) -> float:
        return self.quantity * self.average_price


@dataclass
class ExecutionOrder:
    order_id: str
    quote_id: str
    token_id: str
    side: str
    order_type: str
    status: str
    quantity: float
    price: float
    notional: float
    fee: float
    fee_bps: int
    market_topic_id: str | int
    market_id: str | int
    symbol: str
    direction: str
    created_at: int
    reason: str = ""


@dataclass
class ExecutionQuote:
    quote_id: str
    token_id: str
    side: str
    order_type: str
    quantity: float
    price: float
    notional: float
    fee_bps: int
    expires_at: int
    market_topic_id: str | int
    market_id: str | int
    symbol: str
    direction: str


@dataclass
class AccountState:
    starting_capital: float
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)
    orders: list[ExecutionOrder] = field(default_factory=list)
    realized_pnl: float = 0.0
    transferred_out: float = 0.0
    halted: bool = False
    halt_reason: str = ""
    risk_metrics: dict[str, float] = field(default_factory=dict)
    created_at: int = field(default_factory=lambda: int(time.time() * 1000))
    updated_at: int = field(default_factory=lambda: int(time.time() * 1000))

    @property
    def exposure(self) -> float:
        return sum(position.market_value for position in self.positions.values())

    @property
    def equity(self) -> float:
        return self.cash + self.exposure

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["positions"] = {
            key: asdict(value) for key, value in self.positions.items()
        }
        data["orders"] = [asdict(value) for value in self.orders]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AccountState":
        payload = dict(data)
        payload["positions"] = {
            key: Position(**value)
            for key, value in payload.get("positions", {}).items()
        }
        payload["orders"] = [
            ExecutionOrder(**value) for value in payload.get("orders", [])
        ]
        return cls(**payload)
