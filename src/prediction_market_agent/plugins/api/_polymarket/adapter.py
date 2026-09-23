from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from pathlib import Path

from prediction_market_agent.agent.consultation import AgentConsult
from prediction_market_agent.plugins.api._funding import (
    FundingRequests,
    resolve_conflict,
    OperatorNotes,
    shortfall_message,
)
from prediction_market_agent.runtime.broker import ExecutionGateway
from prediction_market_agent.core.domain import AccountState
from prediction_market_agent.plugin_system.contracts import (
    ApiCapabilities,
    Candle,
    Market,
    MarketCandidate,
    OrderBook,
    Outcome,
    PriceLevel,
    Topic,
    TopicDetail,
    TopicPage,
    AccountFunds,
    FundingResult,
    FUNDING_TIMEOUT_SECONDS,
)
from .read import PolymarketReadClient
from .write import PolymarketWriteTransport
from .config import PolymarketPluginConfig


def _tokens(value: str) -> set[str]:
    return {
        item
        for item in re.findall(r"[\w]+", value.casefold(), flags=re.UNICODE)
        if len(item) >= 3
    }


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _milliseconds(value: Any) -> int | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return int(number if number > 10_000_000_000 else number * 1000)
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


class PolymarketApiPlugin:
    """Polymarket Gamma discovery + CLOB V2 market data and trading adapter."""

    name = "polymarket"
    supports_lightweight_search = True
    capabilities = ApiCapabilities(
        realtime_order_book=True,
        candles=True,
        market_search=True,
        settlement_status=True,
        supported_order_types=("MARKET", "LIMIT"),
        write_workflows=("BUY", "SELL", "CANCEL", "REDEEM", "TRANSFER_OUT"),
        data_features=(
            "event_list_and_detail",
            "topic_list_by_deadline",
            "market_and_outcome_normalization",
            "top_of_book_and_depth",
            "displayed_probability",
            "liquidity_and_volume",
            "outcome_price_history",
            "resolution_source_and_rules",
            "negative_risk_metadata",
        ),
        limitations=(
            "Production trading requires a Polygon wallet and CLOB L2 credentials.",
            "CLOB V2 has dynamic taker fees; Gamma fee fields are not treated as a fixed fee quote.",
            "Price-history points are normalized to flat OHLC candles; they are not exchange trade bars.",
            "Write requests require the plugin's configured Polygon wallet and CLOB credentials.",
        ),
        supported_transfer_directions=("OUTBOUND",),
    )

    def __init__(self, environment: Mapping[str, str] | None = None):
        if environment is None:
            raise ValueError("Polymarket plugin configuration mapping is required")
        self.settings = PolymarketPluginConfig.from_mapping(environment)
        self.client = PolymarketReadClient(self.settings)
        self._write_transport = PolymarketWriteTransport(self.settings)
        self.funding_requests = FundingRequests(
            Path(self.settings.funding_request_file)
            if self.settings.funding_request_file
            else Path('config/plugins/polymarket_funding_request.json')
        )
        self.operator_notes = OperatorNotes(
            Path(self.settings.funding_request_file).with_name(
                f'polymarket_operator_notes.json'
            )
            if self.settings.funding_request_file
            else Path('config/plugins/polymarket_operator_notes.json')
        )
        self._events: list[dict[str, Any]] = []

    def sync_time(self) -> None:
        self.client.sync_time()

    def account_funds(self) -> AccountFunds:
        """Ask the platform what the wallet's collateral actually is, and say when that failed.

        Nothing is assumed when it will not say, and there is no configured figure to assume from.
        A number typed once is right until the first trade or transfer and stale forever after, and
        a stale figure read as spendable is how a caller comes to believe it holds money it does
        not. Zero with the error attached is worse to look at and better to act on: nothing gets
        sized from it, and the reason is on the record rather than hidden behind a plausible number.
        """
        try:
            balance = self._write_transport.collateral_balance()
        except Exception as error:
            return AccountFunds(
                available=0.0,
                currency="pUSD",
                source="declared",
                detail={
                    "why": "the platform balance could not be read, and nothing is assumed in its "
                    "place; treat this account as empty until the read works",
                    "error": str(error)[:300],
                },
            )
        return AccountFunds(
            available=balance, currency="pUSD", source="platform", total=balance,
            detail={"asset_type": "COLLATERAL"},
        )

    def ensure_funds(
        self,
        amount: float,
        currency: str,
        *,
        reason: str = "",
        allow_pending: bool = True,
        timeout_seconds: int = FUNDING_TIMEOUT_SECONDS,
        consult: AgentConsult | None = None,
    ) -> FundingResult:
        """Record what is needed and name where to send it. This plugin cannot pull money in."""
        funds = self.account_funds()
        if funds.available >= amount:
            return FundingResult(
                request_id="", state="satisfied", requested=amount, currency=funds.currency,
                available=funds.available, action="none", detail="Already available",
            )
        if not allow_pending:
            return FundingResult(
                request_id="", state="failed", requested=amount, currency=funds.currency,
                available=funds.available, action="external_transfer_required",
                detail=(
                    "Collateral has to arrive from outside this plugin, so it cannot be funded "
                    "within this call."
                ),
            )
        # Two asks cannot both be in front of the operator. Which one should be is the asker's
        # call, not this plugin's, so it is asked before anything is written.
        resolution = resolve_conflict(
            self.funding_requests,
            amount=amount, currency=funds.currency, reason=reason or "No reason was given",
            consult=consult,
        )
        if not resolution.proceed:
            return FundingResult(
                request_id="", state="refused" if resolution.settled_by_asker else "failed",
                requested=amount, currency=funds.currency, available=funds.available,
                action="request_already_waiting", detail=resolution.detail,
            )
        request = self.funding_requests.record(
            amount=resolution.amount, currency=funds.currency, available=funds.available,
            reason=resolution.reason,
            timeout_seconds=timeout_seconds,
        )
        try:
            target = self._write_transport.deposit_target()
            preferred = target["supported_tokens"][0]
            where = (
                f" Send at least {request['shortfall']:.6f} {funds.currency} of "
                f"{preferred['symbol']} ({preferred['contract']}) on {target['source_chain']} to "
                f"the verified Bridge address {target['deposit_address']}, then confirm it in this "
                "plugin's panel."
            )
        except Exception as error:
            where = f" The deposit address could not be read: {str(error)[:200]}"
        return FundingResult(
            request_id=request["request_id"], state="pending", requested=amount,
            currency=funds.currency, available=funds.available,
            action="awaiting_external_transfer", expires_at=int(request["expires_at"]),
            detail=(
                "This plugin cannot pull collateral in: it only transfers out of the wallet it "
                "authenticates as." + where
            ),
        )

    def funding_status(self, request_id: str) -> FundingResult:
        request = self.funding_requests.find(request_id)
        funds = self.account_funds()
        if request is None:
            return FundingResult(
                request_id=request_id, state="failed", requested=0.0, currency=funds.currency,
                available=funds.available, action="unknown_request",
                detail="No request with that id is pending or recently settled",
            )
        return FundingResult(
            request_id=request_id,
            state=str(request.get("state", "pending")),
            requested=float(request.get("amount", 0)),
            currency=str(request.get("currency", funds.currency)),
            available=float(request.get("available", funds.available)),
            action=str(request.get("state", "pending")),
            detail=str(request.get("detail", "")),
            operator_note=str(request.get("operator_note", "")),
        )

    def confirm_funding(self, values: dict[str, Any] | None = None) -> dict[str, Any]:
        """Check whether the money actually arrived, instead of believing that it did."""
        request = self.funding_requests.pending()
        if request is None:
            return {"ok": False, "message": "There is no funding request to confirm"}
        funds = self.account_funds()
        if funds.source != "platform":
            return {
                "ok": False,
                "message": (
                    "The balance could not be read from the platform, so this confirmation cannot "
                    f"be checked: {funds.detail.get('error', 'no detail')}"
                ),
            }
        target = float(request["amount"])
        if funds.available + 1e-9 < target:
            message = shortfall_message(request, funds.available)
            if funds.available > float(request["available_when_asked"]) + 1e-9:
                # Something arrived, just not all of it. This is a terminal answer to this ask so
                # the decision logic is consulted exactly once. It may use the amount that really
                # arrived, or file a new target for the remaining need and wait again. Leaving this
                # request pending would never wake that choice; calling it satisfied would lie.
                self.funding_requests.settle(
                    operator_note=str((values or {}).get('note', '')),
                    state="partial", available=funds.available, detail=message
                )
            return {"ok": False, "message": message}
        self.funding_requests.settle(
            operator_note=str((values or {}).get('note', '')),
            state="satisfied", available=funds.available,
            detail=f"{funds.available:.6f} {funds.currency} confirmed available",
        )
        return {
            "ok": True,
            "message": (
                f"Confirmed: {funds.available:.6f} {funds.currency} is available, meeting the "
                f"{target:.6f} requested"
            ),
        }

    def reject_funding(self, values: dict[str, Any] | None = None) -> dict[str, Any]:
        """Refusing needs a reason, because the reason is what gets relayed back to the robot.

        A bare "no" tells it nothing it can act on, so it asks again identically on the next cycle
        and the operator answers the same prompt forever. This plugin will not file a refusal
        without one.
        """
        note = str((values or {}).get("note", "")).strip()
        if not note:
            return {
                "ok": False,
                "message": "请填写拒绝理由：机器人会读到它，没有理由它下一轮还会原样再问一次",
            }
        funds = self.account_funds()
        self.funding_requests.settle(
            operator_note=note,
            state="refused", available=funds.available, detail="The operator dismissed the request"
        )
        return {"ok": True, "message": "The funding request was dismissed"}

    def deposit_panel(self, funds: AccountFunds | None = None) -> dict[str, Any]:
        """Where to send money, in what, on which chain - and what the account holds right now.

        Offered whenever the plugin is loaded rather than only when the robot has asked for money:
        an operator who decides to top up should not have to wait to be asked.
        """
        # A Future is accepted internally so the notices view can read the balance at the same
        # time as this independent Bridge route. Public callers may continue passing AccountFunds
        # or nothing.
        instructions = None
        instructions_error = None
        try:
            instructions = self._write_transport.deposit_instructions()
        except Exception as error:
            instructions_error = error
        funds = self._resolved_funds(funds)
        panel: dict[str, Any] = {
            "当前可用": f"{funds.available:.6f} {funds.currency}",
            "余额来源": funds.source,
        }
        if instructions_error is not None:
            panel["读取充值地址失败"] = str(instructions_error)[:300]
            return panel
        assert instructions is not None
        panel.update({
            "链": f"{instructions['chain']}（chain id {instructions['chain_id']}）",
            "Polymarket Bridge 充值地址": instructions["address"],
            "转什么币": instructions["deposit_currency"],
            "可入账币种（名称 / 缩写 / 网络 / 合约，实时读取）": instructions["supported_token_labels"],
            "最终入账账户": instructions["destination_address"],
            "账户抵押币": f"{instructions['account_token_symbol']}（{instructions['account_token_contract']}）",
            "到账需要确认数": instructions["minimum_confirmations"],
            "注意": instructions["warnings"],
        })
        return panel

    def deposit_status(self, txid: str) -> dict[str, Any]:
        """Where one deposit got to, plus what the account holds after it."""
        status = dict(self._write_transport.deposit_status(txid))
        try:
            funds = self.account_funds()
            status["available"] = funds.available
            status["currency"] = funds.currency
        except Exception as error:
            status["balance_error"] = str(error)[:200]
        return status

    def withdrawal_panel(self, funds: AccountFunds | None = None) -> dict[str, Any]:
        """Current spendable balance and exact default-chain withdrawal choices."""
        choices = None
        choices_error = None
        try:
            choices = self._write_transport.withdrawal_options(all_chains=True)
        except Exception as error:
            choices_error = error
        funds = self._resolved_funds(funds)
        panel: dict[str, Any] = {
            "当前可转出": f"{funds.available:.6f} {funds.currency}",
            "余额来源": funds.source,
            "默认接收地址": self.settings.transfer_recipient or "未设置；本次操作可直接填写",
        }
        if choices_error is not None:
            panel["读取提现币种失败"] = str(choices_error)[:300]
            return panel
        assert choices is not None
        panel["可提网络（实时读取）"] = choices["chains"]
        panel["可转出币种（名称 / 缩写 / 网络 / 合约，实时读取）"] = [
            item["label"] for item in choices["options"]
        ]
        panel["默认网络和币种"] = choices["default"]["label"]
        panel["_routes"] = [
            {
                "value": f"{item['chain_id']}:{item['contract']}",
                "label": item["label"],
            }
            for item in choices["options"]
        ]
        return panel

    def _resolved_funds(self, funds: Any | None) -> AccountFunds:
        """Resolve the notices view's shared balance Future without exposing it as public API."""
        if funds is None:
            return self.account_funds()
        result = getattr(funds, "result", None)
        return result() if callable(result) else funds

    def withdrawal_status(self, bridge_address: str) -> dict[str, Any]:
        """Summarise the latest official state for a submitted withdrawal route."""
        status = self._write_transport.bridge_status(bridge_address)
        transactions = status["transactions"]
        states = [str(item.get("status", "UNKNOWN")) for item in transactions if isinstance(item, dict)]
        return {
            "Bridge 地址": bridge_address,
            "状态": " / ".join(states) if states else "尚未被 Bridge 检测到",
            "路线记录数": len(transactions),
        }

    def withdraw(self, values: dict[str, Any] | None = None) -> dict[str, Any]:
        """Execute an operator-requested bridge withdrawal with no hidden destination defaults."""
        payload = values or {}
        recipient = str(payload.get("recipient") or self.settings.transfer_recipient).strip()
        amount = str(payload.get("amount", "")).strip()
        if not amount:
            return {"ok": False, "message": "请填写转出金额"}
        if not recipient:
            return {"ok": False, "message": "请填写交易所或钱包的接收地址"}
        destination = str(payload.get("destination", "")).strip()
        if destination:
            raw_chain, separator, token_address = destination.partition(":")
            if not separator:
                return {"ok": False, "message": "目标币种选项格式无效，请刷新插件页面后重选"}
        else:
            # Backward compatibility for an already-open page from before the bound route selector.
            raw_chain = str(payload.get("chain_id", "")).strip()
            token_address = str(payload.get("token_contract", "")).strip()
        try:
            chain_id = int(raw_chain) if raw_chain else self.settings.chain_id
            result = self._write_transport.withdraw(
                amount,
                recipient=recipient,
                chain_id=chain_id,
                token_address=token_address,
            )
        except Exception as error:
            return {"ok": False, "message": str(error)[:500]}
        destination = result["destination"]
        quote = result.get("quote", {})
        return {
            "ok": True,
            "message": (
                f"已提交 {result['amount']:.6f} {self.account_funds().currency}，目标为 "
                f"{destination['name']} / {destination['symbol']}（{destination['network']}）"
            ),
            "reveal": (
                f"接收地址：{result['recipient']}\n"
                f"目标币种：{destination['name']} / {destination['symbol']}\n"
                f"目标网络：{destination['network']}（chain id {destination['chain_id']}）\n"
                f"目标合约：{destination['contract']}\n"
                f"转出金额：{result['amount']:.6f}\n"
                f"预计到账价值：${float(quote.get('estOutputUsd', 0) or 0):.6f}\n"
                f"Bridge 状态：{result['status']}\n"
                f"交易：{result['transaction']}"
            ),
            **result,
        }

    def confirm_deposit(self, values: dict[str, Any] | None = None) -> dict[str, Any]:
        """Check a deposit the operator says they made, and let a waiting request off if it covers it.

        The money arriving and the robot's request being answered are the same event seen twice, so
        confirming the transaction settles the request when it covers what was asked - otherwise
        the operator would fund the account and still be nagged for the money they just sent.
        """
        txid = str((values or {}).get("txid", "")).strip()
        if not txid:
            return {"ok": False, "state": "invalid", "message": "请填写充值交易号（txid）"}
        try:
            status = self.deposit_status(txid)
        except Exception as error:
            return {"ok": False, "state": "error", "message": str(error)[:300]}
        state = str(status.get("state", ""))
        available = status.get("available")
        balance = (
            f"；当前可用 {available:.6f} {status.get('currency', '')}"
            if isinstance(available, (int, float)) else ""
        )
        if state != "arrived":
            return {"ok": False, "state": state, "txid": txid,
                    # Still on its way is a wait, not a mistake, and the page draws the two apart.
                    "pending": state == "confirming",
                    "message": str(status.get("detail", "")) + balance, **status}
        # The chain says the money is at the address. Whether the platform counts it as spendable
        # is the platform's own answer, and the balance beside it is where that shows - saying
        # "arrived" while the balance still reads zero would be the one thing nobody could act on.
        waiting_on_platform = isinstance(available, (int, float)) and available <= 0
        settled = None
        request = self.funding_requests.pending()
        if request is not None and isinstance(available, (int, float)):
            if available + 1e-9 >= float(request["amount"]):
                self.funding_requests.settle(
                    operator_note=str((values or {}).get("note", "")),
                    state="satisfied", available=available,
                    detail=f"deposit {txid} credited {status.get('amount', 0):.6f}",
                )
                settled = "satisfied"
            elif available > float(request["available_when_asked"]) + 1e-9:
                # Partial credit has to wake the decision once: only it can decide whether the
                # current amount is enough for a freshly justified trade or whether to ask for the
                # target again and keep waiting. No increase means there is no new answer yet.
                self.funding_requests.settle(
                    operator_note=str((values or {}).get("note", "")),
                    state="partial", available=available,
                    detail=shortfall_message(request, available),
                )
                settled = "partial"
        note = "；平台还没把它算进可用余额，通常几分钟内" if waiting_on_platform else ""
        return {"ok": True, "state": "arrived", "txid": txid, "request": settled,
                "pending": waiting_on_platform,
                "message": str(status.get("detail", "")) + balance + note, **status}

    # -- What the operator wrote with their money ------------------------------------------------
    # Collected here because this is where it was typed, and handed over untouched: what a note
    # means, and what to do about it, is the runtime's to work out with a model, not this plugin's.
    def note_from_operator(self, text: str, **context: Any) -> str:
        return self.operator_notes.add(text, **context)

    def operator_messages(self) -> list[dict[str, Any]]:
        return self.operator_notes.pending()

    def acknowledge_operator_messages(self, identifiers: list[str]) -> None:
        self.operator_notes.acknowledge(identifiers)

    def funding_panel(self) -> dict[str, Any]:
        funds = self.account_funds()
        panel: dict[str, Any] = {
            "pending_request": self.funding_requests.pending(),
            "available": funds.available,
            "currency": funds.currency,
            "source": funds.source,
            "detail": funds.detail,
            "funding_mode": "external_transfer_then_confirm",
        }
        try:
            panel["deposit_target"] = self._write_transport.deposit_target()
        except Exception as error:
            panel["deposit_target_error"] = str(error)[:300]
        return panel

    def wallet_panel(self) -> dict[str, Any]:
        """What this key turns out to be, and what is still missing before it can trade."""
        panel: dict[str, Any] = {}
        try:
            self._write_transport.prepare_account_read()
        except Exception as error:
            return {"error": str(error)[:300]}
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="polymarket-wallet") as pool:
            facts = pool.submit(self._write_transport.wallet_facts)
            keys = pool.submit(self._write_transport.builder_api_keys)
            try:
                panel.update(facts.result())
            except Exception as error:
                panel["error"] = str(error)[:300]
            try:
                panel["builder_api_keys"] = keys.result()
            except Exception as error:
                panel["builder_api_keys_error"] = str(error)[:300]
        return panel

    def exportable_secrets(self) -> dict[str, str]:
        try:
            return self._write_transport.exportable_credentials()
        except Exception as error:
            return {"CLOB 凭据": f"读取失败：{str(error)[:200]}"}

    def create_builder_key(self, values: dict[str, Any]) -> dict[str, Any]:
        """Make the key and hand it back to be stored, because a key nobody kept is worse than none.

        Polymarket issues it once. Creating one and not saving it leaves a key on the account that
        nothing here can use and the operator cannot see, and the next attempt makes another.
        """
        del values
        return {"ok": True, "created": self._write_transport.create_builder_api_key()}

    def approve_trading(self, values: dict[str, Any]) -> dict[str, Any]:
        del values
        self._write_transport.setup_trading_approvals()
        return {"ok": True, "message": "交易授权已提交。"}

    def topic_page_size(self) -> int:
        return self.settings.topic_page_size

    @staticmethod
    def _topic(item: dict[str, Any]) -> Topic:
        status = "CLOSED" if item.get("closed") else "OPEN" if item.get("active") else "INACTIVE"
        return Topic(
            topic_id=str(item.get("id", "")),
            title=str(item.get("title", "")),
            question=str(item.get("title", "")),
            description=str(item.get("description", "")),
            category=str(item.get("category", "")),
            status=status,
            liquidity_usdt=float(item.get("liquidity") or 0),
            volume_usdt=float(item.get("volume") or 0),
            slug=str(item.get("slug", "")),
        )

    @staticmethod
    def _market(item: dict[str, Any]) -> Market:
        names = _json_list(item.get("outcomes"))
        prices = _json_list(item.get("outcomePrices"))
        token_ids = _json_list(item.get("clobTokenIds"))
        outcomes: list[Outcome] = []
        for index, name in enumerate(names):
            price: float | None = None
            if index < len(prices) and prices[index] not in {None, ""}:
                try:
                    price = float(prices[index])
                except (TypeError, ValueError):
                    price = None
            outcome_id = str(token_ids[index]) if index < len(token_ids) else ""
            outcomes.append(Outcome(outcome_id, str(name), price))
        accepting = bool(item.get("acceptingOrders")) and bool(item.get("active", True))
        status = "OPEN" if accepting and not item.get("closed") else "CLOSED"
        question = str(item.get("question", ""))
        return Market(
            market_id=str(item.get("conditionId") or item.get("id", "")),
            title=question,
            question=question,
            status=status,
            liquidity_usdt=float(item.get("liquidity") or 0),
            volume_usdt=float(item.get("volume") or 0),
            outcomes=tuple(outcomes),
        )

    def list_topics(self, *, offset: int, limit: int) -> TopicPage:
        batch = self.client.list_events(offset=offset, limit=limit)
        if offset == 0:
            self._events = []
        self._events.extend(batch)
        topics = tuple(self._topic(item) for item in batch)
        return TopicPage(topics, len(batch) == limit, offset + len(batch))

    def list_topics_by_deadline(
        self, *, offset: int, limit: int, after_ms: int, before_ms: int
    ) -> TopicPage:
        """What settles inside a window, soonest first - the listing this venue will not volunteer."""
        stamp = lambda value: (
            datetime.fromtimestamp(value / 1000, timezone.utc).isoformat().replace("+00:00", "Z")
        )
        batch = self.client.list_events_by_deadline(
            offset=offset, limit=limit, after=stamp(after_ms), before=stamp(before_ms)
        )
        self._events.extend(item for item in batch if item not in self._events)
        topics = tuple(self._topic(item) for item in batch)
        return TopicPage(topics, len(batch) == limit, offset + len(batch))

    def get_topic(self, topic_id: str) -> TopicDetail:
        raw = self.client.get_event(topic_id)
        topic = self._topic(raw)
        markets = tuple(
            self._market(item) for item in (raw.get("markets") or []) if isinstance(item, dict)
        )
        first_yes = next(
            (
                outcome.outcome_id
                for market in markets
                for outcome in market.outcomes
                if outcome.name.upper() == "YES" and outcome.outcome_id
            ),
            "",
        )
        return TopicDetail(
            topic=topic,
            start_time_ms=_milliseconds(raw.get("startDate")),
            end_time_ms=_milliseconds(raw.get("endDate")),
            fee_bps=0,
            markets=markets,
            chart_type="EVENT",
            reference_symbol=first_yes,
            resolution={
                "resolutionSource": raw.get("resolutionSource"),
                "negRisk": raw.get("negRisk", raw.get("enableNegRisk")),
                "restricted": raw.get("restricted"),
            },
        )

    def get_order_book(self, market_id: str, outcome_id: str) -> OrderBook:
        del market_id
        raw = self.client.get_order_book(outcome_id)

        def levels(name: str) -> tuple[PriceLevel, ...]:
            return tuple(
                PriceLevel(float(item["price"]), float(item.get("size") or 0))
                for item in (raw.get(name) or [])
                if isinstance(item, dict)
            )

        return OrderBook(
            bids=levels("bids"),
            asks=levels("asks"),
            observed_at_ms=_milliseconds(raw.get("timestamp")),
        )

    def get_candles(
        self, reference_symbol: str, interval: str = "1m", limit: int = 120
    ) -> list[Candle]:
        fidelity = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}.get(
            interval, 5
        )
        api_interval = "1h" if fidelity <= 5 else "1d" if fidelity <= 60 else "1w"
        rows = self.client.get_price_history(
            reference_symbol, interval=api_interval, fidelity=fidelity
        )[-limit:]
        duration = fidelity * 60_000
        return [
            Candle(
                open_time_ms=int(float(item["t"]) * 1000),
                close_time_ms=int(float(item["t"]) * 1000) + duration,
                open=float(item["p"]),
                high=float(item["p"]),
                low=float(item["p"]),
                close=float(item["p"]),
                volume=0.0,
            )
            for item in rows
            if item.get("t") is not None and item.get("p") is not None
        ]

    def create_write_gateway(self, state: AccountState) -> ExecutionGateway:
        return ExecutionGateway(
            state,
            platform=self.name,
            write_transport=self._write_transport,
        )

    def write_transport(self) -> PolymarketWriteTransport:
        return self._write_transport

    def configuration_manifest(self) -> dict[str, Any]:
        return self.settings.manifest()

    RESOLVED_PRICE = 0.99
    """How close to certainty a settled outcome's price gets. Anything below this is still trading."""

    def outcome_won(
        self, detail: TopicDetail, market: Market, outcome: Outcome
    ) -> bool | None:
        """Say whether this outcome resolved in the money, or nothing while it has not resolved.

        A settled market prices its outcomes at the truth: the winner goes to one and the losers to
        zero. That is what "resolved" looks like here - there is no separate winner field - so the
        test is whether the price has left the trading range entirely, not merely which side is
        ahead. A market still open at 0.97 is a market that can still be wrong, and calling it
        settled would book a profit that has not happened.
        """
        event = self.client.get_event(str(detail.topic.topic_id))
        for item in event.get("markets") or []:
            if str(item.get("conditionId") or item.get("id", "")) != market.market_id:
                continue
            if not item.get("closed"):
                return None
            names = _json_list(item.get("outcomes"))
            prices = _json_list(item.get("outcomePrices"))
            for index, name in enumerate(names):
                if str(name) != outcome.name or index >= len(prices):
                    continue
                try:
                    price = float(prices[index])
                except (TypeError, ValueError):
                    return None
                if price >= self.RESOLVED_PRICE:
                    return True
                if price <= 1 - self.RESOLVED_PRICE:
                    return False
                return None
        return None

    def close(self) -> None:
        self._write_transport.close()

    def search_market_candidates(
        self, query: str, limit: int, *, lightweight: bool = False
    ) -> list[MarketCandidate]:
        query_tokens = _tokens(query)
        ranked: list[tuple[float, dict[str, Any]]] = []
        for event in self._events:
            candidate_tokens = _tokens(
                f"{event.get('title', '')} {event.get('description', '')}"
            )
            if query_tokens and candidate_tokens:
                score = len(query_tokens & candidate_tokens) / len(query_tokens | candidate_tokens)
                if score > 0:
                    ranked.append((score, event))
        results: list[MarketCandidate] = []
        for score, raw in sorted(ranked, key=lambda item: item[0], reverse=True)[:limit]:
            topic = self._topic(raw)
            detail: TopicDetail | None = None
            books: dict[str, OrderBook] = {}
            if not lightweight:
                try:
                    detail = self.get_topic(topic.topic_id)
                    for market in detail.markets[:4]:
                        yes = next(
                            (outcome for outcome in market.outcomes if outcome.name.upper() == "YES"),
                            None,
                        )
                        if yes and yes.outcome_id:
                            books[yes.outcome_id] = self.get_order_book(
                                market.market_id, yes.outcome_id
                            )
                except (KeyError, TypeError, ValueError, RuntimeError):
                    detail = None
                    books = {}
            results.append(
                MarketCandidate(
                    platform=self.name,
                    topic=topic,
                    detail=detail,
                    books=books,
                    retrieval_score=round(score, 4),
                )
            )
        return results
