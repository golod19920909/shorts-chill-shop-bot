"""The AI product menu's reasoning layer - no Gemini, no network.

``ProductAssistant`` is the only place that decides WHAT the user sees for
«Стоит ли покупать / Найти дешевле / Найти похожий / Подобрать лучше /
Задать свой вопрос». These tests pin the hard guarantees:

* every returned product is an instance that came from the candidate set
  (a model id outside it is rejected, never turned into a card);
* "cheaper" only compares the same currency at the same observed price;
* facts and computed observations are separated, and no number the
  marketplace did not report can reach the user;
* with no AI (or with a malformed AI answer) every action still answers
  deterministically from the same real data.
"""

from __future__ import annotations

import asyncio
import unittest

from models.product import Marketplace, Product
from models.search_request import SearchMode, SearchRequest
from services.product_assistant import (
    MAX_RESULTS_SHOWN,
    ProductAssistant,
    cheaper_candidates,
    deterministic_advice,
    intent_from_title,
    product_facts,
)
from services.search_service import SearchOutcome


def make_product(
    product_id: str,
    title: str,
    price: float | None = None,
    currency: str | None = "USD",
    **overrides,
) -> Product:
    data: dict = {
        "marketplace": Marketplace.ALIEXPRESS,
        "product_id": product_id,
        "title": title,
        "product_url": f"https://www.aliexpress.com/item/{product_id}.html",
        "price": price,
        "currency": currency,
    }
    data.update(overrides)
    return Product(**data)


class StubAI:
    """Scripted stand-in for :class:`AIService` (no client, no quota)."""

    def __init__(
        self,
        *,
        available: bool = True,
        analysis=None,
        ids=None,
        intent: str | None = None,
        answer: str | None = None,
    ) -> None:
        self.is_available = available
        self._analysis = analysis
        self._ids = ids
        self._intent = intent
        self._answer = answer
        self.calls: list[str] = []

    async def analyze_product(self, product):
        self.calls.append("analyze")
        return self._analysis

    async def select_candidates(self, goal, product, candidates):
        self.calls.append(f"select:{goal}")
        return self._ids

    async def plan_product_search(self, product, goal):
        self.calls.append(f"plan:{goal}")
        return self._intent

    async def answer_product_question(self, question, products):
        self.calls.append("question")
        return self._answer


class StubSearch:
    """The EXISTING search pipeline, replaced by fixed real candidates."""

    def __init__(self, products=None, error: Exception | None = None) -> None:
        self.products = list(products or [])
        self.error = error
        self.requests: list[SearchRequest] = []

    async def find_products(self, request: SearchRequest) -> SearchOutcome:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return SearchOutcome(
            request=request,
            products=list(self.products),
            searched_marketplaces=["AliExpress"],
        )


def run(coro):
    return asyncio.run(coro)


ORIGINAL = make_product(
    "orig",
    "Беспроводные наушники TWS с шумоподавлением",
    25.0,
    rating=4.5,
    orders_count=1200,
)


class ProductFactsTests(unittest.TestCase):
    """Only fields the marketplace actually reported reach the model."""

    def test_missing_fields_are_absent_not_zero(self) -> None:
        sparse = make_product("s", "Чехол", price=None, currency=None)
        facts = product_facts(sparse)

        self.assertNotIn("price", facts)
        self.assertNotIn("rating", facts)
        self.assertNotIn("orders_count", facts)
        self.assertNotIn("availability", facts)

    def test_urls_are_never_part_of_the_prompt(self) -> None:
        product = make_product(
            "u",
            "Наушники",
            10.0,
            affiliate_url="https://s.click.aliexpress.com/abc",
        )
        facts = product_facts(product)

        self.assertNotIn("product_url", facts)
        self.assertNotIn("affiliate_url", facts)
        self.assertNotIn("https", str(facts))

    def test_advice_separates_facts_from_calculations(self) -> None:
        product = make_product(
            "p", "Наушники", 20.0, old_price=25.0, shipping_cost=3.0
        )
        text = deterministic_advice(product)

        self.assertIn("Данные AliExpress", text)
        self.assertIn("Что посчитал бот", text)
        # The disclaimer is what keeps "worth buying" from becoming a promise.
        self.assertIn("не гарантия качества", text)


