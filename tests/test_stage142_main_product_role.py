# -*- coding: utf-8 -*-
"""G25.2-D Stage 14.2: intent understanding + main-product role tests.

Covers the two confirmed production defects from Stage 14.1 (query «Мне
нужна недорогая но хорошая зарядка на телефон» returned a phone-cleaning
kit as the first result):

* the deterministic fallback intent must name the MAIN product («зарядка»)
  and never keep request verbs/filler («нужна», «мне», «но», «недорогая»)
  in the product phrase - nor invent a numeric budget, a rating or any
  compatibility the user never stated;
* under structured SEARCH intent a title that merely MENTIONS the
  requested product in a secondary context (a cleaning kit, a dust plug,
  a brush, a cable) must never be classified as the requested product.

Deterministic synthetic fixtures only: no live AliExpress, no Gemini
(``AIService(api_key=None)`` runs the local fallback parser). Every
pipeline helper mirrors the production order of ``handlers.search._search``
(Stage 2): ``resolve_and_apply -> filter_relevant -> sort_products``.
"""

from __future__ import annotations

import unittest

from models.search_request import SearchMode, SearchRequest
from services.comparison_service import sort_products
from services.query_intent import fallback_intent
from services.relevance import (
    _intent_product_nouns,
    classify_product,
    filter_relevant,
    query_noun_terms,
    rejection_reason,
    relevance_tier,
)
from tests.test_search_intent import make_listing, resolve_and_apply

INCIDENT_QUERY = "Мне нужна недорогая но хорошая зарядка на телефон"

KIT_TITLE = (
    "Набор для чистки разъема зарядки и динамика мобильного телефона, "
    "пылезащитная заглушка для iPhone 16 15 17"
)
DUST_PLUG_TITLE = "Пылезащитная заглушка для разъема зарядки iPhone"
BRUSH_TITLE = "Щетка для чистки зарядного разъема телефона"
CASE_TITLE = "Чехол для телефона силикон прозрачный"
CABLE_TITLE = "Кабель USB-C для зарядки 1м"
WALL_CHARGER_TITLE = "Зарядное устройство Baseus 65W GaN для телефона"
WIRELESS_CHARGER_TITLE = "Зарядка беспроводная для телефона 15W"
ADAPTER_TITLE = "Адаптер быстрой зарядки USB PD 30W для телефона"


def request_for(query: str) -> SearchRequest:
    """The intent-bearing production request for one sentence."""
    request, _, _ = resolve_and_apply(query)
    return request


def classify(query: str, title: str) -> str:
    return classify_product(make_listing("p", title), request_for(query))


