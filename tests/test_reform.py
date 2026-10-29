"""Unit tests for the Global Feed, Search & Promo Reform."""

from __future__ import annotations

import unittest
from typing import Any

from curation.pipeline import (
    MODE_DEALS,
    MODE_POPULAR,
    MODE_SEARCH,
    BoredomFilter,
    ImageDeduplicationFilter,
    ModeRanker,
    QualityFilter,
    normalize_image_url,
)
from handlers import discovery
from handlers.common import product_from_snapshot, product_to_dict
from handlers.photo_search import (
    execute_photo_search,
    filter_photo_results,
    should_trigger_fallback,
)
from config.settings import MarketplaceCredentials
from models.product import Marketplace, Product
from services.marketplaces.aliexpress import AliExpressProvider
from services.search_service import SearchOutcome
from services.vision_search import parse_vision_response
from utils.formatting import format_product_card


def make_test_product(
    product_id: str,
    title: str = "Test Product",
    price: float = 10.0,
    image_url: str | None = None,
    orders_count: int | None = None,
    rating: float | None = None,
) -> Product:
    return Product(
        marketplace=Marketplace.ALIEXPRESS,
        product_id=product_id,
        title=title,
        product_url=f"https://aliexpress.com/item/{product_id}.html",
        # G23: a card without a real affiliate link cannot be shown, so
        # even unit fixtures carry one (the validity stage checks it).
        affiliate_url=f"https://s.click.aliexpress.com/e/_{product_id}",
        price=price,
        image_url=image_url,
        orders_count=orders_count,
        rating=rating,
    )


class ImageNormalizationTests(unittest.TestCase):
    """1. normalize_image_url: Test stripping size tags and query parameters."""

    def test_strip_size_tags(self) -> None:
        url1 = "https://ae01.alicdn.com/kf/S123456789.jpg_640x640.jpg"
        self.assertEqual(normalize_image_url(url1), "https://ae01.alicdn.com/kf/s123456789.jpg")

        url2 = "https://ae01.alicdn.com/kf/Habc123.png_220x220.png"
        self.assertEqual(normalize_image_url(url2), "https://ae01.alicdn.com/kf/habc123.png")

        url3 = "https://ae01.alicdn.com/kf/S999_640x640.jpg"
        self.assertEqual(normalize_image_url(url3), "https://ae01.alicdn.com/kf/s999.jpg")

    def test_strip_query_parameters_and_hash(self) -> None:
        url = "https://ae01.alicdn.com/kf/S123.jpg?hash=abc&spec=xyz#frag"
        self.assertEqual(normalize_image_url(url), "https://ae01.alicdn.com/kf/s123.jpg")

    def test_lowercase_and_whitespace_trimming(self) -> None:
        url = "   HTTPS://AE01.ALICDN.COM/KF/Test_Image.JPG_640x640.jpg?spec=1   "
        self.assertEqual(normalize_image_url(url), "https://ae01.alicdn.com/kf/test_image.jpg")

    def test_empty_or_none_url(self) -> None:
        self.assertEqual(normalize_image_url(None), "")
        self.assertEqual(normalize_image_url(""), "")
        self.assertEqual(normalize_image_url("   "), "")


class ImageDeduplicationFilterTests(unittest.TestCase):
    """2. ImageDeduplicationFilter: Retain card with higher orders/rating."""

    def setUp(self) -> None:
        self.filter = ImageDeduplicationFilter()

    def test_retains_card_with_higher_orders(self) -> None:
        card_low = make_test_product(
            "p1",
            "Item A",
            image_url="https://ae01.alicdn.com/kf/same_img.jpg",
            orders_count=10,
            rating=4.5,
        )
        card_high = make_test_product(
            "p2",
            "Item B",
            image_url="https://ae01.alicdn.com/kf/same_img.jpg_640x640.jpg?hash=99",
            orders_count=100,
            rating=4.5,
        )

        result = self.filter.apply([card_low, card_high])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].product_id, "p2")

    def test_retains_card_with_higher_rating_when_orders_tied(self) -> None:
        card_low_rating = make_test_product(
            "p1",
            "Item A",
            image_url="https://ae01.alicdn.com/kf/same_img.jpg",
            orders_count=50,
            rating=4.2,
        )
        card_high_rating = make_test_product(
            "p2",
            "Item B",
            image_url="https://ae01.alicdn.com/kf/same_img.jpg_220x220.png",
            orders_count=50,
            rating=4.9,
        )


