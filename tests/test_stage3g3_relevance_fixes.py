# -*- coding: utf-8 -*-
"""Stage 3G.3 - spec/hyphen evidence must not replace the product noun.

The Stage 3G.2 replay showed the cost of the Stage-3 derived-adjective
echo: a title that merely HAS a feature («с быстрой зарядкой»,
«Bluetooth-колонкой») or SUPPORTS a charger («кейс …: поддержка …
зарядного устройства 65W») cleared the noun gate purely because the
request's numeric spec («65W», «20W») or the hyphenated component
coincided. Real false positives from the caches are pinned below, next
to the legitimate products that must survive. Every test asserts the
FINAL gate outcome, never an intermediate helper result - except the
one unit test pair that pins WHAT may count as a type-position echo.
Offline by construction: the requests come from the real deterministic
parser (no Gemini, no network), the products are plain listings.
"""

from __future__ import annotations

import unittest

from services.ai_service import AIService
from services.relevance import (
    _type_position_derived_words,
    noun_terms,
    rejection_reason,
    relevance_tier,
)
from tests.test_search_intent import make_listing
from tests.test_stage3g1_relevance_fixes import (
    TITLE_ADAPTER,
    TITLE_OEM,
    TITLE_PD3,
    d4_request,
)

D4_ORIG = "зарядка 65W до 1000 грн"  # (same string the 3G.1 fixtures use)
C4_ORIG = "bluetooth колонка 20W"
B4_ORIG = "аккумулятор Makita 18V"

#: Real AliExpress titles recorded by the benchmark caches - each one a
#: Stage-3G.2 false positive that passed only via spec/hyphen evidence.
TITLE_DJI_CASE = (
    "Портативный водонепроницаемый кейс для DJI NEO Fly More Combo: "
    "поддержка аккумуляторов, пультов RC 2/RC-N3, зарядного хаба и "
    "зарядного устройства 65W"
)
TITLE_GAMING_CHAIR = (
    "Игровое кресло с Bluetooth-колонкой, LED-дисплеем, массажем и "
    "откидной спинкой, кресло для киберспорта"
)
TITLE_THERMOBOX = (
    "Портативный ротоформованный термобокс 45 л со встроенной "
    "Bluetooth-колонкой для использования на улице, отлично подходит "
    "для еды, напитков, музыки, льда. Модель Pro."
)
TITLE_KARAOKE_MIC = (
    "Беспроводной караоке-микрофон G6 Pro для взрослых и детей: для "
    "пения, записи, подкастов, с Bluetooth-колонкой 20 Вт и функцией "
    "левитации"
)
TITLE_POWER_BANK_CASE = (
    "Корпус для Power Bank PD 65W DIY, на 8 аккумуляторов 18650, "
    "с быстрой зарядкой PD3.1 QC3.0 UFCS, регулируемым напряжением "
    "DC 3.3-24В, с LED-подсветкой, USB+Type-C."
)
TITLE_TESLA_HUB = (
    "Многопортовый автомобильный адаптер для центральной консоли, "
    "USB-хаб Type-C 65 Вт с быстрой зарядкой для Tesla Model Y Juniper "
    "2025, Model 3 Highland 2024-2026"
)
TITLE_POWERBANK_100_65 = (
    "Портативный внешний аккумулятор 20000 мАч 100 Вт/65 Вт с цифровым "
    "дисплеем, быстрой зарядкой и высокой мощностью"
)

#: Legitimate products that must survive the narrowing unchanged.
TITLE_BATTERY_TOOL = (
    "Аккумуляторная угловая шлифмашина Makita DGA404 100 мм, "
    "бесщеточная, литиевая, высокомощная, для резки и полировки"
)
TITLE_JIGSAW_DETACHED = (
    "Электрический бесщеточный лобзик Makita DJV182 340 Вт с пильным "
    "полотном, аккумуляторный, с рукояткой-тубусом, портативный, для "
    "плотницких работ"
)
TITLE_DRILL_BATTERY = "Дрель аккумуляторная Makita 18V набор"
TITLE_SPEAKER = "Bluetooth-колонка 20 Вт с LED-дисплеем, NFC"


def c4_request():
    """The LIVE deterministic parse of the audit's C4 query (offline)."""

    return AIService._fallback_parse(C4_ORIG)


def b4_request():
    """The LIVE deterministic parse of the audit's B4 query (offline)."""

    return AIService._fallback_parse(B4_ORIG)


