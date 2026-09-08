from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .managed_config import PLUGIN_KINDS


DEFAULT_SDK_CONFIG = Path(__file__).resolve().parents[1] / "config/plugin_sdk.default.json"


@dataclass(frozen=True)
class PluginSdkConfig:
    path: Path
    directories: dict[str, tuple[Path, ...]]

    @classmethod
    def load(cls, configured: Path) -> "PluginSdkConfig":
        path = configured.expanduser().resolve()
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as error:
            raise ValueError(f"Plugin SDK config is invalid JSON: {path}: {error}") from error
        categories = raw.get("categories") if isinstance(raw, dict) else None
        if not isinstance(categories, dict):
            raise ValueError("Plugin SDK config must contain a categories object")
        package_root = Path(__file__).resolve().parents[1]
        resolved: dict[str, tuple[Path, ...]] = {}
        for kind in PLUGIN_KINDS:
            values = categories.get(kind, [])
            if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
                raise ValueError(f"Plugin SDK category {kind} must be a list of directories")
            paths: list[Path] = []
            for value in values:
                expanded = value.replace("${PACKAGE_ROOT}", str(package_root))
                candidate = Path(expanded).expanduser()
                if not candidate.is_absolute():
                    candidate = path.parent / candidate
                paths.append(candidate.resolve())
            resolved[kind] = tuple(paths)
        return cls(path=path, directories=resolved)

    def manifest(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "categories": {
                kind: [str(path) for path in paths]
                for kind, paths in self.directories.items()
            },
        }
