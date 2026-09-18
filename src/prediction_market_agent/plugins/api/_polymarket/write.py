from __future__ import annotations

import re
import threading
import time
import uuid
import weakref
from contextlib import contextmanager
from dataclasses import replace
from typing import Any

import httpx
from eth_account import Account
from polymarket import BuilderApiKey, PRODUCTION, RelayerApiKey, SecureClient
from polymarket._internal.actions import auth as _auth_actions
from polymarket._internal.l1_auth import sign_api_key_auth
from polymarket.clients._transport import SyncTransport
from polymarket.models import ApiKeyCreds

from .config import PolymarketPluginConfig


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True)
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value


class _QuoteSpec:
    def __init__(
        self,
        token_id: str,
        side: str,
        price: float,
        quantity: float,
        order_type: str,
        expires_at: int,
    ) -> None:
        self.token_id = token_id
        self.side = side
        self.price = price
        self.quantity = quantity
        self.order_type = order_type
        self.expires_at = expires_at


class PolymarketWriteTransport:
    """Official unified client adapter for orders, cancellation, redemption, and pUSD transfer."""

    def __init__(self, settings: PolymarketPluginConfig):
        self.settings = settings
        self._quotes: dict[str, _QuoteSpec] = {}
        self._client: SecureClient | None = None
        # Which httpx clients are ours, so a second pass does not close and rebuild a good one.
        self._policy_clients: weakref.WeakSet[httpx.Client] = weakref.WeakSet()
        self._setup_client: SecureClient | None = None
        self._setup_client_at = 0.0

    def _api_key(self) -> BuilderApiKey | RelayerApiKey | None:
        if (
            self.settings.builder_api_key
            and self.settings.builder_api_secret
            and self.settings.builder_api_passphrase
        ):
            return BuilderApiKey(
                key=self.settings.builder_api_key,
                secret=self.settings.builder_api_secret,
                passphrase=self.settings.builder_api_passphrase,
            )
        if self.settings.relayer_api_key and self.settings.relayer_api_key_address:
            return RelayerApiKey(
                key=self.settings.relayer_api_key,
                address=self.settings.relayer_api_key_address,
            )
        return None

    def derive_credentials(self, environment: Any) -> ApiKeyCreds:
        """Get the CLOB credentials the signing key is entitled to, without leaving the plugin's network.

        The SDK will do this itself when none are supplied, but over a transport it builds during
        construction - before this plugin can install its proxy settings on anything. Doing it here
        means the same derivation runs under the same policy as every other call, so an operator who
        left these blank is not silently making one request by a route they did not configure.
        """
        transport = SyncTransport(
            base_url=environment.clob_url, client=self._http_client(environment.clob_url)
        )
        try:
            signature = sign_api_key_auth(
                Account.from_key(self.settings.private_key),
                chain_id=environment.chain_id,
                timestamp=int(time.time()),
                nonce=0,
            )
            return _auth_actions.create_or_derive_api_key_sync(transport, signature)
        finally:
            transport.close()

    def _http_client(self, base_url: str) -> httpx.Client:
        client = httpx.Client(
            base_url=base_url,
            proxy=self.settings.http_proxy or None,
            trust_env=False,
            http2=True,
            timeout=20,
            headers={"User-Agent": "prediction-market-agent/0.6"},
        )
        self._policy_clients.add(client)
        return client

    _patch_lock = threading.Lock()

    @contextmanager
    def _sdk_uses_our_network(self):
        """Make the SDK build its own transports our way, for as long as this block runs.

        Replacing transports after a client exists is too late. Construction itself goes to the
        network - it asks the relayer whether this wallet is deployed - and it does that through a
        client the SDK made with its own defaults: five seconds to connect, and no proxy. On a host
        where the TLS handshake to Polymarket takes six, that call can only ever time out, and the
        wallet panel showed the operator a raw `_ssl.c:1015` with nothing they could do about it.

        So the policy is installed one level earlier, at the point any transport is made. The patch
        is on the SDK class and therefore process-wide, which is why it is held only for the call
        that needs it and behind a lock: two plugins may have different proxies, and neither should
        get the other's.
        """
        original = SyncTransport.__init__

        def patched(inner_self, *, base_url: str, client: httpx.Client | None = None, **rest: Any):
            original(inner_self, base_url=base_url, client=client or self._http_client(base_url), **rest)

        with self._patch_lock:
            SyncTransport.__init__ = patched
            try:
                yield
            finally:
                SyncTransport.__init__ = original

    def _install_network_policy(self, client: SecureClient) -> None:
        """Catch anything built outside that window - lazily, or by a path not patched."""
        ctx = client._ctx  # polymarket-client 0.3.x: pinned internal transport integration point
        transports = (
            ctx.gamma,
            ctx.data,
            ctx.rfq,
            ctx.clob,
            ctx.secure_clob,
            ctx.relayer,
            ctx.combos,
            ctx.rpc._transport,
        )
        for transport in transports:
            old = transport._client
            if old in self._policy_clients:
                continue
            base_url = str(old.base_url)
            old.close()
            transport._client = self._http_client(base_url)
            transport._owns_client = True

    SETUP_CLIENT_SECONDS = 90.0
    """How long a setup connection is reused before it is made again.

    Connecting is not cheap: it is several round trips, and on a slow path each handshake alone can
    take six seconds. One page of this plugin's panels asks three separate questions - the wallet,
    the deposit address, the balance - and connecting once per question took nearly two minutes, by
    which point the operator has decided the page is broken and gone looking for the button
    somewhere else. Long enough to serve one page from one connection, short enough that a key or
    an address changed in the settings takes effect while the operator is still looking at it.
    """

    def _require_client(self, *, for_trading: bool = True) -> SecureClient:
        """Connect. With `for_trading` false the wallet is not deployed on the way in.

        Everything that trades needs a wallet that exists on chain; everything that sets the
        account up runs before one does. The two are cached separately and the setup one expires,
        so it never quietly stands in for the real one.
        """
        if for_trading and self._client is not None:
            return self._client
        if not for_trading and self._setup_client is not None:
            if time.time() - self._setup_client_at < self.SETUP_CLIENT_SECONDS:
                return self._setup_client
            self._setup_client.close()
            self._setup_client = None
        environment = replace(
            PRODUCTION,
            name="configured",
            chain_id=self.settings.chain_id,
            gamma_url=self.settings.gamma_url,
            clob_url=self.settings.clob_url,
            data_url=self.settings.data_url,
            relayer_url=self.settings.relayer_url,
            rpc_url=self.settings.rpc_url,
        )
        # Supplied only when the operator supplied all three. The SDK derives its own from the
        # signing key when none are given, so demanding them was asking for something it already
        # knew; a partial set is neither, and would fail as an authentication error rather than as
        # the configuration mistake it is.
        parts = (self.settings.api_key, self.settings.api_secret, self.settings.api_passphrase)
        given = [part for part in parts if str(part).strip()]
        if given and len(given) != 3:
            raise ValueError(
                "CLOB API Key、Secret、Passphrase 要么三项都填，要么三项都留空由私钥派生；"
                "现在只填了其中一部分。"
            )
        credentials = (
            ApiKeyCreds(
                key=self.settings.api_key,
                secret=self.settings.api_secret,
                passphrase=self.settings.api_passphrase,
            )
            if given
            else self.derive_credentials(environment)
        )
        # Public create() performs requests during construction before an application can
        # inject transport policy. Version 0.3.x exposes this constructor path; credentials
        # are supplied, validation is deferred, and all transports are replaced before use.
        with self._sdk_uses_our_network():
            client = SecureClient._create(
                private_key=self.settings.private_key,
                wallet=self.settings.funder_address.strip() or None,
                environment=environment,
                credentials=credentials,
                api_key=self._api_key(),
                validate_credentials=False,
            )
        try:
            self._install_network_policy(client)
            if for_trading:
                # Deploying the deposit wallet is a gasless relayer transaction, so it needs a
                # builder or relayer key. Setup runs before there is one - that is what setup is
                # for - and doing this unconditionally made the account impossible to finish: the
                # button that creates the key could not run without the key it was there to create.
                client = client._ensure_wallet_ready()
            else:
                self._setup_client, self._setup_client_at = client, time.time()
                return client
            self._client = client
        except BaseException:
            client.close()
            raise
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
        if self._setup_client is not None:
            self._setup_client.close()
            self._setup_client = None

    def get_quote(
        self,
        *,
        outcome_id: str,
        side: str,
        amount: str,
        order_type: str,
        price_limit: str | None,
        reference_price: float,
        fee_bps: int,
    ) -> dict[str, Any]:
        del fee_bps
        price = float(price_limit or reference_price)
        if not 0 < price < 1:
            raise ValueError("Polymarket quote requires an executable price in (0, 1)")
        normalized_amount = float(amount)
        quantity = normalized_amount / price if side == "BUY" else normalized_amount
        quote_id = f"poly_quote_{uuid.uuid4().hex}"
        expires_at = int(time.time() * 1000) + 30_000
        self._quotes[quote_id] = _QuoteSpec(
            outcome_id, side, price, quantity, order_type, expires_at
        )
        return {"quoteId": quote_id, "averagePrice": price, "expireAt": expires_at}

    def place_order(
        self, *, quote_id: str, order_type: str, price_limit: str | None
    ) -> dict[str, Any]:
        spec = self._quotes.pop(quote_id, None)
        if spec is None or spec.expires_at < int(time.time() * 1000):
            raise RuntimeError("Polymarket local quote is missing or expired")
        client = self._require_client()
        price = float(price_limit or spec.price)
        builder_code = self.settings.builder_code or None
        if order_type == "LIMIT":
            response = client.place_limit_order(
                token_id=spec.token_id,
                price=price,
                size=spec.quantity,
                side=spec.side,
                builder_code=builder_code,
            )
        elif spec.side == "BUY":
            response = client.place_market_order(
                token_id=spec.token_id,
                side="BUY",
                amount=spec.quantity * price,
                max_price=price,
                order_type="FOK",
                builder_code=builder_code,
            )
        else:
            response = client.place_market_order(
                token_id=spec.token_id,
                side="SELL",
                shares=spec.quantity,
                min_price=price,
                order_type="FOK",
                builder_code=builder_code,
            )
        raw = _jsonable(response)
        if not getattr(response, "ok", False):
            raise RuntimeError(
                f"Polymarket order rejected: {getattr(response, 'code', 'unknown')}: "
                f"{getattr(response, 'message', '')}"
            )
        platform_status = str(response.status)
        status = {
            "live": "OPEN",
            "delayed": "OPEN",
            "matched": "FILLED",
        }[platform_status.casefold()]
        return {
            **raw,
            "orderId": str(response.order_id),
            "platformStatus": platform_status,
            "status": status,
        }

    CHAIN_NAMES = {1: "Ethereum", 137: "Polygon", 80002: "Polygon Amoy 测试网"}
    TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
    DEPOSIT_CONFIRMATIONS = 12
    """Blocks before a deposit is called arrived. About half a minute on Polygon."""

    def deposit_instructions(self) -> dict[str, Any]:
        """Everything a deposit needs to land, stated rather than guessed.

        A deposit goes wrong in three ways that look identical afterwards - the wrong chain, the
        wrong token on the right chain, the right token to the wrong address - and all three are
        unrecoverable. So each is named, and the token is read off the chain rather than assumed.
        """
        client = self._require_client()
        token = str(client.environment.collateral_token)
        symbol, decimals = self._token_identity(token)
        chain_id = int(self.settings.chain_id)
        chain = self.CHAIN_NAMES.get(chain_id, f"chain {chain_id}")
        return {
            "chain": chain,
            "chain_id": chain_id,
            "address": str(client.wallet),
            "deposit_currency": "USDC",
            "account_token_symbol": symbol,
            "account_token_contract": token,
            "token_decimals": decimals,
            "minimum_confirmations": self.DEPOSIT_CONFIRMATIONS,
            "warnings": [
                f"链只能是 {chain}（chain id {chain_id}）。转到别的链上的同名地址，钱找不回来",
                f"收款地址是 {client.wallet}，这是平台给这个账户的交易地址，不是你的签名地址",
                f"转 USDC。平台收到后记在账户里叫 {symbol}（{token}），这是平台的记账币，不用你去买",
                "USDC 在 Polygon 上有不止一个合约。哪个能被这个平台入账，以 Polymarket 官网充值页当时显示的为准 - "
                "这里不替你猜。不确定就先转一小笔，把交易号填进来，这里会告诉你平台有没有收到",
            ],
        }

    def _token_identity(self, token: str) -> tuple[str, int]:
        """What the collateral token calls itself, asked of the token."""
        symbol, decimals = "USDC", 6
        try:
            raw = self._rpc("eth_call", [{"to": token, "data": "0x95d89b41"}, "latest"])
            decoded = self._decode_string(str(raw or ""))
            symbol = decoded or symbol
        except Exception:
            pass
        try:
            raw = self._rpc("eth_call", [{"to": token, "data": "0x313ce567"}, "latest"])
            decimals = int(str(raw or "0x6"), 16) or decimals
        except Exception:
            pass
        return symbol, decimals

    @staticmethod
    def _decode_string(value: str) -> str:
        body = value[2:] if value.startswith("0x") else value
        if len(body) < 128:
            return ""
        length = int(body[64:128], 16)
        return bytes.fromhex(body[128:128 + length * 2]).decode("utf-8", errors="ignore").strip()

    def _rpc(self, method: str, params: list[Any]) -> Any:
        url = str(self.settings.rpc_url).strip()
        if not url:
            raise RuntimeError("这个插件没有配置链上 RPC 地址，查不了转账")
        client = self._http_client(url)
        try:
            response = client.post(
                url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
            )
            response.raise_for_status()
            payload = response.json()
        finally:
            client.close()
        if isinstance(payload, dict) and payload.get("error"):
            raise RuntimeError(f"RPC {method} 出错：{str(payload['error'])[:200]}")
        return payload.get("result") if isinstance(payload, dict) else None

    def deposit_status(self, txid: str) -> dict[str, Any]:
        """Where one deposit transaction has got to, read from the chain itself.

        Four answers matter to whoever sent it: this is not a transaction we can find, it is on its
        way, it failed, or the money is here. Anything vaguer leaves them refreshing a balance and
        wondering whether they sent it to the wrong place.
        """
        reference = str(txid or "").strip()
        if not re.fullmatch(r"0x[0-9a-fA-F]{64}", reference):
            return {"state": "invalid",
                    "detail": "交易号应该是 0x 开头、后面 64 位十六进制的那串"}
        receipt = self._rpc("eth_getTransactionReceipt", [reference])
        if not receipt:
            if self._rpc("eth_getTransactionByHash", [reference]):
                return {"state": "confirming", "confirmations": 0,
                        "required": self.DEPOSIT_CONFIRMATIONS,
                        "detail": "交易已经广播，还没有被打包"}
            return {"state": "not_found",
                    "detail": "这条链上查不到这笔交易：确认交易号没有复制错，以及它确实是这条链上的"}
        if int(str(receipt.get("status", "0x0")), 16) == 0:
            return {"state": "failed", "detail": "这笔交易在链上失败了，钱没有转出去"}
        client = self._require_client()
        wallet = str(client.wallet).lower()
        head = int(str(self._rpc("eth_blockNumber", []) or "0x0"), 16)
        mined = int(str(receipt.get("blockNumber", "0x0")), 16)
        confirmations = max(0, head - mined + 1) if head and mined else 0
        # Whatever token actually landed, named. The operator may have sent a different USDC than
        # the one this platform credits, and "nothing arrived" would be the wrong thing to tell
        # them when the money is demonstrably at the address.
        arrived: list[dict[str, Any]] = []
        for log in receipt.get("logs") or []:
            topics = [str(item).lower() for item in (log.get("topics") or [])]
            if len(topics) < 3 or topics[0] != self.TRANSFER_TOPIC:
                continue
            if not topics[2].endswith(wallet[2:]):
                continue
            contract = str(log.get("address", ""))
            symbol, decimals = self._token_identity(contract)
            arrived.append({
                "token_symbol": symbol,
                "token_contract": contract,
                "amount": int(str(log.get("data", "0x0")), 16) / 10**decimals,
            })
        if not arrived:
            return {"state": "wrong_target", "confirmations": confirmations,
                    "detail": f"这笔交易成功了，但没有任何代币转到 {client.wallet}："
                              "可能是地址填错了，或者这是另一笔交易"}
        summary = "、".join(f"{item['amount']:.6f} {item['token_symbol']}" for item in arrived)
        if confirmations < self.DEPOSIT_CONFIRMATIONS:
            return {"state": "confirming", "arrived": arrived, "confirmations": confirmations,
                    "required": self.DEPOSIT_CONFIRMATIONS,
                    "detail": f"链上已经收到 {summary}，等待确认（{confirmations}/{self.DEPOSIT_CONFIRMATIONS}）"}
        # The chain says it arrived; whether the platform counts it as spendable is the platform's
        # answer, and the balance is where that shows.
        return {"state": "arrived", "arrived": arrived, "confirmations": confirmations,
                "amount": sum(float(item["amount"]) for item in arrived),
                "detail": f"链上已确认收到 {summary}"}

    def deposit_target(self) -> dict[str, Any]:
        """Where collateral has to arrive, and whether this client could send it there itself.

        Under a proxy wallet the trading balance lives at an address the signer does not hold, so
        funding is an ERC-20 transfer the operator makes from their own EOA. Naming the address
        turns "fund it yourself" into something actionable.
        """
        client = self._require_client()
        return {
            "wallet_address": str(client.wallet),
            "signer_address": str(client.signer),
            "wallet_type": str(client.wallet_type),
            "collateral_token": str(client.environment.collateral_token),
            "self_funding_possible": str(client.wallet).lower() == str(client.signer).lower(),
        }

    def wallet_facts(self) -> dict[str, Any]:
        """What the signing key already implies, so none of it has to be asked for.

        The address, its wallet type and the credentials in force are all consequences of the key;
        reporting them is how an operator checks the plugin reached the account they meant rather
        than taking it on trust.
        """
        client = self._require_client(for_trading=False)
        credentials = client.credentials
        supplied = bool(str(self.settings.api_key).strip())
        return {
            "说明": "以下都是这个插件已经拿到的值。配置表单里对应的输入框是空的，那不是缺失——留空就是让插件自己算，算出来的就是这里显示的。",
            "Funder 地址（即钱包地址）": str(client.wallet),
            "钱包类型": str(client.wallet_type),
            "CLOB API Key": str(getattr(credentials, "key", "")),
            "CLOB API Secret": self._masked(getattr(credentials, "secret", "")),
            "CLOB Passphrase": self._masked(getattr(credentials, "passphrase", "")),
            "这些凭据从哪来": "你在配置里填的" if supplied else "由钱包私钥派生，每次连线都算得出同样的一套",
            "免 gas 交易已就绪": client.is_gasless_ready(),
        }

    @staticmethod
    def _masked(value: Any) -> str:
        """Enough to recognise it by, not enough to use. The backup panel hands over the whole thing."""
        text = str(value or "")
        return f"{text[:6]}…{text[-4:]}（共 {len(text)} 位，完整值见「备份」）" if len(text) > 12 else ("（无）" if not text else "已设置")

    def exportable_credentials(self) -> dict[str, str]:
        """Everything derived, in full, for an operator who wants it outside this machine."""
        credentials = self._require_client(for_trading=False).credentials
        return {
            "CLOB API Key": str(getattr(credentials, "key", "")),
            "CLOB API Secret": str(getattr(credentials, "secret", "")),
            "CLOB Passphrase": str(getattr(credentials, "passphrase", "")),
        }

    def builder_api_keys(self) -> list[dict[str, Any]]:
        """Builder keys this account already has, for choosing between rather than re-creating."""
        client = self._require_client(for_trading=False)
        return [_jsonable(item) for item in client.fetch_builder_api_keys()]

    def create_builder_api_key(self) -> dict[str, Any]:
        created = self._require_client(for_trading=False).create_builder_api_key()
        return {
            "key": str(created.key),
            "secret": str(created.secret),
            "passphrase": str(created.passphrase),
        }

    def setup_trading_approvals(self) -> dict[str, Any]:
        """Grant the allowances a fresh wallet needs before it can trade.

        On-chain and irreversible in the sense that it spends gas, which is why nothing here does
        it on its own: it happens when an operator asks for it and not as a side effect of
        connecting.
        """
        handle = self._require_client().setup_trading_approvals()
        return _jsonable(handle)

    def collateral_balance(self) -> float:
        """Spendable pUSD as the platform reports it, in whole units rather than base units."""
        answer = self._require_client().get_balance_allowance(asset_type="COLLATERAL")
        return float(answer.balance) / 10**6

    def cancel_orders(self, order_ids: list[str]) -> dict[str, Any]:
        response = self._require_client().cancel_orders(order_ids=order_ids)
        raw = _jsonable(response)
        return {
            **raw,
            "canceled": [str(item) for item in response.canceled],
            "failed": [
                {"orderId": str(order_id), "reason": reason}
                for order_id, reason in response.not_canceled.items()
            ],
        }

    def redeem(self, outcome_ids: list[str]) -> dict[str, Any]:
        client = self._require_client()
        requested = set(outcome_ids)
        condition_ids: list[str] = []
        for position in client.list_positions(redeemable=True, page_size=100).iter_items():
            if str(position.token_id) in requested and str(position.condition_id) not in condition_ids:
                condition_ids.append(str(position.condition_id))
        results = []
        for condition_id in condition_ids:
            handle = client.redeem_positions(condition_id=condition_id)
            results.append(
                {"conditionId": condition_id, "transaction": _jsonable(handle.wait())}
            )
        return {
            "requestedTokenIds": list(outcome_ids),
            "redeemedConditionIds": condition_ids,
            "transactions": results,
        }

    def transfer(self, direction: str, amount: str) -> dict[str, Any]:
        if direction.upper() != "OUTBOUND":
            raise ValueError("Polymarket supports only OUTBOUND pUSD transfers")
        if not self.settings.transfer_recipient:
            raise RuntimeError("POLYMARKET_TRANSFER_RECIPIENT is required for transfer")
        amount_usdt = float(amount)
        amount_base_units = int(round(amount_usdt * 10**6))
        if amount_base_units <= 0:
            raise ValueError("Polymarket transfer amount is below one pUSD base unit")
        client = self._require_client()
        handle = client.transfer_erc20(
            token_address=client.environment.collateral_token,
            recipient_address=self.settings.transfer_recipient,
            amount=amount_base_units,
            metadata=f"Prediction agent transfer {amount_usdt:.6f} pUSD",
        )
        outcome = handle.wait()
        return {
            "status": "COMPLETED",
            "direction": "OUTBOUND",
            "amountPusd": amount_usdt,
            "amountBaseUnits": amount_base_units,
            "recipient": self.settings.transfer_recipient,
            "transaction": _jsonable(outcome),
        }
