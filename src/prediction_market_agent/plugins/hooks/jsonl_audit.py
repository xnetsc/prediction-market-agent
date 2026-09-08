from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from prediction_market_agent.core.hooks import HookManager
from prediction_market_agent.sdk.config_io import json_file_callbacks
from prediction_market_agent.sdk.discovery import (
    PluginConfigField,
    PluginConfiguration,
    PluginInitializationContext,
    PluginSpec,
)


def initialize_plugin(context: PluginInitializationContext) -> PluginSpec:
    load, save, storage = json_file_callbacks(
        context.working_directory / "config" / "plugins" / "hook_jsonl_audit.json"
    )
    configuration = PluginConfiguration(
        fields=(
            PluginConfigField("OUTPUT_PATH", "审计日志路径", "string", "Hook 事件写入的 JSON Lines 文件路径；相对路径以机器人工作目录为基准。", default="hook_events.jsonl"),
            PluginConfigField("EVENTS", "订阅事件", "string", "需要记录的 Hook 事件名，使用英文逗号分隔；填星号表示记录全部标准 Hook 事件。", default="*"),
        ),
        load_callback=load,
        save_callback=save,
        storage=storage,
    )
    lock = threading.Lock()
    registrations = []

    def factory(config, services):
        del config
        manager: HookManager = services["hooks"]
        values = configuration.load()
        output = Path(values.get("OUTPUT_PATH", "hook_events.jsonl")).expanduser()
        if not output.is_absolute():
            output = context.working_directory / output
        selected = values.get("EVENTS", "*").strip()
        events = HookManager.EVENTS if selected == "*" else frozenset(
            item.strip() for item in selected.split(",") if item.strip()
        )
        unknown = sorted(events - HookManager.EVENTS)
        if unknown:
            raise ValueError(f"Unknown hook events: {', '.join(unknown)}")

        def record(event, payload):
            row = json.dumps(
                {"created_at": int(time.time() * 1000), "event": event, "payload": payload},
                ensure_ascii=False,
                default=str,
            )
            output.parent.mkdir(parents=True, exist_ok=True)
            with lock, output.open("a", encoding="utf-8") as handle:
                handle.write(row + "\n")

        for event in sorted(events):
            manager.register(event, record)
            registrations.append((manager, event, record))
        return {"events": sorted(events), "output": str(output.resolve())}

    def teardown():
        while registrations:
            manager, event, callback = registrations.pop()
            manager.unregister(event, callback)

    return PluginSpec(
        "hook",
        "jsonl_audit",
        "把选定的下单、成交、撤单、赎回、转账和 Agent Hook 事件追加到 JSONL。",
        str(context.module_path),
        factory,
        configuration,
        teardown,
    )
