"""Stage 55 (F2): a requested number of OPTIONS must not leak into the query.

Stage 54 confirmed that «дай 3 варианта шуруповерта» / «покажи 5 беспроводных
наушников» put the count (and the word «вариантов») into the search phrase and
into ``intent_product_type``, turning a request for N shown products into a
search keyword. The fix keeps option-count framing out of the retrieval phrase
while leaving a UNITS-per-item quantity («3 pieces», «3 шт») untouched and the
product identity intact. Category-independent: no product name is referenced.
"""

from __future__ import annotations

import asyncio
import unittest

from services.ai_service import AIService
from services.query_intent import apply_intent, fallback_intent
from services.text_matching import strip_option_count


def parse(text: str):
    return asyncio.run(AIService(api_key=None).parse_query(text))


def request_for(text: str):
    """The production effective request: fallback parse + intent + apply."""
    return apply_intent(parse(text), fallback_intent(text))


def _search_phrase(request) -> str:
    return (request.normalized_query or "").lower()


class StripHelperTests(unittest.TestCase):
    """The shared matcher helper, in isolation."""

    def test_option_count_nouns_are_removed(self) -> None:
        self.assertEqual(strip_option_count("дай 3 варианта шуруповёрта"), "шуруповёрта")
        self.assertEqual(strip_option_count("покажи 5 наушников"), "наушников")
        self.assertEqual(strip_option_count("шуруповёрт, несколько вариантов"), "шуруповёрт")
        self.assertEqual(strip_option_count("show me 5 earbuds"), "earbuds")
        self.assertEqual(strip_option_count("3 options of drill"), "of drill")

    def test_units_per_item_are_untouched(self) -> None:
        # «3 pieces» / «3 шт» mean UNITS in a product - never an option count.
        self.assertEqual(strip_option_count("3 pieces wireless earbuds"),
                         "3 pieces wireless earbuds")
        self.assertEqual(strip_option_count("наушники 3 шт"), "наушники 3 шт")

    def test_grammatical_variants(self) -> None:
        for text in ("дай 2 вариантов ноутбука", "пара вариантов телефона",
                     "покажите 4 крепления", "покажи 10 отвёрток"):
            self.assertNotIn("вариант", strip_option_count(text).lower())


class EffectiveRequestTests(unittest.TestCase):
    """The real production path: parse + intent + apply_intent."""

    def _assert_count_absent(self, text: str, *, product_stem: str) -> None:
        request = request_for(text)
        phrase = _search_phrase(request)
        self.assertNotIn("вариант", phrase, f"option word leaked for {text!r}: {phrase!r}")
        self.assertNotIn("опци", phrase, f"option word leaked for {text!r}: {phrase!r}")
        self.assertIn(product_stem, phrase, f"product identity lost for {text!r}: {phrase!r}")
        # The count must not survive as the product type either.
        pt = (request.intent_product_type or "").lower()
        self.assertNotIn("вариант", pt, f"option word in product_type for {text!r}: {pt!r}")
        self.assertIn(product_stem, pt, f"product identity lost in product_type for {text!r}: {pt!r}")

    def test_option_count_phrasings_are_stripped(self) -> None:
        self._assert_count_absent("Дай 3 варианта шуруповерта", product_stem="шурупов")
        self._assert_count_absent("Покажи 5 беспроводных наушников", product_stem="наушник")
        self._assert_count_absent("Шуруповерт, несколько вариантов", product_stem="шурупов")
        self._assert_count_absent("дай 2 вариантов ноутбука", product_stem="ноутбук")
        self._assert_count_absent("show 5 earbuds", product_stem="earbud")

    def test_units_quantity_is_still_extracted_and_kept(self) -> None:
        # A pack of 3 pieces: quantity=3 (units), product phrase keeps its words.
        request = parse("Find 3 pieces of wireless earbuds")
        self.assertEqual(request.quantity, 3)
        phrase = _search_phrase(request)
        self.assertIn("earbuds", phrase)

    def test_query_without_count_is_unchanged(self) -> None:
        request = request_for("беспроводные наушники с хорошим басом")
        self.assertIn("наушник", _search_phrase(request))
        self.assertIsNone(request.max_price)

    def test_quantity_is_not_repurposed_for_option_count(self) -> None:
        # «3 варианта» must NOT set quantity (that field means units per item).
        request = parse("Дай 3 варианта шуруповерта")
        self.assertIsNone(request.quantity)


if __name__ == "__main__":
    unittest.main()
