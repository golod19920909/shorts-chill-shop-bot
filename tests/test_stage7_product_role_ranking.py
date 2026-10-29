# -*- coding: utf-8 -*-
"""G25.2-C Stage 7: false-EXACT product-role guards and the
semantic-dominance invariant of the post-ranking (diversity) stage.

Deterministic fixtures only - no live AliExpress, no Gemini. Requests are
built by the REAL Stage-2 pipeline (``resolve_and_apply``) exactly as the
production text search does, and the diversity fixtures exercise the real
``DiversityFilter`` / ``CurationPipeline.chain`` with generic titles (no
benchmark product names are hard-coded).

Groups (spec section 10):
    A. false exact / compatibility target     (spec section 3, cases 1-3)
    B. requested product vs target device     (spec section 3, cases 4-5)
    C. tier dominance through diversity       (spec section 4)
    D. diversity within the same tier         (spec section 4)
    E. B1 regression (ranked order kept)      (spec section 6)
    F. D5 regression (exact above weaker)     (spec section 5)
    G. search-mode isolation                  (spec section 7)
    H. empty/no-intent backward compatibility (spec section 8)
    I. existing G24.6/G24.8 regressions       (full suite stays green)
"""

from __future__ import annotations

import unittest

from curation.integration import CurationGate
from curation.pipeline import (
    MODE_CATEGORY,
    MODE_POPULAR,
    MODE_RECOMMENDATION,
    MODE_SEARCH,
    CurationPipeline,
    DiversityFilter,
)
from models.search_request import SearchMode, SearchRequest
from services.relevance import (
    _word_share_score,
    classify_product,
    relevance_score,
    relevance_tier,
)
from tests.test_search_intent import make_listing, resolve_and_apply


def verdict_tier(query: str, title: str) -> tuple[str, int, float]:
    """Classify one title exactly as the production search does."""
    request, _, _ = resolve_and_apply(query)
    product = make_listing("p1", title)
    return (
        classify_product(product, request),
        relevance_tier(product, request),
        round(relevance_score(product, request), 4),
    )


def tier_violations(order: list, tiers: dict) -> int:
    """Pairs where a weaker tier sits above a stronger one."""
    bad = 0
    for i, item in enumerate(order):
        for later in order[i + 1:]:
            if tiers.get(item, 0) < tiers.get(later, 0):
                bad += 1
    return bad


def card(cid: str, title: str) -> dict:
    return {"product_id": cid, "title": title}


class FalseExactCompatibilityTargetTests(unittest.TestCase):
    """Group A: spec section 3, cases 1-3."""

    def test_mount_is_exact_dashcam_is_not(self) -> None:
        # 1. "крепление для видеорегистратора"
        verdict, tier, _ = verdict_tier(
            "крепление для видеорегистратора",
            "Крепление для видеорегистратора 70mai Dash Cam M500",
        )
        self.assertEqual(verdict, "exact")
        self.assertEqual(tier, 2)

        verdict, tier, _ = verdict_tier(
            "крепление для видеорегистратора",
            "Видеорегистратор 4K для автомобиля, ночное видение",
        )
        self.assertNotEqual(tier, 2)
        self.assertNotEqual(verdict, "exact")

    def test_case_is_exact_iphone_is_not(self) -> None:
        # 2. "чехол для iPhone"
        verdict, tier, _ = verdict_tier(
            "чехол для iPhone",
            "Чехол для iPhone 15 прозрачный силикон",
        )
        self.assertEqual(verdict, "exact")
        self.assertEqual(tier, 2)

        verdict, tier, _ = verdict_tier(
            "чехол для iPhone",
            "Apple iPhone 15 смартфон 128GB синий",
        )
        self.assertNotEqual(tier, 2)
        self.assertNotEqual(verdict, "exact")

    def test_charger_is_exact_iphone_is_not(self) -> None:
        # 3. "зарядка для iPhone"
        verdict, tier, _ = verdict_tier(
            "зарядка для iPhone",
            "Зарядка быстрая USB-C 20W PD для iPhone 15",
        )
        self.assertEqual(verdict, "exact")
        self.assertEqual(tier, 2)

        verdict, tier, _ = verdict_tier(
            "зарядка для iPhone",
            "Apple iPhone 15 Pro смартфон 256GB титан",
        )
        self.assertNotEqual(tier, 2)
        self.assertNotEqual(verdict, "exact")


