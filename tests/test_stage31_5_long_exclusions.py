# -*- coding: utf-8 -*-
"""G25.2-D Stage 31.5 — long English «without» exclusions (Strategy C).

Exercises the REAL production path: services.query_intent._detect_exclusions +
tests.test_search_intent.resolve_and_apply (AIService(api_key=None) local
fallback, NO Gemini) + services.query_variants.product_query_variants. The
production change is services/query_intent.py ONLY (raise the single-item
capture bound so the trailing lower-case head noun of a long descriptive
«without» phrase is protected); the Stage-25 guard in query_variants.py is
untouched. Brands (Title-Case) still never enter the exclusion; stop-words and
punctuation still bound the scan. No network/provider/AI call.
"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "t_search_intent_315", ROOT / "tests" / "test_search_intent.py")
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


def _leaks(request, variants: list[str], nouns) -> list[str]:
    """Derived variants (beyond primary) that positively name an excluded noun."""
    stems = {n.casefold()[:4] for n in nouns}
    return [v for v in variants[1:]
            if any(_stem(w) in stems for w in v.split() if len(w) >= 3)]


# (query, expected excluded string, head nouns that must reach the guard)
TEN = [
    ("drill without high capacity rechargeable lithium ion battery",
     "high capacity rechargeable lithium ion battery", ["battery"]),
    ("drill without extra high capacity rechargeable lithium ion battery",
     "extra high capacity rechargeable lithium ion battery", ["battery"]),
    ("drill without a high capacity rechargeable lithium ion battery",
     "high capacity rechargeable lithium ion battery", ["battery"]),
    ("drill without high capacity lithium ion battery",
     "high capacity lithium ion battery", ["battery"]),
    ("drill without a high capacity lithium ion battery",
     "high capacity lithium ion battery", ["battery"]),
    ("drill without Bosch high capacity rechargeable lithium ion battery",
     "high capacity rechargeable lithium ion battery", ["battery"]),
    ("drill without high capacity rechargeable lithium ion battery Bosch",
     "high capacity rechargeable lithium ion battery", ["battery"]),
    ("drill without extra high capacity rechargeable lithium ion battery Bosch",
     "extra high capacity rechargeable lithium ion battery", ["battery"]),
    ("laptop without high capacity rechargeable lithium ion battery",
     "high capacity rechargeable lithium ion battery", ["battery"]),
    ("vacuum cleaner without high capacity rechargeable lithium ion bag",
     "high capacity rechargeable lithium ion bag", ["bag"]),
]


class TenLongExclusions(unittest.TestCase):
    def test_head_noun_protected(self) -> None:
        for q, _, nouns in TEN:
            stems = _excluded_stems(request_for(q))
            for n in nouns:
                self.assertIn(n.casefold()[:4], stems,
                              f"{q!r}: {n!r} must reach guard stems")

    def test_no_unsafe_derived_variant(self) -> None:
        for q, _, nouns in TEN:
            req = request_for(q)
            variants = product_query_variants(req)
            self.assertEqual(
                _leaks(req, variants, nouns), [],
                f"{q!r} reintroduces an excluded head noun positively")

    def test_primary_preserved(self) -> None:
        for q, _, _ in TEN:
            self.assertIn("without", variants_for(q)[0].casefold(),
                          f"{q!r}: primary must keep the negation")

    def test_deterministic(self) -> None:
        for q, _, _ in TEN:
            self.assertEqual(variants_for(q), variants_for(q))

class BrandBoundary(unittest.TestCase):
    def test_bosch_battery(self) -> None:
        self.assertEqual(_detect_exclusions("drill without Bosch battery"),
                         ["battery"])

    def test_bosch_long_brand_stays_out_of_exclusion(self) -> None:
        self.assertEqual(
            _detect_exclusions(
                "drill without Bosch high capacity rechargeable lithium "
                "ion battery"),
            ["high capacity rechargeable lithium ion battery"])

    def test_trailing_bosch_stays_out_of_exclusion(self) -> None:
        self.assertEqual(
            _detect_exclusions(
                "drill without high capacity rechargeable lithium ion "
                "battery Bosch"),
            ["high capacity rechargeable lithium ion battery"])

    def test_samsung_charger(self) -> None:
        self.assertEqual(_detect_exclusions("phone without Samsung charger"),
                         ["charger"])

    def test_airpods_case(self) -> None:
        self.assertEqual(_detect_exclusions("headphones without AirPods case"),
                         ["case"])


class NoRunaway(unittest.TestCase):
    def test_stopword_for_still_truncates(self) -> None:
        self.assertEqual(
            _detect_exclusions(
                "drill without high capacity battery for home use"),
            ["high capacity battery"])

    def test_short_multiword_unchanged(self) -> None:
        self.assertEqual(
            _detect_exclusions("drill without rechargeable lithium ion "
                               "battery"),
            ["rechargeable lithium ion battery"])
        self.assertEqual(
            _detect_exclusions("vacuum cleaner without power cable"),
            ["power cable"])

    def test_single_word_unchanged(self) -> None:
        self.assertEqual(_detect_exclusions("vacuum cleaner without bag"),
                         ["bag"])
        self.assertEqual(_detect_exclusions("phone without charger"),
                         ["charger"])


class LegacyAndControls(unittest.TestCase):
    def test_legacy_comma_boundary(self) -> None:
        self.assertEqual(_detect_exclusions("drill without battery, brush"),
                         ["battery"])

    def test_comma_list_full(self) -> None:
        self.assertEqual(
            _detect_exclusions("laptop without charger, cable, and adapter"),
            ["charger", "cable", "adapter"])

    def test_positive_query_no_exclusion(self) -> None:
        for q in ("laptop charger, cable, and adapter",
                  "vacuum cleaner with bag",
                  "compatible with Bosch battery"):
            self.assertEqual(request_for(q).intent_excluded_product_types, [],
                             f"{q!r} must NOT create an exclusion")

    def test_russian_unchanged(self) -> None:
        self.assertEqual(_detect_exclusions("пылесос без мешка"), ["мешка"])


class Invariants(unittest.TestCase):
    def _check(self, q: str) -> None:
        variants = variants_for(q)
        self.assertTrue(variants)
        self.assertTrue(all(v.strip() for v in variants))
        folded = [v.casefold() for v in variants]
        self.assertEqual(len(folded), len(set(folded)))
        self.assertLessEqual(len(variants), MAX_PRODUCT_QUERY_VARIANTS)

    def test_invariants(self) -> None:
        for q, _, _ in TEN:
            self._check(q)
        for q in ("drill without battery, brush", "case for iphone 15",
                  "пылесос без мешка Bosch"):
            self._check(q)

    def test_cap_unchanged(self) -> None:
        self.assertEqual(MAX_PRODUCT_QUERY_VARIANTS, 3)


if __name__ == "__main__":
    unittest.main()

