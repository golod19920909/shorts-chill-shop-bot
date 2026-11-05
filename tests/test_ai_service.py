"""Tests for the local query parser and Gemini-output validation.

The local parser runs when no ``GEMINI_API_KEY`` is configured; the
validation tests call the parsing helpers directly with synthetic
Gemini output. Nothing here touches the network.
"""

from __future__ import annotations

import asyncio
import json
import unittest

from models.product import Marketplace, Product
from models.search_request import SearchRequest, SortPreference
from services.ai_service import AIService


class LocalFallbackParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = AIService(api_key=None)

    def parse(self, text: str) -> SearchRequest:
        return asyncio.run(self.service.parse_query(text))

    def test_service_reports_itself_unavailable_without_key(self) -> None:
        self.assertFalse(self.service.is_available)

    def test_empty_query_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.parse("   ")

    def test_extracts_max_price_and_currency(self) -> None:
        query = "Find wireless headphones under $30 with good reviews"
        request = self.parse(query)

        self.assertEqual(request.max_price, 30.0)
        self.assertEqual(request.currency, "USD")
        self.assertEqual(request.original_query, query)

    def test_does_not_guess_min_rating_from_fuzzy_phrases(self) -> None:
        # "good reviews" is not a number: inferring a rating from it
        # would be guessing, which the project forbids.
        request = self.parse("Find wireless headphones under $30 with good reviews")
        self.assertIsNone(request.min_rating)

    def test_extracts_explicit_min_rating(self) -> None:
        request = self.parse("dash camera with rating above 4.5")
        self.assertEqual(request.min_rating, 4.5)

    def test_extracts_quantity(self) -> None:
        request = self.parse("Find 100 pieces of wireless earbuds")

        self.assertEqual(request.quantity, 100)

    def test_detects_min_orders(self) -> None:
        request = self.parse("phone case with 500 orders")
        self.assertEqual(request.min_orders, 500)

    def test_removed_wholesale_wording_does_not_invent_fields(self) -> None:
        # The wholesale/B2B mode was removed together with Alibaba: such
        # wording must parse normally and must not be turned into other
        # values (no invented quantity, no marketplace preference).
        for text in (
            "найди поставщика беспроводных наушников",
            "bulk purchase of earbuds",
            "нужен товар оптом",
            "supplier with low MOQ",
        ):
            with self.subTest(text=text):
                request = self.parse(text)
                self.assertIsNone(request.quantity)
                self.assertEqual(request.preferred_marketplaces, [])

    def test_detects_named_marketplace(self) -> None:
        request = self.parse("find headphones on aliexpress")
        self.assertEqual(request.preferred_marketplaces, [Marketplace.ALIEXPRESS])

    def test_sorts_by_cheapest(self) -> None:
        request = self.parse("show the cheapest wireless mouse")
        self.assertEqual(request.sort_preference, SortPreference.PRICE_ASC)

    def test_normalized_query_excludes_price_constraints(self) -> None:
        request = self.parse("wireless headphones under $30")
        self.assertNotIn("30", request.normalized_query or "")
        self.assertIn("headphones", request.normalized_query or "")

    def test_price_without_marker_is_not_treated_as_a_limit(self) -> None:
        request = self.parse("10000mah power bank")
        self.assertIsNone(request.max_price)

    def test_russian_max_price(self) -> None:
        request = self.parse("наушники беспроводные до 3000 рублей")
        self.assertEqual(request.max_price, 3000.0)
        self.assertEqual(request.currency, "RUB")

    def test_russian_quality_phrase_is_removed_from_the_catalog_query(self) -> None:
        # Regression (verified live): the marketplace full-text index answers
        # an EMPTY result (resp_code=405) for "с хорошими отзывами", so the
        # phrase must never reach the catalog query. Without this, the very
        # example the welcome message advertises returned nothing at all -
        # while the same query without the phrase returned 50 real products.
        request = self.parse("беспроводные наушники до $30 с хорошими отзывами")

        self.assertEqual(request.normalized_query, "беспроводные наушники")
        self.assertEqual(request.max_price, 30.0)

    def test_russian_rating_phrase_is_removed_from_the_catalog_query(self) -> None:
        # Same live cause as above: "с хорошим рейтингом" empties the result.
        request = self.parse("наушники с хорошим рейтингом")

        self.assertEqual(request.normalized_query, "наушники")


