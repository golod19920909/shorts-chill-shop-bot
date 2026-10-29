# -*- coding: utf-8 -*-
"""G25.2-B Stage 2: structured intent -> production search input tests.

No live AliExpress / Gemini here: retrieval runs through ``StubProvider``,
parsing through ``AIService(api_key=None)`` (the local fallback parser) or
explicit mocks. Every pipeline test mirrors the exact production order of
``handlers.search._search`` (Step 5):

    parse_query -> resolve_request -> parse_query_intent -> apply_intent
"""

from __future__ import annotations

import asyncio
import unittest

import httpx
from pydantic import ValidationError

from config.settings import MarketplaceCredentials
from models.product import Availability, Marketplace, Product
from models.search_request import SearchRequest
from services.ai_service import AIService
from services.conversation import resolve_request
from services.marketplaces.base import MarketplaceProvider
from services.query_intent import (
    apply_intent,
    fallback_intent,
    parse_query_intent,
    reset_ai_state,
)
from services.search_service import SearchService


def run(coro):
    return asyncio.run(coro)


def make_listing(
    product_id: str, title: str, price: float = 10.0, currency: str = "USD"
) -> Product:
    return Product(
        marketplace=Marketplace.ALIEXPRESS,
        product_id=product_id,
        title=title,
        product_url=f"https://www.aliexpress.com/item/{product_id}.html",
        price=price,
        currency=currency,
        availability=Availability.IN_STOCK,
    )


class StubProvider(MarketplaceProvider):
    """Captures the exact ``SearchRequest`` every retrieval call receives."""

    def __init__(self, products: list[Product]) -> None:
        super().__init__(
            MarketplaceCredentials("key", "secret"), httpx.AsyncClient()
        )
        self.marketplace = Marketplace.ALIEXPRESS
        self.display_name = "AliExpress"
        self._products = products
        self.requests: list[SearchRequest] = []

    async def search_products(self, request: SearchRequest):
        self.requests.append(request)
        return list(self._products)

    async def get_product(self, product_id):
        return None

    def generate_affiliate_link(self, product):
        return None


def resolve_and_apply(text, previous=None, ai=None):
    """The exact Stage 2 pipeline order of ``handlers.search._search``."""

    if ai is None:
        ai = AIService(api_key=None)
    parsed = run(ai.parse_query(text))
    effective, refined = run(resolve_request(previous, parsed, text, ai))
    intent, meta = run(parse_query_intent(text, ai))
    effective = apply_intent(effective, intent, refined=refined)
    return effective, refined, meta


def tokens(request: SearchRequest) -> set[str]:
    return set((request.normalized_query or "").casefold().split())


class SearchRequestIntentFieldTests(unittest.TestCase):
    def test_intent_fields_have_safe_defaults(self) -> None:
        request = SearchRequest(original_query="x")
        self.assertIsNone(request.intent_product_type)
        self.assertIsNone(request.intent_brand)
        self.assertIsNone(request.intent_model)
        self.assertIsNone(request.intent_generation)
        self.assertEqual(request.intent_required_attributes, [])
        self.assertEqual(request.intent_compatibility, [])
        self.assertEqual(request.intent_excluded_product_types, [])

    def test_existing_construction_still_works(self) -> None:
        # A pre-G25.2-B payload (no intent keys) constructs unchanged.
        payload = {"original_query": "x", "normalized_query": "y",
                   "max_price": 30.0, "currency": "USD"}
        request = SearchRequest(**payload)
        self.assertEqual(request.max_price, 30.0)
        self.assertEqual(request.currency, "USD")
        self.assertEqual(request.intent_compatibility, [])

    def test_json_round_trip_keeps_intent_fields(self) -> None:
        request = SearchRequest(
            original_query="зарядка для iPhone",
            intent_product_type="зарядка",
            intent_brand="Apple",
            intent_compatibility=["iphone"],
            intent_required_attributes=["18 v"],
            intent_excluded_product_types=["кабель"],
        )
        restored = SearchRequest.model_validate(
            request.model_dump(mode="json"))
        self.assertEqual(restored, request)

    def test_existing_validation_stays_intact(self) -> None:
        with self.assertRaises(ValidationError):
            SearchRequest(original_query="x", max_price=-1)
        with self.assertRaises(ValidationError):
            SearchRequest(original_query="x", junk=1)  # extra="forbid"


class ApplyIntentUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_ai_state()

    def test_returns_a_copy_and_never_mutates_the_input(self) -> None:
        request = SearchRequest(
            original_query="наушники до 3000 грн",
            normalized_query="наушники",
        )
        before = request.model_dump()
        result = apply_intent(request, fallback_intent("зарядка до 500 грн"))
        self.assertIsNot(result, request)
        self.assertEqual(request.model_dump(), before)
        self.assertEqual(result.original_query, "наушники до 3000 грн")

    def test_original_query_is_never_overwritten(self) -> None:
        request = SearchRequest(original_query="грн", normalized_query="")
        result = apply_intent(request, fallback_intent("грн"))
        self.assertEqual(result.original_query, "грн")

    def test_budget_overwrites_only_when_actually_parsed(self) -> None:
        request = SearchRequest(
            original_query="наушники", normalized_query="наушники",
            max_price=30.0, currency="USD",
        )
        # No budget in the intent -> existing price values are preserved.
        kept = apply_intent(request, fallback_intent("наушники"))
        self.assertEqual(kept.max_price, 30.0)
        self.assertEqual(kept.currency, "USD")
        # A parsed budget replaces them (a new budget is a new budget).
        replaced = apply_intent(request, fallback_intent("до 1500 грн"))
        self.assertEqual(replaced.max_price, 1500.0)
        self.assertEqual(replaced.currency, "UAH")

    def test_min_budget_lands_in_min_price(self) -> None:
        request = SearchRequest(original_query="от 1000 грн")
        result = apply_intent(request, fallback_intent("от 1000 грн"))
        self.assertEqual(result.min_price, 1000.0)
        self.assertEqual(result.currency, "UAH")
        self.assertIsNone(result.max_price)

    def test_fresh_product_search_replaces_with_clean_phrase(self) -> None:
        request = SearchRequest(
            original_query="зарядка для iPhone до 2000 грн",
            normalized_query="зарядка для iphone грн",  # legacy parser leaves «грн»
        )
        result = apply_intent(
            request, fallback_intent("зарядка для iPhone до 2000 грн"))
        self.assertEqual(result.normalized_query, "зарядка Apple iphone")
        self.assertNotIn("грн", result.normalized_query)
        self.assertEqual(result.intent_product_type, "зарядка")
        self.assertEqual(result.intent_compatibility, ["iphone"])
        self.assertEqual(result.max_price, 2000.0)
        self.assertEqual(result.currency, "UAH")

    def test_refinement_keeps_the_merged_phrase_and_identity(self) -> None:
        request = SearchRequest(
            original_query="а дешевле?",
            normalized_query="наушники беспроводные",
            intent_product_type="наушники беспроводные",
        )
        result = apply_intent(
            request, fallback_intent("а дешевле?"), refined=True)
        # The merge's phrase and identity survive; the modifier echo of the
        # follow-up never replaces them.
        self.assertEqual(result.normalized_query, "наушники беспроводные")
        self.assertEqual(result.intent_product_type, "наушники беспроводные")

    def test_budget_only_query_scrubs_currency_out(self) -> None:
        # The legacy parser leaves «грн» in normalized_query (G25.1 D-class).
        request = SearchRequest(
            original_query="до 3000 грн", normalized_query="грн")
        result = apply_intent(request, fallback_intent("до 3000 грн"))
        self.assertEqual(result.normalized_query, "")
        self.assertEqual(result.max_price, 3000.0)
        self.assertEqual(result.currency, "UAH")

    def test_empty_intent_never_invents_a_query(self) -> None:
        request = SearchRequest(original_query="грн", normalized_query="")
        result = apply_intent(request, fallback_intent("грн"))
        self.assertEqual(result.normalized_query, "")
        self.assertIsNone(result.intent_product_type)

    def test_intent_lists_merge_over_the_previous_request(self) -> None:
        request = SearchRequest(
            original_query="без микрофона",
            normalized_query="наушники",
            intent_product_type="наушники",
            intent_compatibility=["iphone"],
        )
        result = apply_intent(
            request, fallback_intent("без микрофона"), refined=True)
        # Identity stays, lists gain the new structured state.
        self.assertEqual(result.intent_product_type, "наушники")
        self.assertEqual(result.intent_compatibility, ["iphone"])
        self.assertTrue(
            any("микрофон" in item
                for item in result.intent_excluded_product_types))

    def test_no_product_type_leaves_structured_identity_empty(self) -> None:
        result = apply_intent(
            SearchRequest(original_query="для Toyota Camry 70"),
            fallback_intent("для Toyota Camry 70"),
        )
        self.assertIsNone(result.intent_product_type)
        self.assertEqual(result.intent_compatibility, ["toyota camry 70"])


