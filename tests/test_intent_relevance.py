# -*- coding: utf-8 -*-
"""G25.2-B Stage 3: intent-aware relevance and ranking tests.

Deterministic fixtures only - no live AliExpress, no Gemini (the intent
fields come from the deterministic fallback path of Stage 2, plus one
AI-shaped payload via ``normalize_intent`` proving ``source=ai``
compatibility). Every pipeline helper mirrors the G25.1/G24.8 harness:
    resolve_and_apply -> filter_relevant -> sort_products
"""

from __future__ import annotations

import unittest

from models.search_request import SearchRequest, SearchMode
from services.comparison_service import sort_products
from services.query_intent import apply_intent, normalize_intent
from services.relevance import (
    _word_share_score,
    classify_product,
    filter_relevant,
    intent_score_adjustment,
    rejection_reason,
    relevance_score,
    relevance_tier,
)
from tests.test_search_intent import make_listing, resolve_and_apply


def pipeline(query, titles):
    """Build the intent-bearing request and rank fixture products."""

    request, _, _ = resolve_and_apply(query)
    products = [
        make_listing(str(index), title, 10.0 + (1000 - index))
        for index, title in enumerate(titles)
    ]
    kept, _ = filter_relevant(products, request)
    return request, sort_products(kept, request)


def order_ids(ordered):
    return [product.product_id for product in ordered]


class ProductTypeRankingTests(unittest.TestCase):
    """Section B: product_type is the primary product-role signal."""

    def test_battery_outranks_tool_and_accessory(self) -> None:
        request, ordered = pipeline(
            "аккумулятор для Makita 18V",
            [
                "Дрель аккумуляторная Makita 18V LXT",        # 0 tool
                "Кейс для аккумулятора Makita 18V",           # 1 accessory
                "Аккумулятор Makita 18V 4Ah Li-Ion",          # 2 battery
            ],
        )
        ids = order_ids(ordered)
        self.assertEqual(ids[0], "2")          # battery first
        self.assertLess(ids.index("2"), ids.index("0"))
        battery = ordered[0]
        self.assertEqual(classify_product(battery, request), "exact")
        self.assertEqual(relevance_tier(battery, request), 2)
        self.assertGreater(
            intent_score_adjustment(battery, request),
            intent_score_adjustment(
                make_listing("0", "Дрель аккумуляторная Makita 18V LXT"),
                request,
            ),
        )

    def test_headphones_outrank_their_case(self) -> None:
        # Stage 3A: «без чехла» is now a HARD pool filter, not only the
        # ranking penalty - the excluded case never enters the results at
        # all (so it can neither lead nor follow: it is gone).
        request, ordered = pipeline(
            "наушники без чехла",
            [
                "Чехол для наушников TWS силикон",            # 0 case
                "Наушники TWS Bluetooth 5.3 белые",           # 1 headphones
            ],
        )
        self.assertEqual(order_ids(ordered), ["1"])
        self.assertEqual(classify_product(ordered[0], request), "exact")
        # The classification contract itself is unchanged: the case is still
        # judged "mentioned" - it is rejection_reason/the POOL that now
        # removes it (pinned in tests/test_stage3a_hard_exclusion.py).
        case = make_listing("0", "Чехол для наушников TWS силикон")
        self.assertEqual(classify_product(case, request), "mentioned")
        self.assertEqual(rejection_reason(case, request), "excluded_product_type")

    def test_ssd_outranks_laptop(self) -> None:
        request, ordered = pipeline(
            "SSD 1 ТБ",
            [
                "Ноутбук игровой 16ГБ SSD 512ГБ",             # 0 laptop
                "SSD 11ТБ накопитель для сервера",            # 1 wrong size
                "SSD 1ТБ NVMe M.2 накопитель",                # 2 requested
            ],
        )
        ids = order_ids(ordered)
        self.assertEqual(ids[0], "2")
        self.assertLess(ids.index("2"), ids.index("0"))
        self.assertLess(ids.index("2"), ids.index("1"))


