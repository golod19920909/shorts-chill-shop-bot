# -*- coding: utf-8 -*-
"""G25.2-D Stage 31.3 — conservative English comma-separated «without» lists.

Exercises the REAL production path: services.query_intent._detect_exclusions +
tests.test_search_intent.resolve_and_apply (AIService(api_key=None) local
fallback, NO Gemini) + services.query_variants.product_query_variants. The
production change is services/query_intent.py ONLY; the Stage-25 guard in
query_variants.py is untouched and must protect EVERY excluded item. Scope is
comma-separated lists joined by and/or; long single-item phrases (>5 words)
are out of scope. No network/provider/AI call.
"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "t_search_intent_313", ROOT / "tests" / "test_search_intent.py")
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


def _reintroduces(request, variants: list[str], nouns) -> list[str]:
    """Derived variants (beyond primary) that positively name an excluded noun.

    ``nouns`` are the meaningful excluded words; a variant is unsafe only if it
    contains one of their stems (the primary keeps «without» and is exempt).
    """
    stems = {n.casefold()[:4] for n in nouns}
    return [v for v in variants[1:]
            if any(_stem(w) in stems for w in v.split() if len(w) >= 3)]


# (query, expected excluded strings, protected nouns that must reach the guard)
SEVEN = [
    ("laptop without charger, cable, and adapter",
     ["charger", "cable", "adapter"],
     ["charger", "cable", "adapter"]),
    ("laptop without charger, cable, or adapter",
     ["charger", "cable", "adapter"],
     ["charger", "cable", "adapter"]),
    ("drill without battery, charger, and case",
     ["battery", "charger", "case"],
     ["battery", "charger", "case"]),
    ("camera without lens, memory card, or battery",
     ["lens", "memory card", "battery"],
     ["lens", "memory", "card", "battery"]),
    ("laptop without power adapter, USB-C cable, and charger",
     ["power adapter", "USB cable", "charger"],
     ["power", "adapter", "usb", "cable", "charger"]),
    ("vacuum cleaner without bag, hose, and filter",
     ["bag", "hose", "filter"],
     ["bag", "hose", "filter"]),
    ("headphones without charging case, cable, or adapter",
     ["charging case", "cable", "adapter"],
     ["charging", "case", "cable", "adapter"]),
]


class SevenRequiredCases(unittest.TestCase):
    def test_all_items_detected(self) -> None:
        for q, expected, _ in SEVEN:
            self.assertEqual(_detect_exclusions(q), expected, q)

    def test_every_item_reaches_guard(self) -> None:
        for q, _, nouns in SEVEN:
            stems = _excluded_stems(request_for(q))
            for n in nouns:
                self.assertIn(n.casefold()[:4], stems,
                              f"{q!r}: {n!r} must reach guard stems")

    def test_no_unsafe_derived_variant(self) -> None:
        for q, _, nouns in SEVEN:
            req = request_for(q)
            variants = product_query_variants(req)
            self.assertEqual(
                _reintroduces(req, variants, nouns), [],
                f"{q!r} reintroduces an excluded noun positively")

    def test_primary_preserved(self) -> None:
        for q, _, _ in SEVEN:
            variants = variants_for(q)
            self.assertIn("without", variants[0].casefold(),
                          f"{q!r}: primary must keep the negation")

    def test_modifiers_preserved_in_primary(self) -> None:
        # useful modifiers survive in the primary query.
        self.assertIn("usb-c",
                      variants_for(
                          "laptop without power adapter, USB-C cable, "
                          "and charger")[0].casefold())
        self.assertIn("memory card",
                      variants_for(
                          "camera without lens, memory card, or battery"
                      )[0].casefold())
        self.assertIn("charging case",
                      variants_for(
                          "headphones without charging case, cable, "
                          "or adapter")[0].casefold())

    def test_deterministic(self) -> None:
        for q, _, _ in SEVEN:
            self.assertEqual(variants_for(q), variants_for(q))

class AdditionalListCases(unittest.TestCase):
    def test_phone_without_charger_cable(self) -> None:
        # No trailing and/or: comma boundary -> conservative, first item only.
        self.assertEqual(_detect_exclusions("phone without charger, cable"),
                         ["charger"])

    def test_phone_without_charger_and_cable(self) -> None:
        # Conjunction WITHOUT comma: existing behaviour keeps one item.
        self.assertEqual(_detect_exclusions("phone without charger and cable"),
                         ["charger"])

    def test_phone_without_charger_or_cable(self) -> None:
        self.assertEqual(_detect_exclusions("phone without charger or cable"),
                         ["charger"])

    def test_power_adapter_usbc_charger_full_list(self) -> None:
        excl = _detect_exclusions(
            "laptop without power adapter, USB-C cable, and charger")
        self.assertEqual(excl, ["power adapter", "USB cable", "charger"])

    def test_charging_case_cable_adapter_full_list(self) -> None:
        excl = _detect_exclusions(
            "headphones without charging case, cable, or adapter")
        self.assertEqual(excl, ["charging case", "cable", "adapter"])


class LegacyBoundaryPreserved(unittest.TestCase):
    def test_drill_without_battery_brush_first_noun_only(self) -> None:
        # ESTABLISHED Stage-27 expectation must remain unchanged.
        self.assertEqual(_detect_exclusions("drill without battery, brush"),
                         ["battery"])

    def test_drill_without_battery_bosch(self) -> None:
        self.assertEqual(_detect_exclusions("drill without battery (Bosch)"),
                         ["battery"])

    def test_phone_without_adapter_slash_charger(self) -> None:
        self.assertEqual(_detect_exclusions("phone without an adapter/charger"),
                         ["adapter charger"])

    def test_laptop_without_usbc_charger(self) -> None:
        self.assertEqual(_detect_exclusions("laptop without USB-C charger"),
                         ["USB charger"])

    def test_camera_without_sd_card(self) -> None:
        self.assertEqual(_detect_exclusions("camera without SD card"),
                         ["SD card"])

    def test_drill_without_bosch_battery(self) -> None:
        self.assertEqual(_detect_exclusions("drill without Bosch battery"),
                         ["battery"])

    def test_single_word_unchanged(self) -> None:
        self.assertEqual(_detect_exclusions("vacuum cleaner without bag"),
                         ["bag"])
        self.assertEqual(_detect_exclusions("phone without charger"),
                         ["charger"])

    def test_multiword_unchanged(self) -> None:
        self.assertEqual(
            _detect_exclusions("vacuum cleaner without power cable"),
            ["power cable"])


class NoNegationControls(unittest.TestCase):
    def test_positive_list_no_false_exclusion(self) -> None:
        for q in ("laptop charger, cable, and adapter",
                  "phone charger and cable",
                  "replacement battery for drill",
                  "compatible with Bosch battery",
                  "phone with charger and cable",
                  "vacuum cleaner with bag"):
            self.assertEqual(request_for(q).intent_excluded_product_types, [],
                             f"{q!r} must NOT create an exclusion")

    def test_russian_negation_unchanged(self) -> None:
        self.assertEqual(_detect_exclusions("пылесос без мешка"), ["мешка"])
        self.assertTrue(request_for("пылесос без мешка Bosch")
                        .intent_excluded_product_types)
        req = request_for("наушники без чехла Samsung")
        self.assertTrue(req.intent_excluded_product_types)
        self.assertEqual(
            _reintroduces(req, product_query_variants(req), ["чехла"]), [])


class Invariants(unittest.TestCase):
    def _check(self, q: str) -> None:
        variants = variants_for(q)
        self.assertTrue(variants)
        self.assertTrue(all(v.strip() for v in variants))
        folded = [v.casefold() for v in variants]
        self.assertEqual(len(folded), len(set(folded)))
        self.assertLessEqual(len(variants), MAX_PRODUCT_QUERY_VARIANTS)

    def test_invariants_across_cases(self) -> None:
        for q, _, _ in SEVEN:
            self._check(q)
        for q in ("drill without battery, brush", "case for iphone 15",
                  "пылесос без мешка Bosch", "laptop charger, cable, and adapter"):
            self._check(q)

    def test_cap_unchanged(self) -> None:
        self.assertEqual(MAX_PRODUCT_QUERY_VARIANTS, 3)


if __name__ == "__main__":
    unittest.main()

