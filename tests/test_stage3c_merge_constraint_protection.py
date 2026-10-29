# -*- coding: utf-8 -*-
"""Stage 3C: the AI merge can never silently erase OR invent a hard constraint.

``_merge_is_safe`` now compares the model's merge against the deterministic
merge of the very same message (``services.conversation.deterministic_merge``)
field by field on every HARD constraint:

    max_price, min_price, min_rating, min_orders, quantity, currency,
    category, country, intent_product_type, intent_brand, intent_model,
    intent_generation

and guards the structured intent lists
(``intent_required_attributes``, ``intent_compatibility``,
``intent_excluded_product_types``) against both erasure of an established
item and invention of an item the user never mentioned.

The retrieval phrase / keyword hints may be reformulated with words the two
requests already knew; ``page_no``/``ignored_features``/``rating_filter_applicable``
stay outside the gate (they are recomputed by the pipeline).

A legitimate user change is NEVER rejected: the deterministic expectation
already carries that change, so an AI answer that agrees with it lands.
When anything is rejected, ``resolve_request`` applies the deterministic
merge as its fallback - so no constraint is ever lost.

The stubbed model responses mirror the shipped harness
(``RealAIServiceMergeValidationTests``): a real ``AIService`` with the SDK
client faked and ``_generate`` replaced by a JSON fixture - offline, no
network, no Gemini.
"""

from __future__ import annotations

import asyncio
import json
import unittest

from models.search_request import SearchMode, SearchRequest
from services.ai_service import AIService
from services.conversation import deterministic_merge, resolve_request

HARD_FIELDS = (
    "max_price",
    "min_price",
    "min_rating",
    "min_orders",
    "quantity",
    "currency",
    "category",
    "country",
    "intent_product_type",
    "intent_brand",
    "intent_model",
    "intent_generation",
)


def hard_fields(req: SearchRequest) -> dict:
    """The hard-constraint fields of a request, for building a model payload."""

    return {name: getattr(req, name) for name in HARD_FIELDS}


def run(coro):
    return asyncio.run(coro)


def parse(text: str) -> SearchRequest:
    """The real deterministic parser - the same one used without Gemini."""

    return run(AIService(api_key=None).parse_query(text))


def previous_with(
    *,
    required=(),
    excluded=(),
    max_price=30.0,
    min_price=None,
    min_rating=None,
    min_orders=None,
    quantity=None,
    currency="USD",
    category=None,
    country=None,
    intent_product_type=None,
    intent_brand=None,
    intent_model=None,
    intent_generation=None,
    intent_required_attributes=(),
    intent_compatibility=(),
    intent_excluded_product_types=(),
) -> SearchRequest:
    """A settled request carrying already-established constraints."""

    return SearchRequest(
        original_query="Нужны беспроводные наушники",
        normalized_query="беспроводные наушники",
        max_price=max_price,
        min_price=min_price,
        min_rating=min_rating,
        min_orders=min_orders,
        quantity=quantity,
        currency=currency,
        category=category,
        country=country,
        intent_product_type=intent_product_type,
        intent_brand=intent_brand,
        intent_model=intent_model,
        intent_generation=intent_generation,
        intent_required_attributes=list(intent_required_attributes),
        intent_compatibility=list(intent_compatibility),
        intent_excluded_product_types=list(intent_excluded_product_types),
        required_features=list(required),
        excluded_features=list(excluded),
        mode=SearchMode.SEARCH,
    )


BUDGET_TEXT = "а до 20 долларов"
CURRENCY_TEXT = "а до 20 евро"


