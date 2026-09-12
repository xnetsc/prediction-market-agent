from ._support import *

from prediction_market_agent.plugin_system.contracts import AccountFunds, FundingResult
from prediction_market_agent.agent.consultation import (
    AgentConsultation,
    ConsultOption,
    MAX_ATTEMPTS,
    answer_schema,
    read_values,
)
from prediction_market_agent.plugins.api._funding import (
    CONFLICT_OPTIONS,
    DROP_BOTH,
    FundingRequests,
    KEEP_NEW,
    KEEP_OLD,
    USE_NEW_AMOUNT,
    resolve_conflict,
)


class ScriptedBackend:
    """Answers in a fixed order, so a retry is visibly a second call and not a coincidence."""

    name = "scripted"

    def __init__(self, replies, error: str = ""):
        self.replies = list(replies)
        self.error = error
        self.prompts: list[str] = []
        self.schemas: list[dict] = []
        self.calls: list[str] = []

    def complete(self, prompt, schema, schema_name):
        self.prompts.append(prompt)
        self.schemas.append(schema)
        self.calls.append(schema_name)
        if self.error:
            raise DecisionProviderError(self.error, "raw")
        return StructuredResult(value=self.replies.pop(0), raw_output="raw")


def bound(backend, *, trace=None, payload=None, recorder=None):
    consultation = AgentConsultation()
    consultation.bind(
        backend=backend,
        payload=payload if payload is not None else {"market": {"question": "will it rain"}},
        trace=trace if trace is not None else [],
        preamble="<SYSTEM>",
        instructions="\n<STRATEGY>size from what you hold</STRATEGY>",
        recorder=recorder,
        provider="scripted",
    )
    return consultation


TWO = (ConsultOption(1, "do the first thing"), ConsultOption(2, "do the second thing"))
WITH_FIELDS = (
    ConsultOption(1, "carry on"),
    ConsultOption(2, "use a different figure", fields={"size": "number: the figure you want"}),
)


class AnswerShapeTests(unittest.TestCase):
    """The reply is constrained by contract, not by asking the model nicely."""

    def test_the_offered_values_become_a_closed_set_in_the_schema(self) -> None:
        schema = answer_schema(TWO)
        self.assertEqual(schema["properties"]["choice"]["enum"], [1, 2])
        self.assertEqual(schema["properties"]["choice"]["type"], "integer")
        self.assertFalse(schema["additionalProperties"])
        self.assertIn("reason", schema["required"])

    def test_a_reason_cannot_be_omitted_or_empty(self) -> None:
        self.assertEqual(answer_schema(TWO)["properties"]["reason"]["minLength"], 1)

    def test_a_field_an_option_needs_is_required_only_for_that_option(self) -> None:
        self.assertEqual(read_values(WITH_FIELDS[0], "{}"), {})
        with self.assertRaises(ValueError):
            read_values(WITH_FIELDS[1], "{}")

    def test_numbers_are_read_as_numbers_and_nonsense_is_refused(self) -> None:
        self.assertEqual(read_values(WITH_FIELDS[1], '{"size": "120"}'), {"size": 120.0})
        for bad in ('{"size": "soon"}', '{"size": true}', '{"size": null}'):
            with self.assertRaises(ValueError):
                read_values(WITH_FIELDS[1], bad)

    def test_what_the_option_never_asked_for_is_dropped(self) -> None:
        """Refusing a reply over an extra key would spend an attempt and learn nothing."""
        self.assertEqual(
            read_values(WITH_FIELDS[1], '{"size": 5, "mood": "confident"}'), {"size": 5.0}
        )


