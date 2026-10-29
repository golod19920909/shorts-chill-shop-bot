"""Tests for the marketplace-independent ``Product`` model."""

from __future__ import annotations

import unittest

from pydantic import ValidationError

from models.product import Availability, Marketplace, Product


def make_product(**overrides) -> Product:
    data = {
        "marketplace": Marketplace.ALIEXPRESS,
        "product_id": "1005001234567890",
        "title": "Wireless Headphones",
        "product_url": "https://example.com/item/1005001234567890.html",
    }
    data.update(overrides)
    return Product(**data)


class ProductModelTests(unittest.TestCase):
    def test_minimal_product_leaves_optional_fields_empty(self) -> None:
        product = make_product()

        self.assertEqual(product.marketplace, Marketplace.ALIEXPRESS)
        self.assertIsNone(product.price)
        self.assertIsNone(product.rating)
        self.assertIsNone(product.shipping_cost)
        self.assertIsNone(product.affiliate_url)
        self.assertEqual(product.availability, Availability.UNKNOWN)

    def test_required_fields_are_enforced(self) -> None:
        with self.assertRaises(ValidationError):
            Product(
                marketplace=Marketplace.ALIEXPRESS, title="x", product_url="https://e.com"
            )

    def test_unknown_marketplace_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            make_product(marketplace="amazon")

    def test_unknown_extra_fields_are_rejected(self) -> None:
        # `extra="forbid"` prevents silent drift between providers.
        with self.assertRaises(ValidationError):
            make_product(unexpected_field=1)

    def test_rating_out_of_range_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            make_product(rating=5.5)
        with self.assertRaises(ValidationError):
            make_product(rating=-0.1)

    def test_negative_price_and_orders_are_rejected(self) -> None:
        for field in ("price", "old_price", "shipping_cost"):
            with self.subTest(field=field):
                with self.assertRaises(ValidationError):
                    make_product(**{field: -1})
        with self.assertRaises(ValidationError):
            make_product(orders_count=-5)

    def test_removed_marketplaces_cannot_be_validated(self) -> None:
        # Legacy snapshots may still carry a marketplace that no longer
        # exists. Refusing them here is exactly what keeps such rows off
        # every user-facing screen instead of showing dead offers.
        for removed in ("temu", "alibaba"):
            with self.subTest(marketplace=removed):
                with self.assertRaises(ValidationError):
                    make_product(marketplace=removed)

    def test_aliexpress_is_the_only_marketplace_value(self) -> None:
        self.assertEqual([value.value for value in Marketplace], ["aliexpress"])

    def test_total_known_cost_needs_both_price_and_shipping(self) -> None:
        self.assertIsNone(make_product().total_known_cost)
        self.assertIsNone(make_product(price=19.99).total_known_cost)
        self.assertIsNone(make_product(shipping_cost=3.0).total_known_cost)
        self.assertEqual(make_product(price=19.99, shipping_cost=3.0).total_known_cost, 22.99)

    def test_free_shipping_is_a_known_cost(self) -> None:
        # 0.0 is real data (free shipping), not missing data.
        product = make_product(price=10.0, shipping_cost=0.0)
        self.assertEqual(product.total_known_cost, 10.0)

    def test_marketplace_enum_serializes_to_plain_string(self) -> None:
        payload = make_product().model_dump(mode="json")
        self.assertEqual(payload["marketplace"], "aliexpress")
        self.assertEqual(payload["availability"], "unknown")

    def test_round_trip_through_json_keeps_data(self) -> None:
        original = make_product(
            price=29.99,
            currency="USD",
            old_price=49.99,
            discount_percent=40.0,
            rating=4.7,
            orders_count=1523,
            shipping_cost=2.5,
            seller_name="Shop",
            availability=Availability.IN_STOCK,
        )
        restored = Product.model_validate(original.model_dump(mode="json"))

        self.assertEqual(restored, original)


if __name__ == "__main__":
    unittest.main()