class PipelineTransformationTests(unittest.TestCase):
    """The 15 Stage 9 cases through the exact production pipeline order."""

    def setUp(self) -> None:
        reset_ai_state()

    def test_01_do_3000_grn(self) -> None:
        request, _, meta = resolve_and_apply("до 3000 грн")
        self.assertEqual(request.max_price, 3000.0)
        self.assertEqual(request.currency, "UAH")
        self.assertNotIn("грн", request.normalized_query)
        self.assertEqual(request.normalized_query, "")
        self.assertEqual(meta.source, "fallback")

    def test_02_power_bank_do_1500_grn(self) -> None:
        request, _, _ = resolve_and_apply("power bank до 1500 грн")
        self.assertIn("power", tokens(request))
        self.assertIn("bank", tokens(request))
        self.assertNotIn("грн", tokens(request))
        self.assertNotIn("до", tokens(request))
        self.assertEqual(request.max_price, 1500.0)
        self.assertEqual(request.currency, "UAH")

    def test_03_zaryadka_dlya_iphone(self) -> None:
        request, _, _ = resolve_and_apply("зарядка для iPhone")
        self.assertEqual(request.intent_product_type, "зарядка")
        self.assertIn("iphone", request.intent_compatibility)
        self.assertTrue({"зарядка", "iphone"} <= tokens(request))

    def test_04_zaryadka_dlya_iphone_15_pro(self) -> None:
        request, _, _ = resolve_and_apply("зарядка для iPhone 15 Pro")
        self.assertEqual(request.intent_product_type, "зарядка")
        self.assertIn("iphone 15 pro", request.intent_compatibility)
        # Compatibility never becomes the product type.
        self.assertNotEqual(request.intent_product_type, "iphone 15 pro")
        self.assertTrue({"зарядка", "iphone", "15", "pro"} <= tokens(request))

    def test_05_akkumulyator_dlya_makita_18v(self) -> None:
        request, _, _ = resolve_and_apply("аккумулятор для Makita 18V")
        self.assertEqual(request.intent_product_type, "аккумулятор")
        self.assertEqual(request.intent_brand, "Makita")
        self.assertIn("18 v", request.intent_required_attributes)
        # Battery concept stays - no tool/drill product type.
        self.assertNotEqual(request.intent_product_type, "дрель")
        self.assertTrue({"аккумулятор", "makita"} <= tokens(request))

    def test_06_instrument_dlya_makita_18v(self) -> None:
        request, _, _ = resolve_and_apply("инструмент для Makita 18V")
        self.assertEqual(request.intent_product_type, "инструмент")
        self.assertEqual(request.intent_brand, "Makita")
        self.assertTrue({"инструмент", "makita"} <= tokens(request))

    def test_07_dverniki_volkswagen_golf_7(self) -> None:
        request, _, _ = resolve_and_apply("дворники Volkswagen Golf 7")
        self.assertEqual(request.intent_product_type, "дворники")
        self.assertEqual(request.intent_brand, "Volkswagen")
        # No duplicated brand token in the retrieval phrase.
        phrase = request.normalized_query or ""
        self.assertEqual(phrase.casefold().split().count("volkswagen"), 1)
        self.assertTrue({"дворники", "golf", "7"} <= tokens(request))

    def test_08_dlya_toyota_camry_70(self) -> None:
        request, _, _ = resolve_and_apply("для Toyota Camry 70")
        self.assertIsNone(request.intent_product_type)  # no invented type
        self.assertEqual(request.intent_compatibility, ["toyota camry 70"])
        # The search does not crash or become empty because pt is None.
        self.assertTrue(request.normalized_query)

    def test_09_kamera_dlya_mashiny(self) -> None:
        request, _, _ = resolve_and_apply("камера для машины")
        self.assertEqual(request.intent_product_type, "камера")
        self.assertEqual(request.intent_compatibility, ["машины"])
        self.assertTrue({"камера", "машины"} <= tokens(request))

    def test_10_podarok_dlya_kota(self) -> None:
        request, _, _ = resolve_and_apply("подарок для кота")
        self.assertEqual(request.intent_product_type, "подарок")
        self.assertNotEqual(request.intent_product_type, "кот")
        self.assertEqual(request.intent_compatibility, ["кота"])

    def test_11_ssd_1tb(self) -> None:
        request, _, _ = resolve_and_apply("SSD 1 ТБ")
        self.assertEqual(request.intent_product_type, "ssd")
        self.assertIn("1 тб", request.intent_required_attributes)
        self.assertIn("ssd", tokens(request))
        self.assertIn("тб", tokens(request))

    def test_12_ssd_dlya_noutbuka(self) -> None:
        request, _, _ = resolve_and_apply("SSD для ноутбука")
        self.assertEqual(request.intent_product_type, "ssd")
        self.assertEqual(request.intent_compatibility, ["ноутбука"])
        self.assertTrue({"ssd", "ноутбука"} <= tokens(request))

    def test_13_nabor_instrumentov(self) -> None:
        request, _, _ = resolve_and_apply("набор инструментов")
        self.assertEqual(request.intent_product_type, "набор инструментов")
        self.assertTrue({"набор", "инструментов"} <= tokens(request))

    def test_14_do_2000_grn_dlya_ps5(self) -> None:
        request, _, _ = resolve_and_apply("до 2000 грн для PS5")
        self.assertEqual(request.max_price, 2000.0)
        self.assertEqual(request.currency, "UAH")
        self.assertIn("ps5", request.intent_compatibility)
        self.assertIsNone(request.intent_product_type)
        self.assertNotIn("грн", tokens(request))
        self.assertTrue(request.normalized_query)  # no crash, real phrase

    def test_15_grn_alone(self) -> None:
        request, _, meta = resolve_and_apply("грн")
        self.assertNotIn("грн", request.normalized_query)
        self.assertEqual(request.normalized_query, "")  # no fake query
        self.assertIsNone(request.intent_product_type)
        self.assertIsNone(request.max_price)
        self.assertEqual(meta.source, "fallback")  # graceful, never raises


