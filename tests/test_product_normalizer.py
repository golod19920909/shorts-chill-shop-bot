"""Tests for product normalization.

Covers the shared helpers in ``services.product_normalizer`` (which every
provider must use) and demonstrates the payload -> ``Product`` mapping
pattern with synthetic payloads. No network access and no credentials.
"""

from __future__ import annotations

import unittest

from models.product import Marketplace, Product
from services.product_normalizer import (
    clean_text,
    compute_discount_percent,
    get_first,
    to_float,
    to_int,
)


class NormalizerHelpersTests(unittest.TestCase):
    def test_clean_text(self) -> None:
        self.assertIsNone(clean_text(None))
        self.assertIsNone(clean_text("   "))
        self.assertEqual(clean_text("  Headphones  "), "Headphones")

    def test_clean_text_removes_invisible_marketplace_characters(self) -> None:
        # Real listings arrive with a UTF-8 BOM glued to the first character
        # and zero-width/bidi characters inside the title. They are
        # invisible, but ``strip()`` keeps them (they are not whitespace),
        # so they used to reach the user AND defeat title comparison.
        self.assertEqual(
            clean_text("\ufeff Wireless \u200b Earbuds \u200c Pro \u200d "),
            "Wireless Earbuds Pro",
        )
        self.assertEqual(clean_text("Штаны\u00adспортивные"), "Штаныспортивные")
        self.assertEqual(clean_text("\u202bСкидка\u202c 50%"), "Скидка 50%")

    def test_clean_text_collapses_whitespace_but_keeps_visible_text(self) -> None:
        self.assertEqual(clean_text("Кабель\u00a0USB-C"), "Кабель USB-C")
        self.assertEqual(clean_text("A\nB\tC"), "A B C")
        # Visible characters (including emoji) are never touched.
        self.assertEqual(clean_text(" Наушники  🔥 (2024) "), "Наушники 🔥 (2024)")

    def test_clean_text_that_is_only_invisible_becomes_none(self) -> None:
        self.assertIsNone(clean_text("\ufeff\u200b\u200c\u00a0"))
        self.assertIsNone(clean_text("\u00ad"))

    def test_to_float_parses_common_marketplace_formats(self) -> None:
        self.assertEqual(to_float(19.99), 19.99)
        self.assertEqual(to_float("19.99"), 19.99)
        self.assertEqual(to_float("$19.99"), 19.99)
        self.assertEqual(to_float("19,99 USD"), 19.99)
        self.assertEqual(to_float("US $19.99"), 19.99)

    def test_thousands_separators_are_not_supported(self) -> None:
        # Documented limitation: thousands separators are ambiguous
        # ("1,234" vs "19,99"), and real APIs return plain numbers.
        # Such input is treated as the first numeric token instead of
        # being silently mis-scaled.
        self.assertEqual(to_float("1,234.50"), 1.234)

    def test_to_float_returns_none_instead_of_guessing(self) -> None:
        self.assertIsNone(to_float(None))
        self.assertIsNone(to_float(""))
        self.assertIsNone(to_float("нет данных"))
        self.assertIsNone(to_float(True))

    def test_to_int(self) -> None:
        self.assertEqual(to_int("1523"), 1523)
        self.assertEqual(to_int(4.7), 4)
        self.assertIsNone(to_int("many"))
        self.assertIsNone(to_int(None))

    def test_compute_discount_percent(self) -> None:
        self.assertEqual(compute_discount_percent(75.0, 100.0), 25.0)
        self.assertEqual(compute_discount_percent(49.99, 99.98), 50.0)

    def test_compute_discount_percent_returns_none_when_not_applicable(self) -> None:
        self.assertIsNone(compute_discount_percent(None, 100.0))
        self.assertIsNone(compute_discount_percent(75.0, None))
        self.assertIsNone(compute_discount_percent(100.0, 100.0))
        self.assertIsNone(compute_discount_percent(120.0, 100.0))
        self.assertIsNone(compute_discount_percent(10.0, 0.0))

    def test_get_first_skips_missing_and_empty_values(self) -> None:
        data = {"a": None, "b": "", "c": 0, "d": "value"}
        self.assertEqual(get_first(data, "a", "b", "c", "d"), 0)
        self.assertEqual(get_first(data, "a", "b", "d"), "value")
        self.assertIsNone(get_first(data, "x", "y"))


