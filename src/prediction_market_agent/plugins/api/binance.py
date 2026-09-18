from __future__ import annotations

from typing import Any
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
            PluginConfigField("BINANCE_API_BASE_URL", "API URL", "string", "Binance REST API 的 HTTPS 根地址。已填好官方地址，只有在用测试网或自建入口时才需要改。", required=True, default="https://api.binance.com"),
            PluginConfigField("BINANCE_API_KEY", "API Key", "secret", "Binance API 管理页面生成的 API Key；读取私有端点和签名请求时使用。", needed_to_run=True),
            PluginConfigField("BINANCE_API_SECRET", "API Secret", "secret", "与 API Key 配套的签名密钥；仅由 Binance 插件在本机签名请求。", needed_to_run=True),
            PluginConfigField("BINANCE_PREDICTION_WALLET_ADDRESS", "预测钱包地址", "string", "Binance Web3 预测市场钱包地址，用于真实下单和资产操作。", needed_to_run=True),
            PluginConfigField("BINANCE_PREDICTION_WALLET_ID", "预测钱包 ID", "string", "Binance 预测市场内部钱包 ID，用于需要 walletId 的写接口。", needed_to_run=True),
            PluginConfigField("BINANCE_PREDICTION_ACCOUNT_TYPE", "资金账户", "enum", "预测钱包转入或转出时使用的 Binance 账户类型。", required=True, options=("SPOT", "FUNDING"), default="SPOT"),
            PluginConfigField("BINANCE_TRADING_CAPITAL", "预测钱包当前余额(USDT)·转账后需手工更新", "number", "预测钱包里现在有多少 USDT。之所以要你填：这个插件用的 /sapi/v1/w3w/wallet/prediction 端点里没有读预测钱包余额的接口，现货余额读得到但那不是预测账户能花的钱。所以这个数字被当作可用金额上报（source 标成 declared，不冒充平台答案），账本起点也随之而来。它不拦任何动作——填多了不会被挡，只会在真下单时被平台拒。转账进出由插件自己按你批准的诉求执行，执行后这里要手工跟上。", default=0),
            PluginConfigField("BINANCE_PREDICTION_SLIPPAGE_BPS", "最大滑点(bps)", "integer", "市价请求允许的滑点基点数，100 bps 等于 1%。", default=100, required=True),
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
        # Derived from the schema rather than repeated here. Two lists of the same fact drift,
        # and the one that drifted was this page telling operators a credential was optional.
        missing = tuple(
            field.name
            for field in configuration.fields
            if field.needed_to_run and not str(values.get(field.name, "")).strip()
        )
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

    # The transaction the operator last said they sent, and the chain they asked an address for.
    watched: dict[str, Any] = {"txid": "", "state": "", "detail": "", "network": ""}

    DEPOSIT_FIELDS = [
        {"name": "txid", "label": "充值交易号（txid）", "required": True,
         "placeholder": "0x 或 交易所给的那串交易号；转完账在提币记录里复制"},
        # Conditions people attach when they pay: a deadline, what it may be spent on, a change of
        # approach. Collected here, obeyed by the runtime.
        {"name": "note", "label": "附言（可选）：给这笔钱提的要求", "multiline": True,
         "placeholder": "比如「这笔 3 天内用完」「只买体育」「别再买几个月后才揭标的」——机器人会一直照做，"
                        "做完了会在决策账本里告诉你"},
    ]

    def deposit_action(key: str, name: str, payload: dict) -> dict:
        del key
        if name == "dismiss":
            watched.update(txid="", state="", detail="")
            return {"ok": True, "message": "已清除这笔充值的跟踪"}
        try:
            instance = _live_instance()
        except Exception as error:
            return {"ok": False, "message": str(error)[:300]}
        note = str(payload.get("note", "")).strip()
        if note:
            instance.note_from_operator(note, source="deposit", txid=str(payload.get("txid", "")))
        network = str(payload.get("network", "")).strip().upper()
        if network and not str(payload.get("txid", "")).strip():
            # Asking for another chain's address is not a claim that anything was sent.
            watched.update(network=network)
            return {"ok": True, "message": f"已切到 {network}，地址在上面"}
        answer = instance.confirm_deposit(payload)
        watched.update(
            txid=str(answer.get("txid", "")),
            state=str(answer.get("state", "")),
            detail=str(answer.get("message", "")),
        )
        return answer

    def deposit_watch() -> dict:
        if not watched["txid"] or watched["state"] in {"arrived", "not_found", "invalid", "wrong_target"}:
            return dict(watched)
        try:
            status = _live_instance().deposit_status(watched["txid"])
        except Exception as error:
            return {**watched, "detail": str(error)[:200]}
        watched.update(state=str(status.get("state", "")), detail=str(status.get("detail", "")))
        return dict(watched)

    def deposit_notices() -> list:
        """Offered whenever the plugin is loaded: topping up is not only an answer to the robot."""
        try:
            panel = _live_instance().deposit_panel(watched["network"])
        except Exception as error:
            return [{
                "key": "deposit", "title": '我要充值', "kind": "display",
                "description": '往这个账户充值的链、地址和币种。填好 API Key 并连上交易所之后才能给出地址。',
                "content": {"读取失败": str(error)[:300]},
            }]
        tracking = deposit_watch()
        confirming = tracking["state"] == "confirming"
        content = dict(panel)
        if tracking["txid"]:
            content["这笔充值"] = f"{tracking['txid']}：{tracking['detail'] or tracking['state']}"
        # Read by the robot, not here, and the robot may be paused or out of model quota. Saying so
        # is the difference between "waiting its turn" and the operator thinking it went nowhere.
        try:
            waiting = [str(item.get("text", "")) for item in _live_instance().operator_messages()]
        except Exception:
            waiting = []
        if waiting:
            content["待机器人读取的附言"] = (
                "；".join(waiting) + f"（{len(waiting)} 条，机器人下一轮开始前会读它们，"
                "读懂了会出现在总览页的「转账附言与要求」里）"
            )
        return [{
            "key": "deposit",
            "title": '我要充值',
            "kind": "confirm",
            "description": '按下面的链、地址、币种充值；转完把交易号填进来，插件去交易所查到账。'
                           '要换一条链，就只填网络代码、不填交易号。转错链或错币种的钱拿不回来。',
            "content": content,
            "action_label": '我已充值，去查',
            "action_disabled": confirming,
            "action_note": (tracking["detail"] or '确认中…') if confirming else '',
            "action_fields": [
                *DEPOSIT_FIELDS,
                {"name": "network", "label": "换条链取地址（可选）",
                 "placeholder": "填上面列出的网络代码，比如 BSC、MATIC；只填这个不填交易号就是换地址"},
            ],
            "dismiss_label": '清除这笔跟踪' if tracking["txid"] else "",
        }]

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
            "action_fields": [
                {"name": "note", "label": "附言（可选）", "placeholder": '同意时可以顺带告诉机器人一句话，比如「我只剩这些了，别再要了」——它会读到。', "multiline": True},
                {"name": "txid", "label": "刚充值的交易号（可选）",
                 "placeholder": "如果你是刚充的钱来满足这笔请求，填交易号，这里先去交易所确认到账"},
            ],
            "dismiss_fields": [{"name": "note", "label": "拒绝理由", "required": True, "placeholder": '拒绝理由（会给到机器人，让它别再原样问一遍）', "multiline": True}],
            "dismiss_label": '驳回',
        }]

    def funding_action(key: str, name: str, payload: dict) -> dict:
        del key
        try:
            instance = _live_instance()
        except Exception as error:
            return {"ok": False, "message": str(error)[:300]}
        # A deposit made to answer this request is checked at the exchange before any transfer:
        # approving a move of money that has not landed is how an account ends up short twice.
        note = str(payload.get("note", "")).strip()
        if note:
            instance.note_from_operator(
                note, source=f"funding:{name}", txid=str(payload.get("txid", "")),
            )
        if name == "confirm" and str(payload.get("txid", "")).strip():
            answer = deposit_action("funding", "confirm", payload)
            if not answer.get("ok"):
                return answer
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
        notices_callback=lambda: [*deposit_notices(), *funding_notices()],
        notice_action_callback=lambda key, name, payload: (
            deposit_action(key, name, payload) if key == "deposit"
            else funding_action(key, name, payload)
        ),
        readiness_callback=readiness,
        runtime=PluginRuntime(
            event_loop.start, event_loop.stop, event_loop.status,
            notify_callback=event_loop.notify,
        ),
    )