class ProviderContractTests(unittest.TestCase):
    """What actually reaches the marketplace provider (Step 11)."""

    def setUp(self) -> None:
        reset_ai_state()

    def _primary_search(self, text, products, **kwargs):
        request, _, _ = resolve_and_apply(text)
        stub = StubProvider(products)
        outcome = run(
            SearchService([stub]).find_products(request, **kwargs))
        return request, stub, outcome

    def test_provider_receives_clean_product_query(self) -> None:
        request, stub, _ = self._primary_search(
            "power bank до 1500 грн",
            [make_listing("1", "Power Bank 10000mAh", 12.0)],
            broaden=False,
        )
        received = stub.requests[0]
        self.assertEqual(received.normalized_query, "power bank")
        phrase = (received.normalized_query or "").casefold()
        self.assertNotIn("грн", phrase)
        self.assertNotIn("до", phrase)
        self.assertNotIn("1500", phrase)
        # Budget travels structurally, not as text.
        self.assertEqual(received.max_price, 1500.0)
        self.assertEqual(received.currency, "UAH")
        # original_query stays the user's own text, internally untouched.
        self.assertEqual(received.original_query, "power bank до 1500 грн")

    def test_provider_receives_charger_query_without_constraint_syntax(
        self,
    ) -> None:
        request, stub, _ = self._primary_search(
            "зарядка для iPhone 15 Pro до 2000 грн",
            [make_listing("2", "Зарядка для iPhone 15 Pro 65W", 15.0)],
            broaden=False,
        )
        received = stub.requests[0]
        phrase = (received.normalized_query or "").casefold()
        self.assertTrue({"зарядка", "iphone", "15", "pro"}
                        <= set(phrase.split()))
        self.assertNotIn("грн", phrase)
        self.assertNotIn("до", phrase)
        self.assertNotIn("2000", phrase)
        self.assertEqual(received.max_price, 2000.0)
        self.assertEqual(received.currency, "UAH")
        self.assertEqual(
            received.original_query,
            "зарядка для iPhone 15 Pro до 2000 грн",
        )

    def test_budget_filters_client_side_without_new_api_semantics(
        self,
    ) -> None:
        # Same listing, two prices: the parsed budget must filter exactly
        # through the EXISTING comparison_service numeric path. The
        # listings are denominated in the requested currency (Stage 2:
        # a USD price against a UAH budget is incoherent, not comparable).
        request, stub, outcome = self._primary_search(
            "power bank до 1500 грн",
            [
                make_listing(
                    "cheap", "Power Bank 10000mAh", 12.0, currency="UAH"
                ),
                make_listing(
                    "pricey", "Power Bank 20000mAh", 99999.0, currency="UAH"
                ),
            ],
            broaden=False,
        )
        self.assertEqual([p.product_id for p in outcome.products], ["cheap"])

    def test_budget_only_search_keeps_price_fields_and_clean_normalized(
        self,
    ) -> None:
        # Nothing product-like exists to search: the request must still be
        # usable (prices applied client-side), normalized_query without the
        # currency marker, original_query untouched.
        request, stub, _ = self._primary_search(
            "до 3000 грн",
            [make_listing("3", "Наушники беспроводные", 25.0)],
        )
        received = stub.requests[0]
        self.assertEqual(received.max_price, 3000.0)
        self.assertEqual(received.currency, "UAH")
        self.assertNotIn("грн", (received.normalized_query or ""))
        self.assertEqual(received.original_query, "до 3000 грн")

    def test_find_products_from_text_applies_the_same_intent_layer(
        self,
    ) -> None:
        # Production Path B: the SAME parser + apply_intent, no duplicates.
        stub = StubProvider(
            [make_listing("4", "Power Bank 10000mAh", 12.0)])
        service = SearchService(
            [stub], ai_service=AIService(api_key=None))
        run(service.find_products_from_text("power bank до 1500 грн"))
        self.assertTrue(stub.requests)
        received = stub.requests[0]
        self.assertEqual(received.normalized_query, "power bank")
        self.assertNotIn(
            "грн", (received.normalized_query or "").casefold())
        self.assertEqual(received.max_price, 1500.0)
        self.assertEqual(received.currency, "UAH")


