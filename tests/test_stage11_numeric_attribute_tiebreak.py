# -*- coding: utf-8 -*-
"""G25.2-C Stage 11 (R2): the numeric attribute coverage tie-break.

Deterministic fixtures only - no live AliExpress, no Gemini. Requests are
built by the REAL Stage-2 pipeline (``resolve_and_apply``) exactly as the
production text search does.

Evidence: Stage-9 retrieval audit (B5 «зарядка USB C 65W», C3 «повербанк
20000 мАч 65W») - correct 65 W candidates survived to the pool but ranked
behind otherwise comparable offers that carried no wattage evidence at
all. Stage 11 adds a small bounded EXTRA inside the existing intent
adjustment for every confirmed EXPLICIT numeric spec (a digit bound to a
unit word: «65 w», «20000 мач», «1 тб»), so the candidate whose title
actually carries the requested number+unit leads between otherwise
comparable peers. It reorders ties only: tiers, verdicts, filtering and
every other ranking layer are untouched, and a title that merely omits
the spec is never penalized.

Groups:
    A. explicit 65 W request - confirmed candidate beats a comparable
       candidate with no wattage evidence
    B. «65W» and «65 W» are the same spec; incompatible values («265W»,
       «650W») and bare unrelated numbers never count
    C. missing spec evidence never hard-rejects or demotes
    D. higher relevance tier still dominates the numeric tie-break
    E. no structured intent -> legacy ordering; non-SEARCH modes unchanged
    F. Stage-10 subtype guard and G24.6/G24.8 regression guards hold
"""

from __future__ import annotations

import unittest

from models.search_request import SearchMode, SearchRequest
from services.comparison_service import sort_products
from services.relevance import (
    INTENT_ATTRIBUTE_BONUS,
    INTENT_NUMERIC_COVERAGE_BONUS,
    _is_numeric_attribute,
    _numeric_attribute_coverage,
    _word_share_score,
    classify_product,
    filter_relevant,
    intent_score_adjustment,
    rejection_reason,
    relevance_score,
    relevance_tier,
)
from tests.test_search_intent import make_listing, resolve_and_apply

#: Generic fixture titles - no benchmark query IDs, no ground-truth labels.
CHARGER_65W = "Зарядка USB-C 65W быстрая питание PD3.0"
CHARGER_65W_SPACED = "Зарядка USB-C 65 W быстрая питание PD"
CHARGER_NO_WATTAGE = "Зарядка USB-C быстрая питание PD3.0 универсальная"
CHARGER_WRONG_WATTAGE = "Зарядка USB-C 265W быстрая питание PD3.0"
CHARGER_650W = "Зарядка USB-C 650W мощная питание PD3.0"
CHARGER_UNRELATED_NUMBER = "Зарядка USB-C 5 Вт компактная питание PD"
BANK_65W = "Повербанк 20000 мАч 65W быстрая зарядка, два порта"
BANK_NO_SPEC = "Повербанк 20000 мАч компактный, два порта"
MACHINE_VISION_CAMERA = (
    "Промышленная камера машинного зрения GigE Ethernet, цифровой датчик, "
    "интеллектуальное распознавание AI, глобальный затвор"
)


def order_ids(ordered):
    return [product.product_id for product in ordered]