class AskingTests(unittest.TestCase):
    def test_a_valid_answer_comes_back_as_the_option_that_was_offered(self) -> None:
        backend = ScriptedBackend([{"choice": 2, "values_json": "{}", "reason": "the second reads better"}])
        answer = bound(backend).ask("which one", TWO, subject="a plugin")
        self.assertTrue(answer.ok)
        self.assertTrue(answer.chose(2))
        self.assertEqual(answer.label, "do the second thing")
        self.assertEqual(answer.reason, "the second reads better")

    def test_an_answer_outside_the_options_is_asked_again_with_what_was_wrong(self) -> None:
        backend = ScriptedBackend([
            {"choice": 9, "values_json": "{}", "reason": "nine"},
            {"choice": 1, "values_json": "{}", "reason": "one then"},
        ])
        answer = bound(backend).ask("which one", TWO)
        self.assertTrue(answer.chose(1))
        self.assertEqual(len(backend.prompts), 2)
        self.assertIn("PREVIOUS_ATTEMPT_REJECTED", backend.prompts[1])
        self.assertIn("9", backend.prompts[1])

    def test_a_missing_field_is_asked_again_naming_the_field(self) -> None:
        backend = ScriptedBackend([
            {"choice": 2, "values_json": "{}", "reason": "different figure"},
            {"choice": 2, "values_json": '{"size": 42}', "reason": "forty two"},
        ])
        answer = bound(backend).ask("which one", WITH_FIELDS)
        self.assertEqual(answer.values, {"size": 42.0})
        self.assertIn("size", backend.prompts[1])

    def test_a_model_that_never_answers_within_the_options_gives_up_rather_than_looping(self) -> None:
        backend = ScriptedBackend([{"choice": 9, "values_json": "{}", "reason": "n"}] * 10)
        answer = bound(backend).ask("which one", TWO)
        self.assertFalse(answer.ok)
        self.assertEqual(len(backend.prompts), MAX_ATTEMPTS)
        self.assertIn("did not answer", answer.error)

    def test_an_unreachable_model_is_a_failure_and_never_an_exception(self) -> None:
        """The caller is mid-operation; an exception here abandons that half-done."""
        answer = bound(ScriptedBackend([], error="quota gone")).ask("which one", TWO)
        self.assertFalse(answer.ok)
        self.assertEqual(answer.choice, 0)
        self.assertIn("quota gone", answer.error)

    def test_asking_with_nobody_there_fails_instead_of_inventing_a_default(self) -> None:
        answer = AgentConsultation().ask("which one", TWO)
        self.assertFalse(answer.ok)
        self.assertIn("nobody to ask", answer.error)

    def test_unusable_options_are_the_plugins_bug_and_are_reported_not_asked(self) -> None:
        backend = ScriptedBackend([{"choice": 1, "values_json": "{}", "reason": "r"}])
        consultation = bound(backend)
        for options, complaint in [
            ((), "no options"),
            ((ConsultOption(1, "a"), ConsultOption(1, "b")), "twice"),
            ((ConsultOption(1, "  "),), "no label"),
            ((ConsultOption(1, "a", fields={"x": "colour: blue"}),), "unknown type"),
        ]:
            answer = consultation.ask("which one", options)
            self.assertFalse(answer.ok)
            self.assertIn(complaint, answer.error)
        self.assertEqual(backend.prompts, [])


class QuestionContextTests(unittest.TestCase):
    """A question with no situation attached cannot be answered, only guessed at."""

    def test_the_question_arrives_with_the_round_it_came_from(self) -> None:
        trace = [{"tool": "GET_ORDER_BOOK", "reason": "check the price",
                  "result": {"best_ask": 0.42}}]
        backend = ScriptedBackend([{"choice": 1, "values_json": "{}", "reason": "r"}])
        bound(backend, trace=trace, payload={"market": {"question": "BTC over 100k"}}).ask(
            "which one", TWO, subject="a plugin"
        )
        prompt = backend.prompts[0]
        for expected in ("BTC over 100k", "GET_ORDER_BOOK", "check the price", "0.42",
                         "<STRATEGY>", "a plugin", "which one", "do the second thing"):
            self.assertIn(expected, prompt)

    def test_the_call_being_questioned_is_in_the_question(self) -> None:
        """A tool joins the trace only once it returns, so mid-call it is otherwise invisible."""
        backend = ScriptedBackend([{"choice": 1, "values_json": "{}", "reason": "r"}])
        consultation = bound(backend, trace=[])
        consultation.entering("ENSURE_FUNDS", {"amount": 260, "reason": "a wider spread"})
        consultation.ask("which one", TWO)
        self.assertIn("CALL_IN_PROGRESS", backend.prompts[0])
        self.assertIn("260", backend.prompts[0])
        self.assertIn("a wider spread", backend.prompts[0])

    def test_the_session_remembers_being_asked(self) -> None:
        """Without this the next step is taken by a model walking into the same fork again."""
        trace: list[dict] = []
        backend = ScriptedBackend([{"choice": 2, "values_json": "{}", "reason": "because"}])
        bound(backend, trace=trace).ask("which one", TWO, subject="a plugin")
        self.assertEqual(trace[0]["consulted_by"], "a plugin")
        self.assertEqual(trace[0]["your_answer"]["choice"], 2)

    def test_a_failed_question_is_recorded_as_well_as_an_answered_one(self) -> None:
        recorded: list[dict] = []
        bound(ScriptedBackend([], error="down"), recorder=lambda **v: recorded.append(v)).ask(
            "which one", TWO, subject="a plugin"
        )
        self.assertEqual(recorded[0]["status"], "CONSULT_ERROR")
        self.assertIn("down", recorded[0]["error"])

    def test_a_broken_recorder_does_not_break_the_answer(self) -> None:
        def explode(**values):
            raise RuntimeError("the audit table is gone")

        answer = bound(
            ScriptedBackend([{"choice": 1, "values_json": "{}", "reason": "r"}]), recorder=explode
        ).ask("which one", TWO)
        self.assertTrue(answer.ok)