class ConversationRegressionTests(unittest.TestCase):
    """Existing resolve_request behaviour must stay untouched (Step 10)."""

    def setUp(self) -> None:
        reset_ai_state()

    def _step(self, previous, text):
        return resolve_and_apply(text, previous=previous)

    def test_initial_search_then_modifier_follow_up(self) -> None:
        first, refined, _ = self._step(
            None, "наушники беспроводные до 3000 грн")
        self.assertFalse(refined)
        self.assertEqual(first.intent_product_type, "наушники беспроводные")
        snapshot = first.model_dump()

        # «а дешевле?» is a refinement under the EXISTING resolve rules.
        second, refined, _ = self._step(first, "а дешевле?")
        self.assertTrue(refined)
        # The previous request object is never mutated in place.
        self.assertEqual(first.model_dump(), snapshot)
        # Phrase and product identity survive the modifier-only message.
        self.assertEqual(second.normalized_query, "наушники беспроводные")
        self.assertEqual(
            second.intent_product_type, "наушники беспроводные")
        self.assertEqual(second.original_query, "а дешевле?")

    def test_numeric_budget_follow_up_applies_current_intent(self) -> None:
        first, _, _ = self._step(
            None, "наушники беспроводные до 3000 грн")
        snapshot = first.model_dump()
        # Stage 49 (D2): a budget-only follow-up («до 1500 грн») introduces no
        # new PRODUCT word, so it is a REFINEMENT - the previous product and
        # its other constraints are preserved and only the budget is replaced
        # (was: the follow-up reset the search and lost the product, the exact
        # defect D2 fixes). Budget replacement itself is unchanged.
        second, refined, _ = self._step(first, "до 1500 грн")
        self.assertTrue(refined)
        self.assertEqual(first.model_dump(), snapshot)
        self.assertEqual(second.max_price, 1500.0)
        self.assertEqual(second.currency, "UAH")
        self.assertNotIn("грн", second.normalized_query)
        self.assertEqual(second.original_query, "до 1500 грн")
        # The product concept survives the budget change.
        self.assertIn("наушник", second.normalized_query)

    def test_compatibility_follow_up_applies_to_current_message(
        self,
    ) -> None:
        first, _, _ = self._step(None, "зарядка")
        second, refined, _ = self._step(first, "для iPhone")
        # Existing semantics: a NEW device word resets the conversation.
        self.assertFalse(refined)
        self.assertEqual(second.original_query, "для iPhone")
        self.assertIsNone(second.intent_product_type)
        self.assertEqual(second.intent_compatibility, ["iphone"])
        self.assertTrue(tokens(second) & {"iphone"})

    def test_compatibility_state_survives_a_refinement(self) -> None:
        first, _, _ = self._step(None, "зарядка для iPhone")
        self.assertEqual(first.intent_compatibility, ["iphone"])
        second, refined, _ = self._step(first, "а дешевле?")
        self.assertTrue(refined)
        # Intent fields do not corrupt the previous request state.
        self.assertEqual(second.intent_product_type, "зарядка")
        self.assertEqual(second.intent_compatibility, ["iphone"])
        self.assertEqual(second.normalized_query, "зарядка Apple iphone")
        self.assertEqual(second.original_query, "а дешевле?")

    def test_new_product_message_still_resets_like_before(self) -> None:
        first, _, _ = self._step(None, "наушники")
        second, refined, _ = self._step(first, "плазморезка")
        self.assertFalse(refined)
        self.assertEqual(second.intent_product_type, "плазморезка")
        self.assertEqual(second.normalized_query, "плазморезка")


