"""B2: feature matching, inflections, numeric equivalence, negation and
currency/budget handling.

All checks are OFFLINE and deterministic: they use the real parser
(``AIService._fallback_parse``), the real conversation merge, the real
feature matcher and the real price filter. Never Gemini, never the network.
"""
from __future__ import annotations

import asyncio
import unittest

from models.product import Marketplace, Product
from models.search_request import SearchRequest
from services.ai_service import AIService
from services.comparison_service import filter_products
from services.conversation import (
    _clean_feature,
    _extract_features,
    deterministic_merge,
    is_refinement,
    resolve_request,
)
from services.text_matching import phrase_in_text


def run(coro):
    return asyncio.run(coro)


def parse(text: str) -> SearchRequest:
    """The real deterministic parser (the one used without Gemini)."""
    return run(AIService(api_key=None).parse_query(text))


def product(title: str, pid: str = "1", price: float = 10.0, currency=None) -> Product:
    return Product(
        marketplace=Marketplace.ALIEXPRESS,
        product_id=pid,
        title=title,
        product_url=f"https://example.com/{pid}",
        price=price,
        currency=currency,
    )


# --- 1. Feature fragment cleanup ------------------------------------------


class FeatureFragmentCleanupTests(unittest.TestCase):
    """Dangling prepositions are stripped; complete requirements survive."""
    def test_only_a_preposition_is_not_a_feature(self):
        self.assertIsNone(_clean_feature("из"))
        self.assertIsNone(_clean_feature("с"))

    def test_bare_digits_and_stopwords_still_rejected(self):
        for feature in ("5", "100", "хорошим рейтингом", "бюджет"):
            with self.subTest(feature=feature):
                self.assertIsNone(_clean_feature(feature))

    def test_extract_features_drops_the_dangling_fragment(self):
        # User: "ремень с пряжкой из металла". The capture is "пряжкой из",
        # which must become "пряжкой", not a dangling «из» fragment, and
        # "металла" falls outside the two-word capture.
        required, excluded = _extract_features("ремень с пряжкой из металла")
        self.assertEqual(required, ["пряжкой"])
        self.assertEqual(excluded, [])

    def test_excluded_feature_also_cleaned(self):
        required, excluded = _extract_features("ремень без пряжкой из")
        self.assertEqual(excluded, ["пряжкой"])


# --- 2. Inflection and numeric equivalence --------------------------------


class InflectionAndNumericEquivalenceTests(unittest.TestCase):
    """Equivalent Russian forms and equivalent numeric expressions match;
    unrelated words and different numbers do not."""

    def test_number_word_matches_arabic_numeral_both_directions(self):
        # «с двумя аккумуляторами» matches title «+ 2 аккумулятора».
        self.assertTrue(
            phrase_in_text("+ 2 аккумулятора", "двумя аккумуляторами")
        )
        self.assertTrue(
            phrase_in_text("2 аккумулятора", "двумя аккумуляторами")
        )
        self.assertTrue(
            phrase_in_text("двумя аккумуляторами", "2 аккумулятора")
        )

    def test_inflection_still_works(self):
        self.assertTrue(phrase_in_text("аккумулятор", "аккумуляторами"))
        self.assertTrue(phrase_in_text("шумоподавление", "шумоподавления"))

    def test_different_numbers_do_not_match(self):
        self.assertFalse(
            phrase_in_text("2 аккумулятора", "тремя аккумуляторами")
        )
        self.assertFalse(phrase_in_text("20001 мАч", "20000 мАч"))

    def test_number_word_does_not_match_an_unrelated_word(self):
        self.assertFalse(
            phrase_in_text("несколько аккумулятора", "двумя аккумуляторами")
        )
        self.assertFalse(
            phrase_in_text("двумя аккумуляторами", "много аккумулятора")
        )

    def test_word_boundary_protection_intact(self):
        # «без провода» must not match merely because a title contains
        # «беспроводной» / «беспроводные».
        self.assertFalse(phrase_in_text("беспроводной", "проводная"))
        self.assertFalse(phrase_in_text("беспроводные", "проводные"))
        # a genuinely wired product still matches.
        self.assertTrue(phrase_in_text("проводные наушники", "проводная"))


# --- 3. Negation and explicit exclusions ---------------------------------


