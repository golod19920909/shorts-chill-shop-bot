# -*- coding: utf-8 -*-
"""G25.2-D Stage 16 — regression tests for query-variant defects A-1..A-5.

Built from the EXACT Stage 14.9 evidence (tmp_g252d_stage149_matrix rows
#12, #18, #21, #52, #34, #40, #24, #33, #09, #41, #51) plus boundary cases.
Deterministic synthetic fixtures only — AIService(api_key=None) runs the local
fallback parser; no Gemini, no live AliExpress.
"""
from __future__ import annotations

import unittest

from services.query_intent import fallback_intent
from services.query_variants import product_query_variants
from tests.test_search_intent import resolve_and_apply


def variants_for(text: str) -> list[str]:
    effective, _, _ = resolve_and_apply(text)
    return product_query_variants(effective)


def intent_for(text: str) -> object:
    effective, _, _ = resolve_and_apply(text)
    return effective


class A1PreferenceWordsNeverLeakIntoVariants(unittest.TestCase):
    """«дешевую» (Stage 14.9 #12) must not become a product term."""

    def test_exact_stage149_example(self) -> None:
        variants = variants_for("Хочу купить дешевую мышку для ноутбука")
        for phrase in variants:
            self.assertNotIn("дешев", phrase.casefold())

    def test_product_and_compatibility_survive(self) -> None:
        variants = variants_for("Хочу купить дешевую мышку для ноутбука")
        self.assertIn("мышку", variants[0])
        self.assertIn("ноутбука", variants[0])

    def test_no_budget_or_rating_invented(self) -> None:
        request = intent_for("Хочу купить дешевую мышку для ноутбука")
        self.assertIsNone(request.max_price)
        self.assertIsNone(request.min_price)
        self.assertIsNone(request.min_rating)
        self.assertFalse(request.prefers_good_rating)

    def test_existing_preference_forms_still_stripped(self) -> None:
        for text in ("дешевый ноутбук", "дешевая зарядка", "дешевле наушники"):
            with self.subTest(text=text):
                for phrase in variants_for(text):
                    self.assertNotIn("дешев", phrase.casefold())

    def test_no_over_removal_of_unlisted_quality_words(self) -> None:
        # «дорогой» is NOT in the established preference rules — it must
        # stay (this stage must not invent a broader preference list).
        variants = variants_for("Дорогой подарок для жены")
        self.assertIn("дорогой", variants[0])

    def test_original_query_never_touched(self) -> None:
        text = "Хочу купить дешевую мышку для ноутбука"
        self.assertEqual(intent_for(text).original_query, text)

    def test_variant_stays_informative(self) -> None:
        # Removing the preference must not collapse the product meaning.
        variants = variants_for("Хочу купить дешевую мышку для ноутбука")
        self.assertTrue(all(v.strip() for v in variants))
        self.assertTrue(any("мышку" in v for v in variants))


class A2LeadingDlyaIsStrippedWhenRedundant(unittest.TestCase):
    """«Для Toyota Camry 70» (Stage 14.9 #18) must not start with «для»."""

    def test_exact_stage149_example(self) -> None:
        variants = variants_for("Для Toyota Camry 70")
        self.assertFalse(variants[0].casefold().startswith("для"))

    def test_compatibility_target_survives(self) -> None:
        phrase = variants_for("Для Toyota Camry 70")[0].casefold()
        for token in ("toyota", "camry", "70"):
            self.assertIn(token, phrase)

    def test_meaningful_dlya_phrases_keep_compatibility(self) -> None:
        cases = {
            "зарядка для телефона": "телефон",
            "фильтр для пылесоса Dyson": "пылесоса",
            "Чехол для iPhone 15": "iphone",
            "Набор для чистки мебели": "мебели",
        }
        for text, compat_token in cases.items():
            with self.subTest(text=text):
                variants = variants_for(text)
                self.assertTrue(variants)
                self.assertIn(compat_token, variants[0].casefold())

    def test_no_product_invented_for_product_less_query(self) -> None:
        self.assertIsNone(intent_for("Для Toyota Camry 70").intent_product_type)