class AIOutputValidationTests(unittest.TestCase):
    """Gemini output is validated and filtered before it is trusted."""

    def test_json_fenced_block_is_parsed(self) -> None:
        raw = '```json\n{"max_price": 25, "normalized_query": "headphones"}\n```'
        payload = AIService._extract_json_object(raw)
        self.assertEqual(payload, {"max_price": 25, "normalized_query": "headphones"})

    def test_json_embedded_in_prose_is_parsed(self) -> None:
        raw = 'Конечно! Вот параметры: {"max_price": 25} надеюсь, помог.'
        self.assertEqual(AIService._extract_json_object(raw), {"max_price": 25})

    def test_non_json_output_returns_none(self) -> None:
        self.assertIsNone(AIService._extract_json_object("не JSON вообще"))

    def test_unknown_fields_are_dropped(self) -> None:
        # A hallucinating model must not be able to inject product data.
        request = AIService._to_search_request(
            {
                "max_price": 20,
                "invented_field": "ignored",
                "products": [{"title": "fake"}],
            },
            fallback_text="headphones",
        )
        self.assertEqual(request.max_price, 20.0)
        self.assertEqual(request.original_query, "headphones")

    def test_invalid_sort_preference_falls_back_to_default(self) -> None:
        request = AIService._to_search_request(
            {"sort_preference": "best_seller_ever"}, fallback_text="headphones"
        )
        self.assertEqual(request.sort_preference, SortPreference.RELEVANCE)

    def test_valid_sort_preference_is_normalized(self) -> None:
        request = AIService._to_search_request(
            {"sort_preference": "PRICE_ASC"}, fallback_text="headphones"
        )
        self.assertEqual(request.sort_preference, SortPreference.PRICE_ASC)

    def test_model_noise_is_stripped_from_the_catalog_query(self) -> None:
        # Regression (verified live): a model can keep a quality phrase in
        # ``normalized_query``; such a query makes the marketplace answer an
        # empty result, so the deterministic cleaner is applied to the
        # model's own query as well (not only to the local parser's).
        request = AIService._to_search_request(
            {"normalized_query": "беспроводные наушники с хорошими отзывами"},
            fallback_text="беспроводные наушники до $30 с хорошими отзывами",
        )

        self.assertEqual(request.normalized_query, "беспроводные наушники")

    def test_no_results_reply_is_natural_plain_text_with_one_question(self) -> None:
        service = AIService(api_key=None)
        service._client = object()

        async def fake_generate(contents, *, models=None, operation="generate"):
            self.assertEqual(operation, "no_results_reply")
            self.assertEqual(models, (service.fast_model,))
            self.assertIn("не удалось подобрать", contents)
            return "Пока не вижу подходящего совпадения. Какое из пожеланий можно немного ослабить?"

        service._generate = fake_generate
        reply = asyncio.run(service.compose_no_results_reply(
            "Нужен аккумуляторный инструмент",
            active_slots=[{"kind": "budget", "value": "2500 UAH"}],
            reason="no_eligible",
        ))

        self.assertEqual(
            reply,
            "Пока не вижу подходящего совпадения. Какое из пожеланий можно немного ослабить?",
        )

    def test_no_results_reply_rejects_unbounded_or_non_question_output(self) -> None:
        service = AIService(api_key=None)
        service._client = object()

        async def fake_generate(contents, *, models=None, operation="generate"):
            return "Поиск завершён. Подходящих товаров нет."

        service._generate = fake_generate
        reply = asyncio.run(service.compose_no_results_reply(
            "Нужен инструмент",
            active_slots=[],
            reason="no_candidates",
        ))

        self.assertIsNone(reply)

    def test_valid_catalog_query_is_left_untouched(self) -> None:
        request = AIService._to_search_request(
            {"normalized_query": "wireless headphones"}, fallback_text="наушники"
        )

        self.assertEqual(request.normalized_query, "wireless headphones")

    def test_a_fully_stripped_catalog_query_keeps_its_original_value(self) -> None:
        # Cleaning must never throw a usable request away: when nothing but
        # noise is left, the original value is preserved.
        request = AIService._to_search_request(
            {"normalized_query": "с хорошими отзывами"}, fallback_text="наушники"
        )

        self.assertEqual(request.normalized_query, "с хорошими отзывами")

    def test_context_phrases_never_become_a_literal_feature_filter(self) -> None:
        # Regression (found live): the model turned «для жены» into a
        # ``required_features`` entry, and every listing that worded the same
        # intention as «Подарок женщине» was filtered out of the page. A
        # context-of-use phrase stays intent (and its words stay in the
        # catalogue query), never a literal inclusion filter.
        request = AIService._to_search_request(
            {
                "normalized_query": "gift for wife",
                "required_features": ["для жены", "титановый", "в виде сердца"],
            },
            fallback_text="титановый брелок для жены",
        )

        self.assertEqual(request.required_features, ["титановый"])

    def test_invalid_marketplace_names_are_filtered_out(self) -> None:
        request = AIService._to_search_request(
            {"preferred_marketplaces": ["aliexpress", "amazon"]},
            fallback_text="headphones",
        )
        self.assertEqual(request.preferred_marketplaces, [Marketplace.ALIEXPRESS])

    def test_invalid_values_fall_back_to_local_parser(self) -> None:
        request = AIService._to_search_request(
            {"max_price": "not a number"}, fallback_text="headphones under $30"
        )
        # The fallback parser reads the explicit "$30" from the raw text.
        self.assertEqual(request.max_price, 30.0)

    def test_rank_payload_contains_only_real_fields(self) -> None:
        product = Product(
            marketplace=Marketplace.ALIEXPRESS,
            product_id="ae-1",
            title="Earbuds",
            product_url="https://example.com/ae-1",
            price=12.5,
            currency="USD",
        )
        payload = AIService._rank_payload(product)

        self.assertEqual(payload["id"], "ae-1")
        self.assertEqual(payload["price"], 12.5)
        # No URLs are sent to the model, so it cannot invent links.
        self.assertNotIn("product_url", payload)
        self.assertNotIn("affiliate_url", payload)

    def test_comparison_requires_products_and_client(self) -> None:
        service = AIService(api_key=None)
        self.assertFalse(service.is_available)

    def test_ranking_is_skipped_without_a_client(self) -> None:
        service = AIService(api_key=None)
        product = Product(
            marketplace=Marketplace.ALIEXPRESS,
            product_id="ae-1",
            title="Earbuds",
            product_url="https://example.com/ae-1",
            price=12.5,
            currency="USD",
        )
        self.assertIsNone(asyncio.run(service.rank_candidates([product])))