class FallbackIntentUnderstandsNaturalRequests(unittest.TestCase):
    """Stage B: the incident query parses to the intended main product."""

    def setUp(self) -> None:
        self.intent = fallback_intent(INCIDENT_QUERY)

    def test_main_product_is_the_charger_not_the_accessory(self) -> None:
        self.assertEqual(self.intent.product_type, "зарядка")
        self.assertNotIn("набор", self.intent.product_type)

    def test_request_verbs_and_filler_never_stay_in_the_phrase(self) -> None:
        for junk in ("нужна", "мне", "но", "недорогая", "хорошая"):
            self.assertNotIn(junk, self.intent.product_type, junk)

    def test_no_invented_compatibility_model_or_brand(self) -> None:
        # «на телефон» is the user's own target - nothing beyond it.
        self.assertEqual(self.intent.compatibility, ["телефон"])
        self.assertIsNone(self.intent.brand)
        self.assertIsNone(self.intent.model)
        self.assertIsNone(self.intent.generation)
        self.assertEqual(self.intent.required_attributes, [])

    def test_nedorogaya_invents_no_numeric_budget(self) -> None:
        self.assertIsNone(self.intent.budget.min)
        self.assertIsNone(self.intent.budget.max)
        self.assertIsNone(self.intent.budget.currency)

    def test_horoshaya_invents_no_rating_or_quality_score(self) -> None:
        request = request_for(INCIDENT_QUERY)
        self.assertIsNone(request.min_rating)
        self.assertFalse(request.prefers_good_rating)

    def test_explicit_budget_is_still_parsed_alongside_filler(self) -> None:
        intent = fallback_intent(
            "Мне нужна недорогая зарядка на телефон до 500 грн")
        self.assertEqual(intent.product_type, "зарядка")
        self.assertEqual(intent.compatibility, ["телефон"])
        self.assertEqual(intent.budget.max, 500)
        self.assertEqual(intent.budget.currency, "UAH")

    def test_na_target_split_does_not_eat_a_digit_tail(self) -> None:
        # «на 4 телефона» is a quantity, never a compatibility target.
        intent = fallback_intent("футляр на 4 телефона")
        self.assertEqual(intent.product_type, "футляр на 4 телефона")
        self.assertEqual(intent.compatibility, [])

    def test_production_request_carries_the_clean_intent(self) -> None:
        request = request_for(INCIDENT_QUERY)
        self.assertEqual(request.intent_product_type, "зарядка")
        self.assertEqual(request.intent_compatibility, ["телефон"])
        self.assertEqual(request.normalized_query, "зарядка телефон")

    def test_nuzhna_is_never_a_product_noun(self) -> None:
        request = request_for(INCIDENT_QUERY)
        nouns = query_noun_terms(request, "cyrillic")
        self.assertNotIn("нужна", nouns)
        self.assertIn("зарядка", nouns)


class MainProductRoleClassification(unittest.TestCase):
    """Stage C: mentioning the product is not being the product."""

    def test_cleaning_kit_never_qualifies_as_the_charger(self) -> None:
        request = request_for(INCIDENT_QUERY)
        kit = make_listing("kit", KIT_TITLE)
        self.assertNotEqual(classify_product(kit, request), "exact")
        self.assertEqual(classify_product(kit, request), "mentioned")
        self.assertEqual(relevance_tier(kit, request), 1)

    def test_dust_plug_and_brush_never_qualify_as_the_charger(self) -> None:
        for title in (DUST_PLUG_TITLE, BRUSH_TITLE):
            with self.subTest(title=title):
                self.assertNotEqual(
                    classify(INCIDENT_QUERY, title), "exact")
                self.assertLess(
                    relevance_tier(
                        make_listing("p", title), request_for(INCIDENT_QUERY)
                    ),
                    2,
                )

    def test_case_and_cable_do_not_automatically_qualify(self) -> None:
        for title in (CASE_TITLE, CABLE_TITLE):
            with self.subTest(title=title):
                self.assertNotEqual(
                    classify(INCIDENT_QUERY, title), "exact")

    def test_a_real_wall_charger_can_qualify(self) -> None:
        # A genuine charger is never rejected and is eligible to be the
        # requested product; a title that LEADS with the product noun is
        # EXACT, one that leads with the device word («адаптер», «устройство»)
        # stays a kept tier-1 sibling under the existing role infrastructure.
        request = request_for(INCIDENT_QUERY)
        self.assertEqual(
            classify_product(
                make_listing("w", WIRELESS_CHARGER_TITLE), request),
            "exact",
        )
        for title in (WALL_CHARGER_TITLE, ADAPTER_TITLE):
            with self.subTest(title=title):
                charger = make_listing("p", title)
                self.assertIsNone(rejection_reason(charger, request))
                self.assertIn(
                    classify_product(charger, request),
                    ("exact", "mentioned"),
                )
                self.assertGreaterEqual(
                    relevance_tier(charger, request), 1)

    def test_compatible_charger_other_wording_is_not_rejected(self) -> None:
        request = request_for(INCIDENT_QUERY)
        charger = make_listing("w", WALL_CHARGER_TITLE)
        self.assertIsNone(rejection_reason(charger, request))
        self.assertIn(
            classify_product(charger, request), ("exact", "mentioned"))

    def test_polluted_intent_cannot_resurrect_the_kit(self) -> None:
        # Belt and braces: even a legacy/AI-shape product_type polluted
        # with filler words must never grant the kit an EXACT verdict.
        polluted = SearchRequest(
            original_query="зарядка",
            normalized_query="зарядка",
            intent_product_type="мне недорогая но зарядка на телефон",
        )
        kit = make_listing("kit", KIT_TITLE)
        self.assertNotEqual(classify_product(kit, polluted), "exact")
        self.assertLess(relevance_tier(kit, polluted), 2)
        # Role tokens keep only real product words.
        self.assertEqual(
            _intent_product_nouns(polluted, ["зарядка", "телефон"]),
            ["зарядка", "телефон"],
        )

    def test_charger_pipeline_leads_with_a_real_charger(self) -> None:
        request = request_for(INCIDENT_QUERY)
        products = [
            make_listing("0", KIT_TITLE, 1010.0),
            make_listing("1", DUST_PLUG_TITLE, 1009.0),
            make_listing("2", CABLE_TITLE, 1008.0),
            make_listing("3", WIRELESS_CHARGER_TITLE, 1007.0),
            make_listing("4", WALL_CHARGER_TITLE, 1006.0),
        ]
        kept, _ = filter_relevant(products, request)
        ordered = [p.product_id for p in sort_products(kept, request)]
        self.assertEqual(ordered[0], "3")
        self.assertLess(ordered.index("3"), ordered.index("0"))



