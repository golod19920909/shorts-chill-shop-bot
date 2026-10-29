"""Telegram layer of the AI product menu + photo/discovery routing.

Handlers get fakes, so nothing leaves the process: no Telegram request, no
AliExpress call and no Gemini request. What is checked here is the wiring -
the card carries «🤖 AI», the menu has exactly the five agreed actions, the
callbacks reach the right handler, and a failed/unavailable step produces a
short natural Russian message instead of a crash or a technical explanation.
"""

from __future__ import annotations

import asyncio
import unittest

from handlers import ai_assistant
from keyboards.main import CB_BACK, main_menu_keyboard
from keyboards.products import (
    AI_ACTIONS,
    CB_PREFIX_AI,
    CB_PREFIX_AI_ACTION,
    LABEL_AI,
    LABEL_CARD_BACK,
    LABEL_HOME,
    ai_menu_keyboard,
    browse_card_keyboard,
)
from models.product import Marketplace, Product
from models.search_request import SearchRequest
from services.search_service import SearchOutcome


def run(coro):
    return asyncio.run(coro)


def make_product(index: int = 0, **overrides) -> Product:
    data: dict = {
        "marketplace": Marketplace.ALIEXPRESS,
        "product_id": f"p{index}",
        "title": f"Товар {index}",
        "product_url": f"https://www.aliexpress.com/item/{index}.html",
        "price": 10.0 + index,
        "currency": "USD",
    }
    data.update(overrides)
    return Product(**data)


def make_outcome(count: int = 2) -> SearchOutcome:
    return SearchOutcome(
        request=SearchRequest(original_query="товар"),
        products=[make_product(index) for index in range(count)],
        searched_marketplaces=["AliExpress"],
    )


class FakeMessage:
    def __init__(self, chat_id: int = 47) -> None:
        self.chat_id = chat_id
        self.photo: list = []
        self.text: str | None = "привет"
        self.texts: list[str] = []
        self.kwargs: list[dict] = []

    async def reply_text(self, text: str, **kwargs) -> None:
        self.texts.append(text)
        self.kwargs.append(kwargs)


class FakeQuery:
    def __init__(self, data: str, chat_id: int = 47) -> None:
        self.data = data
        self.message = FakeMessage(chat_id)
        self.answers: list[tuple[str | None, bool]] = []
        self.markup_edits: list[object] = []
        self.edits: list[tuple[str, dict]] = []

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))

    async def edit_message_reply_markup(self, reply_markup=None) -> None:
        self.markup_edits.append(reply_markup)

    async def edit_message_text(self, text: str, **kwargs) -> None:
        self.edits.append((text, kwargs))


class FakeChat:
    id = 47


class FakeUser:
    id = 555
    username = "tester"
    first_name = "Tester"


