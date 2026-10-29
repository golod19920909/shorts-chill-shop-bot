# -*- coding: utf-8 -*-
"""G25.2-C Stage 12 (R3): hard-numeric candidate pool protection.

Deterministic fixtures only - no live AliExpress, no Gemini. Requests are
built by the REAL Stage-2 pipeline (``resolve_and_apply``) exactly as the
production text search does, and every scenario runs through the real
``SearchService`` pipeline (dedup -> relevance -> rank -> spread -> cut ->
feed selection) or through the exact production cut helper.

Evidence: Stage-9 retrieval audit - C3 «повербанк 20000 мАч 65W» (5 strict
banks in RAW, all pooled but ranked #53-64 of 96, cut at the top-30
pre-curation boundary), E2, F2: correct numeric-spec candidates were lost
by their POSITION in the pool, not by relevance. Stage 12 rescues a small
bounded number of already-eligible tail candidates whose titles confirm
the requested explicit numeric spec (the Stage-11 matcher itself), right
at the truncation step - the cut keeps its exact size, the ranking order
and every tier/duplicate rule.

Groups:
    A. an eligible exact numeric-spec candidate outside the pool cutoff
       survives to the final feed (both cut points: without a selector
       and at the selector's deterministic fallback - the Stage-9 path)
    B. a different numeric value / a bare or unrelated number is never
       protected
    C. numeric evidence cannot bypass the existing relevance eligibility
    D. tier ordering: a rescued candidate sits below every kept card and
       a lower tier never takes a higher tier's slot
    E. duplicate / near-duplicate protection remains intact
    F. a strict tested upper bound; no qualifying candidate -> unchanged
    G. no-intent SEARCH and CATEGORY/POPULAR/FEED/DISCOVERY/IMAGE stay
       unchanged
    H. Stage-10 subtype guard, Stage-11 matcher/scoring, G24.6 and G24.8
       guards remain intact
"""

from __future__ import annotations

import asyncio
import unittest

from models.product import Marketplace, Product
from models.search_request import SearchMode, SearchRequest
from services import feed_curation
from services.comparison_service import sort_products
from services.relevance import (
    INTENT_NUMERIC_COVERAGE_BONUS,
    classify_product,
    confirms_numeric_spec,
    filter_relevant,
    intent_score_adjustment,
    relevance_tier,
)
from services.search_service import (
    DEFAULT_MAX_RESULTS,
    NUMERIC_POOL_PROTECTION_LIMIT,
    SearchService,
    _cut_with_numeric_protection,
)
from tests.test_search_intent import resolve_and_apply
from tests.test_search_service import CONFIGURED_CREDENTIALS, StubProvider

#: Generic fixture query - no benchmark query IDs, no ground-truth labels.
QUERY = "повербанк 20000 мАч 65W"
TARGET_TITLE = "Повербанк 65W быстрая зарядка для телефона два порта с кабелем"
IRRELEVANT_65W = "Кабель USB-C 65W для передачи данных быстрый"
WRONG_WATTAGE = "Повербанк 265W компактный для телефона"
BARE_NUMBER = "Повербанк 12000 компактный для телефона"
ACCESSORY_65W = "Кабель для повербанка 65W быстрый"
MACHINE_VISION_CAMERA = (
    "Промышленная камера машинного зрения GigE Ethernet, цифровой датчик, "
    "интеллектуальное распознавание AI, глобальный затвор"
)

_FILLER_ADJECTIVES = (
    "быстрый", "мощный", "тонкий", "стильный", "лёгкий", "складной",
    "карманный", "дорожный", "рабочий", "городской", "спортивный",
    "ёмкий", "современный", "практичный", "элегантный", "надёжный",
    "компактный", "портативный", "умный", "защитный",
)
_FILLER_NOUNS = (
    "порт", "корпус", "экран", "индикатор", "кнопка", "диод", "разъём",
    "браслет",
)


def listing(product_id: str, title: str, **overrides) -> Product:
    """A real-shaped AliExpress listing (image + URL, like the live API)."""

    data = {
        "marketplace": Marketplace.ALIEXPRESS,
        "product_id": product_id,
        "title": title,
        "product_url": f"https://www.aliexpress.com/item/{product_id}.html",
        "image_url": f"https://ae01.alicdn.com/kf/{product_id}.jpg",
        "price": 10.0,
        "currency": "USD",
        "rating": 4.8,
        "orders_count": 1000,
    }
    data.update(overrides)
    return Product(**data)