class ExistingBehaviourIsPreserved(unittest.TestCase):
    """Required regression guards for Stage 14.2."""

    def test_cable_query_still_finds_cables(self) -> None:
        request = request_for("кабель USB-C")
        cable = make_listing("c", "Кабель USB-C 1м для зарядки")
        self.assertEqual(classify_product(cable, request), "exact")

    def test_battery_still_outranks_tool_and_case(self) -> None:
        request = request_for("аккумулятор для Makita 18V")
        battery = make_listing("b", "Аккумулятор Makita 18V 4Ah Li-Ion")
        tool = make_listing("t", "Дрель аккумуляторная Makita 18V LXT")
        case = make_listing("a", "Кейс для аккумулятора Makita 18V")
        self.assertEqual(classify_product(battery, request), "exact")
        self.assertEqual(relevance_tier(battery, request), 2)
        ordered = [p.product_id for p in sort_products([tool, case, battery],
                                                       request)]
        self.assertEqual(ordered[0], "b")

    def test_numeric_specification_parsing_is_unchanged(self) -> None:
        intent = fallback_intent("зарядка 65W")
        self.assertEqual(intent.product_type, "зарядка")
        self.assertEqual(intent.required_attributes, ["65 w"])
        intent = fallback_intent("повербанк 20000 мАч")
        self.assertEqual(intent.product_type, "повербанк")
        self.assertEqual(intent.required_attributes, ["20000 мач"])

    def test_empty_intent_legacy_behaviour_is_unchanged(self) -> None:
        legacy = SearchRequest(
            original_query=INCIDENT_QUERY, normalized_query=INCIDENT_QUERY)
        kit = make_listing("kit", KIT_TITLE)
        self.assertEqual(classify_product(kit, legacy), "mentioned")
        self.assertNotEqual(classify_product(kit, legacy), "exact")

    def test_non_search_modes_stay_untouched(self) -> None:
        request = request_for(INCIDENT_QUERY)
        kit = make_listing("kit", KIT_TITLE)
        for mode in (SearchMode.CATEGORY, SearchMode.POPULAR,
                     SearchMode.FEED, SearchMode.DISCOVERY):
            with self.subTest(mode=mode):
                request.mode = mode
                self.assertEqual(classify_product(kit, request), "unverified")


if __name__ == "__main__":
    unittest.main()

