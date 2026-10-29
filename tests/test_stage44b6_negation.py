"""Stage 44-B6: negation handling in required_features (regression).

Evidence (live Gemini capture, Stage 44-B5): the model answered «пылесос без
мешка» with ``required_features=['без мешка']`` and «ноутбук без операционной
системы» with ``required_features=['без ОС']``. Enforced as a literal
inclusion filter, no marketplace title repeats the negated phrase verbatim, so
the whole page of exactly the requested products was hidden.

The fix (general, category-independent): a phrase that LEADS with a negation
(«без X» / «не X» / «without X» / «no X» / «not X») states an ABSENCE, so it
must never be a literal inclusion filter - it is dropped from
``required_features`` in ``ai_service._to_search_request`` and treated as
"not applied" in ``comparison_service._apply_feature_filters`` (its words
still reach keywords / normalized_query and the structured intent). It is
deliberately NOT turned into an exclusion: the negated word often appears
literally in the CORRECT title («Пылесос БЕЗ МЕШКА»), so excluding «мешок»
would drop the wanted product. A positive requirement plus a separate
exclusion («наушники без провода и без шумоподавления») stays distinct.

Nothing here touches the network: parsing uses the exact captured payloads and
the filter is called with plain ``Product`` objects.
"""

from __future__ import annotations

import unittest

from models.product import Marketplace, Product
from models.search_request import SearchRequest
from services.ai_service import AIService
from services.comparison_service import filter_products
from services.text_matching import is_negation_clause


def make_product(title: str) -> Product:
    return Product(
        marketplace=Marketplace.ALIEXPRESS,
        product_id=title[:20],
        title=title,
        product_url="https://www.aliexpress.com/item/1.html",
        price=10.0,
        currency="USD",
    )


# Exact raw payloads Gemini returned live (Stage 44-B5 capture) for #30/#31/#33.
_PARSE_30 = {
    "original_query": "пылесос без мешка",
    "normalized_query": "bagless vacuum cleaner",
    "category": "пылесосы",
    "keywords": ["пылесос", "без мешка"],
    "required_features": ["без мешка"],
    "excluded_features": [],
}
_PARSE_31 = {
    "original_query": "ноутбук без операционной системы",
    "normalized_query": "laptop no OS",
    "category": "ноутбуки",
    "keywords": ["ноутбук", "без операционной системы"],
    "required_features": ["без ОС"],
    "excluded_features": [],
}
_PARSE_33 = {
    "original_query": "наушники без провода и без шумоподавления",
    "normalized_query": "wireless headphones",
    "category": "headphones",
    "keywords": ["наушники", "беспроводные"],
    "required_features": ["беспроводные"],
    "excluded_features": ["шумоподавление", "ANC", "проводные"],
}


class NegationDetectorTests(unittest.TestCase):
    def test_leading_negation_words_are_detected(self) -> None:
        for phrase in (
            "без мешка",
            "без ОС",
            "без операционной системы",
            "не чехол",
            "without ANC",
            "without a bag",
            "no OS",
            "not a replacement part",
        ):
            with self.subTest(phrase=phrase):
                self.assertTrue(is_negation_clause(phrase))

    def test_positive_attributes_are_never_mistaken_for_a_negation(self) -> None:
        # A word that merely CONTAINS a negatable fragment must stay a literal
        # attribute - the detector is anchored at the FIRST word and matched
        # exactly, so «беспроводной» / «недорогой» / «noise cancelling» are safe.
        for phrase in (
            "беспроводные",
            "беспроводной",
            "недорогой",
            "ноутбук",
            "noise cancelling",
            "шумоподавление",
            "аккумулятор",
            "",
            None,
        ):
            with self.subTest(phrase=phrase):
                self.assertFalse(is_negation_clause(phrase))


