import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from prediction_market_agent.core.config import ApplicationConfigStore, Config
from prediction_market_agent.runtime.dashboard import create_app


class RequestsAreServedTogetherTests(unittest.TestCase):
    """A three-millisecond read took six hundred when it arrived beside the page's other requests.

    Handlers ran on the event loop, so the server could only do one thing at a time and every request
    waited for all the ones ahead of it. That queue - not the database, not the JSON - is what made
    the ledger slow, and only a test that actually sends requests together can see it.
    """

    SLOW = 0.6

    def test_two_slow_requests_finish_in_the_time_of_one(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = Config(
                working_directory=root, session_db=root / "sessions.sqlite3",
                auth_db=root / "auth.sqlite3", management_file=root / "selection.json",
                plugin_directories_file=root / "directories.json",
                application_config_file=root / "application.json",
            )
            app = create_app(config, start_robot=False)
            original = ApplicationConfigStore.manifest

            def slow_manifest(store):
                time.sleep(self.SLOW)
                return original(store)

            with TestClient(app, base_url="http://localhost", headers={"Host": "127.0.0.1"}) as client, \
                    patch.object(ApplicationConfigStore, "manifest", slow_manifest):
                statuses: list[int] = []

                def ask() -> None:
                    response = client.post("/api/local", json={"url": "/api/settings", "body": None})
                    statuses.append(response.status_code)

                threads = [threading.Thread(target=ask) for _ in range(3)]
                started = time.monotonic()
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
                elapsed = time.monotonic() - started

        self.assertEqual(statuses, [200, 200, 200])
        # Served one at a time, three of these take at least three times as long as one.
        self.assertLess(
            elapsed, self.SLOW * 2,
            f"three {self.SLOW}s requests took {elapsed:.2f}s - they were queued, not served together",
        )
