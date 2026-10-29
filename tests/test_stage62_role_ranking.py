# -*- coding: utf-8 -*-
"""Stage 62: compatibility-only matches rank below product-role evidence.

Offline fixtures only - no Gemini, no AliExpress. The requested product
words come from the request itself (no per-category lists), so the cases
below check the general principle rather than hardcoding one example.
"""
from __future__ import annotations

import unittest

from models.search_request import SearchRequest
from services.comparison_service import sort_products
from services.relevance import (
    classify_product,
    filter_relevant,
    relevance_tier,
    rejection_reason,
)
from tests.test_search_intent import make_listing, resolve_and_apply


def request_for(query: str) -> SearchRequest:
    request, _, _ = resolve_and_apply(query)
    return request


class CompatibilityOnlyRankingTests(unittest.TestCase):
    """A bike-mount query: mounts outrank a phone-only case."""

    QUERY = "держатель для телефона на велосипед"

    def test_case_survives_but_ranks_below_mounts(self) -> None:
        request = request_for(self.QUERY)
        case = make_listing("c", "Магнитный чехол для телефона с дизайном")
        mount1 = make_listing(
            "m1", "Держатель для телефона на велосипеде противоударный"
        )
        mount2 = make_listing(
            "m2", "Универсальный держатель для телефона на мотоцикл с демпфером"
        )
        # Not rejected - an accessory stays eligible.
        self.assertIsNone(rejection_reason(case, request))
        # But below every product sharing the requested role.
        self.assertLess(
            relevance_tier(case, request), relevance_tier(mount1, request)
        )
        self.assertLess(
            relevance_tier(case, request), relevance_tier(mount2, request)
        )
        ordered = [p.product_id for p in sort_products([case, mount1, mount2], request)]
        self.assertEqual(ordered[-1], "c")

    def test_exact_mounts_still_outrank_everything(self) -> None:
        request = request_for(self.QUERY)
        case = make_listing("c", "Магнитный чехол для телефона с дизайном")
        exact = make_listing(
            "m1", "Держатель для телефона на велосипеде противоударный"
        )
        self.assertEqual(classify_product(exact, request), "exact")
        self.assertEqual(relevance_tier(exact, request), 2)
        ordered = [p.product_id for p in sort_products([case, exact], request)]
        self.assertEqual(ordered, ["m1", "c"])


class ExplicitAccessoryRequestTests(unittest.TestCase):
    """When the user asks for a case, the case stays eligible on top."""

    def test_case_request_keeps_case_eligible(self) -> None:
        request = request_for("чехол для iPhone")
        case = make_listing("c", "Чехол для iPhone 15 прозрачный силикон")
        self.assertIsNone(rejection_reason(case, request))
        self.assertGreaterEqual(relevance_tier(case, request), 1)

    def test_charger_request_keeps_wall_charger_eligible(self) -> None:
        request = request_for("зарядка на телефон")
        charger = make_listing("w", "Зарядное устройство Baseus 65W GaN для телефона")
        self.assertIsNone(rejection_reason(charger, request))
        self.assertGreaterEqual(relevance_tier(charger, request), 1)


class AmbiguousProductKeptTests(unittest.TestCase):
    """A family word sharing the script is kept, never rejected."""

    def test_family_word_title_is_kept(self) -> None:
        request = request_for("небулайзер")
        vague = make_listing("v", "Комплект аксессуаров для небулайзера")
        self.assertIsNone(rejection_reason(vague, request))


class DeviceWordSiblingKeptTests(unittest.TestCase):
    """A device-word title sharing the product role stays tier 1."""

    def test_wall_charger_sibling_stays_tier1(self) -> None:
        request = request_for("Мне нужна недорогая но хорошая зарядка на телефон")
        charger = make_listing(
            "w", "Зарядное устройство Baseus 65W GaN для телефона"
        )
        self.assertIsNone(rejection_reason(charger, request))
        # Device-word siblings keep tier 1 (they share the requested role
        # through a device-word head verified against the base nouns).
        self.assertEqual(relevance_tier(charger, request), 1)


if __name__ == "__main__":
    unittest.main()
