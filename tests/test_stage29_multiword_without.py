# -*- coding: utf-8 -*-
"""G25.2-D Stage 29 — multi-word English «without X» exclusion fix (OFFLINE).

Exercises the REAL production path: services.query_intent._detect_exclusions
+ tests.test_search_intent.resolve_and_apply (AIService(api_key=None) local
fallback, NO Gemini) + services.query_variants.product_query_variants.
The production change (query_intent.py) extends the Stage-27 rule to a
BOUNDED 1-3 word exclusion noun phrase, so the Stage-25 guard protects the
head noun too. No network/provider/AI call. No existing test modified.
"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "t_search_intent_29", ROOT / "tests" / "test_search_intent.py")
t_si = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(t_si)

from services.query_intent import _detect_exclusions  # noqa: E402
from services.query_variants import (  # noqa: E402
    MAX_PRODUCT_QUERY_VARIANTS,
    product_query_variants,
    _excluded_stems,
)


def variants_for(text: str) -> list[str]:
    request, _, _ = t_si.resolve_and_apply(text)
    return product_query_variants(request)


def request_for(text: str):
    request, _, _ = t_si.resolve_and_apply(text)
    return request


def _stem(word: str) -> str:
    return word.casefold()[:4]


def _unsafe_derived(request, variants: list[str]) -> list[str]:
    """Derived variants reintroducing ANY excluded stem as a positive term."""
    stems = _excluded_stems(request)
    return [v for v in variants[1:]
            if any(_stem(w) in stems for w in v.split() if len(w) >= 3)]


class MultiWordConfirmedCases(unittest.TestCase):
    CASES = [
        ("vacuum cleaner without power cable", {"power", "cable"}),
        ("vacuum cleaner without charging case", {"charging", "case"}),
        ("headphones without charging case", {"charging", "case"}),
        ("phone without power adapter", {"power", "adapter"}),
        ("drill without rechargeable battery", {"rechargeable", "battery"}),
        ("laptop without power adapter", {"power", "adapter"}),
        ("car without spare wheel", {"spare", "wheel"}),
        ("camera without memory card", {"memory", "card"}),
    ]

    def test_exclusion_captures_full_noun_phrase(self) -> None:
        for q, words in self.CASES:
            excl = _detect_exclusions(q)
            self.assertEqual(len(excl), 1, q)
            got = {t.casefold() for t in excl[0].split()}
            self.assertEqual(got, words, q)

    def test_guard_protects_both_words(self) -> None:
        for q, _ in self.CASES:
            req = request_for(q)
            stems = _excluded_stems(req)
            # Both meaningful words reach the stem set.
            self.assertGreaterEqual(len(stems), 2, f"{q!r} stems={stems}")

    def test_no_unsafe_derived_variant(self) -> None:
        for q, _ in self.CASES:
            req = request_for(q)
            variants = product_query_variants(req)
            self.assertEqual(_unsafe_derived(req, variants), [],
                             f"{q!r} reintroduces an excluded word")
            self.assertEqual(variants[0], " ".join(q.split()),
                             "primary preserved")
class BoundaryVariations(unittest.TestCase):
    def test_article_a(self) -> None:
        self.assertEqual(_detect_exclusions("vacuum cleaner without a power "
                                            "cable"), ["power cable"])

    def test_article_the_with_brand(self) -> None:
        self.assertEqual(_detect_exclusions("headphones without the charging "
                                            "case Samsung"), ["charging case"])

    def test_brand_suffix_not_swallowed(self) -> None:
        self.assertEqual(_detect_exclusions("vacuum cleaner without power "
                                            "cable Bosch"), ["power cable"])

    def test_comma_boundary(self) -> None:
        self.assertEqual(_detect_exclusions("phone without a charger, "
                                            "Samsung"), ["charger"])

    def test_all_caps(self) -> None:
        self.assertEqual(_detect_exclusions("VACUUM CLEANER WITHOUT POWER "
                                            "CABLE"), ["POWER CABLE"])

    def test_bare_without_rejected(self) -> None:
        self.assertEqual(_detect_exclusions("cleaner without"), [])

    def test_without_then_empty_rejected(self) -> None:
        self.assertEqual(_detect_exclusions("x without "), [])

    def test_brand_preserved_in_derived_variant(self) -> None:
        # brand 'bosch' survives in a derived variant; excluded noun absent.
        variants = variants_for("vacuum cleaner without power cable Bosch")
        self.assertEqual(variants[0],
                         "vacuum cleaner without power cable Bosch")
        self.assertIn("vacuum cleaner bosch", variants)
        for v in variants[1:]:
            self.assertNotIn("cable", v.casefold())


class SingleWordEnglishUnchanged(unittest.TestCase):
    def test_vacuum_without_bag(self) -> None:
        self.assertEqual(_detect_exclusions("vacuum cleaner without bag"),
                         ["bag"])
        self.assertEqual(variants_for("vacuum cleaner without bag")[0],
                         "vacuum cleaner without bag")

    def test_phone_without_charger(self) -> None:
        self.assertEqual(_detect_exclusions("phone without charger"),
                         ["charger"])

    def test_headphones_without_case_samsung(self) -> None:
        req = request_for("headphones without case Samsung")
        self.assertEqual(req.intent_excluded_product_types, ["case"])
        variants = product_query_variants(req)
        self.assertEqual(_unsafe_derived(req, variants), [])


class RussianRegression(unittest.TestCase):
    def test_vacuum_bagless_bosch(self) -> None:
        req = request_for("пылесос без мешка Bosch")
        self.assertTrue(req.intent_excluded_product_types)
        self.assertEqual(_unsafe_derived(req, product_query_variants(req)), [])

    def test_headphones_no_case_samsung(self) -> None:
        req = request_for("наушники без чехла Samsung")
        self.assertTrue(req.intent_excluded_product_types)
        self.assertEqual(_unsafe_derived(req, product_query_variants(req)), [])

    def test_bagless_brandless_single_variant(self) -> None:
        self.assertEqual(variants_for("пылесос без мешка"),
                         ["пылесос"])


class NonNegationControls(unittest.TestCase):
    def test_case_for_iphone(self) -> None:
        req = request_for("case for iphone 15")
        self.assertEqual(req.intent_excluded_product_types, [])
        self.assertEqual(variants_for("case for iphone 15")[0],
                         "case Apple iphone 15")

    def test_battery_for_drill(self) -> None:
        self.assertEqual(request_for("battery for drill")
                         .intent_excluded_product_types, [])

    def test_bagless_feature_not_negation(self) -> None:
        self.assertEqual(request_for("bagless vacuum cleaner")
                         .intent_excluded_product_types, [])

    def test_wireless_feature_not_negation(self) -> None:
        self.assertEqual(request_for("wireless headphones")
                         .intent_excluded_product_types, [])

    def test_power_cable_for_laptop(self) -> None:
        self.assertEqual(request_for("power cable for laptop")
                         .intent_excluded_product_types, [])


class Invariants(unittest.TestCase):
    def _check(self, q: str) -> None:
        variants = variants_for(q)
        self.assertTrue(variants)
        self.assertTrue(all(v.strip() for v in variants))
        folded = [v.casefold() for v in variants]
        self.assertEqual(len(folded), len(set(folded)))
        self.assertLessEqual(len(variants), MAX_PRODUCT_QUERY_VARIANTS)

    def test_invariants_across_cases(self) -> None:
        for q in ("vacuum cleaner without power cable",
                  "vacuum cleaner without power cable Bosch",
                  "headphones without the charging case Samsung",
                  "phone without a charger, Samsung", "vacuum cleaner without "
                  "bag", "case for iphone 15", "пылесос без мешка Bosch"):
            self._check(q)

    def test_cap_unchanged(self) -> None:
        self.assertEqual(MAX_PRODUCT_QUERY_VARIANTS, 3)

    def test_deterministic(self) -> None:
        for q in ("vacuum cleaner without power cable",
                  "drill without rechargeable battery"):
            self.assertEqual(variants_for(q), variants_for(q))


if __name__ == "__main__":
    unittest.main()

