"""B3: honest comparison routing and honest search-failure reporting.

Offline only: the real ``handlers.search`` entry points run against the
stage-47 fakes (stub marketplace, recording assistant, real SQLite file),
so every test below observes the actual user-facing outcome - the message
on screen and whether the provider was touched - never just a label.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from database.database import Database
from handlers import search as search_handlers
from models.product import Marketplace, Product
from models.search_request import SearchRequest
from services import chat_router
from services.ai_service import AIService
from services.chat_router import ChatAction
from services.search_service import SearchOutcome, SearchService

from tests.test_stage47_conversational import (
    CatalogStub,
    FakeContext,
    FakeUpdate,
    RecordingAssistant,
    TextMessage,
    listing,
    run,
)
from tests.test_handlers_ui import make_context as make_ui_context
from tests.test_handlers_ui import make_outcome


def make_context(testcase, *, provider=None, assistant=None):
    testcase._tmp = tempfile.TemporaryDirectory()
    testcase.addCleanup(testcase._tmp.cleanup)
    db = Database(str(Path(testcase._tmp.name) / "b3.db"))
    run(db.initialize())
    run(db.get_or_create_user(555, "tester", "Tester"))
    provider = provider if provider is not None else CatalogStub(
        by_phrase={
            "шуруповерт": [
                listing("d1", "Шуруповёрт аккумуляторный 12V", price=40.0),
                listing("d2", "Шуруповёрт сетевой 220V", price=25.0),
            ],
        }
    )
    assistant = assistant if assistant is not None else RecordingAssistant()
    testcase.assistant = assistant
    testcase.provider = provider
    return FakeContext(
        bot_data={
            "database": db,
            "search_service": SearchService([provider]),
            "ai_service": AIService(api_key=None),
            "product_assistant": assistant,
        }
    )


def send(context, text):
    update = FakeUpdate(message=TextMessage(text, bot=context.bot))
    run(search_handlers.search_message(update, context))


def screen(context):
    return "\n".join(
        list(context.bot.texts)
        + [str(kwargs.get("caption") or "") for _, kwargs in context.bot.photos]
    ).lower().replace("ё", "е")

class CompareRoutingTests(unittest.TestCase):
    def test_compare_with_valid_session_uses_shown_products(self):
        context = make_context(self)
        send(context, "шуруповерт")
        self.assertIn("last_shown_session", context.user_data)
        queries_before = list(self.provider.queries)
        send(context, "какой из них лучше?")
        self.assertTrue(self.assistant.questions)
        question, count = self.assistant.questions[-1]
        self.assertIn("лучше", question.lower())
        self.assertGreaterEqual(count, 1)
        self.assertEqual(self.provider.queries, queries_before)

    def test_explain_with_valid_session_uses_shown_products(self):
        context = make_context(self)
        send(context, "шуруповерт")
        send(context, "расскажи подробнее об этом товаре")
        self.assertTrue(self.assistant.questions)

    def test_compare_without_session_answers_honestly(self):
        context = make_context(self)
        for text in ("какой из них лучше?", "какой вариант выбрать?"):
            with self.subTest(text=text):
                send(context, text)
                self.assertIn("недоступны", screen(context))
        self.assertFalse(self.provider.queries)
        self.assertEqual(self.assistant.questions, [])

    def test_explain_without_session_answers_honestly(self):
        context = make_context(self)
        send(context, "расскажи подробнее об этом товаре")
        self.assertIn("недоступны", screen(context))
        self.assertFalse(self.provider.queries)
        self.assertEqual(self.assistant.questions, [])

    def test_compare_after_session_eviction_answers_honestly(self):
        context = make_context(self)
        send(context, "шуруповерт")
        self.assertTrue(self.provider.queries)
        context.user_data.pop("last_shown_session", None)
        queries_before = list(self.provider.queries)
        send(context, "чем они отличаются?")
        self.assertIn("недоступны", screen(context))
        self.assertEqual(self.provider.queries, queries_before)
        self.assertEqual(self.assistant.questions, [])

    def test_genuine_new_product_request_still_searches(self):
        context = make_context(self)
        send(context, "шуруповерт")
        queries_before = list(self.provider.queries)
        send(context, "набор отверток")
        self.assertGreater(len(self.provider.queries), len(queries_before))
        self.assertIn("отверт", self.provider.queries[-1].lower())

    def test_b1_followup_still_refines_previous_query(self):
        context = make_context(self)
        send(context, "шуруповерт")
        before = dict(context.user_data["search_conversation"].request)
        send(context, "подешевле")
        after = context.user_data["search_conversation"].request
        self.assertIn("шуруповерт", (after.normalized_query or "").lower())
        self.assertNotEqual(before, {})


class FailureReportingTests(unittest.TestCase):
    """§B3: the message on screen tells GENUINE emptiness and provider
    failure apart - checked against what is actually sent, not a label."""

    def _render(self, outcome):
        context = make_ui_context()
        session_id = run(search_handlers.send_search_results(context, 47, outcome))
        return context, session_id

    def test_zero_products_success_keeps_nothing_found_message(self):
        outcome = make_outcome(
            count=0, searched_marketplaces=["AliExpress"]
        )
        context, session_id = self._render(outcome)
        self.assertIsNone(session_id)
        self.assertEqual(
            context.bot.texts, [search_handlers.EMPTY_RESULT_TEXT]
        )

    def test_unconfigured_provider_reports_unavailable(self):
        outcome = make_outcome(
            count=0,
            searched_marketplaces=[],
            unavailable_marketplaces=["AliExpress"],
        )
        context, session_id = self._render(outcome)
        self.assertIsNone(session_id)
        self.assertEqual(
            context.bot.texts, [search_handlers.SEARCH_UNAVAILABLE_TEXT]
        )

    def test_provider_exception_reports_unavailable(self):
        outcome = make_outcome(
            count=0,
            searched_marketplaces=[],
            failed_marketplaces=["AliExpress"],
        )
        context, session_id = self._render(outcome)
        self.assertIsNone(session_id)
        self.assertEqual(
            context.bot.texts, [search_handlers.SEARCH_UNAVAILABLE_TEXT]
        )

    def test_mixed_failure_with_results_renders_cards_normally(self):
        outcome = make_outcome(
            count=2,
            searched_marketplaces=["AliExpress"],
            failed_marketplaces=["OtherMarket"],
        )
        context, session_id = self._render(outcome)
        self.assertIsNotNone(session_id)
        self.assertNotIn(
            search_handlers.SEARCH_UNAVAILABLE_TEXT, context.bot.texts
        )
        self.assertNotIn(search_handlers.EMPTY_RESULT_TEXT, context.bot.texts)

    def test_all_failures_reports_unavailable(self):
        outcome = make_outcome(
            count=0,
            searched_marketplaces=[],
            failed_marketplaces=["AliExpress"],
            unavailable_marketplaces=["OtherMarket"],
        )
        context, session_id = self._render(outcome)
        self.assertIsNone(session_id)
        self.assertEqual(
            context.bot.texts, [search_handlers.SEARCH_UNAVAILABLE_TEXT]
        )

    def test_filtered_to_zero_still_says_nothing_found(self):
        outcome = make_outcome(
            count=0,
            searched_marketplaces=["AliExpress"],
            products_before_filtering=48,
            products_dropped_as_irrelevant=48,
        )
        context, session_id = self._render(outcome)
        self.assertIsNone(session_id)
        self.assertEqual(
            context.bot.texts, [search_handlers.EMPTY_RESULT_TEXT]
        )

    def test_successful_render_shows_cards(self):
        outcome = make_outcome(count=2, searched_marketplaces=["AliExpress"])
        context, session_id = self._render(outcome)
        self.assertIsNotNone(session_id)
        self.assertNotIn(
            search_handlers.SEARCH_UNAVAILABLE_TEXT, context.bot.texts
        )
        self.assertNotIn(search_handlers.EMPTY_RESULT_TEXT, context.bot.texts)


class IntentPreservationTests(unittest.TestCase):
    def test_required_wordings_route_to_compare_or_explain(self):
        for text, expected in (
            ("Какой из них лучше?", ChatAction.COMPARE),
            ("Какой вариант выбрать?", ChatAction.COMPARE),
            ("Чем они отличаются?", ChatAction.COMPARE),
            ("Расскажи подробнее об этом товаре", ChatAction.EXPLAIN),
            ("Какой из предыдущих вариантов лучше?", ChatAction.COMPARE),
        ):
            with self.subTest(text=text):
                self.assertIs(
                    chat_router.classify(text, has_shown_results=False).action,
                    expected,
                )

    def test_refinements_and_new_products_stay_search(self):
        for text in (
            "а дешевле?",
            "подешевле",
            "а плазморезка?",
            "набор отверток",
            "шуруповерт",
        ):
            with self.subTest(text=text):
                self.assertIs(
                    chat_router.classify(text, has_shown_results=False).action,
                    ChatAction.SEARCH,
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
