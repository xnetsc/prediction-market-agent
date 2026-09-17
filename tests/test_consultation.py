from ._support import *
import json
import threading

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


class WalletCustodyTests(unittest.TestCase):
    """A machine that can spend money its owner cannot reach is the failure to avoid."""

    def _spec(self):
        import importlib.util
        import sys
        from prediction_market_agent.plugin_system.discovery import PluginInitializationContext

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path("src/prediction_market_agent/plugins/api/polymarket.py").resolve()
        spec_mod = importlib.util.spec_from_file_location("polymarket_custody_probe", path)
        module = importlib.util.module_from_spec(spec_mod)
        sys.modules["polymarket_custody_probe"] = module
        self.addCleanup(sys.modules.pop, "polymarket_custody_probe", None)
        spec_mod.loader.exec_module(module)
        return module.initialize_plugin(
            PluginInitializationContext(
                kind="api", module_path=path, working_directory=Path(temp.name)
            )
        )

    def test_a_generated_key_can_be_taken_back_out(self) -> None:
        spec = self._spec()
        self.assertTrue(spec.notice_action_callback("wallet", "confirm", {})["ok"])
        stored = spec.configuration.load()["POLYMARKET_PRIVATE_KEY"]
        exported = spec.notice_action_callback("wallet_backup", "confirm", {})
        self.assertTrue(exported["ok"])
        self.assertIn(stored, exported["reveal"], "the key itself has to come back, not a hint")

    def test_the_key_is_never_in_the_configuration_manifest(self) -> None:
        """Export is a deliberate act; leaking it into every page render is not."""
        spec = self._spec()
        spec.notice_action_callback("wallet", "confirm", {})
        stored = spec.configuration.load()["POLYMARKET_PRIVATE_KEY"]
        self.assertNotIn(stored, json.dumps(spec.configuration.manifest()))

    def test_nothing_to_export_says_so_rather_than_returning_nothing(self) -> None:
        spec = self._spec()
        answer = spec.notice_action_callback("wallet_backup", "confirm", {})
        self.assertFalse(answer["ok"])
        self.assertIn("还没有私钥", answer["message"])