class BrandRankingTests(unittest.TestCase):
    """Section C: explicit brand gets a bonus, strangers a penalty."""

    _WIPER_QUERY = "дворники Volkswagen Golf 7"

    def test_requested_brand_outranks_an_unrelated_brand(self) -> None:
        request, ordered = pipeline(
            self._WIPER_QUERY,
            [
                "Дворники Bosch 55см универсальные",          # 0 other brand
                "Дворники Volkswagen Golf 7 2015-2020",       # 1 wanted
            ],
        )
        self.assertEqual(order_ids(ordered), ["1", "0"])
        self.assertGreater(
            intent_score_adjustment(
                make_listing("1", "Дворники Volkswagen Golf 7"), request),
            intent_score_adjustment(
                make_listing("0", "Дворники Bosch 55см"), request),
        )

    def test_brand_alias_vw_counts_as_volkswagen(self) -> None:
        # «vw» must NOT be treated as a stray brand of a VW query.
        request, _, _ = resolve_and_apply(self._WIPER_QUERY)
        adj_vw = intent_score_adjustment(
            make_listing("a", "Дворники VW Golf 7 оригинал"), request)
        adj_bosch = intent_score_adjustment(
            make_listing("b", "Дворники Bosch 55см"), request)
        self.assertGreater(adj_vw, 0.0)
        self.assertGreater(adj_vw, adj_bosch)

    def test_implicit_brand_never_penalizes_third_parties(self) -> None:
        # Apple comes from «iPhone» implicitly: a Baseus charger is exactly
        # what the user wants, so no brand-mismatch penalty may fire.
        request, _, _ = resolve_and_apply("зарядка для iPhone")
        self.assertEqual(request.intent_brand, "Apple")
        baseus = intent_score_adjustment(
            make_listing("1", "Зарядка Baseus для iPhone 15"), request)
        plain = intent_score_adjustment(
            make_listing("2", "Зарядка для iPhone 15"), request)
        self.assertEqual(baseus, plain)


class ModelGenerationRankingTests(unittest.TestCase):
    """Section D: generation/model disambiguation."""

    def test_golf7_beats_golf5_and_universal(self) -> None:
        request, ordered = pipeline(
            "дворники Volkswagen Golf 7",
            [
                "Дворники универсальные 60см",                # 0
                "Дворники VW Golf 5 2004-2009",               # 1
                "Дворники Volkswagen Golf 7 2015-2020",       # 2
            ],
        )
        ids = order_ids(ordered)
        self.assertEqual(ids[0], "2")
        self.assertLess(ids.index("2"), ids.index("1"))
        self.assertLess(ids.index("1"), ids.index("0"))

    def test_wrong_generation_is_penalized_relative_to_requested(
        self,
    ) -> None:
        request, _, _ = resolve_and_apply("дворники Volkswagen Golf 7")
        adj7 = intent_score_adjustment(
            make_listing("7", "Дворники VW Golf 7"), request)
        adj5 = intent_score_adjustment(
            make_listing("5", "Дворники VW Golf 5"), request)
        self.assertGreater(adj7, adj5)

    def test_explicit_intent_model_ranks_matching_model_first(
        self,
    ) -> None:
        # AI-shaped intent (source=ai payload): model stated explicitly.
        base = SearchRequest(
            original_query="чехол Galaxy", normalized_query="чехол galaxy")
        request = apply_intent(
            base,
            normalize_intent(
                {"product_type": "чехол", "model": "Galaxy S24"}),
        )
        ordered = sort_products(
            [
                make_listing("0", "Чехол для Samsung Galaxy S23"),
                make_listing("1", "Чехол для Samsung Galaxy S24 Ultra"),
            ],
            request,
        )
        self.assertEqual(order_ids(ordered), ["1", "0"])


