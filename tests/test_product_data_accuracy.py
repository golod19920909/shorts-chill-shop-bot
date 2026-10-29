"""G24.2 regression: AliExpress product data accuracy in the card.

A live RAW-API audit found three reported mismatches between the website
and the bot card (orders=0 vs 104 sold, rating "0%" vs 4.8 stars, a
price that must come from the very field the API returned). These tests
pin the mapping stage by stage, using captured RAW shapes:

    RAW API -> _normalize() -> Product -> format_product_card()

Nothing here filters a product out: the card must show the API's own
values, and a value the API did NOT provide must stay unknown (None)
instead of being rendered as a false zero. No network: every case is a
raw dict fed straight into ``AliExpressProvider._normalize``.
"""

from __future__ import annotations

import unittest

from config.settings import MarketplaceCredentials
from services.marketplaces.aliexpress import AliExpressProvider
from utils.formatting import format_product_card

#: A captured keyword-answer row (UAH context): the TARGET pair is the
#: answer in the requested currency, the BASE pair is CNY - mixing the
#: two would fabricate a discount from different units.
TARGET_PAIR_RAW = {
    "product_id": 1005012746771306,
    "product_title": "Стабилизирующий домкрат для прицепа",
    "product_detail_url": "https://ru.aliexpress.com/item/1005012746771306.html",
    "product_main_image_url": "https://ae-pic-a1.aliexpress-media.com/kf/x.jpg",
    "sale_price": "248.30",
    "sale_price_currency": "CNY",
    "target_sale_price": "1833.18",
    "target_sale_price_currency": "UAH",
    "original_price": "435.62",
    "original_price_currency": "CNY",
    "target_original_price": "3216.14",
    "target_original_price_currency": "UAH",
    "discount": "43%",
    "lastest_volume": 104,
    "evaluate_rate": "96.0%",
    "promotion_link": "https://s.click.aliexpress.com/e/_stabilizer",
}


def provider() -> AliExpressProvider:
    """A provider used ONLY for its pure normalization (no network)."""

    return AliExpressProvider(MarketplaceCredentials("key", "secret"))


def normalize(raw: dict):
    return provider()._normalize(dict(raw))


class OrdersMappingTests(unittest.TestCase):
    """RAW ``lastest_volume`` -> Product.orders_count -> card line (§7)."""

    def test_a_orders_mapping_raw_104(self) -> None:
        # Case A: the API answered lastest_volume=104 -> the card shows 104.
        raw = dict(TARGET_PAIR_RAW, lastest_volume=104)
        product = normalize(raw)
        self.assertEqual(product.orders_count, 104)
        self.assertIn("\U0001F9FE Заказов: 104", format_product_card(product))

    def test_b_high_orders_raw_5000(self) -> None:
        # Case B: lastest_volume=5000 must not be clipped, scaled or zeroed.
        raw = dict(TARGET_PAIR_RAW, lastest_volume=5000)
        product = normalize(raw)
        self.assertEqual(product.orders_count, 5000)
        self.assertIn("\U0001F9FE Заказов: 5000", format_product_card(product))

    def test_d_missing_orders_stay_unknown_not_zero(self) -> None:
        # Case A/B reality: the API may omit the field entirely. None is
        # "unknown" and must never be rendered as "0 orders" (= "no sales").
        raw = dict(TARGET_PAIR_RAW)
        raw.pop("lastest_volume")
        product = normalize(raw)
        self.assertIsNone(product.orders_count)
        card = format_product_card(product)
        self.assertNotIn("Заказов", card)
        self.assertNotIn("Заказов: 0", card)

    def test_d_explicit_zero_orders_is_a_real_zero(self) -> None:
        # The API really answering 0 stays 0 - it is a measured value.
        raw = dict(TARGET_PAIR_RAW, lastest_volume=0)
        product = normalize(raw)
        self.assertEqual(product.orders_count, 0)
        self.assertIn("\U0001F9FE Заказов: 0", format_product_card(product))


