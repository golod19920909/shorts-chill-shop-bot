"""Tests for the conversational search engine (deterministic fixtures).

The engine is tested with a fixed ``Product`` pool — no network, no live
Gemini, no live AliExpress. Every scenario of the integration brief maps to
a pool of REAL ``Product`` rows (the bot's own schema) plus the engine's
own ledger.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from models.product import Marketplace, Product
from services.convsearch.engine import (
    ConversationSearchEngine,
    AI_CONVERSATION_SELECTION_LIMIT,
    AI_CONVERSATION_CANDIDATE_POOL_LIMIT,
    AI_CONVERSATION_REVIEW_POOL_LIMIT,
)
from services.convsearch.contract import Outcome, SearchDecision
from services.convsearch.state import Slot
from services.convsearch.state import ConversationEngineState, MessageIntent


def product(
    pid: str, title: str, *, price: float = 25.0, orders: int = 10,
    currency: str = "USD",
) -> Product:
    return Product(
        marketplace=Marketplace.ALIEXPRESS,
        product_id=pid,
        title=title,
        product_url=f"https://www.aliexpress.com/item/{pid}.html",
        price=price,
        currency=currency,
        orders_count=orders,
    )


def reel_pool() -> list[Product]:
    return [
        product("reel-1", "Рыболовная катушка спиннинговая безынерционная", price=30.0, orders=500),
        product("reel-2", "Катушка рыболовная карбоновая", price=20.0, orders=50),
        product("reel-3", "Рыболовная катушка мультипликаторная", price=35.0, orders=200),
        product("reel-holder", "Держатель катушки рыболовный", price=5.0, orders=900),
        product("reel-spring", "Пружина для рыболовной катушки", price=3.0, orders=1200),
        product("reel-case", "Чехол для рыболовной катушки", price=4.0, orders=800),
    ]


def screwdriver_pool() -> list[Product]:
    return [
        product("sd-impact", "Аккумуляторный ударный шуруповёрт", price=40.0, orders=300),
        product("sd-light", "Аккумуляторный шуруповёрт лёгкий бытовой", price=30.0, orders=150),
        product("sd-heavy", "Аккумуляторный шуруповёрт мощный", price=45.0, orders=200),
    ]


def powerbank_pool() -> list[Product]:
    return [
        product("pb-1", "Повербанк 20000 мАч быстрая зарядка", price=30.0, orders=400),
        product("pb-2", "Повербанк 10000 мАч", price=15.0, orders=250),
        product("pb-3", "Повербанк 20000 мАч PD 65W", price=40.0, orders=120),
        product("pb-cable", "Кабель для повербанка", price=3.0, orders=200),
        product("pb-charger", "Зарядное устройство сетевое", price=8.0, orders=300),
    ]


def household_pool() -> list[Product]:
    return [
        product("hh-1", "Органайзер для дома настольный", price=12.0, orders=90, currency="UAH"),
        product("hh-2", "Органайзер для дома стеклянный", price=20.0, orders=60, currency="UAH"),
        product("hh-3", "Органайзер для дома металлический", price=25.0, orders=45, currency="UAH"),
        product("hh-expensive", "Робот-пылесос для дома", price=120.0, orders=300, currency="UAH"),
    ]


class CrossCategoryEngineEvaluationTests(unittest.TestCase):
    """The real conversational pool evaluator must preserve product identity
    across unrelated categories, not only in the standalone type helper."""

    def test_host_products_survive_and_their_components_do_not(self) -> None:
        cases = (
            (
                "smartphone",
                "Smartphone 128GB unlocked",
                "Smartphone screen protector tempered glass",
            ),
            (
                "laptop",
                "Laptop 16GB RAM 512GB SSD",
                "Laptop keyboard replacement",
            ),
            (
                "camera",
                "Digital camera 4K autofocus",
                "Camera lens 50mm",
            ),
            (
                "vacuum cleaner",
                "Vacuum cleaner 2000W",
                "Vacuum cleaner HEPA filter",
            ),
            (
                "garden hose",
                "Garden hose 20m reinforced",
                "Garden hose nozzle adjustable",
            ),
            (
                "wireless headphones",
                "Wireless headphones Bluetooth ANC",
                "Wireless headphones ear pads",
            ),
            (
                "electric drill",
                "Electric drill cordless 21V",
                "Electric drill bits set",
            ),
            (
                "smartwatch",
                "Smartwatch AMOLED GPS",
                "Smartwatch replacement strap",
            ),
        )
        engine = ConversationSearchEngine()
        for index, (requested_type, host_title, component_title) in enumerate(cases):
            with self.subTest(category=requested_type):
                state = ConversationEngineState(user_key=f"cross-category-{index}")
                state.record_product_type(requested_type)
                strategy = SimpleNamespace(
                    product_type=requested_type,
                    request=None,
                    result_limit=30,
                )
                decision = engine._evaluate_pool(
                    state,
                    [
                        product(f"host-{index}", host_title),
                        product(f"component-{index}", component_title, orders=5000),
                    ],
                    strategy,
                    refined=False,
                    intent=MessageIntent.NEW_SEARCH,
                    observation={},
                    exclude_shown=False,
                )
                self.assertIs(decision.outcome, Outcome.RESULTS)
                self.assertEqual(
                    [item.product_id for item in decision.products],
                    [f"host-{index}"],
                    msg=f"requested type: {requested_type}",
                )
                self.assertEqual(decision.report["rejected_type"], 1)


class CrossLanguageRequestedAccessoryTests(unittest.TestCase):
    def test_requested_accessory_titles_reach_semantic_identity_review(self):
        cases = (
            ("чехол для телефона", "Phone case for iPhone 15"),
            ("ремешок для часов", "Watch band for Apple Watch"),
            ("фильтр для воды", "Water filter cartridge"),
        )
        engine = ConversationSearchEngine()
        for index, (requested_type, title) in enumerate(cases):
            with self.subTest(requested_type=requested_type):
                state = ConversationEngineState(
                    user_key=f"cross-language-accessory-{index}"
                )
                state.record_product_type(requested_type)
                candidate = product(f"accessory-{index}", title)
                strategy = SimpleNamespace(
                    product_type=requested_type,
                    request=SimpleNamespace(
                        normalized_query=requested_type,
                        original_query=requested_type,
                    ),
                    result_limit=30,
                )
                decision = engine._evaluate_pool(
                    state, [candidate], strategy,
                    refined=False, intent=MessageIntent.NEW_SEARCH,
                    observation={}, exclude_shown=False,
                )
                self.assertIs(decision.outcome, Outcome.RESULTS)
                self.assertIn(candidate.product_id, decision.report["semantic_review_ids"])
                self.assertEqual(decision.report["safe_ranked"], 0)


class CrossCategoryFeatureReviewTests(unittest.IsolatedAsyncioTestCase):
    """Unlisted feature wording must reach the same semantic review path
    across unrelated product families, not be auto-accepted or dropped."""

    async def test_cross_language_features_are_quarantined_and_routed_across_categories(self):
        cases = (
            ("шуруповёрт", "Cordless drill with 2 batteries", "двумя аккумуляторами"),
            ("налобный фонарь", "LED head torch IPX6", "защитой IPX6"),
            ("беспроводные наушники", "Bluetooth headset active noise cancelling", "шумоподавлением"),
            ("пылесос без мешка", "Bagless vacuum cleaner with HEPA filter", "HEPA фильтром"),
            ("садовый шланг", "Reinforced watering hose 30 m", "длиной 30 метров"),
            ("фильтр для воды", "5-stage water purifier filter", "пятью ступенями очистки"),
            ("ремешок для часов", "Silicone smartwatch band", "силиконовым материалом"),
            ("камера наблюдения", "CCTV camera infrared night vision", "ночным видением"),
        )

        for index, (requested_type, title, feature) in enumerate(cases):
            with self.subTest(category=requested_type):
                engine = ConversationSearchEngine()
                state = ConversationEngineState(user_key=f"cross-category-feature-{index}")
                state.record_product_type(requested_type)
                state.slots.append(Slot(
                    kind="feature",
                    value=feature,
                    source_turn=2,
                    raw_text=feature,
                ))
                candidate = product(f"cross-feature-{index}", title)
                strategy = SimpleNamespace(
                    product_type=requested_type,
                    request=SimpleNamespace(
                        normalized_query=requested_type,
                        original_query=requested_type,
                    ),
                    result_limit=30,
                )

                decision = engine._evaluate_pool(
                    state,
                    [candidate],
                    strategy,
                    refined=True,
                    intent=MessageIntent.REFINE,
                    observation={},
                    exclude_shown=False,
                )

                self.assertIs(decision.outcome, Outcome.RESULTS)
                self.assertEqual(decision.report["feature_review_ids"], [candidate.product_id])
                self.assertIn(candidate.product_id, decision.report["semantic_review_ids"])
                self.assertEqual(decision.report["safe_ranked"], 0)

                selector = AsyncMock(return_value=[candidate.product_id])
                engine.bind_ai_service(SimpleNamespace(
                    select_conversation_candidates=selector,
                ))
                await engine._apply_ai_candidate_selection(decision, strategy, state)

                self.assertEqual(
                    [item.product_id for item in decision.products],
                    [candidate.product_id],
                )
                self.assertEqual(selector.await_args.kwargs["required_features"], [feature])
                self.assertEqual(
                    selector.await_args.kwargs["identity_review_ids"],
                    [candidate.product_id],
                )




class BroadRetrievalTargetTests(unittest.IsolatedAsyncioTestCase):
    async def test_active_budget_is_forwarded_to_semantic_selector(self):
        engine = ConversationSearchEngine()
        state = ConversationEngineState(user_key="selector-budget-forwarding")
        state.slots.append(Slot(
            kind="budget",
            value="до 2500 грн",
            source_turn=1,
            raw_text="до 2500 грн",
        ))
        candidate = product("budget-forwarded", "Рыболовная катушка")
        decision = SearchDecision(
            outcome=Outcome.RESULTS,
            products=[candidate],
            report={},
        )
        selector = AsyncMock(return_value=[candidate.product_id])
        engine.bind_ai_service(SimpleNamespace(
            select_conversation_candidates=selector,
        ))
        strategy = SimpleNamespace(
            request=SimpleNamespace(
                normalized_query="рыболовная катушка",
                original_query="",
                intent_marketplace_terms=[],
            ),
            result_limit=30,
        )

        await engine._apply_ai_candidate_selection(decision, strategy, state)

        self.assertEqual(
            selector.await_args.kwargs["budget_constraint"],
            "до 2500 грн",
        )

    async def test_pagination_continues_to_ai_pool_target_not_display_count(self):
        from services.convsearch.retrieval import BackendOutcome

        pool = [
            product(f"broad-retrieval-{i}", "Рыболовная катушка спиннинговая")
            for i in range(AI_CONVERSATION_CANDIDATE_POOL_LIMIT)
        ]
        observed = []

        class PagingBackend:
            async def retrieve(self, strategy, *, exclude=None, should_continue=None):
                observed.append(should_continue(pool[:30]))
                observed.append(should_continue(pool))
                return BackendOutcome(
                    ok=True,
                    reason="ok",
                    products=pool,
                    request=strategy.request,
                )

        selector = AsyncMock(
            side_effect=lambda candidates, **kwargs: [
                item.product_id for item in candidates[:kwargs["wanted"]]
            ]
        )
        engine = ConversationSearchEngine(retrieval_backend=PagingBackend())
        engine.bind_ai_service(SimpleNamespace(
            is_available=True,
            select_conversation_candidates=selector,
        ))

        decision = await engine.handle_message(
            "broad-retrieval-target",
            "Найди рыболовную катушку",
        )

        self.assertEqual(observed, [True, False])
        self.assertEqual(decision.outcome, Outcome.RESULTS)
        self.assertEqual(len(selector.await_args.args[0]), AI_CONVERSATION_REVIEW_POOL_LIMIT - 10)
        self.assertEqual(selector.await_args.kwargs["wanted"], AI_CONVERSATION_SELECTION_LIMIT)
        self.assertEqual(len(decision.products), 30)
        self.assertEqual(decision.report["ai_selected"], 20)

    async def test_unavailable_ai_keeps_retrieval_target_at_requested_display_count(self):
        from services.convsearch.retrieval import BackendOutcome

        pool = [
            product(f"no-ai-retrieval-{i}", "Рыболовная катушка спиннинговая")
            for i in range(30)
        ]
        observed = []

        class PagingBackend:
            async def retrieve(self, strategy, *, exclude=None, should_continue=None):
                observed.append(should_continue(pool))
                return BackendOutcome(
                    ok=True,
                    reason="ok",
                    products=pool,
                    request=strategy.request,
                )

        engine = ConversationSearchEngine(retrieval_backend=PagingBackend())
        engine.bind_ai_service(SimpleNamespace(
            is_available=False,
            select_conversation_candidates=AsyncMock(),
        ))

        decision = await engine.handle_message(
            "no-ai-retrieval-target",
            "Найди рыболовную катушку",
            result_limit=5,
        )

        self.assertEqual(observed, [False])
        self.assertEqual(decision.outcome, Outcome.RESULTS)
        self.assertEqual(len(decision.products), 5)


class EngineScenarioTests(unittest.TestCase):
    """The four mandatory regression scenarios, plus the state tests."""

    def setUp(self) -> None:
        self.engine = ConversationSearchEngine()

    def test_question_about_shown_options_with_use_case_is_compare(self) -> None:
        for index, message in enumerate((
            "А что из этого лучше для дома?",
            "Какой из них лучше для дома?",
        )):
            with self.subTest(message=message):
                state = ConversationEngineState(user_key=f"shown-use-case-{index}")
                state.record_product_type("шуруповёрт")

                observation = state.observe(message)

                self.assertEqual(observation["intent"], MessageIntent.COMPARE)
                self.assertEqual(
                    state.active_by_kind("type").value, "шуруповёрт"
                )

    def test_targeted_slot_replace_preserves_unrelated_features_and_exclusions(self) -> None:
        state = ConversationEngineState(user_key="targeted-replace")
        state.slots.extend([
            Slot(kind="feature", value="два аккумулятора", source_turn=1, raw_text=""),
            Slot(kind="feature", value="аккумуляторная батарея", source_turn=1, raw_text=""),
            Slot(kind="feature", value="защита IP67", source_turn=1, raw_text=""),
            Slot(kind="exclusion", value="ударный", source_turn=1, raw_text=""),
        ])

        updates = [{
            "operation": "replace",
            "kind": "feature",
            "target": "два аккумулятора",
            "value": "один аккумулятор",
        }]
        observation = state.observe(
            "Не два аккумулятора, а один аккумулятор",
            intent_override=MessageIntent.CORRECTION,
            slot_updates=updates,
        )
        observation["model_slot_updates"] = updates
        state.record_slots(observation)

        self.assertEqual(
            state.active_values("feature"),
            ["аккумуляторная батарея", "защита IP67", "один аккумулятор"],
        )
        # A targeted correction is not a permanent exclusion of the old
        # value; it only supersedes the named feature slot.
        self.assertEqual(state.active_values("exclusion"), ["ударный"])
        self.assertNotIn("два аккумулятора", state.active_values("exclusion"))
        self.assertEqual(
            [
                slot.value for slot in state.slots
                if slot.kind == "feature" and slot.value == "два аккумулятора"
            ][0],
            "два аккумулятора",
        )
        self.assertEqual(
            next(
                slot.status for slot in state.slots
                if slot.kind == "feature" and slot.value == "два аккумулятора"
            ),
            "superseded",
        )

    def test_targeted_use_case_replace_preserves_other_use_cases(self) -> None:
        state = ConversationEngineState(user_key="targeted-use-case")
        state.slots.extend([
            Slot(kind="use_case", value="дома", source_turn=1, raw_text=""),
            Slot(kind="use_case", value="автомобиля", source_turn=1, raw_text=""),
        ])
        updates = [{
            "operation": "replace",
            "kind": "use_case",
            "target": "дома",
            "value": "дачи",
        }]

        observation = state.observe(
            "Не для дома, а для дачи",
            intent_override=MessageIntent.CORRECTION,
            slot_updates=updates,
        )
        observation["model_slot_updates"] = updates
        state.record_slots(observation)

        self.assertEqual(
            state.active_values("use_case"),
            ["автомобиля", "дачи"],
        )
        self.assertEqual(state.active_values("exclusion"), [])

    def test_targeted_slot_remove_preserves_other_slots_of_same_kind(self) -> None:
        state = ConversationEngineState(user_key="targeted-remove")
        state.slots.extend([
            Slot(kind="exclusion", value="ударный", source_turn=1, raw_text=""),
            Slot(kind="exclusion", value="полуавтомат", source_turn=1, raw_text=""),
            Slot(kind="feature", value="защита IP67", source_turn=1, raw_text=""),
        ])

        state.record_slots({
            "model_slot_updates": [{
                "operation": "remove",
                "kind": "exclusion",
                "target": "ударный",
                "value": None,
            }]
        })

        self.assertEqual(state.active_values("exclusion"), ["полуавтомат"])
        self.assertEqual(state.active_values("feature"), ["защита IP67"])

    def test_presentation_limit_is_hard_capped_at_30_even_without_ai(self) -> None:
        from types import SimpleNamespace
        from models.search_request import SearchRequest
        from services.convsearch.engine import RESULT_CARD_LIMIT

        state = ConversationEngineState(user_key="hard-card-cap")
        candidates = [
            product(f"organizer-{index:02d}", f"Органайзер настольный вариант {index}")
            for index in range(35)
        ]
        request = SearchRequest(
            original_query="органайзер",
            normalized_query="органайзер",
        )

        for requested_limit, expected in ((50, 30), (5, 5), (None, 30)):
            with self.subTest(requested_limit=requested_limit):
                strategy = SimpleNamespace(
                    product_type="органайзер",
                    result_limit=requested_limit,
                    request=request,
                )
                decision = self.engine._evaluate_pool(
                    state,
                    candidates,
                    strategy,
                    refined=False,
                    intent=MessageIntent.NEW_SEARCH,
                    observation={},
                    exclude_shown=False,
                )
                self.assertEqual(decision.outcome, Outcome.RESULTS)
                self.assertEqual(len(decision.products), expected)
                self.assertLessEqual(len(decision.products), RESULT_CARD_LIMIT)

    def test_scenario_a_complete_fishing_reel(self) -> None:
        state = ConversationEngineState(user_key="u-a")
        observation = state.observe("Нужна недорогая рыболовная катушка.")
        state.record_slots(observation)
        state.record_product_type("рыболовная катушка")
        self.assertEqual(observation["intent"], MessageIntent.NEW_SEARCH)

        ranked, report = self.engine.shape_pool(
            state, reel_pool(), "рыболовная катушка"
        )
        ids = [product.product_id for product in ranked]
        self.assertEqual(ids[:3], ["reel-1", "reel-3", "reel-2"])
        self.assertNotIn("reel-holder", ids[:3])
        self.assertNotIn("reel-spring", ids[:3])
        self.assertNotIn("reel-case", ids[:3])
        self.assertEqual(report["reason"], "ok")

        obs2 = state.observe("Подешевле.")
        state.record_slots(obs2)
        self.assertEqual(obs2["intent"], MessageIntent.REFINE)
        self.assertEqual(state.active_by_kind("type").value, "рыболовная катушка")

        obs3 = state.observe("Но не запчасти, нужна целая катушка.")
        state.record_slots(obs3)
        self.assertEqual(obs3["intent"], MessageIntent.REFINE)
        self.assertEqual(state.active_by_kind("type").value, "рыболовная катушка")
        self.assertTrue(state.active_values("exclusion"))

    def test_natural_two_battery_followup_is_a_feature_not_a_new_product(self) -> None:
        state = ConversationEngineState(user_key="two-battery-natural-followup")
        first = state.observe("Нужен аккумуляторный шуруповёрт для дома")
        state.record_slots(first)
        state.record_product_type("аккумуляторный шуруповёрт")

        from services.convsearch.engine import (
            _explicit_requested_product_type,
            _product_type_of,
        )
        message = "Хочу, чтобы было два аккумулятора"
        self.assertIsNone(_explicit_requested_product_type(message))
        self.assertIsNone(_product_type_of(message))

        followup = state.observe(message)
        state.record_slots(followup)

        self.assertEqual(followup["intent"], MessageIntent.REFINE)
        self.assertEqual(
            state.active_by_kind("type").value,
            "аккумуляторный шуруповёрт",
        )
        features = state.active_values("feature")
        self.assertTrue(
            any("аккумулятор" in value for value in features),
            f"Expected the requested battery quantity to be recorded as a feature, got {features!r}",
        )
        self.assertFalse(any("хочу" in value or "было" in value for value in features))

    def test_subordinate_feature_followups_ignore_pronoun_and_modal_scaffolding(self) -> None:
        from services.convsearch.engine import (
            _explicit_requested_product_type,
            _product_type_of,
        )

        messages = (
            "Мне нужно, чтобы было два аккумулятора",
            "Хочу, чтобы у него было два аккумулятора",
            "Мне нужно, чтобы у него было два аккумулятора",
            "Хочу, чтоб у него было два аккумулятора",
            "Мне нужно, чтобы у него должно быть два аккумулятора",
        )
        for index, message in enumerate(messages):
            with self.subTest(message=message):
                state = ConversationEngineState(
                    user_key=f"subordinate-feature-{index}"
                )
                state.record_product_type("шуруповёрт")

                self.assertIsNone(_explicit_requested_product_type(message))
                self.assertIsNone(_product_type_of(message))
                observation = state.observe(message)
                state.record_slots(observation)

                self.assertEqual(observation["intent"], MessageIntent.REFINE)
                self.assertEqual(state.active_values("type"), ["шуруповёрт"])
                features = state.active_values("feature")
                self.assertTrue(
                    any("аккумулятор" in value for value in features),
                    f"Battery requirement was not retained: {features!r}",
                )
                self.assertFalse(
                    any(
                        filler in value
                        for value in features
                        for filler in ("хочу", "нужно", "него", "было", "должно", "быть")
                    ),
                    f"Conversational scaffolding leaked into features: {features!r}",
                )

    def test_scenario_b_non_impact_cordless_screwdriver(self) -> None:
        state = ConversationEngineState(user_key="u-b")
        obs1 = state.observe("Посоветуй аккумуляторный шуруповёрт.")
        state.record_slots(obs1)
        state.record_product_type("шуруповёрт")
        self.assertEqual(obs1["intent"], MessageIntent.NEW_SEARCH)

        obs2 = state.observe("Только не ударный.")
        state.record_slots(obs2)
        self.assertEqual(obs2["intent"], MessageIntent.REFINE)
        exclusions = state.active_values("exclusion")
        self.assertTrue(any("ударный" in value for value in exclusions))

        ranked, _report = self.engine.shape_pool(state, screwdriver_pool(), "шуруповёрт")
        ids = [product.product_id for product in ranked]
        self.assertNotIn("sd-impact", ids)

        obs3 = state.observe("А есть полегче?")
        state.record_slots(obs3)
        self.assertEqual(obs3["intent"], MessageIntent.REFINE)
        exclusions = state.active_values("exclusion")
        self.assertTrue(any("ударный" in value for value in exclusions))
        ranked2, _report2 = self.engine.shape_pool(state, screwdriver_pool(), "шуруповёрт")
        self.assertNotIn("sd-impact", [product.product_id for product in ranked2])

    def test_scenario_c_power_bank(self) -> None:
        state = ConversationEngineState(user_key="u-c")
        obs1 = state.observe("Нужен мощный повербанк.")
        state.record_slots(obs1)
        state.record_product_type("повербанк")
        self.assertEqual(obs1["intent"], MessageIntent.NEW_SEARCH)

        ranked, report = self.engine.shape_pool(state, powerbank_pool(), "повербанк")
        ids = [product.product_id for product in ranked]
        self.assertNotIn("pb-cable", ids[:3])
        self.assertNotIn("pb-charger", ids[:3])
        self.assertEqual(report["reason"], "ok")

        obs2 = state.observe("С большой ёмкостью.")
        state.record_slots(obs2)
        self.assertEqual(obs2["intent"], MessageIntent.REFINE)
        obs3 = state.observe("И чтобы была быстрая зарядка.")
        state.record_slots(obs3)
        self.assertEqual(obs3["intent"], MessageIntent.REFINE)
        features = state.active_values("feature")
        self.assertTrue(any("ёмкость" in value or "емкость" in value for value in features))
        self.assertTrue(any("зарядка" in value for value in features))

    def test_alternatives_need_an_existing_task_context(self) -> None:
        state = ConversationEngineState(user_key="alternatives-without-task")
        greeting = state.observe("Привет!")
        state.record_slots(greeting)

        followup = state.observe("Покажи другие варианты.")

        self.assertNotEqual(followup["intent"], MessageIntent.ALTERNATIVES)
        self.assertEqual(state.active(), [])

    def test_scenario_d_household_alternatives(self) -> None:
        state = ConversationEngineState(user_key="u-d")
        obs1 = state.observe("Найди что-нибудь полезное для дома.")
        state.record_slots(obs1)
        self.assertEqual(obs1["intent"], MessageIntent.NEW_SEARCH)

        obs2 = state.observe("До 500 гривен.")
        state.record_slots(obs2)
        budget = state.active_by_kind("budget")
        self.assertIsNotNone(budget)
        self.assertEqual(obs2["budget"]["currency"], "UAH")

        ranked, report = self.engine.shape_pool(state, household_pool(), "")
        self.assertEqual(report["reason"], "ok")

        obs3 = state.observe("Покажи другие варианты.")
        state.record_slots(obs3)
        self.assertEqual(obs3["intent"], MessageIntent.ALTERNATIVES)
        self.assertIsNotNone(state.active_by_kind("budget"))

        obs4 = state.observe("А покажи ещё варианты для дома.")
        state.record_slots(obs4)
        self.assertEqual(obs4["intent"], MessageIntent.ALTERNATIVES)

    def test_explain_request_naming_another_product_is_a_new_search(self) -> None:
        state = ConversationEngineState(user_key="explain-new-product")
        first = state.observe("Нужен аккумуляторный шуруповёрт.")
        state.record_slots(first)
        state.record_product_type("шуруповёрт")

        followup = state.observe("Расскажи про пылесос.")

        self.assertEqual(followup["intent"], MessageIntent.NEW_SEARCH)
        self.assertEqual(state.active_by_kind("type"), None)

        from services.convsearch.engine import _product_type_of
        self.assertEqual(_product_type_of("Расскажи про пылесос."), "пылесос")
        self.assertEqual(_product_type_of("Что насчёт пылесоса?"), "пылесоса")
        self.assertEqual(_product_type_of("Какой пылесос лучше?"), "пылесос")

    def test_new_product_request_resets(self) -> None:
        state = ConversationEngineState(user_key="u-e")
        obs1 = state.observe("Нужна недорогая рыболовная катушка.")
        state.record_slots(obs1)
        state.record_product_type("рыболовная катушка")
        self.assertIsNotNone(state.active_by_kind("type"))
        obs2 = state.observe("Найди автомобильный компрессор.")
        state.record_slots(obs2)
        self.assertEqual(obs2["intent"], MessageIntent.NEW_SEARCH)
        self.assertIsNone(state.active_by_kind("type"))

    def test_product_before_purpose_connector_switches_across_item_families(self) -> None:
        cases = (
            ("коврик", "Клавиатура для ноутбука"),
            ("пылесос", "Нужен фильтр для пылесоса"),
            ("дрель", "Нужна насадка на дрель"),
            ("телефон", "Нужен чехол для телефона"),
        )
        for previous_type, new_request in cases:
            with self.subTest(previous_type=previous_type, new_request=new_request):
                state = ConversationEngineState(user_key=f"switch-{previous_type}")
                state.record_product_type(previous_type)
                state.record_shown(["previous-card"])

                observation = state.observe(new_request)

                self.assertEqual(observation["intent"], MessageIntent.NEW_SEARCH)
                self.assertEqual(state.active_values("type"), [])
                self.assertEqual(state.shown_product_ids, [])

    def test_model_and_spec_tokens_do_not_turn_same_product_into_a_switch(self) -> None:
        cases = (
            ("ноутбук", "Ноутбук Lenovo ThinkPad T14s для работы"),
            ("телефон", "Нужна защита IP67 для телефона"),
        )
        for current_type, follow_up in cases:
            with self.subTest(current_type=current_type, follow_up=follow_up):
                state = ConversationEngineState(user_key=f"same-{current_type}")
                state.record_product_type(current_type)
                state.record_shown(["current-card"])

                observation = state.observe(follow_up)

                self.assertEqual(observation["intent"], MessageIntent.REFINE)
                self.assertEqual(state.active_values("type"), [current_type])
                self.assertEqual(state.shown_product_ids, ["current-card"])

    def test_correction_replaces_previous_requirement(self) -> None:
        state = ConversationEngineState(user_key="u-f")
        obs1 = state.observe("Нужна недорогая рыболовная катушка.")
        state.record_slots(obs1)
        obs2 = state.observe("На самом деле нужна целая катушка без держателя.")
        state.record_slots(obs2)
        self.assertTrue(obs2["reversal"])
        self.assertTrue(any("держател" in v for v in state.active_values("exclusion")))

    def test_two_users_do_not_leak(self) -> None:
        state_a = ConversationEngineState(user_key="u-a")
        state_b = ConversationEngineState(user_key="u-b")
        state_a.observe("Нужна недорогая рыболовная катушка.")
        state_a.record_product_type("рыболовная катушка")
        state_b.observe("Посоветуй аккумуляторный шуруповёрт.")
        state_b.record_product_type("шуруповёрт")
        self.assertEqual(state_a.active_by_kind("type").value, "рыболовная катушка")
        self.assertEqual(state_b.active_by_kind("type").value, "шуруповёрт")

    def test_alternatives_exclude_shown(self) -> None:
        from services.convsearch import retrieval as retrieval_mod
        state = ConversationEngineState(user_key="u-h")
        state.record_shown(["reel-1"])
        survivors = retrieval_mod.exclude_shown(reel_pool(), state)
        ids = [product.product_id for product in survivors]
        self.assertNotIn("reel-1", ids)
        self.assertIn("reel-2", ids)

    def test_provider_failure_vs_empty_vs_rejected(self) -> None:
        engine = ConversationSearchEngine()
        state = ConversationEngineState(user_key="u-i")
        _ranked, report_empty = engine.shape_pool(state, [], "")
        self.assertEqual(report_empty["reason"], "no_candidates")
        state.record_product_type("рыболовная катушка")
        only_accessories = [product("reel-holder", "Держатель катушки рыболовный", price=5.0)]
        _ranked, report_rejected = engine.shape_pool(
            state, only_accessories, "рыболовная катушка"
        )
        self.assertEqual(report_rejected["reason"], "rejected")

    def test_affiliate_url_preserved(self) -> None:
        engine = ConversationSearchEngine()
        state = ConversationEngineState(user_key="u-j")
        ranked, report = engine.shape_pool(
            state, [product("aff-1", "Рыболовная катушка", price=30.0)], "рыболовная катушка"
        )
        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0].product_url, "https://www.aliexpress.com/item/aff-1.html")
        self.assertEqual(report["reason"], "ok")



class FullShownContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_interpreter_receives_all_thirty_shown_cards(self) -> None:
        captured = {}

        class CompareAI:
            async def decide_conversation_turn(self, text, *, context):
                captured.update(context)
                return {"action": "compare"}

        engine = ConversationSearchEngine()
        engine.bind_ai_service(CompareAI())
        state = ConversationEngineState(user_key="compare-thirty")
        state.record_shown([f"p{index}" for index in range(30)])
        shown = [product(f"p{index}", f"Товар {index}") for index in range(30)]

        decision = await engine.handle_message(
            "compare-thirty",
            "Сравни все варианты",
            state=state,
            shown_products=shown,
        )

        self.assertEqual(decision.outcome, Outcome.COMPARE)
        self.assertEqual(len(captured["shown_products"]), 30)
        self.assertEqual(
            [item["id"] for item in captured["shown_products"]],
            [f"p{index}" for index in range(30)],
        )


class RankingRequestPropagationTests(unittest.TestCase):
    def test_engine_passes_the_built_request_into_ranking(self):
        from unittest.mock import patch
        from models.search_request import SearchRequest

        engine = ConversationSearchEngine()
        state = ConversationEngineState(user_key="ranking-request")
        candidate = product("reel", "Рыболовная катушка")
        request = SearchRequest(
            original_query="рыболовная катушка",
            normalized_query="рыболовная катушка",
        )
        strategy = SimpleNamespace(
            product_type="рыболовная катушка",
            request=request,
            result_limit=30,
        )

        with patch(
            "services.convsearch.engine.ranking_mod.rank_candidates",
            wraps=__import__(
                "services.convsearch.ranking", fromlist=["rank_candidates"]
            ).rank_candidates,
        ) as rank:
            decision = engine._evaluate_pool(
                state, [candidate], strategy,
                refined=False, intent=MessageIntent.NEW_SEARCH,
                observation={}, exclude_shown=False,
            )

        self.assertEqual(decision.outcome, Outcome.RESULTS)
        self.assertIs(rank.call_args.kwargs["request"], request)


class SelectorSafetyRegressionTests(unittest.IsolatedAsyncioTestCase):

    async def test_ai_reviews_broad_pool_before_thirty_card_presentation_cut(self):
        engine = ConversationSearchEngine()
        state = ConversationEngineState(user_key="broad-ai-review-pool")
        state.record_product_type("рыболовная катушка")
        candidates = [
            product(f"broad-{i}", "Рыболовная катушка спиннинговая")
            for i in range(80)
        ]
        strategy = SimpleNamespace(
            product_type="рыболовная катушка",
            request=SimpleNamespace(
                normalized_query="рыболовная катушка",
                original_query="рыболовная катушка",
            ),
            result_limit=30,
        )
        decision = engine._evaluate_pool(
            state, candidates, strategy,
            refined=False, intent=MessageIntent.NEW_SEARCH,
            observation={}, exclude_shown=False,
            candidate_pool_limit=AI_CONVERSATION_CANDIDATE_POOL_LIMIT,
        )
        self.assertEqual(len(decision.products), 80)
        original_ids = [item.product_id for item in decision.products]
        observed = {}

        async def select_broad_window(pool, **kwargs):
            observed["ids"] = [item.product_id for item in pool]
            observed["wanted"] = kwargs["wanted"]
            return observed["ids"][:kwargs["wanted"]]

        engine.bind_ai_service(SimpleNamespace(
            select_conversation_candidates=AsyncMock(side_effect=select_broad_window),
        ))
        await engine._apply_ai_candidate_selection(decision, strategy, state)

        self.assertEqual(len(observed["ids"]), AI_CONVERSATION_REVIEW_POOL_LIMIT - 10)
        self.assertEqual(observed["wanted"], AI_CONVERSATION_SELECTION_LIMIT)
        self.assertEqual(len(decision.products), 30)
        self.assertEqual(decision.report["ai_selected"], 20)
        self.assertEqual(decision.report["ai_tail_added"], 10)
        self.assertEqual(
            [item.product_id for item in decision.products[-10:]],
            original_ids[70:80],
        )

    async def test_small_requested_count_reviews_entire_bounded_pool(self):
        engine = ConversationSearchEngine()
        state = ConversationEngineState(user_key="small-count-full-review")
        candidates = [product(f"small-{i}", "Рыболовная катушка") for i in range(80)]
        decision = SearchDecision(
            outcome=Outcome.RESULTS,
            products=candidates,
            report={},
        )
        observed = {}

        async def select_small_count(pool, **kwargs):
            observed["count"] = len(pool)
            return [item.product_id for item in pool[:kwargs["wanted"]]]

        engine.bind_ai_service(SimpleNamespace(
            select_conversation_candidates=AsyncMock(side_effect=select_small_count),
        ))
        strategy = SimpleNamespace(
            request=SimpleNamespace(
                normalized_query="рыболовная катушка",
                original_query="",
            ),
            result_limit=5,
        )

        await engine._apply_ai_candidate_selection(decision, strategy, state)

        self.assertEqual(observed["count"], AI_CONVERSATION_REVIEW_POOL_LIMIT)
        self.assertEqual(len(decision.products), 5)
        self.assertEqual(decision.report["ai_selected"], 5)

    async def test_no_selector_still_enforces_thirty_card_presentation_cap(self):
        engine = ConversationSearchEngine()
        state = ConversationEngineState(user_key="no-selector-presentation-cap")
        decision = SearchDecision(
            outcome=Outcome.RESULTS,
            products=[
                product(f"no-ai-{i}", "Рыболовная катушка")
                for i in range(45)
            ],
            report={},
        )
        strategy = SimpleNamespace(
            request=SimpleNamespace(
                normalized_query="рыболовная катушка",
                original_query="",
            ),
            result_limit=30,
        )

        await engine._apply_ai_candidate_selection(decision, strategy, state)

        self.assertEqual(len(decision.products), 30)

    async def test_selector_underselection_is_not_padded_by_unseen_tail(self):
        engine = ConversationSearchEngine()
        selector = AsyncMock(return_value=["p0"])
        engine.bind_ai_service(SimpleNamespace(select_conversation_candidates=selector))
        state = ConversationEngineState(user_key="selector-pool")
        candidates = [product(f"p{i}", "Рыболовная катушка") for i in range(30)]
        decision = SearchDecision(
            outcome=Outcome.RESULTS,
            products=candidates,
            report={},
        )
        strategy = SimpleNamespace(
            request=SimpleNamespace(normalized_query="рыболовная катушка", original_query=""),
            result_limit=30,
        )

        await engine._apply_ai_candidate_selection(decision, strategy, state)

        self.assertEqual(selector.await_count, 2)
        self.assertEqual(
            len(selector.await_args_list[0].args[0]),
            AI_CONVERSATION_SELECTION_LIMIT,
        )
        self.assertEqual(
            len(selector.await_args_list[1].args[0]),
            AI_CONVERSATION_SELECTION_LIMIT - 1,
        )
        self.assertEqual([item.product_id for item in decision.products], ["p0"])
        self.assertEqual(decision.report["ai_selected"], 1)
        self.assertEqual(decision.report["ai_second_pass"], "completed")
        self.assertEqual(decision.report["ai_tail_added"], 0)

    async def test_short_ai_selection_is_completed_from_remaining_candidates(self):
        engine = ConversationSearchEngine()
        state = ConversationEngineState(user_key="selector-continuation")
        candidates = [product(f"continue-{i}", "Рыболовная катушка") for i in range(30)]
        calls = []

        async def select_batch(pool, **kwargs):
            calls.append({
                "ids": [item.product_id for item in pool],
                "wanted": kwargs["wanted"],
            })
            if len(calls) == 1:
                return [f"continue-{i}" for i in range(4)]
            return [item.product_id for item in pool[:kwargs["wanted"]]]

        selector = AsyncMock(side_effect=select_batch)
        engine.bind_ai_service(SimpleNamespace(
            select_conversation_candidates=selector,
        ))
        decision = SearchDecision(
            outcome=Outcome.RESULTS,
            products=candidates,
            report={},
        )
        strategy = SimpleNamespace(
            request=SimpleNamespace(
                normalized_query="рыболовная катушка",
                original_query="",
            ),
            result_limit=30,
        )

        await engine._apply_ai_candidate_selection(decision, strategy, state)

        self.assertEqual(selector.await_count, 2)
        self.assertEqual(calls[0]["wanted"], 20)
        self.assertEqual(calls[1]["wanted"], 16)
        self.assertEqual(len(calls[1]["ids"]), 16)
        self.assertFalse(set(calls[0]["ids"][:4]) & set(calls[1]["ids"]))
        self.assertEqual(decision.report["ai_first_pass_selected"], 4)
        self.assertEqual(decision.report["ai_second_pass_selected"], 16)
        self.assertEqual(decision.report["ai_selected"], 20)
        self.assertEqual(len(decision.products), 30)
        self.assertEqual(
            [item.product_id for item in decision.products[:20]],
            [f"continue-{i}" for i in range(20)],
        )

    async def test_full_selector_window_keeps_default_thirty_card_tail(self):
        engine = ConversationSearchEngine()
        selected_ids = [f"p{i}" for i in range(AI_CONVERSATION_SELECTION_LIMIT)]
        selector = AsyncMock(return_value=selected_ids)
        engine.bind_ai_service(SimpleNamespace(select_conversation_candidates=selector))
        state = ConversationEngineState(user_key="selector-full-window")
        candidates = [product(f"p{i}", "Рыболовная катушка") for i in range(30)]
        decision = SearchDecision(
            outcome=Outcome.RESULTS,
            products=candidates,
            report={},
        )
        strategy = SimpleNamespace(
            request=SimpleNamespace(normalized_query="рыболовная катушка", original_query=""),
            result_limit=30,
        )

        await engine._apply_ai_candidate_selection(decision, strategy, state)

        self.assertEqual(len(decision.products), 30)
        self.assertEqual(
            [item.product_id for item in decision.products[-10:]],
            [f"p{i}" for i in range(20, 30)],
        )
        self.assertEqual(decision.report["ai_selected"], 20)
        self.assertEqual(decision.report["ai_tail_added"], 10)

    async def test_late_type_mention_is_quarantined_when_semantic_selector_times_out(self):
        engine = ConversationSearchEngine()
        state = ConversationEngineState(user_key="late-type-review")
        state.record_product_type("насадка")
        strategy = SimpleNamespace(
            product_type="насадка",
            request=SimpleNamespace(
                normalized_query="насадка на дрель по дереву",
                original_query="",
            ),
            result_limit=30,
        )
        mixed_listing = product(
            "mixed-drill-attachments",
            "Многофункциональная дрель, угловая шлифовальная машина, "
            "набор шатунов, насадки для шатунов, регулируемый диск",
        )

        decision = engine._evaluate_pool(
            state, [mixed_listing], strategy,
            refined=False, intent=MessageIntent.NEW_SEARCH,
            observation={}, exclude_shown=False,
        )

        self.assertEqual(decision.outcome, Outcome.RESULTS)
        self.assertEqual(
            decision.report["semantic_review_ids"],
            ["mixed-drill-attachments"],
        )
        selector = AsyncMock(side_effect=TimeoutError)
        engine.bind_ai_service(SimpleNamespace(
            select_conversation_candidates=selector,
        ))
        await engine._apply_ai_candidate_selection(decision, strategy, state)

        self.assertEqual(decision.outcome, Outcome.NO_ELIGIBLE)
        self.assertEqual(decision.products, [])
        self.assertEqual(decision.report["ai_selection"], "timeout_fallback")

    async def test_description_only_product_identity_reaches_semantic_selector(self):
        engine = ConversationSearchEngine()
        state = ConversationEngineState(user_key="description-identity-review")
        state.record_product_type("насадка")
        strategy = SimpleNamespace(
            product_type="насадка",
            request=SimpleNamespace(
                normalized_query="насадка дрель дерево",
                original_query="",
            ),
            result_limit=30,
        )
        candidate = product(
            "description-attachment",
            "Универсальный набор инструментов",
        ).model_copy(update={
            "description": "Насадка для дрели по дереву с хвостовиком",
        })

        decision = engine._evaluate_pool(
            state, [candidate], strategy,
            refined=False, intent=MessageIntent.NEW_SEARCH,
            observation={}, exclude_shown=False,
        )

        self.assertEqual(
            decision.report["semantic_review_ids"],
            ["description-attachment"],
        )
        self.assertEqual(decision.report["safe_ranked"], 0)
        selector = AsyncMock(return_value=["description-attachment"])
        engine.bind_ai_service(SimpleNamespace(
            select_conversation_candidates=selector,
        ))
        await engine._apply_ai_candidate_selection(decision, strategy, state)

        self.assertEqual(decision.outcome, Outcome.RESULTS)
        self.assertEqual(
            [item.product_id for item in decision.products],
            ["description-attachment"],
        )
        self.assertEqual(
            selector.await_args.args[0][0].description,
            "Насадка для дрели по дереву с хвостовиком",
        )

    async def test_missing_required_feature_evidence_is_quarantined_for_ai_review(self):
        engine = ConversationSearchEngine()
        state = ConversationEngineState(user_key="feature-review")
        state.record_product_type("сварочный аппарат")
        state.slots.append(Slot(
            kind="feature",
            value="двумя аккумуляторами",
            source_turn=2,
            raw_text="С двумя аккумуляторами",
        ))
        strategy = SimpleNamespace(
            product_type="сварочный аппарат",
            request=SimpleNamespace(
                normalized_query="сварочный аппарат двумя аккумуляторами",
                original_query="",
            ),
            result_limit=30,
        )
        candidate = product("welder-unknown-batteries", "Сварочный аппарат инверторный")

        decision = engine._evaluate_pool(
            state, [candidate], strategy,
            refined=True, intent=MessageIntent.REFINE,
            observation={}, exclude_shown=False,
        )

        self.assertEqual(decision.outcome, Outcome.RESULTS)
        self.assertEqual(
            decision.report["feature_review_ids"],
            ["welder-unknown-batteries"],
        )
        self.assertIn(
            "welder-unknown-batteries",
            decision.report["semantic_review_ids"],
        )

        selector = AsyncMock(return_value=["welder-unknown-batteries"])
        engine.bind_ai_service(SimpleNamespace(select_conversation_candidates=selector))
        await engine._apply_ai_candidate_selection(decision, strategy, state)

        self.assertEqual(
            [p.product_id for p in decision.products],
            ["welder-unknown-batteries"],
        )
        self.assertEqual(selector.await_args.kwargs["query"].find("двумя аккумуляторами") >= 0, True)
        self.assertEqual(selector.await_args.kwargs["required_features"], ["двумя аккумуляторами"])
        self.assertEqual(selector.await_args.kwargs["identity_review_ids"], ["welder-unknown-batteries"])

    def test_required_feature_tokens_must_form_one_positive_span(self):
        from services.convsearch.retrieval import _feature_is_evidenced

        cases = [
            (
                product("affirmative", "Cordless tool with two batteries"),
                "two batteries",
                True,
            ),
            (
                product(
                    "scattered",
                    "Cordless tool with two speed modes",
                ).model_copy(update={"description": "Includes one battery"}),
                "two batteries",
                False,
            ),
            (
                product(
                    "negated-before",
                    "Phone case",
                ).model_copy(update={"description": "Not compatible with iPhone 15"}),
                "iPhone 15",
                False,
            ),
            (
                product(
                    "negated-after",
                    "Battery pack",
                ).model_copy(update={"description": "Two batteries not included"}),
                "two batteries",
                False,
            ),
            (
                product(
                    "positive-compatibility",
                    "Phone case",
                ).model_copy(update={"description": "Compatible with iPhone 15"}),
                "iPhone 15",
                True,
            ),
        ]
        for candidate, feature, expected in cases:
            with self.subTest(product_id=candidate.product_id):
                self.assertEqual(
                    _feature_is_evidenced(candidate, feature),
                    expected,
                )

    async def test_unknown_required_feature_stays_hidden_without_ai_confirmation(self):
        engine = ConversationSearchEngine()
        state = ConversationEngineState(user_key="feature-review-no-ai")
        state.slots.append(Slot(
            kind="feature",
            value="двумя аккумуляторами",
            source_turn=2,
            raw_text="С двумя аккумуляторами",
        ))
        strategy = SimpleNamespace(
            product_type="сварочный аппарат",
            request=SimpleNamespace(
                normalized_query="сварочный аппарат",
                original_query="",
            ),
            result_limit=30,
        )
        candidate = product("welder-unknown-batteries", "Сварочный аппарат инверторный")
        decision = engine._evaluate_pool(
            state, [candidate], strategy,
            refined=True, intent=MessageIntent.REFINE,
            observation={}, exclude_shown=False,
        )

        await engine._apply_ai_candidate_selection(decision, strategy, state)

        self.assertEqual(decision.outcome, Outcome.NO_ELIGIBLE)
        self.assertEqual(decision.products, [])
        self.assertEqual(decision.report["reason"], "unverified_required_features")

    async def test_semantic_selector_can_confirm_alternative_product_head(self):
        engine = ConversationSearchEngine()
        selector = AsyncMock(return_value=["welder-semi"])
        engine.bind_ai_service(SimpleNamespace(select_conversation_candidates=selector))
        state = ConversationEngineState(user_key="semantic-product-identity")
        decision = SearchDecision(
            outcome=Outcome.RESULTS,
            products=[
                product("welder-semi", "Сварочный полуавтомат MIG"),
                product("welder-table", "Сварочный стол усиленный"),
            ],
            report={"semantic_review_ids": ["welder-semi", "welder-table"]},
        )
        strategy = SimpleNamespace(
            request=SimpleNamespace(normalized_query="сварочный аппарат", original_query=""),
            result_limit=30,
        )

        await engine._apply_ai_candidate_selection(decision, strategy, state)

        self.assertEqual([p.product_id for p in decision.products], ["welder-semi"])
        self.assertEqual(decision.report["ai_selection"], "selected")

    async def test_semantic_review_candidates_are_removed_on_selector_timeout(self):
        engine = ConversationSearchEngine()
        selector = AsyncMock(side_effect=TimeoutError)
        engine.bind_ai_service(SimpleNamespace(select_conversation_candidates=selector))
        state = ConversationEngineState(user_key="semantic-review-timeout")
        decision = SearchDecision(
            outcome=Outcome.RESULTS,
            products=[
                product("exact", "Сварочный аппарат инверторный"),
                product("ambiguous", "Сварочный стол усиленный"),
            ],
            report={"semantic_review_ids": ["ambiguous"]},
        )
        strategy = SimpleNamespace(
            request=SimpleNamespace(normalized_query="сварочный аппарат", original_query=""),
            result_limit=30,
        )

        await engine._apply_ai_candidate_selection(decision, strategy, state)

        self.assertEqual([p.product_id for p in decision.products], ["exact"])
        self.assertEqual(decision.outcome, Outcome.RESULTS)
        self.assertEqual(decision.report["ai_selection"], "timeout_fallback")

    async def test_selector_timeout_fails_closed_even_for_deterministically_validated_candidates(self):
        engine = ConversationSearchEngine()
        selector = AsyncMock(side_effect=TimeoutError)
        engine.bind_ai_service(SimpleNamespace(select_conversation_candidates=selector))
        state = ConversationEngineState(user_key="selector-exclusion")
        state.slots.append(Slot(
            kind="exclusion", value="полуавтомат", source_turn=1,
            raw_text="Только не полуавтомат",
        ))
        decision = SearchDecision(
            outcome=Outcome.RESULTS,
            products=[
                product("welder-inverter", "Сварочный аппарат инверторный"),
                product("welder-mma", "Сварочный аппарат MMA"),
            ],
            report={},
        )
        strategy = SimpleNamespace(
            request=SimpleNamespace(normalized_query="сварочный аппарат", original_query=""),
            result_limit=30,
        )

        await engine._apply_ai_candidate_selection(decision, strategy, state)

        self.assertEqual(decision.outcome, Outcome.RESULTS)
        self.assertEqual(
            [p.product_id for p in decision.products],
            ["welder-inverter", "welder-mma"],
        )
        self.assertEqual(decision.report["ai_selection"], "timeout_fallback")
        self.assertEqual(decision.report["reason"], "deterministic_fallback")

    async def test_valid_empty_selector_response_is_an_explicit_no_match(self):
        engine = ConversationSearchEngine()
        selector = AsyncMock(return_value=[])
        engine.bind_ai_service(SimpleNamespace(select_conversation_candidates=selector))
        state = ConversationEngineState(user_key="selector-empty")
        decision = SearchDecision(
            outcome=Outcome.RESULTS,
            products=[product("p1", "Рыболовная катушка")],
            report={},
        )
        strategy = SimpleNamespace(
            request=SimpleNamespace(normalized_query="рыболовная катушка", original_query=""),
            result_limit=30,
        )

        await engine._apply_ai_candidate_selection(decision, strategy, state)

        self.assertEqual(decision.outcome, Outcome.NO_ELIGIBLE)
        self.assertEqual(decision.products, [])
        self.assertEqual(decision.report["ai_selection"], "semantic_no_match")
        self.assertEqual(decision.report["reason"], "semantic_no_match")

    async def test_zero_selector_window_fails_closed_without_second_gemini_call(self):
        engine = ConversationSearchEngine()
        selector = AsyncMock(return_value=["p1"])
        engine.bind_ai_service(SimpleNamespace(
            select_conversation_candidates=selector,
        ))
        state = ConversationEngineState(user_key="selector-budget")
        decision = SearchDecision(
            outcome=Outcome.RESULTS,
            products=[product("p1", "Рыболовная катушка")],
            report={},
        )
        strategy = SimpleNamespace(
            request=SimpleNamespace(
                normalized_query="рыболовная катушка",
                original_query="",
            ),
            result_limit=30,
        )

        await engine._apply_ai_candidate_selection(
            decision, strategy, state, timeout_seconds=0.0
        )

        selector.assert_not_awaited()
        self.assertEqual(decision.outcome, Outcome.RESULTS)
        self.assertEqual([p.product_id for p in decision.products], ["p1"])
        self.assertEqual(decision.report["ai_selection"], "turn_budget_exhausted")

    async def test_selector_error_fails_closed_instead_of_showing_unreviewed_pool(self):
        engine = ConversationSearchEngine()
        selector = AsyncMock(side_effect=RuntimeError("model unavailable"))
        engine.bind_ai_service(SimpleNamespace(select_conversation_candidates=selector))
        state = ConversationEngineState(user_key="selector-error")
        decision = SearchDecision(
            outcome=Outcome.RESULTS,
            products=[product("p1", "Рыболовная катушка")],
            report={},
        )
        strategy = SimpleNamespace(
            request=SimpleNamespace(normalized_query="рыболовная катушка", original_query=""),
            result_limit=30,
        )

        await engine._apply_ai_candidate_selection(decision, strategy, state)

        self.assertEqual(decision.outcome, Outcome.RESULTS)
        self.assertEqual([p.product_id for p in decision.products], ["p1"])
        self.assertEqual(decision.report["ai_selection"], "error_fallback")
        self.assertEqual(decision.report["reason"], "deterministic_fallback")


    async def test_selector_window_reserves_space_for_verified_candidates(self):
        engine = ConversationSearchEngine()
        state = ConversationEngineState(user_key="balanced-selector-window")
        candidates = [
            product(f"review-{i}", "Набор универсальных инструментов")
            for i in range(15)
        ] + [
            product(f"verified-{i}", "Рыболовная катушка спиннинговая")
            for i in range(15)
        ]
        decision = SearchDecision(
            outcome=Outcome.RESULTS,
            products=candidates,
            report={
                "semantic_review_ids": [f"review-{i}" for i in range(15)],
            },
        )
        strategy = SimpleNamespace(
            request=SimpleNamespace(
                normalized_query="рыболовная катушка",
                original_query="",
            ),
            result_limit=30,
        )
        observed = {}

        async def select_window(pool, **kwargs):
            observed["ids"] = [item.product_id for item in pool]
            return list(observed["ids"])

        selector = AsyncMock(side_effect=select_window)
        engine.bind_ai_service(SimpleNamespace(
            select_conversation_candidates=selector,
        ))

        await engine._apply_ai_candidate_selection(decision, strategy, state)

        self.assertEqual(len(observed["ids"]), AI_CONVERSATION_SELECTION_LIMIT)
        self.assertEqual(
            sum(pid.startswith("review-") for pid in observed["ids"]), 10
        )
        self.assertEqual(
            sum(pid.startswith("verified-") for pid in observed["ids"]), 10
        )
        self.assertEqual(decision.report["ai_review_in_window"], 10)
        self.assertEqual(decision.report["ai_ordinary_in_window"], 10)
        # The selector filled its 20-row window, so the five remaining
        # already-verified candidates can safely join the 20 selected rows.
        # The five unreviewed semantic candidates remain quarantined.
        self.assertEqual(len(decision.products), 25)
        self.assertEqual(
            sum(pid.startswith("verified-") for pid in [
                item.product_id for item in decision.products
            ]),
            15,
        )

    async def test_empty_search_gets_one_grounded_natural_followup(self):
        from services.convsearch.retrieval import BackendOutcome

        class EmptyBackend:
            async def retrieve(self, strategy, *, exclude=None, should_continue=None):
                return BackendOutcome(
                    ok=True,
                    reason="no_candidates",
                    products=[],
                    request=strategy.request,
                )

        reply = "Пока не удалось подобрать подходящий вариант. Какое пожелание можно немного ослабить?"
        composer = AsyncMock(return_value=reply)
        interpreter = AsyncMock(return_value=None)
        engine = ConversationSearchEngine(retrieval_backend=EmptyBackend())
        engine.bind_ai_service(SimpleNamespace(
            decide_conversation_turn=interpreter,
            compose_no_results_reply=composer,
        ))

        decision = await engine.handle_message(
            "no-results-user",
            "Нужен аккумуляторный шуруповёрт",
        )

        self.assertEqual(decision.outcome, Outcome.NO_CANDIDATES)
        self.assertEqual(decision.reply, reply)
        composer.assert_awaited_once()
        self.assertTrue(
            any(
                slot["kind"] == "type"
                for slot in composer.await_args.kwargs["active_slots"]
            )
        )


class ConstraintAndFusionRegressionTests(unittest.TestCase):
    def _product(self, pid: str, title: str, *, price=None, currency=None) -> Product:
        return Product(
            marketplace=Marketplace.ALIEXPRESS,
            product_id=pid,
            title=title,
            product_url=f"https://www.aliexpress.com/item/{pid}.html",
            price=price,
            currency=currency,
        )

    def test_budget_rejects_unknown_price_and_currency_mismatch(self) -> None:
        from services.convsearch.retrieval import filter_by_budget
        from services.convsearch.state import Slot

        state = ConversationEngineState(user_key="budget")
        state.slots.append(Slot(
            kind="budget", value="2000 UAH", source_turn=1, raw_text="до 2000 гривен"
        ))
        products = [
            self._product("uah-ok", "Товар", price=1800, currency="UAH"),
            self._product("uah-over", "Товар", price=2200, currency="UAH"),
            self._product("usd-unknown", "Товар", price=20, currency="USD"),
            self._product("price-unknown", "Товар", price=None, currency="UAH"),
            self._product("currency-unknown", "Товар", price=100, currency=None),
        ]

        survivors, rejected = filter_by_budget(products, state)

        self.assertEqual([p.product_id for p in survivors], ["uah-ok"])
        self.assertEqual(
            {p.product_id for p in rejected},
            {"uah-over", "usd-unknown", "price-unknown", "currency-unknown"},
        )

    def test_budget_without_currency_uses_active_search_currency(self) -> None:
        from services.convsearch.retrieval import filter_by_budget

        state = ConversationEngineState(user_key="budget-default-currency")
        state.slots.append(Slot(
            kind="budget", value="2000.0", source_turn=1,
            raw_text="до 2000",
        ))
        products = [
            self._product("uah-ok", "Товар", price=1800, currency="UAH"),
            self._product("uah-over", "Товар", price=2200, currency="UAH"),
            self._product("usd-ambiguous", "Товар", price=20, currency="USD"),
            self._product("unknown-currency", "Товар", price=100, currency=None),
        ]

        survivors, rejected = filter_by_budget(
            products, state, default_currency="UAH",
        )

        self.assertEqual([p.product_id for p in survivors], ["uah-ok"])
        self.assertEqual(
            {p.product_id for p in rejected},
            {"uah-over", "usd-ambiguous", "unknown-currency"},
        )

    def test_explicit_budget_currency_overrides_search_currency(self) -> None:
        from services.convsearch.retrieval import filter_by_budget

        state = ConversationEngineState(user_key="budget-explicit-currency")
        state.slots.append(Slot(
            kind="budget", value="20.0 USD", source_turn=1,
            raw_text="до 20 долларов",
        ))
        products = [
            self._product("usd-ok", "Товар", price=18, currency="USD"),
            self._product("uah-wrong-currency", "Товар", price=500, currency="UAH"),
        ]

        survivors, rejected = filter_by_budget(
            products, state, default_currency="UAH",
        )

        self.assertEqual([p.product_id for p in survivors], ["usd-ok"])
        self.assertEqual(
            [p.product_id for p in rejected],
            ["uah-wrong-currency"],
        )

    def test_explicit_budget_currency_controls_marketplace_request_currency(self) -> None:
        from services.convsearch.retrieval import build_retrieval_strategy

        state = ConversationEngineState(user_key="budget-request-currency")
        state.record_product_type("power bank")
        state.slots.append(Slot(
            kind="budget", value="50.0 USD", source_turn=1,
            raw_text="до 50 долларов",
        ))

        strategy = build_retrieval_strategy(
            state, "до 50 долларов", currency="UAH",
        )

        self.assertIsNotNone(strategy)
        self.assertEqual(strategy.request.currency, "USD")
        self.assertEqual(strategy.request.max_price, 50.0)

    def test_budget_without_currency_keeps_active_search_currency(self) -> None:
        from services.convsearch.retrieval import build_retrieval_strategy

        state = ConversationEngineState(user_key="budget-default-request-currency")
        state.record_product_type("power bank")
        state.slots.append(Slot(
            kind="budget", value="2000.0", source_turn=1,
            raw_text="до 2000",
        ))

        strategy = build_retrieval_strategy(
            state, "до 2000", currency="UAH",
        )

        self.assertIsNotNone(strategy)
        self.assertEqual(strategy.request.currency, "UAH")
        self.assertEqual(strategy.request.max_price, 2000.0)

    def test_budget_without_currency_fails_closed_without_search_currency(self) -> None:
        from services.convsearch.retrieval import filter_by_budget

        state = ConversationEngineState(user_key="budget-no-default-currency")
        state.slots.append(Slot(
            kind="budget", value="2000.0", source_turn=1, raw_text="до 2000",
        ))
        products = [
            self._product("uah", "Товар", price=1800, currency="UAH"),
        ]

        survivors, rejected = filter_by_budget(products, state)

        self.assertEqual(survivors, [])
        self.assertEqual([p.product_id for p in rejected], ["uah"])

    def test_partial_mandatory_feature_evidence_is_not_treated_as_verified(self) -> None:
        from services.convsearch.retrieval import (
            _feature_is_evidenced,
            partition_required_feature_evidence,
        )

        state = ConversationEngineState(user_key="partial-feature-evidence")
        state.slots.append(Slot(
            kind="feature",
            value="dual battery quick charging",
            source_turn=1,
            raw_text="dual battery quick charging",
        ))
        partial = self._product(
            "partial-feature",
            "Dual battery pack with quick power delivery",
        )
        complete = self._product(
            "complete-feature",
            "Dual battery quick charging support",
        )

        self.assertFalse(_feature_is_evidenced(partial, "dual battery quick charging"))
        self.assertTrue(_feature_is_evidenced(complete, "dual battery quick charging"))

        verified, needs_review = partition_required_feature_evidence(
            [partial, complete], state
        )
        self.assertEqual([p.product_id for p in verified], ["complete-feature"])
        self.assertEqual([p.product_id for p in needs_review], ["partial-feature"])

    def test_partial_mandatory_feature_stays_hidden_if_gemini_times_out(self) -> None:
        import asyncio

        async def run() -> None:
            engine = ConversationSearchEngine()
            state = ConversationEngineState(user_key="partial-feature-timeout")
            state.record_product_type("power bank")
            state.slots.append(Slot(
                kind="feature",
                value="dual battery quick charging",
                source_turn=1,
                raw_text="dual battery quick charging",
            ))
            candidate = self._product(
                "partial-feature-timeout",
                "Dual battery pack with quick power delivery",
            )
            strategy = SimpleNamespace(
                product_type="power bank",
                request=SimpleNamespace(
                    normalized_query="power bank dual battery quick charging",
                    original_query="",
                ),
                result_limit=30,
            )
            decision = engine._evaluate_pool(
                state, [candidate], strategy,
                refined=True, intent=MessageIntent.REFINE,
                observation={}, exclude_shown=False,
            )
            engine.bind_ai_service(SimpleNamespace(
                select_conversation_candidates=AsyncMock(side_effect=TimeoutError)
            ))

            await engine._apply_ai_candidate_selection(decision, strategy, state)

            self.assertEqual(decision.outcome, Outcome.NO_ELIGIBLE)
            self.assertEqual(decision.products, [])
            self.assertEqual(decision.report["reason"], "unverified_required_features")

        asyncio.run(run())


    def test_required_feature_needs_positive_evidence(self) -> None:
        from services.convsearch.retrieval import filter_by_required_features
        from services.convsearch.state import Slot

        state = ConversationEngineState(user_key="feature")
        state.slots.append(Slot(
            kind="feature", value="для дома", source_turn=1, raw_text="Для дома"
        ))
        products = [
            self._product("home", "Сварочный аппарат инверторный для дома"),
            self._product("industrial", "Сварочный аппарат промышленный"),
        ]

        survivors, rejected = filter_by_required_features(products, state)

        self.assertEqual([p.product_id for p in survivors], ["home"])
        self.assertEqual([p.product_id for p in rejected], ["industrial"])

    def test_compatibility_is_enforced_by_deterministic_required_feature_filter(self) -> None:
        from services.convsearch.retrieval import filter_by_required_features
        from services.convsearch.state import Slot

        state = ConversationEngineState(user_key="compatibility-filter")
        state.slots.append(Slot(
            kind="compatibility",
            value="iPhone 15",
            source_turn=1,
            raw_text="под iPhone 15",
        ))
        products = [
            self._product("iphone-case", "Phone case").model_copy(update={
                "description": "Compatible with iPhone 15",
            }),
            self._product("samsung-case", "Phone case").model_copy(update={
                "description": "Compatible with Samsung Galaxy",
            }),
            self._product("unknown-case", "Universal phone case"),
        ]

        survivors, rejected = filter_by_required_features(products, state)

        self.assertEqual([p.product_id for p in survivors], ["iphone-case"])
        self.assertEqual(
            {p.product_id for p in rejected},
            {"samsung-case", "unknown-case"},
        )

    def test_route_merge_uses_reciprocal_rank_fusion(self) -> None:
        from services.convsearch.retrieval import merge_route_results

        a = self._product("a", "Product A")
        b = self._product("b", "Product B")
        c = self._product("c", "Product C")

        merged = merge_route_results(
            [a, b, c],
            [b, a],
            [b],
        )

        self.assertEqual([p.product_id for p in merged], ["b", "a", "c"])
        self.assertIs(merged[0], b)

    def test_route_merge_respects_exclusions_and_stable_ties(self) -> None:
        from services.convsearch.retrieval import merge_route_results

        a = self._product("a", "Product A")
        b = self._product("b", "Product B")
        merged = merge_route_results([a, b], [b, a], exclude={"b"})
        self.assertEqual([p.product_id for p in merged], ["a"])


class SellerLanguageReviewContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_engine_passes_bounded_seller_phrase_to_semantic_review(self):
        engine = ConversationSearchEngine()
        state = ConversationEngineState(user_key="seller-language-review")
        candidate = product(
            "english-drill",
            "Cordless drill with 2 batteries",
        )
        decision = SearchDecision(
            outcome=Outcome.RESULTS,
            products=[candidate],
            report={"semantic_review_ids": ["english-drill"]},
        )
        strategy = SimpleNamespace(
            request=SimpleNamespace(
                normalized_query="аккумуляторный шуруповёрт",
                original_query="Нужен аккумуляторный шуруповёрт",
                intent_marketplace_terms=["cordless drill"],
            ),
            result_limit=30,
        )
        observed = {}

        async def selector(pool, **kwargs):
            observed["terms"] = kwargs.get("marketplace_terms")
            observed["identity_ids"] = kwargs.get("identity_review_ids")
            return ["english-drill"]

        engine.bind_ai_service(SimpleNamespace(
            select_conversation_candidates=AsyncMock(side_effect=selector),
        ))

        await engine._apply_ai_candidate_selection(decision, strategy, state)

        self.assertEqual(observed["terms"], ["cordless drill"])
        self.assertEqual(observed["identity_ids"], ["english-drill"])
        self.assertEqual([item.product_id for item in decision.products], ["english-drill"])
        self.assertEqual(decision.report["ai_selection"], "selected")


if __name__ == "__main__":
    unittest.main()


class UncertainTypeVerdictQuarantineTests(unittest.TestCase):
    """Every validator review verdict must stay hidden without Gemini."""

    def test_bare_description_and_cross_language_identity_are_quarantined(self):
        cases = (
            (
                "рыболовная катушка",
                product("bare-reel", "Катушка 5000").model_copy(update={
                    "description": "Рыболовная катушка для спиннинга",
                }),
                "bare_type_with_description_confirmation_requires_review",
            ),
            (
                "шуруповёрт",
                product("english-screwdriver", "Cordless screwdriver with 2 batteries"),
                "cross_language_identity_requires_semantic_review",
            ),
        )

        for index, (requested_type, candidate, expected_reason) in enumerate(cases):
            with self.subTest(requested_type=requested_type):
                from services.convsearch.type_validation import validate_product_type

                verdict = validate_product_type(candidate, requested_type)
                self.assertTrue(verdict.qualifies)
                self.assertIn(expected_reason, verdict.reasons)

                engine = ConversationSearchEngine()
                state = ConversationEngineState(user_key=f"uncertain-type-{index}")
                state.record_product_type(requested_type)
                strategy = SimpleNamespace(
                    product_type=requested_type,
                    request=SimpleNamespace(
                        normalized_query=requested_type,
                        original_query=requested_type,
                    ),
                    result_limit=30,
                )

                decision = engine._evaluate_pool(
                    state, [candidate], strategy,
                    refined=False, intent=MessageIntent.NEW_SEARCH,
                    observation={}, exclude_shown=False,
                )
                self.assertEqual(
                    decision.report["semantic_review_ids"],
                    [candidate.product_id],
                )
                self.assertEqual(decision.report["safe_ranked"], 0)

                # With no semantic selector, the uncertain listing must not
                # escape through the deterministic fallback or old shim.
                import asyncio
                asyncio.run(engine._apply_ai_candidate_selection(
                    decision, strategy, state,
                ))
                self.assertEqual(decision.outcome, Outcome.NO_ELIGIBLE)
                self.assertEqual(decision.products, [])

                shaped, _report = engine.shape_pool(
                    state, [candidate], requested_type,
                )
                self.assertEqual(shaped, [])


class BudgetLifecycleAcrossRefinementsTests(unittest.TestCase):
    def _state_with_budget(self, user_key):
        state = ConversationEngineState(user_key=user_key)
        state.record_product_type("шуруповёрт")
        state.slots.append(Slot(
            kind="budget", value="2000.0 UAH", source_turn=1, raw_text=""
        ))
        return state

    def test_new_budget_replaces_previous_even_when_turn_is_refine(self):
        state = self._state_with_budget("budget-replacement-refine")

        observation = state.observe(
            "А теперь до 3000 грн",
            intent_override=MessageIntent.REFINE,
        )
        state.record_slots(observation)

        self.assertEqual(state.active_values("budget"), ["3000.0 UAH"])
        self.assertEqual(
            [
                slot.status for slot in state.slots
                if slot.kind == "budget" and slot.value == "2000.0 UAH"
            ],
            ["superseded"],
        )

    def test_explicit_no_budget_removes_old_ceiling(self):
        for message in (
            "Бюджет не важен",
            "Цена не имеет значения",
            "Без ограничений по цене",
            "По цене ограничений нет",
            "No budget",
        ):
            with self.subTest(message=message):
                state = self._state_with_budget(
                    "budget-reset-" + str(len(message))
                )

                observation = state.observe(
                    message,
                    intent_override=MessageIntent.REFINE,
                )
                state.record_slots(observation)

                self.assertEqual(state.active_values("budget"), [])
                self.assertEqual(
                    [
                        slot.status for slot in state.slots
                        if slot.kind == "budget"
                    ],
                    ["superseded"],
                )

class ConversationalRecommendationIdentityRegressionTests(unittest.TestCase):
    """Recommendation wording must not be mistaken for a new product name."""

    def test_generic_recommendation_questions_have_no_product_identity(self):
        from services.convsearch.engine import _product_type_of

        for message in (
            "Какой бы ты посоветовал в пределах 1000 грн",
            "Что бы ты рекомендовал до 2000 грн",
            "Какой лучше выбрать?",
            "Помоги выбрать что-нибудь до 1000 грн",
            "Подскажи, что лучше взять?",
            "Which one would you recommend under $30?",
            "Can you help me choose one under $30?",
        ):
            with self.subTest(message=message):
                self.assertIsNone(_product_type_of(message))

    def test_explicitly_named_new_product_still_has_product_identity(self):
        from services.convsearch.engine import _product_type_of

        self.assertEqual(
            _product_type_of("Теперь найди шуруповёрт"),
            "шуруповерт",
        )

class RecommendationFollowupContinuityTests(unittest.IsolatedAsyncioTestCase):
    async def test_recommendation_budget_followup_keeps_active_product(self):
        from services.convsearch.retrieval import BackendOutcome

        observed = {}

        class EmptyBackend:
            async def retrieve(self, strategy, *, exclude=None, should_continue=None):
                observed["query"] = strategy.request.normalized_query
                return BackendOutcome(
                    ok=True,
                    reason="no_candidates",
                    products=[],
                    request=strategy.request,
                )

        class OvereagerAI:
            async def decide_conversation_turn(self, text, *, context):
                return {
                    "action": "new_search",
                    "confidence": 0.99,
                    "product_type": None,
                    "marketplace_terms": [],
                    "question": None,
                    "response": None,
                    "slot_updates": [],
                }

        engine = ConversationSearchEngine(retrieval_backend=EmptyBackend())
        engine.bind_ai_service(OvereagerAI())
        state = ConversationEngineState(user_key="recommendation-followup")
        state.record_product_type("лазерный уровень")

        decision = await engine.handle_message(
            "recommendation-followup",
            "Какой бы ты посоветовал в пределах 1000 грн",
            state=state,
        )

        self.assertEqual(decision.outcome, Outcome.NO_CANDIDATES)
        self.assertIn("лазерный уровень", observed["query"])
        self.assertNotIn("бы посоветовал", observed["query"])
        self.assertEqual(
            decision.report.get("intent"),
            MessageIntent.REFINE,
        )
        self.assertEqual(
            state.active_by_kind("type").value,
            "лазерный уровень",
        )

class SelectorIdentityScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_identity_review_ids_only_include_candidates_sent_to_gemini(self):
        engine = ConversationSearchEngine()
        reviewed = product("reviewed-in-pool", "Набор универсальных инструментов")
        ordinary = product("ordinary-in-pool", "Рыболовная катушка")
        selector = AsyncMock(return_value=[
            reviewed.product_id,
            ordinary.product_id,
        ])
        engine.bind_ai_service(SimpleNamespace(
            select_conversation_candidates=selector,
        ))
        state = ConversationEngineState(user_key="selector-identity-scope")
        decision = SearchDecision(
            outcome=Outcome.RESULTS,
            products=[reviewed, ordinary],
            report={
                "semantic_review_ids": [
                    reviewed.product_id,
                    "review-id-not-in-candidate-pool",
                ],
            },
        )
        strategy = SimpleNamespace(
            request=SimpleNamespace(
                normalized_query="рыболовная катушка",
                original_query="",
            ),
            result_limit=30,
        )

        await engine._apply_ai_candidate_selection(decision, strategy, state)

        self.assertEqual(
            selector.await_args.kwargs["identity_review_ids"],
            [reviewed.product_id],
        )
        self.assertEqual(
            [item.product_id for item in decision.products],
            [reviewed.product_id, ordinary.product_id],
        )

