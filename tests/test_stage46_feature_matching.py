# -*- coding: utf-8 -*-
"""Stage 46 — regression tests for generalized required-feature matching.

Stage 45 found the hard ``required_features`` filter rejecting suitable
products because EQUIVALENT representations of the SAME value did not match
(«650 мм» vs «650mm», «блютуз» vs «Bluetooth»), while cross-language ignoring
silently dropped a hard capacity constraint («20000mAh» vs «20000 мАч»). The fix
canonicalises tokens in ``services.text_matching`` (number+unit split, unit
alias fold across case/script, a tiny cross-script term map) and makes NUMBERS
match exactly.

These tests fix BOTH directions: false negatives (equivalent spellings now
match) and false positives (a different number/unit/type is still rejected).
Offline: plain ``Product`` objects, no network, no live AliExpress titles.
"""

from __future__ import annotations

import unittest

from models.product import Marketplace, Product
from models.search_request import SearchRequest
from services.comparison_service import filter_products
from services.text_matching import can_ever_match, phrase_in_text


def make_product(title: str) -> Product:
    return Product(
        marketplace=Marketplace.ALIEXPRESS,
        product_id=title[:20],
        title=title,
        product_url="https://www.aliexpress.com/item/1.html",
        price=10.0,
        currency="USD",
    )


def surviving(request: SearchRequest, titles: list[str]) -> list[str]:
    kept = filter_products([make_product(t) for t in titles], request)
    return [p.title for p in kept]


class EquivalentRepresentationTests(unittest.TestCase):
    """(1)(2)(3) equivalent spellings / spacing / units must MATCH."""

    def test_unit_spacing_and_script_equivalence(self) -> None:
        self.assertTrue(phrase_in_text("дворники бескаркасные 650мм", "650 мм"))
        self.assertTrue(phrase_in_text("щетка 650 mm", "650мм"))
        self.assertTrue(phrase_in_text("Пауэрбанк 20000 мАч", "20000mAh"))
        self.assertTrue(phrase_in_text("Powerbank 20000mAh", "20000 мАч"))

    def test_established_cross_script_term(self) -> None:
        self.assertTrue(phrase_in_text("Наушники Bluetooth 5.3", "блютуз"))
        self.assertTrue(phrase_in_text("Наушники блютуз", "bluetooth"))

    def test_capacity_constraint_keeps_the_right_product(self) -> None:
        # #126 live case: «20000mAh» used to be cross-language-ignored (dropped);
        # now it is a real hard filter that KEEPS the 20000 мАч bank.
        req = SearchRequest(original_query="powerbank 20000",
                            normalized_query="powerbank",
                            required_features=["20000mAh"])
        kept = surviving(req, [
            "Пауэрбанк 20000 мАч быстрая зарядка",
            "Пауэрбанк 10000 мАч компактный",
        ])
        self.assertEqual(kept, ["Пауэрбанк 20000 мАч быстрая зарядка"])

    def test_wiper_size_constraint_keeps_the_right_product(self) -> None:
        # #110 live case: «650 мм» used to reject every candidate; now the
        # correctly-sized wipers survive while a different size is rejected.
        req = SearchRequest(original_query="дворники 650",
                            normalized_query="дворники",
                            required_features=["650 мм"])
        kept = surviving(req, [
            "Бескаркасные дворники 650мм",
            "Дворники 550мм универсальные",
        ])
        self.assertEqual(kept, ["Бескаркасные дворники 650мм"])

    def test_bluetooth_headphones_no_longer_emptied(self) -> None:
        # #128 live case: «блютуз» used to reject every Bluetooth headphone.
        req = SearchRequest(original_query="наушники блютуз",
                            normalized_query="наушники",
                            required_features=["беспроводные", "блютуз"])
        kept = surviving(req, [
            "Беспроводные Bluetooth наушники SoundPEATS",
            "Беспроводные наушники 2.4 GHz радио",
        ])
        self.assertEqual(kept, ["Беспроводные Bluetooth наушники SoundPEATS"])


class HardConstraintStillStrictTests(unittest.TestCase):
    """(4) a genuinely different value must STILL be rejected."""

    def test_different_capacity_fails(self) -> None:
        self.assertFalse(phrase_in_text("Пауэрбанк 20001 мАч", "20000mAh"))
        self.assertFalse(phrase_in_text("Пауэрбанк 10000 мАч", "20000 мАч"))

    def test_different_unit_fails(self) -> None:
        self.assertFalse(phrase_in_text("Шланг 650 см", "650 мм"))

    def test_numbers_do_not_prefix_match(self) -> None:
        # The latent defect: long numbers used to match by 4-char prefix.
        self.assertFalse(phrase_in_text("SSD 20001 ГБ", "20000"))

    def test_different_dimension_filtered_out(self) -> None:
        req = SearchRequest(original_query="дворники 650",
                            normalized_query="дворники",
                            required_features=["650 мм"])
        kept = surviving(req, ["Дворники 600мм", "Дворники 700мм"])
        self.assertEqual(kept, [])
    def test_different_dimension_filtered_out(self) -> None:
        req = SearchRequest(original_query="дворники 650",
                            normalized_query="дворники",
                            required_features=["650 мм"])
        kept = surviving(req, ["Дворники 600мм", "Дворники 700мм"])
        self.assertEqual(kept, [])