class RatingMappingTests(unittest.TestCase):
    """RAW ``evaluate_rate`` -> Product.positive_rate -> card line (§8)."""

    def test_c_rating_raw_percent_string(self) -> None:
        # Case C: a real share "96.0%" renders as exactly 96%.
        raw = dict(TARGET_PAIR_RAW, evaluate_rate="96.0%")
        product = normalize(raw)
        self.assertEqual(product.positive_rate, 96.0)
        self.assertIn(
            "\U0001F44D Положительные отзывы: 96%",
            format_product_card(product),
        )

    def test_c_zero_share_is_missing_data_not_a_measured_zero(self) -> None:
        # Case C website shows 4.8 stars while the keyword answer carries
        # "0.0%" - AliExpress uses it for "no share at all". Rendering
        # "0%" would claim a measured zero, so the value stays unknown.
        raw = dict(TARGET_PAIR_RAW, evaluate_rate="0.0%")
        product = normalize(raw)
        self.assertIsNone(product.positive_rate)
        card = format_product_card(product)
        self.assertNotIn("Положительные отзывы", card)
        self.assertNotIn("отзывы: 0%", card)

    def test_d_missing_rating_is_not_rendered(self) -> None:
        # The live G24 feed omits evaluate_rate for every row: the line
        # must be absent rather than "0%".
        raw = dict(TARGET_PAIR_RAW)
        raw.pop("evaluate_rate")
        product = normalize(raw)
        self.assertIsNone(product.positive_rate)
        self.assertNotIn("Положительные отзывы", format_product_card(product))

    def test_no_star_rating_is_invented(self) -> None:
        # evaluate_rate is a share, never a 0-5 star rating.
        product = normalize(dict(TARGET_PAIR_RAW, evaluate_rate="96.0%"))
        self.assertIsNone(product.rating)
        self.assertNotIn("⭐ Рейтинг", format_product_card(product))


class PriceMappingTests(unittest.TestCase):
    """RAW price fields -> Product.price -> card (§9): the TARGET pair."""

    def test_e_price_is_the_target_sale_price_not_the_base_price(self) -> None:
        # Captured live RAW: target_sale_price=1833.18 UAH while the base
        # sale_price is 248.30 CNY. The card must show the target value
        # in the target currency - the website/bot discrepancy (601 vs 648)
        # was a price context/timing issue, so the ONLY correct mapping is
        # "exactly the field the API returned".
        product = normalize(TARGET_PAIR_RAW)
        self.assertEqual(product.price, 1833.18)
        self.assertEqual(product.currency, "UAH")
        self.assertNotEqual(product.price, 248.30)

    def test_f_no_double_conversion_of_a_target_price(self) -> None:
        # target_currency=UAH: the value already IS in UAH - it must pass
        # through untouched (the project converts no currencies anywhere).
        product = normalize(TARGET_PAIR_RAW)
        self.assertEqual(product.price, 1833.18)
        self.assertEqual(format_product_card(product).splitlines()[2], "\U0001F4B0 1833.18 UAH")

    def test_f_api_default_currency_answer_is_kept_as_is(self) -> None:
        # Without a user preference the API answers in its documented
        # default (USD): that value is also rendered verbatim.
        raw = dict(
            TARGET_PAIR_RAW,
            target_sale_price="20.89",
            target_sale_price_currency="USD",
            target_original_price="43.52",
            target_original_price_currency="USD",
        )
        product = normalize(raw)
        self.assertEqual(product.price, 20.89)
        self.assertEqual(product.currency, "USD")

    def test_base_pair_is_used_when_no_target_pair_exists(self) -> None:
        raw = dict(TARGET_PAIR_RAW)
        for key in (
            "target_sale_price",
            "target_sale_price_currency",
            "target_original_price",
            "target_original_price_currency",
        ):
            raw.pop(key)
        product = normalize(raw)
        self.assertEqual(product.price, 248.30)
        self.assertEqual(product.currency, "CNY")
        self.assertEqual(product.old_price, 435.62)