def filler_pool(count: int = 30) -> list[Product]:
    """``count`` distinct tier-2 exact banks WITHOUT the requested specs.

    Pairwise distinct words keep them below the near-duplicate threshold,
    their orders_count differs so the ranking stays deterministic, and
    «20000 мАч» gives them a word share above the target's - exactly the
    Stage-9 shape: correct numeric-spec titles buried behind comparable
    offers that carry no wattage evidence at all.
    """

    pairs = [
        (adjective, noun)
        for adjective in _FILLER_ADJECTIVES
        for noun in _FILLER_NOUNS
    ]
    return [
        listing(
            f"filler-{index:02d}",
            f"Повербанк 20000 мАч {adjective} {noun}",
            orders_count=1000 + index,
        )
        for index, (adjective, noun) in enumerate(pairs[:count])
    ]


def target_listing() -> Product:
    return listing("target", TARGET_TITLE, orders_count=None)


def run(coro):
    return asyncio.run(coro)


def plain_head(products: list[Product], limit: int, request) -> list[str]:
    """The pipeline WITHOUT protection: sort -> spread -> plain cut."""

    relevant, _dropped = filter_relevant(list(products), request)
    spread = feed_curation.spread_similar(sort_products(relevant, request))
    return [product.product_id for product in spread[:limit]]


class FallbackAI:
    """The selector double whose ANSWER is None - the deterministic fallback.

    Recording the pool ids proves the AI sees the complete uncut pool, and
    the fallback after it is the exact Stage-9 loss point (80 -> 30).
    """

    is_available = True

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def select_feed_candidates(self, candidates, *, wanted=20, query=None):
        self.calls.append({
            "pool": len(candidates),
            "ids": [product.product_id for product in candidates],
            "wanted": wanted,
        })
        return None


class Stage12TestCase(unittest.TestCase):
    """Shared fixtures: the resolved production request and provider life-cycle."""

    def setUp(self) -> None:
        self.request, _, _ = resolve_and_apply(QUERY)
        self.providers: list[StubProvider] = []

    def tearDown(self) -> None:
        async def close_all() -> None:
            for provider in self.providers:
                await provider.aclose()

        if self.providers:
            run(close_all())

    def service(self, products, ai=None) -> SearchService:
        provider = StubProvider(
            marketplace=Marketplace.ALIEXPRESS,
            display_name="AliExpress",
            products=list(products),
            credentials=CONFIGURED_CREDENTIALS,
        )
        self.providers.append(provider)
        return SearchService([provider], ai_service=ai)


class PoolPreservationTests(Stage12TestCase):
    """Group A: the Stage-9 loss points now keep the confirmed candidate."""

    def _spread_ids(self, products: list[Product]) -> list[str]:
        relevant, _dropped = filter_relevant(list(products), self.request)
        spread = feed_curation.spread_similar(
            sort_products(relevant, self.request)
        )
        return [product.product_id for product in spread]

    def test_confirmed_spec_beyond_the_pool_cut_survives_to_the_feed(
        self,
    ) -> None:
        # No selector: the pool IS the browse cap, so ``_collect``'s
        # ``ranked[:limit]`` cut is the one that used to discard the target.
        products = filler_pool(30) + [
            target_listing(),
            listing("irrelevant", IRRELEVANT_65W),
        ]
        spread_ids = self._spread_ids(products)
        # PRECONDITION: the plain pipeline really puts the target BEYOND the
        # cutoff (the test fails loudly if the fixture drifts), and the
        # irrelevant 65 W cable never reaches the ranked list at all.
        self.assertNotIn("target", spread_ids[:DEFAULT_MAX_RESULTS])
        self.assertNotIn("irrelevant", spread_ids)

        service = self.service(products)
        outcome = run(service.find_products(self.request))
        ids = [product.product_id for product in outcome.products]

        # The exact pool size, the exact order - only the boundary slot of
        # the cut traded for the confirmed candidate at its tail.
        self.assertEqual(len(ids), DEFAULT_MAX_RESULTS)
        self.assertEqual(ids[-1], "target")
        self.assertEqual(ids[:-1], spread_ids[: DEFAULT_MAX_RESULTS - 1])
        self.assertNotIn(spread_ids[DEFAULT_MAX_RESULTS - 1], ids)
        self.assertNotIn("irrelevant", ids)

    def test_the_selector_deterministic_fallback_preserves_the_candidate(
        self,
    ) -> None:
        # The Stage-9 recorded path: a selector exists (pool target 80), the
        # selection cannot answer, and the deterministic top-30 fallback -
        # pool[:max_results] - is exactly where C3's banks were lost.
        products = filler_pool(30) + [target_listing()]
        spread_ids = self._spread_ids(products)
        self.assertNotIn("target", spread_ids[:DEFAULT_MAX_RESULTS])

        ai = FallbackAI()
        service = self.service(products, ai)
        outcome = run(service.find_products(self.request))
        ids = [product.product_id for product in outcome.products]

        # The AI saw the COMPLETE uncut pool first - protection never hides
        # a candidate from the model; it only guards the fallback cut.
        self.assertEqual(len(ai.calls), 1)
        self.assertEqual(ai.calls[0]["pool"], 31)
        self.assertIn("target", ai.calls[0]["ids"])
        # ...and the fallback feed keeps the confirmed candidate at its tail.
        self.assertEqual(len(ids), DEFAULT_MAX_RESULTS)
        self.assertEqual(ids[-1], "target")
        self.assertEqual(ids[:-1], spread_ids[: DEFAULT_MAX_RESULTS - 1])


