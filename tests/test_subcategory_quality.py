from __future__ import annotations

import asyncio
import unittest
from collections import Counter

from config.subcategories import get_subcategory
from models.product import Marketplace, Product
from models.search_request import SearchMode, SearchRequest
from services.search_service import SearchOutcome, SearchService
from services.subcategory_quality import (
    filter_subcategory_products,
    subcategory_match_reason,
    subcategory_product_type,
)


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
        # Drain tools, fireplace ash kits and kitchen aprons are all
        # explicitly excluded by this shelf's current title policy.
        self.assertEqual(dropped, {"excluded_title_concept": 3})

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

        selected = SearchService._finalize_subcategory_selection(
            products, subcategory=get_subcategory("controllers"), limit=20
        )

        self.assertEqual(
            [item.product_id for item in selected],
            ["a", "d", "c"],
        )

    def test_welding_identity_does_not_combine_distant_title_fragments(self):
        subcategory = get_subcategory("welding_equipment")
        self.assertIsNotNone(subcategory)

        self.assertIsNone(
            subcategory_match_reason(
                listing("welder", "MIG 250 welding machine inverter"),
                subcategory,
            )
        )
        self.assertEqual(
            subcategory_match_reason(
                listing(
                    "dent-kit",
                    "Spot welding car dent puller repair kit machine",
                ),
                subcategory,
            ),
            "excluded_title_concept",
        )
        self.assertEqual(
            subcategory_match_reason(
                listing(
                    "cross-sell",
                    "Car body repair kit, spot welding machine included",
                ),
                subcategory,
            ),
            "no_subcategory_product_type",
        )

    def test_finalizer_balances_distinct_welding_types(self):
        subcategory = get_subcategory("welding_equipment")
        self.assertIsNotNone(subcategory)
        products = []
        for kind, title in (
            ("mig", "MIG welding machine"),
            ("tig", "TIG welding machine"),
            ("mma", "MMA welding machine"),
            ("plasma", "plasma cutter"),
        ):
            for model in range(10):
                products.append(
                    listing(
                        f"{kind}-{model}",
                        f"{title} model {model + 100}",
                    )
                )

        selected = SearchService._finalize_subcategory_selection(
            products, subcategory=subcategory, limit=20
        )
        type_counts = Counter(
            subcategory_product_type(product, subcategory)
            for product in selected
        )

        self.assertEqual(len(selected), 20)
        self.assertEqual(set(type_counts.values()), {5})
        self.assertEqual(len(type_counts), 4)

    def test_product_type_prefers_specific_alias_over_generic_alias(self):
        subcategory = get_subcategory("massage_devices")
        self.assertIsNotNone(subcategory)
        neck = listing("neck", "Electric neck shoulder massager with heat")
        face = listing("face", "Portable facial massager beauty device")
        self.assertIn("neck", subcategory_product_type(neck, subcategory))
        self.assertIn("facial", subcategory_product_type(face, subcategory))

    def test_subcategory_selection_uses_reserve_and_refills_to_twenty(self):
        variants = ("Portable", "Compact", "Digital", "Mini", "Heavy")
        products = [
            listing(
                f"family-{family}-variant-{variant_index}",
                f"{variant} family{family} model{family} massage device tool",
            )
            for family in range(10)
            for variant_index, variant in enumerate(variants)
        ]

        class Selector:
            is_available = True

            def __init__(self):
                self.wanted = None

            async def select_subcategory_candidates(
                self, candidates, *, subcategory, wanted
            ):
                self.wanted = wanted
                return [product.product_id for product in candidates[:20]]

        selector = Selector()
        service = SearchService([], ai_service=selector)
        outcome = SearchOutcome(
            request=SearchRequest(
                original_query="Массажёры",
                normalized_query="massage devices",
                mode=SearchMode.CATEGORY,
                category="beauty",
                subcategory_code="massage_devices",
            ),
            products=products,
        )

        asyncio.run(service.apply_feed_selection(outcome))

        self.assertEqual(selector.wanted, 40)
        self.assertEqual(len(outcome.products), 20)
        self.assertEqual(len({item.product_id for item in outcome.products}), 20)
        self.assertEqual(
            len({item.title.split()[1] for item in outcome.products}),
            10,
        )


if __name__ == "__main__":
    unittest.main()
