"""Live Polymarket order books from the public market WebSocket."""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

from websockets.sync.client import connect


LOGGER = logging.getLogger(__name__)
DEFAULT_MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class MarketBookStream:
    """One connection for requested tokens; never serve a disconnected or stale book.

    A subscription produces a full ``book`` snapshot. Incremental price changes maintain that
    snapshot until a read asks for a new one. Reconnecting invalidates every cached book because
    heartbeats alone cannot prove that no market update was missed while the socket was down.
    """

    def __init__(
        self, url: str = DEFAULT_MARKET_WS_URL, *, proxy: str | None = None,
        wait_seconds: float = 8.0, max_age_seconds: float = 15.0,
        max_subscriptions: int = 512,
    ) -> None:
        self.url = url
        self.proxy = proxy
        self.wait_seconds = wait_seconds
        self.max_age_seconds = max_age_seconds
        self.max_subscriptions = max(1, int(max_subscriptions))
        self._condition = threading.Condition()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._wanted: set[str] = set()
        self._last_requested: dict[str, float] = {}
        self._subscribed: set[str] = set()
        self._refresh: set[str] = set()
        self._books: dict[str, tuple[dict[str, Any], float]] = {}
        self._connected = False
        self._last_error = ""

    def get_book(self, token_id: str) -> dict[str, Any]:
        token = str(token_id).strip()
        if not token:
            raise ValueError("a token ID is required for the market WebSocket")
        deadline = time.monotonic() + self.wait_seconds
        with self._condition:
            if self._stop.is_set():
                raise RuntimeError("market WebSocket is closed")
            self._wanted.add(token)
            self._last_requested[token] = time.monotonic()
            if len(self._wanted) > self.max_subscriptions:
                oldest = min(self._wanted - {token}, key=self._last_requested.__getitem__)
                self._wanted.remove(oldest)
                self._last_requested.pop(oldest, None)
                self._books.pop(oldest, None)
                self._refresh.discard(oldest)
            snapshot = self._books.get(token)
            if snapshot is not None and self._connected and time.time() - snapshot[1] <= self.max_age_seconds:
                return snapshot[0]
            self._refresh.add(token)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run, name="polymarket-market-books", daemon=True
                )
                self._thread.start()
            self._condition.notify_all()
            while not self._stop.is_set():
                snapshot = self._books.get(token)
                if snapshot is not None and self._connected and time.time() - snapshot[1] <= self.max_age_seconds:
                    return snapshot[0]
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
            reason = self._last_error or "no fresh order-book snapshot was received"
            raise RuntimeError(f"market WebSocket book unavailable for {token}: {reason}")

    def _run(self) -> None:
        delay = 1.0
        while not self._stop.is_set():
            with self._condition:
                while not self._wanted and not self._stop.is_set():
                    self._condition.wait(1.0)
                if self._stop.is_set():
                    return
                initial = sorted(self._wanted)
            try:
                with connect(
                    self.url, proxy=self.proxy, ping_interval=None,
                    open_timeout=8, close_timeout=2, max_size=8 * 1024 * 1024,
                ) as socket:
                    with self._condition:
                        self._connected = True
                        self._last_error = ""
                        self._books.clear()
                        self._subscribed = set(initial)
                        self._refresh.difference_update(initial)
                        self._condition.notify_all()
                    socket.send(json.dumps({
                        "type": "market", "assets_ids": initial,
                        "custom_feature_enabled": True,
                    }))
                    delay = 1.0
                    next_heartbeat = time.monotonic() + 10.0
                    last_pong = time.monotonic()
                    heartbeat_sent = False
                    while not self._stop.is_set():
                        with self._condition:
                            removed = sorted(self._subscribed - self._wanted)
                            added = sorted(self._wanted - self._subscribed)
                            refresh = sorted(self._refresh & self._wanted & self._subscribed)
                            self._subscribed.difference_update(removed)
                            self._subscribed.update(added)
                            self._refresh.difference_update(added)
                            self._refresh.difference_update(refresh)
                        if removed:
                            socket.send(json.dumps({
                                "assets_ids": removed, "operation": "unsubscribe",
                            }))
                        if added:
                            socket.send(json.dumps({
                                "assets_ids": added, "operation": "subscribe",
                                "custom_feature_enabled": True,
                            }))
                        if refresh:
                            socket.send(json.dumps({
                                "assets_ids": refresh, "operation": "unsubscribe",
                            }))
                            socket.send(json.dumps({
                                "assets_ids": refresh, "operation": "subscribe",
                                "custom_feature_enabled": True,
                            }))
                        if time.monotonic() >= next_heartbeat:
                            socket.send("PING")
                            heartbeat_sent = True
                            next_heartbeat = time.monotonic() + 10.0
                        if heartbeat_sent and time.monotonic() - last_pong > 35.0:
                            raise RuntimeError("market WebSocket heartbeat timed out")
                        try:
                            message = socket.recv(timeout=0.5)
                        except TimeoutError:
                            continue
                        if message == "PONG":
                            last_pong = time.monotonic()
                            continue
                        try:
                            payload = json.loads(message)
                        except (TypeError, ValueError):
                            continue
                        for event in payload if isinstance(payload, list) else [payload]:
                            self._accept(event)
            except Exception as error:
                LOGGER.warning("Polymarket market WebSocket disconnected: %s", error)
                with self._condition:
                    self._last_error = str(error)[:300]
            finally:
                with self._condition:
                    self._connected = False
                    self._books.clear()
                    self._subscribed.clear()
                    self._condition.notify_all()
            if self._stop.wait(delay):
                break
            delay = min(30.0, delay * 2.0)

    def _accept(self, event: Any) -> None:
        if not isinstance(event, dict):
            return
        kind = event.get("event_type") or event.get("type")
        if kind == "book":
            token = str(event.get("asset_id") or event.get("tokenId") or "")
            if token and isinstance(event.get("bids"), list) and isinstance(event.get("asks"), list):
                with self._condition:
                    if token in self._wanted:
                        self._books[token] = (event, time.time())
                        self._condition.notify_all()
        elif kind == "price_change":
            # A changed level can be applied to a full snapshot. Without that snapshot the update
            # is insufficient to construct depth and must not be treated as an order book.
            changes = event.get("price_changes") or event.get("priceChanges") or []
            with self._condition:
                for change in changes:
                    if not isinstance(change, dict):
                        continue
                    token = str(change.get("asset_id") or change.get("tokenId") or "")
                    current = self._books.get(token) if token in self._wanted else None
                    if current is None:
                        continue
                    side = str(change.get("side") or "").upper()
                    field = "bids" if side == "BUY" else "asks" if side == "SELL" else ""
                    if not field:
                        continue
                    try:
                        price = float(change["price"])
                        size = float(change["size"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    book = {**current[0]}
                    levels = [dict(level) for level in book[field] if float(level["price"]) != price]
                    if size > 0:
                        levels.append({"price": str(price), "size": str(size)})
                    book[field] = sorted(levels, key=lambda item: float(item["price"]), reverse=field == "bids")
                    book["timestamp"] = event.get("timestamp") or book.get("timestamp")
                    self._books[token] = (book, time.time())
                self._condition.notify_all()

    def close(self) -> None:
        self._stop.set()
        with self._condition:
            self._condition.notify_all()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=3)