class RequestedProductVsTargetTests(unittest.TestCase):
    """Group B: spec section 3, cases 4-5 + the Stage-6 tool regressions
    that the head-check must NOT re-promote."""

    def test_gift_is_exact_cat_item_is_not(self) -> None:
        # 4. "подарок для кота" - product-type behaviour stays established
        verdict, tier, _ = verdict_tier(
            "подарок для кота",
            "Подарок для кота: домик-когтеточка с лакомством",
        )
        self.assertEqual(verdict, "exact")
        self.assertEqual(tier, 2)

        verdict, tier, _ = verdict_tier(
            "подарок для кота",
            "Плюшевый кот игрушка мягкая антистресс",
        )
        self.assertNotEqual(verdict, "exact")
        self.assertNotEqual(tier, 2)

    def test_ssd_is_exact_laptop_is_not(self) -> None:
        # 5. "SSD для ноутбука" - the laptop is the compatibility target
        verdict, tier, _ = verdict_tier(
            "SSD для ноутбука",
            "SSD 1TB NVMe M.2 для ноутбука, 3D NAND",
        )
        self.assertEqual(verdict, "exact")
        self.assertEqual(tier, 2)

        verdict, tier, _ = verdict_tier(
            "SSD для ноутбука",
            "Ноутбук Dell 15 16GB 1TB SSD",
        )
        self.assertNotEqual(tier, 2)
        self.assertNotEqual(verdict, "exact")

    def test_battery_tools_stay_below_the_battery(self) -> None:
        # Stage-6 B4: «для аккумулятора»-head tools and adjective-headed
        # tools must not become EXACT for a battery request.
        request, _, _ = resolve_and_apply("аккумулятор для Makita 18V")
        battery = make_listing("b", "Аккумулятор Makita 18V 4Ah Li-Ion")
        tool = make_listing(
            "t", "Для аккумулятора Makita 18V, инструмент DTM51, резка"
        )
        adjective_tool = make_listing(
            "a", "Аккумуляторная воздуходувка Makita, для уборки листьев"
        )
        self.assertEqual(classify_product(battery, request), "exact")
        self.assertEqual(relevance_tier(battery, request), 2)
        for product in (tool, adjective_tool):
            self.assertNotEqual(classify_product(product, request), "exact")
            self.assertNotEqual(relevance_tier(product, request), 2)


class TierDominanceThroughDiversityTests(unittest.TestCase):
    """Group C: diversity may never lift a weaker tier above a stronger
    one (generic fixtures, no product names)."""

    @staticmethod
    def _monitor_pool() -> tuple[list, dict]:
        # four same-type tier-2 cards, then weaker tier-1 cards of OTHER
        # types - the legacy whole-list algorithm caps the third and
        # fourth same-type card inside the first block, so a tier-1 card
        # moves above them.
        pool = [
            card("m1", "Монитор игровой широкий 1"),
            card("m2", "Монитор офисный узкий 2"),
            card("m3", "Монитор графический яркий 3"),
            card("m4", "Монитор портативный лёгкий 4"),
            card("a1", "Держатель настенный крепление стальной"),
            card("a2", "Подсветка USB RGB лента гибкая"),
            card("a3", "Кабель HDMI 2.1 8K 2 метра"),
        ]
        tiers = {"m1": 2, "m2": 2, "m3": 2, "m4": 2,
                 "a1": 1, "a2": 1, "a3": 1}
        return pool, tiers

    def test_legacy_algorithm_can_break_tier_dominance(self) -> None:
        # documents the Stage-6 "before" behaviour of the same fixture
        pool, tiers = self._monitor_pool()
        legacy = DiversityFilter().apply(pool)
        order = [c["product_id"] for c in legacy]
        self.assertGreater(tier_violations(order, tiers), 0)

    def test_banded_diversity_keeps_stronger_tier_first(self) -> None:
        pool, tiers = self._monitor_pool()
        banded = DiversityFilter(tier_of=lambda c: tiers[c["product_id"]])
        out = banded.apply(pool)
        order = [c["product_id"] for c in out]
        self.assertEqual(tier_violations(order, tiers), 0)
        self.assertEqual(order[:4], ["m1", "m2", "m3", "m4"])
        self.assertEqual(len(out), len(pool))          # nothing dropped

    def test_tier_zero_sorts_last_and_survives(self) -> None:
        pool = [
            card("u1", "Случайный предмет неизвестный"),
            card("x1", "Точный товар нужный"),
            card("w1", "Упоминание вторичное слово"),
        ]
        tiers = {"u1": 0, "x1": 2, "w1": 1}
        out = DiversityFilter(
            tier_of=lambda c: tiers[c["product_id"]]
        ).apply(pool)
        order = [c["product_id"] for c in out]
        self.assertEqual(order[0], "x1")
        self.assertEqual(set(order), {"u1", "x1", "w1"})


