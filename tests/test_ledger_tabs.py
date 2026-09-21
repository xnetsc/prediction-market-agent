from ._support import *
from types import SimpleNamespace
import json

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
        """Reading a tab and deleting one build the same clauses, so they cannot disagree."""
        source = self._dashboard()
        body = source[source.index("def _ledger_filters("):source.index("READABLE_SCHEMA")]
        self.assertIn('if group == "running"', body)
        self.assertIn("filters.append", body)
        self.assertIn("SessionMemory.FAILED_STATUSES", body)
        for user in ("def decisions(", "def forget_matching("):
            with self.subTest(caller=user):
                after = source[source.index(user):]
                self.assertIn("_ledger_filters(", after[:after.index("\n    def ", 10)])

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
        self.assertNotIn("setInterval(", shell)
        self.assertNotIn("POLL_IN_FLIGHT", shell)

    def test_each_page_refreshes_once_when_entered_instead_of_polling(self) -> None:
        shell = self._shell()
        navigation = Path(
            "src/prediction_market_agent/runtime/static/dashboard-shell.js"
        ).read_text()
        self.assertIn("refreshEnteredDashboardView", shell)
        self.assertIn("name!==currentView", navigation)
        self.assertIn("refreshEnteredDashboardView(name)", navigation)
        self.assertNotIn("setInterval(", navigation)

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
        self.assertIn("r.id+':raw'", views, "the raw block needs a key that survives a redraw")


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

    def test_the_diagnostic_tables_load_only_when_opened(self) -> None:
        """Two and a half megabytes, on every visit, for panels labelled "open when debugging"."""
        shell = self._shell()
        refresh = shell[shell.index("async function refreshAudit("):]
        refresh = refresh[: refresh.index("\n")]
        self.assertNotIn("kind=turns", refresh)
        self.assertNotIn("kind=steps", refresh)
        views = self._views()
        self.assertIn("function loadDiagnosticPanel", views)
        self.assertIn("panel.querySelector('#'+id))loadDiagnosticPanel(id)", views)


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
        """Belt and braces: the observer is not the only caller. A filter change is the exception."""
        views = self._views()
        self.assertIn("(!forced&&!LEDGER_SCROLL_DOWN)", views)
        self.assertIn("loadMoreDecisions({force:true})", views)


