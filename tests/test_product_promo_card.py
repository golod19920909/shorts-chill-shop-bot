"""Regression: product-level promo data must survive the whole pipeline.

Live-verified ``promo_code_info`` (this app key) is a mapping carrying the
coupon code, the marketplace's own benefit sentence (discount amount +
minimum order with currency) and the real validity end. The regression
under test: the card used to keep ONLY the code and silently drop the
amount/conditions the API actually provided.

Pinned contract:

1. ``extract_promo_info`` reads the live shape verbatim (no parsing of
   unknown shapes, nothing invented);
2. ``AliExpressProvider.parse_products`` carries all three fields onto
   ``Product``;
3. ``format_product_card`` / ``product_card_text`` render every provided
   field and NOTHING for a product without real promo data;
4. snapshots (favorites/tracking round-trip) keep the fields.
"""

from __future__ import annotations

import asyncio
import unittest

from config.settings import MarketplaceCredentials
from handlers.common import product_card_text, product_from_snapshot, product_to_dict
from models.product import Marketplace, Product
from services.marketplaces.aliexpress import (
    AliExpressProvider,
    extract_promo_code,
    extract_promo_info,
)
from utils.formatting import format_product_card

#: Exactly what the live API returned for a product with a promo code.
LIVE_PROMO_INFO = {
    "code_campaigntype": "1",
    "code_availabletime_end": "2026-09-29 23:59:59",
    "code_quantity": "1200",
    "code_availabletime_start": "2026-09-21 00:00:00",
    "code_value": "On order over USD 104.7 , get USD 10.48 off",
    "promo_code": "3N41SN59P8VB",
    "code_mini_spend": "104.7",
}

CODE = "3N41SN59P8VB"
BENEFIT = "On order over USD 104.7 , get USD 10.48 off"
VALID_UNTIL = "2026-09-29 23:59:59"


def run(coro):
    return asyncio.run(coro)


def raw_product(**overrides) -> dict:
    raw = {
        "product_id": "999001",
        "product_title": "Беспроводные наушники TWS Max",
        "product_detail_url": "https://www.aliexpress.com/item/999001.html",
        "sale_price": "19.99",
        "sale_price_currency": "USD",
        "promotion_link": "https://s.click.aliexpress.com/e/_test999",
        "product_main_image_url": "https://img.aliexpress.com/x.jpg",
    }
    raw.update(overrides)
    return raw


def parse_one(raw: dict) -> Product:
    provider = AliExpressProvider(MarketplaceCredentials("key", "secret"))
    products = provider.parse_products({"result": {"products": {"product": [raw]}}})
    assert products, "the probe product must normalize"
    return products[0]


class ExtractPromoInfoTests(unittest.TestCase):
    """``extract_promo_info``: live shape in, verbatim fields out."""

    def test_live_shape_returns_all_three_fields(self) -> None:
        code, benefit, valid = extract_promo_info(LIVE_PROMO_INFO)

        self.assertEqual(code, CODE)
        self.assertEqual(benefit, BENEFIT)
        self.assertEqual(valid, VALID_UNTIL)

    def test_extract_promo_code_keeps_its_single_value_contract(self) -> None:
        self.assertEqual(extract_promo_code(LIVE_PROMO_INFO), CODE)
        self.assertIsNone(extract_promo_code(None))

    def test_plain_string_stays_the_code_verbatim(self) -> None:
        self.assertEqual(extract_promo_info("ABCD1234"), ("ABCD1234", None, None))

    def test_json_encoded_mapping_is_unwrapped(self) -> None:
        import json

        code, benefit, valid = extract_promo_info(json.dumps(LIVE_PROMO_INFO))

        self.assertEqual((code, benefit, valid), (CODE, BENEFIT, VALID_UNTIL))

    def test_unknown_shapes_yield_none_and_never_raise(self) -> None:
        for raw in (None, 42, [], {}, object(), ["promo"]):
            with self.subTest(raw=raw):
                self.assertEqual(extract_promo_info(raw), (None, None, None))

    def test_benefit_without_a_code_is_still_returned(self) -> None:
        code, benefit, valid = extract_promo_info(
            {"code_value": "USD 5 off", "code_availabletime_end": None}
        )

        self.assertIsNone(code)
        self.assertEqual(benefit, "USD 5 off")
        self.assertIsNone(valid)


class PipelineTests(unittest.TestCase):
    """raw payload -> provider -> Product: no field is lost on the way."""

    def test_normalization_carries_all_promo_fields(self) -> None:
        product = parse_one(raw_product(promo_code_info=LIVE_PROMO_INFO))

        self.assertEqual(product.promo_code, CODE)
        self.assertEqual(product.promo_benefit, BENEFIT)
        self.assertEqual(product.promo_valid_until, VALID_UNTIL)

    def test_a_product_without_promo_data_keeps_none_everywhere(self) -> None:
        product = parse_one(raw_product())

        self.assertIsNone(product.promo_code)
        self.assertIsNone(product.promo_benefit)
        self.assertIsNone(product.promo_valid_until)

    def test_promo_data_survives_the_favorite_snapshot_round_trip(self) -> None:
        product = parse_one(raw_product(promo_code_info=LIVE_PROMO_INFO))

        restored = product_from_snapshot(product_to_dict(product))

        self.assertEqual(restored.promo_code, CODE)
        self.assertEqual(restored.promo_benefit, BENEFIT)
        self.assertEqual(restored.promo_valid_until, VALID_UNTIL)


class CardRenderingTests(unittest.TestCase):
    """The card shows every provided field - and invents nothing."""

    @staticmethod
    def _product(**promo) -> Product:
        updates = {
            "promo_code": CODE,
            "promo_benefit": BENEFIT,
            "promo_valid_until": VALID_UNTIL,
        }
        updates.update(promo)
        return Product(
            marketplace=Marketplace.ALIEXPRESS,
            product_id="999001",
            title="Набор для рисования",
            product_url="https://www.aliexpress.com/item/999001.html",
            price=19.99,
            currency="USD",
            **updates,
        )

    def test_full_promo_block_is_rendered(self) -> None:
        text = format_product_card(self._product())

        self.assertIn(f"🎟 Промокод: {CODE}", text)
        self.assertIn(BENEFIT, text)
        self.assertIn(f"Действует до: {VALID_UNTIL}", text)

    def test_missing_fields_are_omitted_not_guessed(self) -> None:
        text = format_product_card(
            self._product(promo_benefit=None, promo_valid_until=None)
        )

        self.assertIn(f"🎟 Промокод: {CODE}", text)
        self.assertNotIn("Действует до:", text)
        self.assertNotIn(BENEFIT, text)

    def test_a_benefit_without_a_code_never_claims_a_code(self) -> None:
        text = format_product_card(self._product(promo_code=None))

        self.assertIn(f"🎟 {BENEFIT}", text)
        self.assertNotIn("Промокод:", text)

    def test_no_promo_data_means_no_promo_block(self) -> None:
        text = format_product_card(
            self._product(promo_code=None, promo_benefit=None, promo_valid_until=None)
        )

        self.assertNotIn("🎟", text)

    def test_the_telegram_card_body_contains_the_real_amount(self) -> None:
        body = product_card_text(self._product())

        self.assertIn("USD 10.48 off", body)
        self.assertIn(CODE, body)


if __name__ == "__main__":
    unittest.main()
