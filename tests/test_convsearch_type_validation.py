"""Hard product-type validation tests — principal product vs incidental mention.

Real-ish AliExpress-style titles (RU + EN), including the fishing-reel
regression family from the runtime logs and cross-category negatives
(drill bits, power-bank accessories, replacement parts). Category-general:
no per-product keyword patch anywhere.
"""

from __future__ import annotations

import unittest

from models.product import Marketplace, Product
from services.convsearch.type_validation import validate_product_type


def product(pid: str, title: str, *, price: float = 25.0) -> Product:
    return Product(
        marketplace=Marketplace.ALIEXPRESS,
        product_id=pid,
        title=title,
        product_url=f"https://www.aliexpress.com/item/{pid}.html",
        price=price,
        currency="USD",
    )


REEL = "рыболовная катушка"
BANK = "повербанк"
DRILL = "дрель"


class FishingReelValidationTests(unittest.TestCase):
    """The «катушка» regression family: accessories/parts never qualify."""

    def _verdict(self, title: str) -> bool:
        return validate_product_type(product("x", title), REEL).qualifies

    def test_complete_reel_qualifies(self) -> None:
        self.assertTrue(self._verdict("Рыболовная катушка безынерционная 3000"))
        self.assertTrue(self._verdict("Катушка рыболовная карбоновая spinning"))
        self.assertTrue(self._verdict("Рыболовная катушка мультипликаторная для спиннинга"))

    def test_reel_holder_is_rejected(self) -> None:
        self.assertFalse(self._verdict("Держатель катушки рыболовный"))
        self.assertFalse(self._verdict("Держатель для рыболовной катушки"))

    def test_reel_spring_and_parts_are_rejected(self) -> None:
        self.assertFalse(self._verdict("Пружина для рыболовной катушки"))
        self.assertFalse(self._verdict("Шестерни для катушки рыболовной"))
        self.assertFalse(self._verdict("Подшипники для рыболовной катушки"))
        self.assertFalse(self._verdict("Ремкомплект для рыболовной катушки"))

    def test_reel_case_is_rejected(self) -> None:
        self.assertFalse(self._verdict("Чехол для рыболовной катушки"))
        self.assertFalse(self._verdict("Сумка для рыболовной катушки"))

    def test_line_spool_is_rejected(self) -> None:
        self.assertFalse(self._verdict("Шпуля для рыболовной катушки"))
        self.assertFalse(self._verdict("Запасная шпуля лескоукладчик"))

    def test_bare_ambiguous_noun_is_rejected(self) -> None:
        # "катушка" alone is ambiguous (audio tape reel vs fishing reel):
        # without the context token there is insufficient evidence.
        self.assertFalse(self._verdict("Катушка"))
        # And an audio tape reel mentioning "катушка" does not qualify.
        self.assertFalse(self._verdict("Кассетная катушка для магнитофона"))


class CrossCategoryValidationTests(unittest.TestCase):
    """The same rules must generalize (power bank, drill, appliance)."""

    def test_power_bank_qualifies(self) -> None:
        self.assertTrue(validate_product_type(
            product("p1", "Повербанк 20000 мАч быстрая зарядка"), BANK
        ).qualifies)

    def test_power_bank_accessories_rejected(self) -> None:
        for title in (
            "Кабель для повербанка USB-C",
            "Чехол для повербанка",
            "Зарядное устройство для повербанка",
        ):
            self.assertFalse(validate_product_type(product("p2", title), BANK).qualifies,
                             msg=title)

    def test_genitive_product_mention_is_not_the_product(self) -> None:
        # The title names a switch whose function/target is a welding machine;
        # the genitive phrase must not make the switch pass as a welder.
        for title in (
            "Поворотный кулачковый переключатель сварочного аппарата",
            "Ручка регулировки сварочного аппарата",
            "Комплект деталей сварочного аппарата",
        ):
            self.assertFalse(
                validate_product_type(product("welder-part", title), "сварочный аппарат").qualifies,
                msg=title,
            )

    def test_actual_welder_titles_still_qualify(self) -> None:
        for title in (
            "Сварочный аппарат инверторный бытовой",
            "Инверторный сварочный аппарат MMA 200A",
            "Сварочный полуавтомат для сварки металла",
        ):
            self.assertTrue(
                validate_product_type(product("welder", title), "сварочный аппарат").qualifies,
                msg=title,
            )

    def test_drill_qualifies_but_bits_rejected(self) -> None:
        self.assertTrue(validate_product_type(
            product("d1", "Дрель аккумуляторная ударная 21V"), DRILL
        ).qualifies)
        self.assertFalse(validate_product_type(
            product("d2", "Набор свёрл для дрели 100 шт"), DRILL
        ).qualifies)
        self.assertFalse(validate_product_type(
            product("d3", "Насадка для дрели миксер"), DRILL
        ).qualifies)

    def test_no_type_requested_passes_everything(self) -> None:
        self.assertTrue(validate_product_type(
            product("n1", "Что угодно вообще"), None
        ).qualifies)

    def test_empty_title_is_rejected_when_type_requested(self) -> None:
        self.assertFalse(validate_product_type(product("n2", ""), REEL).qualifies)


if __name__ == "__main__":
    unittest.main()
