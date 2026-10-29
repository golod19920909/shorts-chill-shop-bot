# -*- coding: utf-8 -*-
"""Stage 3G.1 - targeted fixes for the Stage 3G audit's false negatives.

Real titles and query shapes come from the audit caches (tmp_g251 /
tmp_g252b, product ids preserved). Every test asserts the FINAL gate
outcome (``rejection_reason`` / ``filter_relevant`` / ``relevance_tier``),
never an intermediate helper result - except the one extraction unit test
that pins WHAT may count as spec evidence at all. Offline by construction:
the requests come from the real deterministic parser (no Gemini, no
network), the products are plain listings.
"""

from __future__ import annotations

import unittest

from models.product import Marketplace, Product
from models.search_request import SearchRequest
from services.ai_service import AIService
from services.relevance import (
    _spec_phrases,
    comparable_terms,
    filter_relevant,
    query_noun_terms,
    query_terms,
    rejection_reason,
    relevance_tier,
)
from tests.test_search_intent import make_listing, resolve_and_apply

D4_ORIG = "зарядка 65W до 1000 грн"

#: Real AliExpress titles recorded by the benchmark caches.
TITLE_ADAPTER = (
    "Оригинальный адаптер-зарядное устройство 65 Вт и кабель USB-C "
    "для ASUS ROG 9 8 7 7S 6 6 Pro 5 5S"
)
TITLE_OEM = (
    "Новый оригинальный OEM-зарядное устройство для Acer 65 Вт TravelMate "
    "P6 TMP614P-52 ADP-65KE B Блок питания для ноутбука 20 В 3,25 А USB-C "
    "Тип C Адаптер переменного тока"
)
TITLE_PD3 = (
    "Зарядное устройство PD3.0 65W GaN с быстрой зарядкой, двойным USB-A "
    "и Type-C, 15A, для телефонов, ноутбуков и планшетов (американская розетка)"
)
TITLE_HP = (
    "Зарядное устройство USB-C AC DC мощностью 65 Вт для HP 20V 3.25A "
    "Type C TPN-LA23 TPN-HA01 L65505-008 L6744-001 TPN-CA06"
)
TITLE_LENOVO = (
    "Зарядное устройство для ноутбука USB-C мощностью 65 Вт, адаптер "
    "быстрой зарядки Type C для Lenovo ThinkPad, HP, Dell, ASUS, Acer Yoga, "
    "Chromebook, блок питания для ноутбуков."
)


def make_product(
    title: str,
    product_id: str = "1",
    price: float = 10.0,
) -> Product:
    return Product(
        marketplace=Marketplace.ALIEXPRESS,
        product_id=product_id,
        title=title,
        product_url=f"https://www.aliexpress.com/item/{product_id}.html",
        price=price,
    )


def request(query: str, **overrides) -> SearchRequest:
    data = {"original_query": query, "normalized_query": query}
    data.update(overrides)
    return SearchRequest(**data)


def d4_request() -> SearchRequest:
    """The LIVE deterministic parse of the audit's D4 query (offline)."""

    return AIService._fallback_parse(D4_ORIG)


