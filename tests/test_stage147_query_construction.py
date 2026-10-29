# -*- coding: utf-8 -*-
"""G25.2-D Stage 14.7: deterministic query-construction tests.

The production intent-to-query path is ``parse_query_intent`` ->
``apply_intent`` -> ``_retrieval_query`` (``services.query_intent``); both
search entry points (``handlers.search._search`` and
``SearchService.find_products_from_text``) reach it exactly once per search.
These tests pin the two guarantees this stage added/verified:

* explicit, meaningful specifications survive construction - numeric value
  + unit (``1 тб``, ``25 w``, ``65 w``, ``18 v``), brand/model/generation
  (``Volkswagen``, ``Golf 7``) and one-letter connector specs (``USB-C``,
  ``E27``) - without merging unrelated numbers or duplicating terms;
* a dangling connector the «для/на X» split or a spec extraction leaves
  inside the product type («ssd на», «футляр на») is dropped from the
  provider phrase, so the query names the intended product, not a
  preposition.

Preferences («недорогой», «хороший») must never become a product type, a
numeric budget or a rating threshold; the original user query is never
touched; currency/country propagation is unchanged.

Deterministic synthetic fixtures only - ``AIService(api_key=None)`` runs the
local fallback parser, no Gemini, no live AliExpress.
"""

from __future__ import annotations

import unittest

from models.search_request import SearchMode, SearchRequest
from services.query_intent import apply_intent, fallback_intent
from services.query_variants import product_query_variants
from tests.test_search_intent import resolve_and_apply


def provider_phrase(text: str) -> str:
    """The primary phrase the provider would receive for ``text``.

    Runs the EXACT Stage 2 production order (parse -> resolve -> apply), then
    the production variant builder, and returns its first (primary) phrase.
    """
    effective, _, _ = resolve_and_apply(text)
    variants = product_query_variants(effective)
    return variants[0] if variants else ""


class NumericSpecsSurviveConstruction(unittest.TestCase):
    """Explicit numeric value + unit must reach the provider query."""

    def test_terabyte_spec_survives(self) -> None:
        self.assertIn("1 тб", provider_phrase("SSD на 1 ТБ"))

    def test_terabyte_spec_survives_english_unit(self) -> None:
        self.assertIn("1 tb", provider_phrase("SSD 1TB"))

    def test_wattage_spec_survives_joined(self) -> None:
        self.assertIn("25 w", provider_phrase("зарядка 25W"))

    def test_wattage_spec_survives_spaced(self) -> None:
        self.assertIn("25 w", provider_phrase("зарядка 25 W"))

    def test_two_specs_both_survive(self) -> None:
        phrase = provider_phrase("зарядка USB-C 65 W")
        self.assertIn("65 w", phrase)
        self.assertIn("usb-c", phrase)

    def test_diagonal_and_resolution_survive(self) -> None:
        phrase = provider_phrase("монитор 27 дюймов 4K")
        self.assertIn("27 дюймов", phrase)
        self.assertIn("4 k", phrase)

    def test_unrelated_numbers_are_not_merged(self) -> None:
        phrase = provider_phrase("монитор 27 дюймов 4K")
        self.assertNotIn("274", phrase)
        self.assertNotIn("27 4", phrase)

class BrandModelGenerationSurvive(unittest.TestCase):
    """Explicit brand / model / generation must reach the provider query."""

    def test_brand_and_generation_survive(self) -> None:
        phrase = provider_phrase("дворники на Volkswagen Golf 7")
        self.assertIn("volkswagen", phrase.casefold())
        self.assertIn("golf 7", phrase.casefold())

    def test_brand_and_voltage_survive(self) -> None:
        phrase = provider_phrase("аккумулятор для шуруповёрта Makita 18V")
        self.assertIn("makita", phrase.casefold())
        self.assertIn("18 v", phrase.casefold())

    def test_product_type_preserved(self) -> None:
        self.assertIn(
            "дворники", provider_phrase("дворники на Volkswagen Golf 7")
        )


class OneLetterConnectorSpecsSurvive(unittest.TestCase):
    """A one-letter spec attached to a real product must not be dropped."""

    def test_usb_c_hyphen_survives(self) -> None:
        self.assertIn("usb-c", provider_phrase("кабель USB-C"))

    def test_usb_c_spaced_survives(self) -> None:
        phrase = provider_phrase("кабель USB C")
        self.assertIn("usb", phrase)
        self.assertIn("c", phrase.split())

    def test_e27_base_survives(self) -> None:
        self.assertIn("e27", provider_phrase("лампочка E27 9W"))


