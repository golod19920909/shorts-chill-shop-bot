"""AI layer contracts, exercised with a fully stubbed model.

``_generate`` is replaced, so no Gemini request is ever made and no quota is
spent. What is under test is the code AROUND the model:

* the prompt is built from real ``Product`` fields and carries no secrets,
  no credentials and no URLs;
* the answer must be structured and validated;
* a number the marketplace did not report is rejected;
* a malformed / unavailable model degrades to the deterministic path;
* vision output must be a short query, not a product description.
"""

from __future__ import annotations

import asyncio
import json
import unittest

from models.product import Marketplace, Product
from services.ai_service import (
    DEFAULT_FAST_MODEL,
    DEFAULT_FALLBACK_MODEL,
    DEFAULT_MODEL,
    DEFAULT_VISION_MODEL,
    TOP_CANDIDATES,
    AIService,
)

SECRET_SENTINEL = "gemini-secret-must-not-leak"
TOKEN_SENTINEL = "999888777:BOT-TOKEN-SENTINEL"


def run(coro):
    return asyncio.run(coro)


def make_product(**overrides) -> Product:
    data: dict = {
        "marketplace": Marketplace.ALIEXPRESS,
        "product_id": "p-1",
        "title": "Беспроводные наушники TWS",
        "product_url": "https://www.aliexpress.com/item/1.html",
        "affiliate_url": "https://s.click.aliexpress.com/e1x",
        "price": 19.99,
        "currency": "USD",
        "rating": 4.6,
        "orders_count": 3200,
    }
    data.update(overrides)
    return Product(**data)


class StubbedAIService(AIService):
    """An ``AIService`` whose transport is replaced by a scripted answer."""

    def __init__(self, response: str | None) -> None:
        super().__init__(api_key=None)
        # Mark the service as configured so the public methods actually run
        # their prompt/validation path; ``_generate`` never leaves the process.
        self._client = object()
        self._response = response
        self.prompts: list[object] = []
        self.models: list[tuple[str, ...]] = []

    async def _generate(self, contents, *, models=None):
        self.prompts.append(contents)
        self.models.append(tuple(models or ()))
        return self._response


class AnalysisValidationTests(unittest.TestCase):
    def test_a_valid_analysis_is_returned(self) -> None:
        # Only figures the marketplace actually reported may appear: the
        # deterministic renderer adds the "из 5" scale itself, so the model's
        # own text may not introduce a "5" of its own.
        service = StubbedAIService(
            '{"observations": ["Рейтинг 4.6, заказов 3200"],'
            ' "recommendation": "Посмотрите этот вариант."}'
        )

        result = run(service.analyze_product(make_product()))

        self.assertEqual(result["observations"], ["Рейтинг 4.6, заказов 3200"])
        self.assertEqual(result["recommendation"], "Посмотрите этот вариант.")

    def test_the_prompt_contains_real_fields_and_no_secrets(self) -> None:
        service = StubbedAIService('{"observations": [], "recommendation": null}')

        run(service.analyze_product(make_product()))

        prompt = str(service.prompts[0])
        self.assertIn("19.99", prompt)
        self.assertIn("4.6", prompt)
        self.assertIn("3200", prompt)
        self.assertNotIn(SECRET_SENTINEL, prompt)
        self.assertNotIn(TOKEN_SENTINEL, prompt)
        # URLs are never shown to the model, so it cannot echo or invent one.
        self.assertNotIn("s.click.aliexpress.com", prompt)
        self.assertNotIn("product_url", prompt)

    def test_a_number_the_marketplace_never_sent_is_rejected(self) -> None:
        service = StubbedAIService(
            '{"observations": ["У товара 5 звезд"], "recommendation": null}'
        )

        self.assertIsNone(run(service.analyze_product(make_product())))

    def test_a_recommendation_with_an_invented_number_is_dropped(self) -> None:
        service = StubbedAIService(
            '{"observations": ["Рейтинг 4.6"], "recommendation": "Цена 5 долларов"}'
        )

        result = run(service.analyze_product(make_product()))

        self.assertEqual(result["observations"], ["Рейтинг 4.6"])
        self.assertIsNone(result["recommendation"])

    def test_malformed_json_falls_back(self) -> None:
        service = StubbedAIService("Конечно! Вотanalysis: хорошо")

        self.assertIsNone(run(service.analyze_product(make_product())))

    def test_an_unavailable_model_yields_nothing_to_invent_from(self) -> None:
        service = StubbedAIService(None)

        self.assertIsNone(run(service.analyze_product(make_product())))

    def test_without_a_client_nothing_is_requested(self) -> None:
        service = AIService(api_key=None)

        self.assertFalse(service.is_available)
        self.assertIsNone(run(service.analyze_product(make_product())))