class TheRowMarkupActuallyRunsTests(unittest.TestCase):
    """Searching the source for strings passed while the page showed "reason is not defined"."""

    def test_rendering_real_rows_in_a_javascript_engine(self) -> None:
        import shutil
        import subprocess

        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed; this check needs a JavaScript engine")
        completed = subprocess.run(
            [node, "tests/ledger_render_check.js",
             "src/prediction_market_agent/runtime/static/dashboard-views.js"],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(
            completed.returncode, 0, completed.stderr or completed.stdout
        )
        self.assertIn("OK", completed.stdout)


class ListRowsAreLightTests(unittest.TestCase):
    """The database answered in a fifth of a second; the page was slow because of what it shipped."""

    def _shell(self) -> str:
        return Path("src/prediction_market_agent/runtime/dashboard.py").read_text()

    def _views(self) -> str:
        return Path("src/prediction_market_agent/runtime/static/dashboard-views.js").read_text()

    def test_a_list_row_drops_what_only_an_opened_row_draws(self) -> None:
        from prediction_market_agent.runtime.dashboard import _slim_decision

        heavy = {
            "id": 1,
            "context": {"market": {"title": "BTC"}, "order_book": {"bids": ["x"] * 5000}},
            "research": [{"page": "y" * 5000}] * 4,
            "model_raw_output": "z" * 50000,
            "final_decision": {"action": "HOLD", "rationale": "wide spread"},
        }
        slim = _slim_decision(heavy)
        self.assertEqual(slim["context"]["market"], {"title": "BTC"})
        self.assertNotIn("bids", json.dumps(slim["context"]), "the depth itself is not a list fact")
        self.assertNotIn("model_raw_output", slim)
        self.assertNotIn("research", slim)
        self.assertEqual(slim["research_count"], 4)
        self.assertEqual(slim["final_decision"]["rationale"], "wide spread")
        self.assertTrue(slim["slim"])
        self.assertLess(len(json.dumps(slim)), len(json.dumps(heavy)) / 20)

    def test_a_discovery_row_keeps_what_it_never_saw(self) -> None:
        """A shortlist of one reads differently when five were dropped for settling too far out."""
        from prediction_market_agent.runtime.dashboard import _slim_decision

        slim = _slim_decision({
            "id": 2,
            "context": {"stage": "discovery", "candidates": [{"topic_id": "1", "title": "t"}],
                        "dropped_settling_after_horizon": 5, "horizon_days": 3},
        })
        self.assertEqual(slim["context"]["dropped_settling_after_horizon"], 5)
        self.assertEqual(slim["context"]["horizon_days"], 3)

    def test_an_opened_row_fetches_its_full_record(self) -> None:
        shell = self._shell()
        self.assertIn('if path == "/api/decisions/detail":', shell)
        views = self._views()
        self.assertIn("async function hydrateDecisionEntry", views)
        self.assertIn("'/api/decisions/detail?id='", views)

    def test_the_list_still_says_how_much_research_there_was(self) -> None:
        self.assertIn("Number(r.research_count||0)", self._views())

    def test_the_page_no_longer_promises_a_hundred_rows(self) -> None:
        self.assertNotIn("显示最近 100 条匹配记录", self._shell())


class ResultFilterTests(unittest.TestCase):
    """Selecting results reorders what arrives - matches first - and hides the rest on the page."""

    def _audit(self):
        import tempfile

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        config = Config(
            working_directory=root, session_db=root / "s.sqlite3", auth_db=root / "a.sqlite3",
            management_file=root / "m.json", plugin_directories_file=root / "d.json",
            application_config_file=root / "app.json",
        )
        memory = SessionMemory(config.session_db)
        ids = {}
        for index, (action, status) in enumerate((
            ("HOLD", "NO_ACTION"), ("BUY", "COMPLETED"), (None, "RISK_REJECTED"),
            ("HOLD", "NO_ACTION"), ("SELL", "COMPLETED"),
        )):
            decision_id = memory.begin_decision(
                platform="p", market_topic_id="t", market_id="m", token_id=f"o{index}",
                strategy_name="s", strategy_sha256="x", context={},
            )
            memory.connection.execute(
                "UPDATE decision_ledger SET created_at = ? WHERE id = ?", (1000 + index, decision_id)
            )
            memory.connection.commit()
            memory.complete_decision(
                decision_id, provider="x", status=status,
                final_decision=({"action": action} if action else None),
            )
            ids[index] = decision_id
        from prediction_market_agent.runtime.dashboard import AuditData

        data = AuditData(config)
        self.addCleanup(data.management.shutdown)
        return data, ids

    def test_without_a_selection_it_is_plain_newest_first(self) -> None:
        data, ids = self._audit()
        rows = data.decisions(10, 0, "", "", "", "", "concluded")["items"]
        self.assertEqual([r["id"] for r in rows], [ids[4], ids[3], ids[2], ids[1], ids[0]])
        self.assertTrue(all(r["matches_results"] for r in rows))

    def test_selected_results_come_first_and_nothing_is_excluded(self) -> None:
        data, ids = self._audit()
        rows = data.decisions(10, 0, "", "", "", "", "concluded", results="HOLD")["items"]
        self.assertEqual([r["id"] for r in rows[:2]], [ids[3], ids[0]], "matches first, newest first")
        self.assertEqual(len(rows), 5, "the rest still arrive, after the matches")
        self.assertEqual([r["matches_results"] for r in rows], [True, True, False, False, False])

    def test_a_rules_refusal_is_its_own_result(self) -> None:
        data, ids = self._audit()
        rows = data.decisions(10, 0, "", "", "", "", "concluded", results="RISK_REJECTED")["items"]
        self.assertEqual(rows[0]["id"], ids[2])
        self.assertEqual(rows[0]["result"], "RISK_REJECTED")

    def test_the_page_rules_execute(self) -> None:
        import shutil
        import subprocess

        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed; this check needs a JavaScript engine")
        completed = subprocess.run(
            [node, "tests/ledger_filter_check.js",
             "src/prediction_market_agent/runtime/static/dashboard-views.js"],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)


class ReadableRecordTests(unittest.TestCase):
    """A trader should see what was found, how it was analysed, the conclusion and its result."""

    def _audit(self, provider=None):
        import tempfile

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        config = Config(
            working_directory=root, session_db=root / "s.sqlite3", auth_db=root / "a.sqlite3",
            management_file=root / "m.json", plugin_directories_file=root / "d.json",
            application_config_file=root / "app.json",
        )
        memory = SessionMemory(config.session_db)
        decision_id = memory.begin_decision(
            platform="p", market_topic_id="t", market_id="m", token_id="o",
            strategy_name="s", strategy_sha256="x",
            context={"market": {"title": "BTC"}, "active_api_plugin": {"huge": "x" * 50000},
                     "outcome": {"name": "Yes", "displayed_probability": 0.4},
                     "order_book": {"best_bid": 0.39, "best_ask": 0.41}},
        )
        memory.complete_decision(
            decision_id, provider="x", status="NO_ACTION",
            final_decision={"action": "HOLD", "rationale": "spread too wide", "headline": "Hold"},
            execution={"action": "HOLD", "status": "NO_ACTION"},
        )
        from prediction_market_agent.runtime.dashboard import AuditData

        runtime = None
        if provider is not None:
            runtime = SimpleNamespace(_engine=SimpleNamespace(provider=provider))
        data = AuditData(config, runtime=runtime)
        self.addCleanup(data.management.shutdown)
        return data, decision_id

    class _Provider:
        def __init__(self):
            self.calls = 0
            self.payloads = []

        def run(self, payload, **options):
            self.calls += 1
            self.payloads.append(payload)
            return SimpleNamespace(value={"headline": "观望：价差太宽", "found": "BTC 买一 0.39 卖一 0.41",
                                          "analysis": "估计与市场相近"})

    def test_it_is_written_once_and_kept(self) -> None:
        provider = self._Provider()
        data, decision_id = self._audit(provider)
        first = data.readable(decision_id)
        second = data.readable(decision_id)
        self.assertTrue(first["available"])
        self.assertEqual(second["headline"], "观望：价差太宽")
        self.assertTrue(second["cached"])
        self.assertEqual(provider.calls, 1, "the same record must not cost a model call twice")

    def test_the_model_is_given_the_facts_not_the_manifests(self) -> None:
        provider = self._Provider()
        data, decision_id = self._audit(provider)
        data.readable(decision_id)
        brief = json.dumps(provider.payloads[0], ensure_ascii=False)
        self.assertIn("BTC", brief)
        self.assertNotIn("x" * 1000, brief, "plugin manifests were sent to be restated")
        self.assertLess(len(brief), 4000)

    def test_without_a_model_it_says_why_rather_than_failing(self) -> None:
        data, decision_id = self._audit(provider=None)
        answer = data.readable(decision_id)
        self.assertFalse(answer["available"])
        self.assertIn("没有可用的 AI 模型服务", answer["reason"])

    def test_markup_a_model_wrapped_its_answer_in_does_not_reach_the_page(self) -> None:
        """A closing tag cut short by the length limit is how "</analysi" turned up on a card."""
        provider = self._Provider()
        provider.run = lambda payload, **options: SimpleNamespace(value={
            "headline": "<headline>观望</headline>", "found": "BTC 买一 0.39",
            "analysis": "理由充分。</analysi",
        })
        data, decision_id = self._audit(provider)
        answer = data.readable(decision_id)
        self.assertEqual(answer["headline"], "观望")
        self.assertEqual(answer["analysis"], "理由充分。")
        self.assertEqual(data.readable(decision_id)["analysis"], "理由充分。", "stored clean, not cleaned each read")

    def test_only_the_prose_is_restated(self) -> None:
        """Conclusion and result are recorded facts; a paraphrase can only make them less exact."""
        from prediction_market_agent.runtime.dashboard import READABLE_SCHEMA

        self.assertEqual(set(READABLE_SCHEMA["required"]), {"headline", "found", "analysis"})

    def test_a_filled_buy_held_to_settlement_reports_its_profit(self) -> None:
        data, _ = self._audit()
        item = {
            "platform": "p", "token_id": "tok", "created_at": 0,
            "execution": {"order": {"side": "BUY", "status": "FILLED", "quantity": 28.5,
                                    "notional": 12.0, "fee": 0.04}},
        }
        memory = SessionMemory(data.config.session_db)
        memory.record_action(platform="p", market_topic_id="t", token_id="tok", action="REDEEM",
                             request={"winning": True}, result={})
        settled = data._settlement_for(item)
        self.assertTrue(settled["won"])
        self.assertAlmostEqual(settled["profit"], 28.5 - 12.04, places=6)

    def test_an_unsettled_buy_says_so_and_a_hold_has_no_settlement(self) -> None:
        data, _ = self._audit()
        pending = {"platform": "p", "token_id": "none", "created_at": 0,
                   "execution": {"order": {"side": "BUY", "status": "FILLED", "quantity": 1,
                                           "notional": 1, "fee": 0}}}
        self.assertEqual(data._settlement_for(pending), {"settled": False})
        self.assertIsNone(data._settlement_for({"execution": {"action": "HOLD", "status": "NO_ACTION"}}))

    def test_the_card_renders_every_record_shape(self) -> None:
        import shutil
        import subprocess

        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed; this check needs a JavaScript engine")
        completed = subprocess.run(
            [node, "tests/ledger_render_check.js",
             "src/prediction_market_agent/runtime/static/dashboard-views.js"],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)


class HeadlineContractTests(unittest.TestCase):
    def test_both_calls_must_return_a_headline(self) -> None:
        from prediction_market_agent.agent.decision import DECISION_SCHEMA
        from prediction_market_agent.runtime.market_discovery import DISCOVERY_SCHEMA

        self.assertIn("headline", DECISION_SCHEMA["required"])
        self.assertIn("headline", DISCOVERY_SCHEMA["required"])

    def test_a_decision_recorded_before_headlines_still_reads(self) -> None:
        from prediction_market_agent.agent.decision import Decision

        decision = Decision.from_mapping({
            "action": "HOLD", "order_type": "MARKET", "notional_usdt": 0, "quantity_fraction": 0,
            "limit_price": None, "confidence": 0.5, "estimated_probability": 0.5, "rationale": "r",
        })
        self.assertEqual(decision.headline, "")


class DeletingManyAtOnceTests(unittest.TestCase):
    """Ticked rows, or everything under one tab and filter - with the same protections as one."""

    def setUp(self) -> None:
        import tempfile

        from prediction_market_agent.runtime.dashboard import AuditData

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.config = Config(
            working_directory=root, session_db=root / "s.sqlite3", auth_db=root / "a.sqlite3",
            management_file=root / "m.json", plugin_directories_file=root / "d.json",
            application_config_file=root / "app.json",
        )
        self.memory = SessionMemory(self.config.session_db)
        self.addCleanup(self.memory.close)
        self.data = AuditData(self.config)
        self.addCleanup(self.data.management.shutdown)

    def _row(self, status: str, action: str | None = None, platform: str = "polymarket") -> int:
        decision_id = self.memory.begin_decision(
            platform=platform, market_topic_id="t", market_id="m", token_id=f"o{status}{action}",
            strategy_name="built_in", strategy_sha256="x", context={},
        )
        if status != SessionMemory.IN_PROGRESS:
            self.memory.complete_decision(
                decision_id, provider="claude", status=status,
                final_decision={"action": action, "rationale": "r"} if action else None,
            )
        return decision_id

    def _remaining(self) -> set[int]:
        return {int(row[0]) for row in self.memory.connection.execute("SELECT id FROM decision_ledger")}

    def test_the_category_is_the_tab_and_its_selected_results(self) -> None:
        hold_a, hold_b = self._row("NO_ACTION", "HOLD"), self._row("NO_ACTION", "HOLD")
        buy = self._row("COMPLETED", "BUY")
        failed = self._row("PROVIDER_ERROR")
        match = {"group": "concluded", "results": "HOLD"}
        preview = self.data.forget_matching(match, dry_run=True)
        self.assertEqual(preview["matching"], 2, "on the page a result reorders; deleting it selects")
        self.assertEqual(self._remaining(), {hold_a, hold_b, buy, failed}, "a dry run deletes nothing")
        answer = self.data.forget_matching(match, dry_run=False, until_id=preview["until_id"])
        self.assertEqual(answer["decisions"], 2)
        self.assertEqual(self._remaining(), {buy, failed})

    def test_a_tab_without_a_result_filter_means_the_whole_tab(self) -> None:
        kept = self._row("NO_ACTION", "HOLD")
        first, second = self._row("PROVIDER_ERROR"), self._row("EXECUTION_ERROR")
        answer = self.data.forget_matching({"group": "failed"}, dry_run=False)
        self.assertEqual(answer["matching"], 2)
        self.assertEqual(self._remaining(), {kept})
        del first, second

    def test_rows_written_after_the_count_are_not_swept_up(self) -> None:
        self._row("PROVIDER_ERROR")
        preview = self.data.forget_matching({"group": "failed"}, dry_run=True)
        later = self._row("PROVIDER_ERROR")
        self.data.forget_matching({"group": "failed"}, dry_run=False, until_id=preview["until_id"])
        self.assertEqual(self._remaining(), {later})

    def test_deciding_to_do_nothing_is_not_a_trade_to_protect(self) -> None:
        """Every decision writes an execution row, so protecting all of them protected every hold."""
        held = self._row("NO_ACTION", "HOLD")
        rejected = self._row("RISK_REJECTED", "BUY")
        failed = self._row("EXECUTION_ERROR", "BUY")
        for decision_id, action, result in (
            (held, "HOLD", {"status": "NO_ACTION"}),
            (rejected, "RISK_REJECTED", {"status": "REJECT", "reason": "over the limit"}),
            (failed, "BUY_FAILED", {"status": "EXECUTION_ERROR", "error": "venue refused"}),
        ):
            self.memory.record_action(platform="polymarket", market_topic_id="t", token_id=str(decision_id),
                                      action=action, request={}, result=result, decision_id=decision_id)
        answer = self.data.forget_decisions([held, rejected, failed], "", "")
        self.assertEqual((answer["decisions"], answer["kept_executed"]), (3, 0))
        self.assertEqual(self._remaining(), set())

    def test_executed_trades_are_kept_and_named(self) -> None:
        traded = self._row("COMPLETED", "BUY")
        self._row("COMPLETED", "BUY")
        self.memory.record_action(platform="polymarket", market_topic_id="t", token_id="x", action="BUY",
                                  request={}, result={"status": "FILLED", "order": {"order_id": "o-1"}},
                                  decision_id=traded)
        preview = self.data.forget_matching({"group": "concluded", "results": "BUY"}, dry_run=True)
        self.assertEqual((preview["matching"], preview["kept_executed"]), (2, 1))
        answer = self.data.forget_matching({"group": "concluded", "results": "BUY"}, dry_run=False)
        self.assertEqual(answer["decisions"], 1)
        self.assertEqual(answer["kept_ids"], [traded])
        self.assertEqual(self._remaining(), {traded})

    def test_other_filters_on_the_page_narrow_it_too(self) -> None:
        mine = self._row("PROVIDER_ERROR", platform="polymarket")
        other = self._row("PROVIDER_ERROR", platform="binance")
        self.data.forget_matching({"group": "failed", "platform": "binance"}, dry_run=False)
        self.assertEqual(self._remaining(), {mine})
        del other

    def test_a_request_that_names_no_tab_is_refused(self) -> None:
        self._row("PROVIDER_ERROR")
        with self.assertRaises(ValueError):
            self.data.forget_matching({}, dry_run=False)

    def test_the_page_offers_both_ways(self) -> None:
        shell = Path("src/prediction_market_agent/runtime/dashboard.py").read_text()
        views = Path("src/prediction_market_agent/runtime/static/dashboard-views.js").read_text()
        self.assertIn('if path == "/api/decisions/forget" and isinstance(payload.get("match"), dict):', shell)
        for fragment in ('id="ledgerPickAll"', 'onclick="forgetPicked()"', 'onclick="forgetCategory()"'):
            self.assertIn(fragment, shell)
        self.assertIn("{match,dry_run:true}", views)
        self.assertIn("{match,until_id:preview.until_id}", views)


class TheLedgerStaysReadableTests(unittest.TestCase):
    """Evidence that only ever grows, shown open, is evidence nobody can read past.

    The survey plan, the candidate table and the incident stream all belong in the ledger and none
    of them belong in front of the decisions: one round's候选 fills a screen, and incidents
    accumulate for as long as the robot has ever run. They are collapsed, and the incidents can be
    cleared up to what was on screen.
    """

    def _views(self) -> str:
        return Path("src/prediction_market_agent/runtime/static/dashboard-views.js").read_text()

    def test_the_survey_plan_and_the_candidate_table_start_collapsed(self) -> None:
        views = self._views()
        self.assertIn('<details class="discovery-detail"><summary>续扫计划与下轮检索', views)
        self.assertIn('<details class="discovery-detail"><summary>最近候选', views)
        for marker in ('<details class="discovery-detail"',):
            self.assertNotIn(marker + " open", views, "collapsed means collapsed by default")

    def test_the_incident_stream_is_collapsed_and_can_be_cleared(self) -> None:
        views = self._views()
        self.assertIn('<details class="discovery-incidents"><summary>采集与分析异常', views)
        self.assertIn("forgetIncidents(event,", views)
        self.assertIn("'/api/incidents/forget'", views)

    def test_clearing_is_bounded_by_what_was_on_screen(self) -> None:
        """An incident recorded while the operator reads must survive their click."""
        source = Path("src/prediction_market_agent/runtime/dashboard.py").read_text()
        body = source[source.index("def forget_incidents("):source.index("def forget_instruction(")]
        self.assertIn("WHERE id <= ?", body)
        self.assertIn('"/api/incidents/forget"', source)

    def test_clearing_removes_only_up_to_that_point(self) -> None:
        from prediction_market_agent.runtime.dashboard import AuditData

        root = Path(tempfile.mkdtemp())
        config = Config(
            working_directory=root, session_db=root / "s.sqlite3", auth_db=root / "a.sqlite3",
            management_file=root / "m.json", plugin_directories_file=root / "d.json",
            application_config_file=root / "app.json",
        )
        memory = SessionMemory(config.session_db)
        ids = [
            memory.record_runtime_incident(
                platform="polymarket", stage="discovery_continuation", severity="warning",
                message=f"第 {index} 条", fallback="继续",
            )
            for index in range(3)
        ]
        memory.connection.close()
        data = AuditData(config)
        self.addCleanup(data.management.shutdown)
        self.assertEqual(data.forget_incidents(ids[1]), {"deleted": 2, "until_id": ids[1]})
        remaining = SessionMemory(config.session_db)
        self.addCleanup(remaining.connection.close)
        left = remaining.connection.execute("SELECT id FROM runtime_incidents").fetchall()
        self.assertEqual([row[0] for row in left], [ids[2]], "the newest one survived the click")


class TheCoreComesFirstTests(unittest.TestCase):
    """The page is for reading decisions; everything else on it is evidence about them.

    The decisions used to be third, under a paragraph explaining how to read the page, a five-step
    diagram, and a full survey-and-candidate section - so the thing the page exists for started
    below the fold while the trivia had the whole screen.
    """

    def _shell(self) -> str:
        return Path("src/prediction_market_agent/runtime/dashboard.py").read_text()

    def test_the_decision_list_comes_before_the_survey_evidence(self) -> None:
        shell = self._shell()
        self.assertLess(
            shell.index('<h3>决策记录</h3>'),
            shell.index('<h3>市场采集与候选分析</h3>'),
            "decisions first, evidence after",
        )

    def test_how_to_read_the_page_does_not_occupy_the_page(self) -> None:
        shell = self._shell()
        guide = shell[shell.index('<details class="page-guide">'):]
        self.assertIn("这个页面怎么看", guide[:80])
        self.assertIn("process-strip", guide[:900], "the five-step diagram is inside it")
        self.assertNotIn('<details class="page-guide" open', shell)


class EachRoundSaysWhatItDidTests(unittest.TestCase):
    """Every discovery round was titled "发现轮次", which distinguishes it from nothing.

    The facts are on the row already: how many candidates it looked at, how many it took, which one
    first, and why it took none. A list of forty rounds with the same four characters on each is a
    list nobody can scan.
    """

    def _views(self) -> str:
        return Path("src/prediction_market_agent/runtime/static/dashboard-views.js").read_text()

    def test_the_generic_label_is_gone(self) -> None:
        views = self._views()
        self.assertNotIn("isDiscovery(r)?'发现轮次'", views)
        self.assertIn("isDiscovery(r)?discoveryTitle(r)", views)

    def test_the_title_is_built_from_what_the_round_did(self) -> None:
        block = self._views()
        block = block[block.index("function discoveryTitle("):]
        for piece in ("选中 ", "一个都没选", "正在挑选", "没选成", "candidates"):
            self.assertIn(piece, block[:1400], piece)

    def test_a_round_that_took_nothing_says_why(self) -> None:
        block = self._views()
        block = block[block.index("function discoveryTitle("):]
        self.assertIn("final.skipped_reason", block[:1400])
