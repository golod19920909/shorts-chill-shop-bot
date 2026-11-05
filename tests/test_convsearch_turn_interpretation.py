"""End-to-end conversational turn tests with offline AI/provider doubles."""
from __future__ import annotations

import unittest

from models.product import Marketplace, Product
from handlers.search import _products_actually_shown
from services.convsearch.contract import Outcome
from services.convsearch.engine import (
    ConversationSearchEngine,
    _is_different_product_type,
    _product_type_of,
)
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

    async def retrieve(self, strategy, *, exclude=None, should_continue=None):
        self.strategies.append(strategy)
        return BackendOutcome(
            ok=True,
            reason="ok" if self.products else "no_candidates",
            products=list(self.products),
            request=strategy.request,
        )


class MarketplacePhraseRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_search_passes_bounded_alternate_phrase_into_real_strategy(self):
        ai = FakeAI({
            "action": "new_search",
            "confidence": 0.99,
            "product_type": "шуруповерт",
            "marketplace_terms": ["cordless drill"],
            "question": None,
            "response": None,
            "slot_updates": [],
        })
        backend = FakeBackend([listing("drill-1", "Cordless drill 12V")])
        engine = ConversationSearchEngine(retrieval_backend=backend)
        engine.bind_ai_service(ai)
        state = ConversationEngineState(user_key="marketplace-phrase-route")

        await engine.handle_message(
            "marketplace-phrase-route",
            "Нужен аккумуляторный шуруповерт",
            state=state,
            currency="UAH",
        )

        self.assertEqual(len(backend.strategies), 1)
        self.assertEqual(
            backend.strategies[0].request.intent_marketplace_terms,
            ["cordless drill"],
        )
        self.assertEqual(
            backend.strategies[0].request.normalized_query,
            "шуруповерт аккумуляторный",
        )


    async def test_refinement_passes_alternate_phrase_with_active_feature_into_search(self):
        ai = FakeAI({
            "action": "refine",
            "confidence": 0.99,
            "product_type": None,
            "marketplace_terms": ["cordless drill with two batteries"],
            "question": None,
            "response": None,
            "slot_updates": [],
        })
        backend = FakeBackend([listing("drill-2", "Cordless drill with 2 batteries")])
        engine = ConversationSearchEngine(retrieval_backend=backend)
        engine.bind_ai_service(ai)
        state = ConversationEngineState(user_key="marketplace-refinement-route")
        state.record_product_type("аккумуляторный шуруповерт")
        state.slots.append(Slot(
            kind="feature",
            value="двумя аккумуляторами",
            source_turn=1,
            raw_text="с двумя аккумуляторами",
        ))

        await engine.handle_message(
            "marketplace-refinement-route",
            "Поищи именно с двумя аккумуляторами",
            state=state,
            currency="UAH",
        )

        self.assertEqual(len(backend.strategies), 1)
        self.assertEqual(
            backend.strategies[0].request.intent_marketplace_terms,
            ["cordless drill with two batteries"],
        )
        self.assertIn(
            "двумя аккумуляторами",
            backend.strategies[0].request.normalized_query,
        )


class ShownProductContextTests(unittest.TestCase):
    def test_only_cards_really_opened_in_telegram_are_sent_as_shown_context(self):
        products = [
            listing("p1", "Товар первый"),
            listing("p2", "Товар второй"),
            listing("p3", "Товар третий"),
        ]

        shown = _products_actually_shown(products, ["p1"])

        self.assertEqual([product.product_id for product in shown], ["p1"])

    def test_browsed_cards_are_kept_in_original_result_order_and_bounded(self):
        products = [listing(f"p{i}", f"Товар {i}") for i in range(20)]

        shown = _products_actually_shown(
            products, [f"p{i}" for i in range(1, 18)], limit=12
        )

        self.assertEqual(
            [product.product_id for product in shown],
            [f"p{i}" for i in range(6, 18)],
        )


class ConversationalTurnIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_gemini_receives_grounded_context_for_shown_products(self):
        ai = FakeAI({
            "action": "compare", "confidence": 0.99,
            "product_type": None, "question": None,
        })
        backend = FakeBackend([])
        engine = ConversationSearchEngine(retrieval_backend=backend)
        engine.bind_ai_service(ai)
        state = ConversationEngineState(user_key="shown-context")
        products = [
            listing("shown-1", "Шуруповёрт бесщёточный 20 В", 2400),
            listing("shown-2", "Шуруповёрт компактный 12 В", 1800),
        ]
        state.record_product_type("шуруповёрт")
        state.record_shown([p.product_id for p in products])

        await engine.handle_message(
            "shown-context", "Какой из них лучше?",
            state=state, currency="UAH", shown_products=products,
        )

        context = ai.contexts[0][1]
        self.assertEqual(
            context["shown_products"],
            [
                {"id": "shown-1", "title": "Шуруповёрт бесщёточный 20 В",
                 "price": 2400, "currency": "UAH"},
                {"id": "shown-2", "title": "Шуруповёрт компактный 12 В",
                 "price": 1800, "currency": "UAH"},
            ],
        )

    async def test_shown_count_matches_current_visible_snapshot_not_stale_ledger_ids(self):
        ai = FakeAI({
            "action": "compare", "confidence": 0.99,
            "product_type": None, "question": None,
        })
        engine = ConversationSearchEngine(retrieval_backend=FakeBackend([]))
        engine.bind_ai_service(ai)
        state = ConversationEngineState(user_key="shown-count")
        state.record_product_type("устройство")
        state.record_shown(["old-1", "old-2", "old-3"])

        await engine.handle_message(
            "shown-count", "Что лучше?",
            state=state, currency="UAH",
            shown_products=[listing("current-1", "Устройство первое")],
        )

        self.assertEqual(ai.contexts[0][1]["shown_product_count"], 1)
        self.assertEqual(
            [item["id"] for item in ai.contexts[0][1]["shown_products"]],
            ["current-1"],
        )

    async def test_natural_response_does_not_consume_pending_clarification(self):
        ai = FakeAI({
            "action": "respond", "confidence": 0.99,
            "response": "Рад помочь!", "product_type": None, "slot_updates": [],
        })
        backend = FakeBackend([])
        engine = ConversationSearchEngine(retrieval_backend=backend)
        engine.bind_ai_service(ai)
        state = ConversationEngineState(user_key="pending-natural-response")
        state.last_question = "Для какой задачи нужен товар?"

        decision = await engine.handle_message(
            "pending-natural-response", "Спасибо!",
            state=state, currency="UAH",
        )

        self.assertIs(decision.outcome, Outcome.RESPOND)
        self.assertEqual(state.last_question, "Для какой задачи нужен товар?")
        self.assertEqual(backend.strategies, [])

    async def test_same_grounded_product_type_cannot_reset_active_search(self):
        ai = FakeAI({
            "action": "new_search", "confidence": 0.99,
            "product_type": "устройство", "question": None,
        })
        backend = FakeBackend([])
        engine = ConversationSearchEngine(retrieval_backend=backend)
        engine.bind_ai_service(ai)
        state = ConversationEngineState(user_key="same-type-continuity")
        state.record_product_type("устройство")

        decision = await engine.handle_message(
            "same-type-continuity", "сделай устройство подешевле",
            state=state, currency="UAH",
        )

        self.assertEqual(decision.report["intent"], MessageIntent.REFINE)
        self.assertEqual(state.active_values("type"), ["устройство"])
        self.assertEqual(backend.strategies[-1].product_type, "устройство")

    async def test_product_switch_uses_head_noun_not_shared_adjective(self):
        self.assertTrue(
            _is_different_product_type("сварочный стол", "сварочный аппарат")
        )
        self.assertFalse(
            _is_different_product_type("шуруповёрт аккумуляторный", "аккумуляторный шуруповёрт")
        )

    async def test_explicit_product_switch_with_shared_adjective_starts_new_search(self):
        ai = FakeAI({
            "action": "new_search", "confidence": 0.99,
            "product_type": "сварочный стол", "question": None,
        })
        backend = FakeBackend([listing("table", "Сварочный стол усиленный")])
        engine = ConversationSearchEngine(retrieval_backend=backend)
        engine.bind_ai_service(ai)
        state = ConversationEngineState(user_key="shared-adjective-switch")
        state.record_product_type("сварочный аппарат")

        await engine.handle_message(
            "shared-adjective-switch", "Хочу сварочный стол",
            state=state, currency="UAH",
        )

        self.assertEqual(state.active_values("type"), ["сварочный стол"])
        self.assertEqual(backend.strategies[-1].product_type, "сварочный стол")
        self.assertEqual(backend.strategies[-1].request.normalized_query, "сварочный стол")

    async def test_subordinate_feature_clause_cannot_switch_product_even_if_ai_says_new_search(self):
        ai = FakeAI({
            "action": "new_search", "confidence": 0.99,
            "product_type": "два аккумулятора", "question": None,
        })
        backend = FakeBackend([
            listing("driver", "Аккумуляторный шуруповёрт 20V с двумя аккумуляторами")
        ])
        engine = ConversationSearchEngine(retrieval_backend=backend)
        engine.bind_ai_service(ai)
        state = ConversationEngineState(user_key="subordinate-feature-switch-guard")
        state.record_product_type("шуруповёрт")

        decision = await engine.handle_message(
            "subordinate-feature-switch-guard",
            "Хочу, чтобы было два аккумулятора",
            state=state, currency="UAH",
        )

        self.assertEqual(decision.report["intent"], MessageIntent.REFINE)
        self.assertEqual(state.active_values("type"), ["шуруповёрт"])
        self.assertEqual(backend.strategies[-1].product_type, "шуруповёрт")
        self.assertTrue(
            any("аккумулятор" in value for value in state.active_values("feature"))
        )

    async def test_untyped_alternatives_request_keeps_the_current_product(self):
        ai = FakeAI({
            "action": "new_search", "confidence": 0.99,
            "product_type": None, "question": None,
        })
        backend = FakeBackend([listing("alternative", "Устройство новая модель")])
        engine = ConversationSearchEngine(retrieval_backend=backend)
        engine.bind_ai_service(ai)
        state = ConversationEngineState(user_key="untyped-alternatives")
        state.record_product_type("устройство")
        state.record_shown(["previously-shown"])

        decision = await engine.handle_message(
            "untyped-alternatives", "Покажи другие варианты",
            state=state, currency="UAH",
        )

        self.assertIs(decision.outcome, Outcome.RESULTS)
        self.assertEqual(decision.report["intent"], MessageIntent.ALTERNATIVES)
        self.assertEqual(backend.strategies[-1].product_type, "устройство")

    async def test_new_search_without_grounded_identity_asks_instead_of_reusing_old_type(self):
        ai = FakeAI({
            "action": "new_search", "confidence": 0.99,
            "product_type": None, "question": None,
        })
        backend = FakeBackend([])
        engine = ConversationSearchEngine(retrieval_backend=backend)
        engine.bind_ai_service(ai)
        state = ConversationEngineState(user_key="missing-new-type")
        state.record_product_type("устройство")

        decision = await engine.handle_message(
            "missing-new-type", "найди что-нибудь другое",
            state=state, currency="UAH",
        )

        self.assertIs(decision.outcome, Outcome.CLARIFY)
        self.assertEqual(decision.report["reason"], "new_search_without_product_identity")
        self.assertEqual(state.active_values("type"), ["устройство"])
        self.assertEqual(backend.strategies, [])

    async def test_natural_response_does_not_search_or_mutate_shopping_slots(self):
        ai = FakeAI({
            "action": "respond", "confidence": 0.99,
            "response": "Люмен — единица светового потока.",
            "product_type": None, "slot_updates": [],
        })
        backend = FakeBackend([listing("unused", "Фонарик 1000 лм")])
        engine = ConversationSearchEngine(retrieval_backend=backend)
        search_started = []

        async def announce_search_start():
            search_started.append(True)
        engine.bind_ai_service(ai)
        state = ConversationEngineState(user_key="natural-response")

        decision = await engine.handle_message(
            "natural-response", "Что означает люмен?", state=state, currency="UAH",
            on_search_start=announce_search_start,
        )

        self.assertIs(decision.outcome, Outcome.RESPOND)
        self.assertEqual(decision.reply, "Люмен — единица светового потока.")
        self.assertEqual(backend.strategies, [])
        self.assertEqual(search_started, [])
        self.assertEqual([turn.text for turn in state.turns], ["Что означает люмен?"])
        self.assertEqual(state.active(), [])


    async def test_shown_product_question_cannot_take_free_form_respond_path(self):
        ai = FakeAI({
            "action": "respond", "confidence": 0.99,
            "response": "Да, он водонепроницаемый.",
            "product_type": None, "slot_updates": [],
        })
        backend = FakeBackend([])
        engine = ConversationSearchEngine(retrieval_backend=backend)
        engine.bind_ai_service(ai)
        state = ConversationEngineState(user_key="shown-product-respond-guard")
        state.record_product_type("налобный фонарь")
        state.record_shown(["headlamp-1"])

        decision = await engine.handle_message(
            "shown-product-respond-guard",
            "А он водонепроницаемый?",
            state=state,
            shown_products=[listing("headlamp-1", "LED headlamp IPX7")],
            currency="UAH",
        )

        self.assertIs(decision.outcome, Outcome.COMPARE)
        self.assertEqual(backend.strategies, [])
        self.assertEqual(decision.report["intent"], MessageIntent.COMPARE)

    async def test_general_concept_question_stays_natural_with_shown_products(self):
        ai = FakeAI({
            "action": "respond", "confidence": 0.99,
            "response": "IPX7 означает защиту от временного погружения в воду.",
            "product_type": None, "slot_updates": [],
        })
        backend = FakeBackend([])
        engine = ConversationSearchEngine(retrieval_backend=backend)
        engine.bind_ai_service(ai)
        state = ConversationEngineState(user_key="shown-product-concept")
        state.record_product_type("налобный фонарь")
        state.record_shown(["headlamp-1"])

        decision = await engine.handle_message(
            "shown-product-concept",
            "Что означает IPX7?",
            state=state,
            shown_products=[listing("headlamp-1", "LED headlamp IPX7")],
            currency="UAH",
        )

        self.assertIs(decision.outcome, Outcome.RESPOND)
        self.assertEqual(decision.reply, "IPX7 означает защиту от временного погружения в воду.")
        self.assertEqual(backend.strategies, [])


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

    async def test_modifier_only_fragment_never_starts_a_product_search(self):
        self.assertIsNone(_product_type_of("Нужен автомобильный"))
        self.assertIsNone(_product_type_of("Ищу промышленный"))
        self.assertIsNone(_product_type_of("сделай его потише"))
        self.assertIsNone(_product_type_of("Какой из них лучше?"))
        self.assertEqual(
            _product_type_of("Нужен автомобильный компрессор"),
            "автомобильный компрессор",
        )

        ai = FakeAI(None)
        backend = FakeBackend([listing("wrong", "Автомобильный кондиционер")])
        engine = ConversationSearchEngine(retrieval_backend=backend)
        engine.bind_ai_service(ai)
        state = ConversationEngineState(user_key="modifier-only-fragment")

        decision = await engine.handle_message(
            "modifier-only-fragment", "Нужен автомобильный",
            state=state, currency="UAH",
        )

        self.assertIs(decision.outcome, Outcome.CLARIFY)
        self.assertEqual(backend.strategies, [])
        self.assertIsNone(state.active_by_kind("type"))

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
        search_started = []

        async def announce_search_start():
            search_started.append(len(backend.strategies))

        first = await engine.handle_message(
            "integration-budget", "Сварочный аппарат", state=state, currency="UAH",
            on_search_start=announce_search_start,
        )
        self.assertIs(first.outcome, Outcome.RESULTS)
        self.assertEqual(search_started, [0])
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

    async def test_pending_clarification_fragment_does_not_search_when_gemini_times_out(self):
        question = "Для какой задачи нужен компрессор?"
        ai = FakeAI(
            {
                "action": "clarify",
                "confidence": 0.97,
                "product_type": None,
                "question": question,
            },
            None,
        )
        backend = FakeBackend([
            listing("wrong", "Компрессор кондиционера автомобильный")
        ])
        engine = ConversationSearchEngine(retrieval_backend=backend)
        engine.bind_ai_service(ai)
        state = ConversationEngineState(user_key="pending-fragment-timeout")

        first = await engine.handle_message(
            "pending-fragment-timeout", "Нужен компрессор для машины",
            state=state, currency="UAH",
        )
        self.assertIs(first.outcome, Outcome.CLARIFY)
        self.assertEqual(first.question, question)

        second = await engine.handle_message(
            "pending-fragment-timeout", "автомобильный",
            state=state, currency="UAH",
        )

        self.assertIs(second.outcome, Outcome.CLARIFY)
        self.assertEqual(second.question, question)
        self.assertEqual(backend.strategies, [])
        self.assertEqual(state.active_by_kind("type").value, "компрессор")

    async def test_pending_clarification_new_search_without_new_type_preserves_active_product(self):
        ai = FakeAI(
            {
                "action": "clarify",
                "confidence": 0.97,
                "product_type": None,
                "question": "Для какой задачи нужна насадка?",
            },
            {
                "action": "new_search",
                "confidence": 0.98,
                "product_type": None,
                "question": None,
            },
        )
        backend = FakeBackend([
            listing("wood-bit", "Насадка для дрели по дереву")
        ])
        engine = ConversationSearchEngine(retrieval_backend=backend)
        engine.bind_ai_service(ai)
        state = ConversationEngineState(user_key="pending-use-case-answer")

        first = await engine.handle_message(
            "pending-use-case-answer", "Нужна насадка на дрель",
            state=state, currency="UAH",
        )
        self.assertIs(first.outcome, Outcome.CLARIFY)
        self.assertEqual(state.active_by_kind("type").value, "насадка")

        second = await engine.handle_message(
            "pending-use-case-answer", "для дерева",
            state=state, currency="UAH",
        )

        self.assertIs(second.outcome, Outcome.RESULTS)
        self.assertEqual(state.active_by_kind("type").value, "насадка")
        self.assertEqual(state.active_values("compatibility"), ["дрель"])
        self.assertEqual(state.active_values("use_case"), ["дерева"])
        self.assertEqual(backend.strategies[-1].product_type, "насадка")

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
            listing("screwdriver-1", "Шуруповёрт аккумуляторный с двумя аккумуляторами"),
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
            listing("screwdriver-1", "Шуруповёрт аккумуляторный с двумя аккумуляторами"),
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
        # Engine selection is not proof that Telegram delivered the card.
        self.assertEqual(state.shown_product_ids, [])

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
        self.assertEqual(state.shown_product_ids, [])
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

            async def retrieve(self, strategy, *, exclude=None, should_continue=None):
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

    async def test_removing_positive_slot_does_not_create_negative_exclusion(self):
        state = ConversationEngineState(user_key="remove-positive")
        state.slots.append(Slot(
            kind="feature",
            value="IP67",
            source_turn=1,
            raw_text="",
        ))
        state.turns.append(Turn(index=0, text="Не нужно IP67"))
        state.record_slots({
            "intent": MessageIntent.CORRECTION,
            "model_slot_updates": [{
                "operation": "remove",
                "kind": "feature",
                "target": "IP67",
                "value": None,
            }],
            "negations": ["IP67"],
            "spans": ["IP67"],
            "use_cases": [],
            "compatibilities": [],
            "preferences": [],
            "budget": {},
        })

        self.assertEqual(state.active_values("feature"), [])
        self.assertEqual(state.active_values("exclusion"), [])

    async def test_model_added_exclusion_is_not_recreated_as_positive_feature(self):
        state = ConversationEngineState(user_key="negative-slot")
        state.turns.append(Turn(index=0, text="без ударного режима"))
        state.record_slots({
            "intent": MessageIntent.REFINE,
            "model_slot_updates": [{
                "operation": "add",
                "kind": "exclusion",
                "target": None,
                "value": "ударного режима",
            }],
            "negations": ["ударного режима"],
            "spans": ["ударного режима"],
            "use_cases": [],
            "compatibilities": [],
            "preferences": [],
            "budget": {},
        })

        self.assertEqual(state.active_values("exclusion"), ["ударного режима"])
        self.assertEqual(state.active_values("feature"), [])

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

        first = await engine.handle_message(
            "ai-refine-shown-question", "Нужен шуруповёрт",
            state=state, currency="UAH",
        )
        self.assertIs(first.outcome, Outcome.RESULTS)
        # Simulate the handler acknowledging the first card after Telegram send.
        state.record_shown([first.products[0].product_id])
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
        # Simulate successful delivery of the first card by the Telegram handler.
        state.record_shown([first.products[0].product_id])
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
            async def retrieve(self, strategy, *, exclude=None, should_continue=None):
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
            async def select_conversation_candidates(self, candidates, *, query, wanted=20, required_features=None, identity_review_ids=None):
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
            async def select_conversation_candidates(self, candidates, *, query, wanted=20, required_features=None, identity_review_ids=None):
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
