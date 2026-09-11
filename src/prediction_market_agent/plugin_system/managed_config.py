from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


PLUGIN_KINDS = (
    "api",
    "decision_provider",
    "decision_strategy",
    "market_discovery",
    "research_tool",
    "agent_policy",
    "risk",
    "hook",
)

DEFAULT_PLUGIN_SELECTION_CONFIG = (
    Path(__file__).resolve().parents[1] / "config/plugin_selection.default.json"
)


def _string_list(value: Any, field_name: str) -> tuple[str, ...] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field_name} must be a list of strings")
    result: list[str] = []
    for item in value:
        normalized = item.strip().lower()
        if normalized and normalized not in result:
            result.append(normalized)
    return tuple(result)


@dataclass(frozen=True)
class ManagedRuntimeConfig:
    path: Path
    enabled: dict[str, tuple[str, ...]] = field(default_factory=dict)
    decision_strategy: str = ""
    strategy_evolution: bool = True
    robot_paused: bool = False
    paused_platforms: tuple[str, ...] = ()
    source_path: Path | None = None

    @classmethod
    def load(cls, path: Path) -> "ManagedRuntimeConfig":
        resolved = path.expanduser().resolve()
        source = resolved if resolved.exists() else DEFAULT_PLUGIN_SELECTION_CONFIG.resolve()
        try:
            raw = json.loads(source.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as error:
            raise ValueError(f"Managed plugin settings are invalid JSON: {source}: {error}") from error
        if not isinstance(raw, dict):
            raise ValueError("Managed plugin settings must be a JSON object")
        enabled_raw = raw.get("enabled", {})
        if not isinstance(enabled_raw, dict):
            raise ValueError("Managed plugin settings enabled must be an object")
        enabled: dict[str, tuple[str, ...]] = {}
        for kind in PLUGIN_KINDS:
            values = _string_list(enabled_raw.get(kind), f"enabled.{kind}")
            if values is not None:
                enabled[kind] = values
        strategy = str(raw.get("decision_strategy", "")).strip().lower()
        evolution = raw.get("strategy_evolution", True)
        if not isinstance(evolution, bool):
            raise ValueError(
                "Managed plugin setting strategy_evolution must be a boolean"
            )
        robot_paused = raw.get("robot_paused", False)
        if not isinstance(robot_paused, bool):
            raise ValueError("Managed plugin setting robot_paused must be a boolean")
        paused_platforms = _string_list(
            raw.get("paused_platforms", []), "paused_platforms"
        )
        return cls(
            path=resolved,
            enabled=enabled,
            decision_strategy=strategy,
            strategy_evolution=evolution,
            robot_paused=robot_paused,
            paused_platforms=paused_platforms or (),
            source_path=source,
        )

    def selected(self, kind: str, fallback: tuple[str, ...]) -> tuple[str, ...]:
        return self.enabled.get(kind, fallback)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "enabled": {kind: list(names) for kind, names in self.enabled.items()},
            "decision_strategy": self.decision_strategy,
            "strategy_evolution": self.strategy_evolution,
            "robot_paused": self.robot_paused,
            "paused_platforms": list(self.paused_platforms),
        }


def path_is_within(path: Path, roots: tuple[Path, ...]) -> bool:
    resolved = path.expanduser().resolve()
    return any(resolved == root or root in resolved.parents for root in roots)


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def save_managed_config(path: Path, value: dict[str, Any]) -> ManagedRuntimeConfig:
    candidate = {
        "version": 1,
        "enabled": value.get("enabled", {}),
        "decision_strategy": value.get("decision_strategy", ""),
        "strategy_evolution": value.get("strategy_evolution", True),
        "robot_paused": value.get("robot_paused", False),
        "paused_platforms": value.get("paused_platforms", []),
    }
    encoded = json.dumps(candidate, ensure_ascii=False, indent=2) + "\n"
    atomic_write_text(path.expanduser().resolve(), encoded)
    return ManagedRuntimeConfig.load(path)