class NonMatchTests(Stage12TestCase):
    """Group B: a wrong value / a bare number never earns protection."""

    def test_a_different_numeric_value_is_not_protected(self) -> None:
        head = filler_pool(6)
        tail = [
            listing("wrong", WRONG_WATTAGE),
            listing("bare", BARE_NUMBER),
        ]
        self.assertFalse(confirms_numeric_spec(tail[0], self.request))
        self.assertFalse(confirms_numeric_spec(tail[1], self.request))

        cut = _cut_with_numeric_protection(head + tail, 6, self.request)
        self.assertEqual(
            [product.product_id for product in cut],
            [product.product_id for product in head],
        )

    def test_a_bare_numeric_attribute_never_opens_the_gate(self) -> None:
        # «65W» in the tail WOULD be a rescue - but the request's only
        # attribute is a bare number, which Stage 11 never treats as a spec,
        # so the whole protection gate stays closed.
        bare_request = SearchRequest(
            original_query=QUERY,
            intent_product_type="повербанк",
            intent_required_attributes=["20000"],
        )
        self.assertFalse(confirms_numeric_spec(target_listing(), bare_request))

        head = filler_pool(6)
        cut = _cut_with_numeric_protection(
            head + [target_listing()], 6, bare_request
        )
        self.assertEqual(
            [product.product_id for product in cut],
            [product.product_id for product in head],
        )


class EligibilityTests(Stage12TestCase):
    """Group C: numeric evidence can never bypass the relevance gate."""

    def test_numeric_evidence_cannot_bypass_the_relevance_gate(self) -> None:
        # The cable DOES confirm «65 w» ...
        cable = listing("cable", IRRELEVANT_65W)
        self.assertTrue(confirms_numeric_spec(cable, self.request))
        # ... and still loses at the very first gate: no query word of its
        # own language matches, so the pool never sees it.
        relevant, dropped = filter_relevant(
            filler_pool(3) + [cable], self.request
        )
        self.assertNotIn("cable", [product.product_id for product in relevant])
        self.assertTrue(dropped)

        # End-to-end the same: an irrelevant product cannot ride its number
        # into the feed, however eligible the tail slots are.
        products = filler_pool(30) + [target_listing(), cable]
        service = self.service(products)
        outcome = run(service.find_products(self.request))
        self.assertNotIn(
            "cable", [product.product_id for product in outcome.products]
        )


class TierOrderTests(Stage12TestCase):
    """Group D: rescue reorders the boundary only, never across tiers."""

    def test_a_rescued_candidate_sits_below_every_kept_card(self) -> None:
        head = filler_pool(6)
        cut = _cut_with_numeric_protection(
            head + [target_listing()], 6, self.request
        )
        ids = [product.product_id for product in cut]
        self.assertEqual(len(ids), 6)
        self.assertEqual(ids[-1], "target")
        # Exactly the boundary card made room; the other five keep order.
        self.assertEqual(ids[:-1], [product.product_id for product in head[:5]])
        self.assertNotIn(head[5].product_id, ids)

    def test_a_lower_tier_candidate_never_takes_a_higher_tier_slot(
        self,
    ) -> None:
        head = filler_pool(6)
        accessory = listing("accessory", ACCESSORY_65W)
        self.assertEqual(relevance_tier(accessory, self.request), 1)
        self.assertEqual(relevance_tier(head[0], self.request), 2)

        # All boundary slots are tier 2 -> the tier-1 accessory gets NO slot:
        # it stays cut and cannot displace (or outrank) a tier-2 card.
        cut = _cut_with_numeric_protection(head + [accessory], 6, self.request)
        self.assertEqual(
            [product.product_id for product in cut],
            [product.product_id for product in head],
        )

        # With a mixed head the rescue may take only a SAME-tier slot: the
        # tier-2 cards are untouchable, the tier-1 boundary card gives way.
        mixed = [
            head[0],
            head[1],
            *[
                listing(f"acc-{index}", f"Кабель для повербанка USB{index} метр")
                for index in range(4)
            ],
        ]
        for card in mixed[2:]:
            self.assertEqual(relevance_tier(card, self.request), 1)
        cut = _cut_with_numeric_protection(
            mixed + [accessory], 6, self.request
        )
        ids = [product.product_id for product in cut]
        self.assertEqual(len(ids), 6)
        self.assertEqual(ids[:2], [head[0].product_id, head[1].product_id])
        self.assertEqual(ids[-1], "accessory")
        self.assertNotIn(mixed[5].product_id, ids)