class ProductQuestionContextCoverageTests(unittest.TestCase):
    """A question about shown cards must not silently ignore cards 9–30."""

    def test_question_prompt_includes_all_products_on_a_default_page(self) -> None:
        service = AIService(api_key=None)
        service._client = object()
        captured: list[str] = []

        async def fake_generate(contents, *, models=None, operation="generate"):
            captured.append(contents)
            return "нет данных"

        service._generate = fake_generate
        products = [
            Product(
                marketplace=Marketplace.ALIEXPRESS,
                product_id=f"page-{index}",
                title=f"Product number {index}",
                product_url=f"https://example.com/{index}",
                price=float(index + 1),
                currency="USD",
            )
            for index in range(30)
        ]

        result = asyncio.run(
            service.answer_product_question("Compare all shown products", products)
        )

        self.assertIsNone(result)
        self.assertEqual(len(captured), 1)
        self.assertIn('"id": "page-0"', captured[0])
        self.assertIn('"id": "page-29"', captured[0])



class GroundedProductQuestionTests(unittest.TestCase):
    def test_missing_selection_criteria_produces_one_clarifying_question(self) -> None:
        service = self._service(
            '{"claims":[],"recommendation":{"product_id":null,"evidence":[]},'
            '"clarification":"Что для вас важнее — цена или автономность?"}'
        )
        answer = asyncio.run(
            service.answer_product_question("Какой из них лучше?", self._products())
        )
        self.assertEqual(answer, "Что для вас важнее — цена или автономность?")

    def test_clarification_with_multiple_questions_or_links_is_rejected(self) -> None:
        for question in (
            "Что важнее? Цена или автономность?",
            "Что важнее? https://example.com",
        ):
            response = json.dumps(
                {
                    "claims": [],
                    "recommendation": {"product_id": None, "evidence": []},
                    "clarification": question,
                }
            )
            service = self._service(response)
            self.assertIsNone(asyncio.run(
                service.answer_product_question("Какой лучше?", self._products())
            ))

    def test_recommendation_requires_an_exact_quote_from_the_selected_product(self) -> None:
        service = self._service(
            '{"claims":[],"recommendation":{"product_id":"item-1","evidence":['
            '{"field":"description","quote":"Includes two batteries"}]}}'
        )
        answer = asyncio.run(
            service.answer_product_question("Какой лучше для работы без розетки?", self._products())
        )
        self.assertIn("наиболее подходящим выглядит", answer)
        self.assertIn("Cordless drill", answer)
        self.assertIn("Includes two batteries", answer)

    def test_recommendation_with_invented_evidence_is_not_accepted(self) -> None:
        service = self._service(
            '{"claims":[],"recommendation":{"product_id":"item-1","evidence":['
            '{"field":"description","quote":"Includes five batteries"}]}}'
        )
        self.assertIsNone(asyncio.run(
            service.answer_product_question("Какой лучше?", self._products())
        ))

    def test_malformed_recommendation_field_is_ignored_safely(self) -> None:
        service = self._service(
            '{"claims":[],"recommendation":{"product_id":"item-1","evidence":['
            '{"field":["description"],"quote":"Includes two batteries"}]}}'
        )
        self.assertIsNone(asyncio.run(
            service.answer_product_question("Какой лучше?", self._products())
        ))

    def test_recommendation_with_unknown_product_id_is_not_accepted(self) -> None:
        service = self._service(
            '{"claims":[],"recommendation":{"product_id":"invented","evidence":['
            '{"field":"description","quote":"Includes two batteries"}]}}'
        )
        self.assertIsNone(asyncio.run(
            service.answer_product_question("Какой лучше?", self._products())
        ))

    def test_slow_product_question_returns_to_honest_fallback_within_budget(self) -> None:
        service = AIService(api_key=None, timeout_seconds=0.01)
        service._client = object()

        async def slow_generate(contents, *, models=None, operation="generate"):
            await asyncio.sleep(0.1)
            return '{"claims":[]}'

        service._generate = slow_generate
        result = asyncio.run(
            service.answer_product_question("Which one is better?", [
                Product(
                    marketplace=Marketplace.ALIEXPRESS,
                    product_id="item-1",
                    title="Cordless drill",
                    product_url="https://example.com/item-1",
                )
            ])
        )

        self.assertIsNone(result)

    def _service(self, response: str) -> AIService:
        service = AIService(api_key=None)
        service._client = object()

        async def fake_generate(contents, *, models=None, operation="generate"):
            self.assertIn('"claims"', contents)
            return response

        service._generate = fake_generate
        return service

    def _products(self) -> list[Product]:
        return [
            Product(
                marketplace=Marketplace.ALIEXPRESS,
                product_id="item-1",
                title="Cordless drill",
                description="Includes two batteries and a charger",
                product_url="https://example.com/item-1",
            )
        ]

    def test_answer_is_built_from_exact_listing_quote(self) -> None:
        service = self._service(
            '{"claims":[{"product_id":"item-1","field":"description",'
            '"quote":"Includes two batteries"}]}'
        )
        answer = asyncio.run(
            service.answer_product_question("У какого товара два аккумулятора?", self._products())
        )
        self.assertIn("Includes two batteries", answer)
        self.assertIn("Cordless drill", answer)
        self.assertNotIn("имеет две батареи", answer)

    def test_malformed_claim_field_is_ignored_safely(self) -> None:
        service = self._service(
            '{"claims":[{"product_id":"item-1","field":["description"],'
            '"quote":"Includes two batteries"}]}'
        )
        self.assertIsNone(asyncio.run(
            service.answer_product_question("У какого товара два аккумулятора?", self._products())
        ))

    def test_invented_quote_is_rejected(self) -> None:
        service = self._service(
            '{"claims":[{"product_id":"item-1","field":"description",'
            '"quote":"Includes three batteries"}]}'
        )
        self.assertIsNone(asyncio.run(
            service.answer_product_question("У какого товара три аккумулятора?", self._products())
        ))

    def test_unknown_product_id_is_rejected(self) -> None:
        service = self._service(
            '{"claims":[{"product_id":"invented","field":"title","quote":"Cordless drill"}]}'
        )
        self.assertIsNone(asyncio.run(
            service.answer_product_question("Что за товар?", self._products())
        ))