class RequiredAttributeTests(unittest.TestCase):
    """Section E: specs are positive ranking evidence, digit-safe."""

    def test_18v_matches_18v_only(self) -> None:
        from services.relevance import _intent_spec_matches

        self.assertTrue(_intent_spec_matches("18 v", "аккумулятор 18v"))
        self.assertTrue(_intent_spec_matches("18 v", "аккумулятор 18 v"))
        self.assertFalse(_intent_spec_matches("18 v", "аккумулятор 218v"))
        self.assertFalse(_intent_spec_matches("18 v", "аккумулятор 118v"))

    def test_65w_matches_65w_only(self) -> None:
        from services.relevance import _intent_spec_matches

        self.assertTrue(_intent_spec_matches("65 w", "зарядка 65w"))
        self.assertFalse(_intent_spec_matches("65 w", "зарядка 265w"))
        self.assertFalse(_intent_spec_matches("65 w", "зарядка 650w"))

    def test_1tb_never_matches_11tb(self) -> None:
        from services.relevance import _intent_spec_matches

        self.assertTrue(_intent_spec_matches("1 тб", "ssd 1тб nvme"))
        self.assertTrue(_intent_spec_matches("1 тб", "ssd 1tb nvme"))
        self.assertFalse(_intent_spec_matches("1 тб", "ssd 11тб"))
        self.assertFalse(_intent_spec_matches("1 тб", "ssd 1.5тб"))

    def test_matching_attribute_ranks_above_missing_one(self) -> None:
        request, _, _ = resolve_and_apply("аккумулятор для Makita 18V")
        with_spec = intent_score_adjustment(
            make_listing("a", "Аккумулятор Makita 18V 4Ah"), request)
        without_spec = intent_score_adjustment(
            make_listing("b", "Аккумулятор Makita 4Ah"), request)
        self.assertGreater(with_spec, without_spec)

    def test_absent_attribute_never_penalizes(self) -> None:
        # Positive-only rule: a missing spec costs nothing, it just does
        # not earn the bonus (no G24.x over-filtering through scores).
        request, _, _ = resolve_and_apply("SSD 1 ТБ")
        plain = intent_score_adjustment(
            make_listing("c", "SSD внешний"), request)
        self.assertGreaterEqual(plain, 0.0)


class CompatibilityTests(unittest.TestCase):
    """Section F: compatibility improves fit, never becomes the product."""

    def test_iphone_charger_ranks_above_generic_and_phone(self) -> None:
        request, ordered = pipeline(
            "зарядка для iPhone 15 Pro до 2000 грн",
            [
                "iPhone 15 Pro Max 256GB смартфон",           # 0 the phone
                "Зарядка USB-C 30W универсальная",            # 1 generic
                "Зарядка беспроводная для iPhone 15 Pro",     # 2 wanted
            ],
        )
        ids = order_ids(ordered)
        self.assertEqual(ids[0], "2")
        self.assertLess(ids.index("2"), ids.index("1"))
        self.assertLess(ids.index("2"), ids.index("0"))
        # The phone is demoted below every charger.
        self.assertLess(ids.index("1"), ids.index("0"))

    def test_compat_target_leading_is_penalized(self) -> None:
        request, _, _ = resolve_and_apply("зарядка для iPhone 15 Pro")
        phone = intent_score_adjustment(
            make_listing("p", "iPhone 15 Pro Max смартфон"), request)
        charger = intent_score_adjustment(
            make_listing("c", "Зарядка для iPhone 15 Pro"), request)
        self.assertLess(phone, charger)

    def test_cat_never_becomes_the_product_type(self) -> None:
        request, ordered = pipeline(
            "подарок для кота",
            [
                "Кота игрушечная вязаная",                    # 0 cat-shaped
                "Подарок для кота набор лакомств",            # 1 gift
            ],
        )
        # intent_product_type is not mutated by any scoring step.
        self.assertEqual(request.intent_product_type, "подарок")
        self.assertNotEqual(request.intent_product_type, "кот")
        ids = order_ids(ordered)
        self.assertEqual(ids[0], "1")


