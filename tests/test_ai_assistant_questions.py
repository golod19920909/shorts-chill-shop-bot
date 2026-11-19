from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from handlers.ai_assistant import _products_for_question
from models.product import Marketplace, Product


def make_product(product_id, title):
    return Product(
        marketplace=Marketplace.ALIEXPRESS,
        product_id=product_id,
        title=title,
        product_url=f"https://www.aliexpress.com/item/{product_id}.html",
        price=10.0,
        currency="USD",
    )


class ProductQuestionComparisonScopeTests(unittest.TestCase):
    def setUp(self):
        self.selected = make_product("1", "Наушники TWS")
        self.other = make_product("2", "Наушники Bluetooth")
        self.context = SimpleNamespace(user_data={})
        self.outcome = SimpleNamespace(products=[self.selected, self.other])

    def test_explicit_two_item_comparison_includes_cached_alternative(self):
        with patch(
            "handlers.ai_assistant.common.get_cached_outcome",
            return_value=self.outcome,
        ):
            products = _products_for_question(
                self.context, "session", 0, self.selected, "какой из двух дешевле?"
            )

        self.assertEqual([p.product_id for p in products], ["1", "2"])

    def test_which_product_variant_question_includes_cached_alternative(self):
        with patch(
            "handlers.ai_assistant.common.get_cached_outcome",
            return_value=self.outcome,
        ):
            products = _products_for_question(
                self.context, "session", 0, self.selected, "какой вариант лучше?"
            )

        self.assertEqual([p.product_id for p in products], ["1", "2"])

    def test_question_about_selected_product_uses_only_its_facts(self):
        with patch(
            "handlers.ai_assistant.common.get_cached_outcome",
            return_value=self.outcome,
        ):
            products = _products_for_question(
                self.context, "session", 0, self.selected, "какая автономность у этого товара?"
            )

        self.assertEqual([p.product_id for p in products], ["1"])


if __name__ == "__main__":
    unittest.main()
