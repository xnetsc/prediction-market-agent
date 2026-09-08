from __future__ import annotations

from prediction_paper_bot.plugin_config_io import json_file_callbacks
from prediction_paper_bot.plugins.binance import BinancePredictionApiPlugin
from prediction_paper_bot.plugins.discovery import (
    PluginConfigField,
    PluginConfiguration,
    PluginInitializationContext,
    PluginSpec,
    close_plugin_instances,
)


def initialize_plugin(context: PluginInitializationContext) -> PluginSpec:
    path = context.working_directory / "config" / "plugins" / "binance.json"
    load, save, storage = json_file_callbacks(path)
    configuration = PluginConfiguration(
        fields=(
            PluginConfigField("BINANCE_API_BASE_URL", "API URL", "string", "Binance REST API 的 HTTPS 根地址。", required=True),
            PluginConfigField("BINANCE_API_KEY", "API Key", "secret", "Binance API 管理页面生成的 API Key；读取私有端点和签名请求时使用。"),
            PluginConfigField("BINANCE_API_SECRET", "API Secret", "secret", "与 API Key 配套的签名密钥；仅由 Binance 插件在本机签名请求。"),
            PluginConfigField("BINANCE_PREDICTION_WALLET_ADDRESS", "预测钱包地址", "string", "Binance Web3 预测市场钱包地址，用于真实下单和资产操作。"),
            PluginConfigField("BINANCE_PREDICTION_WALLET_ID", "预测钱包 ID", "string", "Binance 预测市场内部钱包 ID，用于需要 walletId 的写接口。"),
            PluginConfigField("BINANCE_PREDICTION_ACCOUNT_TYPE", "资金账户", "enum", "预测钱包转入或转出时使用的 Binance 账户类型。", required=True, options=("SPOT", "FUNDING")),
            PluginConfigField("BINANCE_PREDICTION_SLIPPAGE_BPS", "最大滑点(bps)", "integer", "市价请求允许的滑点基点数，100 bps 等于 1%。", required=True),
            PluginConfigField("BINANCE_HTTP_PROXY", "HTTP 代理", "string", "Binance 插件独立使用的代理。填 DIRECT 直连、SYSTEM 读取本机系统代理，或填写 http(s) URL。", required=True),
            PluginConfigField("BINANCE_NETWORK_RULES_JSON", "网络规则 JSON", "string", "Binance 插件允许访问的 scheme、host、HTTP method 与各 method 路径模式；由插件构造网络规则引擎。", required=True),
        ),
        load_callback=load,
        save_callback=save,
        storage=storage,
    )

    instances = []

    def factory(config):
        del config
        instance = BinancePredictionApiPlugin(
            {name: str(value) for name, value in configuration.load().items()}
        )
        instances.append(instance)
        return instance

    return PluginSpec(
        kind="api",
        name="binance",
        description="Binance 预测市场的行情、下单、撤单、赎回与资金划转适配器。",
        origin=str(context.module_path),
        factory=factory,
        configuration=configuration,
        teardown=lambda: close_plugin_instances(instances),
    )
