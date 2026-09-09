from __future__ import annotations

from prediction_market_agent.plugin_system.config_io import json_file_callbacks
from prediction_market_agent.plugin_system.discovery import (
    PluginConfigField,
    PluginConfiguration,
    PluginInitializationContext,
    PluginSpec,
    close_plugin_instances,
)
from prediction_market_agent.plugins.api._polymarket.adapter import PolymarketApiPlugin


def initialize_plugin(context: PluginInitializationContext) -> PluginSpec:
    path = context.working_directory / "config" / "plugins" / "polymarket.json"
    load, save, delete, storage = json_file_callbacks(path)
    fields = (
        PluginConfigField("POLYMARKET_GAMMA_URL", "Gamma URL", "string", "Polymarket 事件发现与元数据 Gamma API 的 HTTPS 根地址。", required=True),
        PluginConfigField("POLYMARKET_CLOB_URL", "CLOB URL", "string", "Polymarket 订单簿行情和订单交易 API 的 HTTPS 根地址。", required=True),
        PluginConfigField("POLYMARKET_DATA_URL", "Data API URL", "string", "Polymarket 持仓和账户数据 API 的 HTTPS 根地址。", required=True),
        PluginConfigField("POLYMARKET_RELAYER_URL", "Relayer URL", "string", "Polymarket gasless 交易、赎回和转账 relayer 的 HTTPS 根地址。", required=True),
        PluginConfigField("POLYMARKET_RPC_URL", "Polygon RPC URL", "string", "执行或确认 EVM 链上操作使用的 HTTPS JSON-RPC 地址。", required=True),
        PluginConfigField("POLYMARKET_CHAIN_ID", "Chain ID", "integer", "Polymarket 插件执行链上请求时使用的 EVM chain ID。", required=True),
        PluginConfigField("POLYMARKET_PRIVATE_KEY", "钱包私钥", "secret", "签署 Polymarket CLOB 与链上交易的钱包私钥；只在本机插件内使用。"),
        PluginConfigField("POLYMARKET_API_KEY", "CLOB API Key", "secret", "Polymarket CLOB 二级认证的 API Key。"),
        PluginConfigField("POLYMARKET_API_SECRET", "CLOB API Secret", "secret", "Polymarket CLOB 二级认证的 API Secret。"),
        PluginConfigField("POLYMARKET_API_PASSPHRASE", "CLOB Passphrase", "secret", "Polymarket CLOB 二级认证的 Passphrase。"),
        PluginConfigField("POLYMARKET_FUNDER_ADDRESS", "Funder 地址", "string", "实际持有资金和头寸的 Polymarket proxy/deposit wallet 地址。"),
        PluginConfigField("POLYMARKET_BUILDER_CODE", "Builder Code", "string", "订单归因使用的 Polymarket Builder Code；不使用时可留空。"),
        PluginConfigField("POLYMARKET_RELAYER_API_KEY", "Relayer API Key", "secret", "调用 Polymarket relayer 的用户 API Key。"),
        PluginConfigField("POLYMARKET_RELAYER_API_KEY_ADDRESS", "Relayer Key 地址", "string", "与用户 relayer API Key 关联的链上地址。"),
        PluginConfigField("POLYMARKET_BUILDER_API_KEY", "Builder API Key", "secret", "使用 Builder relayer 身份时的 API Key。"),
        PluginConfigField("POLYMARKET_BUILDER_API_SECRET", "Builder API Secret", "secret", "使用 Builder relayer 身份时的 API Secret。"),
        PluginConfigField("POLYMARKET_BUILDER_API_PASSPHRASE", "Builder Passphrase", "secret", "使用 Builder relayer 身份时的 API Passphrase。"),
        PluginConfigField("POLYMARKET_TRANSFER_RECIPIENT", "转出地址", "string", "TRANSFER_OUT 操作默认接收 pUSD 的 EVM 地址。"),
        PluginConfigField("POLYMARKET_HTTP_PROXY", "HTTP 代理", "string", "Polymarket 插件独立使用的代理。填 DIRECT 直连、SYSTEM 读取本机系统代理，或填写 http(s) URL。", required=True),
        PluginConfigField("POLYMARKET_NETWORK_RULES_JSON", "网络规则 JSON", "string", "Polymarket 插件允许访问的 scheme、host、HTTP method 与各 method 路径模式；由插件构造网络规则引擎。", required=True),
    )
    configuration = PluginConfiguration(fields, load, save, delete, storage)

    instances = []

    def factory(config):
        del config
        instance = PolymarketApiPlugin(
            {name: str(value) for name, value in configuration.load().items()}
        )
        instances.append(instance)
        return instance

    return PluginSpec(
        kind="api",
        name="polymarket",
        description="Polymarket Gamma、CLOB、Data 与 Relayer 的统一预测市场适配器。",
        origin=str(context.module_path),
        factory=factory,
        configuration=configuration,
        teardown=lambda: close_plugin_instances(instances),
    )
