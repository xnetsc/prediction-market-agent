from ._support import *

from prediction_market_agent.runtime.memory import SessionMemory


class WhichKindOfRowTests(unittest.TestCase):
    """Three kinds answer three different questions; mixing them serves none of them."""

    def test_every_status_lands_in_exactly_one_group(self) -> None:
        for status, group in (
            ("STARTED", "running"),
            ("PROVIDER_ERROR", "failed"),
            ("EXECUTION_ERROR", "failed"),
            ("NO_ACTION", "concluded"),
            ("COMPLETED", "concluded"),
        ):
            with self.subTest(status=status):
                self.assertEqual(SessionMemory.decision_group(status), group)

    def test_a_rule_refusing_an_action_is_a_conclusion_not_a_fault(self) -> None:
        """Nothing broke: a filter plugin was asked and said no, which is an outcome."""
        self.assertEqual(SessionMemory.decision_group("RISK_REJECTED"), "concluded")

    def test_an_unknown_status_is_shown_rather_than_hidden(self) -> None:
        """A row nobody classified must still appear on the tab people actually read."""
        self.assertEqual(SessionMemory.decision_group("SOMETHING_NEW"), "concluded")

    def test_the_in_progress_status_is_the_one_deletion_protects(self) -> None:
        self.assertEqual(SessionMemory.decision_group(SessionMemory.IN_PROGRESS), "running")


class TheTabsAreServedNotGuessedTests(unittest.TestCase):
    """A count taken from the rows on screen only ever says how many are on screen."""

    def _dashboard(self) -> str:
        return Path("src/prediction_market_agent/runtime/dashboard.py").read_text()

    def _views(self) -> str:
        return Path("src/prediction_market_agent/runtime/static/dashboard-views.js").read_text()

    def test_the_group_filters_in_sql_not_after_paging(self) -> None:
        source = self._dashboard()
        body = source[source.index("def decisions("):]
        self.assertIn('if group == "running"', body)
        self.assertIn("filters.append", body)
        self.assertIn("SessionMemory.FAILED_STATUSES", body)

    def test_each_row_says_which_group_it_is_in(self) -> None:
        self.assertIn('item["group"] = SessionMemory.decision_group', self._dashboard())

    def test_the_default_tab_is_the_one_with_conclusions(self) -> None:
        self.assertIn("let LEDGER_TAB='concluded'", self._views())

    def test_the_counts_are_fetched_per_group(self) -> None:
        views = self._views()
        self.assertIn("refreshLedgerTabCounts", views)
        self.assertIn("'/api/decisions?limit=200&group='+group", views)

    def test_an_empty_tab_explains_itself_per_tab(self) -> None:
        """An empty default tab must not read as a dead robot."""
        views = self._views()
        self.assertIn("此刻没有正在分析的记录", views)
        self.assertIn("一次都没失败过", views)


class TheIntervalIsAFloorTests(unittest.TestCase):
    """How often a plugin will be asked is not how often there is anything worth looking at."""

    def _runtime_source(self, venue: str) -> str:
        return Path(
            f"src/prediction_market_agent/plugins/api/_{venue}/runtime.py"
        ).read_text()

    def test_both_plugins_ask_and_take_the_longer_of_the_two(self) -> None:
        for venue in ("polymarket", "binance"):
            with self.subTest(venue=venue):
                source = self._runtime_source(venue)
                self.assertIn('services.get("next_scan_delay")', source)
                self.assertIn("max(\n                                settings.scan_interval_seconds", source)

    def test_a_plugin_that_never_asks_keeps_its_own_interval(self) -> None:
        """The service is optional; nothing about the old behaviour changed for a plugin ignoring it."""
        for venue in ("polymarket", "binance"):
            with self.subTest(venue=venue):
                self.assertIn("if callable(pacing):", self._runtime_source(venue))

    def test_a_broken_request_falls_back_to_the_interval(self) -> None:
        for venue in ("polymarket", "binance"):
            with self.subTest(venue=venue):
                source = self._runtime_source(venue)
                block = source[source.index("if callable(pacing):"):]
                self.assertIn("except Exception:", block[:600])
                self.assertIn("delay = settings.scan_interval_seconds", block[:900])

    def test_the_framework_never_offers_less_than_the_floor(self) -> None:
        source = Path("src/prediction_market_agent/runtime/controller.py").read_text()
        block = source[source.index("def next_scan_delay("):]
        self.assertIn("max(int(minimum_seconds), requested)", block[:1600])