class DiversityWithinSameTierTests(unittest.TestCase):
    """Group D: for a uniform-tier pool the banded result must equal the
    original whole-list diversity output - diversity inside a tier is
    untouched."""

    def test_single_band_matches_legacy_spread(self) -> None:
        pool = [
            card("k1", "Клавиатура мембранная белая"),
            card("k2", "Клавиатура механическая чёрная"),
            card("k3", "Клавиатура компактная красная"),
            card("k4", "Клавиатура игровая подсветка"),
            card("o1", "Мышь беспроводная ergonomic"),
            card("o2", "Мышь игровая RGB сенсор"),
        ]
        tiers = {c["product_id"]: 2 for c in pool}
        legacy = [c["product_id"] for c in DiversityFilter().apply(pool)]
        banded = [
            c["product_id"]
            for c in DiversityFilter(tier_of=lambda c: 2).apply(pool)
        ]
        self.assertEqual(banded, legacy)      # same algorithm, same result
        self.assertEqual(set(banded), set(legacy))  # nothing dropped
        # within-tier subtype spacing still applied: the third same-type
        # card moved behind the other types (order differs from input),
        # and both keyboard cards kept the top of the block.
        self.assertEqual(banded[0], "k1")
        self.assertNotEqual(
            banded, [c["product_id"] for c in pool]
        )
        self.assertLess(banded.index("o1"), banded.index("k3"))


class RankedOrderPreservedRegressionTests(unittest.TestCase):
    """Group E: B1-style - a materially lower-ranked card must not jump
    above much stronger candidates solely because of diversity rotation."""

    @staticmethod
    def _pool() -> tuple[list, dict]:
        # ranked-major input: a run of same-type tier-2 cards, other
        # tier-2 types, and ONE tier-1 card deep in the list (the
        # "ranked #25" of Stage 6).
        pool = [
            card("c1", "Камера заднего вида широкая 1"),
            card("c2", "Камера заднего вида узкая 2"),
            card("c3", "Камера обзорная круглая 3"),
            card("c4", "Камера ночной режим 4"),
            card("c5", "Камера резкая картинка 5"),
            card("c6", "Датчик парковочный ультразвук 6"),
            card("c7", "Реле силовое автомобильное 7"),
            card("c8", "Кронштейн металлический угловой 8"),
            card("z1", "Зарядка быстрая компактная USB"),
        ]
        tiers = {c["product_id"]: 2 for c in pool}
        tiers["z1"] = 1
        return pool, tiers

    def test_legacy_can_lift_the_deep_tier1_card(self) -> None:
        # documents the Stage-6 "before" behaviour (B1: ranked #25 -> #3)
        pool, tiers = self._pool()
        order = [c["product_id"] for c in DiversityFilter().apply(pool)]
        self.assertGreater(tier_violations(order, tiers), 0)
        self.assertLess(order.index("z1"), order.index("c5"))

    def test_stage7_keeps_the_whole_tier2_block_above(self) -> None:
        pool, tiers = self._pool()
        out = DiversityFilter(
            tier_of=lambda c: tiers[c["product_id"]]
        ).apply(pool)
        order = [c["product_id"] for c in out]
        self.assertEqual(tier_violations(order, tiers), 0)
        self.assertTrue(all(order.index(f"c{i}") < order.index("z1")
                            for i in range(1, 9)))
        self.assertEqual(len(out), len(pool))          # dropped = 0


class ExactAboveWeakerRegressionTests(unittest.TestCase):
    """Group F: D5-style - one strong exact card deep inside a same-type
    tier-2 run must stay above every tier-1 card after diversity."""

    @staticmethod
    def _pool() -> tuple[list, dict, str]:
        exact = "m5"
        pool = [
            card("m1", "Монитор для селфи базовый 1"),
            card("m2", "Монитор для селфи угловой 2"),
            card("m3", "Монитор для влогов малый 3"),
            card("m4", "Монитор сценический яркий 4"),
            card(exact, "Монитор для настольного ПК 5"),
            card("a1", "Держатель алюминиевый настольный"),
            card("a2", "Подсветка светодиодная USB"),
            card("a3", "Кабель питания удлинитель 3м"),
            card("a4", "Крылья антивибрационные мягкие"),
        ]
        tiers = {c["product_id"]: 2 for c in pool[:5]}    # monitors t2
        tiers.update({c["product_id"]: 1 for c in pool[5:]})  # weaker t1
        return pool, tiers, exact

    def test_legacy_can_push_the_exact_below_tier1(self) -> None:
        # documents the Stage-6 "before" behaviour (D5: ranked #8 -> #14)
        pool, tiers, exact = self._pool()
        order = [c["product_id"] for c in DiversityFilter().apply(pool)]
        self.assertGreater(tier_violations(order, tiers), 0)
        self.assertGreater(order.index(exact),
                           min(order.index(x) for x in
                               ("a1", "a2", "a3", "a4")))

    def test_stage7_keeps_the_exact_above_every_tier1(self) -> None:
        pool, tiers, exact = self._pool()
        out = DiversityFilter(
            tier_of=lambda c: tiers[c["product_id"]]
        ).apply(pool)
        order = [c["product_id"] for c in out]
        self.assertEqual(tier_violations(order, tiers), 0)
        exact_pos = order.index(exact)
        for weak in ("a1", "a2", "a3", "a4"):
            self.assertLess(exact_pos, order.index(weak))
        # diversity still applies inside the weak band (nothing dropped,
        # all cards present exactly once)
        self.assertEqual(sorted(order), sorted(c["product_id"] for c in pool))
        self.assertEqual(len(out), len(pool))