class MemoryAcrossThreadsTests(unittest.TestCase):
    """The loop and the console are different threads; a store only one can reach is no store."""

    def _memory(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        return SessionMemory(Path(temp.name) / "m.sqlite3")

    def _in_thread(self, work):
        outcome: list = []
        thread = threading.Thread(target=lambda: outcome.append(self._call(work)))
        thread.start()
        thread.join()
        return outcome[0]

    @staticmethod
    def _call(work):
        try:
            return ("ok", work())
        except Exception as error:
            return ("error", error)

    def test_another_thread_can_read(self) -> None:
        memory = self._memory()
        memory.settled_decisions()
        state, value = self._in_thread(memory.settled_decisions)
        self.assertEqual(state, "ok", f"a reader thread was refused: {value}")

    def test_another_thread_can_write_and_the_first_sees_it(self) -> None:
        """A cycle that records from its own thread is worthless if the page cannot read it back."""
        memory = self._memory()
        state, value = self._in_thread(
            lambda: memory.open_funding_continuation(
                request_id="r-1", platform="venue", market_topic_id="t", token_id="o",
                decision_id=1, asked_for=10.0, currency="USDT", reason="because",
                conclusion={},
            )
        )
        self.assertEqual(state, "ok", f"a writer thread was refused: {value}")
        self.assertEqual(len(memory.open_funding_continuations("venue")), 1)


class ClaudeTurnBudgetTests(unittest.TestCase):
    """One turn was not enough to answer, and the failure said nothing about why."""

    def _backend(self, returncode: int, stdout: str):
        from prediction_market_agent.plugins.providers.claude import ClaudeCliBackend

        backend = ClaudeCliBackend.__new__(ClaudeCliBackend)
        backend.executable = "/bin/claude"
        backend.model = ""
        backend.effort = ""
        backend.proxy = ""
        backend.timeout = 60
        backend.control = None
        self.commands: list[list[str]] = []

        def fake_run(command, **options):
            self.commands.append(command)
            return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

        return backend, fake_run

    def _complete(self, backend, fake_run):
        import prediction_market_agent.plugins.providers.claude as module

        with patch.object(module.subprocess, "run", fake_run):
            return backend.complete("prompt", {"type": "object"}, "probe")

    def test_the_budget_covers_thinking_before_answering(self) -> None:
        backend, fake_run = self._backend(0, json.dumps({"structured_output": {"ok": True}}))
        self._complete(backend, fake_run)
        [command] = self.commands
        turns = int(command[command.index("--max-turns") + 1])
        self.assertGreater(turns, 1, "a single turn is spent thinking and never reaches an answer")

    def test_a_failure_says_which_way_it_failed(self) -> None:
        """Empty stderr plus a bare message is a provider that looks broken for no stated reason."""
        from prediction_market_agent.agent.decision import DecisionProviderError

        backend, fake_run = self._backend(
            1, json.dumps({"is_error": True, "subtype": "error_max_turns",
                           "terminal_reason": "max_turns"})
        )
        with self.assertRaises(DecisionProviderError) as caught:
            self._complete(backend, fake_run)
        self.assertIn("error_max_turns", str(caught.exception))


class DecisionCapacityTests(unittest.TestCase):
    """Every cycle exists to reach a decision. With nothing able to answer, the rest is waste."""

    def _watch(self, *, names=("codex", "claude"), unavailable=None):
        from prediction_market_agent.agent.provider_health import ProviderHealthRegistry
        from prediction_market_agent.runtime.decision_capacity import DecisionCapacityWatch

        health = ProviderHealthRegistry(names)
        provider = SimpleNamespace(
            available_names=names, health=health, unavailable=unavailable or {}
        )
        self.told: list[dict] = []
        watch = DecisionCapacityWatch(
            provider, lambda: [("a-platform", self.told.append)]
        )
        return watch, health

    def test_a_provider_in_backoff_is_not_capacity(self) -> None:
        watch, health = self._watch()
        self.assertTrue(watch.check()["available"])
        health.record_failure("codex", "You've hit your usage limit")
        self.assertTrue(watch.check()["available"], "one left is still enough")
        health.record_failure("claude", "You've hit your usage limit")
        reading = watch.check()
        self.assertFalse(reading["available"])
        self.assertEqual(set(reading["waiting"]), {"codex", "claude"})

    def test_the_platforms_are_told_only_when_the_answer_turns_over(self) -> None:
        watch, health = self._watch()
        watch.check()
        health.record_failure("codex", "usage limit")
        health.record_failure("claude", "usage limit")
        watch.check()
        watch.check()
        self.assertEqual(len(self.told), 1, "the same fact twice is not news")
        self.assertFalse(self.told[0]["available"])
        health.record_success("claude", latency_seconds=0.5)
        watch.check()
        self.assertEqual(len(self.told), 2)
        self.assertTrue(self.told[1]["available"])

    def test_why_each_one_cannot_answer_travels_with_the_fact(self) -> None:
        """A platform that stood down should be able to say what it is waiting for."""
        watch, health = self._watch(unavailable={"openai_compatible": "请填写 API Key"})
        health.record_failure("codex", "usage limit")
        health.record_failure("claude", "not logged in")
        reading = watch.check()
        self.assertEqual(reading["waiting"]["codex"], "rate_limit")
        self.assertIn("openai_compatible", reading["waiting"])

    def test_a_platform_that_breaks_on_the_news_does_not_stop_the_others(self) -> None:
        from prediction_market_agent.agent.provider_health import ProviderHealthRegistry
        from prediction_market_agent.runtime.decision_capacity import DecisionCapacityWatch

        health = ProviderHealthRegistry(("codex",))
        reached: list[dict] = []

        def explode(message):
            raise RuntimeError("this plugin is broken")

        watch = DecisionCapacityWatch(
            SimpleNamespace(available_names=("codex",), health=health, unavailable={}),
            lambda: [("broken", explode), ("fine", reached.append)],
        )
        watch.check()
        health.record_failure("codex", "usage limit")
        watch.check()
        self.assertEqual(len(reached), 1)


class PlatformStandsDownTests(unittest.TestCase):
    """The framework states the fact; the plugin decides its own schedule."""

    def _loops(self):
        from prediction_market_agent.plugins.api._binance.runtime import BinanceEventLoop
        from prediction_market_agent.plugins.api._polymarket.runtime import PolymarketEventLoop

        return {"binance": BinanceEventLoop, "polymarket": PolymarketEventLoop}

    def test_both_platforms_hold_when_nothing_can_answer_and_resume_when_it_can(self) -> None:
        for name, factory in self._loops().items():
            with self.subTest(platform=name):
                loop = factory.__new__(factory)
                loop._lock = threading.RLock()
                loop._stop = threading.Event()
                loop._may_decide = threading.Event()
                loop._may_decide.set()
                loop._status = {}
                loop.notify({"kind": "decision_capacity", "available": False,
                             "waiting": {"codex": "rate_limit"}})
                self.assertFalse(loop._may_decide.is_set())
                self.assertTrue(loop.status()["holding"])
                self.assertIn("rate_limit", loop.status()["holding_because"])
                loop.notify({"kind": "decision_capacity", "available": True, "waiting": {}})
                self.assertTrue(loop._may_decide.is_set())
                self.assertFalse(loop.status()["holding"])

    def test_a_message_about_something_else_changes_nothing(self) -> None:
        from prediction_market_agent.plugins.api._polymarket.runtime import PolymarketEventLoop

        loop = PolymarketEventLoop.__new__(PolymarketEventLoop)
        loop._lock = threading.RLock()
        loop._stop = threading.Event()
        loop._may_decide = threading.Event()
        loop._may_decide.set()
        loop._status = {}
        loop.notify({"kind": "something_else", "available": False})
        self.assertTrue(loop._may_decide.is_set())

    def test_stopping_releases_a_held_loop(self) -> None:
        """A loop parked waiting for a model must not keep the process from shutting down."""
        from prediction_market_agent.plugins.api._polymarket.runtime import PolymarketEventLoop

        loop = PolymarketEventLoop.__new__(PolymarketEventLoop)
        loop._lock = threading.RLock()
        loop._stop = threading.Event()
        loop._may_decide = threading.Event()
        loop._thread = None
        loop._status = {}
        loop.notify({"kind": "decision_capacity", "available": False, "waiting": {}})
        loop.stop()
        self.assertTrue(loop._may_decide.is_set())


class ForgettingDecisionsTests(unittest.TestCase):
    """A wrong entry does not sit there looking untidy; it goes on shaping what happens next."""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.memory = SessionMemory(Path(temp.name) / "m.sqlite3")

    def _decision(self, *, platform="venue", status="OK", token="o-1"):
        decision_id = self.memory.begin_decision(
            platform=platform, market_topic_id="t-1", market_id="m-1", token_id=token,
            strategy_name="s", strategy_sha256="x", context={},
        )
        self.memory.record_turn(
            platform=platform, provider="p", market_topic_id="t-1", token_id=token,
            input_payload={}, raw_output="", decision=None, status=status,
            decision_id=decision_id,
        )
        self.memory.record_agent_step(
            platform=platform, provider="p", market_topic_id="t-1", token_id=token,
            step_index=0, input_payload={}, raw_output="", control=None, status=status,
            decision_id=decision_id,
        )
        self.memory.complete_decision(
            decision_id, provider="p", model_raw_output="", status=status,
        )
        return decision_id

    def _counts(self):
        return {
            table: self.memory.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("decision_ledger", "provider_turns", "agent_steps")
        }

    def test_a_run_that_failed_for_a_fixed_reason_can_be_taken_out(self) -> None:
        self._decision(status="OK")
        self._decision(status="PROVIDER_ERROR")
        self._decision(status="PROVIDER_ERROR")
        removed = self.memory.forget_decisions(status="PROVIDER_ERROR")
        self.assertEqual(removed["decisions"], 2)
        self.assertEqual(self._counts(), {"decision_ledger": 1, "provider_turns": 1, "agent_steps": 1})

    def test_the_reasoning_goes_with_the_decision(self) -> None:
        """Leaving turns and steps behind would keep feeding the measurements they belong to."""
        decision_id = self._decision(status="PROVIDER_ERROR")
        self.memory.forget_decisions(decision_ids=[decision_id])
        self.assertEqual(self._counts(), {"decision_ledger": 0, "provider_turns": 0, "agent_steps": 0})

    def test_what_the_venue_actually_did_is_never_removed_with_it(self) -> None:
        """The balance is its own state, so deleting the record would leave only the effect."""
        decision_id = self._decision(status="OK")
        self.memory.record_action(
            platform="venue", market_topic_id="t-1", token_id="o-1", action="BUY",
            request={}, result={}, decision_id=decision_id,
        )
        removed = self.memory.forget_decisions(decision_ids=[decision_id])
        self.assertEqual(removed["decisions"], 0)
        self.assertEqual(removed["kept_executed"], 1)
        self.assertEqual(self._counts()["decision_ledger"], 1)

    def test_one_platforms_ledger_can_be_cleared_without_touching_another(self) -> None:
        self._decision(platform="venue")
        self._decision(platform="other")
        self.memory.forget_decisions(platform="venue")
        rows = self.memory.connection.execute(
            "SELECT platform FROM decision_ledger"
        ).fetchall()
        self.assertEqual([row[0] for row in rows], ["other"])

    def test_naming_nothing_is_refused_rather_than_meaning_everything(self) -> None:
        self._decision()
        with self.assertRaises(ValueError) as caught:
            self.memory.forget_decisions()
        self.assertIn("entire ledger", str(caught.exception))
        self.assertEqual(self._counts()["decision_ledger"], 1)

    def test_the_measurements_stop_counting_what_was_removed(self) -> None:
        """The point of allowing this: a provider is ranked on evidence that is still true."""
        self._decision(status="PROVIDER_ERROR")
        self._decision(status="PROVIDER_ERROR")
        before = self.memory.provider_delivery()
        self.memory.forget_decisions(status="PROVIDER_ERROR")
        after = self.memory.provider_delivery()
        self.assertNotEqual(before, after)
        self.assertEqual(after, {})


class BuilderKeyChoiceTests(unittest.TestCase):
    """Polymarket issues the secret once, so a create button that is the only option makes litter."""

    def _spec_with(self, existing):
        import importlib.util
        import sys
        from prediction_market_agent.plugin_system.discovery import PluginInitializationContext

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path("src/prediction_market_agent/plugins/api/polymarket.py").resolve()
        loader = importlib.util.spec_from_file_location("polymarket_builder_probe", path)
        module = importlib.util.module_from_spec(loader)
        sys.modules["polymarket_builder_probe"] = module
        self.addCleanup(sys.modules.pop, "polymarket_builder_probe", None)
        loader.loader.exec_module(module)
        spec = module.initialize_plugin(
            PluginInitializationContext(kind="api", module_path=path,
                                        working_directory=Path(temp.name))
        )
        spec.notice_action_callback("wallet", "confirm", {})  # generate a wallet
        self.created: list[str] = []

        class Instance:
            def create_builder_key(_self, values):
                self.created.append("created")
                return {"ok": True, "created": {"key": "made", "secret": "s", "passphrase": "p"}}

            def approve_trading(_self, values):
                return {"ok": True}

            def wallet_panel(_self):
                return {"builder_api_keys": existing}

        module._live_instance = lambda: Instance()
        return spec

    def test_an_existing_key_can_be_used_instead_of_making_another(self) -> None:
        spec = self._spec_with([{"key": "already-there"}])
        answer = spec.notice_action_callback("wallet", "confirm", {
            "existing_key": "already-there", "existing_secret": "s", "existing_passphrase": "p",
        })
        self.assertTrue(answer["ok"])
        self.assertEqual(self.created, [], "nothing new should have been made")
        self.assertEqual(spec.configuration.load()["POLYMARKET_BUILDER_API_KEY"], "already-there")

    def test_a_half_given_key_is_refused_rather_than_half_saved(self) -> None:
        spec = self._spec_with([])
        answer = spec.notice_action_callback("wallet", "confirm", {"existing_key": "only-the-id"})
        self.assertFalse(answer["ok"])
        self.assertEqual(self.created, [])
        self.assertEqual(spec.configuration.load().get("POLYMARKET_BUILDER_API_KEY", ""), "")

    def test_the_offer_says_whether_one_already_exists(self) -> None:
        """Creating another is a different act from creating the first, and should read as one."""
        import inspect
        from prediction_market_agent.plugins.api import polymarket as plugin

        offer = inspect.getsource(plugin)
        offer = offer[offer.index("def wallet_notices"):offer.index("def _live_instance_panel")]
        self.assertIn("再创建一个 Builder API Key", offer)
        self.assertIn('panel.get("builder_api_keys")', offer)
        for field in ("existing_key", "existing_secret", "existing_passphrase"):
            self.assertIn(field, offer, "the operator must be able to supply one they hold")


class SilentEmptyRoundTests(unittest.TestCase):
    """A round that picked nothing should say why; the model was asked and the answer was dropped."""

    def test_the_schema_asks_for_a_reason_and_the_code_now_reads_it(self) -> None:
        import inspect
        from prediction_market_agent.runtime import market_discovery

        self.assertIn("skipped_reason", market_discovery.DISCOVERY_SCHEMA["required"])
        select = inspect.getsource(market_discovery.DiscoveryEngine._select)
        self.assertIn('result.value.get("skipped_reason"', select)

    def test_an_empty_round_is_logged_with_what_the_model_said(self) -> None:
        import inspect
        from prediction_market_agent.runtime import market_discovery

        source = inspect.getsource(market_discovery.DiscoveryEngine.discover)
        self.assertIn("selected nothing from", source)
        self.assertIn("skipped_reason or", source, "silence must not be reported as a blank")


class LedgerDeleteControlTests(unittest.TestCase):
    """Judging a record to be junk does not require reading it again, so the control is on the row."""

    def _script(self) -> str:
        return Path("src/prediction_market_agent/runtime/static/dashboard-views.js").read_text()

    def test_the_control_sits_on_the_row_not_inside_it(self) -> None:
        script = self._script()
        entry = script[script.index("class=\"decision-entry\""):]
        summary = entry[: entry.index("</summary>")]
        self.assertIn("forgetDecision(event,", summary)
        body = entry[entry.index("</summary>"): entry.index("</details>")]
        self.assertNotIn("forgetDecision", body, "it must not also be buried inside the panel")

    def test_the_click_does_not_open_the_entry_it_is_deleting(self) -> None:
        """A click inside a summary toggles the panel, which would spring open over the prompt."""
        script = self._script()
        handler = script[script.index("async function forgetDecision"):]
        handler = handler[: handler.index("\n}")]
        self.assertIn("event.preventDefault()", handler)
        self.assertIn("event.stopPropagation()", handler)
        self.assertIn("confirm(", handler, "a destructive control on every row needs a check")


class RunningDecisionsCanBeDeletedAndStopTests(unittest.TestCase):
    """A row stuck in STARTED after a restart must be removable, and removing one must end its work."""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.memory = SessionMemory(Path(temp.name) / "m.sqlite3")

    def _open(self, token="o-1") -> int:
        return self.memory.begin_decision(
            platform="venue", market_topic_id="t-1", market_id="m-1", token_id=token,
            strategy_name="s", strategy_sha256="x", context={},
        )

    def test_a_running_decision_is_deleted_and_marked_cancelled(self) -> None:
        decision_id = self._open()
        answer = self.memory.forget_decisions(decision_ids=[decision_id])
        self.assertEqual(answer["decisions"], 1)
        self.assertEqual(answer["cancelled_in_progress"], 1)
        self.assertTrue(self.memory.is_cancelled(decision_id))

    def test_a_new_decision_never_inherits_an_old_cancellation(self) -> None:
        """Ids restart in every database; a cancellation must only ever stop the row it was for."""
        decision_id = self._open()
        self.memory.forget_decisions(decision_ids=[decision_id])
        self.assertTrue(self.memory.is_cancelled(decision_id))
        reopened = self._open(token="o-new")
        self.assertFalse(self.memory.is_cancelled(reopened))

    def test_a_cancellation_in_one_database_does_not_reach_another(self) -> None:
        decision_id = self._open()
        self.memory.forget_decisions(decision_ids=[decision_id])
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        elsewhere = SessionMemory(Path(temp.name) / "other.sqlite3")
        same_id = elsewhere.begin_decision(
            platform="venue", market_topic_id="t", market_id="m", token_id="o",
            strategy_name="s", strategy_sha256="x", context={},
        )
        self.assertEqual(same_id, decision_id, "the point is that both databases use this id")
        self.assertFalse(elsewhere.is_cancelled(same_id))

    def test_the_cancellation_is_visible_to_another_memory_instance(self) -> None:
        """The console deletes through one instance and the engine runs through another."""
        decision_id = self._open()
        self.memory.forget_decisions(decision_ids=[decision_id])
        other = SessionMemory(self.memory.path)
        self.assertTrue(other.is_cancelled(decision_id))

    def test_a_finished_decision_is_not_reported_as_cancelled(self) -> None:
        finished = self._open(token="o-done")
        self.memory.complete_decision(finished, provider="p", status="NO_ACTION")
        answer = self.memory.forget_decisions(decision_ids=[finished])
        self.assertEqual(answer["decisions"], 1)
        self.assertEqual(answer["cancelled_in_progress"], 0)

    def test_the_page_describes_what_deleting_a_running_row_does(self) -> None:
        script = Path("src/prediction_market_agent/runtime/static/dashboard-views.js").read_text()
        self.assertIn("answer.cancelled_in_progress", script)
        self.assertNotIn("kept_in_progress", script)


class CancelledDecisionsStopWorkingTests(unittest.TestCase):
    """Deleted means stopped: no more model calls spent, and above all no order placed."""

    def test_the_control_loop_stops_before_its_next_model_call(self) -> None:
        from prediction_market_agent.agent.decision import AgentDecisionProvider, DecisionCancelled

        calls: list[str] = []
        stop = {"now": False}

        class Backend:
            name = "b"

            def complete(self, prompt, schema, schema_name):
                calls.append(schema_name)
                stop["now"] = True  # deleted while this call was in flight
                return StructuredResult(
                    value={"next_action": "GET_TOPIC", "arguments_json": "{}", "reason": "r"},
                    raw_output="{}",
                )

        provider = AgentDecisionProvider(Backend(), config(Path("/tmp/cancel-state.json")))
        with self.assertRaises(DecisionCancelled):
            provider.decide(
                {"market": {}},
                tool_executor=lambda name, arguments: {},
                tool_descriptions={"GET_TOPIC": {"purpose": "p", "arguments": {}}},
                should_stop=lambda: stop["now"],
            )
        self.assertEqual(calls, ["agent_control"], "it asked the model again after being deleted")

    def test_cancellation_does_not_count_against_the_provider(self) -> None:
        """Nothing failed, so it must not fall through to the next provider or be scored as a fault."""
        from prediction_market_agent.agent.decision import DecisionCancelled, DecisionProviderError

        self.assertFalse(issubclass(DecisionCancelled, DecisionProviderError))

    def test_nothing_is_executed_for_a_decision_deleted_after_the_model_answered(self) -> None:
        import inspect
        from prediction_market_agent.runtime import evaluation

        source = inspect.getsource(evaluation)
        guard = source.index("if self.memory.is_cancelled(decision_id):")
        self.assertLess(guard, source.index("action_rule = self.risk.evaluate("))
        self.assertLess(guard, source.index("execution = self._execute_decision("))
