"""Complete API plugin example with static reads and an online HTTP write transport."""

import json
import urllib.parse
import urllib.request

from prediction_market_agent.runtime.broker import ExecutionGateway
from prediction_market_agent.sdk.contracts import (
    ApiCapabilities,
    Market,
    MarketCandidate,
    OrderBook,
    Outcome,
    PriceLevel,
    Topic,
    TopicDetail,
    TopicPage,
)
from prediction_market_agent.sdk.config_io import json_file_callbacks
from prediction_market_agent.sdk.discovery import (
    PluginConfigField,
    PluginConfiguration,
    PluginInitializationContext,
    PluginSpec,
)
from prediction_market_agent.core.risk import NetworkWriteGate


class ExampleHttpWriteTransport:
    def __init__(self, base_url, token, gate):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.gate = gate

    def _post(self, path, payload):
        url = self.base_url + path
        self.gate.check("POST", url)
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
        if not isinstance(result, dict):
            raise RuntimeError("Example API response must be a JSON object")
        return result

    def get_quote(self, **values):
        return self._post("/quote", values)

    def place_order(self, **values):
        result = self._post("/order", values)
        status = str(result.get("status", "")).upper()
        if status not in {"OPEN", "FILLED", "CANCELED", "REJECTED", "FAILED"}:
            raise RuntimeError("Example service must return a normalized order status")
        return {**result, "status": status}

    def cancel_orders(self, order_ids):
        return self._post("/cancel", {"order_ids": order_ids})

    def redeem(self, outcome_ids):
        return self._post("/redeem", {"outcome_ids": outcome_ids})

    def transfer(self, direction, amount):
        return self._post("/transfer", {"direction": direction, "amount": amount})


class StaticDemoPlugin:
    name = "static_demo"
    capabilities = ApiCapabilities(
        realtime_order_book=False,
        candles=False,
        market_search=True,
        settlement_status=False,
        supported_order_types=("MARKET", "LIMIT"),
        write_workflows=("GET_QUOTE", "BUY", "SELL", "CANCEL", "REDEEM", "TRANSFER"),
        data_features=("static_demo_topic", "static_two_sided_book"),
        limitations=("Read data is deterministic; every declared write uses the configured HTTP server.",),
        supported_transfer_directions=("INBOUND", "OUTBOUND"),
    )

    def __init__(self, base_url, api_token):
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("BASE_URL must be an HTTP(S) URL")
        self.network_rule_engine = NetworkWriteGate(
            allowed_hosts=frozenset({parsed.hostname}),
            allowed_schemes=frozenset({parsed.scheme}),
            allowed_methods=frozenset({"POST"}),
            allowed_read_paths=frozenset(),
            allowed_paths_by_method={"POST": frozenset({"/quote", "/order", "/cancel", "/redeem", "/transfer"})},
            target_name="network:static_demo",
        )
        self._write_transport = ExampleHttpWriteTransport(base_url, api_token, self.network_rule_engine)

    def sync_time(self):
        return None

    def list_topics(self, *, offset, limit):
        topics = () if offset or limit <= 0 else (
            Topic("demo-topic", "Demo event", "Will the demo resolve YES?", "SDK example.", "demo", "OPEN", 10000, 0, "demo-event"),
        )
        return TopicPage(topics, False, offset + len(topics))

    def get_topic(self, topic_id):
        if topic_id != "demo-topic":
            raise KeyError(topic_id)
        topic = self.list_topics(offset=0, limit=1).topics[0]
        market = Market("demo-market", "YES", topic.question, "OPEN", 10000, 0, (Outcome("demo-yes", "YES", 0.5), Outcome("demo-no", "NO", 0.5)))
        return TopicDetail(topic, 0, 4102444800000, 0, (market,), "EVENT", "")

    def get_order_book(self, market_id, outcome_id):
        if market_id != "demo-market" or outcome_id not in {"demo-yes", "demo-no"}:
            raise KeyError((market_id, outcome_id))
        return OrderBook((PriceLevel(0.49, 100),), (PriceLevel(0.51, 100),), 0)

    def get_candles(self, reference_symbol, interval="1m", limit=120):
        del reference_symbol, interval, limit
        return []

    def create_write_gateway(self, state, risk):
        return ExecutionGateway(state, risk, platform=self.name, write_transport=self._write_transport)

    def search_market_candidates(self, query, limit):
        if not query.strip() or limit <= 0:
            return []
        detail = self.get_topic("demo-topic")
        return [MarketCandidate(self.name, detail.topic, detail, {"demo-yes": self.get_order_book("demo-market", "demo-yes")}, 1.0)]

    def write_transport(self):
        return self._write_transport

    def configuration_manifest(self):
        return {"external_network": True, "api_token_present": bool(self._write_transport.token)}

    def outcome_won(self, detail, market, outcome):
        del detail, market, outcome
        return None

    def close(self):
        return None


def initialize_plugin(context: PluginInitializationContext) -> PluginSpec:
    load, save, storage = json_file_callbacks(
        context.working_directory / "examples" / "plugin_configs" / "static_demo.json"
    )
    configuration = PluginConfiguration(
        fields=(
            PluginConfigField("BASE_URL", "服务 URL", "string", "实现示例 quote/order/cancel/redeem/transfer JSON 端点的 HTTP(S) 服务根地址。", required=True),
            PluginConfigField("API_TOKEN", "API Token", "secret", "示例服务 Authorization Bearer 凭证；原样交给远端验证。"),
        ),
        load_callback=load,
        save_callback=save,
        storage=storage,
    )
    instances = []

    def factory(config):
        del config
        values = configuration.load()
        instance = StaticDemoPlugin(values["BASE_URL"], values.get("API_TOKEN", ""))
        instances.append(instance)
        return instance

    def teardown():
        while instances:
            instances.pop().close()

    return PluginSpec(
        kind="api",
        name="static_demo",
        description="静态读取、线上 HTTP 写传输和私有配置齐全的 API 插件开发示例。",
        origin=str(context.module_path),
        factory=factory,
        configuration=configuration,
        teardown=teardown,
    )