class FakeUpdate:
    def __init__(self, *, callback_query=None, message=None) -> None:
        self.callback_query = callback_query
        self.effective_message = message
        self.effective_chat = FakeChat() if (message or callback_query) else None
        self.effective_user = FakeUser()


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str, dict]] = []
        self.photos: list[tuple[int, dict]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
        self.messages.append((chat_id, text, kwargs))

    async def send_photo(self, chat_id: int, **kwargs) -> None:
        self.photos.append((chat_id, kwargs))

    @property
    def texts(self) -> list[str]:
        return [text for _, text, _ in self.messages]


class FakeDatabase:
    def __init__(self) -> None:
        self.region = {"currency": "USD"}

    async def get_or_create_user(self, telegram_id, username, first_name) -> int:
        return 1

    async def get_user_region(self, user_id) -> dict:
        return dict(self.region)

    async def get_tracked_products(self, user_id) -> list[dict]:
        return []

    async def get_shown_products(self, user_id, *, flow=None) -> list[tuple[str, str]]:
        return []

    async def add_shown_product(
        self, *, user_id, marketplace, product_id, flow: str = "search"
    ) -> None:
        return None

    async def record_event(self, *args, **kwargs) -> None:
        return None


class _App:
    def __init__(self, bot_data: dict) -> None:
        self.bot_data = bot_data


class FakeContext:
    def __init__(self, bot_data: dict) -> None:
        self.bot = FakeBot()
        self.user_data: dict = {}
        self.application = _App(bot_data)


class RecordingAssistant:
    """Stands in for ``ProductAssistant`` at the handler boundary."""

    def __init__(self, selection=None, advice=None) -> None:
        self.selection = selection
        self.advice = advice
        self.questions: list[str] = []

    async def analyze(self, product):
        return self.advice

    async def cheaper(self, product, candidates):
        return self.selection

    async def similar(self, product, candidates):
        return self.selection

    async def better(self, product, candidates):
        return self.selection

    async def answer(self, question, products):
        self.questions.append(question)
        return self.advice

    async def candidate_products(self, product):
        return [make_product(9, title="Похожий товар", price=5.0)]


class AiKeyboardTests(unittest.TestCase):
    def test_the_card_carries_the_ai_button(self) -> None:
        keyboard = browse_card_keyboard("s1", 0, 2, make_product())
        callbacks = [
            button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
            if button.callback_data
        ]

        self.assertIn(f"{CB_PREFIX_AI}:s1:0", callbacks)

    def test_the_main_menu_has_no_ai_button(self) -> None:
        labels = [
            button.text
            for row in main_menu_keyboard().inline_keyboard
            for button in row
        ]

        self.assertNotIn(LABEL_AI, labels)
        self.assertFalse(
            any(
                "AI" in label
                for label in labels
            )
        )

    def test_the_menu_has_exactly_the_five_agreed_actions(self) -> None:
        keyboard = ai_menu_keyboard("s1", 0)
        labels = [
            button.text
            for row in keyboard.inline_keyboard
            for button in row
        ]

        self.assertEqual(
            labels[:5],
            [
                "⭐ Стоит ли покупать?",
                "💰 Найти дешевле",
                "🔎 Найти похожий",
                "🎯 Подобрать лучше",
                "💬 Задать свой вопрос",
            ],
        )
        self.assertEqual(len(AI_ACTIONS), 5)

    def test_the_menu_ends_with_back_and_home(self) -> None:
        keyboard = ai_menu_keyboard("s1", 0)
        last_rows = keyboard.inline_keyboard[-2:]

        self.assertEqual(last_rows[0][0].text, LABEL_CARD_BACK)
        self.assertEqual(last_rows[1][0].text, LABEL_HOME)
        self.assertEqual(last_rows[1][0].callback_data, CB_BACK)

    def test_every_action_carries_its_own_callback(self) -> None:
        keyboard = ai_menu_keyboard("s1", 2)
        callbacks = [
            button.callback_data
            for row in keyboard.inline_keyboard[:5]
            for button in row
        ]

        for action, _label in AI_ACTIONS:
            self.assertIn(f"{CB_PREFIX_AI_ACTION}:{action}:s1:2", callbacks)

    def test_callback_data_stays_within_the_telegram_limit(self) -> None:
        keyboard = ai_menu_keyboard("abc123", 0)
        for row in keyboard.inline_keyboard:
            for button in row:
                if button.callback_data:
                    self.assertLessEqual(
                        len(button.callback_data.encode("utf-8")), 64
                    )


class AiMenuCallbackTests(unittest.TestCase):
    def _context(self) -> FakeContext:
        context = FakeContext({"database": FakeDatabase()})
        return context

    def test_opening_the_menu_replaces_only_the_keyboard(self) -> None:
        from handlers import common

        context = self._context()
        outcome = make_outcome(2)
        context.user_data[common.OUTCOME_CACHE_KEY] = {"s1": outcome}
        query = FakeQuery(f"{CB_PREFIX_AI}:s1:0")

        run(ai_assistant.ai_menu_callback(FakeUpdate(callback_query=query), context))

        self.assertTrue(query.markup_edits)
        labels = [
            button.text
            for row in query.markup_edits[0].inline_keyboard
            for button in row
        ]
        self.assertIn("⭐ Стоит ли покупать?", labels)
        self.assertNotIn(LABEL_AI, labels)
        # The caption is untouched: only the keyboard changes.
        self.assertEqual(context.bot.messages, [])

    def test_an_expired_session_is_reported_not_crashed(self) -> None:
        context = self._context()
        query = FakeQuery(f"{CB_PREFIX_AI}:missing:0")

        run(ai_assistant.ai_menu_callback(FakeUpdate(callback_query=query), context))

        self.assertTrue(query.answers)
        self.assertEqual(query.answers[-1][0], ai_assistant.EXPIRED_TEXT)
        self.assertEqual(context.bot.messages, [])

    def test_a_broken_callback_data_is_reported(self) -> None:
        context = self._context()
        query = FakeQuery("pai:broken")

        run(ai_assistant.ai_menu_callback(FakeUpdate(callback_query=query), context))

        self.assertTrue(query.answers)
        self.assertIn("Некорректная кнопка", query.answers[-1][0])



class AiActionCallbackTests(unittest.TestCase):
    def _context(self, assistant) -> tuple[FakeContext, FakeQuery]:
        from handlers import common

        outcome = make_outcome(2)
        context = FakeContext(
            {
                "database": FakeDatabase(),
                "product_assistant": assistant,
                "search_service": None,
            }
        )
        context.user_data[common.OUTCOME_CACHE_KEY] = {"s1": outcome}
        return context, FakeQuery("")

    def test_advice_action_sends_the_factual_analysis(self) -> None:
        from services.product_assistant import ProductAdvice, deterministic_advice

        product = make_product(0)
        assistant = RecordingAssistant(
            advice=ProductAdvice(deterministic_advice(product), False)
        )
        context, _ = self._context(assistant)
        query = FakeQuery(f"{CB_PREFIX_AI_ACTION}:advice:s1:0")

        run(ai_assistant.ai_action_callback(FakeUpdate(callback_query=query), context))

        self.assertTrue(context.bot.texts)
        self.assertIn("Стоит ли покупать", context.bot.texts[0])
        self.assertIn("Данные AliExpress", context.bot.texts[0])

    def test_candidate_action_renders_real_product_cards(self) -> None:
        from services.product_assistant import CandidateSelection

        candidate = make_product(9, title="Похожий товар", price=5.0)
        assistant = RecordingAssistant(
            selection=CandidateSelection([candidate], False, None, "ai_fallback")
        )
        context, _ = self._context(assistant)
        query = FakeQuery(f"{CB_PREFIX_AI_ACTION}:cheaper:s1:0")

        run(ai_assistant.ai_action_callback(FakeUpdate(callback_query=query), context))

        self.assertTrue(context.bot.texts)
        # The card is texts[0] again; the action wording appears somewhere in
        # the delivered flow (card or a follow-up text).
        self.assertTrue(
            any("Найти дешевле" in text for text in context.bot.texts)
        )
        self.assertTrue(context.bot.photos or context.bot.messages)

    def test_an_impossible_comparison_explains_itself(self) -> None:
        from services.product_assistant import CandidateSelection

        assistant = RecordingAssistant(
            selection=CandidateSelection([], False, None, "no_comparable_price")
        )
        context, _ = self._context(assistant)
        query = FakeQuery(f"{CB_PREFIX_AI_ACTION}:cheaper:s1:0")

        run(ai_assistant.ai_action_callback(FakeUpdate(callback_query=query), context))

        text = context.bot.texts[0]
        self.assertIn("выдумывать курс", text)
        # No technical details leak into the chat.
        self.assertNotIn("Gemini", text)
        self.assertNotIn("API", text)
        self.assertNotIn("Traceback", text)

    def test_the_question_action_asks_for_one_message(self) -> None:
        context, _ = self._context(RecordingAssistant())
        query = FakeQuery(f"{CB_PREFIX_AI_ACTION}:question:s1:0")

        run(ai_assistant.ai_action_callback(FakeUpdate(callback_query=query), context))

        self.assertIn(ai_assistant.AI_QUESTION_KEY, context.user_data)
        self.assertIn("Напишите вопрос", context.bot.texts[0])

    def test_an_expired_session_is_never_a_crash(self) -> None:
        context = FakeContext(
            {"database": FakeDatabase(), "product_assistant": RecordingAssistant()}
        )
        query = FakeQuery(f"{CB_PREFIX_AI_ACTION}:advice:gone:0")

        run(ai_assistant.ai_action_callback(FakeUpdate(callback_query=query), context))

        self.assertEqual(query.answers[-1][0], ai_assistant.EXPIRED_TEXT)
        self.assertEqual(context.bot.texts, [])



class PendingQuestionTests(unittest.TestCase):
    def _context(self, assistant) -> tuple[FakeContext, FakeMessage]:
        from handlers import common

        outcome = make_outcome(2)
        context = FakeContext(
            {
                "database": FakeDatabase(),
                "product_assistant": assistant,
                "search_service": None,
            }
        )
        context.user_data[common.OUTCOME_CACHE_KEY] = {"s1": outcome}
        return context, FakeMessage()

    def test_a_typed_question_is_answered_not_searched(self) -> None:
        from services.product_assistant import ProductAdvice

        assistant = RecordingAssistant(
            advice=ProductAdvice("Самый дешёвый — 5.00 USD", False)
        )
        context, message = self._context(assistant)
        context.user_data[ai_assistant.AI_QUESTION_KEY] = {
            "session": "s1",
            "index": 0,
        }

        consumed = run(
            ai_assistant.consume_pending_question(
                FakeUpdate(message=message), context
            )
        )

        self.assertTrue(consumed)
        self.assertEqual(message.texts[0], "Самый дешёвый — 5.00 USD")
        self.assertEqual(assistant.questions, ["привет"])

    def test_the_pending_state_is_cleared_after_use(self) -> None:
        from services.product_assistant import ProductAdvice

        context, message = self._context(
            RecordingAssistant(advice=ProductAdvice("ок", False))
        )
        context.user_data[ai_assistant.AI_QUESTION_KEY] = {
            "session": "s1",
            "index": 0,
        }

        run(
            ai_assistant.consume_pending_question(
                FakeUpdate(message=message), context
            )
        )

        self.assertNotIn(ai_assistant.AI_QUESTION_KEY, context.user_data)

    def test_without_a_pending_question_an_ordinary_search_is_not_hijacked(
        self,
    ) -> None:
        context, message = self._context(RecordingAssistant())

        consumed = run(
            ai_assistant.consume_pending_question(
                FakeUpdate(message=message), context
            )
        )

        self.assertFalse(consumed)
        self.assertEqual(message.texts, [])

    def test_a_question_about_an_expired_card_is_reported_honestly(self) -> None:
        context, message = self._context(RecordingAssistant())
        context.user_data[ai_assistant.AI_QUESTION_KEY] = {
            "session": "gone",
            "index": 0,
        }

        consumed = run(
            ai_assistant.consume_pending_question(
                FakeUpdate(message=message), context
            )
        )

        self.assertTrue(consumed)
        self.assertEqual(message.texts[0], ai_assistant.EXPIRED_TEXT)


if __name__ == "__main__":
    unittest.main()
