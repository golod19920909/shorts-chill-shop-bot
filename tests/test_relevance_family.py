"""G24.6: family-aware relevance regression tests.

Contract proven here (services/relevance.py docstring R3/R5/R6):

* exact product matches - tier 2, always ranked first;
* related / same family / same use case / accessory results - KEPT at
  tier 1, never above exact matches;
* modifier-only overlap and clearly unrelated titles - still rejected;
* cross-language (English query vs Russian titles) results are judged in
  the romanized space; unverifiable ones are kept at tier 0, ranked last
  and logged - never a silent bypass;
* no product-specific synonyms or special cases: the transliteration is
  an alphabet table, and the gate behaves the same for invented words.
"""

from __future__ import annotations

import unittest

from models.product import Marketplace, Product
from models.search_request import SearchRequest
from services.comparison_service import sort_products
from services.relevance import (
    _CYR_TO_LAT,
    classify_product,
    filter_relevant,
    rejection_reason,
    relevance_tier,
)


def product(title: str, pid: str = "1", *, price: float = 10.0, orders: int = 10,
            url: str | None = None) -> Product:
    return Product(
        marketplace=Marketplace.ALIEXPRESS,
        product_id=pid,
        title=title,
        product_url=url
        if url is not None
        else f"https://www.aliexpress.com/item/{pid}.html",
        price=price,
        orders_count=orders,
    )


def request(text: str) -> SearchRequest:
    return SearchRequest(original_query=text, normalized_query=text)


# Real AliExpress titles captured by the G24.6 live audit (test FIXTURES
# only - no logic in this file references any of these products).
NEBULIZERS_EXACT = [
    "Семейный медицинский небулайзер, ингалятор, медицинский резервуар, чашка, мундштук",
    "Ультразвуковой сетчатый небулайзер с таймером, портативный ингалятор для взрослых",
    "Портативный сетчатый небулайзер JUSTLANG для кошек и собак, мини-ингалятор",
]
NEBULIZERS_FAMILY = [
    "Комплект аксессуаров для небулайзера с Т-образным соединителем и мундштуком",
    "Шланг для небулайзера длиной 2 м, аксессуар для небулайзера с воздушным компрессором",
    "Набор из 15 аксессуаров для семейного небулайзера: мягкие трубки, губчатые фильтры",
]
NEBULIZERS_JUNK = [
    "Детский мультяшный рюкзак с милым пончиком, сумка с яичками для дошкольного возраста",
    "Формочка для печенья из нержавеющей стали, круглая форма для пончиков",
    "Синтетические эластичные шиньоны с грязным пучком, вьющиеся резинки для волос",
]

KEYBOARD_VACUUMS = [
    "Литиевая аккумуляторная ручная воздуходувка, маленький пылесос для удаления пыли "
    "с клавиатуры компьютера, для сада, листьев, снега",
    "Бесщеточный турбовентилятор A25F-130000 об/мин, ручной, 2-в-1: воздуходувка и "
    "пылесос для автомобиля, клавиатуры, дома",
    "Беспроводной очиститель клавиатуры и пылесос для компьютера, 51000 об/мин",
]


class NebulizerFunnelTests(unittest.TestCase):
    """Req 1: «небулайзер» must not collapse to only exact-title matches."""

    def test_family_and_accessory_results_survive_the_relevance_gate(self) -> None:
        request_ = request("небулайзер")
        products = [
            product(t, pid=f"e{i}") for i, t in enumerate(NEBULIZERS_EXACT)
        ] + [
            product(t, pid=f"f{i}") for i, t in enumerate(NEBULIZERS_FAMILY)
        ] + [
            product(t, pid=f"j{i}") for i, t in enumerate(NEBULIZERS_JUNK)
        ]

        kept, dropped = filter_relevant(products, request_)

        kept_ids = {p.product_id for p in kept}
        # Every exact AND family/accessory result is retained...
        self.assertTrue(kept_ids >= {f"e{i}" for i in range(3)})
        self.assertTrue(kept_ids >= {f"f{i}" for i in range(3)})
        # ...only the token-less junk leaves, with the explained reason.
        self.assertEqual(
            dropped, {"no_query_term_in_title": 3}
        )
        self.assertEqual(len(kept), 6)

    def test_exact_family_members_are_tier_2_accessories_tier_1(self) -> None:
        request_ = request("небулайзер")
        for title in NEBULIZERS_EXACT:
            with self.subTest(title=title):
                self.assertEqual(
                    relevance_tier(product(title), request_), 2
                )
        for title in NEBULIZERS_FAMILY:
            with self.subTest(title=title):
                self.assertEqual(
                    relevance_tier(product(title), request_), 1
                )