class DiscountConsistencyTests(unittest.TestCase):
    """RAW ``discount`` -> Product.discount_percent -> card (§10/§11)."""

    def test_g_raw_discount_string_is_taken_verbatim(self) -> None:
        product = normalize(TARGET_PAIR_RAW)
        self.assertEqual(product.discount_percent, 43.0)
        self.assertIn("\U0001F525 Скидка: 43%", format_product_card(product))

    def test_g_computed_discount_uses_the_same_currency_pair(self) -> None:
        # No raw discount: it is computed from the SAME (target) pair the
        # price came from - never from a mix of currencies.
        raw = dict(TARGET_PAIR_RAW)
        raw.pop("discount")
        product = normalize(raw)
        self.assertEqual(product.discount_percent, 43.0)

    def test_g_no_base_currency_old_price_next_to_a_target_price(self) -> None:
        # The reported failure mode: price in UAH + old_price in CNY
        # fabricates a meaningless discount. If the target original price
        # is missing, old_price stays unknown.
        raw = dict(TARGET_PAIR_RAW)
        raw.pop("target_original_price")
        product = normalize(raw)
        self.assertEqual(product.price, 1833.18)
        self.assertIsNone(product.old_price)
        card = format_product_card(product)
        self.assertNotIn("435.62", card)

    def test_g_zero_discount_is_not_rendered_as_a_discount(self) -> None:
        product = normalize(dict(TARGET_PAIR_RAW, discount="0%"))
        self.assertEqual(product.discount_percent, 0.0)
        self.assertNotIn("Скидка", format_product_card(product))


class ProductIdentityTests(unittest.TestCase):
    """One Product = one RAW row: id/url/image/affiliate together (§12)."""

    def test_h_identity_fields_come_from_the_same_raw_row(self) -> None:
        product = normalize(TARGET_PAIR_RAW)
        self.assertEqual(product.product_id, "1005012746771306")
        self.assertEqual(product.product_url, TARGET_PAIR_RAW["product_detail_url"])
        self.assertEqual(product.image_url, TARGET_PAIR_RAW["product_main_image_url"])
        self.assertEqual(product.affiliate_url, TARGET_PAIR_RAW["promotion_link"])
        self.assertEqual(product.title, TARGET_PAIR_RAW["product_title"])


class CardRenderingTests(unittest.TestCase):
    """Normalized values reach Telegram verbatim - no re-conversion (§19)."""

    def test_i_card_shows_exactly_the_normalized_values(self) -> None:
        product = normalize(
            dict(
                TARGET_PAIR_RAW,
                target_sale_price="648.17",
                lastest_volume=104,
                evaluate_rate="96.0%",
                discount="7%",
            )
        )
        card = format_product_card(product)
        self.assertIn("\U0001F4B0 648.17 UAH", card)
        self.assertIn("\U0001F9FE Заказов: 104", card)
        self.assertIn("\U0001F44D Положительные отзывы: 96%", card)
        self.assertIn("\U0001F525 Скидка: 7%", card)

    def test_i_card_omits_every_unknown_field(self) -> None:
        raw = dict(TARGET_PAIR_RAW)
        raw.pop("lastest_volume")
        raw.pop("evaluate_rate")
        raw.pop("discount")
        # Without an old price in the SAME pair there is nothing to
        # compute a discount from - the line must disappear, not guess.
        raw.pop("target_original_price")
        raw.pop("original_price")
        card = format_product_card(normalize(raw))
        self.assertNotIn("Заказов", card)
        self.assertNotIn("Положительные отзывы", card)
        self.assertNotIn("Скидка", card)
        # The fields the API DID return are still shown.
        self.assertIn("\U0001F4B0 1833.18 UAH", card)


if __name__ == "__main__":
    unittest.main()


