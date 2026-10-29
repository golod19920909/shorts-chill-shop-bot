# -*- coding: utf-8 -*-
"""G25.2-D Stage 24 — regression tests for negation/exclusion semantics
(OFFLINE). Uses the real production path (tests.test_search_intent.
resolve_and_apply, AIService(api_key=None) local fallback, NO Gemini) +
product_query_variants. No production/existing-test edits.

Asserts OBSERVED and INTENDED behavior separately:
  * Stage-23 Russian bagless negation stays preserved (SAFE).
  * Stage-16 A-3 exclusion protection for the brand-less case (SAFE).
  * wired vs wireless; missing battery/charger; component exclusion;
    compatibility phrases with negation; English terms; unrelated query.
  * Determinism across repeated runs.
  * A CONFIRMED DEFECT is asserted as the CURRENT (unsafe) observed behavior
    with a clear comment — NOT encoded as desired behavior: the
    «пылесос без мешка <brand>» case where the excluded item is reintroduced
    by the _own_phrase short-query branch (query_variants.py L189-197), which
    the Stage-16 A-3 guard (L149-159) does not cover.
"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "t_search_intent_24t", ROOT / "tests" / "test_search_intent.py")
t_si = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(t_si)

from services.query_variants import product_query_variants  # noqa: E402
from services.query_intent import _detect_exclusions  # noqa: E402


def variants_for(text: str) -> list[str]:
    request, _, _ = t_si.resolve_and_apply(text)
    return product_query_variants(request)


def request_for(text: str):
    request, _, _ = t_si.resolve_and_apply(text)
    return request


class Stage23BaglessPreserved(unittest.TestCase):
    def test_bagless_negation_single_variant(self) -> None:
        variants = variants_for("пылесос без мешка")
        self.assertEqual(variants, ["пылесос"])
        self.assertEqual(request_for("пылесос без мешка")
                         .intent_excluded_product_types, ["мешка"])

    def test_no_positive_мешка_variant(self) -> None:
        for phrase in variants_for("пылесос без мешка"):
            if "без мешка" not in phrase:
                self.assertNotIn("мешк", phrase.casefold())


class Stage16A3ExclusionProtection(unittest.TestCase):
    def test_headphones_without_case_brandless(self) -> None:
        variants = variants_for("наушники без чехла")
        self.assertEqual(variants[0], "наушники")
        for phrase in variants[1:]:
            self.assertNotIn("чехл", phrase.casefold())

    def test_excluded_stem_absent_from_derived_variants_brandless(self) -> None:
        self.assertEqual(variants_for("пылесос без мешка"),
                         ["пылесос"])


class WiredVsWireless(unittest.TestCase):
    def test_wireless_headphones_preserved(self) -> None:
        req = request_for("наушники без проводов")
        self.assertEqual(req.intent_excluded_product_types, ["проводов"])
        self.assertEqual(variants_for("наушники без проводов"),
                         ["наушники"])

    def test_wired_headphones_no_exclusion(self) -> None:
        self.assertEqual(request_for("наушники с проводами")
                         .intent_excluded_product_types, [])


class MissingBatteryOrChargerPackageContent(unittest.TestCase):
    def test_drill_without_battery_preserved(self) -> None:
        req = request_for("дрель без аккумулятора")
        self.assertEqual(req.intent_excluded_product_types, ["аккумулятора"])
        variants = variants_for("дрель без аккумулятора")
        self.assertEqual(variants[0], "дрель")
        for phrase in variants[1:]:
            self.assertNotIn("аккумулятор", phrase.casefold())

    def test_phone_without_charger_preserved(self) -> None:
        req = request_for("телефон без зарядки в комплекте")
        self.assertTrue(req.intent_excluded_product_types)
        self.assertEqual(variants_for(
            "телефон без зарядки в комплекте")[0], "телефон")


class ComponentExclusionVsAppliance(unittest.TestCase):
    def test_vacuum_without_filter_preserved(self) -> None:
        self.assertEqual(variants_for("пылесос без фильтра")[0],
                         "пылесос")

    def test_car_without_ac_preserved(self) -> None:
        req = request_for("машина без кондиционера")
        self.assertTrue(req.intent_excluded_product_types)
        self.assertEqual(req.normalized_query, "машина")


class CompatibilityPhraseWithNegation(unittest.TestCase):
    def test_filter_for_bagless_vacuum_not_inverted(self) -> None:
        variants = variants_for("фильтр для пылесоса без мешка")
        # Normalization strips the «для» connector (Stage-16 A-2); the product
        # («фильтр»), the compatibility («пылесоса») and the negation
        # («без мешка») all survive — no inversion into a positive bag term.
        self.assertEqual(variants[0], "фильтр пылесоса без мешка")
        self.assertIn("без мешка", variants[0])
        for phrase in variants[1:]:
            if "без мешка" not in phrase:
                self.assertNotIn("мешк", phrase.casefold())

    def test_vacuum_capable_of_bagless_operation(self) -> None:
        variants = variants_for("пылесос с возможностью работы без мешка")
        self.assertTrue(variants)
        for phrase in variants:
            self.assertIn("пылесос", phrase.casefold())


class EnglishNegationTerms(unittest.TestCase):
    def test_english_without_not_a_russian_negation(self) -> None:
        # Stage 27: an English «without X» IS now parsed as an exclusion,
        # independently of the Russian «без» rule. The English result is
        # produced by the English pattern, NOT by the Cyrillic one.
        req = request_for("vacuum cleaner without bag")
        self.assertEqual(req.intent_excluded_product_types, ["bag"])
        # Primary query preserved (negation word intact).
        self.assertEqual(variants_for("vacuum cleaner without bag")[0],
                         "vacuum cleaner without bag")
        # The English exclusion comes from the English rule alone: the same
        # phrase has no Cyrillic «без», and a Russian «без X» phrase is parsed
        # by the separate Cyrillic pattern (different token), so the English
        # behavior is not explained by the Russian-specific path.
        self.assertEqual(_detect_exclusions("vacuum cleaner without bag"),
                         ["bag"])
        self.assertEqual(_detect_exclusions("пылесос без мешка"),
                         ["мешка"])

    def test_english_bagless_preserved(self) -> None:
        self.assertEqual(variants_for("bagless vacuum cleaner")[0],
                         "bagless vacuum cleaner")

    def test_english_wireless_preserved(self) -> None:
        self.assertEqual(variants_for("wireless headphones")[0],
                         "wireless headphones")


class EnglishWithoutBroadeningInversion(unittest.TestCase):
    """FIXED in Stage 27 (query_intent.py «without X» exclusion rule).

    Before Stage 27 the parser had no English «без»-equivalent, so a derived
    short-query variant dropped «without» and named X positively
    (e.g. «vacuum cleaner without bag» -> «vacuum cleaner bag») — a
    broadening-inversion. Stage 27 added an English exclusion pattern, so
    intent_excluded_product_types now carries X and the existing Stage-25
    short-query guard removes the unsafe derived variant. These tests now
    assert the CORRECTED behavior: primary preserved, unsafe positive variant
    absent, exclusion parsed via the real production intent parser."""

    def test_drill_without_battery_names_battery(self) -> None:
        req = request_for("drill without battery")
        self.assertEqual(req.intent_excluded_product_types, ["battery"])
        variants = product_query_variants(req)
        self.assertEqual(variants[0], "drill without battery")  # primary kept
        self.assertNotIn("drill battery", variants)  # unsafe variant GONE

    def test_phone_without_charger_names_charger(self) -> None:
        req = request_for("phone without charger included")
        # Stage 29: the bounded multi-word «without» rule captures the noun
        # phrase («charger included»), not just the first word.
        self.assertEqual(req.intent_excluded_product_types,
                         ["charger included"])
        variants = product_query_variants(req)
        self.assertEqual(variants[0], "phone without charger included")
        # No derived variant names the excluded item (charger) positively.
        self.assertFalse(
            any("charger" in v and "without" not in v for v in variants[1:]))

    def test_vacuum_without_bag_names_bag(self) -> None:
        req = request_for("vacuum cleaner without bag")
        self.assertEqual(req.intent_excluded_product_types, ["bag"])
        variants = product_query_variants(req)
        self.assertEqual(variants[0], "vacuum cleaner without bag")
        self.assertNotIn("vacuum cleaner bag", variants)  # unsafe variant GONE


class UnrelatedQueryUnchanged(unittest.TestCase):
    def test_plain_vacuum_query(self) -> None:
        self.assertEqual(request_for("пылесос")
                         .intent_excluded_product_types, [])
        self.assertEqual(variants_for("пылесос"), ["пылесос"])


class Determinism(unittest.TestCase):
    def test_repeated_runs_identical(self) -> None:
        self.assertEqual(variants_for("пылесос без мешка"),
                         variants_for("пылесос без мешка"))


class ConfirmedDefect_ObservedNotDesired(unittest.TestCase):
    """Stage-24 defect, FIXED in Stage 25 (query_variants.py L227-242).

    The «пылесос без мешка <brand>» inversion — a derived short-query variant
    naming the excluded type positively («пылесос мешка») — was a CONFIRMED
    DEFECT. Stage 24 recorded it as the then-CURRENT unsafe behavior; the
    Stage 25 A-3 short-query guard extension now removes that unsafe derived
    variant. This test asserts the CORRECTED behavior via the real production
    query-variant path (variants_for -> product_query_variants)."""

    def test_branded_bagless_reintroduces_мешка_variant(self) -> None:
        variants = variants_for("пылесос без мешка Bosch")
        # Primary parser phrase preserved (negation intact).
        self.assertEqual(variants[0], "пылесос без мешка Bosch")
        # The Stage-24 unsafe positive variant is now ABSENT.
        self.assertNotIn("пылесос мешка", variants)
        # No derived variant names the excluded stem positively.
        for phrase in variants[1:]:
            self.assertNotIn("мешк", phrase.casefold())

    def test_brandless_case_does_not_have_the_bug(self) -> None:
        self.assertNotIn("пылесос мешка", variants_for("пылесос без мешка"))


if __name__ == "__main__":
    unittest.main()

