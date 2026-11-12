from __future__ import annotations

import unittest

from config.subcategories import get_subcategory
from models.product import Marketplace, Product
from services.search_service import SearchService
from services.subcategory_quality import filter_subcategory_products, subcategory_match_reason


def listing(product_id: str, title: str) -> Product:
    return Product(
        marketplace=Marketplace.ALIEXPRESS,
        product_id=product_id,
        title=title,
        product_url=f"https://www.aliexpress.com/item/{product_id}.html",
        image_url=f"https://ae01.alicdn.com/kf/{product_id}.jpg",
        price=10.0,
        currency="USD",
        orders_count=100,
    )


class SubcategoryQualityTests(unittest.TestCase):
    def test_cleaning_shelf_rejects_plumbing_fireplace_and_unrelated_items(self):
        subcategory = get_subcategory("cleaning")
        self.assertIsNotNone(subcategory)
        products = [
            listing("scrubber", "Electric Spin Scrubber Brush for Bathroom"),
            listing("drain", "Drain Pipe Cleaning Snake Tool"),
            listing("ash", "Fireplace Ash Cleaning Kit"),
            listing("apron", "Kitchen Apron for Cleaning"),
        ]

        kept, dropped = filter_subcategory_products(products, subcategory)

        self.assertEqual([item.product_id for item in kept], ["scrubber"])
        self.assertEqual(dropped["excluded_title_concept"], 2)
        self.assertEqual(dropped["no_subcategory_product_type"], 1)

    def test_gamepad_shelf_rejects_network_equipment(self):
        subcategory = get_subcategory("controllers")
        self.assertIsNotNone(subcategory)

        self.assertIsNone(
            subcategory_match_reason(
                listing("pad", "Wireless Bluetooth Gamepad Controller"),
                subcategory,
            )
        )
        self.assertEqual(
            subcategory_match_reason(
                listing("router", "GL.iNet Flint 3e WiFi 7 Router"),
                subcategory,
            ),
            "excluded_title_concept",
        )

    def test_lighting_shelf_requires_a_real_light_product(self):
        subcategory = get_subcategory("home_lighting")
        self.assertIsNotNone(subcategory)

        self.assertIsNone(
            subcategory_match_reason(
                listing("lamp", "Vintage Mushroom Desk Lamp"),
                subcategory,
            )
        )
        self.assertEqual(
            subcategory_match_reason(
                listing("router", "WiFi 7 Router with LED Display"),
                subcategory,
            ),
            "no_subcategory_product_type",
        )

    def test_russian_inflections_match_catalog_aliases(self):
        subcategory = get_subcategory("kitchen")
        self.assertIsNotNone(subcategory)
        self.assertIsNone(
            subcategory_match_reason(
                listing("kitchen-tool", "Кухонная принадлежность для кухни"),
                subcategory,
            )
        )

    def test_final_subcategory_selection_drops_near_identical_families(self):
        products = [
            listing("a", "GuliKit KingKong 3 Max Wireless Gamepad Controller"),
            listing("b", "GuliKit KingKong 3 Max Wireless Gamepad Controller RGB"),
            listing("c", "GameSir Nova Lite Bluetooth Gamepad"),
            listing("d", "8BitDo Ultimate 2 Wireless Controller"),
        ]

        selected = SearchService._finalize_subcategory_selection(products, limit=20)

        self.assertEqual(
            [item.product_id for item in selected],
            ["a", "c", "d"],
        )


if __name__ == "__main__":
    unittest.main()