class NegationAndExclusionTests(unittest.TestCase):
    """Negative requirements become exclusions (never positive required
    features), and the previous product is preserved mid-conversation."""

    def setUp(self):
        self.previous = parse("шуруповёрт")

    def test_negation_becomes_exclusion_not_required_feature(self):
        cases = [
            ("только не ударную", ["ударную"]),
            ("но не ударный", ["ударный"]),
            ("без чехла", ["чехла"]),
            ("без провода", ["провода"]),
        ]
        for text, expected_excluded in cases:
            with self.subTest(text=text):
                required, excluded = _extract_features(text)
                self.assertEqual(required, [])
                self.assertEqual(excluded, expected_excluded)

    def test_word_containing_не_is_not_an_exclusion(self):
        # «недорогой» merely contains «не»; it is not a standalone negation.
        required, excluded = _extract_features("недорогой шуруповерт")
        self.assertEqual(required, [])
        self.assertEqual(excluded, [])

    def test_totally_not_impact_refines_and_excludes_impact(self):
        merged, refined = deterministic_merge(
            self.previous,
            parse("только не ударную"),
            "только не ударную",
        )
        self.assertTrue(refined)
        self.assertIn("ударную", merged.excluded_features)
        # The previous product identity is preserved, not replaced.
        self.assertIn("шурупов", (merged.normalized_query or "").lower())
        self.assertTrue(
            is_refinement(
                self.previous, parse("только не ударную"), "только не ударную"
            )
        )

    def test_exclusion_filters_impact_drill_keeps_plain_drill(self):
        request = SearchRequest(
            original_query="шуруповёрт",
            normalized_query="шуруповёрт",
            excluded_features=["ударная"],
        )
        kept = filter_products(
            [
                product("Ударный шуруповёрт 18В", "1", currency="USD"),
                product("Шуруповёрт сетевой", "2", currency="USD"),
            ],
            request,
        )
        self.assertEqual([p.product_id for p in kept], ["2"])

    def test_excluded_провода_does_not_match_беспроводной(self):
        request = SearchRequest(
            original_query="наушники без провода",
            normalized_query="наушники",
            excluded_features=["провода"],
        )
        kept = filter_products(
            [
                product("Беспроводные наушники", "1", currency="USD"),
                product("Наушники с проводом", "2", currency="USD"),
            ],
            request,
        )
        self.assertEqual([p.product_id for p in kept], ["1"])

    def test_existing_accessory_exclusion_preserved(self):
        # An explicitly unwanted accessory is rejected; a product that
        # merely contains «шумоподавление» (which shares the stem) survives
        # because the exclusion «чехла» is not present in its title.
        request = SearchRequest(
            original_query="наушники без чехла",
            normalized_query="наушники",
            excluded_features=["чехла"],
        )
        kept = filter_products(
            [
                product("Наушники с чехлом в комплекте", "1", currency="USD"),
                product("Наушники с шумоподавлением", "2", currency="USD"),
            ],
            request,
        )
        self.assertEqual([p.product_id for p in kept], ["2"])


# --- 4. Currency and budget -----------------------------------------------


class CurrencyAndBudgetTests(unittest.TestCase):
    """The parsed amount and currency stay associated through parsing and
    merging, and incompatible currencies are never silently compared as USD."""

    def test_budget_parses_amount_and_currency(self):
        cases = [
            ("до 40 долларов", 40.0, "USD"),
            ("до 1500 грн", 1500.0, "UAH"),
            ("до 5000 рублей", 5000.0, "RUB"),
            ("до 20 евро", 20.0, "EUR"),
            ("до 100 юаней", 100.0, "CNY"),
            ("до 30 $", 30.0, "USD"),
        ]
        for text, amount, currency in cases:
            with self.subTest(text=text):
                req = parse(text)
                self.assertEqual(req.max_price, amount)
                self.assertEqual(req.currency, currency)

    def test_currency_stays_associated_through_merge(self):
        previous = parse("наушники до 30 долларов")  # max 30, USD
        follow_up = parse("а до 40 долларов")  # max 40, USD
        merged, refined = deterministic_merge(
            previous, follow_up, "а до 40 долларов"
        )
        self.assertTrue(refined)
        self.assertEqual(merged.max_price, 40.0)
        self.assertEqual(merged.currency, "USD")

    def test_currency_changes_with_follow_up_budget(self):
        previous = parse("наушники до 30 долларов")  # USD
        follow_up = parse("а до 1500 грн")  # UAH
        merged, refined = deterministic_merge(
            previous, follow_up, "а до 1500 грн"
        )
        self.assertTrue(refined)
        self.assertEqual(merged.max_price, 1500.0)
        self.assertEqual(merged.currency, "UAH")

    def test_usd_budget_compares_usd_product(self):
        request = SearchRequest(
            original_query="наушники до 40 долларов",
            normalized_query="наушники",
            max_price=40.0,
            currency="USD",
        )
        kept = filter_products(
            [
                product("Наушники дешевые", "1", price=30.0, currency="USD"),
                product("Наушники дорогие", "2", price=99.0, currency="USD"),
            ],
            request,
        )
        self.assertEqual([p.product_id for p in kept], ["1"])

    def test_non_usd_budget_is_not_silently_compared_as_usd(self):
        # A RUB budget must not be compared against a USD price as if it
        # were USD; no conversion and no invented rate, so the product is
        # honestly dropped rather than mismatched.
        request = SearchRequest(
            original_query="наушники до 5000 рублей",
            normalized_query="наушники",
            max_price=5000.0,
            currency="RUB",
        )
        kept = filter_products(
            [
                product("Наушники Bluetooth", "1", price=100.0, currency="USD"),
            ],
            request,
        )
        self.assertEqual(kept, [])


# --- 5. B1 follow-up preserved + numeric-spec / word-boundary protections --


class B1AndProtectionsTests(unittest.TestCase):
    """B1 conversation behaviour and earlier protections stay intact."""

    def test_b1_followup_still_refines(self):
        previous = parse("Нужны беспроводные наушники до $30")
        merged, refined = run(
            resolve_request(
                previous,
                parse("а с хорошим микрофоном"),
                "а с хорошим микрофоном",
            )
        )
        self.assertTrue(refined)
        self.assertEqual(merged.max_price, 30.0)
        self.assertEqual(merged.required_features, ["хорошим микрофоном"])

    def test_product_switch_still_resets(self):
        previous = parse("шуруповёрт")
        _, refined = run(
            resolve_request(previous, parse("плазморезка"), "плазморезка")
        )
        self.assertFalse(refined)

    def test_numeric_spec_and_word_boundary_protections_intact(self):
        for feature in ("65W", "12V", "20000mAh", "USB-C"):
            with self.subTest(feature=feature):
                self.assertEqual(_clean_feature(feature), feature)
        self.assertIsNone(_clean_feature("5"))
        self.assertIsNone(_clean_feature("100"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
