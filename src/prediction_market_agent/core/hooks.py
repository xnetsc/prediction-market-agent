from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from typing import Any


HookCallback = Callable[[str, dict[str, Any]], None]


class HookManager:
    EVENTS = frozenset(
        {
            "before_agent_tool",
            "after_agent_tool",
            "before_trade_decision",
            "after_trade_decision",
            "before_quote",
            "after_quote",
            "before_order",
            "after_order",
            "before_fill",
            "after_fill",
            "before_cancel",
            "after_cancel",
            "before_redeem",
            "after_redeem",
            "before_transfer",
            "after_transfer",
        }
    )

    def __init__(self) -> None:
        self._callbacks: dict[str, list[HookCallback]] = defaultdict(list)

    def register(self, event: str, callback: HookCallback) -> None:
        if event not in self.EVENTS:
            raise ValueError(f"Unknown trading hook event: {event}")
        self._callbacks[event].append(callback)

    def unregister(self, event: str, callback: HookCallback) -> None:
        callbacks = self._callbacks.get(event, [])
        if callback in callbacks:
            callbacks.remove(callback)
        if not callbacks:
            self._callbacks.pop(event, None)

    def emit(self, event: str, payload: dict[str, Any]) -> None:
        if event not in self.EVENTS:
            raise ValueError(f"Unknown trading hook event: {event}")
        for callback in self._callbacks.get(event, []):
            callback(event, payload)

    def manifest(self) -> dict[str, Any]:
        return {
            "events": sorted(self.EVENTS),
            "registered_callbacks": {
                event: len(callbacks) for event, callbacks in self._callbacks.items()
            },
            "authority": "Hooks cannot bypass risk or network-write gates",
        }