class DuplicateGuardTests(Stage12TestCase):
    """Group E: near-identical offers never occupy the protected slots."""

    def test_a_near_identical_twin_never_takes_a_protected_slot(
        self,
    ) -> None:
        head = filler_pool(4)
        twin_a = listing("spec-a", "Повербанк 65W компактный для телефона")
        twin_b = listing("spec-b", "Повербанк 65W компактный для телефона серый")
        distinct = listing("spec-c", "Повербанк 20000 мАч 65W мощный")
        self.assertGreaterEqual(
            feed_curation.similarity(twin_a.title, twin_b.title),
            feed_curation.NEAR_DUPLICATE_THRESHOLD,
        )

        cut = _cut_with_numeric_protection(
            head + [twin_a, twin_b, distinct], 4, self.request
        )
        ids = [product.product_id for product in cut]
        # Both protected slots go to DISTINCT offers: the twin is skipped.
        self.assertEqual(len(ids), 4)
        self.assertEqual(ids[-2:], ["spec-a", "spec-c"])
        self.assertNotIn("spec-b", ids)

    def test_a_near_duplicate_of_a_kept_card_is_not_rescued(self) -> None:
        kept_twin = listing("kept-twin", "Повербанк 65W компактный для телефона")
        head = [kept_twin, *filler_pool(4)]
        beyond = [
            listing(
                "twin-of-kept",
                "Повербанк 65W компактный для телефона серый",
            ),
            listing("other-spec", "Повербанк 20000 мАч 65W мощный"),
        ]

        cut = _cut_with_numeric_protection(head + beyond, 5, self.request)
        ids = [product.product_id for product in cut]
        self.assertEqual(len(ids), 5)
        self.assertNotIn("twin-of-kept", ids)
        self.assertEqual(ids[-1], "other-spec")
        # The kept twin stays exactly where the ranking put it.
        self.assertEqual(ids[0], "kept-twin")


class BoundAndInertiaTests(Stage12TestCase):
    """Group F: a strict upper bound; nothing else changes, byte for byte."""

    def test_protection_has_a_strict_upper_bound(self) -> None:
        # The documented hard limit of the stage, asserted, not assumed.
        self.assertEqual(NUMERIC_POOL_PROTECTION_LIMIT, 2)

        head = filler_pool(5)
        tail = [
            listing(f"spec-{index}", f"Повербанк 65W {feature}")
            for index, feature in enumerate(
                ("быстрый", "мощный", "тонкий", "стильный", "лёгкий")
            )
        ]
        for card in tail:
            self.assertTrue(confirms_numeric_spec(card, self.request))

        cut = _cut_with_numeric_protection(head + tail, 5, self.request)
        ids = [product.product_id for product in cut]
        # Exactly the bound rescued, in ranked order; the pool NEVER grows.
        self.assertEqual(len(ids), 5)
        self.assertEqual(ids[-2:], ["spec-0", "spec-1"])
        self.assertNotIn("spec-2", ids)
        self.assertNotIn("spec-4", ids)

    def test_no_matching_candidate_leaves_the_cut_unchanged(self) -> None:
        head = filler_pool(4)
        tail = [
            listing("wrong", WRONG_WATTAGE),
            listing("bare", BARE_NUMBER),
            listing("acc", ACCESSORY_65W),
        ]
        products = head + tail
        cut = _cut_with_numeric_protection(products, 4, self.request)
        self.assertEqual(cut, products[:4])
        self.assertEqual(
            [product.product_id for product in cut],
            [product.product_id for product in head],
        )