class BoredomFilterTests(unittest.TestCase):
    """3. BoredomFilter: Test suppression of banned keywords and items < $0.99."""

    def setUp(self) -> None:
        self.popular_filter = BoredomFilter(mode=MODE_POPULAR)
        self.deals_filter = BoredomFilter(mode=MODE_DEALS)
        self.search_filter = BoredomFilter(mode=MODE_SEARCH)

    def test_banned_keywords_suppressed_in_popular_and_deals(self) -> None:
        banned_titles = [
            "Щеточки для наращивания ресниц 100 шт",
            "Набор одноразовых салфеток для салона 500 шт",
            "Патчи гидрогелевые 1000 шт",
            "Носки мужские набор",
            "Носки женские хлопковые",
            "Базовые штаны спортивные",
            "Простые трусы боксеры",
            "Кабель usb Type-C быстрая зарядка",
            "Защитное стекло для смартфона",
        ]
        for title in banned_titles:
            product = make_test_product("p", title=title, price=5.0)
            self.assertFalse(
                self.popular_filter.is_valid(product),
                f"Expected '{title}' to be suppressed in popular mode",
            )
            self.assertFalse(
                self.deals_filter.is_valid(product),
                f"Expected '{title}' to be suppressed in deals mode",
            )

    def test_price_floor_below_99_cents_suppressed_in_curated(self) -> None:
        cheap_product = make_test_product("p", title="Крутой брелок", price=0.50)
        self.assertFalse(self.popular_filter.is_valid(cheap_product))
        self.assertFalse(self.deals_filter.is_valid(cheap_product))

        valid_product = make_test_product("p", title="Крутой брелок", price=1.20)
        self.assertTrue(self.popular_filter.is_valid(valid_product))
        self.assertTrue(self.deals_filter.is_valid(valid_product))

    def test_strict_constraint_allows_all_items_in_search_mode(self) -> None:
        cheap_item = make_test_product("p1", title="Кабель USB 100 шт", price=0.45)
        self.assertTrue(self.search_filter.is_valid(cheap_item))
        self.assertEqual(len(self.search_filter.apply([cheap_item])), 1)


class NoBogoClaimTests(unittest.TestCase):
    """G24.4: the card must never claim a "1+1 / second free" promotion.

    ``product.query`` has no documented BOGO field (G24.3 audit), so a
    product TITLE - "в подарок", "1+1", "buy 1 get 1" and the like - can
    never prove such a promotion and must never produce a badge.
    """

    @staticmethod
    def parse(title: str) -> Product:
        """Normalize one raw AliExpress row with the production parser."""
        provider = AliExpressProvider(MarketplaceCredentials("key", "secret"))
        raw = {
            "product_id": "777001",
            "product_title": title,
            "product_detail_url": "https://www.aliexpress.com/item/777001.html",
            "sale_price": "9.99",
            "sale_price_currency": "USD",
            "promotion_link": "https://s.click.aliexpress.com/e/_test777",
            "product_main_image_url": "https://img.aliexpress.com/x.jpg",
        }
        products = provider.parse_products(
            {"result": {"products": {"product": [raw]}}}
        )
        if not products:
            raise AssertionError(f"fixture product was dropped: {title!r}")
        return products[0]

    def assert_no_bogo_claim(self, card: str) -> None:
        self.assertNotIn("АКЦИЯ: 1+1", card)
        self.assertNotIn("ВТОРОЙ В ПОДАРОК", card)
        self.assertFalse(card.startswith("🎁"), card)

    def test_a_normal_product_has_no_bogo_badge(self) -> None:
        card = format_product_card(
            make_test_product("std1", "Беспроводные наушники TWS", price=15.0)
        )

        self.assert_no_bogo_claim(card)

    def test_v_podarok_in_a_title_is_not_a_bogo_promotion(self) -> None:
        # Live false positive found by G24.3: an avocado-keychain gift set
        # was labelled "1+1 / second free" merely for the words "в подарок".
        product = self.parse("Силиконовый чехол для телефона в подарок")

        self.assert_no_bogo_claim(format_product_card(product))
        self.assertEqual(product.title, "Силиконовый чехол для телефона в подарок")

    def test_1_plus_1_in_a_title_is_not_a_bogo_promotion(self) -> None:
        product = self.parse("Беспроводные наушники TWS 1+1")

        self.assert_no_bogo_claim(format_product_card(product))

    def test_buy_1_get_1_in_a_title_is_not_a_bogo_promotion(self) -> None:
        product = self.parse("Buy 1 Get 1 free socks")

        self.assert_no_bogo_claim(format_product_card(product))

    def test_bogo_wording_does_not_touch_unrelated_normalization(self) -> None:
        # G24.4: removing the claim must not change any other field.
        plain = self.parse("Обычный спиннинг рыболовный carbon 2.1m")
        promo_worded = self.parse(
            "Обычный спиннинг рыболовный carbon 2.1m 1+1 в подарок"
        )

        self.assertEqual(promo_worded.price, plain.price)
        self.assertEqual(promo_worded.currency, plain.currency)
        self.assertEqual(promo_worded.product_url, plain.product_url)
        self.assertEqual(promo_worded.affiliate_url, plain.affiliate_url)
        self.assertEqual(promo_worded.image_url, plain.image_url)

    def test_an_old_snapshot_with_bogo_keys_is_still_readable(self) -> None:
        # Snapshots saved before G24.4 carry is_bogo/promo_tag; the
        # snapshot reader drops keys the model no longer defines, so
        # stored favorites keep loading.
        product = self.parse("Обычный спиннинг рыболовный carbon 2.1m")
        snapshot = product_to_dict(product)
        snapshot["is_bogo"] = True
        snapshot["promo_tag"] = "🎁 АКЦИЯ: 1+1 / ВТОРОЙ В ПОДАРОК!"

        restored = product_from_snapshot(snapshot)

        self.assertEqual(restored.product_id, product.product_id)
        self.assertFalse(hasattr(restored, "is_bogo"))
        self.assertFalse(hasattr(restored, "promo_tag"))


