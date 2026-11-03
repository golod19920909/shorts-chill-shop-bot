"""End-to-end conversational turn tests with offline AI/provider doubles."""
from __future__ import annotations

import unittest

from models.product import Marketplace, Product
from services.convsearch.contract import Outcome
from services.convsearch.engine import ConversationSearchEngine, _product_type_of
from services.convsearch.retrieval import BackendOutcome, build_retrieval_strategy
from services.convsearch.state import ConversationEngineState, MessageIntent, Slot, Turn


def listing(pid: str, title: str, price: float = 1000.0) -> Product:
    return Product(
        marketplace=Marketplace.ALIEXPRESS,
        product_id=pid,
        title=title,
        product_url=f"https://www.aliexpress.com/item/{pid}.html",
        price=price,
        currency="UAH",
    )


class FakeAI:
    def __init__(self, *turns):
        self.turns = list(turns)
        self.contexts = []

    async def decide_conversation_turn(self, text, *, context=None):
        self.contexts.append((text, context))
        return self.turns.pop(0) if self.turns else None


class FakeBackend:
    def __init__(self, products):
        self.products = products
        self.strategies = []

    async def retrieve(self, strategy, *, exclude=None):
        self.strategies.append(strategy)
        return BackendOutcome(
            ok=True,
            reason="ok" if self.products else "no_candidates",
            products=list(self.products),
            request=strategy.request,
        )


class ConversationalTurnIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_deterministic_product_type_excludes_request_verbs_and_relation_targets(self):
        self.assertEqual(
            _product_type_of("Ищу шуруповерта для дома"),
            "шуруповерта",
        )
        self.assertEqual(
            _product_type_of("Требуется аккумуляторный шуруповерт для дома"),
            "аккумуляторный шуруповерт",
        )
        self.assertEqual(
            _product_type_of("Нужна насадка на дрель по дереву"),
            "насадка",
        )
        self.assertIsNone(_product_type_of("для дома"))

    async def test_retrieval_does_not_turn_request_verbs_into_required_features(self):
        state = ConversationEngineState(user_key="request-verb-filter")
        state.record_product_type("шуруповерт")
        state.turns.append(Turn(index=0, text="Ищу шуруповерта"))

        strategy = build_retrieval_strategy(
            state, "Ищу шуруповерта", currency="UAH"
        )

        self.assertIsNotNone(strategy)
        self.assertEqual(strategy.product_type, "шуруповерт")
        self.assertEqual(strategy.request.required_features, [])
        self.assertEqual(strategy.request.normalized_query, "шуруповерт")

    async def test_budget_followup_keeps_active_product_and_is_not_searched_as_type(self):
        ai = FakeAI(
            {
                "action": "new_search",
                "confidence": 0.98,
                "product_type": "сварочный аппарат",
                "question": None,
            },
            {
                "action": "refine",
                "confidence": 0.99,
                "product_type": None,
                "question": None,
            },
        )
        backend = FakeBackend([
            listing("welder-1", "Сварочный аппарат инверторный бытовой")
        ])
        engine = ConversationSearchEngine(retrieval_backend=backend)
        engine.bind_ai_service(ai)
        state = ConversationEngineState(user_key="integration-budget")

        first = await engine.handle_message(
            "integration-budget", "Сварочный аппарат", state=state, currency="UAH"
        )
        self.assertIs(first.outcome, Outcome.RESULTS)
        self.assertEqual(state.active_by_kind("type").value, "сварочный аппарат")

        followup = "До 2000 тысяч гривен"
        second = await engine.handle_message(
            "integration-budget", followup, state=state, currency="UAH"
        )
        self.assertIs(second.outcome, Outcome.RESULTS)
        self.assertEqual(backend.strategies[-1].product_type, "сварочный аппарат")
        self.assertEqual(
            backend.strategies[-1].request.normalized_query, "сварочный аппарат"
        )
        self.assertEqual(backend.strategies[-1].request.max_price, 2_000_000.0)
        self.assertFalse(backend.strategies[-1].request.required_features)
        self.assertEqual(ai.contexts[-1][1]["active_slots"][0]["value"], "сварочный аппарат")

    async def test_gemini_clarify_asks_question_without_provider_call(self):
        ai = FakeAI({
            "action": "clarify",
            "confidence": 0.96,
            "product_type": None,
            "question": "Что именно хотите подобрать?",
        })
        backend = FakeBackend([listing("x", "Любой товар")])
        engine = ConversationSearchEngine(retrieval_backend=backend)
        engine.bind_ai_service(ai)

        state = ConversationEngineState(user_key="clarify-user")
        decision = await engine.handle_message(
            "clarify-user", "Что-нибудь полезное", state=state
        )

        self.assertIs(decision.outcome, Outcome.CLARIFY)
        self.assertEqual(decision.question, "Что именно хотите подобрать?")
        self.assertEqual(state.last_outcome, Outcome.CLARIFY.value)
        self.assertEqual(state.last_question, "Что именно хотите подобрать?")
        self.assertEqual(backend.strategies, [])

    async def test_clarification_answer_keeps_type_when_gemini_calls_it_refinement(self):
        ai = FakeAI(
            {
                "action": "clarify",
                "confidence": 0.96,
                "product_type": None,
                "question": "Какой товар вас интересует?",
            },
            {
                "action": "refine",
                "confidence": 0.98,
                "product_type": None,
                "question": None,
            },
        )
        backend = FakeBackend([
            listing("vacuum-home", "Пылесос бытовой для дома")
        ])
        engine = ConversationSearchEngine(retrieval_backend=backend)
        engine.bind_ai_service(ai)
        state = ConversationEngineState(user_key="clarification-refine")

        first = await engine.handle_message(
            "clarification-refine", "Что-нибудь полезное до 2000 гривен",
            state=state, currency="UAH",
        )
        self.assertIs(first.outcome, Outcome.CLARIFY)

        second = await engine.handle_message(
            "clarification-refine", "Пылесос для дома",
            state=state, currency="UAH",
        )

        self.assertIs(second.outcome, Outcome.RESULTS)
        self.assertEqual(state.active_by_kind("type").value, "пылесос")
        self.assertEqual(state.active_by_kind("budget").value, "2000.0 UAH")
        self.assertEqual(backend.strategies[-1].product_type, "пылесос")

    async def test_clarification_answer_recovers_product_type_without_gemini(self):
        ai = FakeAI(
            {
                "action": "clarify",
                "confidence": 0.96,
                "product_type": None,
                "question": "Какой товар вас интересует?",
            },
            None,
        )
        backend = FakeBackend([
            listing("vacuum-home", "Пылесос бытовой для дома")
        ])
        engine = ConversationSearchEngine(retrieval_backend=backend)
        engine.bind_ai_service(ai)
        state = ConversationEngineState(user_key="clarification-no-ai")

        first = await engine.handle_message(
            "clarification-no-ai", "Что-нибудь полезное до 2000 гривен",
            state=state, currency="UAH",
        )
        self.assertIs(first.outcome, Outcome.CLARIFY)

        second = await engine.handle_message(
            "clarification-no-ai", "Пылесос для дома",
            state=state, currency="UAH",
        )

        self.assertIs(second.outcome, Outcome.RESULTS)
        self.assertEqual(state.active_by_kind("type").value, "пылесос")
        self.assertEqual(state.active_by_kind("budget").value, "2000.0 UAH")
        self.assertEqual(backend.strategies[-1].product_type, "пылесос")

    async def test_clarification_answer_receives_pending_question_context(self):
        question = "Какой товар вас интересует?"
        ai = FakeAI(
            {
                "action": "clarify",
                "confidence": 0.96,
                "product_type": None,
                "question": question,
            },
            {
                "action": "new_search",
                "confidence": 0.99,
                "product_type": "пылесос",
                "question": None,
            },
        )
        backend = FakeBackend([listing("vacuum-1", "Пылесос бытовой для дома")])
        engine = ConversationSearchEngine(retrieval_backend=backend)
        engine.bind_ai_service(ai)
        state = ConversationEngineState(user_key="clarification-answer")

        first_text = "Что-нибудь полезное до 2000 гривен"
        first = await engine.handle_message(
            "clarification-answer", first_text, state=state, currency="UAH"
        )
        self.assertIs(first.outcome, Outcome.CLARIFY)

        answer = "Пылесос для дома"
        second = await engine.handle_message(
            "clarification-answer", answer, state=state, currency="UAH"
        )

        self.assertIs(second.outcome, Outcome.RESULTS)
        self.assertEqual(ai.contexts[1][1]["last_outcome"], Outcome.CLARIFY.value)
        self.assertEqual(ai.contexts[1][1]["pending_question"], question)
        self.assertEqual(ai.contexts[1][1]["recent_turns"], [first_text])
        self.assertEqual([turn.text for turn in state.turns], [first_text, answer])
        self.assertEqual(state.active_by_kind("type").value, "пылесос")
        self.assertEqual(state.active_by_kind("budget").value, "2000.0 UAH")
        self.assertEqual(backend.strategies[-1].product_type, "пылесос")
        self.assertEqual(backend.strategies[-1].request.max_price, 2000.0)
        self.assertIn("дома", backend.strategies[-1].request.normalized_query)
        self.assertIsNone(state.last_question)

    async def test_clarification_answer_can_explicitly_switch_existing_product(self):
        ai = FakeAI(
            {"action": "new_search", "confidence": 0.99,
             "product_type": "шуруповёрт", "question": None},
            {"action": "new_search", "confidence": 0.99,
             "product_type": "пылесос", "question": None},
        )
        backend = FakeBackend([
            listing("screwdriver-1", "Шуруповёрт аккумуляторный"),
            listing("vacuum-1", "Пылесос бытовой"),
        ])
        engine = ConversationSearchEngine(retrieval_backend=backend)
        engine.bind_ai_service(ai)
        state = ConversationEngineState(user_key="clarification-product-switch")

        first = await engine.handle_message(
            "clarification-product-switch",
            "Нужен шуруповёрт для дома до 2000 гривен",
            state=state, currency="UAH",
        )
        self.assertIs(first.outcome, Outcome.RESULTS)
        state.last_outcome = Outcome.CLARIFY.value
        state.last_question = "Всё-таки нужен шуруповёрт или другой товар?"

        second = await engine.handle_message(
            "clarification-product-switch",
            "На самом деле нужен пылесос",
            state=state, currency="UAH",
        )

        self.assertIs(second.outcome, Outcome.RESULTS)
        self.assertEqual(second.report["intent"], MessageIntent.NEW_SEARCH)
        self.assertEqual(state.active_values("type"), ["пылесос"])
        self.assertEqual(state.active_values("use_case"), [])
        self.assertEqual(state.active_values("budget"), [])
        self.assertEqual(backend.strategies[-1].product_type, "пылесос")
        self.assertIsNone(backend.strategies[-1].request.max_price)

    async def test_clarification_answer_for_same_product_preserves_existing_constraints(self):
        ai = FakeAI(
            {"action": "new_search", "confidence": 0.99,
             "product_type": "шуруповёрт", "question": None},
            {"action": "new_search", "confidence": 0.99,
             "product_type": "шуруповёрт", "question": None},
        )
        backend = FakeBackend([
            listing("screwdriver-1", "Шуруповёрт аккумуляторный для дома"),
        ])
        engine = ConversationSearchEngine(retrieval_backend=backend)
        engine.bind_ai_service(ai)
        state = ConversationEngineState(user_key="clarification-same-product")

        first = await engine.handle_message(
            "clarification-same-product",
            "Нужен шуруповёрт для дома до 2000 гривен",
            state=state, currency="UAH",
        )
        self.assertIs(first.outcome, Outcome.RESULTS)
        state.last_outcome = Outcome.CLARIFY.value
        state.last_question = "Для каких задач нужен шуруповёрт?"

        second = await engine.handle_message(
            "clarification-same-product",
            "Для сборки мебели",
            state=state, currency="UAH",
        )

        self.assertIs(second.outcome, Outcome.RESULTS)
        self.assertEqual(state.active_values("type"), ["шуруповёрт"])
        self.assertEqual(state.active_by_kind("budget").value, "2000.0 UAH")
        self.assertEqual(backend.strategies[-1].product_type, "шуруповёрт")
        self.assertEqual(backend.strategies[-1].request.max_price, 2000.0)

    async def test_clarification_answer_with_only_use_case_does_not_search_itself(self):
        ai = FakeAI(
            {
                "action": "clarify",
                "confidence": 0.96,
                "product_type": None,
                "question": "Какой товар вас интересует?",
            },
            {
                "action": "refine",
                "confidence": 0.99,
                "product_type": None,
                "question": None,
            },
        )
        backend = FakeBackend([listing("home-1", "Товар для дома")])
        engine = ConversationSearchEngine(retrieval_backend=backend)
        engine.bind_ai_service(ai)
        state = ConversationEngineState(user_key="clarification-needs-product")

        first = await engine.handle_message(
            "clarification-needs-product", "Что-нибудь полезное",
            state=state, currency="UAH",
        )
        self.assertIs(first.outcome, Outcome.CLARIFY)

        second = await engine.handle_message(
            "clarification-needs-product", "Для дома",
            state=state, currency="UAH",
        )

        self.assertIs(second.outcome, Outcome.CLARIFY)
        self.assertIsNone(state.active_by_kind("type"))
        self.assertEqual(backend.strategies, [])

    async def test_use_case_followup_is_soft_when_gemini_is_unavailable(self):
        ai = FakeAI(None, None)
        backend = FakeBackend([
            listing("household", "Сварочный аппарат инверторный бытовой")
        ])
        engine = ConversationSearchEngine(retrieval_backend=backend)
        engine.bind_ai_service(ai)
        state = ConversationEngineState(user_key="soft-use-case-fallback")

        first = await engine.handle_message(
            "soft-use-case-fallback", "Сварочный аппарат",
            state=state, currency="UAH",
        )
        self.assertIs(first.outcome, Outcome.RESULTS)

        second = await engine.handle_message(
            "soft-use-case-fallback", "Для дома",
            state=state, currency="UAH",
        )

        self.assertIs(second.outcome, Outcome.RESULTS)
        self.assertEqual(state.active_values("use_case"), ["дома"])
        self.assertFalse(state.active_values("feature"))
        self.assertIn("дома", backend.strategies[-1].request.normalized_query)
        self.assertFalse(backend.strategies[-1].request.required_features)
        self.assertEqual([item.product_id for item in second.products], ["household"])

    async def test_multi_turn_exclusion_keeps_product_type_and_required_features(self):
        ai = FakeAI(
            {
                "action": "new_search",
                "confidence": 0.99,
                "product_type": "сварочный аппарат",
                "question": None,
            },
            {
                "action": "refine",
                "confidence": 0.99,
                "product_type": None,
                "question": None,
            },
            {
                "action": "refine",
                "confidence": 0.99,
                "product_type": None,
                "question": None,
            },
            {
                # A model may call an additive negative constraint a correction.
                "action": "correction",
                "confidence": 0.99,
                "product_type": None,
                "question": None,
            },
        )
        backend = FakeBackend([
            listing("good", "Сварочный аппарат инверторный для дома с двумя аккумуляторами"),
            # Suitable household wording without the literal «дом» must not
            # be rejected as though use-case context were a mandatory feature.
            listing("household", "Сварочный аппарат бытовой с двумя аккумуляторами"),
            listing("forbidden", "Сварочный полуавтомат для дома с двумя аккумуляторами"),
            listing("missing-feature", "Сварочный аппарат инверторный для дома с одним аккумулятором"),
        ])
        engine = ConversationSearchEngine(retrieval_backend=backend)
        engine.bind_ai_service(ai)
        state = ConversationEngineState(user_key="multi-turn-exclusion")

        first = await engine.handle_message(
            "multi-turn-exclusion", "Сварочный аппарат", state=state, currency="UAH"
        )
        self.assertIs(first.outcome, Outcome.RESULTS)

        second = await engine.handle_message(
            "multi-turn-exclusion", "Для дома", state=state, currency="UAH"
        )
        self.assertIs(second.outcome, Outcome.RESULTS)

        third = await engine.handle_message(
            "multi-turn-exclusion", "С двумя аккумуляторами",
            state=state, currency="UAH",
        )
        self.assertIs(third.outcome, Outcome.RESULTS)

        fourth = await engine.handle_message(
            "multi-turn-exclusion", "Только не полуавтомат",
            state=state, currency="UAH",
        )

        self.assertIs(fourth.outcome, Outcome.RESULTS)
        self.assertEqual(state.active_by_kind("type").value, "сварочный аппарат")
        self.assertTrue(any("дом" in value for value in state.active_values("use_case")))
        self.assertFalse(any("дом" in value for value in state.active_values("feature")))
        self.assertTrue(any("аккумулятор" in value for value in state.active_values("feature")))
        self.assertTrue(any("полуавтомат" in value for value in state.active_values("exclusion")))
        self.assertEqual(
            {item.product_id for item in fourth.products},
            {"good", "household"},
        )

    async def test_explicit_product_switch_after_reversal_resets_old_constraints_and_shown_ids(self):
        ai = FakeAI(
            {
                "action": "new_search",
                "confidence": 0.99,
                "product_type": "шуруповёрт",
                "question": None,
            },
            {
                "action": "new_search",
                "confidence": 0.99,
                "product_type": "пылесос",
                "question": None,
            },
        )
        backend = FakeBackend([
            listing("screwdriver-1", "Шуруповёрт аккумуляторный"),
            listing("vacuum-1", "Пылесос бытовой"),
        ])
        engine = ConversationSearchEngine(retrieval_backend=backend)
        engine.bind_ai_service(ai)
        state = ConversationEngineState(user_key="product-switch-reset")

        first = await engine.handle_message(
            "product-switch-reset",
            "Нужен шуруповёрт для дома с двумя аккумуляторами до 2000 гривен, не ударный",
            state=state,
            currency="UAH",
        )
        self.assertIs(first.outcome, Outcome.RESULTS)
        self.assertEqual(state.active_by_kind("type").value, "шуруповёрт")
        self.assertTrue(state.active_values("use_case"))
        self.assertTrue(state.active_values("feature") or state.active_values("exclusion"))
        self.assertEqual(state.shown_product_ids, ["screwdriver-1"])

        # «На самом деле» is a reversal marker, but Gemini has explicitly
        # identified a different product. This must start a clean request.
        second = await engine.handle_message(
            "product-switch-reset",
            "На самом деле нужен пылесос",
            state=state,
            currency="UAH",
        )

        self.assertIs(second.outcome, Outcome.RESULTS)
        self.assertEqual(state.active_values("type"), ["пылесос"])
        self.assertEqual(state.active_values("use_case"), [])
        self.assertEqual(state.active_values("feature"), [])
        self.assertEqual(state.active_values("exclusion"), [])
        self.assertEqual(state.active_values("budget"), [])
        self.assertEqual(state.shown_product_ids, ["vacuum-1"])
        self.assertEqual(backend.strategies[-1].product_type, "пылесос")
        self.assertIsNone(backend.strategies[-1].request.max_price)
        self.assertEqual(backend.strategies[-1].request.required_features, [])
        self.assertEqual(backend.strategies[-1].request.excluded_features, [])

    async def test_explicit_new_product_request_switches_context_despite_use_case_connectors(self):
        class SequencedBackend:
            def __init__(self):
                self.products = [
                    [listing("tweezer-1", "Пинцет для наращивания ресниц")],
                    [listing("drill-attachment-1", "Насадка на дрель для обработки дерева")],
                ]
                self.strategies = []

            async def retrieve(self, strategy, *, exclude=None):
                index = len(self.strategies)
                self.strategies.append(strategy)
                products = self.products[index]
                return BackendOutcome(
                    ok=True,
                    reason="ok" if products else "no_candidates",
                    products=products,
                    request=strategy.request,
                )

        # Gemini incorrectly labels the second, explicitly new product request
        # as a refinement. The user's request construction must win.
        ai = FakeAI(
            {"action": "new_search", "confidence": 0.99,
             "product_type": "пинцет", "question": None},
            {"action": "refine", "confidence": 0.99,
             "product_type": None, "question": None,
             "slot_updates": [
                 {"kind": "feature", "operation": "add",
                  "value": "нержавеющая сталь"},
             ]},
        )
        backend = SequencedBackend()
        engine = ConversationSearchEngine(retrieval_backend=backend)
        engine.bind_ai_service(ai)
        state = ConversationEngineState(user_key="explicit-product-switch")

        first = await engine.handle_message(
            "explicit-product-switch", "Пинцет для наращивания ресниц",
            state=state, currency="UAH",
        )
        self.assertIs(first.outcome, Outcome.RESULTS)
        self.assertEqual(state.active_values("type"), ["пинцет"])

        second = await engine.handle_message(
            "explicit-product-switch",
            "А теперь мне нужна насадка на дрель для дерева",
            state=state, currency="UAH",
        )

        self.assertIs(second.outcome, Outcome.RESULTS)
        self.assertEqual(second.report["intent"], MessageIntent.NEW_SEARCH)
        self.assertEqual(state.active_values("type"), ["насадка"])
        self.assertEqual(state.active_values("compatibility"), ["дрель"])
        self.assertEqual(state.active_values("use_case"), ["дерева"])
        self.assertEqual(state.active_values("feature"), [])
        self.assertEqual(
            backend.strategies[-1].product_type,
            "насадка",
        )
        self.assertEqual(
            backend.strategies[-1].request.normalized_query,
            "насадка дрель дерева",
        )
        self.assertEqual(
            [item.product_id for item in second.products],
            ["drill-attachment-1"],
        )
        self.assertNotIn("наращивания ресниц", state.active_values("use_case"))
        self.assertEqual(state.active_values("compatibility"), ["дрель"])

    async def test_model_compatibility_slot_prevents_raw_followup_words_becoming_features(self):
        state = ConversationEngineState(user_key="compatibility-query")
        state.record_product_type("насадка")
        state.turns.append(Turn(index=0, text="подходит дрели"))
        state.slots.append(Slot(
            kind="compatibility",
            value="дрели",
            source_turn=1,
            raw_text="",
        ))

        strategy = build_retrieval_strategy(
            state, "подходит дрели", currency="UAH"
        )

        self.assertIsNotNone(strategy)
        self.assertIn("дрели", strategy.request.normalized_query)
        self.assertNotIn("подходит", strategy.request.required_features)

    async def test_model_remove_operations_do_not_readd_deterministic_extractions(self):
        cases = [
            ("use_case", "дома", "use_cases"),
            ("compatibility", "дрели", "compatibilities"),
            ("feature", "IP67", "spans"),
            ("preference", "полегче", "preferences"),
            ("exclusion", "ударный", "negations"),
        ]
        for kind, value, extracted_key in cases:
            with self.subTest(kind=kind):
                state = ConversationEngineState(user_key=f"remove-{kind}")
                state.slots.append(Slot(
                    kind=kind,
                    value=value,
                    source_turn=1,
                    raw_text="",
                ))
                state.turns.append(Turn(index=0, text=f"Убери {value}"))
                observation = {
                    "intent": MessageIntent.CORRECTION,
                    "model_slot_updates": [{
                        "operation": "remove",
                        "kind": kind,
                        "target": value,
                        "value": None,
                    }],
                    "use_cases": [],
                    "compatibilities": [],
                    "spans": [],
                    "preferences": [],
                    "negations": [],
                    "budget": {},
                }
                observation[extracted_key] = [value]

                state.record_slots(observation)

                self.assertEqual(state.active_values(kind), [])

    async def test_model_compatibility_does_not_duplicate_soft_use_case(self):
        state = ConversationEngineState(user_key="compatibility-followup")
        state.record_product_type("насадка")

        updates = [{
            "operation": "add",
            "kind": "compatibility",
            "target": None,
            "value": "дрели",
        }]
        observation = state.observe(
            "для дрели",
            intent_override=MessageIntent.REFINE,
            slot_updates=updates,
        )
        # The engine attaches only AIService-validated updates to the
        # observation after the deterministic state transition.
        observation["model_slot_updates"] = updates
        state.record_slots(observation)

        self.assertEqual(state.active_values("compatibility"), ["дрели"])
        self.assertEqual(state.active_values("use_case"), [])

    async def test_ambiguous_for_phrase_stays_soft_without_semantic_confirmation(self):
        backend = FakeBackend([
            listing("attachment-for-wood", "Насадка для обработки дерева"),
        ])
        engine = ConversationSearchEngine(retrieval_backend=backend)
        state = ConversationEngineState(user_key="fitment-for-soft")

        decision = await engine.handle_message(
            "fitment-for-soft", "Нужна насадка для дерева",
            state=state, currency="UAH",
        )

        self.assertIs(decision.outcome, Outcome.RESULTS)
        self.assertEqual(state.active_values("compatibility"), [])
        self.assertEqual(state.active_values("use_case"), ["дерева"])
        self.assertNotIn("дерева", backend.strategies[-1].request.required_features)

    async def test_explicit_item_head_wins_over_model_relation_target(self):
        ai = FakeAI({
            "action": "new_search",
            "confidence": 0.99,
            # Deliberately wrong: «дрель» is the compatibility target, not
            # the item the user asked the bot to find.
            "product_type": "дрель",
            "question": None,
        })
        backend = FakeBackend([
            listing("attachment", "Насадка для дрели по дереву"),
        ])
        engine = ConversationSearchEngine(retrieval_backend=backend)
        engine.bind_ai_service(ai)
        state = ConversationEngineState(user_key="item-head-authority")

        decision = await engine.handle_message(
            "item-head-authority", "Нужна насадка на дрель по дереву",
            state=state, currency="UAH",
        )

        self.assertIs(decision.outcome, Outcome.RESULTS)
        self.assertEqual(state.active_values("type"), ["насадка"])
        self.assertEqual(state.active_values("compatibility"), ["дрель"])
        self.assertEqual(backend.strategies[-1].product_type, "насадка")

    async def test_accessory_fitment_is_mandatory_and_missing_evidence_is_quarantined(self):
        backend = FakeBackend([
            listing("unknown-fitment", "Насадка универсальная для обработки"),
        ])
        engine = ConversationSearchEngine(retrieval_backend=backend)
        state = ConversationEngineState(user_key="fitment-required")

        decision = await engine.handle_message(
            "fitment-required", "Нужна насадка на дрель по дереву",
            state=state, currency="UAH",
        )

        self.assertIs(decision.outcome, Outcome.NO_ELIGIBLE)
        self.assertEqual(decision.products, [])
        self.assertEqual(state.active_values("compatibility"), ["дрель"])
        self.assertIn("дрель", backend.strategies[-1].request.normalized_query)
        self.assertIn("дрель", backend.strategies[-1].request.required_features)
        self.assertEqual(decision.report["reason"], "unverified_required_features")

    async def test_work_material_context_after_po_is_preserved_without_ai(self):
        # «по X» is a productive Russian application/material construction,
        # not a new product type and not a hard literal title requirement.
        for material in ("дереву", "металлу", "бетону"):
            with self.subTest(material=material):
                backend = FakeBackend([
                    listing("attachment", "Насадка для дрели универсальная")
                ])
                engine = ConversationSearchEngine(retrieval_backend=backend)
                state = ConversationEngineState(user_key=f"work-{material}")
                decision = await engine.handle_message(
                    f"work-{material}",
                    f"Нужна насадка на дрель по {material}",
                    state=state, currency="UAH",
                )
                self.assertIs(decision.outcome, Outcome.RESULTS)
                self.assertEqual(state.active_values("type"), ["насадка"])
                self.assertEqual(state.active_values("compatibility"), ["дрель"])
                self.assertEqual(state.active_values("use_case"), [material])
                self.assertIn("насадка дрель", backend.strategies[-1].request.normalized_query)
                self.assertIn(material, backend.strategies[-1].request.normalized_query)
                self.assertEqual(state.active_values("feature"), [])

    async def test_material_followup_cannot_become_a_new_product_type(self):
        ai = FakeAI(
            {"action": "new_search", "confidence": 0.99,
             "product_type": "пинцет", "question": None},
            # Simulate the live failure: Gemini mistakes the material for a
            # product and proposes it as the new product_type.
            {"action": "new_search", "confidence": 0.99,
             "product_type": "нержавеющая сталь", "question": None},
        )
        backend = FakeBackend([
            listing(
                "tweezer-1",
                "Пинцет для наращивания ресниц из нержавеющей стали",
            ),
        ])
        engine = ConversationSearchEngine(retrieval_backend=backend)
        engine.bind_ai_service(ai)
        state = ConversationEngineState(user_key="material-followup")

        first = await engine.handle_message(
            "material-followup", "Пинцет для наращивания ресниц",
            state=state, currency="UAH",
        )
        self.assertIs(first.outcome, Outcome.RESULTS)
        self.assertEqual(state.active_by_kind("type").value, "пинцет")

        second = await engine.handle_message(
            "material-followup", "Оставь варианты из нержавеющей стали",
            state=state, currency="UAH",
        )

        self.assertIs(second.outcome, Outcome.RESULTS)
        self.assertEqual(second.report["intent"], MessageIntent.REFINE)
        self.assertEqual(state.active_by_kind("type").value, "пинцет")
        self.assertEqual(
            state.active_values("feature"), ["нержавеющей стали"]
        )
        self.assertEqual(backend.strategies[-1].product_type, "пинцет")
        self.assertIn("нержавеющей", backend.strategies[-1].request.normalized_query)

    async def test_gemini_refine_cannot_override_a_message_naming_a_new_product(self):
        ai = FakeAI(
            {"action": "new_search", "confidence": 0.99,
             "product_type": "шуруповёрт", "question": None},
            {"action": "refine", "confidence": 0.99,
             "product_type": None, "question": None},
        )
        backend = FakeBackend([
            listing("screwdriver-1", "Шуруповёрт аккумуляторный"),
            listing("vacuum-1", "Пылесос бытовой"),
        ])
        engine = ConversationSearchEngine(retrieval_backend=backend)
        engine.bind_ai_service(ai)
        state = ConversationEngineState(user_key="ai-refine-new-product")

        await engine.handle_message(
            "ai-refine-new-product", "Нужен шуруповёрт",
            state=state, currency="UAH",
        )
        second = await engine.handle_message(
            "ai-refine-new-product", "Расскажи про пылесос",
            state=state, currency="UAH",
        )

        self.assertIs(second.outcome, Outcome.RESULTS)
        self.assertEqual(second.report["intent"], MessageIntent.NEW_SEARCH)
        self.assertEqual(backend.strategies[-1].product_type, "пылесос")
        self.assertEqual(state.active_values("type"), ["пылесос"])

    async def test_gemini_refine_cannot_override_question_about_shown_products(self):
        ai = FakeAI(
            {"action": "new_search", "confidence": 0.99,
             "product_type": "шуруповёрт", "question": None},
            {"action": "refine", "confidence": 0.99,
             "product_type": None, "question": None},
        )
        backend = FakeBackend([
            listing("screwdriver-1", "Шуруповёрт аккумуляторный"),
        ])
        engine = ConversationSearchEngine(retrieval_backend=backend)
        engine.bind_ai_service(ai)
        state = ConversationEngineState(user_key="ai-refine-shown-question")

        await engine.handle_message(
            "ai-refine-shown-question", "Нужен шуруповёрт",
            state=state, currency="UAH",
        )
        strategy_count = len(backend.strategies)
        second = await engine.handle_message(
            "ai-refine-shown-question", "Какой из них лучше?",
            state=state, currency="UAH",
        )

        self.assertIs(second.outcome, Outcome.COMPARE)
        self.assertEqual(len(backend.strategies), strategy_count)
        self.assertEqual(state.active_values("type"), ["шуруповёрт"])

    async def test_clarify_for_new_product_keeps_type_for_followup_answer(self):
        ai = FakeAI(
            {"action": "clarify", "confidence": 0.99,
             "product_type": None, "question": "Для каких задач нужен пылесос?"},
            {"action": "refine", "confidence": 0.99,
             "product_type": None, "question": None},
        )
        backend = FakeBackend([
            listing("vacuum-1", "Пылесос бытовой для дома"),
        ])
        engine = ConversationSearchEngine(retrieval_backend=backend)
        engine.bind_ai_service(ai)
        state = ConversationEngineState(user_key="clarify-new-product-type")

        first = await engine.handle_message(
            "clarify-new-product-type", "Расскажи про пылесос",
            state=state, currency="UAH",
        )
        self.assertIs(first.outcome, Outcome.CLARIFY)
        self.assertEqual(state.active_values("type"), ["пылесос"])

        second = await engine.handle_message(
            "clarify-new-product-type", "Для дома",
            state=state, currency="UAH",
        )
        self.assertIs(second.outcome, Outcome.RESULTS)
        self.assertEqual(state.active_values("type"), ["пылесос"])
        self.assertEqual(backend.strategies[-1].product_type, "пылесос")

    async def test_gemini_compare_cannot_override_a_message_naming_a_new_product(self):
        ai = FakeAI(
            {"action": "new_search", "confidence": 0.99,
             "product_type": "шуруповёрт", "question": None},
            {"action": "compare", "confidence": 0.99,
             "product_type": None, "question": None},
        )
        backend = FakeBackend([
            listing("screwdriver-1", "Шуруповёрт аккумуляторный"),
            listing("vacuum-1", "Пылесос бытовой"),
        ])
        engine = ConversationSearchEngine(retrieval_backend=backend)
        engine.bind_ai_service(ai)
        state = ConversationEngineState(user_key="ai-compare-new-product")

        first = await engine.handle_message(
            "ai-compare-new-product", "Нужен шуруповёрт",
            state=state, currency="UAH",
        )
        self.assertIs(first.outcome, Outcome.RESULTS)

        second = await engine.handle_message(
            "ai-compare-new-product", "Расскажи про пылесос",
            state=state, currency="UAH",
        )

        self.assertIs(second.outcome, Outcome.RESULTS)
        self.assertEqual(backend.strategies[-1].product_type, "пылесос")
        self.assertEqual(state.active_values("type"), ["пылесос"])

    async def test_gemini_new_search_cannot_override_question_about_shown_products(self):
        ai = FakeAI(
            {"action": "new_search", "confidence": 0.99,
             "product_type": "шуруповёрт", "question": None},
            {"action": "new_search", "confidence": 0.99,
             "product_type": "шуруповёрт", "question": None},
        )
        backend = FakeBackend([
            listing("screwdriver-1", "Шуруповёрт аккумуляторный"),
        ])
        engine = ConversationSearchEngine(retrieval_backend=backend)
        engine.bind_ai_service(ai)
        state = ConversationEngineState(user_key="ai-new-search-shown-question")

        first = await engine.handle_message(
            "ai-new-search-shown-question", "Нужен шуруповёрт",
            state=state, currency="UAH",
        )
        self.assertIs(first.outcome, Outcome.RESULTS)
        strategy_count = len(backend.strategies)

        second = await engine.handle_message(
            "ai-new-search-shown-question", "Какой из них лучше?",
            state=state, currency="UAH",
        )

        self.assertIs(second.outcome, Outcome.COMPARE)
        self.assertEqual(len(backend.strategies), strategy_count)
        self.assertEqual(state.active_values("type"), ["шуруповёрт"])

    async def test_grounded_gemini_slots_are_added_to_the_authoritative_ledger(self):
        ai = FakeAI(
            {
                "action": "new_search",
                "confidence": 0.99,
                "product_type": "фонарь",
                "question": None,
                "slot_updates": [],
            },
            {
                "action": "refine",
                "confidence": 0.99,
                "product_type": None,
                "question": None,
                "slot_updates": [
                    {"kind": "use_case", "value": "на стройке"},
                    {"kind": "feature", "value": "защита IP67"},
                ],
            },
        )
        backend = FakeBackend([
            listing("worklight-ip67", "Фонарь с защитой IP67"),
        ])
        engine = ConversationSearchEngine(retrieval_backend=backend)
        engine.bind_ai_service(ai)
        state = ConversationEngineState(user_key="grounded-slots")

        first = await engine.handle_message(
            "grounded-slots", "Нужен фонарь",
            state=state, currency="UAH",
        )
        self.assertIs(first.outcome, Outcome.RESULTS)

        second = await engine.handle_message(
            "grounded-slots",
            "Буду работать на стройке, нужна защита IP67",
            state=state, currency="UAH",
        )

        # This regression isolates slot interpretation and persistence;
        # candidate eligibility is covered by the separate product-type and
        # feature-evidence suites.
        self.assertIsNotNone(second.request)
        self.assertEqual(state.active_values("use_case"), ["на стройке"])
        self.assertEqual(state.active_values("feature"), ["защита IP67"])
        self.assertIn("стройке", backend.strategies[-1].request.normalized_query)
        self.assertEqual(
            backend.strategies[-1].request.required_features,
            ["защита IP67"],
        )

    async def test_provider_failure_preserves_pending_clarification_in_ledger(self):
        class FailingBackend:
            async def retrieve(self, strategy, *, exclude=None):
                return BackendOutcome(
                    ok=False,
                    reason="provider_error",
                    request=strategy.request,
                )

        ai = FakeAI(
            {
                "action": "new_search",
                "confidence": 0.99,
                "product_type": "пылесос",
                "question": None,
            },
            {
                "action": "new_search",
                "confidence": 0.99,
                "product_type": "пылесос",
                "question": None,
            },
        )
        engine = ConversationSearchEngine(retrieval_backend=FailingBackend())
        engine.bind_ai_service(ai)
        state = ConversationEngineState(user_key="provider-failure-clarification")
        observation = state.observe("до 2000 гривен")
        state.record_slots(observation)
        state.last_outcome = Outcome.CLARIFY.value
        state.last_question = "Какой товар вас интересует?"

        first = await engine.handle_message(
            "provider-failure-clarification",
            "Пылесос для дома",
            state=state,
            currency="UAH",
        )
        self.assertIs(first.outcome, Outcome.PROVIDER_ERROR)
        self.assertEqual(state.last_outcome, Outcome.PROVIDER_ERROR.value)
        self.assertEqual(state.last_question, "Какой товар вас интересует?")

        # The user retries the same answer after the provider outage. The
        # retained question, not last_outcome, must keep this in the same
        # clarification transaction instead of resetting its ledger.
        retry = await engine.handle_message(
            "provider-failure-clarification",
            "Пылесос для дома",
            state=state,
            currency="UAH",
        )

        self.assertIs(retry.outcome, Outcome.PROVIDER_ERROR)
        self.assertEqual(ai.contexts[1][1]["pending_question"], "Какой товар вас интересует?")
        self.assertEqual(state.active_by_kind("type").value, "пылесос")
        self.assertEqual(state.active_by_kind("budget").value, "2000.0 UAH")
        self.assertEqual(state.last_question, "Какой товар вас интересует?")

    async def test_semantic_selector_removes_wrong_product_from_results(self):
        class SelectingAI(FakeAI):
            async def select_conversation_candidates(self, candidates, *, query, wanted=20):
                self.selection_input = [item.product_id for item in candidates]
                self.selection_query = query
                self.selection_wanted = wanted
                return ["welder-1"]

        ai = SelectingAI(None)
        backend = FakeBackend([
            listing("mask-1", "Сварочная маска автоматическая"),
            listing("welder-1", "Сварочный аппарат инверторный бытовой"),
        ])
        engine = ConversationSearchEngine(retrieval_backend=backend)
        engine.bind_ai_service(ai)

        decision = await engine.handle_message(
            "semantic-user", "Сварочный аппарат",
            state=ConversationEngineState(user_key="semantic-user"), currency="UAH"
        )

        self.assertIs(decision.outcome, Outcome.RESULTS)
        self.assertEqual([item.product_id for item in decision.products], ["welder-1"])
        self.assertEqual(decision.report["ai_selection"], "selected")
        self.assertEqual(ai.selection_wanted, 2)
        self.assertIn("сварочный аппарат", ai.selection_query)

    async def test_semantic_selector_empty_result_does_not_restore_irrelevant_items(self):
        class EmptySelectingAI(FakeAI):
            async def select_conversation_candidates(self, candidates, *, query, wanted=20):
                return []

        ai = EmptySelectingAI(None)
        backend = FakeBackend([
            listing("mask-1", "Сварочная маска автоматическая"),
            listing("mask-2", "Маска сварщика защитная"),
        ])
        engine = ConversationSearchEngine(retrieval_backend=backend)
        engine.bind_ai_service(ai)

        decision = await engine.handle_message(
            "semantic-empty-user", "Сварочный аппарат",
            state=ConversationEngineState(user_key="semantic-empty-user"), currency="UAH"
        )

        self.assertIs(decision.outcome, Outcome.NO_ELIGIBLE)
        self.assertEqual(decision.products, [])
        self.assertEqual(decision.report["reason"], "semantic_no_match")

    async def test_uncertain_or_missing_ai_decision_uses_deterministic_fallback(self):
        ai = FakeAI(None)
        backend = FakeBackend([listing("w1", "Сварочный аппарат инверторный")])
        engine = ConversationSearchEngine(retrieval_backend=backend)
        engine.bind_ai_service(ai)

        decision = await engine.handle_message(
            "fallback-user", "Сварочный аппарат",
            state=ConversationEngineState(user_key="fallback-user"), currency="UAH"
        )

        self.assertIs(decision.outcome, Outcome.RESULTS)
        self.assertEqual(backend.strategies[0].product_type, "сварочный аппарат")


if __name__ == "__main__":
    unittest.main()