class CrossLanguageConstraintTests(unittest.TestCase):
    """(3 cont.) cross-script number/term features must NOT be silently ignored."""

    def test_numeric_feature_not_ignored_across_script(self) -> None:
        self.assertTrue(can_ever_match("20000mAh", ["Пауэрбанк 20000 мАч"]))

    def test_cross_script_term_not_ignored(self) -> None:
        self.assertTrue(can_ever_match("блютуз", ["Bluetooth headphones"]))

    def test_genuine_cross_language_word_still_ignored(self) -> None:
        # Preserved: a real cross-language WORD feature against an opposite-
        # script result set is still reported inapplicable (existing behavior).
        self.assertFalse(can_ever_match("для жены", ["gift for wife"]))


class ModelCompatibilityTests(unittest.TestCase):
    """(5) compatible vs incompatible device, using existing semantics."""

    def test_compatible_model_survives_incompatible_rejected(self) -> None:
        req = SearchRequest(original_query="чехол для iPhone 15",
                            normalized_query="чехол iPhone 15",
                            required_features=["iPhone 15"],
                            excluded_features=["Galaxy"])
        kept = surviving(req, [
            "Чехол для iPhone 15 Pro силиконовый",
            "Чехол для Samsung Galaxy S24",
        ])
        self.assertEqual(kept, ["Чехол для iPhone 15 Pro силиконовый"])


class WrongProductTypeTests(unittest.TestCase):
    """(6) matches required feature but wrong product type -> relevance gate."""

    def test_feature_matches_but_kept_by_feature_filter(self) -> None:
        # The FEATURE filter matches on the stated value; product-noun
        # suitability is the relevance gate's job, not this matcher's. Assert
        # the feature filter behaves predictably (keeps on a feature match).
        req = SearchRequest(original_query="powerbank 20000",
                            normalized_query="powerbank",
                            required_features=["20000mAh"])
        kept = surviving(req, ["Power Bank 20000 мАч", "Аккумулятор 20000 мАч 18650"])
        self.assertEqual(len(kept), 2)


class NegationBehaviorPreservedTests(unittest.TestCase):
    """(7) Stage 44-B6/B7 negation behavior unchanged."""

    def test_bagless_vacuum_not_hard_filtered_on_negation(self) -> None:
        # «без мешка» is intent, never a literal required feature.
        req = SearchRequest(original_query="пылесос без мешка",
                            normalized_query="bagless vacuum cleaner",
                            required_features=["без мешка"])
        kept = surviving(req, ["Пылесос циклонный беспроводной"])
        self.assertEqual(kept, ["Пылесос циклонный беспроводной"])

    def test_multi_item_exclusion_still_excludes(self) -> None:
        req = SearchRequest(original_query="vacuum without bag, brush, filter",
                            normalized_query="vacuum",
                            excluded_features=["bag", "brush", "filter"])
        kept = surviving(req, [
            "Bagless vacuum cleaner HEPA",
            "Vacuum with dust bag",
        ])
        self.assertEqual(kept, ["Bagless vacuum cleaner HEPA"])


class NoSubstringFalsePositiveTests(unittest.TestCase):
    """(8) unrelated products must not pass via fuzzy/substring matching."""

    def test_conducting_not_matched_inside_wireless(self) -> None:
        # The invariant this module was built for must survive canonicalisation.
        self.assertFalse(phrase_in_text("Наушники беспроводные", "проводные"))

    def test_different_value_dropped_same_value_kept(self) -> None:
        req = SearchRequest(original_query="SSD 1 ТБ",
                            normalized_query="ssd",
                            required_features=["1 ТБ"])
        kept = surviving(req, ["SSD 2 ТБ", "SSD 1 ТБ"])
        self.assertEqual(kept, ["SSD 1 ТБ"])


class UnverifiableFeatureTests(unittest.TestCase):
    """(9) a feature that cannot be verified from evidence -> documented behavior."""

    def test_cross_language_unverifiable_feature_is_reported_ignored(self) -> None:
        # A same-language feature the title never states stays a genuine miss
        # (filters); a cross-language one is reported ignored, never invented.
        req = SearchRequest(original_query="фен тихий",
                            normalized_query="фен",
                            required_features=["бесшумный"])
        filter_products([make_product("Hair dryer professional 2200W")], req)
        # 'бесшумный' is cyrillic vs a latin-only title -> cross-language -> ignored.
        self.assertIn("бесшумный", req.ignored_features)

    def test_same_language_unverifiable_feature_still_filters(self) -> None:
        req = SearchRequest(original_query="фен бесшумный",
                            normalized_query="фен",
                            required_features=["бесшумный"])
        kept = surviving(req, ["Фен профессиональный 2200 Вт"])
        self.assertEqual(kept, [])  # genuine same-language miss keeps filtering


if __name__ == "__main__":
    unittest.main()