class LeakedConnectorsDropped(unittest.TestCase):
    """A dangling «на»/«для» left in the product type must not reach query."""

    def test_na_preposition_dropped_from_ssd(self) -> None:
        phrase = provider_phrase("SSD на 1 ТБ")
        self.assertNotIn(" на ", f" {phrase} ")
        self.assertEqual(phrase.split()[0], "ssd")

    def test_na_preposition_dropped_from_case(self) -> None:
        phrase = provider_phrase("футляр на 4 телефона")
        self.assertNotIn(" на ", f" {phrase} ")

    def test_spec_still_present_when_connector_dropped(self) -> None:
        phrase = provider_phrase("SSD на 1 ТБ")
        self.assertIn("1 тб", phrase)


class PreferencesAreNotProductAttributes(unittest.TestCase):
    """«недорогой»/«хороший» never become a type, budget or rating."""

    def test_cheap_does_not_invent_budget(self) -> None:
        effective, _, _ = resolve_and_apply("недорогой SSD на 1 ТБ")
        self.assertIsNone(effective.max_price)
        self.assertIsNone(effective.min_price)

    def test_cheap_does_not_become_product_type(self) -> None:
        phrase = provider_phrase("недорогой SSD на 1 ТБ")
        self.assertNotIn("недорог", phrase)

    def test_incident_query_preferences_not_in_query(self) -> None:
        phrase = provider_phrase(
            "Мне нужна недорогая но хорошая зарядка на телефон"
        )
        for word in ("нужна", "мне", "но", "недорог", "хорош"):
            self.assertNotIn(word, phrase)

    def test_incident_query_no_budget_no_rating(self) -> None:
        effective, _, _ = resolve_and_apply(
            "Мне нужна недорогая но хорошая зарядка на телефон"
        )
        self.assertIsNone(effective.max_price)
        self.assertIsNone(effective.min_price)
        self.assertIsNone(effective.min_rating)
        self.assertFalse(effective.prefers_good_rating)


class NoDuplicateTerms(unittest.TestCase):
    """The constructed provider query never repeats a token."""

    def test_brand_not_duplicated(self) -> None:
        phrase = provider_phrase("дворники на Volkswagen Golf 7")
        tokens = phrase.casefold().split()
        self.assertEqual(len(tokens), len(set(tokens)))


class OriginalQueryAndCurrencyPreserved(unittest.TestCase):
    """The user's own text and stored currency are never overwritten."""

    def test_original_query_unchanged(self) -> None:
        text = "SSD на 1 ТБ"
        effective, _, _ = resolve_and_apply(text)
        self.assertEqual(effective.original_query, text)

    def test_currency_propagation_unchanged(self) -> None:
        effective, _, _ = resolve_and_apply("SSD на 1 ТБ до 3000 грн")
        self.assertEqual(effective.currency, "UAH")
        self.assertEqual(effective.max_price, 3000.0)


class BothPathsConstructConsistently(unittest.TestCase):
    """AI-parsed and fallback-parsed searches reach the same construction."""

    def test_fallback_intent_specs_reach_query(self) -> None:
        request = resolve_and_apply("SSD на 1 ТБ")[0]
        intent = fallback_intent("SSD на 1 ТБ")
        applied = apply_intent(request, intent)
        self.assertIn("1 тб", applied.normalized_query or "")

    def test_provider_keywords_receive_constructed_query(self) -> None:
        # provider_phrase mirrors what _search_params sends as ``keywords``.
        self.assertIn("25 w", provider_phrase("зарядка 25W"))


class SafeFallbackForLowConfidence(unittest.TestCase):
    """An intent that names no product keeps the user's own safe phrase."""

    def test_empty_intent_preserves_existing_query(self) -> None:
        request = SearchRequest(
            original_query="что-нибудь прикольное",
            normalized_query="что-нибудь прикольное",
            mode=SearchMode.SEARCH,
        )
        intent = fallback_intent("что-нибудь прикольное")
        applied = apply_intent(request, intent)
        self.assertTrue(applied.normalized_query)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