class CandidateSelectionValidationTests(unittest.TestCase):
    def test_known_ids_are_returned_in_the_model_order(self) -> None:
        service = StubbedAIService('{"ids": ["b", "a"]}')

        result = run(
            service.select_candidates(
                "similar",
                make_product(),
                [make_product(product_id="a"), make_product(product_id="b")],
            )
        )

        self.assertEqual(result, ["b", "a"])

    def test_unknown_ids_reach_the_caller_which_rejects_them(self) -> None:
        # The service does not decide identity: it returns the raw order and
        # ``ProductAssistant`` validates it against the candidate set.
        service = StubbedAIService('{"ids": ["D", "a"]}')

        result = run(
            service.select_candidates(
                "similar", make_product(), [make_product(product_id="a")]
            )
        )

        self.assertEqual(result, ["D", "a"])

    def test_a_non_list_answer_is_rejected(self) -> None:
        service = StubbedAIService('{"ids": "a"}')

        self.assertIsNone(
            run(
                service.select_candidates(
                    "similar", make_product(), [make_product()]
                )
            )
        )

    def test_malformed_json_is_rejected(self) -> None:
        service = StubbedAIService("ids: a, b")

        self.assertIsNone(
            run(
                service.select_candidates(
                    "similar", make_product(), [make_product()]
                )
            )
        )

    def test_an_empty_selection_is_rejected(self) -> None:
        service = StubbedAIService('{"ids": []}')

        self.assertIsNone(
            run(
                service.select_candidates(
                    "similar", make_product(), [make_product()]
                )
            )
        )

    def test_the_selection_prompt_carries_no_urls(self) -> None:
        service = StubbedAIService('{"ids": ["a"]}')

        run(
            service.select_candidates(
                "similar", make_product(), [make_product()]
            )
        )

        prompt = str(service.prompts[0])
        self.assertIn("p-1", prompt)
        self.assertNotIn("s.click.aliexpress.com", prompt)



class QuestionValidationTests(unittest.TestCase):
    def test_an_answer_over_real_data_is_returned(self) -> None:
        service = StubbedAIService("У этого товара цена 19.99 USD.")

        answer = run(
            service.answer_product_question("Сколько стоит?", [make_product()])
        )

        self.assertIn("19.99", answer)

    def test_an_invented_number_is_rejected(self) -> None:
        service = StubbedAIService("Цена этого товара 2.99 USD.")

        self.assertIsNone(
            run(
                service.answer_product_question(
                    "Сколько стоит?", [make_product()]
                )
            )
        )

    def test_one_bad_number_no_longer_discards_a_correct_answer(self) -> None:
        # Observed live: a single stray figure made the whole answer vanish,
        # so the feature was effectively dead. Unsupported SENTENCES are
        # dropped; the verified ones are still shown.
        service = StubbedAIService(
            "Цена товара 19.99 USD. Обычно доставка занимает 30 дней."
        )

        answer = run(
            service.answer_product_question("Сколько стоит и как долго едет?", [make_product()])
        )

        self.assertIsNotNone(answer)
        self.assertIn("19.99", answer)
        self.assertNotIn("30 дней", answer)

    def test_every_sentence_invented_yields_no_answer_at_all(self) -> None:
        service = StubbedAIService(
            "Доставка 30 дней. Рейтинг 4.9 из 5. Вес 500 грамм."
        )

        self.assertIsNone(
            run(
                service.answer_product_question("Какая доставка?", [make_product()])
            )
        )

    def test_the_documented_no_data_answer_becomes_none(self) -> None:
        service = StubbedAIService("нет данных")

        self.assertIsNone(
            run(
                service.answer_product_question(
                    "А он подходит для ноутбука?", [make_product()]
                )
            )
        )

    def test_a_claim_the_data_cannot_answer_is_left_to_the_caller(self) -> None:
        # A claim without numbers cannot be caught by the numeric gate, so the
        # deterministic layer answers first whenever the fields can answer,
        # and its honest "no such data" wins over a guess.
        from services.product_assistant import deterministic_answer

        self.assertIsNone(
            deterministic_answer("А он подходит для ноутбука?", [make_product()])
        )

    def test_an_empty_question_is_refused(self) -> None:
        service = StubbedAIService("что угодно")

        self.assertIsNone(
            run(service.answer_product_question("   ", [make_product()]))
        )

    def test_no_products_means_no_request(self) -> None:
        service = StubbedAIService("ответ")

        self.assertIsNone(run(service.answer_product_question("вопрос?", [])))
        self.assertEqual(service.prompts, [])


