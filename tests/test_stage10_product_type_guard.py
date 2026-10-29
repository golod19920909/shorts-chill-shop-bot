# -*- coding: utf-8 -*-
"""G25.2-C Stage 10 (R1): the unrequested-subtype false-EXACT guard.

Deterministic fixtures only - no live AliExpress, no Gemini. Requests are
built by the REAL Stage-2 pipeline (``resolve_and_apply``) exactly as the
production text search does.

Evidence: Stage-9 retrieval audit (C5 «камера 4K для машины», E3 «камера
заднего вида для машины») - industrial machine-vision cameras were awarded
``exact``/tier 2 for ordinary camera requests because the head noun «камера»
alone matched. The guard downgrades such titles to ``mentioned`` (tier 1,
KEPT - never rejected) while a request that itself declares the subtype
(«камера для машинного зрения») keeps its EXACT verdict.

Groups:
    A. the false-EXACT case + E3 variant (no tier 2, kept)
    B. legit machine-vision request still gets EXACT
    C. control: ordinary camera title still gets EXACT
    D. empty/no-intent -> legacy G24.6 behaviour byte-identical
    E. non-SEARCH modes stay unverified (mode isolation)
    F. the downgraded candidate is KEPT, never rejected (no hard exclusion)
"""

from __future__ import annotations

import unittest

from models.search_request import SearchMode, SearchRequest
from services.relevance import (
    classify_product,
    filter_relevant,
    rejection_reason,
    relevance_tier,
)
from tests.test_search_intent import make_listing, resolve_and_apply

#: Generic fixture titles - no benchmark query IDs, no ground-truth labels.
MACHINE_VISION_CAMERA = (
    "Промышленная камера машинного зрения GigE Ethernet, цифровой датчик, "
    "интеллектуальное распознавание AI, глобальный затвор"
)
MACHINE_VISION_CAMERA_2 = (
    "Промышленная камера машинного зрения 10GigE 8K с датчиком обнаружения ИИ"
)
ORDINARY_CAMERA = "Камера видеорегистратор 4K для автомобиля, ночное видение"


def verdict_tier(query: str, title: str) -> tuple[str, int, str | None]:
    """Classify one title exactly as the production search does."""
    request, _, _ = resolve_and_apply(query)
    product = make_listing("p1", title)
    return (
        classify_product(product, request),
        relevance_tier(product, request),
        rejection_reason(product, request),
    )


class MachineVisionFalseExactTests(unittest.TestCase):
    """Group A: Stage-9 C5/E3 evidence - no false tier 2."""

    def test_ordinary_camera_query_never_gets_exact_for_machine_vision(
        self,
    ) -> None:
        verdict, tier, rejected = verdict_tier(
            "камера 4K для машины", MACHINE_VISION_CAMERA
        )
        self.assertNotEqual(verdict, "exact")
        self.assertNotEqual(tier, 2)
        self.assertIsNone(rejected)

    def test_second_machine_vision_fixture_is_downgraded_too(self) -> None:
        verdict, tier, rejected = verdict_tier(
            "камера 4K для машины", MACHINE_VISION_CAMERA_2
        )
        self.assertNotEqual(verdict, "exact")
        self.assertNotEqual(tier, 2)
        self.assertIsNone(rejected)

    def test_rear_view_camera_query_is_not_exact_for_machine_vision(
        self,
    ) -> None:
        # Stage-9 E3: «камера заднего вида для машины» shares «камера».
        verdict, tier, rejected = verdict_tier(
            "камера заднего вида для машины", MACHINE_VISION_CAMERA
        )
        self.assertNotEqual(verdict, "exact")
        self.assertNotEqual(tier, 2)
        self.assertIsNone(rejected)


class LegitMachineVisionRequestTests(unittest.TestCase):
    """Group B: a request declaring the subtype keeps its EXACT verdict."""

    def test_machine_vision_query_still_gets_exact_for_machine_vision(
        self,
    ) -> None:
        verdict, tier, rejected = verdict_tier(
            "камера для машинного зрения", MACHINE_VISION_CAMERA
        )
        self.assertEqual(verdict, "exact")
        self.assertEqual(tier, 2)
        self.assertIsNone(rejected)

    def test_ordinary_camera_title_stays_exact_for_machine_vision_query(
        self,
    ) -> None:
        verdict, tier, rejected = verdict_tier(
            "камера для машинного зрения", ORDINARY_CAMERA
        )
        self.assertEqual(verdict, "exact")
        self.assertEqual(tier, 2)
        self.assertIsNone(rejected)


class ControlOrdinaryCameraTests(unittest.TestCase):
    """Group C: the shared word alone is still enough WITHOUT a subtype."""

    def test_ordinary_camera_title_keeps_exact_for_ordinary_query(
        self,
    ) -> None:
        verdict, tier, rejected = verdict_tier(
            "камера 4K для машины", ORDINARY_CAMERA
        )
        self.assertEqual(verdict, "exact")
        self.assertEqual(tier, 2)
        self.assertIsNone(rejected)


class EmptyIntentBackwardCompatibilityTests(unittest.TestCase):
    """Group D: no structured intent -> byte-identical G24.6 behaviour."""

    def test_legacy_request_keeps_the_exact_verdict(self) -> None:
        # The guard is gated on _has_structured_intent: without any
        # ``intent_*`` field the head-noun EXACT is untouched.
        request = SearchRequest(
            original_query="камера 4k для машины",
            normalized_query="камера 4k для машины",
        )
        product = make_listing("p1", MACHINE_VISION_CAMERA)
        self.assertEqual(classify_product(product, request), "exact")
        self.assertEqual(relevance_tier(product, request), 2)

    def test_plain_camera_request_without_intent_is_unchanged(self) -> None:
        request = SearchRequest(
            original_query="камера", normalized_query="камера"
        )
        product = make_listing("p1", ORDINARY_CAMERA)
        self.assertEqual(classify_product(product, request), "exact")
        self.assertEqual(relevance_tier(product, request), 2)


class SearchModeIsolationTests(unittest.TestCase):
    """Group E: only SEARCH (PRODUCT_TYPE_MODE) ever carries the guard."""

    def test_category_mode_with_intent_stays_unverified(self) -> None:
        request = SearchRequest(
            original_query="камера 4k для машины",
            normalized_query="камера 4k для машины",
            mode=SearchMode.CATEGORY,
            intent_product_type="камера",
            intent_required_attributes=["4 k"],
            intent_compatibility=["машины"],
        )
        product = make_listing("p1", MACHINE_VISION_CAMERA)
        self.assertEqual(classify_product(product, request), "unverified")
        request.mode = SearchMode.POPULAR
        self.assertEqual(classify_product(product, request), "unverified")


class KeptNotRejectedTests(unittest.TestCase):
    """Group F: the guard falls to ``mentioned``/tier 1 - NEVER excludes."""

    def test_downgraded_candidate_survives_the_relevance_gate(self) -> None:
        request, _, _ = resolve_and_apply("камера 4k для машины")
        exact = make_listing("e", ORDINARY_CAMERA)
        downgraded = make_listing("m", MACHINE_VISION_CAMERA)
        kept, dropped = filter_relevant([exact, downgraded], request)
        self.assertIn(downgraded.product_id, [p.product_id for p in kept])
        self.assertFalse(dropped)
        self.assertIsNone(rejection_reason(downgraded, request))


if __name__ == "__main__":
    unittest.main()