class AnalyzeTests(unittest.TestCase):
    def test_without_ai_the_answer_is_deterministic_and_factual(self) -> None:
        assistant = ProductAssistant(ai_service=StubAI(available=False))

        advice = run(assistant.analyze(ORIGINAL))

        self.assertFalse(advice.used_ai)
        self.assertIn("25.00 USD", advice.text)
        self.assertIn("4.5", advice.text)
        self.assertIn("1200", advice.text)

    def test_ai_analysis_is_rendered_on_top_of_the_real_facts(self) -> None:
        ai = StubAI(
            analysis={
                "observations": ["Рейтинг 4.5 выше среднего по выборке"],
                "recommendation": "Стоит посмотреть и этот вариант.",
            }
        )
        assistant = ProductAssistant(ai_service=ai)

        advice = run(assistant.analyze(ORIGINAL))

        self.assertTrue(advice.used_ai)
        self.assertIn("Данные AliExpress", advice.text)
        self.assertIn("Рейтинг 4.5", advice.text)

    def test_malformed_ai_analysis_falls_back_to_the_deterministic_block(
        self,
    ) -> None:
        assistant = ProductAssistant(ai_service=StubAI(analysis=None))

        advice = run(assistant.analyze(ORIGINAL))

        self.assertFalse(advice.used_ai)
        self.assertIn("Данные AliExpress", advice.text)


class CheaperTests(unittest.TestCase):
    """«На ть дешевле» compares real observed prices, in one currency."""

    def test_only_strictly_cheaper_same_currency_candidates(self) -> None:
        candidates = [
            make_product("cheap", "Наушники беспроводные", 15.0),
            make_product("equal", "Наушники беспроводные", 25.0),
            make_product("rich", "Наушники беспроводные", 40.0),
        ]
        result = cheaper_candidates(ORIGINAL, candidates)

        self.assertEqual([p.product_id for p in result], ["cheap"])

    def test_different_currencies_are_never_compared_numerically(self) -> None:
        # There is no documented conversion mechanism in this project, so a
        # price in another currency cannot be called "cheaper".
        candidates = [
            make_product("eur", "Наушники", 20.0, currency="EUR"),
            make_product("rub", "Наушники", 1000.0, currency="RUB"),
        ]

        self.assertEqual(cheaper_candidates(ORIGINAL, candidates), [])

    def test_missing_price_is_not_a_candidate(self) -> None:
        candidates = [make_product("unknown", "Наушники", None)]

        self.assertEqual(cheaper_candidates(ORIGINAL, candidates), [])

    def test_assistant_explains_when_prices_cannot_be_compared(self) -> None:
        assistant = ProductAssistant(ai_service=StubAI(available=False))
        candidates = [make_product("eur", "Наушники", 20.0, currency="EUR")]

        selection = run(assistant.cheaper(ORIGINAL, candidates))

        self.assertEqual(selection.products, [])
        self.assertEqual(selection.reason, "no_comparable_price")

    def test_results_are_capped_for_the_chat(self) -> None:
        candidates = [
            make_product(f"c{index}", "Наушники беспроводные", 10.0 + index)
            for index in range(10)
        ]
        assistant = ProductAssistant(ai_service=StubAI(available=False))

        selection = run(assistant.cheaper(ORIGINAL, candidates))

        self.assertLessEqual(len(selection.products), MAX_RESULTS_SHOWN)
        self.assertEqual(selection.reason, "ai_fallback")
        self.assertFalse(selection.used_ai)
        self.assertIn("Подбор без AI", selection.note)