class SearchModeIsolationTests(unittest.TestCase):
    """Group G: the Stage-7 wiring exists for NORMAL TEXT SEARCH only."""

    def _chain_diversity(self, mode, relevance):
        pipe = CurationPipeline()
        return pipe, pipe.chain(mode=mode, relevance=relevance)[-1]

    def test_search_chain_gets_the_tier_hook(self) -> None:
        tiers = {"some": (2, 1.0)}
        pipe, stage = self._chain_diversity(MODE_SEARCH, tiers)
        self.assertIsInstance(stage, DiversityFilter)
        self.assertIsNotNone(stage.tier_of)
        self.assertIsNot(stage, pipe.diversity)   # fresh, search-only

    def test_every_other_mode_keeps_the_shared_legacy_filter(self) -> None:
        tiers = {"some": (2, 1.0)}
        for mode in (MODE_CATEGORY, MODE_POPULAR, MODE_RECOMMENDATION,
                     "discovery", "feed", "image", "deals"):
            with self.subTest(mode=mode):
                pipe, stage = self._chain_diversity(mode, tiers)
                self.assertIs(stage, pipe.diversity)
                self.assertIsNone(stage.tier_of)

    def test_relevance_map_is_built_for_text_search_only(self) -> None:
        # integration gate: a photo/discovery request that reaches the
        # wiring with curation mode «search» must NOT receive the map.
        text = SearchRequest(original_query="наушники")
        photo = SearchRequest(original_query="photo",
                               mode=SearchMode.IMAGE)
        pool = [make_listing("1", "Беспроводные наушники TWS")]
        self.assertIsNotNone(
            CurationGate._relevance_map(pool, MODE_SEARCH, text)
        )
        self.assertIsNone(
            CurationGate._relevance_map(pool, MODE_SEARCH, photo)
        )
        self.assertIsNone(CurationGate._relevance_map(pool, MODE_SEARCH, None))

    def test_intent_fields_are_ignored_outside_product_type_mode(self) -> None:
        # Fix A is gated by PRODUCT_TYPE_MODE (SEARCH): the same
        # structured intent in another mode can never grant a verdict.
        request = SearchRequest(
            original_query="крепление для видеорегистратора",
            normalized_query="крепление видеорегистратора",
            mode=SearchMode.CATEGORY,
            intent_product_type="крепление",
            intent_compatibility=["видеорегистратор"],
        )
        product = make_listing("p", "Крепление для видеорегистратора X")
        self.assertEqual(classify_product(product, request), "unverified")
        request.mode = SearchMode.POPULAR
        self.assertEqual(classify_product(product, request), "unverified")


class NoIntentBackwardCompatibilityTests(unittest.TestCase):
    """Group H: no structured intent -> byte-identical legacy behaviour."""

    def test_legacy_classify_keeps_g246_semantics(self) -> None:
        request = SearchRequest(
            original_query="зубная щетка",
            normalized_query="зубная щетка",
        )
        head = make_listing("h", "Электрическая зубная щетка Braun")
        target = make_listing(
            "t", "Футляр для электрической зубной щетки прозрачный"
        )
        self.assertEqual(classify_product(head, request), "exact")
        self.assertEqual(relevance_tier(head, request), 2)
        self.assertEqual(classify_product(target, request), "mentioned")
        self.assertNotEqual(relevance_tier(target, request), 2)

    def test_score_without_intent_is_the_word_share(self) -> None:
        request = SearchRequest(
            original_query="зубная щетка",
            normalized_query="зубная щетка",
        )
        product = make_listing("h", "Электрическая зубная щетка Braun")
        self.assertEqual(
            relevance_score(product, request),
            _word_share_score(product, request),
        )

    def test_diversity_without_the_hook_runs_the_original_algorithm(self) -> None:
        # default construction: tier_of is None and apply == the original
        # whole-list _spread (legacy callers cannot even see the new
        # parameter's effect).
        pool = [
            card("x1", "Товар первый сильный"),
            card("y1", "Товар второй слабый"),
            card("x2", "Товар третий сильный"),
        ]
        default = DiversityFilter()
        self.assertIsNone(default.tier_of)
        self.assertEqual(default.apply(pool), default._spread(pool))


if __name__ == "__main__":
    unittest.main()