class A3NegationNeverBecomesPositive(unittest.TestCase):
    """«без X» (Stage 14.9 #21, #52) must not invert into a positive variant."""

    def test_exact_stage149_examples(self) -> None:
        for text, excluded in (
            ("Пылесос без мешка", "мешка"),
            ("Шуруповёрт без аккумулятора", "аккумулятора"),
        ):
            with self.subTest(text=text):
                variants = variants_for(text)
                request = intent_for(text)
                self.assertTrue(request.intent_excluded_product_types)
                stem = excluded[:4]
                for phrase in variants[1:]:  # every DERIVED variant
                    self.assertNotIn(
                        stem, phrase.casefold(),
                        f"excluded word in variant {phrase!r}")

    def test_primary_retains_the_negation(self) -> None:
        self.assertEqual(intent_for("Пылесос без мешка").intent_excluded_product_types, ["мешка"])

    def test_safe_variant_may_still_exist(self) -> None:
        # The excluded noun may appear in the PRIMARY only as part of the
        # negation «без мешка»; no DERIVED variant may carry it positively.
        for phrase in variants_for("Пылесос без мешка для дома"):
            words = phrase.casefold().split()
            for i, word in enumerate(words):
                if word.startswith("мешк"):
                    self.assertTrue(
                        i > 0 and words[i - 1] == "без",
                        f"positive «мешка» in variant {phrase!r}")

    def test_bez_is_not_a_negation_everywhere(self) -> None:
        # «беспроводные» is a single token — never a negation.
        request = intent_for("Беспроводные наушники")
        self.assertEqual(request.intent_excluded_product_types, [])
        self.assertIn("беспроводные", variants_for("Беспроводные наушники")[0])

    def test_excluded_types_preserved_on_request(self) -> None:
        request = intent_for("Шуруповёрт без аккумулятора")
        self.assertIn("аккумулятора", request.intent_excluded_product_types)

    def test_exclusion_from_testsuite_example(self) -> None:
        # «наушники без чехла» (tests/test_intent_relevance.py): the case
        # must never become a positive «наушники чехла» variant.
        variants = variants_for("наушники без чехла")
        self.assertEqual(variants[0], "наушники")
        for phrase in variants[1:]:
            self.assertNotIn("чехл", phrase.casefold())


class A4MissingUnitsAreExtracted(unittest.TestCase):
    """Stage 14.9 #34/#40/#24/#33/#09/#41: kg/liters/mm/GB/m/размер."""

    def test_each_missing_unit_keeps_value_and_unit(self) -> None:
        cases = {
            "Гантели 10 кг": "10 кг",
            "Аквариум 50 литров": "50 литров",
            "Шланг для полива 20 метров": "20 метров",
            "Коврик для йоги 6 мм": "6 мм",
            "Оперативная память 16 ГБ": "16 гб",
            "Кроссовки Nike 43 размер": "43 размер",
        }
        for text, spec in cases.items():
            with self.subTest(text=text):
                self.assertIn(spec, fallback_intent(text).required_attributes)
                self.assertIn(spec, intent_for(text).intent_required_attributes)

    def test_non_unit_tokens_are_not_eaten(self) -> None:
        guards = {
            "Лампочка E27 9W": ("лампочка e27", ["9 w"]),
            "Монитор 27 дюймов 4K": ("монитор", ["27 дюймов", "4 k"]),
            "Видеокарта RTX 4060": ("видеокарта rtx", ["4060"]),
            "SSD на 1 ТБ": ("ssd", ["1 тб"]),
            "Повербанк 20000 мАч 65W": ("повербанк", ["20000 мач", "65 w"]),
            "Аккумулятор Makita 18V": ("аккумулятор", ["18 v"]),
            "Перфоратор Bosch 800W": ("перфоратор", ["800 w"]),
        }
        for text, (pt_contains, specs) in guards.items():
            with self.subTest(text=text):
                intent = fallback_intent(text)
                self.assertIn(pt_contains, intent.product_type or "")
                for spec in specs:
                    self.assertIn(spec, intent.required_attributes)

    def test_golf_generation_number_untouched(self) -> None:
        intent = fallback_intent("Дворники на Volkswagen Golf 7")
        self.assertIn("volkswagen golf 7", intent.compatibility)
        self.assertNotIn("7", intent.required_attributes)

    def test_spacing_variations(self) -> None:
        for text in ("гантели 10кг", "гантели 10 кг"):
            with self.subTest(text=text):
                self.assertIn("10 кг",
                              fallback_intent(text).required_attributes)



