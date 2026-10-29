# -*- coding: utf-8 -*-
"""Stage 3A: ``intent_excluded_product_types`` is a HARD pool filter.

Pre-fix, the field reached only ``intent_score_adjustment`` (a ranking
penalty): «наушники без чехла» kept «Чехол для наушников» IN the pool at
tier 1, merely ranked lower. These tests pin the hard gate in
``services.relevance.rejection_reason`` together with its guards:

* the HEAD noun decides - a title that IS the prohibited type is rejected,
  while the R5 family/accessory protection keeps everything that merely
  mentions or belongs to the requested product;
* ambiguity tolerance (the Stage-2 ``currency is None`` precedent) - a head
  that also names the requested product/brand is never hard-rejected;
* mode and no-intent isolation - CATEGORY and pre-intent requests are
  byte-identical.

Offline fixtures only: the request comes from the exact production pipeline
(``resolve_and_apply`` = parse -> resolve_request -> parse_query_intent ->
apply_intent), products are deterministic listings - no live marketplace,
no Gemini.
"""

from __future__ import annotations

import unittest

from models.search_request import SearchMode, SearchRequest
from services.relevance import (
    filter_relevant,
    rejection_reason,
    relevance_tier,
)
from tests.test_search_intent import make_listing, resolve_and_apply


def request_for(query: str) -> SearchRequest:
    request, _, _ = resolve_and_apply(query)
    return request


def ids(products) -> list[str]:
    return [product.product_id for product in products]


class ExplicitExclusionTests(unittest.TestCase):
    """An explicitly prohibited type never passes the pool gate."""

    QUERY = "наушники без чехла"

    def test_the_excluded_case_never_enters_the_pool(self) -> None:
        request = request_for(self.QUERY)
        self.assertTrue(request.intent_excluded_product_types)
        case = make_listing("c", "Чехол для наушников TWS силикон")
        headphones = make_listing("h", "Наушники TWS Bluetooth 5.3")
        cable = make_listing("k", "Кабель USB-C для наушников")

        # An explicit, machine-readable reason - never a silent drop.
        self.assertEqual(
            rejection_reason(case, request), "excluded_product_type"
        )
        kept, dropped = filter_relevant([case, headphones, cable], request)
        self.assertEqual(ids(kept), ["h", "k"])
        self.assertEqual(dropped, {"excluded_product_type": 1})
        # The ranking can never lift a rejected title back either.
        self.assertEqual(relevance_tier(case, request), 0)

    def test_the_requested_product_keeps_its_verified_tier(self) -> None:
        request = request_for(self.QUERY)
        # Cyrillic-dominant title (the head noun «наушники» leads it), the
        # same shape the Stage-3 fixture uses.
        headphones = make_listing("h", "Наушники TWS Bluetooth 5.3 белые")

        self.assertIsNone(rejection_reason(headphones, request))
        self.assertEqual(relevance_tier(headphones, request), 2)

    def test_an_accessory_for_the_requested_product_stays_eligible(self) -> None:
        # R5: «без чехла» bans CASES; «Кабель для наушников» merely belongs
        # to the wanted family and must not be caught by the exclusion.
        request = request_for(self.QUERY)
        cable = make_listing("k", "Кабель USB-C для наушников")

        self.assertIsNone(rejection_reason(cable, request))
        kept, dropped = filter_relevant([cable], request)
        self.assertEqual(ids(kept), ["k"])
        self.assertEqual(dropped, {})

    def test_english_without_case_rejects_the_latin_case_listing(self) -> None:
        request = request_for("headphones without case")
        self.assertEqual(
            [phrase.casefold() for phrase in request.intent_excluded_product_types],
            ["case"],
        )
        case = make_listing("c", "Case cover for AirPods Pro")
        headphones = make_listing("h", "Wireless headphones TWS")

        self.assertEqual(
            rejection_reason(case, request), "excluded_product_type"
        )
        kept, dropped = filter_relevant([case, headphones], request)
        self.assertEqual(ids(kept), ["h"])
        self.assertEqual(dropped, {"excluded_product_type": 1})


class PhoneNotTabletTests(unittest.TestCase):
    """«не нужен X» is the same hard exclusion as «без X»."""

    def test_the_tablet_is_rejected_and_the_phone_stays(self) -> None:
        request = request_for("телефон, не нужен планшет")
        self.assertTrue(request.intent_excluded_product_types)
        tablet = make_listing("t", "Планшет Samsung Galaxy Tab A9")
        phone = make_listing("p", "Телефон Samsung Galaxy S24")

        self.assertEqual(
            rejection_reason(tablet, request), "excluded_product_type"
        )
        self.assertIsNone(rejection_reason(phone, request))
        kept, dropped = filter_relevant([tablet, phone], request)
        self.assertEqual(ids(kept), ["p"])
        self.assertEqual(dropped, {"excluded_product_type": 1})


class HeadNounScopingTests(unittest.TestCase):
    """The exclusion follows the HEAD noun, never a body mention."""

    QUERY = "пылесос без мешка"

    def test_a_bag_listing_is_rejected_but_the_vacuum_is_kept(self) -> None:
        request = request_for(self.QUERY)
        bag = make_listing("b", "Мешок для пылесоса циклонный 3л")
        vacuum = make_listing("v", "Пылесос Samsung с мешком 3л")

        # The head «мешок» IS the excluded type - the accessory ban works...
        self.assertEqual(
            rejection_reason(bag, request), "excluded_product_type"
        )
        # ...while the requested product merely MENTIONS «мешком» in the
        # body - ambiguous by construction, so doubt keeps it eligible.
        self.assertIsNone(rejection_reason(vacuum, request))
        kept, dropped = filter_relevant([bag, vacuum], request)
        self.assertEqual(ids(kept), ["v"])
        self.assertEqual(dropped, {"excluded_product_type": 1})


class ExclusionIsolationTests(unittest.TestCase):
    """The gate must not widen beyond structured SEARCH requests."""

    def test_category_mode_is_untouched_by_the_exclusion(self) -> None:
        # CATEGORY reaches the token filter but never the structured gate:
        # even a hostile intent payload must behave exactly as without it.
        request = SearchRequest(
            original_query="наушники без чехла",
            normalized_query="наушники",
            mode=SearchMode.CATEGORY,
            intent_product_type="наушники",
            intent_excluded_product_types=["чехол"],
        )
        case = make_listing("c", "Чехол для наушников TWS")

        self.assertIsNone(rejection_reason(case, request))
        kept, dropped = filter_relevant([case], request)
        self.assertEqual(ids(kept), ["c"])
        self.assertEqual(dropped, {})

    def test_without_structured_intent_the_legacy_behaviour_stays(self) -> None:
        # The words «без чехла» in the QUERY TEXT alone must not filter -
        # only structured intent does (pre-intent G24.6 requests stay
        # byte-identical).
        plain = SearchRequest(
            original_query="наушники без чехла",
            normalized_query="наушники без чехла",
        )
        case = make_listing("c", "Чехол для наушников TWS")

        self.assertIsNone(rejection_reason(case, plain))
        kept, dropped = filter_relevant([case], plain)
        self.assertEqual(ids(kept), ["c"])
        self.assertEqual(dropped, {})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