class SpecOrHyphenEchoNoLongerAccepted(unittest.TestCase):
    """A feature mention plus a coinciding spec is NOT product identity."""

    def test_case_listing_supporting_a_charger_stays_rejected(self) -> None:
        """«поддержка … зарядного устройства 65W» is support, not identity."""

        case = make_listing("c", TITLE_DJI_CASE)

        self.assertEqual(
            rejection_reason(case, d4_request()),
            "no_product_noun_in_title",
        )
        self.assertEqual(relevance_tier(case, d4_request()), 0)

    def test_hyphenated_speaker_components_stay_rejected(self) -> None:
        """«Bluetooth-колонкой» inside a chair/thermobox/mic is a feature."""

        for product_id, title in (
            ("ch", TITLE_GAMING_CHAIR),
            ("tb", TITLE_THERMOBOX),
            ("mc", TITLE_KARAOKE_MIC),
        ):
            with self.subTest(title=title[:40]):
                product = make_listing(product_id, title)

                self.assertEqual(
                    rejection_reason(product, c4_request()),
                    "no_product_noun_in_title",
                )

    def test_charging_feature_phrases_stay_rejected(self) -> None:
        """«с быстрой зарядкой» never names the product, whatever corroborates."""

        for product_id, title in (
            ("cs", TITLE_POWER_BANK_CASE),
            ("hb", TITLE_TESLA_HUB),
            ("pb", TITLE_POWERBANK_100_65),
        ):
            with self.subTest(title=title[:40]):
                product = make_listing(product_id, title)

                self.assertEqual(
                    rejection_reason(product, d4_request()),
                    "no_product_noun_in_title",
                )


class GenuineProductsSurviveTheNarrowing(unittest.TestCase):
    """Every legitimate accept of the caches keeps its decision."""

    def test_hyphenated_charger_compounds_still_pass(self) -> None:
        """Stage 3G.1's adapter/OEM/PD3 compounds are type-position echoes."""

        for product_id, title in (
            ("a", TITLE_ADAPTER),
            ("o", TITLE_OEM),
            ("p", TITLE_PD3),
        ):
            with self.subTest(title=title[:40]):
                self.assertIsNone(
                    rejection_reason(
                        make_listing(product_id, title), d4_request()
                    )
                )

    def test_charger_head_noun_keeps_tier_one(self) -> None:
        """A leading «зарядное устройство» still earns the related tier."""

        charger = make_listing(
            "z", "Зарядное устройство 65W GaN для телефонов и ноутбуков"
        )

        self.assertIsNone(rejection_reason(charger, d4_request()))
        self.assertEqual(relevance_tier(charger, d4_request()), 1)

    def test_battery_tools_of_the_brand_still_pass(self) -> None:
        """«Аккумуляторная … шлифмашина» chains adjectives to a real noun."""

        tool = make_listing("t", TITLE_BATTERY_TOOL)

        self.assertIsNone(rejection_reason(tool, b4_request()))

    def test_detached_participial_attribute_still_passes(self) -> None:
        """«…, аккумуляторный, …» between commas is still the product."""

        jigsaw = make_listing("j", TITLE_JIGSAW_DETACHED)

        self.assertIsNone(rejection_reason(jigsaw, b4_request()))

    def test_documented_drill_battery_keep_is_retained(self) -> None:
        """The Stage-3 docstring example «Дрель аккумуляторная» is untouched."""

        drill = make_listing("d", TITLE_DRILL_BATTERY)

        self.assertIsNone(rejection_reason(drill, b4_request()))

    def test_speaker_head_noun_wins_over_hyphen_component(self) -> None:
        """A real колонка with a hyphenated component stays EXACT."""

        speaker = make_listing("s", TITLE_SPEAKER)

        self.assertIsNone(rejection_reason(speaker, c4_request()))
        self.assertEqual(relevance_tier(speaker, c4_request()), 2)


class ProtectedBehaviourTests(unittest.TestCase):
    """The junk the noun gate always rejected keeps its reason."""

    def test_bare_adjective_echo_stays_rejected(self) -> None:
        junk = make_listing("j", "Телескопическая штанга для фото")

        self.assertEqual(
            rejection_reason(junk, AIService._fallback_parse("телескоп")),
            "no_product_noun_in_title",
        )

    def test_unrelated_title_stays_rejected_by_r3(self) -> None:
        cross = make_listing("x", "Крест нательный православный из серебра")

        self.assertEqual(
            rejection_reason(cross, d4_request()),
            "no_query_term_in_title",
        )


class TypePositionEchoUnitTests(unittest.TestCase):
    """The ONE helper-level pin: WHAT may count as a type-position echo."""

    def test_only_name_phrase_echoes_are_returned(self) -> None:
        title_nouns = set(noun_terms(TITLE_ADAPTER, "cyrillic"))

        self.assertEqual(
            _type_position_derived_words(
                TITLE_ADAPTER.lower(), "заряд", "зарядка", title_nouns
            ),
            ["зарядное"],
        )

    def test_feature_echoes_are_never_returned(self) -> None:
        title_nouns = set(noun_terms(TITLE_POWER_BANK_CASE, "cyrillic"))

        self.assertEqual(
            _type_position_derived_words(
                TITLE_POWER_BANK_CASE.lower(), "заряд", "зарядка", title_nouns
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()