class RankingBoostTests(unittest.TestCase):
    """G24.4: the BOGO ranking boost is removed - real signals only."""

    def test_bogo_wording_in_a_title_gets_no_ranking_advantage(self) -> None:
        promo_worded = make_test_product("p1", "Item 1+1 в подарок", orders_count=100)
        plain = make_test_product("p2", "Item Normal", orders_count=110)

        ranker = ModeRanker(mode=MODE_POPULAR)
        ranked = ranker.apply([promo_worded, plain])

        # The removed +15% BOGO boost would have promoted p1 (115 > 110);
        # without it the real 110 orders rank first.
        self.assertEqual([p.product_id for p in ranked], ["p2", "p1"])


class PopularCategoryDiscoveryTests(unittest.TestCase):
    """4. Popular Category Discovery: engaging categories rotation & filtering."""

    def test_engaging_categories_pool_contains_required_topics(self) -> None:
        expected = [
            "умный дом гаджеты",
            "автотовары аксессуары",
            "инструменты для дома",
            "туристическое снаряжение",
            "электроника для авто",
            "компьютерные аксессуары",
        ]
        for topic in expected:
            self.assertIn(topic, discovery.ENGAGING_CONSUMER_CATEGORIES)


class VisionSearchAndSmartFallbackTests(unittest.IsolatedAsyncioTestCase):
    """3. Vision Prompt & Photo Search Pipeline tests."""

    def test_parse_vision_response(self) -> None:
        raw_json = '{"query": "спиннинг рыболовный", "fallback_category": "рыболовные снасти"}'
        parsed = parse_vision_response(raw_json)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["query"], "спиннинг рыболовный")
        self.assertEqual(parsed["fallback_category"], "рыболовные снасти")

    def test_should_trigger_fallback_when_few_results_or_zero_orders(self) -> None:
        p1 = make_test_product("1", "A", orders_count=10)
        p2 = make_test_product("2", "B", orders_count=20)
        self.assertTrue(should_trigger_fallback([p1, p2]))

        p3 = make_test_product("3", "C", orders_count=0)
        p1_zero = make_test_product("1", "A", orders_count=0)
        p2_zero = make_test_product("2", "B", orders_count=0)
        self.assertTrue(should_trigger_fallback([p1_zero, p2_zero, p3]))

        self.assertFalse(should_trigger_fallback([p1, p2, p3]))

    def test_photo_results_use_the_search_quality_profile(self) -> None:
        # A photo is a DIRECT search: $1 price floor, NO ceiling - and NO
        # orders floor. The orders field of a keyword answer is a
        # recent-window volume (live audit 2026: ordinary relevant listings
        # read 0..tens), so dropping sub-100 items here would hide the very
        # products the vision query found. Popularity stays in the ordering.
        weak = make_test_product("w1", "Фонарик налобный", price=12.0, orders_count=99)
        strong = make_test_product("s1", "Фонарик налобный", price=12.0, orders_count=100)
        pricey = make_test_product(
            "p1", "Профессиональный объектив", price=5000.0, orders_count=500
        )
        tooCheap = make_test_product(
            "c1", "Сувенирный брелок", price=0.5, orders_count=9999
        )

        kept = filter_photo_results([weak, strong, pricey, tooCheap])

        # Everything real survives - except the sub-$1 item (price floor).
        self.assertEqual([p.product_id for p in kept], ["w1", "s1", "p1"])

    async def test_execute_photo_search_smart_fallback(self) -> None:
        class FakeSearchService:
            async def find_products(self, request, **kwargs):
                if request.original_query == "narrow query":
                    return SearchOutcome(
                        request=request,
                        products=[
                            make_test_product("p1", "Item P1", price=10.0, orders_count=0)
                        ],
                    )
                return SearchOutcome(
                    request=request,
                    products=[
                        make_test_product(
                            f"fb_{i}", f"Item FB {i}", price=15.0, orders_count=100 + i
                        )
                        for i in range(4)
                    ],
                )

        results = await execute_photo_search(
            "narrow query", "fallback cat", FakeSearchService()
        )
        self.assertTrue(len(results) >= 4)
        self.assertTrue(any(p.product_id.startswith("fb_") for p in results))


if __name__ == "__main__":
    unittest.main()
