# -*- coding: utf-8 -*-
"""G25.2-D Stage 25 — regression tests for the A-3 short-query guard
extension (OFFLINE). Exercises the REAL production path:
tests.test_search_intent.resolve_and_apply (AIService(api_key=None) local
fallback, NO Gemini) + services.query_variants.product_query_variants.
No production/existing-test edits; production file patched is
services/query_variants.py (Stage 25 minimal fix).

Asserts the FIXED safety properties required by the task:
  * «пылесос без мешка Bosch» / «наушники без чехла Samsung» no longer emit
    a derived variant that reintroduces the excluded noun positively;
  * the primary query keeps the negation;
  * existing guards, brand/model specs, compat accessories, unrelated
    queries, non-negated queries are unchanged;
  * invariants: no empty/duplicate variants, cap unchanged, deterministic.
"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "t_search_intent_25t", ROOT / "tests" / "test_search_intent.py")
t_si = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(t_si)

from services.query_variants import (  # noqa: E402
    MAX_PRODUCT_QUERY_VARIANTS,
    product_query_variants,
)


def variants_for(text: str) -> list[str]:
    request, _, _ = t_si.resolve_and_apply(text)
    return product_query_variants(request)


def request_for(text: str):
    request, _, _ = t_si.resolve_and_apply(text)
    return request


class ConfirmedReproductionFixed(unittest.TestCase):
    """The two Stage-24 confirmed-inversion inputs, now SAFE."""

    def test_vacuum_bagless_bosch_no_reintroduced_мешка(self) -> None:
        req = request_for("пылесос без мешка Bosch")
        self.assertTrue(req.intent_excluded_product_types)  # exclusion set
        variants = product_query_variants(req)
        self.assertEqual(variants[0], "пылесос без мешка Bosch")  # primary kept
        self.assertNotIn("пылесос мешка", variants)  # unsafe variant gone
        for v in variants[1:]:
            self.assertNotIn("мешк", v.casefold())  # stem never positive

    def test_headphones_no_case_samsung_no_reintroduced_чехла(self) -> None:
        req = request_for("наушники без чехла Samsung")
        self.assertTrue(req.intent_excluded_product_types)
        variants = product_query_variants(req)
        self.assertEqual(variants[0], "наушники без чехла Samsung")
        self.assertNotIn("наушники чехла", variants)
        for v in variants[1:]:
            self.assertNotIn("чехл", v.casefold())


class ExistingGuardsUnchanged(unittest.TestCase):
    def test_bagless_no_brand_still_single_variant(self) -> None:
        self.assertEqual(variants_for("пылесос без мешка"),
                         ["пылесос"])

    def test_stage16_a3_brandless_headphones(self) -> None:
        variants = variants_for("наушники без чехла")
        self.assertEqual(variants[0], "наушники")
        for v in variants[1:]:
            self.assertNotIn("чехл", v.casefold())

    def test_no_positive_мешка_variant_anywhere(self) -> None:
        for v in variants_for("пылесос без мешка"):
            if "без мешка" not in v:
                self.assertNotIn("мешк", v.casefold())
class BrandModelSpecPreserved(unittest.TestCase):
    def test_iphone_case_keeps_brand_and_model(self) -> None:
        req = request_for("чехол для iPhone 15")
        self.assertEqual(req.intent_excluded_product_types, [])
        variants = product_query_variants(req)
        self.assertEqual(variants[0], "чехол Apple iphone 15")
        self.assertGreaterEqual(len(variants), 2)  # normal generation intact

    def test_sneakers_size_spec_preserved(self) -> None:
        req = request_for("кроссовки Nike 43 размер")
        self.assertEqual(req.intent_excluded_product_types, [])
        self.assertIn("кроссовки nike 43 размер",
                      product_query_variants(req))


class NonNegatedUnchanged(unittest.TestCase):
    def test_plain_vacuum(self) -> None:
        self.assertEqual(variants_for("пылесос"), ["пылесос"])

    def test_plain_headphones(self) -> None:
        self.assertEqual(variants_for("наушники"), ["наушники"])


class CompatibilityAccessoryPreserved(unittest.TestCase):
    """Patch must NOT broadly remove legit accessory queries."""

    def test_filter_for_bagless_vacuum_keeps_filter_variants(self) -> None:
        req = request_for("фильтр для пылесоса без мешка")
        self.assertEqual(req.intent_excluded_product_types, ["мешка"])
        variants = product_query_variants(req)
        self.assertEqual(variants[0], "фильтр пылесоса без мешка")
        self.assertTrue(all("фильтр" in v.casefold() for v in variants))


class UnrelatedCategoryUnchanged(unittest.TestCase):
    def test_cat_food(self) -> None:
        req = request_for("корм для кошек")
        self.assertEqual(req.intent_excluded_product_types, [])


class Invariants(unittest.TestCase):
    def _check(self, text: str) -> None:
        variants = variants_for(text)
        self.assertTrue(variants, "must never be empty")
        self.assertTrue(all(v.strip() for v in variants), "no empty strings")
        folded = [v.casefold() for v in variants]
        self.assertEqual(len(folded), len(set(folded)), "no duplicates")
        self.assertLessEqual(len(variants), MAX_PRODUCT_QUERY_VARIANTS)

    def test_no_empty_or_dup_across_negation_cases(self) -> None:
        for q in ("пылесос без мешка Bosch", "наушники без чехла Samsung",
                  "пылесос без мешка", "фильтр для пылесоса без мешка",
                  "чехол для iPhone 15"):
            self._check(q)

    def test_cap_unchanged(self) -> None:
        self.assertEqual(MAX_PRODUCT_QUERY_VARIANTS, 3)

    def test_deterministic(self) -> None:
        for q in ("пылесос без мешка Bosch", "наушники без чехла Samsung"):
            self.assertEqual(variants_for(q), variants_for(q))


if __name__ == "__main__":
    unittest.main()

