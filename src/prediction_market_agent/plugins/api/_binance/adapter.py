from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from pathlib import Path

from prediction_market_agent.agent.consultation import AgentConsult
from prediction_market_agent.plugins.api._funding import (
    FundingRequests,
    OperatorNotes,
    resolve_conflict,
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
from .write import BinancePredictionWriteTransport
from .config import BinancePluginConfig
from .read import BinancePredictionReadClient


def _tokens(value: str) -> set[str]:
    return {
        item
        for item in re.findall(r"[\w]+", value.casefold(), flags=re.UNICODE)
        if len(item) >= 3
    }


class BinancePredictionApiPlugin:
    """Binance production read and write API adapter."""

    name = "binance"
    capabilities = ApiCapabilities(
        realtime_order_book=True,
        candles=True,
        market_search=True,
        settlement_status=True,
        supported_order_types=("MARKET", "LIMIT"),
        write_workflows=("GET_QUOTE", "BUY", "SELL", "CANCEL", "REDEEM", "TRANSFER"),
        data_features=(
            "topic_list",
            "market_detail",
            "top_of_book_and_depth",
            "displayed_probability",
            "liquidity_and_volume",
            "crypto_reference_candles",
            "resolution_metadata",
        ),
        limitations=(
            "Private Prediction endpoints require Binance signed requests and SAS for writes.",
            "Historical prediction-market trades are not exposed by this adapter.",
            "Write requests require the plugin's configured credentials and wallet identifiers.",
        ),
        supported_transfer_directions=("INBOUND", "OUTBOUND"),
    )

    def __init__(self, environment: Mapping[str, str] | None = None):
        if environment is None:
            raise ValueError("Binance plugin configuration mapping is required")
        self.settings = BinancePluginConfig.from_mapping(environment)
        self._write_transport = BinancePredictionWriteTransport(self.settings)
        self.funding_requests = FundingRequests(
            Path(self.settings.funding_request_file)
            if self.settings.funding_request_file
            else Path('config/plugins/binance_funding_request.json')
        )
        self.operator_notes = OperatorNotes(
            Path(self.settings.funding_request_file).with_name(
                f'binance_operator_notes.json'
            )
            if self.settings.funding_request_file
            else Path('config/plugins/binance_operator_notes.json')
        )
        self.client = BinancePredictionReadClient(
            self.settings.api_key,
            self.settings.api_secret,
            self.settings.base_url,
            http_proxy=self.settings.http_proxy,
        )
        self._topics: list[dict[str, Any]] = []

    def sync_time(self) -> None:
        self.client.sync_time()

    def account_funds(self) -> AccountFunds:
        """What the prediction wallet can spend - which is not the spot balance.

        Predictions trade out of a separate Web3 prediction wallet; spot or funding money only
        becomes spendable after an INBOUND transfer. This plugin implements no endpoint that reads
        the prediction wallet itself, so `available` is the configured figure and says so. The spot
        balance is reported alongside as what a top-up could draw on, never as what is spendable:
        presenting the source of funds as the funds themselves would tell the model it can trade
        money that is sitting somewhere else.
        """
        detail: dict[str, Any] = {
            "why_declared": "no endpoint here reads the Web3 prediction wallet; available is "
            "BINANCE_TRADING_CAPITAL as configured",
            "transfer_source_account": self.settings.account_type,
        }
        if self.settings.account_type.upper() == "SPOT":
            try:
                usdt = self.client.spot_balances().get("USDT", {"free": 0.0, "locked": 0.0})
                detail["fundable_from_spot"] = usdt["free"]
                detail["spot_locked"] = usdt["locked"]
            except Exception as error:
                detail["fundable_from_spot_error"] = str(error)[:300]
        else:
            detail["fundable_from"] = (
                f"{self.settings.account_type} account, which this plugin cannot read"
            )
        return AccountFunds(
            available=float(self.settings.trading_capital),
            currency="USDT",
            source="declared",
            detail=detail,
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
        """Record what is needed and wait for approval; do not move money on the ask alone.

        The wallet can be drawn on automatically, which is exactly why it is not: an ask arrives
        mid-decision, from reasoning nobody has read yet, and money leaving on it would be a
        transfer at a moment nobody chose. The caller gets an id to ask about later.
        """
        funds = self.account_funds()
        if currency.upper() != "USDT":
            return FundingResult(
                request_id="", state="failed", requested=amount, currency=currency,
                available=funds.available, action="unsupported_currency",
                detail=f"This account settles in USDT, not {currency}",
            )
        if funds.available >= amount:
            return FundingResult(
                request_id="", state="satisfied", requested=amount, currency="USDT",
                available=funds.available, action="none", detail="Already available",
            )
        if not allow_pending:
            return FundingResult(
                request_id="", state="failed", requested=amount, currency="USDT",
                available=funds.available, action="approval_required",
                detail=(
                    "This account needs an operator to approve the transfer, so it cannot be "
                    "funded within this call."
                ),
            )
        # Two asks cannot both be in front of the operator. Which one should be is the asker's
        # call, not this plugin's, so it is asked before anything is written.
        resolution = resolve_conflict(
            self.funding_requests,
            amount=amount, currency="USDT", reason=reason or "No reason was given",
            consult=consult,
        )
        if not resolution.proceed:
            return FundingResult(
                request_id="", state="refused" if resolution.settled_by_asker else "failed",
                requested=amount, currency="USDT", available=funds.available,
                action="request_already_waiting", detail=resolution.detail,
            )
        request = self.funding_requests.record(
            amount=resolution.amount, currency="USDT", available=funds.available,
            reason=resolution.reason,
            timeout_seconds=timeout_seconds,
        )
        return FundingResult(
            request_id=request["request_id"], state="pending", requested=amount,
            currency="USDT", available=funds.available, action="awaiting_operator_approval",
            expires_at=int(request["expires_at"]),
            detail=(
                f"A transfer of {request['shortfall']:.6f} USDT into the prediction wallet is "
                "waiting for approval in this plugin's panel. Nothing moves until it is approved."
            ),
        )

    def funding_status(self, request_id: str) -> FundingResult:
        """Where an earlier request got to, by the id that request returned."""
        request = self.funding_requests.find(request_id)
        funds = self.account_funds()
        if request is None:
            return FundingResult(
                request_id=request_id, state="failed", requested=0.0, currency="USDT",
                available=funds.available, action="unknown_request",
                detail="No request with that id is pending or recently settled",
            )
        return FundingResult(
            request_id=request_id,
            state=str(request.get("state", "pending")),
            requested=float(request.get("amount", 0)),
            currency=str(request.get("currency", "USDT")),
            available=float(request.get("available", funds.available)),
            action=str(request.get("state", "pending")),
            detail=str(request.get("detail", "")),
            operator_note=str(request.get("operator_note", "")),
        )

    def approve_funding(self, values: dict[str, Any] | None = None) -> dict[str, Any]:
        """Carry out the standing request, drawing on the configured source account."""
        request = self.funding_requests.pending()
        if request is None:
            return {"ok": False, "message": "There is no funding request to approve"}
        shortfall = float(request["shortfall"]) or float(request["amount"])
        try:
            self._write_transport.transfer("INBOUND", str(shortfall))
        except Exception as error:
            message = f"The transfer failed: {str(error)[:300]}"
            self.funding_requests.settle(
                operator_note=str((values or {}).get('note', '')),
                state="failed", available=float(request["available_when_asked"]), detail=message
            )
            return {"ok": False, "message": message}
        moved_to = float(request["available_when_asked"]) + shortfall
        target = float(request["amount"])
        # A transfer the venue accepted can still land short; report what arrived, not what was
        # asked for, so a caller reading the outcome is not told a shortfall was covered.
        state = "satisfied" if moved_to + 1e-9 >= target else "partial"
        message = (
            f"Moved {shortfall:.6f} USDT from the {self.settings.account_type} account into the "
            "prediction wallet"
        )
        self.funding_requests.settle(
            state=state, available=moved_to, detail=message,
            operator_note=str((values or {}).get("note", "")).strip(),
        )
        return {"ok": True, "message": message}

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
            state="refused", available=funds.available, detail="The operator declined the transfer"
        )
        return {"ok": True, "message": "The funding request was dismissed; nothing moved"}

    DEPOSIT_COIN = "USDT"
    """What this account settles in, so it is what a deposit has to be."""

    def deposit_panel(self, network: str = "") -> dict[str, Any]:
        """Where money enters this account, on which chain, and what it holds now.

        Two steps here rather than one: a deposit lands in the exchange account, and only an
        inbound transfer makes it spendable on predictions. Saying so up front is the difference
        between an operator who tops up and one who tops up and then waits for a balance that was
        never going to move on its own.
        """
        funds = self.account_funds()
        panel: dict[str, Any] = {
            "预测账户可用": f"{funds.available:.6f} {funds.currency}（{funds.source}）",
            "现货账户可划转": funds.detail.get("fundable_from_spot", "读不到"),
            "充值币种": self.DEPOSIT_COIN,
            "两步": "先充值到币安账户，再由这里划转进预测钱包才可下单",
        }
        try:
            networks = self.client.deposit_networks(self.DEPOSIT_COIN)
        except Exception as error:
            panel["读取可充值网络失败"] = str(error)[:300]
            return panel
        open_networks = [item for item in networks if item["deposit_open"]]
        panel["可充值网络"] = [
            f"{item['network']}（{item['name']}，{item['minimum_confirmations']} 个确认"
            + ("，需要 memo/tag" if item["needs_memo"] else "") + "）"
            for item in open_networks
        ] or ["交易所目前没有开放这个币的充值网络"]
        chosen = str(network or "").upper()
        if not chosen:
            preferred = next((item for item in open_networks if item["default"]), None)
            chosen = str((preferred or (open_networks[0] if open_networks else {})).get("network", ""))
        if not chosen:
            return panel
        try:
            address = self.client.deposit_address(self.DEPOSIT_COIN, chosen)
        except Exception as error:
            panel["读取充值地址失败"] = f"{chosen}：{str(error)[:250]}"
            return panel
        entry = next((item for item in networks if item["network"] == chosen), {})
        panel.update({
            "链": f"{chosen}（{entry.get('name', '')}）",
            "收款地址": address["address"],
            "到账需要确认数": entry.get("minimum_confirmations", "?"),
        })
        if address["memo"]:
            panel["memo / tag"] = address["memo"] + "（不填这个，钱到不了你的账户）"
        if entry.get("note"):
            panel["交易所提示"] = entry["note"]
        panel["注意"] = [
            f"链只能是 {chosen}。同一个地址在别的链上收到的钱，交易所不认",
            f"币种只能是 {self.DEPOSIT_COIN}",
            "充值先进币安账户，还要在这里划转进预测钱包才能下单",
        ]
        return panel

    def deposit_status(self, txid: str) -> dict[str, Any]:
        """What the exchange says about one deposit. It is the only thing that can say it arrived."""
        reference = str(txid or "").strip()
        if not reference:
            return {"state": "invalid", "detail": "请填写充值交易号（txid）"}
        records = self.client.deposit_history(self.DEPOSIT_COIN, limit=100)
        found = next(
            (item for item in records if str(item.get("txId", "")).strip().lower() == reference.lower()),
            None,
        )
        if found is None:
            return {"state": "not_found",
                    "detail": "交易所最近的充值记录里没有这笔：可能还没被它看到（刚转出的先等等），"
                              "也可能交易号复制错了，或者这笔不是转到这个账户的"}
        # 0 pending, 6 credited but not withdrawable, 1 success, 7 wrong deposit, 8 waiting confirm.
        code = int(found.get("status", 0) or 0)
        amount = float(found.get("amount", 0) or 0)
        network = str(found.get("network", ""))
        confirmations = str(found.get("confirmTimes", "")).strip()
        common = {"amount": amount, "network": network, "confirmations": confirmations}
        if code in (0, 8):
            return {**common, "state": "confirming",
                    "detail": f"交易所看到了这笔 {amount:.6f} {self.DEPOSIT_COIN}（{network}），还在确认"
                              + (f"（{confirmations}）" if confirmations else "")}
        if code == 7:
            return {**common, "state": "wrong_target",
                    "detail": "交易所把这笔标成了异常充值，需要在币安站内处理"}
        if code in (1, 6):
            return {**common, "state": "arrived",
                    "detail": f"{amount:.6f} {self.DEPOSIT_COIN} 已入币安账户（{network}）"
                              + ("，暂不可提现" if code == 6 else "")}
        return {**common, "state": "confirming", "detail": f"交易所返回的状态码是 {code}"}

    def confirm_deposit(self, values: dict[str, Any] | None = None) -> dict[str, Any]:
        """Follow a deposit, and say what still has to happen before the robot can spend it."""
        txid = str((values or {}).get("txid", "")).strip()
        if not txid:
            return {"ok": False, "state": "invalid", "message": "请填写充值交易号（txid）"}
        try:
            status = self.deposit_status(txid)
        except Exception as error:
            return {"ok": False, "state": "error", "message": str(error)[:300]}
        state = str(status.get("state", ""))
        try:
            funds = self.account_funds()
            spot = funds.detail.get("fundable_from_spot")
            balance = f"；现货可划转 {spot}" if spot is not None else ""
        except Exception:
            balance = ""
        if state != "arrived":
            return {"ok": False, "state": state, "txid": txid, "pending": state == "confirming",
                    "message": str(status.get("detail", "")) + balance, **status}
        return {"ok": True, "state": "arrived", "txid": txid,
                "message": str(status.get("detail", "")) + balance
                           + "。这笔钱在币安账户里，机器人要用还需要一次划转："
                           "有待批的资金请求就在下面批准，没有的话等机器人下次开口。",
                **status}

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
        return {
            "pending_request": self.funding_requests.pending(),
            "available": funds.available,
            "currency": funds.currency,
            "source": funds.source,
            "detail": funds.detail,
            "funding_mode": "automatic_on_approval",
        }

    def topic_page_size(self) -> int:
        return self.settings.topic_page_size

    @staticmethod
    def _topic(item: dict[str, Any]) -> Topic:
        return Topic(
            topic_id=str(item.get("marketTopicId", "")),
            title=str(item.get("title", "")),
            question=str(item.get("question", "")),
            description=str(item.get("description", "")),
            category=str(item.get("l1Category", "")),
            status=str(item.get("status", "")),
            liquidity_usdt=float(item.get("liquidity") or 0),
            volume_usdt=float(item.get("tradeVolume") or 0),
            slug=str(item.get("slug", "")),
        )

    @staticmethod
    def _market(item: dict[str, Any]) -> Market:
        def displayed_probability(outcome: dict[str, Any]) -> float | None:
            value = outcome.get("chance")
            if value is None:
                value = outcome.get("price")
            return None if value is None else float(value)

        outcomes = tuple(
            Outcome(
                outcome_id=str(outcome.get("tokenId", "")),
                name=str(outcome.get("name", "")),
                displayed_probability=displayed_probability(outcome),
            )
            for outcome in item.get("outcomes") or []
        )
        return Market(
            market_id=str(item.get("marketId", "")),
            title=str(item.get("title", "")),
            question=str(item.get("question", "")),
            status=str(item.get("tradingStatus", item.get("status", ""))),
            liquidity_usdt=float(item.get("liquidity") or 0),
            volume_usdt=float(item.get("tradeVolume") or 0),
            outcomes=outcomes,
        )

    def list_topics(self, *, offset: int, limit: int) -> TopicPage:
        page = self.client.list_markets(offset=offset, limit=limit)
        if offset == 0:
            self._topics = []
        batch = page.get("marketTopics", [])
        if isinstance(batch, list):
            self._topics.extend(batch)
        topics = tuple(self._topic(item) for item in batch if isinstance(item, dict))
        return TopicPage(
            topics=topics,
            has_more=bool(page.get("hasMore")),
            next_offset=offset + len(topics),
        )

    def get_topic(self, topic_id: str) -> TopicDetail:
        detail = self.client.market_detail(int(topic_id))
        topic = self._topic(detail)
        return TopicDetail(
            topic=topic,
            start_time_ms=(int(detail["startDate"]) if detail.get("startDate") is not None else None),
            end_time_ms=(int(detail["endDate"]) if detail.get("endDate") is not None else None),
            fee_bps=int(detail.get("feeRateBps") or 0),
            markets=tuple(self._market(item) for item in detail.get("markets") or []),
            chart_type=str(detail.get("chartType", "")),
            reference_symbol=str(detail.get("symbol", "")),
            resolution=dict(detail.get("variantData") or {}),
        )

    def get_order_book(self, market_id: str, outcome_id: str) -> OrderBook:
        book = self.client.order_book(int(market_id), outcome_id)

        def levels(name: str) -> tuple[PriceLevel, ...]:
            return tuple(
                PriceLevel(price=float(item["price"]), quantity=float(item.get("quantity") or 0))
                for item in book.get(name) or []
            )

        return OrderBook(
            bids=levels("bids"),
            asks=levels("asks"),
            observed_at_ms=(int(book["timestamp"]) if book.get("timestamp") is not None else None),
        )

    def get_candles(
        self, reference_symbol: str, interval: str = "1m", limit: int = 120
    ) -> list[Candle]:
        rows = self.client.klines(reference_symbol, interval=interval, limit=limit)
        return [
            Candle(
                open_time_ms=int(row[0]),
                close_time_ms=int(row[6]),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
            )
            for row in rows
        ]

    def create_write_gateway(self, state: AccountState) -> ExecutionGateway:
        return ExecutionGateway(
            state,
            platform=self.name,
            write_transport=self._write_transport,
        )

    def write_transport(self) -> BinancePredictionWriteTransport:
        return self._write_transport

    def configuration_manifest(self) -> dict[str, Any]:
        return self.settings.manifest()

    def outcome_won(
        self, detail: TopicDetail, market: Market, outcome: Outcome
    ) -> bool | None:
        if detail.resolution.get("endPrice") is None or detail.resolution.get("startPrice") is None:
            return None
        winner = "UP" if float(detail.resolution["endPrice"]) > float(detail.resolution["startPrice"]) else "DOWN"
        direction_won = market.title.upper() == winner
        return direction_won if outcome.name.upper() == "YES" else not direction_won

    def close(self) -> None:
        close = getattr(self._write_transport, "close", None)
        if callable(close):
            close()

    def search_market_candidates(self, query: str, limit: int) -> list[MarketCandidate]:
        query_tokens = _tokens(query)
        ranked: list[tuple[float, dict[str, Any]]] = []
        for topic in self._topics:
            candidate = _tokens(f"{topic.get('title', '')} {topic.get('question', '')}")
            if not query_tokens or not candidate:
                continue
            score = len(query_tokens & candidate) / len(query_tokens | candidate)
            if score > 0:
                ranked.append((score, topic))
        results: list[MarketCandidate] = []
        for score, item in sorted(ranked, key=lambda value: value[0], reverse=True)[:limit]:
            topic = self._topic(item)
            detail: TopicDetail | None = None
            books: dict[str, OrderBook] = {}
            try:
                detail = self.get_topic(topic.topic_id)
                for market in detail.markets[:4]:
                    yes = next(
                        (outcome for outcome in market.outcomes if outcome.name.upper() == "YES"), None
                    )
                    if yes:
                        books[yes.outcome_id] = self.get_order_book(market.market_id, yes.outcome_id)
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
