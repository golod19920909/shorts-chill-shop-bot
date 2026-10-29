# -*- coding: utf-8 -*-
"""G25.2-D Stage 31 — harden English «without» boundaries (OFFLINE).

Closes the 7 UNSAFE derived-variant leaks the Stage-30 read-only audit
confirmed (Title-Case brand-led exclusions, hyphen/slash boundaries, a short
ALL-CAPS abbreviation, and a >3-word exclusion that dropped the head noun).
Exercises the REAL production path: services.query_intent._detect_exclusions +
tests.test_search_intent.resolve_and_apply (AIService(api_key=None) local
fallback, NO Gemini) + services.query_variants.product_query_variants. The
production change is query_intent.py ONLY; the Stage-25 guard in
query_variants.py is untouched. No network/provider/AI call.
"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "t_search_intent_31", ROOT / "tests" / "test_search_intent.py")
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
    """Derived variants (beyond primary) reintroducing ANY excluded stem."""
    stems = _excluded_stems(request)
    return [v for v in variants[1:]
            if any(_stem(w) in stems for w in v.split() if len(w) >= 3)]


# (query, head-noun that must NOT reappear positively in a derived variant)
SEVEN_UNSAFE = [
    ("laptop without USB-C charger", "charger"),
    ("camera without SD card", "card"),
    ("phone without an adapter/charger", "charger"),
    ("drill without rechargeable lithium ion battery", "battery"),
    ("drill without Bosch battery", "battery"),
    ("phone without Samsung charger", "charger"),
    ("headphones without AirPods case", "case"),
]


class SevenUnsafeLeaksClosed(unittest.TestCase):
    def test_head_noun_is_protected(self) -> None:
        for q, head in SEVEN_UNSAFE:
            req = request_for(q)
            stems = _excluded_stems(req)
            self.assertIn(
                head.casefold()[:4], stems,
                f"{q!r}: head noun {head!r} must reach the guard stems")

    def test_no_unsafe_derived_variant(self) -> None:
        for q, _ in SEVEN_UNSAFE:
            req = request_for(q)
            variants = product_query_variants(req)
            self.assertEqual(
                _unsafe_derived(req, variants), [],
                f"{q!r} must not reintroduce the excluded item positively")
            # The parser may normalize/reorder the primary (e.g. pull a spec
            # «usb-c» out), but the negation word MUST survive in it.
            self.assertIn("without", variants[0].casefold(),
                          f"{q!r}: primary must keep the negation")

    def test_head_noun_not_in_any_derived_variant(self) -> None:
        for q, head in SEVEN_UNSAFE:
            req = request_for(q)
            for v in product_query_variants(req)[1:]:
                self.assertNotIn(
                    head[:4].casefold(),
                    [w[:4].casefold() for w in v.split()],
                    f"{q!r}: derived {v!r} must not name {head!r}")


class BrandPreservedNotExcluded(unittest.TestCase):
    def test_bosch_battery_excludes_only_battery(self) -> None:
        self.assertEqual(_detect_exclusions("drill without Bosch battery"),
                         ["battery"])

    def test_samsung_charger_excludes_only_charger(self) -> None:
        self.assertEqual(_detect_exclusions("phone without Samsung charger"),
                         ["charger"])

    def test_airpods_case_excludes_only_case(self) -> None:
        self.assertEqual(_detect_exclusions("headphones without AirPods case"),
                         ["case"])

    def test_brand_survives_in_derived_variant(self) -> None:
        # Bosch is NOT an excluded stem, so it may still appear in a broader
        # derived variant while the excluded noun (battery) stays out.
        req = request_for("drill without Bosch battery")
        variants = product_query_variants(req)
        self.assertEqual(variants[0], "drill without Bosch battery")
        self.assertIn("drill bosch", variants)
        for v in variants[1:]:
            self.assertNotIn("battery", v.casefold())


class AbbreviationAndSeparatorBoundaries(unittest.TestCase):
    def test_short_allcaps_abbreviation_kept(self) -> None:
        # «SD» must not be dropped for being < 3 chars.
        self.assertEqual(_detect_exclusions("camera without SD card"),
                         ["SD card"])

    def test_hyphen_internal(self) -> None:
        self.assertEqual(_detect_exclusions("laptop without USB-C charger"),
                         ["USB charger"])

    def test_slash_internal(self) -> None:
        self.assertEqual(_detect_exclusions("phone without an adapter/charger"),
                         ["adapter charger"])

    def test_allcaps_phrase(self) -> None:
        self.assertEqual(
            _detect_exclusions("laptop without USB CABLE"), ["USB CABLE"])


class LongerExclusionHeadNounPreserved(unittest.TestCase):
    def test_four_word_phrase_keeps_head_noun(self) -> None:
        self.assertEqual(
            _detect_exclusions(
                "drill without rechargeable lithium ion battery"),
            ["rechargeable lithium ion battery"])

    def test_stopword_truncates_phrase(self) -> None:
        # «for travel» is not part of the exclusion.
        self.assertEqual(
            _detect_exclusions("laptop without power adapter for travel"),
            ["power adapter"])

    def test_conjunction_truncates_phrase(self) -> None:
        self.assertEqual(
            _detect_exclusions("phone without charger and cable"),
            ["charger"])


class SingleWordAndMultiwordUnchanged(unittest.TestCase):
    """Stages 26-29 lower-case / ALL-CAPS results must be byte-identical."""

    def test_single_word(self) -> None:
        for q, e in (("vacuum cleaner without bag", ["bag"]),
                     ("phone without charger", ["charger"]),
                     ("drill without battery", ["battery"])):
            self.assertEqual(_detect_exclusions(q), e, q)

    def test_articles(self) -> None:
        self.assertEqual(_detect_exclusions("drill without the battery"),
                         ["battery"])
        self.assertEqual(_detect_exclusions("  vacuum   without   a   bag  "),
                         ["bag"])

    def test_multiword(self) -> None:
        self.assertEqual(
            _detect_exclusions("vacuum cleaner without power cable"),
            ["power cable"])
        self.assertEqual(
            _detect_exclusions("vacuum cleaner without a power cable"),
            ["power cable"])

    def test_comma_and_punct_boundaries(self) -> None:
        self.assertEqual(_detect_exclusions("drill without battery, brush"),
                         ["battery"])
        self.assertEqual(_detect_exclusions("phone without charger."),
                         ["charger"])
        self.assertEqual(_detect_exclusions("phone without a charger, Samsung"),
                         ["charger"])

    def test_brand_suffix_not_swallowed(self) -> None:
        self.assertEqual(
            _detect_exclusions("vacuum cleaner without power cable Bosch"),
            ["power cable"])
        self.assertEqual(
            _detect_exclusions("headphones without the charging case Samsung"),
            ["charging case"])

    def test_allcaps_preserved(self) -> None:
        self.assertEqual(
            _detect_exclusions("VACUUM CLEANER WITHOUT POWER CABLE"),
            ["POWER CABLE"])

    def test_bare_and_empty_rejected(self) -> None:
        self.assertEqual(_detect_exclusions("cleaner without"), [])
        self.assertEqual(_detect_exclusions("x without "), [])
        self.assertEqual(_detect_exclusions("vacuum cleaner without ..."), [])
        self.assertEqual(_detect_exclusions("vacuum cleaner without the"), [])

class NonNegationControls(unittest.TestCase):
    def test_no_without_no_exclusion(self) -> None:
        for q in ("case for iphone 15", "battery for drill",
                  "bagless vacuum cleaner", "wireless headphones",
                  "power cable for laptop", "charger included",
                  "phone charger Samsung", "compatible with Bosch battery"):
            self.assertEqual(request_for(q).intent_excluded_product_types, [],
                             f"{q!r} must NOT acquire an exclusion")

    def test_compatible_with_not_negation(self) -> None:
        self.assertEqual(_detect_exclusions("compatible with Bosch battery"),
                         [])


class RussianRegression(unittest.TestCase):
    def test_vacuum_bagless_bosch(self) -> None:
        req = request_for("пылесос без мешка Bosch")
        self.assertTrue(req.intent_excluded_product_types)
        self.assertEqual(
            _unsafe_derived(req, product_query_variants(req)), [])

    def test_headphones_no_case_samsung(self) -> None:
        req = request_for("наушники без чехла Samsung")
        self.assertTrue(req.intent_excluded_product_types)
        self.assertEqual(
            _unsafe_derived(req, product_query_variants(req)), [])

    def test_bagless_brandless_single_variant(self) -> None:
        self.assertEqual(variants_for("пылесос без мешка"),
                         ["пылесос"])


class Invariants(unittest.TestCase):
    def _check(self, q: str) -> None:
        variants = variants_for(q)
        self.assertTrue(variants)
        self.assertTrue(all(v.strip() for v in variants))
        folded = [v.casefold() for v in variants]
        self.assertEqual(len(folded), len(set(folded)))
        self.assertLessEqual(len(variants), MAX_PRODUCT_QUERY_VARIANTS)

    def test_invariants_across_all_cases(self) -> None:
        for q, _ in SEVEN_UNSAFE:
            self._check(q)
        for q in ("vacuum cleaner without power cable",
                  "headphones without the charging case Samsung",
                  "case for iphone 15", "battery for drill",
                  "пылесос без мешка Bosch", "compatible with Bosch battery"):
            self._check(q)

    def test_cap_unchanged(self) -> None:
        self.assertEqual(MAX_PRODUCT_QUERY_VARIANTS, 3)

    def test_deterministic(self) -> None:
        for q, _ in SEVEN_UNSAFE:
            self.assertEqual(variants_for(q), variants_for(q))


if __name__ == "__main__":
    unittest.main()