class ModeAndIntentIsolationTests(Stage12TestCase):
    """Group G: no-intent SEARCH and every non-SEARCH mode stay unchanged."""

    def test_no_intent_search_is_unchanged(self) -> None:
        plain = SearchRequest(original_query=QUERY)
        products = filler_pool(30) + [target_listing()]
        expected = plain_head(products, DEFAULT_MAX_RESULTS, plain)
        self.assertNotIn("target", expected)

        service = self.service(products)
        outcome = run(service.find_products(plain))
        ids = [product.product_id for product in outcome.products]
        self.assertEqual(ids, expected)
        self.assertNotIn("target", ids)

    def test_non_search_modes_never_protect(self) -> None:
        for mode in (
            SearchMode.CATEGORY,
            SearchMode.POPULAR,
            SearchMode.FEED,
            SearchMode.DISCOVERY,
            SearchMode.IMAGE,
        ):
            with self.subTest(mode=mode):
                request = self.request.model_copy(update={"mode": mode})
                head = filler_pool(5)
                cut = _cut_with_numeric_protection(
                    head + [target_listing()], 5, request
                )
                self.assertEqual(cut, head)

    def test_category_end_to_end_is_unchanged(self) -> None:
        # G24.8: CATEGORY keeps its own pipeline - structured numeric intent
        # on the request changes nothing about the cut.
        request = self.request.model_copy(update={"mode": SearchMode.CATEGORY})
        products = filler_pool(30) + [target_listing()]
        expected = plain_head(products, DEFAULT_MAX_RESULTS, request)
        self.assertNotIn("target", expected)

        service = self.service(products)
        outcome = run(service.find_products(request))
        self.assertEqual(
            [product.product_id for product in outcome.products], expected
        )


class GuardRegressionTests(Stage12TestCase):
    """Group H: Stage-10 / Stage-11 / G24.6 / G24.8 guards remain intact."""

    def test_stage10_machine_vision_guard_remains_effective(self) -> None:
        request, _, _ = resolve_and_apply("камера 4K для машины")
        product = listing("mv", MACHINE_VISION_CAMERA)
        self.assertNotEqual(classify_product(product, request), "exact")
        self.assertNotEqual(relevance_tier(product, request), 2)

        # The pool protection cannot smuggle it through a CATEGORY cut:
        # the mode gate keeps the cut byte-identical.
        category = request.model_copy(update={"mode": SearchMode.CATEGORY})
        head = filler_pool(3)
        cut = _cut_with_numeric_protection(head + [product], 3, category)
        self.assertEqual(
            [card.product_id for card in cut],
            [card.product_id for card in head],
        )

    def test_stage11_numeric_matcher_is_reused_not_rewritten(self) -> None:
        request, _, _ = resolve_and_apply("зарядка USB C 65W")
        cases = {
            "Зарядка USB-C 65W быстрая питание PD3.0": True,
            "Зарядка USB-C 65 W быстрая питание PD": True,
            "Зарядка USB-C 265W быстрая питание PD3.0": False,
            "Зарядка USB-C 650W мощная питание PD3.0": False,
            "Зарядка USB-C 5 Вт компактная питание PD": False,
            "Зарядка USB-C быстрая питание PD3.0 универсальная": False,
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                self.assertEqual(
                    confirms_numeric_spec(listing("p", title), request),
                    expected,
                )
        # The Stage-11 score bonus keeps its own meaning, untouched.
        self.assertEqual(INTENT_NUMERIC_COVERAGE_BONUS, 0.05)

    def test_g246_cross_script_requests_without_intent_are_untouched(
        self,
    ) -> None:
        # A latin title for a cyrillic request keeps its cross-writing path;
        # with no structured intent the cut is a plain ``products[:limit]``.
        plain = SearchRequest(original_query="мощный блок питания")
        head = [
            listing("a", "Блок питания для компьютера настольный"),
            listing("b", "Блок питания настенный импульсный"),
        ]
        latin = listing("latin", "High Power Supply Unit 65W black adapter")
        cut = _cut_with_numeric_protection(head + [latin], 2, plain)
        self.assertEqual(cut, head[:2])

    def test_g248_category_gate_stays_r3_only(self) -> None:
        # CATEGORY keeps the word-level gate: no product-type tier, no
        # intent adjustment and no pool protection, intent fields present.
        request = SearchRequest(
            original_query="сад",
            mode=SearchMode.CATEGORY,
            intent_product_type="мебель",
            intent_required_attributes=["65 w"],
        )
        product = listing("p", "Садовая мебель комплект")
        self.assertEqual(relevance_tier(product, request), 1)
        self.assertEqual(intent_score_adjustment(product, request), 0.0)

        head = filler_pool(3)
        spec = listing("spec", "Повербанк 65W быстрый")
        cut = _cut_with_numeric_protection(head + [spec], 3, request)
        self.assertEqual(
            [card.product_id for card in cut],
            [card.product_id for card in head],
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
