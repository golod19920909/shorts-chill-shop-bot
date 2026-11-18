"""Offline regression tests for the curated catalog product ranker."""

from __future__ import annotations

import unittest

from config.subcategories import Subcategory
from models.product import Availability, Marketplace, Product
from services.subcategory_ranking import _bm25, rank_subcategory_products


def shelf() -> Subcategory:
    return Subcategory(
        code="ranking_test",
        category_code="electronics",
        title="Audio",
        description="Audio products",
        query_routes=("wireless headphones",),
        keywords=("wireless headphones",),
        product_types=("wireless headphones", "bluetooth headphones"),
        exclude_terms=(),
        product_type_groups=(("wireless headphones", "bluetooth headphones"),),
    )


def product(product_id: str, title: str, **fields) -> Product:
    values = {
        "marketplace": Marketplace.ALIEXPRESS,
        "product_id": product_id,
        "title": title,
        "product_url": f"https://www.aliexpress.com/item/{product_id}.html",
        "price": 30.0,
        "currency": "USD",
    }
    values.update(fields)
    return Product(**values)


class BM25Tests(unittest.TestCase):
    def test_okapi_bm25_prefers_stronger_title_match(self):
        scores = _bm25(
            "wireless headphones",
            [
                "wireless headphones",
                "wireless headphones bluetooth gaming microphone "
                "noise cancelling over ear stereo headset",
            ],
        )
        self.assertGreater(scores[0], scores[1])

    def test_curated_ranker_uses_real_positive_feedback_when_stars_are_missing(self):
        weak = product(
            "a-unknown-quality",
            "Wireless headphones",
            orders_count=100,
            discount_percent=0,
        )
        strong = product(
            "z-positive-feedback",
            "Wireless headphones",
            orders_count=100,
            discount_percent=0,
            positive_rate=98.7,
        )

        ranked = rank_subcategory_products([weak, strong], shelf())

        self.assertEqual(
            [item.product_id for item in ranked],
            ["z-positive-feedback", "a-unknown-quality"],
        )
        # The result is deterministic for the same immutable candidate pool.
        self.assertEqual(
            [item.product_id for item in rank_subcategory_products([strong, weak], shelf())],
            ["z-positive-feedback", "a-unknown-quality"],
        )

    def test_marketplace_offer_signals_break_equal_relevance(self):
        ordinary = product(
            "ordinary",
            "Wireless headphones",
            orders_count=100,
            discount_percent=0,
            positive_rate=90,
        )
        attractive = product(
            "attractive",
            "Wireless headphones",
            orders_count=100_000,
            discount_percent=45,
            rating=4.8,
            availability=Availability.IN_STOCK,
            positive_rate=98,
        )

        ranked = rank_subcategory_products([ordinary, attractive], shelf())

        self.assertEqual(ranked[0].product_id, "attractive")
        self.assertEqual(ranked[1].product_id, "ordinary")


if __name__ == "__main__":
    unittest.main()
