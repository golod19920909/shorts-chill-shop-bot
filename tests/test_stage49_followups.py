"""Stage 49 - conversational routing, deterministic refinement and
complete-product-vs-spare-part relevance regressions.

All checks are OFFLINE and deterministic: they use the real regex fallback
parser (``AIService._fallback_parse``) and the real deterministic router /
merge / relevance functions, never Gemini and never the network.
"""

from __future__ import annotations

import asyncio
import unittest

from models.product import Marketplace, Product
from models.search_request import SearchRequest
from services.ai_service import AIService
from services.chat_router import ChatAction, classify
from services.conversation import resolve_request
from services.relevance import rejection_reason, relevance_tier


def parse(text: str) -> SearchRequest:
    """The real deterministic parser (used when Gemini is absent)."""
    return AIService._fallback_parse(text)


def product(title: str, pid: str = "1") -> Product:
    return Product(
        marketplace=Marketplace.ALIEXPRESS,
        product_id=pid,
        title=title,
        product_url=f"https://example.com/{pid}",
        price=10.0,
    )



# --- D1: conversational routing -------------------------------------------


class TestRoutingComparison(unittest.TestCase):
    """Natural Russian comparison phrasings must route to COMPARE with a
    shown result set, and stay SEARCH without one."""

    def test_comparison_phrasings_route_to_compare(self) -> None:
        cases = [
            "Какой вариант лучше подойдёт для моих задач?",
            "Что лучше?",
            "Какой лучше выбрать?",
            "Какой из них лучше?",
            "Что выгоднее?",
            "А первый лучше?",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertIs(
                    classify(text, has_shown_results=True).action,
                    ChatAction.COMPARE,
                )

    def test_result_reference_phrasings_route_to_explain(self) -> None:
        cases = [
            "А что насчёт второго варианта?",
            "Расскажи подробнее о втором.",
            "Какие у него характеристики?",
            "Почему ты рекомендуешь этот?",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertIs(
                    classify(text, has_shown_results=True).action,
                    ChatAction.EXPLAIN,
                )

    def test_broad_words_stay_search(self) -> None:
        for text in ("лучше", "вариант", "лучший подарок для мамы"):
            with self.subTest(text=text):
                self.assertIs(
                    classify(text, has_shown_results=True).action,
                    ChatAction.SEARCH,
                )

    def test_without_shown_results_stays_compare_or_explain(self) -> None:
        # B3: the wording's intent survives an empty cache - the caller
        # answers honestly about the missing results instead of searching
        # the question text as product keywords.
        for text, expected in (
            ("Какой вариант лучше подойдёт для моих задач?", ChatAction.COMPARE),
            ("А что насчёт второго варианта?", ChatAction.EXPLAIN),
        ):
            with self.subTest(text=text):
                self.assertIs(
                    classify(text, has_shown_results=False).action,
                    expected,
                )


# --- D2: deterministic refinement of short follow-ups ---------------------


class TestDeterministicRefinement(unittest.TestCase):
    """Short context-dependent follow-ups must refine the previous request
    through the deterministic (no-AI) path."""

    def setUp(self) -> None:
        self.prev = parse("шуруповерт аккумуляторный для дома")

    def _resolve(self, text: str):
        return asyncio.run(resolve_request(self.prev, parse(text), text))

    def test_relative_comparatives_refine(self) -> None:
        for text in ("Подешевле", "Дешевле", "Подороже", "Помощнее",
                     "Повыше качеством"):
            with self.subTest(text=text):
                merged, refined = self._resolve(text)
                self.assertTrue(refined)
                self.assertEqual(
                    merged.normalized_query, self.prev.normalized_query
                )

    def test_feature_only_followup_refines(self) -> None:
        merged, refined = self._resolve("С двумя аккумуляторами")
        self.assertTrue(refined)
        self.assertIn("аккумулятор", " ".join(merged.required_features))
        self.assertEqual(
            merged.normalized_query, self.prev.normalized_query
        )

    def test_exclusion_only_followup_refines(self) -> None:
        merged, refined = self._resolve("Без провода")
        self.assertTrue(refined)
        self.assertTrue(merged.excluded_features)
        self.assertEqual(
            merged.normalized_query, self.prev.normalized_query
        )

    def test_new_product_still_replaces(self) -> None:
        _, refined = self._resolve(
            "Забудь шуруповёрт, найди набор отвёрток"
        )
        self.assertFalse(refined)

    def test_bare_new_product_is_new_search(self) -> None:
        _, refined = self._resolve("нужны кроссовки")
        self.assertFalse(refined)

    def test_no_invented_budget_for_cheaper(self) -> None:
        merged, refined = self._resolve("Подешевле")
        self.assertTrue(refined)
        self.assertIsNone(merged.max_price)

    def test_explicit_budget_replaces(self) -> None:
        prev = parse("шуруповерт до 100 долларов")
        merged, refined = asyncio.run(
            resolve_request(prev, parse("до 40 долларов"), "до 40 долларов")
        )
        self.assertTrue(refined)
        self.assertEqual(merged.max_price, 40.0)

    def test_refinement_follow_ups_preserve_the_previous_product(self) -> None:
        """Follow-ups that name no new product must refine, not reset,
        and must keep the previous product as the catalog query."""
        cases = (
            "Сделай подешевле",
            "посоветуй что-нибудь",
            "а есть покороче?",
            "только не ударную",
        )
        for text in cases:
            with self.subTest(text=text):
                merged, refined = self._resolve(text)
                self.assertTrue(
                    refined, f"{text!r} should refine, not start a new search"
                )
                self.assertEqual(
                    merged.normalized_query,
                    self.prev.normalized_query,
                    f"{text!r} must preserve the previous product query",
                )

    def test_genuine_product_switches_still_reset(self) -> None:
        """An explicit new product must still replace the previous search."""
        cases = (
            "а плазморезка?",
            "а нужны кроссовки",
            "набор отвёрток",
        )
        for text in cases:
            with self.subTest(text=text):
                _, refined = self._resolve(text)
                self.assertFalse(
                    refined, f"{text!r} should switch to a new product"
                )

    def test_controls_still_refine(self) -> None:
        """Plain comparatives and budget caps keep refining (no regression)."""
        for text in ("подешевле", "дешевые", "а дешевые?", "только до 40 долларов"):
            with self.subTest(text=text):
                _, refined = self._resolve(text)
                self.assertTrue(refined)

    def test_follow_ups_route_to_search_during_active_conversation(self) -> None:
        """Inside an active search these messages stay SEARCH - the
        refinement-vs-switch decision is made downstream by
        ``services.conversation`` (not reclassified by the router)."""
        for text in (
            "Сделай подешевле",
            "посоветуй что-нибудь",
            "а есть покороче?",
            "только не ударную",
        ):
            with self.subTest(text=text):
                self.assertIs(
                    classify(text, has_conversation=True).action,
                    ChatAction.SEARCH,
                )


# --- O2: spare-part leakage for a complete-product request ----------------


class TestSparePartRelevance(unittest.TestCase):
    """A spare part must not survive as a relevant result for a request that
    asks for the COMPLETE product, while an explicit spare-part request keeps
    retrieving the part."""

    def test_complete_drill_request_rejects_spare_part(self) -> None:
        request = parse("шуруповерт аккумуляторный для дома")
        gearbox = product(
            "Редукторный механизм 125463-0 для аккумуляторной "
            "дрели-шуруповерта MAKITA"
        )
        self.assertEqual(
            rejection_reason(gearbox, request),
            "spare_part_not_requested_product",
        )
        self.assertEqual(relevance_tier(gearbox, request), 0)

    def test_complete_drill_request_keeps_complete_drill(self) -> None:
        request = parse("шуруповерт аккумуляторный для дома")
        complete = product("Дрель-шуруповёрт аккумуляторная 21V набор")
        self.assertIsNone(rejection_reason(complete, request))

    def test_explicit_spare_part_request_keeps_the_part(self) -> None:
        request = parse("редуктор для дрели-шуруповерта")
        gearbox = product(
            "Редукторный механизм 125463-0 для дрели-шуруповерта MAKITA"
        )
        self.assertIsNone(rejection_reason(gearbox, request))
        self.assertGreaterEqual(relevance_tier(gearbox, request), 1)

    def test_accessory_request_is_not_blocked(self) -> None:
        request = parse("зарядное устройство для дрели")
        charger = product("Зарядное устройство 12В для дрели MAKITA")
        self.assertIsNone(rejection_reason(charger, request))

    def test_battery_for_cordless_drill_is_kept(self) -> None:
        request = parse("шуруповерт аккумуляторный для дома")
        battery = product("Аккумулятор 18В для дрели-шуруповерта")
        self.assertIsNone(rejection_reason(battery, request))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