class AlphanumericSpecCorroborationTests(unittest.TestCase):
    """Stage 3G.1 (A): «65W»/«65 Вт» survive as corroboration evidence."""

    def test_real_65w_charger_titles_are_accepted(self) -> None:
        r = d4_request()
        cases = (
            (TITLE_ADAPTER, "1005012020797681"),
            (TITLE_OEM, "1005008889962698"),
            (TITLE_PD3, "p3"),
            (TITLE_HP, "p4"),
            (TITLE_LENOVO, "p5"),
        )
        for title, pid in cases:
            with self.subTest(title=title[:40]):
                product = make_product(title, pid)
                self.assertIsNone(rejection_reason(product, r))
                # The rescue is RELATED evidence, never a false EXACT.
                self.assertEqual(relevance_tier(product, r), 1)

    def test_glued_and_spaced_wattage_write_the_same_spec(self) -> None:
        r = request(D4_ORIG)

        self.assertEqual(_spec_phrases(r), ("65w",))
        for title in (TITLE_PD3, TITLE_HP):
            with self.subTest(title=title[:40]):
                self.assertIsNone(rejection_reason(make_product(title), r))

    def test_usb_c_charger_without_the_usb_word_is_accepted(self) -> None:
        r = request("зарядка USB C 65W")
        without_usb = make_product(
            "Быстрое зарядное устройство Type-C 65W PD 3.0 для телефона"
        )
        with_usb = make_product(
            "Быстрое зарядное устройство USB Type-C 65W PD для телефона"
        )

        self.assertIsNone(rejection_reason(without_usb, r))
        self.assertIsNone(rejection_reason(with_usb, r))
        self.assertEqual(relevance_tier(without_usb, r), 1)

    def test_a_different_wattage_never_corroborates(self) -> None:
        r = d4_request()
        wrong = make_product(
            "Зарядное устройство PD 265W GaN быстрое для ноутбука Type-C"
        )

        self.assertEqual(
            rejection_reason(wrong, r), "no_product_noun_in_title"
        )

    def test_a_shared_bare_number_never_becomes_relevance(self) -> None:
        """The noun echo + the shared «1000» (the budget) prove nothing."""
        r = d4_request()
        numeric_only = make_product("Зарядный тестер аккумуляторов 1000 циклов")

        self.assertEqual(
            rejection_reason(numeric_only, r), "no_product_noun_in_title"
        )

    def test_the_budget_amount_is_never_a_spec(self) -> None:
        self.assertNotIn("1000", " ".join(_spec_phrases(d4_request())))


class HyphenatedCompoundTests(unittest.TestCase):
    """Stage 3G.1 (B): a hyphen-joined component still needs corroboration."""

    def test_adapter_and_oem_compounds_are_accepted(self) -> None:
        r = d4_request()

        for title, pid in ((TITLE_ADAPTER, "1005012020797681"),
                           (TITLE_OEM, "1005008889962698")):
            with self.subTest(pid=pid):
                product = make_product(title, pid)
                self.assertIsNone(rejection_reason(product, r))
                self.assertEqual(relevance_tier(product, r), 1)

    def test_a_compound_without_the_requested_spec_is_still_rejected(
        self,
    ) -> None:
        r = d4_request()
        wrong_wattage = make_product(
            "Оригинальный адаптер-зарядное устройство 20 Вт и кабель "
            "USB-C для телефонов"
        )

        self.assertEqual(
            rejection_reason(wrong_wattage, r), "no_product_noun_in_title"
        )

    def test_speaker_compound_titles_are_kept(self) -> None:
        cases = (
            (request("колонка bluetooth"),
             "Портативная Bluetooth-колонка ZEALOT-S49 с объемным стереозвуком "
             "360°, мощностью 20 Вт, водонепроницаемостью IPX6, беспроводным "
             "Bluetooth 5.2, для использования на открытом воздухе.", 2),
            (request("колонка"),
             "SONGX SONGMONSTER Милый робот-караоке-колонка Bluetooth "
             "с 2 беспроводными микрофонами, портативная домашняя мини-колонка "
             "KTV, HIFI стерео", 1),
            (request("колонка"),
             "Креативная колонка-наушник в форме гигантского уха, портативный "
             "беспроводной аудиоплеер, декор для рабочего стола, подарочная "
             "колонка для дома, общежития, вечеринок.", 2),
        )
        for r, title, tier in cases:
            with self.subTest(title=title[:40]):
                product = make_product(title)
                self.assertIsNone(rejection_reason(product, r))
                self.assertEqual(relevance_tier(product, r), tier)

    def test_a_word_boundary_still_guards_the_root(self) -> None:
        """«заряд» inside «Разрядка» is not the request's noun: the title
        shares no word with «зарядка 65W до 1000 грн» and stays rejected."""
        r = d4_request()
        keyboard = make_product("Разрядка подсветки для клавиатуры RGB")

        self.assertEqual(
            rejection_reason(keyboard, r), "no_query_term_in_title"
        )