class ExclusionTests(unittest.TestCase):
    """Section G: excluded types are penalized, the product is not."""

    def test_case_is_penalized_headphones_are_not(self) -> None:
        request, _, _ = resolve_and_apply("наушники без чехла")
        self.assertTrue(request.intent_excluded_product_types)
        case = intent_score_adjustment(
            make_listing("c", "Чехол для наушников TWS"), request)
        headphones = intent_score_adjustment(
            make_listing("h", "Наушники TWS Bluetooth"), request)
        self.assertLess(case, 0.0)             # meaningful negative signal
        self.assertGreaterEqual(headphones, 0.0)  # main product untouched
        self.assertGreater(headphones, case)


_FIXTURE_TITLES = [
    "Наушники TWS Bluetooth 5.3",
    "Чехол для наушников TWS",
    "Аккумулятор Makita 18V 4Ah",
    "Зарядка для iPhone 15 Pro",
    "SSD 1ТБ NVMe накопитель",
    "Ноутбук игровой SSD 512ГБ",
    "Дворники VW Golf 7",
    "iPhone 15 Pro Max смартфон",
]


class EmptyIntentTests(unittest.TestCase):
    """Section J: without intent fields the behaviour is exactly the old."""

    def test_adjustment_is_zero_for_every_fixture(self) -> None:
        plain = SearchRequest(original_query="x", normalized_query="x")
        for title in _FIXTURE_TITLES:
            product = make_listing("p", title)
            self.assertEqual(
                intent_score_adjustment(product, plain), 0.0, title)

    def test_score_returns_the_untouched_base_value(self) -> None:
        plain = SearchRequest(
            original_query="наушники", normalized_query="наушники")
        for title in _FIXTURE_TITLES:
            product = make_listing("p", title)
            self.assertEqual(
                relevance_score(product, plain),
                _word_share_score(product, plain),
                title,
            )

    def test_legacy_tier_order_is_preserved(self) -> None:
        # G24.6 semantic: exact beats "mentioned" without any intent.
        plain = SearchRequest(
            original_query="наушники", normalized_query="наушники")
        exact = make_listing("e", "Наушники беспроводные с микрофоном")
        mentioned = make_listing("m", "Кейс для наушников силикон")
        ordered = sort_products([mentioned, exact], plain)
        self.assertEqual(order_ids(ordered), ["e", "m"])


class SearchModeIsolationTests(unittest.TestCase):
    """Section I: only the text Find Product search may feel the intent."""

    def _hostile_request(self, mode):
        plain = SearchRequest(original_query="x", normalized_query="x")
        return plain.model_copy(
            update={
                "mode": mode,
                "intent_product_type": "наушники",
                "intent_brand": "Apple",
                "intent_compatibility": ["iphone"],
                "intent_required_attributes": ["18 v"],
                "intent_excluded_product_types": ["чехол"],
            }
        )

    def test_non_search_modes_ignore_intent_fields(self) -> None:
        product = make_listing("p", "Наушники TWS Bluetooth")
        for mode in (SearchMode.CATEGORY, SearchMode.POPULAR,
                     SearchMode.DISCOVERY, SearchMode.IMAGE,
                     SearchMode.FEED):
            request = self._hostile_request(mode)
            self.assertEqual(
                intent_score_adjustment(product, request), 0.0,
                mode.value)

    def test_category_classifies_exactly_as_without_intent(self) -> None:
        product = make_listing("p", "Наушники TWS Bluetooth")
        category = self._hostile_request(SearchMode.CATEGORY)
        plain = SearchRequest(
            original_query="x", normalized_query="x",
            mode=SearchMode.CATEGORY)
        self.assertEqual(
            classify_product(product, category),
            classify_product(product, plain),
        )
        self.assertEqual(
            relevance_score(product, category),
            relevance_score(product, plain),
        )