class ProductIdSafetyTests(unittest.TestCase):
    """A model can order real ids; it can never create a product."""

    def test_unknown_ids_are_dropped_and_the_rest_survive(self) -> None:
        candidates = [
            make_product("a", "Наушники беспроводные", 10.0),
            make_product("b", "Наушники беспроводные", 12.0),
            make_product("c", "Наушники беспроводные", 14.0),
        ]
        assistant = ProductAssistant(ai_service=StubAI(ids=["b", "D", "a"]))

        selection = run(assistant.cheaper(ORIGINAL, candidates))

        self.assertEqual([p.product_id for p in selection.products], ["b", "a"])
        self.assertTrue(selection.used_ai)

    def test_a_fully_invented_id_list_yields_the_deterministic_order(self) -> None:
        candidates = [
            make_product("a", "Наушники беспроводные", 10.0),
            make_product("b", "Наушники беспроводные", 12.0),
        ]
        assistant = ProductAssistant(ai_service=StubAI(ids=["D", "E"]))

        selection = run(assistant.cheaper(ORIGINAL, candidates))

        self.assertEqual([p.product_id for p in selection.products], ["a", "b"])
        self.assertFalse(selection.used_ai)

    def test_a_duplicated_id_cannot_duplicate_a_card(self) -> None:
        candidates = [make_product("a", "Наушники беспроводные", 10.0)]
        assistant = ProductAssistant(ai_service=StubAI(ids=["a", "a", "a"]))

        selection = run(assistant.cheaper(ORIGINAL, candidates))

        self.assertEqual([p.product_id for p in selection.products], ["a"])

    def test_the_original_product_is_never_offered_back(self) -> None:
        candidates = [ORIGINAL, make_product("a", "Наушники", 10.0)]
        assistant = ProductAssistant(ai_service=StubAI(ids=["orig", "a"]))

        selection = run(assistant.similar(ORIGINAL, candidates))

        self.assertNotIn("orig", [p.product_id for p in selection.products])


class SimilarAndBetterTests(unittest.TestCase):
    def test_similar_ranking_is_deterministic_without_ai(self) -> None:
        candidates = [
            make_product("other", "Чехол для телефона", 5.0),
            make_product(
                "same", "Беспроводные наушники TWS шумоподавление", 24.0
            ),
        ]
        assistant = ProductAssistant(ai_service=StubAI(available=False))

        selection = run(assistant.similar(ORIGINAL, candidates))

        self.assertEqual(selection.products[0].product_id, "same")
        self.assertFalse(selection.used_ai)

    def test_better_needs_real_comparable_signals(self) -> None:
        # No rating and no orders anywhere: "better" cannot be established,
        # and the screen says so instead of guessing.
        blind = make_product("blind", "Наушники", 10.0)
        candidates = [make_product("other", "Наушники", 9.0)]
        assistant = ProductAssistant(ai_service=StubAI(available=False))

        selection = run(assistant.better(blind, candidates))

        self.assertEqual(selection.products, [])
        self.assertEqual(selection.reason, "no_criteria")

    def test_better_uses_reported_rating_and_orders(self) -> None:
        strong = make_product(
            "strong",
            "Наушники беспроводные",
            24.0,
            rating=4.8,
            orders_count=5000,
        )
        weak = make_product(
            "weak", "Наушники беспроводные", 24.0, rating=4.1, orders_count=10
        )
        original = make_product(
            "base", "Наушники", 25.0, rating=4.4, orders_count=100
        )
        assistant = ProductAssistant(ai_service=StubAI(available=False))

        selection = run(assistant.better(original, [strong, weak]))

        self.assertEqual([p.product_id for p in selection.products], ["strong"])