class WiringGuardTests(unittest.TestCase):
    """Source-level guards: order, single-parse, no Stage 3 leakage."""

    @staticmethod
    def _source(path: str) -> str:
        import io
        with io.open(path, encoding="utf-8") as handle:
            return handle.read()

    def test_search_handler_order_and_single_parse(self) -> None:
        source = self._source("handlers/search.py")
        early_exit = source.find("if ai_service is None")
        # parse_query and parse_query_intent are INDEPENDENT, so they are issued
        # CONCURRENTLY (asyncio.gather) - the intent PARSE may precede the merge.
        # The invariant the pipeline actually relies on is therefore anchored on
        # APPLY, not parse: the parsed intent must be applied AFTER the merge,
        # and retrieval must happen after that. ``parse_query_intent`` must
        # still exist exactly once (single parse - the duplication guard).
        intent_parse = source.find("parse_query_intent(text, ai_service)")
        resolved = source.find("await resolve_request(")
        applied = source.find("apply_intent(")
        # The retrieval call itself (assigned, then returned - the exact
        # return syntax must not couple this order guard to the code style).
        searched = source.find("await search_service.find_products(")
        self.assertGreater(intent_parse, -1)
        self.assertGreater(applied, -1)
        self.assertGreater(resolved, -1)
        self.assertGreater(searched, -1)
        # The ai_service-is-None branch (Path B) returns BEFORE _search's
        # own intent call -> no path parses intent twice.
        self.assertGreater(intent_parse, early_exit)
        # Single parse: the concurrent block references parse_query_intent once.
        self.assertEqual(source.count("parse_query_intent(text, ai_service)"), 1)
        # Mandatory order: merge first, then intent APPLIED, then retrieval.
        self.assertGreater(applied, resolved)
        self.assertGreater(searched, applied)

    def test_find_products_from_text_uses_the_same_layer(self) -> None:
        source = self._source("services/search_service.py")
        method = source.split("async def find_products_from_text(")[1]
        body = method.split("async def find_feed_page(")[0]
        self.assertIn("await parse_query_intent(", body)
        self.assertIn("apply_intent(request, intent", body)

    def test_find_products_itself_never_parses_intent(self) -> None:
        source = self._source("services/search_service.py")
        body = source.split("async def find_products(")[1].split(
            "async def find_products_from_text(")[0]
        self.assertNotIn("parse_query_intent", body)

    def test_stage_3_relevance_consumes_but_never_parses_intent(self) -> None:
        # G25.2-B Stage 3: relevance may READ the intent_* fields for
        # ranking, but it must never parse/rewrite intent (that stays in
        # services/query_intent.py) and must not grow an AI dependency.
        source = self._source("services/relevance.py")
        self.assertIn("intent_product_type", source)      # consumed
        self.assertNotIn("parse_query_intent", source)     # never parses
        self.assertNotIn("fallback_intent", source)
        self.assertNotIn("INTENT_PROMPT", source)
        self.assertNotIn("genai", source)

    def test_no_second_intent_implementation_exists(self) -> None:
        import os
        hits = []
        for root, _dirs, files in os.walk("services"):
            for name in files:
                if name.endswith(".py") and name != "query_intent.py":
                    text = self._source(os.path.join(root, name))
                    if "def apply_intent(" in text or (
                            "def parse_query_intent(" in text):
                        hits.append(name)
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()