class SearchPlanningValidationTests(unittest.TestCase):
    def test_a_short_plain_query_is_accepted(self) -> None:
        service = StubbedAIService('"беспроводные наушники"')

        planned = run(
            service.plan_product_search(make_product(), "similar")
        )

        self.assertEqual(planned, "беспроводные наушники")

    def test_a_planned_link_is_rejected(self) -> None:
        service = StubbedAIService(
            "наушники https://www.aliexpress.com/item/1.html"
        )

        self.assertIsNone(
            run(service.plan_product_search(make_product(), "similar"))
        )

    def test_an_overlong_plan_is_rejected(self) -> None:
        service = StubbedAIService("наушники " + "очень " * 40)

        self.assertIsNone(
            run(service.plan_product_search(make_product(), "similar"))
        )

    def test_the_planning_prompt_contains_no_urls(self) -> None:
        service = StubbedAIService("наушники")

        run(service.plan_product_search(make_product(), "cheaper"))

        self.assertNotIn("s.click.aliexpress.com", str(service.prompts[0]))


class ModelChainTests(unittest.TestCase):
    def test_product_analysis_uses_the_smart_model(self) -> None:
        service = StubbedAIService('{"observations": [], "recommendation": null}')
        service.smart_model = "smart-model"
        service.fast_model = "fast-model"

        run(service.analyze_product(make_product()))

        self.assertEqual(service.models[-1][0], "smart-model")

    def test_selection_uses_the_fast_model(self) -> None:
        service = StubbedAIService('{"ids": ["a"]}')
        service.fast_model = "fast-model"

        run(
            service.select_candidates(
                "similar", make_product(), [make_product()]
            )
        )

        self.assertEqual(service.models[-1][0], "fast-model")

    def test_feed_selection_uses_the_fast_model(self) -> None:
        service = StubbedAIService('{"ids": ["p-1"]}')
        service.fast_model = "fast-model"

        run(service.select_feed_candidates([make_product()], query="наушники"))

        self.assertEqual(service.models[-1][0], "fast-model")

    def test_the_three_primary_roles_share_one_documented_model(self) -> None:
        # One generation for parsing, selection and photo reading; the fallback
        # stays a different, cheaper model - it is a safety net, not a role.
        self.assertEqual(DEFAULT_MODEL, "gemini-3.8-flash")
        self.assertEqual(DEFAULT_FAST_MODEL, "gemini-3.8-flash")
        self.assertEqual(DEFAULT_VISION_MODEL, "gemini-3.8-flash")
        self.assertEqual(DEFAULT_FALLBACK_MODEL, "gemini-3.1-flash-lite")


