from __future__ import annotations
from prediction_market_agent.plugin_system.network_diagnostics import configured_proxy_route

from prediction_market_agent.plugin_system.config_io import json_file_callbacks
from prediction_market_agent.plugins.api._binance.adapter import BinancePredictionApiPlugin
from prediction_market_agent.plugin_system.discovery import (
    PluginConfigField,
    PluginConfiguration,
    PluginInitializationContext,
    PluginSpec,
    PluginReadiness,
    PluginRuntime,
    close_plugin_instances,
)
from prediction_market_agent.plugins.api._binance.config import BinancePluginConfig
from prediction_market_agent.plugins.api._binance.runtime import BinanceEventLoop


def initialize_plugin(context: PluginInitializationContext) -> PluginSpec:
    path = context.working_directory / "config" / "plugins" / "binance.json"
    load, save, delete, storage = json_file_callbacks(path)
    configuration = PluginConfiguration(
        fields=(
            PluginConfigField("BINANCE_API_BASE_URL", "API URL", "string", "Binance REST API 的 HTTPS 根地址。", required=True),
            PluginConfigField("BINANCE_API_KEY", "API Key", "secret", "Binance API 管理页面生成的 API Key；读取私有端点和签名请求时使用。"),
            PluginConfigField("BINANCE_API_SECRET", "API Secret", "secret", "与 API Key 配套的签名密钥；仅由 Binance 插件在本机签名请求。"),
            PluginConfigField("BINANCE_PREDICTION_WALLET_ADDRESS", "预测钱包地址", "string", "Binance Web3 预测市场钱包地址，用于真实下单和资产操作。"),
            PluginConfigField("BINANCE_PREDICTION_WALLET_ID", "预测钱包 ID", "string", "Binance 预测市场内部钱包 ID，用于需要 walletId 的写接口。"),
            PluginConfigField("BINANCE_PREDICTION_ACCOUNT_TYPE", "资金账户", "enum", "预测钱包转入或转出时使用的 Binance 账户类型。", required=True, options=("SPOT", "FUNDING")),
            PluginConfigField("BINANCE_TRADING_CAPITAL", "交易账户起始资金(USDT)", "number", "这个平台的交易账户开户时有多少钱。这是账户设置,不是风控上限——框架不会用它拦任何动作,它只是记账的起点,决定模型 READ_ACCOUNT 时看到的本金和净结果基准。填 0 表示账户没钱,买不了任何东西。", default=0),
            PluginConfigField("BINANCE_PREDICTION_SLIPPAGE_BPS", "最大滑点(bps)", "integer", "市价请求允许的滑点基点数，100 bps 等于 1%。", required=True),
            PluginConfigField("BINANCE_HTTP_PROXY", "代理使用方式", "string", "默认 INHERIT，使用程序设置里的统一代理。也可单独填 DIRECT、HOST、ENVIRONMENT、SYSTEM（仅原生 macOS）或完整 http(s) URL。", required=True, default="INHERIT"),
            PluginConfigField("BINANCE_SCAN_INTERVAL_SECONDS", "扫描间隔（秒）", "integer", "Binance 完成一个市场扫描与决策周期后等待到下一周期的秒数。", default=60),
            PluginConfigField("BINANCE_ERROR_BACKOFF_SECONDS", "失败退避初值（秒）", "integer", "Binance 周期失败后的首次重试等待秒数；连续失败时指数增长。", default=30),
            PluginConfigField("BINANCE_ERROR_BACKOFF_MAX_SECONDS", "失败退避上限（秒）", "integer", "Binance 连续失败重试等待的最大秒数。", default=900),
            PluginConfigField("BINANCE_MAX_TOPICS_PER_CYCLE", "每轮主题上限", "integer", "Binance 每轮允许框架发现后提交给业务队列的主题上限；发现范围和调用哪些读接口由发现策略决定。", default=10),
            PluginConfigField("BINANCE_MAX_DECISIONS_PER_CYCLE", "每轮决策上限", "integer", "Binance 每次事件循环最多交给 Agent 的 outcome 决策数。", default=6),
            PluginConfigField("BINANCE_TOPIC_PAGE_SIZE", "主题分页大小", "integer", "框架发现标的时，Binance 每次主题列表网络请求加载的记录数。", default=100),
        ),
        load_callback=load,
        save_callback=save,
        delete_callback=delete,
        storage=storage,
        retired_fields=("BINANCE_NETWORK_RULES_JSON",),
    )

    instances = []

    def resolved_values() -> dict[str, str]:
        values = {name: str(value) for name, value in configuration.load().items()}
        route = context.proxy_settings(
            values["BINANCE_HTTP_PROXY"], field_name="BINANCE_HTTP_PROXY"
        )
        values["BINANCE_HTTP_PROXY"] = route["proxy"] or "DIRECT"
        return values

    def settings() -> BinancePluginConfig:
        return BinancePluginConfig.from_mapping(resolved_values())

    event_loop = BinanceEventLoop(settings)

    def readiness() -> PluginReadiness:
        try:
            values = configuration.load()
            settings()
        except (KeyError, TypeError, ValueError) as error:
            return PluginReadiness(False, (str(error),))
        required = (
            "BINANCE_API_KEY",
            "BINANCE_API_SECRET",
            "BINANCE_PREDICTION_WALLET_ADDRESS",
            "BINANCE_PREDICTION_WALLET_ID",
        )
        missing = tuple(name for name in required if not str(values.get(name, "")).strip())
        return (
            PluginReadiness(False, tuple(f"Missing runtime field: {name}" for name in missing))
            if missing
            else PluginReadiness(True)
        )

    def factory(config):
        del config
        instance = BinancePredictionApiPlugin(resolved_values())
        instances.append(instance)
        return instance


    def _live_instance():
        """Act on the running plugin, or make a throwaway one so the panel still answers."""
        if instances:
            return instances[-1]
        return BinancePredictionApiPlugin(resolved_values())

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
                "title": '资金划转请求',
                "kind": "display",
                "description": '机器人请求预测账户达到某个可用金额时，这里会出现待批准的划转。批准后才会从配置的资金账户划入，在此之前不会动任何钱。',
                "content": {"error": str(error)[:300]},
            }]
        if not panel.get("pending_request"):
            return []
        return [{
            "key": "funding",
            "title": '资金划转请求',
            "kind": "confirm",
            "description": '机器人请求预测账户达到某个可用金额时，这里会出现待批准的划转。批准后才会从配置的资金账户划入，在此之前不会动任何钱。',
            "content": panel,
            "action_label": '批准并转账',
            "action_fields": [{"name": "note", "label": "附言（可选）", "placeholder": '同意时可以顺带告诉机器人一句话，比如「我只剩这些了，别再要了」——它会读到。', "multiline": True}],
            "dismiss_fields": [{"name": "note", "label": "拒绝理由", "required": True, "placeholder": '拒绝理由（会给到机器人，让它别再原样问一遍）', "multiline": True}],
            "dismiss_label": '驳回',
        }]

    def funding_action(key: str, name: str, payload: dict) -> dict:
        del key
        try:
            instance = _live_instance()
        except Exception as error:
            return {"ok": False, "message": str(error)[:300]}
        handler = {"confirm": instance.approve_funding, "dismiss": instance.reject_funding}.get(name)
        if handler is None:
            return {"ok": False, "message": f"Unknown funding action: {name}"}
        try:
            return handler(payload)
        except Exception as error:
            return {"ok": False, "message": str(error)[:300]}

    return PluginSpec(
        kind="api",
        name="binance",
        description="Binance 预测市场的行情、下单、撤单、赎回与资金划转适配器。",
        origin=str(context.module_path),
        factory=factory,
        configuration=configuration,
        network_routes_callback=lambda: configured_proxy_route(
            load,
            "BINANCE_HTTP_PROXY",
            default="INHERIT",
            resolver=lambda value: context.proxy_settings(
                value, field_name="BINANCE_HTTP_PROXY"
            ),
        ),
        teardown=lambda: close_plugin_instances(instances),
        notices_callback=funding_notices,
        notice_action_callback=funding_action,
        readiness_callback=readiness,
        runtime=PluginRuntime(event_loop.start, event_loop.stop, event_loop.status),
    )