class KeyboardVacuumFamilyTests(unittest.TestCase):
    """Req 2: blower/air-duster titles explicitly about keyboard cleaning."""

    def test_related_products_that_reference_keyboard_use_are_kept(self) -> None:
        request_ = request("мини пылесос для клавиатуры")

        for title in KEYBOARD_VACUUMS:
            with self.subTest(title=title):
                self.assertIsNone(rejection_reason(product(title), request_))
                self.assertEqual(classify_product(product(title), request_), "mentioned")
                self.assertEqual(relevance_tier(product(title), request_), 1)

    def test_exact_keyboard_vacuum_ranks_above_the_related_blowers(self) -> None:
        request_ = request("мини пылесос для клавиатуры")
        exact = product(
            "Миниатюрный настольный пылесос, беспроводной очиститель клавиатуры",
            pid="exact",
        )
        related = product(KEYBOARD_VACUUMS[0], pid="related")

        ordered = sort_products([related, exact], request_)

        self.assertEqual([p.product_id for p in ordered], ["exact", "related"])


class RankingContractTests(unittest.TestCase):
    """Reqs 3+5: exact first, accessories below, unrelated still rejected."""

    def test_exact_product_outranks_a_related_one(self) -> None:
        request_ = request("электрическая зубная щетка")
        exact = product("Электрическая зубная щетка Oral B", pid="brush")
        related = product("Футляр для электрической зубной щетки, дорожный", pid="case")

        ordered = sort_products([related, exact], request_)

        self.assertEqual([p.product_id for p in ordered], ["brush", "case"])
        self.assertGreater(
            relevance_tier(exact, request_), relevance_tier(related, request_)
        )

    def test_an_accessory_never_outranks_the_exact_product(self) -> None:
        request_ = request("наушники")
        exact = product("Наушники беспроводные TWS Bluetooth 5.3", pid="hp")
        accessory = product(
            "Чехол для наушников с мягкой подкладкой, защитный", pid="case"
        )

        self.assertIsNone(rejection_reason(accessory, request_))
        self.assertGreater(
            relevance_tier(exact, request_), relevance_tier(accessory, request_)
        )
        ordered = sort_products([accessory, exact], request_)
        self.assertEqual([p.product_id for p in ordered], ["hp", "case"])

    def test_clearly_unrelated_products_are_still_rejected(self) -> None:
        request_ = request("небулайзер")
        for title in NEBULIZERS_JUNK:
            with self.subTest(title=title):
                self.assertEqual(
                    rejection_reason(product(title), request_),
                    "no_query_term_in_title",
                )

    def test_a_match_on_a_shared_adjective_only_is_still_rejected(self) -> None:
        """Req 4/6: incidental words («беспроводная мышь») are not evidence."""

        request_ = request("беспроводные наушники")
        mouse = product("Беспроводная мышь для игр RGB", pid="mouse")

        self.assertEqual(
            rejection_reason(mouse, request_), "no_product_noun_in_title"
        )


class LedLampTests(unittest.TestCase):
    """Req 7: other LED products rejected, lamps retained."""

    def test_unrelated_led_product_is_rejected(self) -> None:
        request_ = request("LED лампа")
        chair = product(
            "Игровое кресло с Bluetooth-колонкой, LED-дисплеем, массажем и откидной спинкой",
            pid="chair",
        )

        self.assertEqual(
            rejection_reason(chair, request_), "no_product_noun_in_title"
        )

    def test_a_lamp_is_retained_and_ranked_exact(self) -> None:
        request_ = request("LED лампа")
        lamp = product(
            "Настольная лампа светодиодная LED с USB-зарядкой, регулируемая",
            pid="lamp",
        )

        self.assertIsNone(rejection_reason(lamp, request_))
        self.assertEqual(relevance_tier(lamp, request_), 2)


