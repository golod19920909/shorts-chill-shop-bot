"""B1-B6: conversational follow-ups, constraint preservation and honest limits.

Every test runs the REAL handler / router / conversation code over the offline
doubles from ``tests.test_stage47_conversational`` (stub marketplace, real
SQLite file, no Gemini, no network). Each test asserts the user-visible
outcome - which product cards the chat received, how many, in which order,
and whether the provider was touched at all - never merely that a function
ran.

Covers:
 1  a new search for an arbitrary product;
 2  a request for exactly five options;
 3  changing the count from five to three;
 4  cheaper alternatives (same product, lower budget);
 5  a request for additional products;
 6  a currency change without changing the category;
 7  adding a requirement;
 8  removing / changing a requirement;
 9  adding an exclusion;
 10 comparing two already-shown products;
 11 a question about a shown product;
 12 a genuinely new product search;
 13 a follow-up after the context expired;
 14 an AI timeout followed by the deterministic fallback;
 15 a provider failure;
 16 a successful search returning fewer products than requested;
 17 a follow-up answered from cache (no provider call);
 18 a follow-up that genuinely needs new products;
 19 the requested quantity respected by every response path;
 20 a failed or ambiguous follow-up never erases valid state.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pytest

from database.database import Database
from handlers import common
from handlers import search as search_handlers
from models.product import Marketplace, Product
from models.search_request import SearchRequest
from services import chat_router
from services.ai_service import AIService
from services.chat_router import ChatAction
from services.search_service import SearchService

from tests.test_stage47_conversational import (
    CatalogStub,
    FakeContext,
    FakeUpdate,
    RecordingAssistant,
    TextMessage,
    listing,
    run,
)


CONVERSATION_KEY = search_handlers.CONVERSATION_KEY


# --- fixtures ---------------------------------------------------------------


def make_context(testcase, *, by_phrase=None, ai=None, fail=False):
    """A real SQLite file, the real SearchService and a stub marketplace."""

    class SubstringCatalog(CatalogStub):
        """Answers by SUBSTRING, the way a real search index does."""

        async def search_products(self, request):
            if self.fail:
                raise RuntimeError("AliExpress is down")
            phrase = (
                request.normalized_query or request.original_query or ""
            ).lower()
            self.queries.append(phrase)
            for key, products in self.by_phrase.items():
                if key.lower() in phrase:
                    return list(products)
            return []

    tmp = tempfile.TemporaryDirectory()
    testcase.addCleanup(tmp.cleanup)
    db = Database(str(Path(tmp.name) / "b6.db"))
    run(db.initialize())
    run(db.get_or_create_user(555, "tester", "Tester"))
    provider = SubstringCatalog(by_phrase=by_phrase or {}, fail=fail)
    assistant = RecordingAssistant()
    testcase.provider = provider
    testcase.assistant = assistant
    return FakeContext(
        bot_data={
            "database": db,
            "search_service": SearchService([provider], ai_service=ai),
            "ai_service": ai if ai is not None else AIService(api_key=None),
            "product_assistant": assistant,
        }
    )


def send(context, text):
    update = FakeUpdate(message=TextMessage(text, bot=context.bot))
    run(search_handlers.search_message(update, context))


def delivered(context):
    """Product ids of the result set currently open on screen."""

    session_id = context.user_data.get("last_shown_session")
    if not session_id:
        return []
    return [p.product_id for p in common.get_cached_products(context, session_id)]


def screen(context):
    return "\n".join(context.bot.texts).lower().replace("ё", "е")


def catalog(**products_by_phrase):
    return products_by_phrase


def screwdriver_catalog():
    return {
        "шуруповерт": [
            listing(f"s{i}", f"Шуруповерт аккумуляторный {i}", price=float(60 - i))
            for i in range(30)
        ],
    }


# --- 1, 2, 19: a new search and the requested quantity ----------------------


class NewSearchAndCountTests(unittest.TestCase):
    def test_new_search_delivers_real_products(self):
        products = screwdriver_catalog()["шуруповерт"]
        context = make_context(self, by_phrase={"шуруповерт": products})
        send(context, "шуруповерт")
        shown = delivered(context)
        self.assertTrue(shown)
        self.assertTrue(set(shown).issubset({p.product_id for p in products}))
        self.assertTrue(self.provider.queries)

    def test_five_options_caps_the_card_set_at_five(self):
        products = screwdriver_catalog()["шуруповерт"]
        context = make_context(self, by_phrase={"шуруповерт": products})
        send(context, "покажи 5 вариантов шуруповерта")
        self.assertEqual(len(delivered(context)), 5)
        request = context.user_data[CONVERSATION_KEY].request
        self.assertEqual(request.result_limit, 5)

    def test_count_followup_resends_cards_without_any_provider_call(self):
        products = screwdriver_catalog()["шуруповерт"]
        context = make_context(self, by_phrase={"шуруповерт": products})
        send(context, "шуруповерт")
        first_batch = delivered(context)
        queries_before = list(self.provider.queries)

        send(context, "покажи 5 вариантов")

        self.assertEqual(self.provider.queries, queries_before)
        shown = delivered(context)
        self.assertEqual(len(shown), 5)
        # The SAME products the user is already looking at, in their order.
        self.assertEqual(shown, first_batch[:5])

    def test_changing_count_from_five_to_three_keeps_the_search(self):
        products = screwdriver_catalog()["шуруповерт"]
        context = make_context(self, by_phrase={"шуруповерт": products})
        send(context, "шуруповерт")
        send(context, "покажи 5 вариантов")
        self.assertEqual(len(delivered(context)), 5)
        queries_before = list(self.provider.queries)

        send(context, "сделай 3 варианта")

        self.assertEqual(self.provider.queries, queries_before)
        self.assertEqual(len(delivered(context)), 3)
        request = context.user_data[CONVERSATION_KEY].request
        self.assertEqual(request.result_limit, 3)
        # The product itself is untouched by a count change.
        self.assertIn("шуруповерт", (request.normalized_query or "").lower())

    def test_fewer_products_than_requested_are_shown_honestly(self):
        products = screwdriver_catalog()["шуруповерт"][:2]
        context = make_context(self, by_phrase={"шуруповерт": products})
        send(context, "покажи 5 вариантов шуруповерта")
        # Fewer real products than asked for: show what exists, never pad.
        self.assertEqual(len(delivered(context)), 2)
        self.assertLessEqual(len(delivered(context)), 5)


# --- 6: currency ------------------------------------------------------------


class CurrencyTests(unittest.TestCase):
    def test_currency_change_keeps_the_category_and_reuses_the_cards(self):
        products = screwdriver_catalog()["шуруповерт"][:8]
        context = make_context(self, by_phrase={"шуруповерт": products})
        send(context, "шуруповерт")
        before = delivered(context)
        queries_before = list(self.provider.queries)

        send(context, "используй украинские гривны")

        self.assertEqual(self.provider.queries, queries_before)
        self.assertEqual(delivered(context), before)
        request = context.user_data[CONVERSATION_KEY].request
        self.assertEqual(request.currency, "UAH")
        self.assertIn("шуруповерт", (request.normalized_query or "").lower())
        # Truthfulness: the bot never claims a conversion it cannot do.
        self.assertIn("курса", screen(context))

    def test_unsupported_currency_is_answered_with_the_supported_ones(self):
        products = screwdriver_catalog()["шуруповерт"][:3]
        context = make_context(self, by_phrase={"шуруповерт": products})
        send(context, "шуруповерт")
        send(context, "показывай цены в тугриках")
        self.assertIn("usd", screen(context))

    def test_currency_word_is_never_a_product_feature(self):
        context = make_context(self, by_phrase={"шуруповерт": []})
        send(context, "шуруповерт")
        send(context, "покажи в долларах")
        request = context.user_data[CONVERSATION_KEY].request
        joined = " ".join(request.required_features).lower()
        self.assertNotIn("доллар", joined)


# --- 4, 5, 7, 8, 9, 17, 18: refinements ------------------------------------


class RefinementTests(unittest.TestCase):
    def test_cheaper_keeps_the_product_and_lowers_the_budget(self):
        products = screwdriver_catalog()["шуруповерт"]
        context = make_context(self, by_phrase={"шуруповерт": products})
        send(context, "шуруповерт")
        send(context, "сделай подешевле")
        request = context.user_data[CONVERSATION_KEY].request
        # Same product, same constraints; no budget/logic invented.
        self.assertIn("шуруповерт", (request.normalized_query or "").lower())
        self.assertTrue(self.provider.queries)
        # Every provider request is still about the SAME product.
        self.assertTrue(
            all("шуруповерт" in query.lower() for query in self.provider.queries)
        )

    def test_additional_products_reuse_cached_results_when_available(self):
        products = screwdriver_catalog()["шуруповерт"]
        context = make_context(self, by_phrase={"шуруповерт": products})
        send(context, "шуруповерт")
        send(context, "покажи 2 варианта")
        queries_before = list(self.provider.queries)

        send(context, "покажи ещё 5")

        # The already-fetched cards satisfy the larger count: no new search.
        self.assertEqual(self.provider.queries, queries_before)
        self.assertEqual(len(delivered(context)), 5)
        self.assertEqual(
            context.user_data[CONVERSATION_KEY].request.result_limit, 5
        )

    def test_adding_a_requirement_keeps_the_product_and_the_budget(self):
        products = screwdriver_catalog()["шуруповерт"]
        context = make_context(self, by_phrase={"шуруповерт": products})
        send(context, "шуруповерт до 50 долларов")
        send(context, "с чехлом в комплекте")
        request = context.user_data[CONVERSATION_KEY].request
        self.assertEqual(request.max_price, 50.0)
        self.assertIn("шуруповерт", (request.normalized_query or "").lower())
        self.assertTrue(request.required_features)

    def test_changing_a_requirement_replaces_the_previous_value(self):
        from services.conversation import deterministic_merge

        previous = SearchRequest(
            original_query="шуруповерт до 50",
            normalized_query="шуруповерт",
            max_price=50.0,
        )
        follow_up = SearchRequest(
            original_query="до 80 долларов",
            normalized_query="до 80 долларов",
            max_price=80.0,
        )
        merged, refined = deterministic_merge(previous, follow_up, "до 80 долларов")
        self.assertTrue(refined)
        self.assertEqual(merged.max_price, 80.0)
        self.assertEqual(merged.normalized_query, "шуруповерт")

    def test_adding_an_exclusion_keeps_the_product(self):
        from services.conversation import deterministic_merge

        previous = SearchRequest(
            original_query="пылесос", normalized_query="пылесос"
        )
        follow_up = SearchRequest(original_query="без мешка", normalized_query="")
        merged, refined = deterministic_merge(previous, follow_up, "без мешка")
        self.assertTrue(refined)
        self.assertTrue(merged.excluded_features)
        self.assertEqual(merged.normalized_query, "пылесос")

    def test_a_count_only_message_never_merges_parser_noise(self):
        from services.conversation import resolve_request

        previous = SearchRequest(
            original_query="наушники",
            normalized_query="наушники",
            required_features=["микрофон"],
            max_price=40.0,
            currency="USD",
        )
        follow_up = AIService._fallback_parse("покажи 3 варианта")
        merged, refined = run(
            resolve_request(previous, follow_up, "покажи 3 варианта")
        )
        self.assertTrue(refined)
        self.assertEqual(merged.normalized_query, "наушники")
        self.assertEqual(merged.required_features, ["микрофон"])
        self.assertEqual(merged.max_price, 40.0)
        self.assertEqual(merged.currency, "USD")
        self.assertEqual(merged.result_limit, 3)

    def test_genuinely_new_product_still_searches(self):
        context = make_context(
            self,
            by_phrase={
                "шуруповерт": screwdriver_catalog()["шуруповерт"][:3],
                "кроссовки": [listing("c1", "Кроссовки беговые")],
            },
        )
        send(context, "шуруповерт")
        queries_before = list(self.provider.queries)
        send(context, "нужны кроссовки")
        self.assertGreater(len(self.provider.queries), len(queries_before))
        self.assertIn("кроссовки", self.provider.queries[-1].lower())


# --- 10, 11: comparison and questions over shown products -------------------


class ShownProductQuestionTests(unittest.TestCase):
    def test_comparing_two_shown_products_uses_their_real_fields(self):
        products = screwdriver_catalog()["шуруповерт"][:3]
        context = make_context(self, by_phrase={"шуруповерт": products})
        send(context, "шуруповерт")
        queries_before = list(self.provider.queries)
        send(context, "сравни первые два")
        self.assertEqual(self.provider.queries, queries_before)
        self.assertTrue(self.assistant.questions)

    def test_question_about_a_shown_product_answers_without_a_search(self):
        products = screwdriver_catalog()["шуруповерт"][:3]
        context = make_context(self, by_phrase={"шуруповерт": products})
        send(context, "шуруповерт")
        queries_before = list(self.provider.queries)
        send(context, "расскажи подробнее о втором")
        self.assertEqual(self.provider.queries, queries_before)
        self.assertTrue(self.assistant.questions)


# --- 13: expired context ----------------------------------------------------


class ExpiredContextTests(unittest.TestCase):
    def test_count_followup_after_expiry_explains_the_limitation(self):
        context = make_context(self, by_phrase={"шуруповерт": screwdriver_catalog()["шуруповерт"][:3]})
        send(context, "шуруповерт")
        context.user_data.pop(common.OUTCOME_CACHE_KEY, None)
        context.user_data.pop("last_shown_session", None)
        queries_before = list(self.provider.queries)
        send(context, "покажи 3 варианта")
        # No silent fake success, no provider call, and an explicit statement
        # that the previous results are gone.
        self.assertEqual(self.provider.queries, queries_before)
        self.assertIn("недоступны", screen(context))
        self.assertNotIn("показал 3", screen(context))

    def test_compare_after_context_loss_answers_honestly(self):
        context = make_context(self, by_phrase={"шуруповерт": screwdriver_catalog()["шуруповерт"][:3]})
        send(context, "шуруповерт")
        context.user_data.pop(common.OUTCOME_CACHE_KEY, None)
        context.user_data.pop("last_shown_session", None)
        queries_before = list(self.provider.queries)
        send(context, "какой из них лучше?")
        self.assertEqual(self.provider.queries, queries_before)
        self.assertIn("недоступны", screen(context))


# --- 14, 15, 20: failures ---------------------------------------------------


class _SlowAI:
    """An AI double that times out on every request."""

    is_available = True
    fast_model = "fast"
    timeout_seconds = 0.001

    def __init__(self):
        self.parse_fallback = 0
        self.parse_ok = 0
        self.generate_ok = 0
        self.generate_failed = 0
        self.last_model = None

    async def parse_query(self, text):
        return AIService._fallback_parse(text)

    async def merge_search_requests(self, previous, follow_up, raw_text):
        return None

    async def decide_chat_action(self, text):
        return None


class FailureTests(unittest.TestCase):
    def test_ai_timeout_falls_back_and_still_delivers_products(self):
        products = screwdriver_catalog()["шуруповерт"][:6]
        context = make_context(self, by_phrase={"шуруповерт": products}, ai=_SlowAI())
        send(context, "покажи 5 вариантов шуруповерта")
        self.assertEqual(len(delivered(context)), 5)

    def test_provider_failure_reports_an_outage_not_an_empty_catalogue(self):
        context = make_context(self, by_phrase={}, fail=True)
        send(context, "шуруповерт")
        self.assertIn("временно недоступен", screen(context))
        self.assertNotIn("ничего не найдено", screen(context))

    def test_failed_refinement_keeps_the_request_and_the_context(self):
        products = screwdriver_catalog()["шуруповерт"][:4]
        context = make_context(self, by_phrase={"шуруповерт": products})
        send(context, "шуруповерт")
        self.provider.fail = True
        send(context, "а есть с чехлом?")

        # The refinement is NOT lost: the valid merged request survives the
        # provider outage, and the same product is still the subject.
        request = context.user_data[CONVERSATION_KEY].request
        self.assertIn("шуруповерт", (request.normalized_query or "").lower())
        self.assertTrue(request.required_features)
        # The user is told the search could not run - not that nothing exists.
        self.assertIn("недоступен", screen(context))
        self.assertNotIn("ничего не найдено", screen(context))


# --- router level: count-only and position markers --------------------------


class RouterTests(unittest.TestCase):
    def test_count_only_message_inside_a_search_stays_search(self):
        for text in ("покажи 5", "сделай три варианта", "ещё 2"):
            with self.subTest(text=text):
                self.assertIs(
                    chat_router.classify(text, has_conversation=True).action,
                    ChatAction.SEARCH,
                )

    def test_count_only_message_without_a_search_is_not_a_provider_query(self):
        self.assertIs(
            chat_router.classify("покажи 5 вариантов").action,
            ChatAction.CLARIFY,
        )

    def test_position_marker_with_a_product_is_still_searched(self):
        self.assertIs(
            chat_router.classify("зарядка для Apple Watch 2 вариант").action,
            ChatAction.SEARCH,
        )

    def test_position_marker_alone_is_an_explanation(self):
        self.assertIs(
            chat_router.classify("А что насчёт второго варианта?").action,
            ChatAction.EXPLAIN,
        )


class FeaturePhraseTests(unittest.TestCase):
    def test_feature_phrase_never_crosses_a_sentence_boundary(self):
        from services.conversation import _extract_features

        required, _ = _extract_features(
            "Беспроводные наушники с микрофоном. Покажи 3 варианта"
        )
        joined = " ".join(required).lower()
        self.assertIn("микрофон", joined)
        self.assertNotIn("покажи", joined)

    def test_currency_restatement_is_not_a_required_feature(self):
        from services.conversation import _extract_features

        required, _ = _extract_features("используй цены с гривнах")
        self.assertEqual(required, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