class A5NibudIsNeverAProductNoun(unittest.TestCase):
    """«нибудь» (Stage 14.9 #51) must not become a product noun."""

    def test_exact_stage149_example(self) -> None:
        # The malformed variant «нибудь машины» (a fragment of «что-нибудь»
        # misread as a product noun) must be omitted; the primary keeps the
        # user's own words.
        variants = variants_for("Что-нибудь прикольное для машины")
        self.assertNotIn("нибудь машины", variants)
        for phrase in variants[1:]:
            self.assertNotIn("нибудь", phrase.casefold())

    def test_meaningful_nouns_are_preserved(self) -> None:
        # Other nouns in the same request must still form variants.
        cases = {
            "Набор отвёрток": "набор",
            "Дворники на Volkswagen Golf 7": "дворники",
            "Коврик для йоги 6 мм": "коврик",
        }
        for text, noun in cases.items():
            with self.subTest(text=text):
                variants = variants_for(text)
                self.assertTrue(any(noun in v for v in variants))

    def test_deterministic_fallback(self) -> None:
        first = variants_for("Что-нибудь прикольное для машины")
        second = variants_for("Что-нибудь прикольное для машины")
        self.assertEqual(first, second)


class VariantHygieneAndPreservedBehavior(unittest.TestCase):
    """Cross-cutting safety + preservation of existing stages."""

    ALL = [
        "Хочу купить дешевую мышку для ноутбука",
        "Для Toyota Camry 70",
        "Пылесос без мешка",
        "Шуруповёрт без аккумулятора",
        "Гантели 10 кг",
        "Аквариум 50 литров",
        "Шланг для полива 20 метров",
        "Коврик для йоги 6 мм",
        "Оперативная память 16 ГБ",
        "Кроссовки Nike 43 размер",
        "Что-нибудь прикольное для машины",
        "Мне нужна недорогая, но хорошая зарядка на телефон",
        "SSD на 1 ТБ",
        "Видеокарта RTX 4060",
        "Набор для чистки мебели",
        "Лампочка E27 9W",
        "Монитор 27 дюймов 4K",
    ]

    def test_no_empty_or_duplicate_variants(self) -> None:
        for text in self.ALL:
            with self.subTest(text=text):
                variants = variants_for(text)
                self.assertTrue(variants)
                self.assertTrue(all(v.strip() for v in variants))
                folded = [v.casefold() for v in variants]
                self.assertEqual(len(folded), len(set(folded)))

    def test_variant_count_within_policy(self) -> None:
        for text in self.ALL:
            with self.subTest(text=text):
                self.assertLessEqual(len(variants_for(text)), 3)

    def test_brand_model_compatibility_preserved(self) -> None:
        cases = {
            "Дворники на Volkswagen Golf 7": (
                "volkswagen", ["volkswagen golf 7"]),
            "Чехол для iPhone 15": ("apple", ["iphone 15"]),
            "Аккумулятор Makita 18V": ("makita", ["makita"]),
            "Видеорегистратор для Toyota Camry 70": (
                "toyota", ["toyota camry 70"]),
        }
        for text, (brand, compat) in cases.items():
            with self.subTest(text=text):
                request = intent_for(text)
                self.assertEqual(
                    (request.intent_brand or "").casefold(), brand)
                self.assertEqual(request.intent_compatibility, compat)

    def test_stage147_construction_unchanged(self) -> None:
        # The exact Stage-14.7 pinned phrases must not move.
        expected = {
            "SSD на 1 ТБ": ["ssd 1 тб"],
            "зарядка 25W": ["25 w"],
            "Мне нужна недорогая, но хорошая зарядка на телефон":
                ["зарядка телефон"],
        }
        for text, needles in expected.items():
            with self.subTest(text=text):
                phrase = variants_for(text)[0]
                for needle in needles:
                    self.assertIn(needle, phrase)

    def test_searcher_mode_only_broadening(self) -> None:
        # A non-SEARCH request keeps exactly one phrase (G24.8 isolation).
        from models.search_request import SearchMode
        from services.ai_service import AIService

        import asyncio

        async def _run() -> list[str]:
            ai = AIService(api_key=None)
            parsed = await ai.parse_query("пылесос")
            request = parsed.model_copy(update={"mode": SearchMode.CATEGORY})
            return product_query_variants(request)

        self.assertEqual(len(asyncio.run(_run())), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