class CrossLanguageTests(unittest.TestCase):
    """Req 8: English queries are judged, never a silent bypass."""

    EN = "portable nebulizer"

    def test_family_title_is_promoted_by_romanized_evidence(self) -> None:
        title = (
            "Портативный медицинский сетчатый небулайзер-ингалятор для взрослых "
            "и детей, прибор для измерения уровня кислорода"
        )

        self.assertIsNone(rejection_reason(product(title), request(self.EN)))
        self.assertEqual(classify_product(product(title), request(self.EN)), "exact")
        self.assertEqual(relevance_tier(product(title), request(self.EN)), 2)

    def test_modifier_only_russian_junk_is_rejected(self) -> None:
        """«portable» in the title alone is the spam signature, not family."""

        title = (
            "Электрическая портативная расческа-массажер с функцией увлажнения "
            "и массажа кожи головы"
        )

        self.assertEqual(
            rejection_reason(product(title), request(self.EN)),
            "no_product_noun_in_title",
        )

    def test_unjudgeable_titles_are_kept_ranked_last_and_logged(self) -> None:
        """No evidence in any script: kept honestly at tier 0, not silent."""

        family = product(
            "Портативный медицинский сетчатый небулайзер-ингалятор", pid="family"
        )
        opaque = product(
            "Мягкая игрушка-антистресс в виде пончика, медленно восстанавливающая форму",
            pid="opaque",
        )
        request_ = request(self.EN)

        kept, dropped = filter_relevant([family, opaque], request_)
        self.assertEqual(
            [p.product_id for p in kept], ["family", "opaque"]
        )
        self.assertEqual(dropped, {})
        self.assertEqual(relevance_tier(opaque, request_), 0)
        self.assertGreater(
            relevance_tier(family, request_), relevance_tier(opaque, request_)
        )
        ordered = sort_products([opaque, family], request_)
        self.assertEqual([p.product_id for p in ordered], ["family", "opaque"])
        with self.assertLogs("services.relevance", level="INFO") as logs:
            filter_relevant([opaque], request_)
        self.assertTrue(
            any("cross-language unverified" in line for line in logs.output)
        )

    def test_an_english_query_still_returns_russian_results(self) -> None:
        """The documented honest limitation stays: nothing is hidden."""

        phone = product("Силиконовый держатель для телефона на присоске", pid="hold")

        self.assertIsNone(rejection_reason(phone, request("phone accessories")))
        self.assertEqual(relevance_tier(phone, request("phone accessories")), 0)


class GenericityTests(unittest.TestCase):
    """Req 9: deterministic AND product-agnostic - no special cases."""

    def test_the_transliteration_is_an_alphabet_table_not_a_vocabulary(self) -> None:
        canary = ("небулайзер", "пылесос", "ингалятор", "наушники")
        for key, value in _CYR_TO_LAT.items():
            self.assertEqual(len(key), 1, f"table key must be ONE letter: {key!r}")
            self.assertNotIn(key, canary)
            self.assertNotIn(value, canary)
        self.assertGreaterEqual(len(_CYR_TO_LAT), 30)

    def test_the_gate_works_for_invented_words(self) -> None:
        """A nonsense query behaves structurally like a real one."""

        request_ = request("фларб дублиум")
        exact = product("Профессиональный фларб для дублиума 2026", pid="ok")
        junk = product("Вилка столовая никелированная", pid="junk")

        self.assertIsNone(rejection_reason(exact, request_))
        self.assertEqual(relevance_tier(exact, request_), 2)
        self.assertEqual(
            rejection_reason(junk, request_), "no_query_term_in_title"
        )

    def test_a_headphone_query_still_returns_headphones(self) -> None:
        """Req 6 positive half: the real product is retained as exact."""

        request_ = request("наушники")
        hp = product("Наушники беспроводные TWS Bluetooth 5.3 с микрофоном")

        self.assertIsNone(rejection_reason(hp, request_))
        self.assertEqual(relevance_tier(hp, request_), 2)


if __name__ == "__main__":
    unittest.main()


