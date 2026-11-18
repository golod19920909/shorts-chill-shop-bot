"""Offline contract checks for the independent editorial collections."""
from __future__ import annotations

import inspect
import unittest

from config.subcategories import get_all_subcategories, get_subcategory
from config.thematic_collections import (
    BUDGET_TIERS,
    THEME_MAX_PRICE_BY_CODE,
    THEME_SHELVES,
    THEMATIC_COLLECTIONS,
)
from database.models import FLOW_FEED, FLOW_SEARCH
from models.product import Marketplace, Product
from models.search_request import SearchRequest
from services.search_service import SearchService
from handlers import thematic_collections as thematic_handlers
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
                # The runtime route planner merges every configured
                # priority route with one representative from each canonical
                # product type not already covered by those routes.
                route_plan = list(dict.fromkeys([
                    *shelf.query_routes,
                    *(group[0] for group in shelf.product_type_groups),
                ]))
                self.assertEqual(
                    len(route_plan),
                    len(set(route.casefold() for route in route_plan)),
                )
                self.assertGreater(len(route_plan), 3)
                self.assertLessEqual(len(route_plan), 20)
                self.assertEqual(
                    set(route.casefold() for route in route_plan[len(shelf.query_routes):]),
                    set(
                        group[0].casefold()
                        for group in shelf.product_type_groups
                        if group[0].casefold() not in {
                            route.casefold() for route in shelf.query_routes
                        }
                    ),
                )
                grouped_aliases = [
                    alias.casefold()
                    for group in shelf.product_type_groups
                    for alias in group
                ]
                self.assertEqual(len(grouped_aliases), len(set(grouped_aliases)))
                self.assertEqual(
                    set(grouped_aliases),
                    {alias.casefold() for alias in shelf.product_types},
                )
                self.assertTrue(shelf.keywords)
                self.assertTrue(shelf.exclude_terms)
                self.assertTrue(shelf.description.strip())

    def test_theme_history_isolated_per_shelf_and_user_scope(self) -> None:
        # popular_shown already keys rows by (user_id, flow, product_id);
        # adding the shelf code to flow isolates all six shelves durably.
        scopes = {f"themes:{shelf.code}" for shelf in THEME_SHELVES}
        self.assertEqual(len(scopes), 6)
        self.assertTrue(scopes.isdisjoint({FLOW_FEED, FLOW_SEARCH}))
        self.assertNotIn("themes", scopes)

    def test_search_request_accepts_the_bounded_thematic_route_budget(self) -> None:
        request = SearchRequest(original_query="thematic shelf")
        request._thematic_retrieval_queries = [
            f"route {index}" for index in range(19)
        ]
        self.assertEqual(len(request._thematic_retrieval_queries), 19)
        self.assertNotIn(
            "_thematic_retrieval_queries",
            SearchRequest.model_json_schema().get("properties", {}),
        )
        ordinary = SearchRequest(
            original_query="ordinary subcategory",
            retrieval_queries=["route one", "route two"],
        )
        self.assertEqual(len(ordinary.retrieval_queries), 2)

    def test_thematic_handlers_show_any_positive_result_count(self) -> None:
        source = inspect.getsource(thematic_handlers)
        self.assertNotIn("THEME_MINIMUM_PRODUCTS", source)
        self.assertEqual(source.count("minimum_products=1"), 2)
        self.assertGreaterEqual(source.count("if not outcome.products:"), 2)

    def test_thematic_delivery_reuses_the_smart_search_card_pipeline_without_recuration(self) -> None:
        source = inspect.getsource(thematic_handlers)
        # find_by_subcategory already performs retrieval, candidate selection,
        # identity validation and final subtype/family curation. The shared
        # send_search_results function must therefore receive that finished
        # list as pre-curated, just like Smart Search, rather than applying a
        # second generic curation pass before caching and rendering cards.
        self.assertEqual(source.count("pre_curated=True"), 2)
        self.assertEqual(source.count("send_search_results("), 2)

    def test_thematic_candidate_window_round_robins_across_validated_types(self) -> None:
        shelf = get_subcategory("theme_unusual_devices")
        self.assertIsNotNone(shelf)
        products = []
        for prefix, title, count in (
            ("jar", "Automatic jar opener electric kitchen tool", 8),
            ("paste", "Automatic toothpaste dispenser wall mounted", 4),
            ("brush", "Electric cleaning brush rotating scrubber", 4),
        ):
            for index in range(count):
                product_id = f"{prefix}-{index}"
                products.append(Product(
                    marketplace=Marketplace.ALIEXPRESS,
                    product_id=product_id,
                    title=f"{title} model {index}",
                    product_url=f"https://www.aliexpress.com/item/{product_id}.html",
                ))

        balanced = SearchService._balance_thematic_pool(
            products, subcategory=shelf, limit=9
        )
        counts = {}
        for product in balanced:
            from services.subcategory_quality import subcategory_product_type

            type_key = subcategory_product_type(product, shelf)
            counts[type_key] = counts.get(type_key, 0) + 1

        self.assertEqual(len(balanced), 9)
        self.assertEqual(sorted(counts.values()), [3, 3, 3])

    def test_thematic_collections_use_lightbulb_branding_consistently(self) -> None:
        from keyboards.main import LABEL_THEMATIC_COLLECTIONS

        start_source = inspect.getsource(__import__("handlers.start", fromlist=["HELP_TEXT"]))
        self.assertEqual(LABEL_THEMATIC_COLLECTIONS, "💡 Тематические подборки")
        self.assertIn("💡 <b>Тематические подборки</b>", thematic_handlers.THEME_MENU_TEXT)
        self.assertIn("💡 ", str(thematic_collections_keyboard().inline_keyboard))
        self.assertIn("💡 <b>Тематические подборки</b>", start_source)
        self.assertNotIn("✨ <b>Тематические подборки</b>", start_source)
        self.assertNotIn("✨ <b>", inspect.getsource(thematic_handlers))

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