class FundingConflictTests(unittest.TestCase):
    """Two asks cannot both wait, and which one survives is the asker's to say."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = FundingRequests(Path(self.temp.name) / "funding.json")
        self.first = self.store.record(
            amount=120.0, currency="USDT", available=0.0, reason="YES looked 8 points cheap",
            timeout_seconds=1800,
        )

    def _resolve(self, reply, amount=260.0, reason="a wider spread turned up"):
        backend = ScriptedBackend([reply] if isinstance(reply, dict) else reply)
        self.backend = backend
        return resolve_conflict(
            self.store, amount=amount, currency="USDT", reason=reason, consult=bound(backend)
        )

    def test_the_four_answers_are_offered_and_only_one_carries_a_figure(self) -> None:
        values = [option.value for option in CONFLICT_OPTIONS]
        self.assertEqual(values, [KEEP_NEW, KEEP_OLD, DROP_BOTH, USE_NEW_AMOUNT])
        carrying = [option for option in CONFLICT_OPTIONS if option.fields]
        self.assertEqual([option.value for option in carrying], [USE_NEW_AMOUNT])
        self.assertEqual(set(carrying[0].fields), {"amount", "reason"})

    def test_both_asks_and_both_reasons_are_put_to_the_asker(self) -> None:
        self._resolve({"choice": KEEP_NEW, "values_json": "{}", "reason": "the new one"})
        question = self.backend.prompts[0]
        self.assertIn("YES looked 8 points cheap", question)
        self.assertIn("a wider spread turned up", question)
        self.assertIn("120", question)
        self.assertIn("260", question)

    def test_keeping_the_new_one_retires_the_old_one_by_id(self) -> None:
        resolution = self._resolve({"choice": KEEP_NEW, "values_json": "{}", "reason": "newer"})
        self.assertTrue(resolution.proceed)
        self.assertEqual(resolution.amount, 260.0)
        retired = self.store.find(self.first["request_id"])
        self.assertEqual(retired["state"], "refused")
        self.assertIn("newer", retired["detail"])

    def test_keeping_the_old_one_leaves_it_untouched_and_names_it(self) -> None:
        resolution = self._resolve({"choice": KEEP_OLD, "values_json": "{}", "reason": "older"})
        self.assertFalse(resolution.proceed)
        self.assertEqual(self.store.pending()["request_id"], self.first["request_id"])
        self.assertIn(self.first["request_id"], resolution.detail)

    def test_dropping_both_leaves_nothing_waiting(self) -> None:
        resolution = self._resolve({"choice": DROP_BOTH, "values_json": "{}", "reason": "off it"})
        self.assertFalse(resolution.proceed)
        self.assertIsNone(self.store.pending())
        self.assertEqual(self.store.find(self.first["request_id"])["state"], "refused")

    def test_a_corrected_figure_replaces_both_asks(self) -> None:
        """Forcing a choice between two amounts it no longer believes in wastes the correction."""
        resolution = self._resolve(
            {"choice": USE_NEW_AMOUNT, "reason": "neither",
             "values_json": '{"amount": 150, "reason": "150 is what the book supports"}'}
        )
        self.assertTrue(resolution.proceed)
        self.assertEqual(resolution.amount, 150.0)
        self.assertEqual(resolution.reason, "150 is what the book supports")
        self.assertIsNone(self.store.pending())

    def test_a_corrected_figure_with_no_figure_is_asked_again(self) -> None:
        resolution = self._resolve([
            {"choice": USE_NEW_AMOUNT, "values_json": "{}", "reason": "neither"},
            {"choice": USE_NEW_AMOUNT, "reason": "neither",
             "values_json": '{"amount": 90, "reason": "90 then"}'},
        ])
        self.assertEqual(resolution.amount, 90.0)

    def test_an_unanswerable_question_keeps_the_waiting_request_and_refuses_the_new_ask(self) -> None:
        """The waiting one may be seconds from approval; a failed question must not destroy it."""
        backend = ScriptedBackend([], error="the model is down")
        resolution = resolve_conflict(
            self.store, amount=260.0, currency="USDT", reason="a wider spread",
            consult=bound(backend),
        )
        self.assertFalse(resolution.proceed)
        self.assertFalse(resolution.settled_by_asker)
        self.assertEqual(self.store.pending()["request_id"], self.first["request_id"])
        self.assertIn(self.first["request_id"], resolution.detail)
        self.assertIn("FUNDING_STATUS", resolution.detail)

    def test_with_nobody_to_ask_the_newest_ask_stands(self) -> None:
        """A panel action or a sweep has no asker; the operator must not see a stale intention."""
        resolution = resolve_conflict(
            self.store, amount=260.0, currency="USDT", reason="a wider spread", consult=None
        )
        self.assertTrue(resolution.proceed)
        self.assertEqual(self.store.find(self.first["request_id"])["state"], "refused")

    def test_nothing_is_asked_when_nothing_is_waiting(self) -> None:
        self.store.clear()
        backend = ScriptedBackend([])
        resolution = resolve_conflict(
            self.store, amount=50.0, currency="USDT", reason="first ask", consult=bound(backend)
        )
        self.assertTrue(resolution.proceed)
        self.assertEqual(resolution.detail, "")
        self.assertEqual(backend.prompts, [])

    def test_an_expired_request_is_not_a_conflict(self) -> None:
        self.store.record(amount=120.0, currency="USDT", available=0.0, reason="old",
                          timeout_seconds=-1)
        backend = ScriptedBackend([])
        resolution = resolve_conflict(
            self.store, amount=50.0, currency="USDT", reason="fresh", consult=bound(backend)
        )
        self.assertTrue(resolution.proceed)
        self.assertEqual(backend.prompts, [])


def hold_decision() -> dict:
    return {
        "action": "HOLD", "order_type": "MARKET", "notional_usdt": 0, "quantity_fraction": 0,
        "limit_price": None, "confidence": 0.7, "estimated_probability": 0.5,
        "rationale": "Nothing to do until the money question is settled.",
    }


class ThroughTheRealLoopTests(unittest.TestCase):
    """The wire, not the pieces: a plugin questioning the model part-way through its own call."""

    def _plugin(self, store):
        class Plugin:
            name = "venue"

            def __init__(self) -> None:
                self.asked_with = None

            def account_funds(self):
                return AccountFunds(available=0.0, currency="USDT", source="platform")

            def ensure_funds(self, amount, currency, *, reason="", allow_pending=True,
                             timeout_seconds=1800, consult=None):
                self.asked_with = consult
                resolution = resolve_conflict(
                    store, amount=amount, currency=currency, reason=reason, consult=consult
                )
                if not resolution.proceed:
                    return FundingResult(
                        request_id="", state="refused", requested=amount, currency=currency,
                        available=0.0, action="request_already_waiting",
                        detail=resolution.detail,
                    )
                request = store.record(
                    amount=resolution.amount, currency=currency, available=0.0,
                    reason=resolution.reason, timeout_seconds=timeout_seconds,
                )
                return FundingResult(
                    request_id=request["request_id"], state="pending", requested=resolution.amount,
                    currency=currency, available=0.0, action="awaiting_operator_approval",
                    detail=resolution.detail,
                )

        return Plugin()

    def _record(self, name, result):
        self.tool_results.append((name, result))
        return result

    def _run(self, script, plugin):
        self.tool_results = []
        from prediction_market_agent.agent.decision import AgentDecisionProvider
        from prediction_market_agent.agent.research import ResearchToolContext, ResearchToolbox
        from prediction_market_agent.runtime.market_tools import DESCRIPTIONS, MarketToolset

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        memory = SessionMemory(Path(temp.name) / "m.sqlite3")
        consultation = AgentConsultation()
        toolbox = ResearchToolbox(
            [MarketToolset({"venue": SimpleNamespace(plugin=plugin)}, "venue")],
            ResearchToolContext(
                client=plugin, memory=memory, platform="venue", market_topic_id="t",
                market_id="m", token_id="o", symbol="BTCUSDT", history_limit=1,
                consultation=consultation,
            ),
        )
        backend = ScriptedBackend(script)
        provider = AgentDecisionProvider(backend, config(Path(temp.name) / "state.json"))
        result = provider.decide(
            {"market": {"title": "t", "outcome": "YES"},
             "order_book": {"best_bid": 0.40, "best_ask": 0.42},
             "portfolio": {"platform": "venue", "cash": 0.0}},
            tool_executor=lambda name, arguments: self._record(
                name, toolbox.execute(name, arguments)
            ),
            tool_descriptions=DESCRIPTIONS,
            instructions="\n<STRATEGY>size from what you hold</STRATEGY>",
            consultation=consultation,
        )
        return backend, consultation, result

    def test_the_plugin_receives_a_live_way_to_ask_and_the_answer_shapes_what_it_does(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = FundingRequests(Path(temp.name) / "funding.json")
        first = store.record(amount=120.0, currency="USDT", available=0.0, reason="cheap YES",
                             timeout_seconds=1800)
        plugin = self._plugin(store)
        backend, consultation, result = self._run([
            {"next_action": "ENSURE_FUNDS",
             "arguments_json": '{"amount": 260, "reason": "a wider spread"}',
             "reason": "need more"},
            # The plugin's question is answered by the same model, mid-tool-call.
            {"choice": USE_NEW_AMOUNT, "reason": "neither figure is right",
             "values_json": '{"amount": 150, "reason": "150 is what the book supports"}'},
            {"next_action": "DECIDE", "arguments_json": "{}", "reason": "done"},
            hold_decision(),
        ], plugin)
        self.assertEqual(
            [name for name, _ in self.tool_results], ["ENSURE_FUNDS"],
            f"the tool did not run cleanly: {self.tool_results}",
        )
        self.assertIs(plugin.asked_with, consultation, "the plugin got the asking interface")
        self.assertIn("plugin_consultation", backend.calls)
        self.assertEqual(store.pending()["amount"], 150.0, "the corrected figure is what waits")
        self.assertEqual(store.find(first["request_id"])["state"], "refused")
        self.assertEqual(result.decision.action, "HOLD")

    def test_the_model_sees_both_the_question_it_answered_and_what_came_of_it(self) -> None:
        """Answering and then being blind to the consequence would invite the same ask again."""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = FundingRequests(Path(temp.name) / "funding.json")
        store.record(amount=120.0, currency="USDT", available=0.0, reason="cheap YES",
                     timeout_seconds=1800)
        plugin = self._plugin(store)
        backend, _, _ = self._run([
            {"next_action": "ENSURE_FUNDS",
             "arguments_json": '{"amount": 260, "reason": "a wider spread"}', "reason": "more"},
            {"choice": KEEP_OLD, "values_json": "{}", "reason": "the first one still stands"},
            {"next_action": "DECIDE", "arguments_json": "{}", "reason": "done"},
            hold_decision(),
        ], plugin)
        self.assertIn("plugin_consultation", backend.calls)
        self.assertEqual(backend.calls.count("agent_control"), 2)

    def test_the_consultation_is_let_go_when_the_round_ends(self) -> None:
        """A handle still pointing at a finished round could question a dead backend."""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = FundingRequests(Path(temp.name) / "funding.json")
        _, consultation, _ = self._run([
            {"next_action": "DECIDE", "arguments_json": "{}", "reason": "nothing to do"},
            hold_decision(),
        ], self._plugin(store))
        self.assertFalse(consultation.open)


class DerivableConfigurationTests(unittest.TestCase):
    """Asking for something the plugin can work out is asking the operator to do its job."""

    def _fields(self, plugin: str) -> dict:
        from prediction_market_agent.plugin_system.discovery import load_plugin_catalog
        from prediction_market_agent.core.config import Config as _Config

        catalog = load_plugin_catalog(_Config(state_file=Path("/tmp/derivable-state.json")))
        self.addCleanup(catalog.shutdown)
        return {f.name: f for f in catalog.get("api", plugin).configuration.fields}

    def test_the_signing_key_is_the_only_thing_polymarket_cannot_work_out(self) -> None:
        fields = self._fields("polymarket")
        self.assertTrue(fields["POLYMARKET_PRIVATE_KEY"].needed_to_run)
        for derivable in (
            "POLYMARKET_API_KEY", "POLYMARKET_API_SECRET", "POLYMARKET_API_PASSPHRASE",
            "POLYMARKET_FUNDER_ADDRESS",
        ):
            with self.subTest(field=derivable):
                self.assertFalse(
                    fields[derivable].needed_to_run,
                    f"{derivable} follows from the private key and must not be demanded",
                )

    def test_a_half_filled_credential_set_is_refused_as_configuration_not_as_auth(self) -> None:
        """Two of three silently ignored would surface later as a login error nobody could place."""
        from prediction_market_agent.plugins.api._polymarket.write import PolymarketWriteTransport

        transport = PolymarketWriteTransport.__new__(PolymarketWriteTransport)
        transport._client = None
        transport.settings = SimpleNamespace(
            api_key="only-the-key", api_secret="", api_passphrase="",
            private_key="0x" + "11" * 32, funder_address="",
            chain_id=137, gamma_url="https://g", clob_url="https://c", data_url="https://d",
            relayer_url="https://r", rpc_url="https://rpc",
        )
        with self.assertRaises(ValueError) as caught:
            transport._require_client()
        self.assertIn("三项", str(caught.exception))

    def test_leaving_all_three_blank_derives_them_over_the_plugins_own_transport(self) -> None:
        """Deriving over the SDK's construction-time transport would bypass the configured proxy."""
        import inspect
        from prediction_market_agent.plugins.api._polymarket import write

        derive = inspect.getsource(write.PolymarketWriteTransport.derive_credentials)
        self.assertIn("self._http_client(", derive, "the transport must be the plugin's own")
        require = inspect.getsource(write.PolymarketWriteTransport._require_client)
        self.assertIn("self.derive_credentials(environment)", require)

    def test_generating_a_wallet_is_offered_and_never_taken_unasked(self) -> None:
        """Who holds the key is the operator's decision, so it waits for them to make it."""
        import inspect
        from prediction_market_agent.plugins.api import polymarket as plugin

        source = inspect.getsource(plugin)
        self.assertIn("Account.create()", source, "the plugin can make a wallet")
        offer = source[source.index("def wallet_notices"):source.index("def wallet_action")]
        self.assertIn("action_label", offer, "and only offers it as something to press")
        self.assertNotIn("Account.create()", offer, "never inside the listing itself")


class NoticesReachThePageTests(unittest.TestCase):
    """A control the operator never sees is a control that does not exist."""

    def test_the_page_gates_notices_on_the_field_the_payload_carries(self) -> None:
        """It gated on `notices`, which no summary ever contained, so none were ever drawn."""
        from prediction_market_agent.plugin_system.discovery import PluginSpec

        script = Path("src/prediction_market_agent/runtime/static/dashboard-views.js").read_text()
        gate = [
            line for line in script.split("\n")
            if "renderPluginNotices(card" in line and "p.enabled" in line
        ]
        self.assertEqual(len(gate), 1, "one place decides whether a plugin's notices are drawn")
        self.assertIn("p.has_notices", gate[0])
        self.assertNotIn("p.notices", gate[0])

        manifest = PluginSpec(
            kind="api", name="probe", description="a plugin that has something to say",
            origin="o", factory=lambda config: None, teardown=lambda: None,
            notices_callback=lambda: [],
        ).manifest()
        self.assertIn(
            "has_notices", manifest,
            "the page reads this key, so the payload has to be the thing that carries it",
        )