class ScoringContractTests(unittest.TestCase):
    """Determinism, non-mutation and AI-shaped intent compatibility."""

    def test_scoring_never_mutates_request_or_product(self) -> None:
        request, _, _ = resolve_and_apply("зарядка для iPhone до 2000 грн")
        product = make_listing("p", "Зарядка для iPhone 15 Pro")
        request_before = request.model_dump()
        product_before = product.model_dump()
        relevance_score(product, request)
        relevance_tier(product, request)
        classify_product(product, request)
        self.assertEqual(request.model_dump(), request_before)
        self.assertEqual(product.model_dump(), product_before)

    def test_score_is_deterministic(self) -> None:
        request, _, _ = resolve_and_apply("дворники Volkswagen Golf 7")
        product = make_listing("p", "Дворники VW Golf 7")
        values = {relevance_score(product, request) for _ in range(5)}
        self.assertEqual(len(values), 1)

    def test_ai_shaped_intent_scores_like_fallback_intent(self) -> None:
        # source=ai payload through the SAME normalize/apply pipeline.
        ai_request = apply_intent(
            SearchRequest(
                original_query="зарядка", normalized_query="зарядка"),
            normalize_intent({
                "product_type": "зарядка",
                "compatibility": ["iPhone 15 Pro"],
                "budget": {"max": 2000, "currency": "грн"},
            }),
        )
        self.assertEqual(ai_request.currency, "UAH")
        charger = make_listing("c", "Зарядка для iPhone 15 Pro")
        generic = make_listing("g", "Зарядка USB-C 30W")
        self.assertGreater(
            intent_score_adjustment(charger, ai_request),
            intent_score_adjustment(generic, ai_request),
        )

    def test_adjustment_stays_bounded(self) -> None:
        request, _, _ = resolve_and_apply(
            "аккумулятор для Makita 18V без чехла")
        for title in _FIXTURE_TITLES:
            value = intent_score_adjustment(
                make_listing("p", title), request)
            self.assertGreaterEqual(value, -1.0, title)
            self.assertLessEqual(value, 1.0, title)


class RequiredPipelineChecks(unittest.TestCase):
    """The four pipeline examples of the Stage 3 spec."""

    def test_makita_battery_pipeline(self) -> None:
        _, ordered = pipeline(
            "аккумулятор для Makita 18V",
            [
                "Дрель аккумуляторная Makita 18V LXT",
                "Кейс для аккумулятора Makita 18V",
                "Аккумулятор Makita 18V 4Ah Li-Ion",
            ],
        )
        self.assertEqual(order_ids(ordered)[0], "2")  # battery first

    def test_charger_pipeline(self) -> None:
        _, ordered = pipeline(
            "зарядка для iPhone 15 Pro до 2000 грн",
            [
                "iPhone 15 Pro Max 256GB смартфон",
                "Зарядка USB-C 30W универсальная",
                "Зарядка беспроводная для iPhone 15 Pro",
            ],
        )
        self.assertEqual(order_ids(ordered), ["2", "1", "0"])

    def test_wipers_pipeline(self) -> None:
        _, ordered = pipeline(
            "дворники Volkswagen Golf 7",
            [
                "Дворники универсальные 60см",
                "Дворники VW Golf 5 2004-2009",
                "Дворники Volkswagen Golf 7 2015-2020",
            ],
        )
        self.assertEqual(order_ids(ordered), ["2", "1", "0"])

    def test_ssd_pipeline(self) -> None:
        _, ordered = pipeline(
            "SSD 1 ТБ",
            [
                "Ноутбук игровой 16ГБ SSD 512ГБ",
                "SSD 11ТБ накопитель для сервера",
                "SSD 1ТБ NVMe M.2 накопитель",
            ],
        )
        ids = order_ids(ordered)
        self.assertEqual(ids[0], "2")
        self.assertLess(ids.index("2"), ids.index("0"))


if __name__ == "__main__":
    unittest.main()