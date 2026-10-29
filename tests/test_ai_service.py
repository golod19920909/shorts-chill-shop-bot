"""Tests for the local query parser and Gemini-output validation.

The local parser runs when no ``GEMINI_API_KEY`` is configured; the
validation tests call the parsing helpers directly with synthetic
Gemini output. Nothing here touches the network.
"""

from __future__ import annotations

import asyncio
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


if __name__ == "__main__":
    unittest.main()

