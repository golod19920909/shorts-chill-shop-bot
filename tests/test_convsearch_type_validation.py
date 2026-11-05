"""Hard product-type validation tests — principal product vs incidental mention.

Real-ish AliExpress-style titles (RU + EN), including the fishing-reel
regression family from the runtime logs and cross-category negatives
(drill bits, power-bank accessories, replacement parts). Category-general:
no per-product keyword patch anywhere.
"""

from __future__ import annotations

import unittest

from models.product import Marketplace, Product
from services.convsearch.type_validation import stems_match, validate_product_type


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


class StemMatchingTests(unittest.TestCase):
    """Word-form matching must not collapse unrelated Latin product nouns."""

    def test_russian_inflections_keep_existing_stem_matching(self) -> None:
        self.assertTrue(stems_match("шуруповерт", "шуруповерта"))
        self.assertTrue(stems_match("катушка", "катушки"))

    def test_english_plural_forms_match_without_prefix_false_positives(self) -> None:
        self.assertTrue(stems_match("bearing", "bearings"))
        self.assertTrue(stems_match("battery", "batteries"))
        self.assertFalse(stems_match("table", "tablet"))
        self.assertFalse(stems_match("drill", "drilling"))


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

    def test_sparse_title_with_description_subtype_is_deferred_for_review(self) -> None:
        item = product("x", "Катушка 5000")
        item.description = "Рыболовная катушка для спиннинга"
        verdict = validate_product_type(item, REEL)
        self.assertTrue(verdict.qualifies)
        self.assertIn(
            "bare_type_with_description_confirmation_requires_review",
            verdict.reasons,
        )

    def test_conflicting_title_subtype_is_not_rescued_by_description(self) -> None:
        item = product("x", "Кассетная катушка для магнитофона")
        item.description = "Рыболовная катушка, spinning reel"
        verdict = validate_product_type(item, REEL)
        self.assertFalse(verdict.qualifies)
        self.assertIn("ambiguous_bare_noun", verdict.reasons)

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

    def test_partial_type_coverage_keeps_a_lower_identity_score(self) -> None:
        verdict = validate_product_type(
            product("partial-type", "Аккумуляторный шуруповёрт 21V"),
            "аккумуляторный профессиональный шуруповёрт",
        )

        self.assertTrue(verdict.qualifies)
        self.assertIn("partial_type_coverage", verdict.reasons)
        self.assertNotIn("type_head", verdict.reasons)
        self.assertAlmostEqual(verdict.score, 0.5 * (2 / 3))

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

    def test_shared_modifier_without_requested_head_requires_semantic_review(self) -> None:
        verdict = validate_product_type(
            product("welder-table", "Сварочный стол усиленный"),
            "сварочный аппарат",
        )
        self.assertTrue(verdict.qualifies)
        self.assertIn("type_identity_requires_semantic_review", verdict.reasons)
        self.assertLess(verdict.score, 0.5)

    def test_valid_variant_head_is_deferred_for_semantic_confirmation(self) -> None:
        verdict = validate_product_type(
            product("welder-semi", "Сварочный полуавтомат MIG"),
            "сварочный аппарат",
        )
        self.assertTrue(verdict.qualifies)
        self.assertIn("type_identity_requires_semantic_review", verdict.reasons)

    def test_non_accessory_set_requests_are_not_fitment_relations(self) -> None:
        from services.convsearch.type_validation import is_accessory_product_type

        self.assertFalse(is_accessory_product_type("наборы на свадьбу"))
        self.assertFalse(is_accessory_product_type("комплекты для дома"))
        self.assertTrue(is_accessory_product_type("набор насадок"))
        self.assertTrue(is_accessory_product_type("комплект кабелей"))

    def test_description_only_product_identity_requires_semantic_review(self) -> None:
        candidate = product(
            "generic-title",
            "Универсальный набор инструментов",
        ).model_copy(update={
            "description": "Насадка для дрели по дереву с хвостовиком",
        })

        verdict = validate_product_type(candidate, "насадка")

        self.assertTrue(verdict.qualifies)
        self.assertIn("type_identity_requires_semantic_review", verdict.reasons)
        self.assertLess(verdict.score, 0.5)

    def test_bundle_title_containing_requested_item_is_deferred_for_review(self) -> None:
        candidate = product(
            "attachment-set",
            "Набор насадок для дрели",
        )

        verdict = validate_product_type(candidate, "насадка")

        self.assertTrue(verdict.qualifies)
        self.assertIn("type_identity_requires_semantic_review", verdict.reasons)
        self.assertLess(verdict.score, 0.5)

    def test_description_cannot_override_title_identifying_an_accessory(self) -> None:
        candidate = product(
            "reel-spring-description",
            "Пружина для рыболовной катушки",
        ).model_copy(update={
            "description": "Совместима с рыболовной катушкой",
        })

        verdict = validate_product_type(candidate, REEL)

        self.assertFalse(verdict.qualifies)
        self.assertIn("type_only_in_role_clause", verdict.reasons)

    def test_late_type_mention_in_a_mixed_listing_title_requires_review(self) -> None:
        verdict = validate_product_type(
            product(
                "mixed-listing",
                "Многофункциональная дрель, угловая шлифовальная машина, "
                "набор шатунов, насадки для шатунов, регулируемый диск",
            ),
            "насадка",
        )
        # A later mention must not count as a verified principal-product match.
        self.assertTrue(verdict.qualifies)
        self.assertIn(
            "type_mentioned_only_after_listing_separator", verdict.reasons
        )
        self.assertLess(verdict.score, 0.5)

    def test_host_first_compound_accessory_titles_are_not_the_requested_host(self) -> None:
        for title, requested_type in (
            ("Phone case for iPhone 15", "phone"),
            ("Camera lens 50mm", "camera"),
            ("Cordless drill bits set", "drill"),
            ("Battery charger USB-C", "battery"),
        ):
            verdict = validate_product_type(
                product("host-accessory-compound", title), requested_type
            )
            self.assertFalse(verdict.qualifies, msg=title)
            self.assertIn(
                "requested_type_is_host_for_accessory", verdict.reasons,
                msg=title,
            )

    def test_descriptive_adjective_is_not_misread_as_accessory(self) -> None:
        for title, requested_type in (
            ("Наушники проводные", "наушники"),
            ("Повербанк проводной зарядки", "повербанк"),
        ):
            verdict = validate_product_type(
                product("descriptive-adjective", title), requested_type
            )
            self.assertTrue(verdict.qualifies, msg=title)

    def test_host_with_accessory_relation_remains_a_host_product(self) -> None:
        for title, requested_type in (
            ("Phone with protective case", "phone"),
            ("Camera with lens cover", "camera"),
        ):
            verdict = validate_product_type(
                product("host-with-accessory", title), requested_type
            )
            self.assertTrue(verdict.qualifies, msg=title)

    def test_later_cross_sell_fragment_does_not_reject_primary_host(self) -> None:
        verdict = validate_product_type(
            product("host-cross-sell", "Phone 15, case included"), "phone"
        )
        self.assertTrue(verdict.qualifies)

    def test_host_product_with_requested_accessory_is_not_the_accessory(self) -> None:
        for title, requested_type in (
            ("Дрель аккумуляторная с насадкой", "насадка"),
            ("Телефон с защитным чехлом", "чехол"),
            ("Шуруповёрт с насадкой-миксером", "насадка"),
        ):
            verdict = validate_product_type(
                product("host-with-accessory", title), requested_type
            )
            self.assertFalse(verdict.qualifies, msg=title)

    def test_english_host_product_with_accessory_is_not_the_accessory(self) -> None:
        verdict = validate_product_type(
            product("drill-with-attachment", "Drill with attachment"),
            "attachment",
        )
        self.assertFalse(verdict.qualifies)
        self.assertIn("type_is_accessory_of_host_product", verdict.reasons)

    def test_adjective_before_requested_accessory_is_still_valid(self) -> None:
        verdict = validate_product_type(
            product("real-attachment", "Аккумуляторная насадка для дрели"),
            "насадка",
        )
        self.assertTrue(verdict.qualifies)

    def test_requested_type_in_primary_title_clause_is_deterministic(self) -> None:
        verdict = validate_product_type(
            product("real-attachment", "Насадка для дрели по дереву"),
            "насадка",
        )
        self.assertTrue(verdict.qualifies)
        self.assertNotIn(
            "type_mentioned_only_after_listing_separator", verdict.reasons
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

    def test_cross_language_title_is_deferred_to_semantic_review(self) -> None:
        # A Russian request may retrieve an English marketplace title.
        # Different scripts cannot be compared with shared word stems, so
        # the candidate must reach semantic review rather than be discarded.
        verdict = validate_product_type(
            product("english-drill", "Cordless screwdriver drill driver 21V"),
            "аккумуляторный шуруповёрт",
        )
        self.assertTrue(verdict.qualifies)
        self.assertIn(
            "cross_language_identity_requires_semantic_review",
            verdict.reasons,
        )
        self.assertLess(verdict.score, 0.5)

    def test_same_language_synonym_without_shared_type_token_is_reviewed(self) -> None:
        verdict = validate_product_type(
            product("synonym-drill", "Беспроводная электрическая дрель"),
            "шуруповёрт",
        )

        # No shared stem is uncertainty, not proof that the marketplace
        # listing is unrelated. Gemini must still confirm the identity.
        self.assertTrue(verdict.qualifies)
        self.assertIn(
            "type_identity_requires_semantic_review",
            verdict.reasons,
        )
        self.assertLess(verdict.score, 0.5)

    def test_accessory_with_no_shared_type_token_is_still_rejected(self) -> None:
        verdict = validate_product_type(
            product("driver-bits", "Набор бит для шуруповёрта"),
            "шуруповёрт",
        )

        self.assertFalse(verdict.qualifies)
        self.assertTrue(
            any(reason.startswith("accessory_head:") for reason in verdict.reasons)
        )

    def test_cross_language_accessory_is_still_rejected(self) -> None:
        verdict = validate_product_type(
            product("english-bits", "Drill bits set for cordless screwdriver"),
            "шуруповёрт",
        )
        self.assertFalse(verdict.qualifies)
        self.assertTrue(any(reason.startswith("accessory_head:") for reason in verdict.reasons))

    def test_no_type_requested_passes_everything(self) -> None:
        self.assertTrue(validate_product_type(
            product("n1", "Что угодно вообще"), None
        ).qualifies)

    def test_empty_title_is_rejected_when_type_requested(self) -> None:
        self.assertFalse(validate_product_type(product("n2", ""), REEL).qualifies)

    def test_trailing_adjective_does_not_replace_requested_product_head(self) -> None:
        for requested_type in ("карманный фонарик", "фонарик карманный"):
            verdict = validate_product_type(
                product("flashlight", "Карманный светодиодный фонарик 1000 lm"),
                requested_type,
            )
            self.assertTrue(verdict.qualifies, msg=requested_type)



if __name__ == "__main__":
    unittest.main()