class CurrencyEvidenceTests(unittest.TestCase):
    """Stage 3G.1 (C): a currency word is a price, never product evidence."""

    def test_currency_words_leave_the_query_terms_and_nouns(self) -> None:
        r = d4_request()

        self.assertEqual(query_terms(r), ("зарядка",))
        self.assertEqual(query_noun_terms(r, "cyrillic"), ("зарядка",))

        budget_only = SearchRequest(original_query="до 3000 грн")
        self.assertEqual(query_terms(budget_only), ())
        self.assertEqual(query_noun_terms(budget_only, "cyrillic"), ())

    def test_a_price_quoting_title_no_longer_rescues_an_unrelated_product(
        self,
    ) -> None:
        r = d4_request()
        case = make_product("Чехол для телефона 1000 грн новый")

        self.assertEqual(comparable_terms(case.title, r), ["зарядка"])
        self.assertEqual(
            rejection_reason(case, r), "no_query_term_in_title"
        )

    def test_budget_only_requests_keep_the_pool_unverified(self) -> None:
        """Without a product word the gate has nothing to judge and keeps
        everything (tier 0) - the currency word neither filters nor
        rescues anything anymore."""
        r = SearchRequest(original_query="до 3000 грн")
        headphones = make_product("Наушники беспроводные Bluetooth 5.3")
        case = make_product("Чехол для телефона силиконовый")

        kept, dropped = filter_relevant([headphones, case], r)
        self.assertEqual(len(kept), 2)
        self.assertEqual(dropped, {})
        self.assertEqual(relevance_tier(headphones, r), 0)

    def test_the_budget_parser_still_detects_the_amount_and_currency(
        self,
    ) -> None:
        """Price parsing and currency detection stay exactly as they were."""
        r = d4_request()

        self.assertEqual(r.max_price, 1000.0)
        self.assertEqual(r.currency, "UAH")
        self.assertEqual(r.original_query, D4_ORIG)


class ProtectedBehaviorTests(unittest.TestCase):
    """Every protection the Stage 3G audit listed must survive A-C."""

    def test_bare_adjective_echo_stays_rejected(self) -> None:
        junk = make_product("Телескопическая штанга для фото")

        self.assertEqual(
            rejection_reason(junk, request("телескоп")),
            "no_product_noun_in_title",
        )

    def test_unrelated_titles_stay_rejected(self) -> None:
        cross = make_product("Крест нательный православный из серебра")

        self.assertEqual(
            rejection_reason(cross, d4_request()),
            "no_query_term_in_title",
        )

    def test_spare_part_leak_stays_rejected(self) -> None:
        r = AIService._fallback_parse("шуруповерт аккумуляторный для дома")
        gearbox = make_product(
            "Редукторный механизм 125463-0 для аккумуляторной "
            "дрели-шуруповерта MAKITA"
        )
        complete = make_product("Дрель-шуруповёрт аккумуляторная 21V набор")

        self.assertEqual(
            rejection_reason(gearbox, r), "spare_part_not_requested_product"
        )
        self.assertEqual(relevance_tier(gearbox, r), 0)
        self.assertIsNone(rejection_reason(complete, r))

    def test_hard_exclusion_via_intent_stays_intact(self) -> None:
        request_, _, _ = resolve_and_apply("наушники без чехла")
        self.assertTrue(request_.intent_excluded_product_types)
        case = make_listing("c", "Чехол для наушников TWS силикон")
        headphones = make_listing("h", "Наушники TWS Bluetooth 5.3")
        cable = make_listing("k", "Кабель USB-C для наушников")

        self.assertEqual(
            rejection_reason(case, request_), "excluded_product_type"
        )
        kept, dropped = filter_relevant([case, headphones, cable], request_)
        self.assertEqual([p.product_id for p in kept], ["h", "k"])
        self.assertEqual(dropped, {"excluded_product_type": 1})

    def test_exact_identity_still_earns_tier_two(self) -> None:
        r = request("электрическая зубная щетка")
        exact = make_product("Электрическая зубная щетка Philips Sonicare")
        accessory = make_product("Футляр для электрической зубной щетки")

        self.assertIsNone(rejection_reason(exact, r))
        self.assertEqual(relevance_tier(exact, r), 2)
        # R5 keeps the related accessory, but never at the EXACT tier.
        self.assertIsNone(rejection_reason(accessory, r))
        self.assertEqual(relevance_tier(accessory, r), 1)

    def test_cross_script_noun_evidence_is_unchanged(self) -> None:
        nebulizer = make_product("Handheld Nebulizer Portable USB")

        self.assertIsNone(
            rejection_reason(nebulizer, request("небулайзер"))
        )


if __name__ == "__main__":  # pragma: no cover - direct execution helper
    unittest.main()