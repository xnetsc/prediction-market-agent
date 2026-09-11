from __future__ import annotations
from prediction_market_agent.plugin_system.network_diagnostics import configured_proxy_route

from prediction_market_agent.plugin_system.config_io import json_file_callbacks
from prediction_market_agent.plugin_system.discovery import (
    PluginConfigField,
    PluginConfiguration,
    PluginInitializationContext,
    PluginSpec,
    PluginReadiness,
    PluginRuntime,
    close_plugin_instances,
)
from prediction_market_agent.plugins.api._polymarket.config import PolymarketPluginConfig
from prediction_market_agent.plugins.api._polymarket.runtime import PolymarketEventLoop
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
        PluginConfigField("POLYMARKET_TRADING_CAPITAL", "交易账户起始资金(USDT)", "number", "这个平台的交易账户开户时有多少钱。这是账户设置,不是风控上限——框架不会用它拦任何动作,它只是记账的起点,决定模型 READ_ACCOUNT 时看到的本金和净结果基准。Polymarket 只支持转出,所以这笔钱要先在钱包里。填 0 表示账户没钱,买不了任何东西。", default=0),
        PluginConfigField("POLYMARKET_HTTP_PROXY", "代理使用方式", "string", "默认 INHERIT，使用程序设置里的统一代理。也可单独填 DIRECT、HOST、ENVIRONMENT、SYSTEM（仅原生 macOS）或完整 http(s) URL。", required=True, default="INHERIT"),
        PluginConfigField("POLYMARKET_SCAN_INTERVAL_SECONDS", "扫描间隔（秒）", "integer", "Polymarket 完成一个市场扫描与决策周期后等待到下一周期的秒数。", default=60),
        PluginConfigField("POLYMARKET_ERROR_BACKOFF_SECONDS", "失败退避初值（秒）", "integer", "Polymarket 周期失败后的首次重试等待秒数；连续失败时指数增长。", default=30),
        PluginConfigField("POLYMARKET_ERROR_BACKOFF_MAX_SECONDS", "失败退避上限（秒）", "integer", "Polymarket 连续失败重试等待的最大秒数。", default=900),
        PluginConfigField("POLYMARKET_MAX_TOPICS_PER_CYCLE", "每轮主题上限", "integer", "Polymarket 每轮允许框架发现后提交给业务队列的主题上限；发现范围和调用哪些读接口由发现策略决定。", default=10),
        PluginConfigField("POLYMARKET_MAX_DECISIONS_PER_CYCLE", "每轮决策上限", "integer", "Polymarket 每次事件循环最多交给 Agent 的 outcome 决策数。", default=6),
        PluginConfigField("POLYMARKET_TOPIC_PAGE_SIZE", "主题分页大小", "integer", "框架发现标的时，Polymarket 每次事件列表网络请求加载的记录数。", default=100),
    )
    configuration = PluginConfiguration(
        fields, load, save, delete, storage,
        retired_fields=("POLYMARKET_NETWORK_RULES_JSON",),
    )

    instances = []

    def resolved_values() -> dict[str, str]:
        values = {name: str(value) for name, value in configuration.load().items()}
        route = context.proxy_settings(
            values["POLYMARKET_HTTP_PROXY"], field_name="POLYMARKET_HTTP_PROXY"
        )
        values["POLYMARKET_HTTP_PROXY"] = route["proxy"] or "DIRECT"
        return values

    def settings() -> PolymarketPluginConfig:
        return PolymarketPluginConfig.from_mapping(resolved_values())

    event_loop = PolymarketEventLoop(settings)

    def readiness() -> PluginReadiness:
        try:
            values = configuration.load()
            settings()
        except (KeyError, TypeError, ValueError) as error:
            return PluginReadiness(False, (str(error),))
        required = (
            "POLYMARKET_PRIVATE_KEY",
            "POLYMARKET_API_KEY",
            "POLYMARKET_API_SECRET",
            "POLYMARKET_API_PASSPHRASE",
            "POLYMARKET_FUNDER_ADDRESS",
        )
        missing = tuple(name for name in required if not str(values.get(name, "")).strip())
        return (
            PluginReadiness(False, tuple(f"Missing runtime field: {name}" for name in missing))
            if missing
            else PluginReadiness(True)
        )

    def factory(config):
        del config
        instance = PolymarketApiPlugin(resolved_values())
        instances.append(instance)
        return instance


    def _live_instance():
        """Act on the running plugin, or make a throwaway one so the panel still answers."""
        if instances:
            return instances[-1]
        return PolymarketApiPlugin(resolved_values())

    def funding_status() -> dict:
        try:
            return _live_instance().funding_panel()
        except Exception as error:  # configuration incomplete, credentials missing, network down
            return {"error": str(error)[:300], "pending_request": None}

    def funding_notices() -> list:
        """Answer only when there is something outstanding, so a quiet plugin shows nothing."""
        try:
            panel = _live_instance().funding_panel()
        except Exception as error:
            return [{
                "key": "funding",
                "title": '资金到账确认',
                "kind": "display",
                "description": '机器人请求达到某个可用金额时，这里会显示需要转入的金额和收款地址。你转账后点确认，插件会重新读取余额核对，不满足会说明还差多少。',
                "content": {"error": str(error)[:300]},
            }]
        if not panel.get("pending_request"):
            return []
        return [{
            "key": "funding",
            "title": '资金到账确认',
            "kind": "confirm",
            "description": '机器人请求达到某个可用金额时，这里会显示需要转入的金额和收款地址。你转账后点确认，插件会重新读取余额核对，不满足会说明还差多少。',
            "content": panel,
            "action_label": '我已转账，去核对',
            "dismiss_label": '驳回',
        }]

    def funding_action(key: str, name: str, payload: dict) -> dict:
        del key, payload
        try:
            instance = _live_instance()
        except Exception as error:
            return {"ok": False, "message": str(error)[:300]}
        handler = {"confirm": instance.confirm_funding, "dismiss": instance.reject_funding}.get(name)
        if handler is None:
            return {"ok": False, "message": f"Unknown funding action: {name}"}
        try:
            return handler()
        except Exception as error:
            return {"ok": False, "message": str(error)[:300]}

    return PluginSpec(
        kind="api",
        name="polymarket",
        description="Polymarket Gamma、CLOB、Data 与 Relayer 的统一预测市场适配器。",
        origin=str(context.module_path),
        factory=factory,
        configuration=configuration,
        network_routes_callback=lambda: configured_proxy_route(
            load,
            "POLYMARKET_HTTP_PROXY",
            default="INHERIT",
            resolver=lambda value: context.proxy_settings(
                value, field_name="POLYMARKET_HTTP_PROXY"
            ),
        ),
        teardown=lambda: close_plugin_instances(instances),
        notices_callback=funding_notices,
        notice_action_callback=funding_action,
        readiness_callback=readiness,
        runtime=PluginRuntime(event_loop.start, event_loop.stop, event_loop.status),
    )