class StrictSelectionTests(unittest.TestCase):
    """«СТРОГО 20 лучших» and the shared junk filter of a selection (§3).

    The pool handed to the model is the RAW marketplace answer (60-80 listings in
    production, see ``search_service.RAW_POOL_TARGET``), so what is under test
    here is the code AROUND that answer: the cap, the validation of ids and the
    rules the prompt really carries. No request ever leaves the process.
    """

    @staticmethod
    def pool(count: int = 30) -> list[Product]:
        return [
            make_product(
                product_id=f"p-{index}",
                title=f"Беспроводные наушники TWS {index}",
            )
            for index in range(count)
        ]

    def test_the_feed_selection_is_capped_at_twenty_in_code(self) -> None:
        # The model lists all 30 candidates: the CAP, not the prompt, is what
        # keeps the carousel at the agreed size.
        service = StubbedAIService(json.dumps({"ids": [f"p-{i}" for i in range(30)]}))

        chosen = run(service.select_feed_candidates(self.pool(30), query="наушники"))

        self.assertEqual(len(chosen), TOP_CANDIDATES)
        self.assertEqual(chosen[0], "p-0")
        self.assertEqual(chosen[-1], f"p-{TOP_CANDIDATES - 1}")

    def test_the_selection_prompt_carries_the_strict_rules(self) -> None:
        service = StubbedAIService('{"ids": ["p-0"]}')

        run(service.select_feed_candidates(self.pool(3), query="наушники беспроводные"))

        prompt = str(service.prompts[-1])
        # The user's own words make "not the product" decidable.
        self.assertIn("наушники беспроводные", prompt)
        # ...and every rule of the strict filter is really in the prompt.
        self.assertIn("4.5", prompt)
        self.assertIn("аксессуары", prompt)
        self.assertIn("аномально низкой ценой", prompt)
        self.assertIn("20", prompt)
        # Every candidate travelled: the model cannot reject what it never saw.
        for index in range(3):
            self.assertIn(f"p-{index}", prompt)

    def test_an_unavailable_service_selects_nothing(self) -> None:
        # No client means no selection: the caller keeps its deterministic list.
        service = AIService(api_key=None)

        self.assertIsNone(run(service.select_feed_candidates(self.pool(3), query="x")))
        self.assertEqual(service.rank_fallback, 1)

    def test_a_repeated_id_cannot_take_two_slots(self) -> None:
        service = StubbedAIService('{"ids": ["p-0", "p-0", "p-1"]}')

        chosen = run(service.select_feed_candidates(self.pool(3), query="x"))

        self.assertEqual(chosen, ["p-0", "p-1"])

    def test_an_answer_without_a_usable_id_list_is_rejected(self) -> None:
        for answer in (
            '{"ids": "p-0"}',
            '{"ids": []}',
            '{"order": ["p-0"]}',
            "не json",
        ):
            with self.subTest(answer=answer):
                service = StubbedAIService(answer)

                self.assertIsNone(
                    run(service.select_feed_candidates(self.pool(3), query="x"))
                )

    def test_the_discovery_ranking_is_a_capped_selection_too(self) -> None:
        service = StubbedAIService(
            json.dumps({"order": [f"p-{i}" for i in range(30)]})
        )

        order = run(service.rank_candidates(self.pool(30), query="наушники для бега"))

        self.assertEqual(len(order), TOP_CANDIDATES)
        prompt = str(service.prompts[-1])
        self.assertIn("наушники для бега", prompt)
        self.assertIn("СТРОГО не более 20", prompt)

    def test_the_assistant_selection_uses_the_same_rules_and_cap(self) -> None:
        service = StubbedAIService(json.dumps({"ids": [f"p-{i}" for i in range(30)]}))

        chosen = run(service.select_candidates("similar", make_product(), self.pool(30)))

        self.assertEqual(len(chosen), TOP_CANDIDATES)
        prompt = str(service.prompts[-1])
        self.assertIn("4.5", prompt)
        self.assertIn("Беспроводные наушники TWS", prompt)

    def test_a_thin_pool_is_never_padded_to_twenty(self) -> None:
        # The number is a CEILING, not a quota: a short answer stays short (a
        # product the pool does not contain is dropped later, by
        # ``feed_curation.apply_selection`` - never invented here).
        service = StubbedAIService('{"ids": ["p-0", "p-1"]}')

        chosen = run(service.select_feed_candidates(self.pool(2), query="x"))

        self.assertEqual(chosen, ["p-0", "p-1"])


if __name__ == "__main__":
    unittest.main()
