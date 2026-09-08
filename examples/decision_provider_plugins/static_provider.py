"""Deterministic decision-provider plugin showing schema, storage, factory, and teardown."""

from prediction_market_agent.agent.decision import StructuredResult
from prediction_market_agent.sdk.config_io import json_file_callbacks
from prediction_market_agent.sdk.discovery import (
    PluginConfigField,
    PluginConfiguration,
    PluginInitializationContext,
    PluginSpec,
)


class StaticBackend:
    name = "static_provider"

    def __init__(self, rationale: str):
        self.rationale = rationale

    def complete(self, prompt, schema, schema_name):
        del prompt, schema
        if schema_name == "agent_control":
            value = {
                "next_action": "DECIDE",
                "arguments_json": "{}",
                "reason": "This example does not need external evidence.",
            }
        else:
            value = {
                "action": "HOLD",
                "order_type": "MARKET",
                "notional_usdt": 0,
                "quantity_fraction": 0,
                "limit_price": None,
                "confidence": 0,
                "estimated_probability": 0.5,
                "rationale": self.rationale,
            }
        return StructuredResult(value=value, raw_output=str(value))


def initialize_plugin(context: PluginInitializationContext) -> PluginSpec:
    load, save, storage = json_file_callbacks(
        context.working_directory / "examples" / "plugin_configs" / "static_provider.json"
    )
    configuration = PluginConfiguration(
        fields=(
            PluginConfigField(
                "RATIONALE",
                "示例理由",
                "string",
                "静态 Provider 每次 HOLD 时返回的解释文本。",
                default="Deterministic example response.",
            ),
        ),
        load_callback=load,
        save_callback=save,
        storage=storage,
    )
    instances = []

    def factory(config):
        del config
        backend = StaticBackend(configuration.load()["RATIONALE"])
        instances.append(backend)
        return backend

    def teardown():
        instances.clear()

    return PluginSpec(
        "decision_provider",
        "static_provider",
        "不访问网络、始终返回 HOLD 的完整 Provider 插件示例。",
        str(context.module_path),
        factory,
        configuration,
        teardown,
    )