class MergeConstraintProtectionTests(unittest.TestCase):
    """The AI merge keeps every hard constraint, or is refused."""

    def setUp(self) -> None:
        self.service = AIService(api_key=None)
        self.service._client = object()  # pretend an SDK is present

    def _with_response(self, payload: dict) -> None:
        async def fake_generate(contents, *, models=None):
            return json.dumps(payload, ensure_ascii=False)

        self.service._generate = fake_generate

    # ------------------------------------------------------------------
    # ERASURE - model OMITS a field the deterministic merge keeps
    # ------------------------------------------------------------------

    def test_max_price_erased_is_rejected(self):
        previous = previous_with(max_price=30.0)
        self._with_response({"normalized_query": "беспроводные наушники"})
        follow_up = parse(BUDGET_TEXT)

        self.assertIsNone(
            run(self.service.merge_search_requests(previous, follow_up, BUDGET_TEXT))
        )
        effective, refined = run(
            resolve_request(previous, follow_up, BUDGET_TEXT, ai_service=self.service)
        )
        self.assertTrue(refined)
        self.assertEqual(effective.max_price, 20.0)

    def test_min_price_erased_is_rejected(self):
        previous = previous_with(min_price=5.0)
        self._with_response({"normalized_query": "беспроводные наушники"})
        follow_up = parse(BUDGET_TEXT)

        self.assertIsNone(
            run(self.service.merge_search_requests(previous, follow_up, BUDGET_TEXT))
        )
        effective, _ = run(
            resolve_request(previous, follow_up, BUDGET_TEXT, ai_service=self.service)
        )
        self.assertEqual(effective.min_price, 5.0)

    def test_currency_erased_is_rejected(self):
        previous = previous_with(currency="USD")
        self._with_response({"normalized_query": "беспроводные наушники"})
        follow_up = parse(CURRENCY_TEXT)

        self.assertIsNone(
            run(self.service.merge_search_requests(previous, follow_up, CURRENCY_TEXT))
        )
        effective, _ = run(
            resolve_request(
                previous, follow_up, CURRENCY_TEXT, ai_service=self.service
            )
        )
        self.assertEqual(effective.currency, "EUR")

    def test_category_erased_is_rejected(self):
        previous = previous_with(category="электроника")
        self._with_response({"normalized_query": "беспроводные наушники"})
        follow_up = parse(BUDGET_TEXT)

        self.assertIsNone(
            run(self.service.merge_search_requests(previous, follow_up, BUDGET_TEXT))
        )
        effective, _ = run(
            resolve_request(previous, follow_up, BUDGET_TEXT, ai_service=self.service)
        )
        self.assertEqual(effective.category, "электроника")

    def test_country_erased_is_rejected(self):
        previous = previous_with(country="RU")
        self._with_response({"normalized_query": "беспроводные наушники"})
        follow_up = parse(BUDGET_TEXT)

        self.assertIsNone(
            run(self.service.merge_search_requests(previous, follow_up, BUDGET_TEXT))
        )
        effective, _ = run(
            resolve_request(previous, follow_up, BUDGET_TEXT, ai_service=self.service)
        )
        self.assertEqual(effective.country, "RU")

    def test_intent_product_type_erased_is_rejected(self):
        previous = previous_with(intent_product_type="наушники")
        self._with_response({"normalized_query": "беспроводные наушники"})
        follow_up = parse(BUDGET_TEXT)

        self.assertIsNone(
            run(self.service.merge_search_requests(previous, follow_up, BUDGET_TEXT))
        )
        effective, _ = run(
            resolve_request(previous, follow_up, BUDGET_TEXT, ai_service=self.service)
        )
        self.assertEqual(effective.intent_product_type, "наушники")

    def test_intent_brand_erased_is_rejected(self):
        previous = previous_with(intent_brand="Sony")
        self._with_response({"normalized_query": "беспроводные наушники"})
        follow_up = parse(BUDGET_TEXT)

        self.assertIsNone(
            run(self.service.merge_search_requests(previous, follow_up, BUDGET_TEXT))
        )
        effective, _ = run(
            resolve_request(previous, follow_up, BUDGET_TEXT, ai_service=self.service)
        )
        self.assertEqual(effective.intent_brand, "Sony")

    def test_intent_model_erased_is_rejected(self):
        previous = previous_with(intent_model="WH-1000XM4")
        self._with_response({"normalized_query": "беспроводные наушники"})
        follow_up = parse(BUDGET_TEXT)

        self.assertIsNone(
            run(self.service.merge_search_requests(previous, follow_up, BUDGET_TEXT))
        )
        effective, _ = run(
            resolve_request(previous, follow_up, BUDGET_TEXT, ai_service=self.service)
        )
        self.assertEqual(effective.intent_model, "WH-1000XM4")

    def test_intent_generation_erased_is_rejected(self):
        previous = previous_with(intent_generation="4")
        self._with_response({"normalized_query": "беспроводные наушники"})
        follow_up = parse(BUDGET_TEXT)

        self.assertIsNone(
            run(self.service.merge_search_requests(previous, follow_up, BUDGET_TEXT))
        )
        effective, _ = run(
            resolve_request(previous, follow_up, BUDGET_TEXT, ai_service=self.service)
        )
        self.assertEqual(effective.intent_generation, "4")

    def test_min_rating_erased_is_rejected(self):
        previous = previous_with(min_rating=4.5)
        self._with_response({"normalized_query": "беспроводные наушники"})
        follow_up = parse(BUDGET_TEXT)

        self.assertIsNone(
            run(self.service.merge_search_requests(previous, follow_up, BUDGET_TEXT))
        )
        effective, _ = run(
            resolve_request(previous, follow_up, BUDGET_TEXT, ai_service=self.service)
        )
        self.assertEqual(effective.min_rating, 4.5)

    def test_min_orders_erased_is_rejected(self):
        previous = previous_with(min_orders=100)
        self._with_response({"normalized_query": "беспроводные наушники"})
        follow_up = parse(BUDGET_TEXT)

        self.assertIsNone(
            run(self.service.merge_search_requests(previous, follow_up, BUDGET_TEXT))
        )
        effective, _ = run(
            resolve_request(previous, follow_up, BUDGET_TEXT, ai_service=self.service)
        )
        self.assertEqual(effective.min_orders, 100)

    def test_quantity_erased_is_rejected(self):
        previous = previous_with(quantity=2)
        self._with_response({"normalized_query": "беспроводные наушники"})
        follow_up = parse(BUDGET_TEXT)

        self.assertIsNone(
            run(self.service.merge_search_requests(previous, follow_up, BUDGET_TEXT))
        )
        effective, _ = run(
            resolve_request(previous, follow_up, BUDGET_TEXT, ai_service=self.service)
        )
        self.assertEqual(effective.quantity, 2)

    # ------------------------------------------------------------------
    # INVENTION - model ADDS / changes a field the deterministic merge
    # does not have
    # ------------------------------------------------------------------

    def test_max_price_invented_is_rejected(self):
        previous = previous_with(max_price=None)
        self._with_response({"normalized_query": "беспроводные наушники", "max_price": 999.0})
        follow_up = parse(BUDGET_TEXT)

        self.assertIsNone(
            run(self.service.merge_search_requests(previous, follow_up, BUDGET_TEXT))
        )

    def test_currency_invented_is_rejected(self):
        previous = previous_with(currency=None)
        self._with_response({"normalized_query": "беспроводные наушники", "currency": "JPY"})
        follow_up = parse(BUDGET_TEXT)

        self.assertIsNone(
            run(self.service.merge_search_requests(previous, follow_up, BUDGET_TEXT))
        )

    def test_category_invented_is_rejected(self):
        previous = previous_with(category=None)
        self._with_response({"normalized_query": "беспроводные наушники", "category": "сумки"})
        follow_up = parse(BUDGET_TEXT)

        self.assertIsNone(
            run(self.service.merge_search_requests(previous, follow_up, BUDGET_TEXT))
        )

    def test_country_invented_is_rejected(self):
        previous = previous_with(country=None)
        self._with_response({"normalized_query": "беспроводные наушники", "country": "DE"})
        follow_up = parse(BUDGET_TEXT)

        self.assertIsNone(
            run(self.service.merge_search_requests(previous, follow_up, BUDGET_TEXT))
        )

    def test_intent_product_type_invented_is_rejected(self):
        previous = previous_with(intent_product_type=None)
        self._with_response(
            {"normalized_query": "чехол для телефона", "intent_product_type": "чехол"}
        )
        follow_up = parse("чехол для телефона")

        self.assertIsNone(
            run(self.service.merge_search_requests(previous, follow_up, "чехол для телефона"))
        )

    def test_intent_brand_invented_is_rejected(self):
        previous = previous_with(intent_brand=None)
        self._with_response(
            {"normalized_query": "беспроводные наушники", "intent_brand": "Sony"}
        )
        follow_up = parse(BUDGET_TEXT)

        self.assertIsNone(
            run(self.service.merge_search_requests(previous, follow_up, BUDGET_TEXT))
        )

    def test_intent_required_attribute_invented_is_rejected(self):
        previous = previous_with()
        # «65W» - spec token the user never mentioned; not in either request's
        # words, so it is an invention and the whole merge is refused.
        self._with_response(
            {
                "normalized_query": "зарядное устройство",
                "intent_required_attributes": ["65W"],
            }
        )
        follow_up = parse("зарядное устройство")

        self.assertIsNone(
            run(self.service.merge_search_requests(previous, follow_up, "зарядное устройство"))
        )

    # ------------------------------------------------------------------
    # LEGITIMATE USER CHANGE - model AGREES with deterministic expectation
    # ------------------------------------------------------------------

    def _stub_to_match(self, previous: SearchRequest, follow_up: SearchRequest, raw_text: str) -> None:
        """Stub the model to return exactly the deterministic expectation.

        A legitimate user change is carried by ``expected`` too, so an AI
        answer that agrees with it on every hard field is accepted.
        """

        expected, _ = deterministic_merge(previous, follow_up, raw_text)
        payload = {
            "normalized_query": expected.normalized_query,
            **hard_fields(expected),
            "required_features": list(expected.required_features),
            "excluded_features": list(expected.excluded_features),
        }
        self._with_response(payload)

    def test_max_price_change_is_accepted(self):
        # User explicitly names a new budget: deterministic replaces, the
        # model agrees -> accepted, change applied.
        previous = previous_with(max_price=30.0)
        follow_up = parse(BUDGET_TEXT)
        self._stub_to_match(previous, follow_up, BUDGET_TEXT)

        merged = run(
            self.service.merge_search_requests(previous, follow_up, BUDGET_TEXT)
        )
        self.assertIsNotNone(merged)
        self.assertEqual(merged.max_price, 20.0)

    def test_currency_change_is_accepted(self):
        previous = previous_with(currency="USD")
        follow_up = parse(CURRENCY_TEXT)
        self._stub_to_match(previous, follow_up, CURRENCY_TEXT)

        merged = run(
            self.service.merge_search_requests(previous, follow_up, CURRENCY_TEXT)
        )
        self.assertIsNotNone(merged)
        self.assertEqual(merged.currency, "EUR")

    def test_explicit_cancellation_clears_price_is_accepted(self):
        # «без ограничения цены» - the deterministic parser clears max_price;
        # the model agrees (None) -> accepted, constraint cleared.
        previous = previous_with(max_price=30.0)
        follow_up = parse("без ограничения цены")
        self._stub_to_match(previous, follow_up, "без ограничения цены")

        merged = run(
            self.service.merge_search_requests(
                previous, follow_up, "без ограничения цены"
            )
        )
        self.assertIsNotNone(merged)
        self.assertIsNone(merged.max_price)

    def test_product_switch_resets_is_accepted(self):
        # The user names a NEW product: deterministic resets price/category;
        # the model agrees -> accepted.
        previous = previous_with(max_price=30.0, category="электроника")
        follow_up = parse("чехол для телефона")
        self._stub_to_match(previous, follow_up, "чехол для телефона")

        merged = run(
            self.service.merge_search_requests(
                previous, follow_up, "чехол для телефона"
            )
        )
        self.assertIsNotNone(merged)
        self.assertIsNone(merged.max_price)
        self.assertIsNone(merged.category)

    def test_untouched_constraints_are_kept_is_accepted(self):
        previous = previous_with(
            max_price=30.0,
            currency="USD",
            category="электроника",
            country="RU",
            intent_brand="Sony",
        )
        follow_up = parse(BUDGET_TEXT)
        self._stub_to_match(previous, follow_up, BUDGET_TEXT)

        merged = run(
            self.service.merge_search_requests(previous, follow_up, BUDGET_TEXT)
        )
        self.assertIsNotNone(merged)
        # "а до 20 долларов" sets a NEW budget, so max_price moves to 20.0;
        # everything else the user did not touch is carried over.
        self.assertEqual(merged.max_price, 20.0)
        self.assertEqual(merged.currency, "USD")
        self.assertEqual(merged.category, "электроника")
        self.assertEqual(merged.country, "RU")
        self.assertEqual(merged.intent_brand, "Sony")

    def test_refinement_keeps_and_applies_budget(self):
        previous = previous_with(max_price=30.0, currency="USD")
        follow_up = parse(BUDGET_TEXT)
        self._stub_to_match(previous, follow_up, BUDGET_TEXT)

        merged = run(
            self.service.merge_search_requests(previous, follow_up, BUDGET_TEXT)
        )
        self.assertIsNotNone(merged)
        self.assertEqual(merged.max_price, 20.0)
        self.assertEqual(merged.currency, "USD")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
