# -*- coding: utf-8 -*-
"""G25.2-F mention-only fallback of ``_final_relevance_cut``.

Covers the 2026-10-25 live regression (a pool of only mention-only tier-1
candidates shipped 0 for «Мощный повербанк") and every selection rule the
correction introduced, including the truthful per-category log counters.
"""
import logging
import unittest
from unittest import mock

from models.product import Marketplace, Product
from models.search_request import SearchMode, SearchRequest
from services.relevance import _mentions_only
from services.comparison_service import sort_products
from services.search_service import MENTION_ONLY_FILL_LIMIT, _final_relevance_cut


def make_product(product_id: str, title: str) -> Product:
    return Product(
        marketplace=Marketplace.ALIEXPRESS,
        product_id=product_id,
        title=title,
        product_url=f"https://www.aliexpress.com/item/{product_id}.html",
    )


def make_request(mode=SearchMode.SEARCH, query="power bank") -> SearchRequest:
    return SearchRequest(
        original_query=query,
        normalized_query=query,
        mode=mode,
    )


def mention_only_pool(count: int, prefix: str = "m"):
    """Realistic RUSSIAN device titles that merely MENTION a power bank -
    they are NOT genuine power banks and the tests never claim they are."""
    titles = [
        "Портативное аварийное радио AM/FM с солнечной батареей, с функцией "
        "повербанка",
        "Мощный светодиодный фонарь для кемпинга, работает от повербанка",
        "Подводный фонарь для ночной рыбалки с функцией повербанка",
        "Терапевтический пояс с возможностью подключения к повербанку",
        "Станция аренды повербанков на 8 слотов с QR-сканером",
        "Машинка для мыльных пузырей, питание от повербанка",
    ]
    return [
        make_product(f"{prefix}{i}", titles[i % len(titles)])
        for i in range(count)
    ]


def genuine_pool(count: int, prefix: str = "g"):
    return [
        make_product(
            f"{prefix}{i}",
            f"Повербанк 20000 мАч быстрый зарядка PD 65W модель {i}",
        )
        for i in range(count)
    ]