class ConversationSelectionEvidenceTests(unittest.TestCase):
    def _service(self, response: str) -> AIService:
        service = AIService(api_key=None)
        service._client = object()
        service.captured_prompts = []

        async def fake_generate(contents, *, models=None, operation="generate"):
            service.captured_prompts.append(contents)
            self.assertEqual(operation, "conversation_selection")
            self.assertIn('"selections"', contents)
            return response

        service._generate = fake_generate
        return service

    def _product(self) -> Product:
        return Product(
            marketplace=Marketplace.ALIEXPRESS,
            product_id="drill-1",
            title="Cordless screwdriver with 2 batteries",
            product_url="https://example.com/drill-1",
        )

    def test_selection_requires_exact_listing_quote_for_mandatory_feature(self) -> None:
        service = self._service(
            '{"selections":[{"id":"drill-1","identity_evidence":"Cordless screwdriver",'
            '"requirement_evidence":{"двумя аккумуляторами":"with 2 batteries"}}]}'
        )
        result = asyncio.run(service.select_conversation_candidates(
            [self._product()],
            query="cordless screwdriver with two batteries",
            required_features=["двумя аккумуляторами"],
            identity_review_ids=["drill-1"],
        ))
        self.assertEqual(result, ["drill-1"])

    def test_selection_rejects_invented_or_missing_feature_evidence(self) -> None:
        service = self._service(
            '{"selections":[{"id":"drill-1","identity_evidence":"Cordless screwdriver",'
            '"requirement_evidence":{"двумя аккумуляторами":"includes two batteries"}}]}'
        )
        result = asyncio.run(service.select_conversation_candidates(
            [self._product()],
            query="cordless screwdriver with two batteries",
            required_features=["двумя аккумуляторами"],
            identity_review_ids=["drill-1"],
        ))
        self.assertIsNone(result)

    def test_every_mandatory_feature_needs_its_own_listing_quote(self) -> None:
        service = self._service(
            '{"selections":[{"id":"drill-1","identity_evidence":"Cordless screwdriver",'
            '"requirement_evidence":{"двумя аккумуляторами":"with 2 batteries"}}]}'
        )
        result = asyncio.run(service.select_conversation_candidates(
            [self._product()],
            query="cordless screwdriver",
            required_features=["двумя аккумуляторами", "быстрой зарядкой"],
            identity_review_ids=["drill-1"],
        ))
        self.assertIsNone(result)

    def test_evidence_quote_cannot_join_title_and_description_boundaries(self) -> None:
        product = self._product().model_copy(update={
            "title": "Cordless",
            "description": "screwdriver with 2 batteries",
        })
        service = self._service(
            '{"selections":[{"id":"drill-1","identity_evidence":"Cordless screwdriver",'
            '"requirement_evidence":{}}]}'
        )
        result = asyncio.run(service.select_conversation_candidates(
            [product],
            query="cordless screwdriver",
            identity_review_ids=["drill-1"],
        ))
        self.assertIsNone(result)

    def test_category_path_cannot_substitute_for_listing_evidence(self) -> None:
        product = self._product().model_copy(update={
            "title": "Cordless screwdriver",
            "category_path": ["Tools", "Two battery sets"],
        })
        service = self._service(
            '{"selections":[{"id":"drill-1","identity_evidence":"Cordless screwdriver",'
            '"requirement_evidence":{"двумя аккумуляторами":"Two battery sets"}}]}'
        )
        result = asyncio.run(service.select_conversation_candidates(
            [product],
            query="cordless screwdriver with two batteries",
            required_features=["двумя аккумуляторами"],
            identity_review_ids=["drill-1"],
        ))
        self.assertIsNone(result)

    def test_selection_rejects_identity_quote_not_present_in_listing(self) -> None:
        service = self._service(
            '{"selections":[{"id":"drill-1","identity_evidence":"professional power tool",'
            '"requirement_evidence":{}}]}'
        )
        result = asyncio.run(service.select_conversation_candidates(
            [self._product()],
            query="шуруповёрт",
            identity_review_ids=["drill-1"],
        ))
        self.assertIsNone(result)

    def test_empty_selections_is_a_valid_semantic_no_match(self) -> None:
        service = self._service('{"selections":[]}')
        result = asyncio.run(service.select_conversation_candidates(
            [self._product()],
            query="шуруповёрт",
            required_features=["двумя аккумуляторами"],
        ))
        self.assertEqual(result, [])

    def test_no_budget_does_not_create_an_implicit_price_ceiling(self) -> None:
        service = self._service(
            '{"selections":[{"id":"drill-1","requirement_evidence":{}}]}'
        )
        result = asyncio.run(service.select_conversation_candidates(
            [self._product()], query="шуруповёрт"
        ))
        self.assertEqual(result, ["drill-1"])
        prompt = service.captured_prompts[-1]
        self.assertIn("Пользователь НЕ задавал бюджет", prompt)
        self.assertIn("Не придумывай верхний предел", prompt)
        self.assertIn("Если цена отсутствует, не считай", prompt)
        self.assertIn("карточка честно покажет", prompt)
        self.assertNotIn("всё равно требуются известные", prompt)

    def test_explicit_budget_is_a_hard_selection_constraint(self) -> None:
        service = self._service(
            '{"selections":[{"id":"drill-1","requirement_evidence":{}}]}'
        )
        result = asyncio.run(service.select_conversation_candidates(
            [self._product()],
            query="шуруповёрт",
            budget_constraint="до 2500 грн",
        ))
        self.assertEqual(result, ["drill-1"])
        prompt = service.captured_prompts[-1]
        self.assertIn("Пользователь задал жёсткий бюджет: до 2500 грн", prompt)
        self.assertIn("цена не должна превышать указанную границу", prompt)


