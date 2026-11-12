from __future__ import annotations

import asyncio
from collections import Counter
import unittest

from config.subcategories import get_all_subcategories, get_subcategory
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
    def test_every_catalog_subcategory_has_a_classifiable_product_type(self):
        subcategories = get_all_subcategories()
        self.assertGreaterEqual(len(subcategories), 80)

        for subcategory in subcategories:
            aliases = subcategory.product_types or subcategory.keywords
            self.assertTrue(aliases, subcategory.code)
            matched_type = None
            for index, alias in enumerate(aliases):
                product = listing(
                    f"sample-{subcategory.code}-{index}",
                    alias,
                )
                matched_type = subcategory_product_type(product, subcategory)
                if matched_type is not None:
                    break
            self.assertIsNotNone(matched_type, subcategory.code)

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

        selected_ids = [item.product_id for item in selected]
        self.assertEqual(len(selected_ids), 3)
        self.assertEqual(set(selected_ids), {"a", "c", "d"})
        self.assertNotIn("b", selected_ids)

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
        neck = listing("neck", "Electric neck massager with heat")
        face = listing("face", "Portable facial massager beauty device")
        self.assertIn("neck", subcategory_product_type(neck, subcategory))
        self.assertIn("facial", subcategory_product_type(face, subcategory))

    def test_deterministic_fallback_keeps_twenty_when_gemini_is_unavailable(self):
        products = [
            listing(
                f"fallback-{index}",
                f"Neck massager model {index + 100}",
            )
            for index in range(25)
        ]
        service = SearchService([])
        outcome = SearchOutcome(
            request=SearchRequest(
                original_query="Массажёры",
                normalized_query="neck massager",
                mode=SearchMode.CATEGORY,
                category="beauty",
                subcategory_code="massage_devices",
            ),
            products=products,
        )

        asyncio.run(service.apply_feed_selection(outcome))

        self.assertEqual(len(outcome.products), 20)
        self.assertEqual(
            len({product.product_id for product in outcome.products}),
            20,
        )
        self.assertEqual(
            service.last_subcategory_selection_stats["selector_status"],
            "unavailable",
        )
        self.assertEqual(
            service.last_subcategory_selection_stats["finalizer_input"],
            25,
        )

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
                # Simulate Gemini using the full 40-ID budget while spreading
                # its approved primary + reserve candidates across all ten
                # product families. The selector must not return only the
                # first 20 family-clustered rows when the test exercises refill.
                diverse = [
                    candidates[family * len(variants) + variant_index]
                    for variant_index in range(4)
                    for family in range(10)
                ]
                return [product.product_id for product in diverse[:wanted]]

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
        diagnostics = service.last_subcategory_selection_stats
        self.assertEqual(diagnostics["selector_status"], "success")
        self.assertEqual(diagnostics["selector_returned_ids"], 40)
        self.assertEqual(diagnostics["gemini_approved_candidates"], 40)
        self.assertEqual(diagnostics["finalizer_input"], 40)
        self.assertEqual(diagnostics["final_count"], 20)
        self.assertEqual(diagnostics["shortfall"], 0)


    def test_window_cleaner_shelf_rejects_accessories_and_spare_parts(self):
        subcategory = get_subcategory("window_cleaners")
        self.assertIsNotNone(subcategory)
        products = [
            listing("robot", "HUTT window cleaning robot automatic glass cleaner"),
            listing("battery", "Replacement battery for HUTT window cleaning robot"),
            listing("pads", "Microfiber cleaning pads for window cleaner"),
            listing("case", "Storage case for robotic window cleaner"),
        ]

        kept, dropped = filter_subcategory_products(products, subcategory)

        self.assertEqual([item.product_id for item in kept], ["robot"])
        self.assertEqual(
            dropped,
            {"accessory_or_component": 3},
        )

    def test_hand_tools_shelf_rejects_holder_but_keeps_real_tool_set(self):
        subcategory = get_subcategory("hand_tools")
        self.assertIsNotNone(subcategory)
        holder = listing(
            "holder",
            "Wall-mounted holder for pliers and wrenches",
        )
        tool_set = listing(
            "tool-set",
            "Hand tool set with screwdriver, pliers and wrench",
        )

        self.assertEqual(
            subcategory_match_reason(holder, subcategory),
            "accessory_or_component",
        )
        self.assertIsNone(subcategory_match_reason(tool_set, subcategory))

    def test_accessory_items_remain_valid_in_accessory_subcategories(self):
        laptop = get_subcategory("laptop_accessories")
        console = get_subcategory("console_accessories")
        self.assertIsNotNone(laptop)
        self.assertIsNotNone(console)

        self.assertIsNone(
            subcategory_match_reason(
                listing("stand", "Aluminum adjustable laptop stand"),
                laptop,
            )
        )
        self.assertIsNone(
            subcategory_match_reason(
                listing(
                    "dock",
                    "Controller charging station for Nintendo Switch",
                ),
                console,
            )
        )

    def test_audio_subtype_uses_validated_leading_product_not_add_on(self):
        subcategory = get_subcategory("audio")
        self.assertIsNotNone(subcategory)

        speaker = listing(
            "speaker-mic",
            "Bluetooth Karaoke Speaker with Wireless Microphone",
        )
        self.assertIn("speaker", subcategory_product_type(speaker, subcategory))

    def test_storage_shelf_rejects_non_household_storage_products(self):
        subcategory = get_subcategory("storage")
        self.assertIsNotNone(subcategory)
        products = [
            listing("closet", "Adjustable Closet Organizer Storage Shelves"),
            listing("ammo", "Ammunition Storage Box for 5.56 Cartridges"),
            listing("tobacco", "Tobacco Storage Container Humidor Box"),
            listing("car", "Car Seat Gap Organizer Storage Box"),
        ]

        kept, dropped = filter_subcategory_products(products, subcategory)

        self.assertEqual([item.product_id for item in kept], ["closet"])
        self.assertEqual(dropped, {"excluded_title_concept": 3})

    def test_cleaning_shelf_rejects_pet_food_and_industrial_products(self):
        subcategory = get_subcategory("cleaning")
        self.assertIsNotNone(subcategory)
        products = [
            listing("brush", "Electric Spin Scrubber Cleaning Brush"),
            listing("pet", "Pet Paw Washer Cleaning Brush"),
            listing("churn", "Electric Butter Churn Machine"),
            listing("industrial", "Industrial Floor Scrubber Machine"),
        ]

        kept, dropped = filter_subcategory_products(products, subcategory)

        self.assertEqual([item.product_id for item in kept], ["brush"])
        self.assertEqual(dropped, {"excluded_title_concept": 3})

    def test_finalizer_revalidates_gemini_and_reserve_candidates(self):
        subcategory = get_subcategory("window_cleaners")
        self.assertIsNotNone(subcategory)
        products = [
            listing("robot", "HUTT window cleaning robot automatic glass cleaner"),
            listing("battery", "Replacement battery for HUTT window cleaning robot"),
            listing("pads", "Microfiber cleaning pads for window cleaner"),
        ]

        selected = SearchService._finalize_subcategory_selection(
            products, subcategory=subcategory, limit=20
        )

        self.assertEqual([item.product_id for item in selected], ["robot"])


    def test_cycling_subtypes_do_not_share_a_broad_four_letter_stem(self):
        subcategory = get_subcategory("cycling")
        self.assertIsNotNone(subcategory)
        light = listing(
            "bike-light",
            "Велосипедный фонарь LED USB rechargeable",
        )
        spoke_tool = listing(
            "spoke-tool",
            "Измеритель натяжения спиц велосипедных колес, инструмент для проверки",
        )

        self.assertEqual(
            subcategory_product_type(light, subcategory),
            "велосипедный фонарь",
        )
        self.assertEqual(
            subcategory_product_type(spoke_tool, subcategory),
            "измеритель натяжения спиц",
        )
        self.assertIsNone(subcategory_match_reason(spoke_tool, subcategory))

    def test_portable_gadgets_requires_a_concrete_device_identity(self):
        subcategory = get_subcategory("portable_gadgets")
        self.assertIsNotNone(subcategory)

        generic = listing("generic", "Mini electronic device for everyday use")
        projector = listing("projector", "Mini Portable Projector 1080P")
        fan = listing("fan", "Rechargeable Handheld Fan")

        self.assertEqual(
            subcategory_match_reason(generic, subcategory),
            "no_subcategory_product_type",
        )
        self.assertIsNone(subcategory_match_reason(projector, subcategory))
        self.assertIsNone(subcategory_match_reason(fan, subcategory))

    def test_bathroom_shelf_covers_common_principal_products(self):
        subcategory = get_subcategory("bathroom")
        self.assertIsNotNone(subcategory)

        for product in (
            listing("mat", "Non Slip Bath Mat for Bathroom"),
            listing("curtain", "Waterproof Shower Curtain"),
            listing("dispenser", "Automatic Soap Dispenser"),
        ):
            self.assertIsNone(
                subcategory_match_reason(product, subcategory),
                product.title,
            )

        self.assertEqual(
            subcategory_match_reason(
                listing("sticker", "Decorative Bathroom Wall Sticker"),
                subcategory,
            ),
            "no_subcategory_product_type",
        )

    def test_console_controller_and_accessory_shelves_keep_distinct_roles(self):
        controllers = get_subcategory("console_controllers")
        accessories = get_subcategory("console_accessories")
        self.assertIsNotNone(controllers)
        self.assertIsNotNone(accessories)

        gamepad = listing("gamepad", "Wireless Gamepad Controller for PS5")
        dock = listing("dock", "Controller Charging Dock for Nintendo Switch")

        self.assertIsNone(subcategory_match_reason(gamepad, controllers))
        self.assertEqual(
            subcategory_match_reason(dock, controllers),
            "no_subcategory_product_type",
        )
        self.assertIsNone(subcategory_match_reason(dock, accessories))

    def test_sports_electronics_identifies_heart_rate_strap_before_bike_computer(self):
        subcategory = get_subcategory("sports_electronics")
        self.assertIsNotNone(subcategory)
        strap = listing(
            "heart-rate-strap",
            "CYCPLUS H2 Датчик сердечного ритма Нагрудный ремень "
            "ANT BLE Велосипедный компьютер Аксессуары для велосипеда",
        )

        self.assertEqual(
            subcategory_product_type(strap, subcategory),
            "датчик сердечного ритма",
        )
        self.assertIsNone(subcategory_match_reason(strap, subcategory))


if __name__ == "__main__":
    unittest.main()
