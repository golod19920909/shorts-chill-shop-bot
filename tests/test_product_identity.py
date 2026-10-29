"""Tests for ``services.product_identity``: identity and deduplication.

The bot must never show one listing twice, and it must not merge two
different listings that merely look similar. Both directions are tested
with plain data - no network, no marketplace.
"""

from __future__ import annotations

import unittest

from models.product import Marketplace, Product
from services.product_identity import (
    canonical_product_url,
    deduplicate_products,
    normalized_product_url,
    normalize_title,
    product_identity,
)
from services.product_normalizer import clean_text


def make_product(
    product_id: str = "1005001",
    *,
    title: str = "Товар",
    url: str = "https://www.aliexpress.com/item/1005001.html",
    price: float | None = 19.99,
    affiliate_url: str | None = None,
) -> Product:
    return Product(
        marketplace=Marketplace.ALIEXPRESS,
        product_id=product_id,
        title=title,
        product_url=url,
        price=price,
        currency="USD",
        affiliate_url=affiliate_url,
    )


class CanonicalUrlTests(unittest.TestCase):
    def test_tracking_parameters_are_removed_and_the_rest_sorted(self) -> None:
        canonical = canonical_product_url(
            "https://www.aliexpress.com/item/1005001.html"
            "?aff_fcid=trace&utm_source=telegram&b=2&a=1"
        )

        self.assertEqual(canonical, "https://aliexpress.com/item/1005001.html?a=1&b=2")

    def test_scheme_host_and_fragment_are_normalized(self) -> None:
        self.assertEqual(
            canonical_product_url("http://m.Aliexpress.com//item//1005001.html#top"),
            "https://aliexpress.com/item/1005001.html",
        )

    def test_tracking_variants_of_one_listing_are_equal(self) -> None:
        first = canonical_product_url(
            "https://www.aliexpress.com/item/1005001.html?aff_trace_key=abc"
        )
        second = canonical_product_url(
            "https://aliexpress.com/item/1005001.html?aff_platform=link"
        )

        self.assertEqual(first, second)

    def test_different_items_stay_different(self) -> None:
        self.assertNotEqual(
            canonical_product_url("https://aliexpress.com/item/1005001.html"),
            canonical_product_url("https://aliexpress.com/item/1005002.html"),
        )

    def test_non_web_urls_have_no_canonical_form(self) -> None:
        for url in (None, "", "   ", "/item/1.html", "javascript:void(0)", "not a url"):
            self.assertIsNone(canonical_product_url(url), url)


class NormalizedUrlTests(unittest.TestCase):
    def test_query_is_dropped_entirely(self) -> None:
        self.assertEqual(
            normalized_product_url(
                "https://www.aliexpress.com/item/1005001.html?variant=red"
            ),
            "aliexpress.com/item/1005001.html",
        )

    def test_www_and_trailing_slash_are_removed(self) -> None:
        self.assertEqual(
            normalized_product_url("https://s.click.aliexpress.com/e/_ABC/"),
            "s.click.aliexpress.com/e/_ABC",
        )

    def test_invalid_url_returns_none(self) -> None:
        self.assertIsNone(normalized_product_url("http://"))
        self.assertIsNone(normalized_product_url(None))


class IdentityPriorityTests(unittest.TestCase):
    def test_product_id_wins_over_everything_else(self) -> None:
        product = make_product(
            "1005001", url="https://www.aliexpress.com/item/other.html"
        )

        kind, value = product_identity(product)

        self.assertEqual(kind, "id")
        self.assertEqual(value, "aliexpress:1005001")

    def test_canonical_url_is_used_without_an_id(self) -> None:
        product = make_product(
            "   ",
            url=(
                "https://www.aliexpress.com/item/1005001.html"
                "?aff_trace_key=trace"
            ),
        )

        kind, value = product_identity(product)

        self.assertEqual(kind, "canonical_url")
        self.assertEqual(value, "https://aliexpress.com/item/1005001.html")

    def test_title_and_price_are_the_last_resort(self) -> None:
        product = make_product("", title="  Чехол   для iPhone ", url="", price=2.5)

        kind, value = product_identity(product)

        self.assertEqual(kind, "title_price")
        self.assertEqual(value, "aliexpress:чехол для iphone:2.50:USD")

    def test_identical_products_without_any_identity_are_not_merged(self) -> None:
        """Two truly unknown products stay separate, not collapsed."""

        first = make_product("", title="", url="", price=None)
        second = make_product("", title="", url="", price=None)

        identities = {product_identity(first), product_identity(second)}

        self.assertEqual(len(identities), 2)

    def test_normalize_title_collapses_case_and_spaces(self) -> None:
        self.assertEqual(normalize_title("  Wireless   EARBUDS "), "wireless earbuds")


class DeduplicationTests(unittest.TestCase):
    def test_same_id_with_different_affiliate_links_is_one_product(self) -> None:
        plain = make_product("1005001")
        affiliate = make_product(
            "1005001",
            url="https://aliexpress.com/item/1005001.html?aff_trace_key=x",
            affiliate_url="https://s.click.aliexpress.com/e/_AFF",
        )

        unique = deduplicate_products([plain, affiliate, plain])

        self.assertEqual(len(unique), 1)
        self.assertIs(unique[0], plain)

    def test_different_ids_are_kept_even_with_equal_titles_and_prices(self) -> None:
        """A phone case is not another phone case: no fuzzy merging."""

        first = make_product("1005001", title="Чехол для iPhone", price=2.99)
        second = make_product("1005002", title="Чехол для iPhone", price=2.99)

        self.assertEqual(len(deduplicate_products([first, second])), 2)

    def test_duplicates_without_ids_are_merged_by_canonical_url(self) -> None:
        first = make_product("", url="https://aliexpress.com/item/7.html")
        second = make_product(
            "", url="https://www.aliexpress.com/item/7.html?aff_trace_key=t"
        )

        unique = deduplicate_products([first, second])

        self.assertEqual(len(unique), 1)

    def test_order_is_preserved(self) -> None:
        products = [
            make_product("a"),
            make_product("b"),
            make_product("a"),
            make_product("c"),
        ]

        ids = [product.product_id for product in deduplicate_products(products)]

        self.assertEqual(ids, ["a", "b", "c"])

    def test_empty_input(self) -> None:
        self.assertEqual(deduplicate_products([]), [])


class InvisibleCharacterIdentityTests(unittest.TestCase):
    """A hidden BOM/zero-width character must not create a second product.

    The last-resort identity is ``title + price + currency`` and a BOM is
    NOT whitespace, so before ``clean_text`` learned to strip invisible
    characters, ``"\\ufeffШтаны"`` and ``"Штаны"`` were two different
    identities - one listing reached the feed twice.
    """

    def test_titles_differing_only_by_invisible_characters_are_one_product(
        self,
    ) -> None:
        plain = make_product(
            "", title=clean_text("Штаны спортивные"), url="", price=19.99
        )
        contaminated = make_product(
            "",
            title=clean_text("\ufeff Штаны\u200b спортивные"),
            url="",
            price=19.99,
        )

        self.assertEqual(
            product_identity(plain), product_identity(contaminated)
        )
        self.assertEqual(len(deduplicate_products([plain, contaminated])), 1)


if __name__ == "__main__":
    unittest.main()

