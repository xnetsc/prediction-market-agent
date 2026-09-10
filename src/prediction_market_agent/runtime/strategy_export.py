from __future__ import annotations

import time
from typing import Any

from ..agent.evolution import (
    DECISION_EVOLUTION_KEY,
    DISCOVERY_EVOLUTION_KEY,
    compose_overlay_payload,
    render_overlay_block,
)
from ..agent.market_discovery import BuiltInMarketDiscovery
from ..agent.strategy import BuiltInDecisionStrategy
from ..core.config import Config
from .decision_strategy import DecisionEvolution
from .market_discovery import DiscoveryEngine
from .memory import SessionMemory

LANES = ("discovery", "decision")

HEADINGS = {
    "discovery": "MARKET_DISCOVERY_STRATEGY",
    "decision": "CONFIGURED_DECISION_STRATEGY",
}


def _lane_view(
    *,
    lane: str,
    strategy: Any,
    priors: tuple[Any, ...],
    lessons: tuple[Any, ...],
    measurements: dict[str, Any],
    evolution_enabled: bool,
    now_ms: int,
    overlay_key: str,
    memory: SessionMemory,
) -> dict[str, Any]:
    payload = compose_overlay_payload(
        strategy.to_prompt_payload(),
        strategy=strategy,
        priors=priors,
        lessons=lessons,
        measurements=measurements,
        evolution_enabled=evolution_enabled,
        now_ms=now_ms,
    )
    stored_lessons = memory.load_discovery_lessons(overlay_key)
    return {
        "lane": lane,
        "strategy": getattr(strategy, "name", "built_in"),
        "source": "built-in" if isinstance(
            strategy, (BuiltInMarketDiscovery, BuiltInDecisionStrategy)
        ) else "plugin",
        "sha256": getattr(strategy, "sha256", ""),
        "evolution": payload["evolution"],
        "instructions": strategy.instructions,
        "priors": [prior.to_dict() for prior in priors],
        "lessons_applied": payload.get("lessons", []),
        "lessons_all": stored_lessons,
        "lessons_withheld": payload.get("lessons_withheld", 0),
        "measurements_full": measurements,
        "prompt_text": render_overlay_block(payload, HEADINGS[lane]),
        "prompt_chars": len(render_overlay_block(payload, HEADINGS[lane])),
    }


def export_strategies(
    config: Config, *, engine: Any = None, lane: str = "", memory: SessionMemory | None = None
) -> dict[str, Any]:
    """Export what each strategy lane currently sends to the model, evolution included.

    The built-in strategies have no configuration surface, but an operator running a trading robot
    must still be able to read exactly what it is being told to do. Export is that window: it
    reports the active strategy and, separately, the built-in's own evolved state, which keeps
    being measured even while a plugin is the one running.
    """
    requested = [item for item in LANES if not lane or item == lane]
    if not requested:
        raise ValueError(f"Unknown strategy lane: {lane}")
    owns_memory = memory is None
    store = memory or SessionMemory(config.session_db)
    now_ms = int(time.time() * 1000)
    evolution_enabled = bool(config.strategy_evolution)
    try:
        result: dict[str, Any] = {
            "generated_at": now_ms,
            "evolution_enabled_for_operator_strategies": evolution_enabled,
            "lanes": {},
        }
        for item in requested:
            if item == "discovery":
                active = (
                    engine.discovery.strategy if engine is not None else BuiltInMarketDiscovery()
                )
                runner = DiscoveryEngine(
                    memory=store,
                    strategy=active,
                    provider=None,
                    evolution_enabled=(
                        True
                        if not getattr(active, "evolution_switchable", True)
                        else evolution_enabled
                    ),
                )
                view = _lane_view(
                    lane=item,
                    strategy=active,
                    priors=runner._priors(),
                    lessons=runner._lessons(),
                    measurements=runner.measurements(),
                    evolution_enabled=runner.evolution_enabled,
                    now_ms=now_ms,
                    overlay_key=DISCOVERY_EVOLUTION_KEY,
                    memory=store,
                )
                builtin = BuiltInMarketDiscovery()
            else:
                active = (
                    engine.decision_evolution.strategy
                    if engine is not None
                    else BuiltInDecisionStrategy()
                )
                runner = DecisionEvolution(
                    memory=store,
                    strategy=active,
                    evolution_enabled=(
                        True
                        if not getattr(active, "evolution_switchable", True)
                        else evolution_enabled
                    ),
                )
                view = _lane_view(
                    lane=item,
                    strategy=active,
                    priors=runner._priors(),
                    lessons=runner._lessons(),
                    measurements=runner.measurements(),
                    evolution_enabled=runner.evolution_enabled,
                    now_ms=now_ms,
                    overlay_key=DECISION_EVOLUTION_KEY,
                    memory=store,
                )
                builtin = BuiltInDecisionStrategy()

            if view["source"] != "built-in":
                # The built-in keeps evolving underneath an operator plugin, so report it too.
                if item == "discovery":
                    fallback = DiscoveryEngine(
                        memory=store, strategy=builtin, provider=None, evolution_enabled=True
                    )
                else:
                    fallback = DecisionEvolution(
                        memory=store, strategy=builtin, evolution_enabled=True
                    )
                view["built_in"] = _lane_view(
                    lane=item,
                    strategy=builtin,
                    priors=fallback._priors(),
                    lessons=fallback._lessons(),
                    measurements=fallback.measurements(),
                    evolution_enabled=True,
                    now_ms=now_ms,
                    overlay_key=(
                        DISCOVERY_EVOLUTION_KEY if item == "discovery" else DECISION_EVOLUTION_KEY
                    ),
                    memory=store,
                )
            result["lanes"][item] = view
        return result
    finally:
        if owns_memory:
            store.close()