class MentionOnlyFinalCutTests(unittest.TestCase):
    """1. Russian regression: ONLY mention-only candidates in the pool."""

    def test_russian_regression_pool_ships_bounded_list(self):
        # 2026-10-25: «Мощный повербанк» had 15 verified tier-1 candidates,
        # all mention-only, and shipped 0. The bounded honest list is the
        # corrected behaviour.
        req = make_request(query="Мощный повербанк")
        products = sort_products(mention_only_pool(15), req)
        outcome = _final_relevance_cut(products, 30, req)
        self.assertTrue(outcome)
        self.assertLessEqual(len(outcome), MENTION_ONLY_FILL_LIMIT)

    def test_mention_only_bounded_even_in_small_pool(self):
        # 1 mention-only candidate alone still ships (bounded by the pool).
        products = sort_products(mention_only_pool(1), make_request())
        outcome = _final_relevance_cut(products, 30, make_request())
        self.assertEqual(len(outcome), 1)

    # 2. Related products present -> they lead, mentions cannot displace.

    def test_related_products_lead_mention_only_follow(self):
        req = make_request()
        products = sort_products(
            mention_only_pool(6) + genuine_pool(2), req
        )
        outcome = _final_relevance_cut(products, 30, req)
        ids = [p.product_id for p in outcome]
        self.assertEqual([i for i in ids if i.startswith("g")], ["g0", "g1"])
        self.assertEqual(ids[:2], ["g0", "g1"])

    # 3. More related than the limit -> the limit wins, no mention filler.

    def test_related_overflow_respects_limit(self):
        req = make_request()
        products = sort_products(
            mention_only_pool(4) + genuine_pool(10), req
        )
        outcome = _final_relevance_cut(products, 5, req)
        self.assertEqual(len(outcome), 5)
        self.assertTrue(
            all(p.product_id.startswith("g") for p in outcome)
        )

    # 4. Fewer related than the limit -> related retained, tier-0 fallback
    #    fills; mentions never displace related products (they only ship in
    #    the no-related branch).

    def test_few_related_never_displaced(self):
        req = make_request()
        products = sort_products(
            mention_only_pool(6) + genuine_pool(2), req
        )
        outcome = _final_relevance_cut(products, 4, req)
        ids = [p.product_id for p in outcome]
        self.assertEqual(ids[:2], ["g0", "g1"])
        self.assertNotIn("m0", ids)

    # 5. Empty input ships nothing.

    def test_empty_input_returns_empty(self):
        self.assertEqual(_final_relevance_cut([], 30, make_request()), [])

    # 6. No verified candidates -> established numeric-protection fallback.

    def test_no_verified_candidates_previous_behaviour(self):
        req = make_request()
        # Titles sharing NO request word: relevance_tier == 0 for all.
        junk = [
            make_product(f"k{i}", f"Sailor Moon drawstring bag {i}")
            for i in range(40)
        ]
        ranked = sort_products(junk, req)
        outcome = _final_relevance_cut(ranked, 30, req)
        self.assertEqual(len(outcome), 30)

    # 7. Non-SEARCH mode keeps its previous behaviour.

    def test_non_search_mode_unchanged(self):
        req = make_request(mode=SearchMode.CATEGORY)
        products = sort_products(mention_only_pool(15), req)
        outcome = _final_relevance_cut(products, 5, req)
        # _cut_with_numeric_protection ships the ranked head unchanged.
        self.assertEqual(len(outcome), 5)

    # 8. Limit edge cases.

    def test_zero_limit_returns_empty(self):
        products = sort_products(
            mention_only_pool(3) + genuine_pool(2), make_request()
        )
        self.assertEqual(_final_relevance_cut(products, 0, make_request()), [])

    def test_limit_smaller_than_mention_pool(self):
        req = make_request(query="Мощный повербанк")
        products = sort_products(mention_only_pool(15), req)
        outcome = _final_relevance_cut(products, 3, req)
        self.assertEqual(len(outcome), 3)

    # 9. Logging regression: truthful, never negative, mention-only never
    #    reported as "unverified fill".

    def test_log_reports_truthful_non_negative_counts(self):
        req = make_request(query="Мощный повербанк")
        products = sort_products(mention_only_pool(15), req)
        with self.assertLogs("services.search_service", level="INFO") as captured:
            _final_relevance_cut(products, 30, req)
        cut_lines = [
            line for line in captured.output
            if "Final relevance cut" in line
        ]
        self.assertTrue(cut_lines)
        line = cut_lines[-1]
        self.assertNotIn("verified +", line)   # old misleading format gone
        self.assertIn("mention-only=15", line)
        self.assertIn("related=0", line)
        self.assertIn("tier0-fill=0", line)
        self.assertNotIn("+ -", line)          # no negative fill anywhere

    def test_log_reports_mention_only_as_its_own_category(self):
        req = make_request()
        products = sort_products(
            mention_only_pool(6) + genuine_pool(2), req
        )
        with self.assertLogs("services.search_service", level="INFO") as captured:
            _final_relevance_cut(products, 30, req)
        line = [
            l for l in captured.output if "Final relevance cut" in l
        ][-1]
        self.assertIn("related=2", line)
        self.assertIn("mention-only=0", line)   # none SELECTED beside related
        self.assertIn("tier0-fill=0", line)

    # 10. Genuine power-bank titles keep their existing verified tier.

    def test_genuine_power_banks_still_verified(self):
        req = make_request()
        for title in (
            "Повербанк 20000 мАч быстрый зарядка PD 65W USB-C",
            "Power bank 20000mAh fast charging",
            "Мощный повербанк 30000 мАч с фонариком",
        ):
            p = make_product("x", title)
            self.assertFalse(_mentions_only(p, req), title)
            self.assertGreaterEqual(
                __import__("services.relevance", fromlist=["relevance_tier"])
                .relevance_tier(p, req),
                1,
                title,
            )

    def test_mentions_only_detector_shape(self):
        req = make_request()
        genuine = make_product("g0", "Power bank 20000mAh")
        mention = make_product("m0", "Camping lantern with power bank feature")
        self.assertFalse(_mentions_only(genuine, req))
        self.assertTrue(_mentions_only(mention, req))


if __name__ == "__main__":
    unittest.main()
