"""Tests for the conversational search engine (deterministic fixtures).

The engine is tested with a fixed ``Product`` pool — no network, no live
Gemini, no live AliExpress. Every scenario of the integration brief maps to
a pool of REAL ``Product`` rows (the bot's own schema) plus the engine's
own ledger.
"""

from __future__ import annotations

import unittest

from models.product import Marketplace, Product
from services.convsearch.engine import ConversationSearchEngine
from services.convsearch.state import ConversationEngineState, MessageIntent


def product(pid: str, title: str, *, price: float = 25.0, orders: int = 10) -> Product:
    return Product(
        marketplace=Marketplace.ALIEXPRESS,
        product_id=pid,
        title=title,
        product_url=f"https://www.aliexpress.com/item/{pid}.html",
        price=price,
        currency="USD",
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
        product("hh-1", "Органайзер для дома настольный", price=12.0, orders=90),
        product("hh-2", "Органайзер для дома стеклянный", price=20.0, orders=60),
        product("hh-3", "Органайзер для дома металлический", price=25.0, orders=45),
        product("hh-expensive", "Робот-пылесос для дома", price=120.0, orders=300),
    ]


class EngineScenarioTests(unittest.TestCase):
    """The four mandatory regression scenarios, plus the state tests."""

    def setUp(self) -> None:
        self.engine = ConversationSearchEngine()

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