class ExplicitNumericRequestTests(unittest.TestCase):
    """Group A: the Stage-9 B5/C3 shape - confirmed spec leads peers."""

    def test_65w_candidate_beats_comparable_candidate_without_wattage(
        self,
    ) -> None:
        request, _, _ = resolve_and_apply("зарядка USB C 65W")
        products = [
            make_listing("no-wattage", CHARGER_NO_WATTAGE, 10.0),
            make_listing("w65", CHARGER_65W, 10.0),
        ]
        kept, dropped = filter_relevant(products, request)
        self.assertFalse(dropped)  # no hard exclusion of either candidate
        ordered = sort_products(kept, request)
        self.assertEqual(order_ids(ordered), ["w65", "no-wattage"])

    def test_confirmed_numeric_spec_outranks_missing_one_in_adjustment(
        self,
    ) -> None:
        request, _, _ = resolve_and_apply("зарядка USB C 65W")
        with_spec = intent_score_adjustment(
            make_listing("a", CHARGER_65W), request)
        without_spec = intent_score_adjustment(
            make_listing("b", CHARGER_NO_WATTAGE), request)
        self.assertGreater(with_spec, without_spec)

    def test_the_extra_bonus_is_exactly_the_published_constant(self) -> None:
        # The numeric coverage adds exactly INTENT_NUMERIC_COVERAGE_BONUS
        # per confirmed spec on top of the generic attribute bonus.
        request, _, _ = resolve_and_apply("зарядка USB C 65W")
        one_spec = make_listing("a", CHARGER_65W)
        baseline = intent_score_adjustment(
            make_listing("b", CHARGER_NO_WATTAGE), request)
        with_spec = intent_score_adjustment(one_spec, request)
        self.assertEqual(
            round(with_spec - baseline, 4),
            round(INTENT_ATTRIBUTE_BONUS + INTENT_NUMERIC_COVERAGE_BONUS, 4),
        )

    def test_power_bank_with_both_specs_beats_capacity_only_peer(self) -> None:
        # C3 shape: same capacity, only one title confirms the 65 W spec.
        request, _, _ = resolve_and_apply("повербанк 20000 мАч 65W")
        products = [
            make_listing("capacity", BANK_NO_SPEC, 10.0),
            make_listing("both", BANK_65W, 10.0),
        ]
        kept, dropped = filter_relevant(products, request)
        self.assertFalse(dropped)
        ordered = sort_products(kept, request)
        self.assertEqual(order_ids(ordered), ["both", "capacity"])


class SpecEquivalenceTests(unittest.TestCase):
    """Group B: only unambiguous formatting is equivalent."""

    def test_65w_and_65_w_are_the_same_spec(self) -> None:
        request, _, _ = resolve_and_apply("зарядка USB C 65W")
        glued = _numeric_attribute_coverage(
            request.intent_required_attributes, CHARGER_65W.lower())
        spaced = _numeric_attribute_coverage(
            request.intent_required_attributes, CHARGER_65W_SPACED.lower())
        self.assertEqual(glued, 1)
        self.assertEqual(spaced, 1)

    def test_spaced_and_glued_titles_score_identically(self) -> None:
        # Equivalent formatting must not flip the score of two titles
        # that both confirm the same spec.
        request, _, _ = resolve_and_apply("зарядка USB C 65W")
        glued = relevance_score(make_listing("g", CHARGER_65W), request)
        spaced = relevance_score(
            make_listing("s", CHARGER_65W_SPACED), request)
        self.assertEqual(glued, spaced)

    def test_incompatible_value_is_never_counted_as_match(self) -> None:
        request, _, _ = resolve_and_apply("зарядка USB C 65W")
        self.assertEqual(
            _numeric_attribute_coverage(
                request.intent_required_attributes,
                CHARGER_WRONG_WATTAGE.lower()),
            0,
        )
        self.assertEqual(
            _numeric_attribute_coverage(
                request.intent_required_attributes, CHARGER_650W.lower()),
            0,
        )

    def test_number_unrelated_to_the_requested_attribute_counts_nothing(
        self,
    ) -> None:
        # «5 Вт» is a real number+unit, but it is not the REQUESTED
        # attribute («65 w»): no coverage, no bonus.
        request, _, _ = resolve_and_apply("зарядка USB C 65W")
        self.assertEqual(
            _numeric_attribute_coverage(
                request.intent_required_attributes,
                CHARGER_UNRELATED_NUMBER.lower()),
            0,
        )

    def test_a_bare_number_is_not_a_numeric_specification(self) -> None:
        self.assertTrue(_is_numeric_attribute("65 w"))
        self.assertTrue(_is_numeric_attribute("20000 мач"))


class MissingSpecIsNeutralTests(unittest.TestCase):
    """Group C: no evidence of a spec is not contradicting evidence."""

    def test_candidate_without_wattage_is_kept_not_rejected(self) -> None:
        request, _, _ = resolve_and_apply("зарядка USB C 65W")
        product = make_listing("b", CHARGER_NO_WATTAGE)
        self.assertIsNone(rejection_reason(product, request))
        kept, dropped = filter_relevant([product], request)
        self.assertEqual(len(kept), 1)
        self.assertFalse(dropped)

    def test_missing_spec_never_lowers_the_adjustment(self) -> None:
        # A title without the spec scores exactly as the pre-Stage-11
        # pipeline scored it: the generic attribute bonus alone, >= 0.
        request, _, _ = resolve_and_apply("зарядка USB C 65W")
        plain = make_listing("b", CHARGER_NO_WATTAGE)
        self.assertGreaterEqual(intent_score_adjustment(plain, request), 0.0)
        self.assertEqual(
            _numeric_attribute_coverage(
                request.intent_required_attributes,
                CHARGER_NO_WATTAGE.lower()),
            0,
        )


