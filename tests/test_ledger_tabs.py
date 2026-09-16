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

    def test_the_counts_are_counted_not_measured_by_fetching_rows(self) -> None:
        """Six hundred rows over three round trips to display three numbers is why this was slow."""
        views = self._views()
        self.assertIn("'/api/decisions/counts?'", views)
        self.assertNotIn("limit=200&group=", views)
        body = self._dashboard()
        counts = body[body.index("def decision_counts("):body.index("def forget_decisions(")]
        self.assertIn("SELECT COUNT(*) FROM decision_ledger", counts)

    def test_the_view_says_it_is_loading_rather_than_looking_empty(self) -> None:
        """Silence on a slow page reads as "there is nothing here", which is the wrong answer."""
        views = self._views()
        self.assertIn("function setLedgerLoading", views)
        self.assertIn("正在读取决策记录", views)
        shell = self._dashboard()
        self.assertIn("setLedgerLoading(true)", shell)
        self.assertIn("finally{setLedgerLoading(false)}", shell)

    def test_a_failed_load_says_so_instead_of_showing_nothing(self) -> None:
        self.assertIn("没能读到决策记录", self._dashboard())

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


class ReadingIsNotInterruptedTests(unittest.TestCase):
    """Rebuilding history on a timer only ever cost the reader their place."""

    def _views(self) -> str:
        return Path("src/prediction_market_agent/runtime/static/dashboard-views.js").read_text()

    def _shell(self) -> str:
        return Path("src/prediction_market_agent/runtime/dashboard.py").read_text()

    def test_the_ledger_is_not_on_the_timer(self) -> None:
        shell = self._shell()
        timer = shell[shell.index("setInterval(()=>Promise.all("):]
        self.assertNotIn("refreshAudit", timer[:200], "history does not change once written")

    def test_there_is_a_refresh_control_and_it_says_when_it_last_read(self) -> None:
        shell = self._shell()
        self.assertIn('onclick="refreshAudit()"', shell)
        self.assertIn("ledgerStamp", shell)
        self.assertIn("读取于 ", shell)

    def test_the_page_says_it_does_not_refresh_itself(self) -> None:
        """Otherwise a view that never updates is indistinguishable from one that is stuck."""
        self.assertIn("这里不自动刷新", self._shell())

    def test_an_open_json_block_survives_a_manual_refresh(self) -> None:
        views = self._views()
        self.assertIn("OPEN_DETAILS", views)
        self.assertIn("rememberOpenDetails()", views)
        self.assertIn("detail(raw,r.id+':'+index)", views)


class PagedLedgerTests(unittest.TestCase):
    """A hundred rows of context and raw model output before anything can be drawn is why it was slow."""

    def _views(self) -> str:
        return Path("src/prediction_market_agent/runtime/static/dashboard-views.js").read_text()

    def _shell(self) -> str:
        return Path("src/prediction_market_agent/runtime/dashboard.py").read_text()

    def test_the_first_page_is_small(self) -> None:
        views = self._views()
        self.assertIn("const LEDGER_FIRST_PAGE=10", views)
        self.assertIn("const LEDGER_PAGE=5", views)
        self.assertIn("'/api/decisions?limit='+LEDGER_FIRST_PAGE+'&offset=0'", self._shell())

    def test_more_rows_are_appended_not_redrawn(self) -> None:
        """Redrawing would close whatever the reader has open - the thing this view stopped doing."""
        views = self._views()
        self.assertIn("function appendDecisionEntries", views)
        self.assertIn("insertAdjacentHTML('beforeend'", views)

    def test_scrolling_to_the_end_asks_for_the_next_page(self) -> None:
        views = self._views()
        self.assertIn("IntersectionObserver", views)
        self.assertIn("ledgerSentinel", views)
        self.assertIn("loadMoreDecisions()", views)

    def test_it_stops_asking_once_the_ledger_runs_out(self) -> None:
        """Otherwise scrolling at the bottom would fetch an empty page forever."""
        views = self._views()
        self.assertIn("LEDGER_EXHAUSTED", views)
        self.assertIn("没有更多记录了", views)

    def test_two_scrolls_at_once_do_not_load_the_same_page_twice(self) -> None:
        self.assertIn("if(LEDGER_FETCHING||LEDGER_EXHAUSTED", self._views())

    def test_the_diagnostic_tables_are_not_a_hundred_rows_each(self) -> None:
        """Three more hundred-row fetches on the same page, behind a collapsed panel nobody opened."""
        shell = self._shell()
        self.assertNotIn("kind=actions&limit=100", shell)
        self.assertIn("kind=actions&limit=20", shell)


class LoadingIsShownWhereItLandsTests(unittest.TestCase):
    """A spinner elsewhere says something is happening but not where; the gap is where they look."""

    def _views(self) -> str:
        return Path("src/prediction_market_agent/runtime/static/dashboard-views.js").read_text()

    def _css(self) -> str:
        return Path("src/prediction_market_agent/runtime/static/dashboard.css").read_text()

    def test_placeholders_are_shaped_like_the_rows_they_replace(self) -> None:
        views = self._views()
        self.assertIn("function skeletonRowsHtml", views)
        for part in ("skeleton-bar meta", "skeleton-bar title", "skeleton-bar reason"):
            self.assertIn(part, views)

    def test_they_go_where_the_rows_will_appear(self) -> None:
        views = self._views()
        self.assertIn("more.insertAdjacentHTML('beforebegin'", views)
        loader = views[views.index("async function loadMoreDecisions"):]
        loader = loader[: loader.index("\n}")]
        self.assertIn("showLedgerSkeleton(LEDGER_PAGE)", loader, "written but never shown")

    def test_the_first_page_shows_them_too(self) -> None:
        """The wait before anything at all appears is the one that reads as "no data"."""
        views = self._views()
        busy = views[views.index("function setLedgerLoading"):]
        self.assertIn("skeletonRowsHtml(LEDGER_FIRST_PAGE)", busy[:900])

    def test_they_are_cleared_however_the_fetch_ends(self) -> None:
        self.assertIn("finally{clearLedgerSkeleton();LEDGER_FETCHING=false}", self._views())

    def test_the_motion_is_real_but_optional(self) -> None:
        css = self._css()
        self.assertIn("@keyframes skeleton-sweep", css)
        self.assertIn("prefers-reduced-motion", css)


class OnlyDownwardsLoadsMoreTests(unittest.TestCase):
    """The sentinel keeps intersecting while the reader scrolls back up through what just loaded."""

    def _views(self) -> str:
        return Path("src/prediction_market_agent/runtime/static/dashboard-views.js").read_text()

    def test_direction_is_tracked(self) -> None:
        views = self._views()
        self.assertIn("LEDGER_SCROLL_DOWN", views)
        self.assertIn("LEDGER_SCROLL_DOWN=position>LEDGER_LAST_SCROLL", views)

    def test_the_observer_only_fires_going_down(self) -> None:
        self.assertIn("entry.isIntersecting)&&LEDGER_SCROLL_DOWN", self._views())

    def test_scrolling_up_stops_the_animation_in_progress(self) -> None:
        self.assertIn("if(!LEDGER_SCROLL_DOWN)clearLedgerSkeleton()", self._views())

    def test_the_fetch_itself_also_declines_when_going_up(self) -> None:
        """Belt and braces: the observer is not the only caller."""
        self.assertIn("LEDGER_EXHAUSTED||!LEDGER_SCROLL_DOWN", self._views())
