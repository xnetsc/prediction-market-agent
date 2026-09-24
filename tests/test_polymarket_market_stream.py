from __future__ import annotations

import json
import queue
import unittest
from unittest.mock import patch

from prediction_market_agent.plugins.api._polymarket.market_stream import MarketBookStream
from prediction_market_agent.plugins.api._polymarket.read import PolymarketReadClient


class FakeSocket:
    def __init__(self, *, send_snapshot: bool):
        self.sent: list[str] = []
        self.messages: queue.Queue[str] = queue.Queue()
        self.send_snapshot = send_snapshot

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def send(self, message: str) -> None:
        self.sent.append(message)
        if not self.send_snapshot or message == "PING":
            return
        payload = json.loads(message)
        if payload.get("type") == "market" or payload.get("operation") == "subscribe":
            for token in payload["assets_ids"]:
                self.messages.put(json.dumps({
                    "event_type": "book", "asset_id": token, "timestamp": "1000",
                    "bids": [{"price": "0.40", "size": "10"}],
                    "asks": [{"price": "0.42", "size": "9"}],
                }))

    def recv(self, *, timeout: float):
        try:
            return self.messages.get(timeout=timeout)
        except queue.Empty as error:
            raise TimeoutError from error


class MarketBookStreamTests(unittest.TestCase):
    def test_subscription_returns_book_without_http_read_and_honors_proxy(self) -> None:
        socket = FakeSocket(send_snapshot=True)
        with patch(
            "prediction_market_agent.plugins.api._polymarket.market_stream.connect",
            return_value=socket,
        ) as connect:
            stream = MarketBookStream(
                proxy="http://localhost:8080", wait_seconds=1, max_age_seconds=15
            )
            try:
                book = stream.get_book("yes-token")
                self.assertEqual(book["bids"][0]["price"], "0.40")
                self.assertEqual(json.loads(socket.sent[0])["assets_ids"], ["yes-token"])
                self.assertEqual(connect.call_args.kwargs["proxy"], "http://localhost:8080")
                self.assertFalse(hasattr(PolymarketReadClient, "get_order_book"))
            finally:
                stream.close()

    def test_missing_snapshot_fails_closed(self) -> None:
        socket = FakeSocket(send_snapshot=False)
        with patch(
            "prediction_market_agent.plugins.api._polymarket.market_stream.connect",
            return_value=socket,
        ):
            stream = MarketBookStream(wait_seconds=0.05)
            try:
                with self.assertRaisesRegex(RuntimeError, "no fresh order-book snapshot"):
                    stream.get_book("yes-token")
            finally:
                stream.close()

    def test_incremental_change_updates_only_a_known_full_book(self) -> None:
        stream = MarketBookStream(wait_seconds=0.05)
        stream._connected = True
        stream._wanted.add("yes")
        stream._accept({
            "event_type": "price_change", "price_changes": [
                {"asset_id": "yes", "side": "BUY", "price": "0.41", "size": "4"}
            ],
        })
        self.assertNotIn("yes", stream._books)
        stream._accept({
            "event_type": "book", "asset_id": "yes", "timestamp": "1000",
            "bids": [{"price": "0.40", "size": "10"}], "asks": [],
        })
        stream._accept({
            "event_type": "price_change", "timestamp": "1001", "price_changes": [
                {"asset_id": "yes", "side": "BUY", "price": "0.41", "size": "4"}
            ],
        })
        self.assertEqual([float(item["price"]) for item in stream.get_book("yes")["bids"]],
                         [0.41, 0.40])
        stream.close()

    def test_subscription_set_is_bounded_and_old_books_are_dropped(self) -> None:
        socket = FakeSocket(send_snapshot=True)
        with patch(
            "prediction_market_agent.plugins.api._polymarket.market_stream.connect",
            return_value=socket,
        ):
            stream = MarketBookStream(wait_seconds=1, max_subscriptions=2)
            try:
                stream.get_book("one")
                stream.get_book("two")
                stream.get_book("three")
                self.assertEqual(stream._wanted, {"two", "three"})
                self.assertNotIn("one", stream._books)
            finally:
                stream.close()


if __name__ == "__main__":
    unittest.main()
