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
from services.convsearch.engine import ConversationSearchEngine, AI_CONVERSATION_SELECTION_LIMIT
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


class EngineScenarioTests(unittest.TestCase):
    """The four mandatory regression scenarios, plus the state tests."""

    def setUp(self) -> None:
        self.engine = ConversationSearchEngine()

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

        self.assertEqual(len(selector.await_args.args[0]), AI_CONVERSATION_SELECTION_LIMIT)
        self.assertEqual([item.product_id for item in decision.products], ["p0"])
        self.assertEqual(decision.report["ai_selected"], 1)
        self.assertEqual(decision.report["ai_tail_added"], 0)

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

    async def test_selector_timeout_preserves_deterministically_validated_candidates_with_exclusion(self):
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

    async def test_selector_error_preserves_deterministic_safe_pool(self):
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


if __name__ == "__main__":
    unittest.main()
