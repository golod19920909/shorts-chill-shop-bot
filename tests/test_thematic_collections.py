"""Offline contract checks for the independent editorial collections."""
from __future__ import annotations

import unittest

from config.subcategories import get_all_subcategories, get_subcategory
from config.thematic_collections import (
    BUDGET_TIERS,
    THEME_MAX_PRICE_BY_CODE,
    THEME_SHELVES,
    THEMATIC_COLLECTIONS,
)
from database.models import FLOW_THEMES
from keyboards.main import (
    CB_THEMATIC_COLLECTIONS,
    MAIN_MENU_CALLBACKS,
    categories_keyboard,
    main_menu_keyboard,
    thematic_budget_keyboard,
    thematic_collections_keyboard,
)


def _callbacks(keyboard) -> list[str]:
    return [
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data
    ]


class ThematicCollectionConfigTests(unittest.TestCase):
    def test_exactly_four_approved_editorial_collections(self) -> None:
        self.assertEqual(
            [item["title"] for item in THEMATIC_COLLECTIONS],
            [
                "Необычные полезные устройства",
                "Умные решения для дома",
                "Гаджеты, о которых мало кто знает",
                "Полезные мелочи до определённого бюджета",
            ],
        )
        self.assertEqual(len({item["code"] for item in THEMATIC_COLLECTIONS}), 4)
        self.assertFalse(
            any(
                token in f"{item['code']} {item['title']}".casefold()
                for item in THEMATIC_COLLECTIONS
                for token in ("automotive", "автомоб", "машин")
            )
        )

    def test_budget_collection_has_exact_usd_ceilings(self) -> None:
        self.assertEqual(
            BUDGET_TIERS,
            (
                ("theme_budget_5", "До $5"),
                ("theme_budget_10", "До $10"),
                ("theme_budget_20", "До $20"),
            ),
        )
        self.assertEqual(
            THEME_MAX_PRICE_BY_CODE,
            {
                "theme_budget_5": 5.0,
                "theme_budget_10": 10.0,
                "theme_budget_20": 20.0,
            },
        )

    def test_thematic_shelves_are_independent_from_ordinary_catalog(self) -> None:
        ordinary_codes = {item.code for item in get_all_subcategories()}
        thematic_codes = {item.code for item in THEME_SHELVES}
        self.assertEqual(len(THEME_SHELVES), 6)
        self.assertTrue(thematic_codes.isdisjoint(ordinary_codes))
        for shelf in THEME_SHELVES:
            resolved = get_subcategory(shelf.code)
            self.assertIsNotNone(resolved)
            self.assertEqual(resolved.code, shelf.code)

    def test_every_thematic_shelf_has_specific_routes_and_product_types(self) -> None:
        for shelf in THEME_SHELVES:
            with self.subTest(code=shelf.code):
                self.assertGreaterEqual(len(shelf.query_routes), 2)
                self.assertTrue(all(route.strip() for route in shelf.query_routes))
                # Every retrieval route must name a concrete product type.
                # Generic editorial phrases often return resp_code=405
                # (a valid but empty Affiliate API answer) for every page.
                self.assertTrue(
                    all(route.casefold() in {
                        product_type.casefold()
                        for product_type in shelf.product_types
                    } for route in shelf.query_routes)
                )
                self.assertTrue(shelf.product_types)
                self.assertTrue(shelf.product_type_groups)
                grouped_aliases = [
                    alias.casefold()
                    for group in shelf.product_type_groups
                    for alias in group
                ]
                self.assertEqual(len(grouped_aliases), len(set(grouped_aliases)))
                self.assertTrue(set(grouped_aliases).issubset({
                    alias.casefold() for alias in shelf.product_types
                }))
                self.assertTrue(shelf.keywords)
                self.assertTrue(shelf.exclude_terms)
                self.assertTrue(shelf.description.strip())

    def test_theme_history_has_its_own_scope(self) -> None:
        self.assertEqual(FLOW_THEMES, "themes")
        self.assertNotEqual(FLOW_THEMES, "feed")
        self.assertNotEqual(FLOW_THEMES, "search")

    def test_thematic_collections_are_a_top_level_menu_entry(self) -> None:
        self.assertIn(CB_THEMATIC_COLLECTIONS, MAIN_MENU_CALLBACKS)
        self.assertIn(
            CB_THEMATIC_COLLECTIONS,
            _callbacks(main_menu_keyboard()),
        )
        rows = [
            [button.callback_data for button in row if button.callback_data]
            for row in main_menu_keyboard().inline_keyboard
        ]
        self.assertEqual(rows[:6], [
            ["menu:search", "menu:photo"],
            ["menu:feed", "menu:categories"],
            ["menu:themes"],
            ["menu:favorites", "menu:tracking"],
            ["menu:currency", "menu:settings"],
            ["menu:hot", "menu:pcodes"],
        ])
        self.assertNotIn("theme:menu", _callbacks(categories_keyboard()))

    def test_thematic_keyboard_exposes_only_approved_collections_and_tiers(self) -> None:
        collection_callbacks = _callbacks(thematic_collections_keyboard())
        self.assertEqual(
            sorted(item for item in collection_callbacks if item.startswith("theme:item:")),
            sorted(
                f"theme:item:{item['code']}"
                for item in THEMATIC_COLLECTIONS
                if item["code"] != "budget_finds"
            ),
        )
        self.assertEqual(
            sum(item == "theme:budget" for item in collection_callbacks),
            1,
        )
        tier_callbacks = _callbacks(thematic_budget_keyboard())
        self.assertEqual(
            sorted(item for item in tier_callbacks if item.startswith("theme:tier:")),
            sorted(f"theme:tier:{code}" for code, _label in BUDGET_TIERS),
        )


if __name__ == "__main__":
    unittest.main()