class ParsingStageTests(unittest.TestCase):
    def test_negated_phrase_never_becomes_a_literal_required_feature(self) -> None:
        # #30 - the exact live Gemini answer.
        request = AIService._to_search_request(dict(_PARSE_30), "пылесос без мешка")
        self.assertEqual(request.required_features, [])
        self.assertEqual(request.normalized_query, "bagless vacuum cleaner")

        # #31 - the exact live Gemini answer.
        request = AIService._to_search_request(
            dict(_PARSE_31), "ноутбук без операционной системы"
        )
        self.assertEqual(request.required_features, [])

    def test_negation_is_not_converted_into_a_contradictory_exclusion(self) -> None:
        # The negated noun («мешок») must NOT be added to excluded_features: a
        # real bagless title («Пылесос БЕЗ МЕШКА») carries it literally and
        # would be wrongly dropped.
        request = AIService._to_search_request(dict(_PARSE_30), "пылесос без мешка")
        self.assertEqual(request.excluded_features, [])

    def test_positive_requirement_and_separate_exclusion_stay_distinct(self) -> None:
        # #33 - already-correct live answer: «беспроводные» is a POSITIVE
        # requirement; only «шумоподавление/ANC/проводные» are excluded.
        request = AIService._to_search_request(
            dict(_PARSE_33), "наушники без провода и без шумоподавления"
        )
        self.assertEqual(request.required_features, ["беспроводные"])
        self.assertEqual(
            request.excluded_features, ["шумоподавление", "ANC", "проводные"]
        )


class FeatureFilterTests(unittest.TestCase):
    def test_bagless_products_survive_the_vacuum_query(self) -> None:
        # Before the fix ``required_features=['без мешка']`` emptied the page:
        # no real bagless title repeats «без мешка» verbatim.
        request = AIService._to_search_request(dict(_PARSE_30), "пылесос без мешка")
        products = [
            make_product("Пылесос циклонный беспроводной вертикальный"),
            make_product("Bagless Vacuum Cleaner Cordless 20000Pa"),
        ]
        kept = filter_products(products, request)
        self.assertEqual(len(kept), 2)

    def test_no_os_laptops_survive_the_laptop_query(self) -> None:
        request = AIService._to_search_request(
            dict(_PARSE_31), "ноутбук без операционной системы"
        )
        products = [
            make_product("Ноутбук 15.6 дюймов без предустановленной ОС"),
            make_product("Laptop 15.6 inch FreeDOS no OS"),
        ]
        kept = filter_products(products, request)
        self.assertEqual(len(kept), 2)

    def test_a_negation_reaching_the_filter_is_reported_not_applied(self) -> None:
        # Defence in depth: even if a negated phrase reaches the filter through
        # another path, it is treated as intent (ignored_features), never as a
        # literal inclusion filter.
        request = SearchRequest(
            original_query="пылесос без мешка",
            normalized_query="bagless vacuum cleaner",
            required_features=["без мешка"],
        )
        products = [make_product("Пылесос циклонный беспроводной")]
        kept = filter_products(products, request)
        self.assertEqual(len(kept), 1)
        self.assertIn("без мешка", request.ignored_features)

    def test_genuine_exclusion_still_filters(self) -> None:
        # The separate exclusion of #33 keeps working: a WIRED / ANC headphone
        # is removed, the wireless one stays - relevant products are not
        # removed merely because the title lacks a negated phrase.
        request = AIService._to_search_request(
            dict(_PARSE_33), "наушники без провода и без шумоподавления"
        )
        products = [
            make_product("Наушники беспроводные Bluetooth"),
            make_product("Наушники проводные с шумоподавлением"),
        ]
        kept = filter_products(products, request)
        self.assertEqual([p.title for p in kept], ["Наушники беспроводные Bluetooth"])

    def test_existing_compatibility_and_brand_behavior_intact(self) -> None:
        # A genuine literal attribute (compatibility) plus a separate exclusion
        # both keep working after the fix: the Samsung cable is dropped by the
        # required «iPhone», the case is dropped by the excluded «case», only
        # the real charger survives. Uses a same-script (latin) set so the
        # cross-language guard does not step in - this asserts the fix touched
        # nothing but negation phrases.
        request = SearchRequest(
            original_query="charger for iPhone 15 Pro, not a case",
            normalized_query="iPhone 15 Pro charger",
            keywords=["charger"],
            required_features=["iPhone"],
            excluded_features=["case"],
        )
        products = [
            make_product("iPhone 15 Pro Charger 20W Fast"),
            make_product("iPhone 15 Pro Case Cover Silicone"),
            make_product("USB-C Cable for Samsung Galaxy"),
        ]
        kept = filter_products(products, request)
        self.assertEqual([p.title for p in kept], ["iPhone 15 Pro Charger 20W Fast"])



if __name__ == "__main__":
    unittest.main()