class TierDominanceTests(unittest.TestCase):
    """Group D: the tie-break reorders within a tier, never across tiers."""

    def test_exact_product_without_spec_stays_above_mentioned_with_spec(
        self,
    ) -> None:
        # A tier-2 «зарядка» leads with the requested product; a tier-1
        # accessory that confirms 65 W must not overtake it.
        request, _, _ = resolve_and_apply("зарядка USB C 65W")
        accessory = make_listing(
            "acc", "Кабель USB-C 65W для зарядки устройства", 10.0)
        exact = make_listing("exact", "Зарядка USB-C быстрая PD3.0", 10.0)
        self.assertEqual(relevance_tier(exact, request), 2)
        self.assertEqual(relevance_tier(accessory, request), 1)
        ordered = sort_products([accessory, exact], request)
        self.assertEqual(order_ids(ordered), ["exact", "acc"])

    def test_the_numeric_extra_is_smaller_than_the_attribute_bonus(
        self,
    ) -> None:
        # The bonus is deliberately tiny: it reorders ties only and can
        # never outweigh the generic per-attribute evidence signal.
        self.assertLess(
            INTENT_NUMERIC_COVERAGE_BONUS, INTENT_ATTRIBUTE_BONUS)


class ModeAndIntentIsolationTests(unittest.TestCase):
    """Group E: legacy ordering and non-SEARCH modes stay byte-identical."""

    def test_no_structured_intent_preserves_legacy_ordering(self) -> None:
        plain = SearchRequest(original_query="зарядка USB C 65W")
        for title in (CHARGER_65W, CHARGER_NO_WATTAGE, CHARGER_650W):
            product = make_listing("p", title)
            self.assertEqual(
                relevance_score(product, plain),
                _word_share_score(product, plain),
                title,
            )
            self.assertEqual(intent_score_adjustment(product, plain), 0.0)

    def test_non_search_modes_never_apply_the_tie_break(self) -> None:
        base = SearchRequest(original_query="зарядка USB C 65W")
        for mode in (SearchMode.CATEGORY, SearchMode.FEED, SearchMode.POPULAR,
                     SearchMode.DISCOVERY, SearchMode.IMAGE):
            request = base.model_copy(update={
                "mode": mode,
                "intent_product_type": "зарядка",
                "intent_required_attributes": ["65 w"],
            })
            product = make_listing("p", CHARGER_65W)
            self.assertEqual(intent_score_adjustment(product, request), 0.0)
            self.assertEqual(
                relevance_score(product, request),
                _word_share_score(product, request),
            )


class GuardRegressionTests(unittest.TestCase):
    """Group F: Stage-10 subtype guard and G24.6/G24.8 guards hold."""

    def test_stage10_machine_vision_guard_remains_effective(self) -> None:
        request, _, _ = resolve_and_apply("камера 4K для машины")
        product = make_listing("mv", MACHINE_VISION_CAMERA)
        self.assertNotEqual(classify_product(product, request), "exact")
        self.assertNotEqual(relevance_tier(product, request), 2)
        self.assertIsNone(rejection_reason(product, request))

    def test_g246_cross_script_behaviour_is_untouched(self) -> None:
        # A latin title for a cyrillic request keeps its cross-writing
        # evidence path: no intent fields -> plain word-share score.
        plain = SearchRequest(original_query="мощный блок питания")
        product = make_listing(
            "x", "High Power Supply Unit 65W black adapter")
        self.assertEqual(
            relevance_score(product, plain),
            _word_share_score(product, plain),
        )

    def test_g248_category_mode_keeps_r3_only(self) -> None:
        # CATEGORY keeps the word-level gate: no product-type tier, no
        # intent adjustment even when intent fields are present.
        request = SearchRequest(
            original_query="сад",
            mode=SearchMode.CATEGORY,
            intent_product_type="мебель",
            intent_required_attributes=["65 w"],
        )
        product = make_listing("p", "Садовая мебель комплект")
        self.assertEqual(relevance_tier(product, request), 1)
        self.assertEqual(intent_score_adjustment(product, request), 0.0)
        self.assertEqual(
            relevance_score(product, request),
            _word_share_score(product, request),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