class CandidatePayloadTests(unittest.TestCase):
    def test_rank_payload_carries_real_marketplace_category_context(self) -> None:
        product = Product(
            marketplace=Marketplace.ALIEXPRESS,
            product_id="category-context",
            title="Cordless screwdriver",
            product_url="https://example.com/item/category-context.html",
            category_path=["Tools", "Power Tools"],
        )

        payload = AIService._rank_payload(product)

        self.assertEqual(payload["category_path"], ["Tools", "Power Tools"])


if __name__ == "__main__":
    unittest.main()


class GroundedProductComparisonTests(unittest.TestCase):
    def _products(self) -> list[Product]:
        return [
            Product(
                marketplace=Marketplace.ALIEXPRESS,
                product_id="keyboard-1",
                title="Keyboard for ThinkPad T490s/T495, US layout",
                description="Backlit replacement keyboard",
                product_url="https://example.com/keyboard-1",
                price=1519.41,
                currency="UAH",
            ),
            Product(
                marketplace=Marketplace.ALIEXPRESS,
                product_id="keyboard-2",
                title="Keyboard for ThinkPad X280/X390, FR/ES/US layout",
                description="Backlit replacement keyboard",
                product_url="https://example.com/keyboard-2",
                price=1068.55,
                currency="UAH",
            ),
        ]

    def _service(self, response: str) -> AIService:
        service = AIService(api_key=None)
        service._client = object()

        async def fake_generate(contents, *, models=None, operation="generate"):
            self.assertIn('"differences"', contents)
            self.assertIn('"id": "keyboard-1"', contents)
            self.assertIn('"id": "keyboard-2"', contents)
            return response

        service._generate = fake_generate
        return service

    def test_comparison_returns_only_exact_paired_quotes_from_both_cards(self) -> None:
        response = json.dumps({
            "differences": [{
                "aspect": "compatibility",
                "first": {
                    "product_id": "keyboard-1",
                    "field": "title",
                    "quote": "ThinkPad T490s/T495",
                },
                "second": {
                    "product_id": "keyboard-2",
                    "field": "title",
                    "quote": "ThinkPad X280/X390",
                },
            }]
        })
        service = self._service(response)

        result = asyncio.run(
            service.extract_paired_listing_evidence("Сравни эти два варианта", self._products())
        )

        self.assertEqual(
            result,
            [("compatibility", "ThinkPad T490s/T495", "ThinkPad X280/X390")],
        )

    def test_comparison_rejects_invented_or_misassigned_quotes(self) -> None:
        cases = (
            {
                "first": {
                    "product_id": "keyboard-1",
                    "field": "title",
                    "quote": "ThinkPad T490s/T495",
                },
                "second": {
                    "product_id": "keyboard-2",
                    "field": "title",
                    "quote": "ThinkPad T14s",
                },
            },
            {
                "first": {
                    "product_id": "keyboard-2",
                    "field": "title",
                    "quote": "ThinkPad X280/X390",
                },
                "second": {
                    "product_id": "keyboard-1",
                    "field": "title",
                    "quote": "ThinkPad T490s/T495",
                },
            },
        )
        for difference in cases:
            with self.subTest(difference=difference):
                service = self._service(json.dumps({
                    "differences": [{**difference, "aspect": "compatibility"}]
                }))
                self.assertIsNone(asyncio.run(
                    service.extract_paired_listing_evidence("Сравни эти варианты", self._products())
                ))

    def test_comparison_rejects_unknown_aspect_label(self) -> None:
        response = json.dumps({
            "differences": [{
                "aspect": "best_value",
                "first": {
                    "product_id": "keyboard-1",
                    "field": "title",
                    "quote": "ThinkPad T490s/T495",
                },
                "second": {
                    "product_id": "keyboard-2",
                    "field": "title",
                    "quote": "ThinkPad X280/X390",
                },
            }]
        })
        service = self._service(response)

        self.assertIsNone(asyncio.run(
            service.extract_paired_listing_evidence("Сравни эти варианты", self._products())
        ))

    def test_comparison_rejects_full_title_and_nonexistent_text(self) -> None:
        first_title = self._products()[0].title
        response = json.dumps({
            "differences": [{
                "aspect": "compatibility",
                "first": {
                    "product_id": "keyboard-1",
                    "field": "title",
                    "quote": first_title,
                },
                "second": {
                    "product_id": "keyboard-2",
                    "field": "title",
                    "quote": "the keyboard is much better",
                },
            }]
        })
        service = self._service(response)

        self.assertIsNone(asyncio.run(
            service.extract_paired_listing_evidence("Сравни эти варианты", self._products())
        ))

    def test_comparison_with_one_product_does_not_call_gemini(self) -> None:
        service = AIService(api_key=None)
        service._client = object()
        calls = []

        async def fake_generate(contents, *, models=None, operation="generate"):
            calls.append(contents)
            return '{"differences":[]}'

        service._generate = fake_generate
        self.assertIsNone(asyncio.run(
            service.extract_paired_listing_evidence("Сравни", self._products()[:1])
        ))
        self.assertEqual(calls, [])

    def test_slow_comparison_returns_to_deterministic_fallback(self) -> None:
        service = AIService(api_key=None, timeout_seconds=0.01)
        service._client = object()

        async def slow_generate(contents, *, models=None, operation="generate"):
            await asyncio.sleep(0.1)
            return '{"differences":[]}'

        service._generate = slow_generate
        self.assertIsNone(asyncio.run(
            service.extract_paired_listing_evidence("Сравни эти варианты", self._products())
        ))

