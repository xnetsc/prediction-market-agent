from __future__ import annotations
from prediction_market_agent.plugin_system.network_diagnostics import configured_proxy_route

from polymarket import PRODUCTION

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
        PluginConfigField("POLYMARKET_GAMMA_URL", "Gamma URL", "string", "Polymarket 事件发现与元数据 Gamma API 的 HTTPS 根地址。", required=True, default=PRODUCTION.gamma_url),
        PluginConfigField("POLYMARKET_CLOB_URL", "CLOB URL", "string", "Polymarket 订单簿行情和订单交易 API 的 HTTPS 根地址。", required=True, default=PRODUCTION.clob_url),
        PluginConfigField("POLYMARKET_DATA_URL", "Data API URL", "string", "Polymarket 持仓和账户数据 API 的 HTTPS 根地址。", required=True, default=PRODUCTION.data_url),
        PluginConfigField("POLYMARKET_RELAYER_URL", "Relayer URL", "string", "Polymarket gasless 交易、赎回和转账 relayer 的 HTTPS 根地址。", required=True, default=PRODUCTION.relayer_url),
        PluginConfigField("POLYMARKET_RPC_URL", "Polygon RPC URL", "string", "执行或确认 EVM 链上操作使用的 HTTPS JSON-RPC 地址。", required=True, default=PRODUCTION.rpc_url),
        PluginConfigField("POLYMARKET_CHAIN_ID", "Chain ID", "integer", "Polymarket 插件执行链上请求时使用的 EVM chain ID。", required=True, default=PRODUCTION.chain_id),
        PluginConfigField("POLYMARKET_PRIVATE_KEY", "钱包私钥", "secret", "签署 Polymarket CLOB 与链上交易的钱包私钥；只在本机插件内使用，不外传。你可以粘贴自己已有的钱包私钥；如果还没有，下方「钱包」面板里有一个「生成一个新钱包」按钮，点了才会生成——插件不会自作主张替你决定用哪种方式。生成的钱包一开始是空的，要你自己往里转钱。", needed_to_run=True),
        PluginConfigField("POLYMARKET_API_KEY", "CLOB API Key", "secret", "Polymarket CLOB 二级认证的 API Key。留空即可——这三项能由私钥派生，插件会在连线时自己办。想用你自己已有的那套凭据就填进来，填了就以你填的为准。", ),
        PluginConfigField("POLYMARKET_API_SECRET", "CLOB API Secret", "secret", "Polymarket CLOB 二级认证的 API Secret。留空由私钥派生；三项要么都填，要么都留空。", ),
        PluginConfigField("POLYMARKET_API_PASSPHRASE", "CLOB Passphrase", "secret", "Polymarket CLOB 二级认证的 Passphrase。留空由私钥派生；三项要么都填，要么都留空。", ),
        PluginConfigField("POLYMARKET_FUNDER_ADDRESS", "Funder 地址", "string", "实际持有资金和头寸的 Polymarket proxy/deposit wallet 地址。留空则由私钥推出来，下方「钱包」面板会显示推出来的是哪个地址和哪种钱包类型。只有当你要用一个跟签名私钥不同的代理钱包时才需要填。", ),
        PluginConfigField("POLYMARKET_BUILDER_CODE", "Builder Code", "string", "订单归因使用的 Polymarket Builder Code；不使用时可留空。"),
        PluginConfigField("POLYMARKET_RELAYER_API_KEY", "Relayer API Key", "secret", "调用 Polymarket relayer 的用户 API Key。"),
        PluginConfigField("POLYMARKET_RELAYER_API_KEY_ADDRESS", "Relayer Key 地址", "string", "与用户 relayer API Key 关联的链上地址。"),
        PluginConfigField("POLYMARKET_BUILDER_API_KEY", "Builder API Key", "secret", "使用 Builder relayer 身份时的 API Key。"),
        PluginConfigField("POLYMARKET_BUILDER_API_SECRET", "Builder API Secret", "secret", "使用 Builder relayer 身份时的 API Secret。"),
        PluginConfigField("POLYMARKET_BUILDER_API_PASSPHRASE", "Builder Passphrase", "secret", "使用 Builder relayer 身份时的 API Passphrase。"),
        PluginConfigField("POLYMARKET_TRANSFER_RECIPIENT", "转出地址", "string", "TRANSFER_OUT 操作默认接收 pUSD 的 EVM 地址。"),
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
        retired_fields=("POLYMARKET_NETWORK_RULES_JSON", "POLYMARKET_TRADING_CAPITAL"),
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
            "action_fields": [{"name": "note", "label": "附言（可选）", "placeholder": '确认时可以顺带告诉机器人一句话，比如「我只剩这些了，别再要了」——它会读到。', "multiline": True}],
            "dismiss_fields": [{"name": "note", "label": "拒绝理由", "required": True, "placeholder": '拒绝理由（会给到机器人，让它别再原样问一遍）', "multiline": True}],
            "dismiss_label": '驳回',
        }]

    def wallet_notices() -> list:
        """Offer every shortcut this plugin can take, and take none of them unasked.

        Most of what this plugin used to demand is a consequence of the signing key, so an operator
        who has one should not be hunting for an address or a credential that the SDK can state.
        But which way to get a key is theirs: pasting one they already control and letting the bot
        make one are different decisions about who holds the money, and a plugin that picked for
        them would be answering a question it was never asked.
        """
        values = configuration.load()
        if not str(values.get("POLYMARKET_PRIVATE_KEY", "")).strip():
            return [{
                "key": "wallet",
                "title": '钱包',
                "kind": "confirm",
                "description": '这个插件用一个 EVM 钱包签名和持仓。你可以在上面的「钱包私钥」里粘贴自己已有的钱包，也可以让插件替你生成一个新的——两条路都行，插件不会替你选。',
                "content": {"state": "还没有钱包", "note": '生成的钱包由这台机器保管私钥，里面一开始没有钱，要你自己往里转。如果你更希望自己掌握私钥，就直接填上面那个字段，别点这个按钮。'},
                "action_label": '替我生成一个新钱包',
                "dismiss_label": '不用，我自己填',
            }]
        panel = _live_instance_panel()
        return [{
            # Whoever holds this key holds the money. When the operator pasted their own they have
            # it already; when this machine generated one, this is the only copy in existence, and
            # a bot that can spend funds its owner cannot reach is a worse failure than any of the
            # ones the rest of this plugin guards against. So it comes back out on request.
            "key": "wallet_backup",
            "title": '备份钱包私钥',
            "kind": "confirm",
            "description": '这个钱包的私钥就是它的钱。导出来自己存一份——如果这台机器坏了、或者你想换个工具管这些钱，没有它就再也拿不回来。导出的内容只显示在这一页上。',
            "content": {"wallet": panel.get("wallet", ""), "note": '按下按钮后私钥会显示在这里，请复制到你自己的密码管理器或离线备份，然后离开本页。'},
            "action_label": '显示私钥，我要备份',
            "dismiss_label": '不用，我已经有备份了',
        }, {
            "key": "wallet",
            "title": '钱包',
            "kind": "confirm",
            "description": '这是插件根据你的私钥连上去之后实际拿到的身份。地址、钱包类型和 CLOB 凭据都是私钥推出来的，不用你填；下面两个按钮是可选的便利，不点也能跑。',
            "content": panel,
            "action_label": '创建一个 Builder API Key',
            "dismiss_label": '办理交易授权（上链、花 gas）',
        }]

    def _live_instance_panel() -> dict:
        try:
            return _live_instance().wallet_panel()
        except Exception as error:
            return {"error": str(error)[:300]}

    def wallet_action(key: str, name: str, payload: dict) -> dict:
        if key == "wallet_backup":
            if name != "confirm":
                return {"ok": True, "message": '好，那就不显示了。'}
            stored = str(configuration.load().get("POLYMARKET_PRIVATE_KEY", "")).strip()
            if not stored:
                return {"ok": False, "message": '还没有私钥可以导出。'}
            return {
                "ok": True,
                "message": f'私钥：{stored}\n\n复制它，存到你自己的地方。任何拿到这串字符的人都能动这个钱包里的钱。',
            }
        if name == "dismiss" and not str(configuration.load().get("POLYMARKET_PRIVATE_KEY", "")).strip():
            return {"ok": True, "message": '好，等你自己填上私钥。'}
        if not str(configuration.load().get("POLYMARKET_PRIVATE_KEY", "")).strip():
            from eth_account import Account

            account = Account.create()
            saved = dict(configuration.load())
            saved["POLYMARKET_PRIVATE_KEY"] = account.key.hex()
            configuration.save(saved)
            return {
                "ok": True,
                "message": f'已生成钱包 {account.address}。私钥保存在这台机器的插件配置里。'
                           '它现在是空的——往这个地址转 USDC 之后才能交易。',
            }
        try:
            instance = _live_instance()
        except Exception as error:
            return {"ok": False, "message": str(error)[:300]}
        handler = {"confirm": instance.create_builder_key, "dismiss": instance.approve_trading}.get(name)
        if handler is None:
            return {"ok": False, "message": f"Unknown wallet action: {name}"}
        try:
            answer = handler(payload)
        except Exception as error:
            return {"ok": False, "message": str(error)[:300]}
        created = answer.pop("created", None)
        if created:
            saved = dict(configuration.load())
            saved["POLYMARKET_BUILDER_API_KEY"] = created["key"]
            saved["POLYMARKET_BUILDER_API_SECRET"] = created["secret"]
            saved["POLYMARKET_BUILDER_API_PASSPHRASE"] = created["passphrase"]
            configuration.save(saved)
            answer["message"] = (
                f'已创建并保存 Builder API Key {created["key"][:8]}…。'
                '有了它，钱包首次上链部署和后续免 gas 交易才能进行。'
            )
        return answer

    def funding_action(key: str, name: str, payload: dict) -> dict:
        del key
        try:
            instance = _live_instance()
        except Exception as error:
            return {"ok": False, "message": str(error)[:300]}
        handler = {"confirm": instance.confirm_funding, "dismiss": instance.reject_funding}.get(name)
        if handler is None:
            return {"ok": False, "message": f"Unknown funding action: {name}"}
        try:
            return handler(payload)
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
        notices_callback=lambda: [*wallet_notices(), *funding_notices()],
        notice_action_callback=lambda key, name, payload: (
            wallet_action(key, name, payload)
            if key.startswith("wallet")
            else funding_action(key, name, payload)
        ),
        readiness_callback=readiness,
        runtime=PluginRuntime(event_loop.start, event_loop.stop, event_loop.status),
    )