class QuestionTests(unittest.TestCase):
    """«Задать свой вопрос» answers from data or admits the data is absent."""

    def test_cheapest_question_is_answered_without_ai(self) -> None:
        ai = StubAI()
        assistant = ProductAssistant(ai_service=ai)
        products = [
            make_product("a", "Наушники", 20.0, orders_count=10),
            make_product("b", "Наушники", 12.0, orders_count=20),
        ]

        advice = run(assistant.answer("Какой вариант дешевле?", products))

        self.assertFalse(advice.used_ai)
        self.assertIn("12.00 USD", advice.text)
        self.assertEqual(ai.calls, [])

    def test_most_ordered_question_uses_real_order_counts(self) -> None:
        assistant = ProductAssistant(ai_service=StubAI(available=False))
        products = [
            make_product("a", "Наушники", 20.0, orders_count=10),
            make_product("b", "Наушники", 30.0, orders_count=900),
        ]

        advice = run(assistant.answer("Какой вариант имеет больше заказов?", products))

        self.assertIn("900", advice.text)

    def test_missing_information_is_reported_honestly(self) -> None:
        assistant = ProductAssistant(ai_service=StubAI(available=False))
        products = [make_product("a", "Наушники", 20.0)]

        advice = run(assistant.answer("А он подходит для ноутбука?", products))

        self.assertFalse(advice.used_ai)
        self.assertIn("В данных AliExpress этого нет", advice.text)

    def test_feature_question_uses_real_titles_only(self) -> None:
        assistant = ProductAssistant(ai_service=StubAI(available=False))
        products = [
            make_product(
                "mic", "Беспроводные наушники с микрофоном", 20.0
            ),
        ]

        advice = run(
            assistant.answer("Есть ли здесь вариант с микрофоном?", products)
        )

        # The answer is built from the REAL title of a REAL candidate, and the
        # question words themselves never appear as if they were a fact.
        self.assertIn("Беспроводные наушники с микрофоном", advice.text)
        self.assertIn("20.00 USD", advice.text)
        self.assertNotIn("В данных AliExpress этого нет", advice.text)

    def test_mixed_currencies_are_not_ranked_against_each_other(self) -> None:
        assistant = ProductAssistant(ai_service=StubAI(available=False))
        products = [
            make_product("usd", "Наушники", 20.0, currency="USD"),
            make_product("eur", "Наушники", 15.0, currency="EUR"),
        ]

        advice = run(assistant.answer("Что здесь самое дешевое?", products))

        self.assertIn("конвертации нет", advice.text)

    def test_ai_answer_is_used_only_when_the_data_cannot_answer(self) -> None:
        assistant = ProductAssistant(ai_service=StubAI(answer="В ответе нет поля о цвете."))
        products = [make_product("a", "Наушники", 20.0)]

        advice = run(assistant.answer("Какого они цвета?", products))

        self.assertTrue(advice.used_ai)
        self.assertIn("цвете", advice.text)


class CandidateGenerationTests(unittest.TestCase):
    """Candidates always come from the EXISTING search service."""

    def test_candidates_come_from_the_shared_search_pipeline(self) -> None:
        search = StubSearch(
            [make_product("a", "Наушники беспроводные", 10.0)]
        )
        assistant = ProductAssistant(ai_service=StubAI(available=False), search_service=search)

        candidates = run(assistant.candidate_products(ORIGINAL))

        self.assertEqual([p.product_id for p in candidates], ["a"])
        self.assertEqual(len(search.requests), 1)
        self.assertEqual(search.requests[0].mode, SearchMode.SEARCH)
        self.assertEqual(search.requests[0].currency, ORIGINAL.currency)

    def test_ai_may_only_rephrase_the_intent_not_invent_it(self) -> None:
        search = StubSearch([])
        assistant = ProductAssistant(
            ai_service=StubAI(intent="наушники беспроводные tws"),
            search_service=search,
        )

        run(assistant.candidate_products(ORIGINAL))

        self.assertEqual(
            search.requests[0].original_query, "наушники беспроводные tws"
        )

    def test_deterministic_intent_comes_from_the_real_title(self) -> None:
        intent = intent_from_title(ORIGINAL.title)

        self.assertIn("наушники", intent.lower())
        self.assertLessEqual(len(intent), 100)
        # Marketing noise from the title is not turned into a query.
        self.assertNotIn("скидка", intent.lower())

    def test_a_failing_candidate_search_yields_no_products_not_a_crash(self) -> None:
        assistant = ProductAssistant(
            ai_service=StubAI(available=False),
            search_service=StubSearch(error=RuntimeError("api down")),
        )

        self.assertEqual(run(assistant.candidate_products(ORIGINAL)), [])

    def test_no_search_service_means_no_candidates(self) -> None:
        assistant = ProductAssistant(ai_service=StubAI(available=False))

        self.assertEqual(run(assistant.candidate_products(ORIGINAL)), [])


if __name__ == "__main__":
    unittest.main()
