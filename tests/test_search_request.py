"""Tests for the structured ``SearchRequest`` model and the local
(Gemini-free) query parser."""

from __future__ import annotations

import asyncio
import unittest

from pydantic import ValidationError

from models.product import Marketplace, Product
from models.search_request import SearchRequest, SortPreference
from services.ai_service import AIService


class SearchRequestModelTests(unittest.TestCase):
    def test_minimal_request_has_safe_defaults(self) -> None:
        request = SearchRequest(original_query="wireless headphones")

        self.assertIsNone(request.max_price)
        self.assertIsNone(request.min_rating)
        self.assertIsNone(request.quantity)
        self.assertEqual(request.keywords, [])
        self.assertEqual(request.preferred_marketplaces, [])
        self.assertEqual(request.sort_preference, SortPreference.RELEVANCE)

    def test_original_query_is_required(self) -> None:
        with self.assertRaises(ValidationError):
            SearchRequest()

    def test_numeric_limits_are_validated(self) -> None:
        with self.assertRaises(ValidationError):
            SearchRequest(original_query="x", max_price=-1)
        with self.assertRaises(ValidationError):
            SearchRequest(original_query="x", min_rating=5.1)
        with self.assertRaises(ValidationError):
            SearchRequest(original_query="x", quantity=0)

    def test_the_marketplace_page_is_internal_and_validated(self) -> None:
        # ``page_no`` is retrieval bookkeeping of ``services.search_service``:
        # the first documented page by default, and a nonsensical value cannot
        # come back through a stored request.
        self.assertEqual(SearchRequest(original_query="x").page_no, 1)
        self.assertEqual(SearchRequest(original_query="x", page_no=2).page_no, 2)
        with self.assertRaises(ValidationError):
            SearchRequest(original_query="x", page_no=0)

    def test_marketplace_values_are_validated(self) -> None:
        request = SearchRequest(
            original_query="x", preferred_marketplaces=["aliexpress"]
        )
        self.assertEqual(request.preferred_marketplaces, [Marketplace.ALIEXPRESS])
        with self.assertRaises(ValidationError):
            SearchRequest(original_query="x", preferred_marketplaces=["amazon"])
        # A removed marketplace cannot come back through a stored request.
        with self.assertRaises(ValidationError):
            SearchRequest(original_query="x", preferred_marketplaces=["alibaba"])

    def test_model_is_extensible_by_optional_fields(self) -> None:
        # New optional fields must not break construction from old payloads.
        payload = SearchRequest(original_query="x").model_dump(mode="json")
        self.assertIn("sort_preference", payload)
        self.assertEqual(SearchRequest(**payload).original_query, "x")

    def test_json_round_trip(self) -> None:
        request = SearchRequest(
            original_query="наушники до 30 долларов",
            normalized_query="wireless headphones",
            max_price=30.0,
            currency="USD",
            quantity=100,
            preferred_marketplaces=[Marketplace.ALIEXPRESS],
            sort_preference=SortPreference.PRICE_ASC,
        )
        restored = SearchRequest.model_validate(request.model_dump(mode="json"))
        self.assertEqual(restored, request)


class LocalFallbackParserTests(unittest.TestCase):
    """``AIService`` without a Gemini key must still parse explicit facts.

    These tests never touch the network: no API key is passed, so the
    deterministic fallback parser is always used.
    """

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




class AIOutputValidationTests(unittest.TestCase):
    """Gemini output is validated and filtered before it is trusted."""

    def test_json_fenced_block_is_parsed(self) -> None:
        raw = '```json\n{"max_price": 25, "normalized_query": "headphones"}\n```'
        payload = AIService._extract_json_object(raw)
        self.assertEqual(payload, {"max_price": 25, "normalized_query": "headphones"})

    def test_non_json_output_returns_none(self) -> None:
        self.assertIsNone(AIService._extract_json_object("не JSON вообще"))

    def test_unknown_fields_are_dropped(self) -> None:
        request = AIService._to_search_request(
            {
                "max_price": 20,
                "invented_field": "ignored",
                "products": [{"title": "fake"}],
            },
            fallback_text="headphones",
        )
        # Gemini cannot inject invented product data into the request.
        self.assertEqual(request.max_price, 20.0)
        self.assertEqual(request.original_query, "headphones")

    def test_invalid_sort_preference_falls_back_to_default(self) -> None:
        request = AIService._to_search_request(
            {"sort_preference": "best_seller_ever"}, fallback_text="headphones"
        )
        self.assertEqual(request.sort_preference, SortPreference.RELEVANCE)

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


if __name__ == "__main__":
    unittest.main()