def build_product_from_payload(payload: dict) -> Product:
    """Example of the mapping each real provider must implement.

    This mirrors the intended provider pattern (generic keys here, since
    real marketplace field names are documented per API and are NOT
    invented in the application code).
    """

    price = to_float(get_first(payload, "price", "sale_price", "min_price"))
    old_price = to_float(get_first(payload, "old_price", "original_price"))

    return Product(
        marketplace=Marketplace(payload["marketplace"]),
        product_id=str(payload["product_id"]),
        title=clean_text(payload.get("title")) or "",
        description=clean_text(payload.get("description")),
        image_url=clean_text(payload.get("image_url")),
        price=price,
        currency=clean_text(payload.get("currency")),
        old_price=old_price,
        discount_percent=compute_discount_percent(price, old_price),
        rating=to_float(payload.get("rating")),
        orders_count=to_int(payload.get("orders_count")),
        shipping_cost=to_float(payload.get("shipping_cost")),
        delivery_estimate=clean_text(payload.get("delivery_estimate")),
        seller_name=clean_text(payload.get("seller_name")),
        product_url=str(payload["product_url"]),
    )


class PayloadNormalizationTests(unittest.TestCase):
    def test_full_payload_is_mapped_correctly(self) -> None:
        product = build_product_from_payload(
            {
                "marketplace": "aliexpress",
                "product_id": 1005001234567890,
                "title": "  Wireless Bluetooth Headphones  ",
                "price": "US $19.99",
                "old_price": "29,99",
                "currency": "USD",
                "rating": "4.8",
                "orders_count": "1523",
                "shipping_cost": 2.5,
                "delivery_estimate": "15-25 days",
                "seller_name": "Audio Store",
                "product_url": "https://example.com/item/1.html",
            }
        )

        self.assertEqual(product.marketplace, Marketplace.ALIEXPRESS)
        self.assertEqual(product.product_id, "1005001234567890")
        self.assertEqual(product.title, "Wireless Bluetooth Headphones")
        self.assertEqual(product.price, 19.99)
        self.assertEqual(product.old_price, 29.99)
        self.assertEqual(product.discount_percent, 33.3)
        self.assertEqual(product.rating, 4.8)
        self.assertEqual(product.orders_count, 1523)
        self.assertEqual(product.shipping_cost, 2.5)
        self.assertEqual(product.total_known_cost, 22.49)

    def test_alternative_price_key_names_are_supported(self) -> None:
        product = build_product_from_payload(
            {
                "marketplace": "aliexpress",
                "product_id": "abc",
                "title": "Earbuds",
                "sale_price": 7.5,
                "product_url": "https://example.com/t/abc",
            }
        )
        self.assertEqual(product.price, 7.5)
        self.assertIsNone(product.discount_percent)

    def test_missing_and_unparseable_fields_stay_none(self) -> None:
        # Missing data must never be silently replaced with 0 or a guess.
        product = build_product_from_payload(
            {
                "marketplace": "aliexpress",
                "product_id": 42,
                "title": "LED strip",
                "price": "ask the seller",
                "rating": "n/a",
                "orders_count": None,
                "shipping_cost": "",
                "product_url": "https://example.com/a/42",
            }
        )

        self.assertIsNone(product.price)
        self.assertIsNone(product.rating)
        self.assertIsNone(product.orders_count)
        self.assertIsNone(product.shipping_cost)
        self.assertIsNone(product.total_known_cost)


if __name__ == "__main__":
    unittest.main()

