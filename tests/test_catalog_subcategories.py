"""Structural checks for the curated visual-catalog configuration."""
from __future__ import annotations

import unittest

from config.catalog import CATEGORIES
from config.subcategories import (
    CATEGORY_DESCRIPTIONS,
    SUBCATEGORIES,
    get_all_subcategories,
    get_subcategories,
    get_subcategory,
)


class SubcategoryCatalogTests(unittest.TestCase):
    def test_every_existing_category_has_a_description_and_subcategories(self):
        codes = {category.code for category in CATEGORIES}
        self.assertEqual(set(CATEGORY_DESCRIPTIONS), codes)
        for code in codes:
            with self.subTest(category=code):
                self.assertGreaterEqual(len(get_subcategories(code)), 4)

    def test_subcategory_codes_are_unique_and_lookup_is_stable(self):
        codes = [item.code for item in SUBCATEGORIES]
        self.assertEqual(len(codes), len(set(codes)))
        for item in SUBCATEGORIES:
            with self.subTest(subcategory=item.code):
                self.assertIs(get_subcategory(item.code), item)

    def test_every_subcategory_belongs_to_an_existing_category(self):
        category_codes = {category.code for category in CATEGORIES}
        for item in SUBCATEGORIES:
            with self.subTest(subcategory=item.code):
                self.assertIn(item.category_code, category_codes)

    def test_search_routes_are_bounded_and_unique(self):
        for item in SUBCATEGORIES:
            with self.subTest(subcategory=item.code):
                self.assertGreaterEqual(len(item.query_routes), 1)
                self.assertLessEqual(len(item.query_routes), 3)
                self.assertEqual(
                    len(item.query_routes),
                    len({route.casefold() for route in item.query_routes}),
                )
                self.assertTrue(all(route.strip() for route in item.query_routes))

    def test_every_subcategory_has_russian_relevance_keywords(self):
        for item in SUBCATEGORIES:
            with self.subTest(subcategory=item.code):
                self.assertGreaterEqual(len(item.keywords), 3)
                self.assertTrue(all(word.strip() for word in item.keywords))

    def test_flat_lookup_preserves_configured_order(self):
        self.assertEqual(get_all_subcategories(), SUBCATEGORIES)


if __name__ == "__main__":
    unittest.main()
