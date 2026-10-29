"""Real Telegram dispatch of the callbacks a human presses (§26-§28).

Unit tests of single handlers proved nothing when a button's data was not
matched by ANY registered pattern - the production symptom was «нажал
«❤️ Избранное» и ничего не произошло», while every handler test passed.

This module drives the REAL ``Application`` built by
``bot.build_application`` (all production handlers, their patterns and
their order) with real ``telegram.Update`` objects through
``Application.process_update`` - the same code path a Telegram press
takes, minus the network. What it therefore proves together:

    callback data of a REAL keyboard
    -> registered pattern that matches it
    -> the intended handler (not the catch-all)
    -> database lookup / session cache
    -> message edit or send (and the photo-message fallback)

No request leaves the process: the bot is a ``telegram.Bot`` subclass
whose API methods record their calls instead of sending them, and the
database is a temporary file.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from telegram import Bot, Update
from telegram.error import BadRequest

from bot import build_application
from config.settings import MarketplaceCredentials, Settings
from handlers import ai_assistant, common
from handlers.favorites import EMPTY_FAVORITES_TEXT
from handlers.search import browse_callback
from handlers.start import menu_callback, nav_back_callback, stale_callback
from keyboards.main import CB_BACK, CB_FAVORITES, main_menu_keyboard
from keyboards.products import (
    CB_PREFIX_AI,
    CB_PREFIX_AI_ACTION,
    CB_PREFIX_NAV,
    LABEL_CARD_NEXT,
    LABEL_CARD_RESTART,
    browse_card_keyboard,
)
from models.product import Marketplace, Product
from models.search_request import SearchRequest
from services.search_service import SearchOutcome

FAKE_TOKEN = "123456789:FAKE-TOKEN-FOR-TESTS"

#: The button a manual tester presses to open the favorites screen.
FAVORITES_LABEL_MARK = "Избранное"
#: Card navigation labels of a real product card.
NEXT_LABEL_MARK = "Следующий"
BACK_LABEL_MARK = "Назад"


class OfflineBot(Bot):
    """A real ``telegram.Bot`` that records API calls instead of sending.

    Subclassing (instead of monkey-patching an instance) is required by
    PTB: ``TelegramObject`` freezes its instance attributes after
    construction, which is also why every recorded attribute here starts
    with an underscore.
    """

    def __init__(self) -> None:
        super().__init__(token=FAKE_TOKEN)
        self._calls: list[tuple] = []
        self._fail_next_edit = False

    @property
    def calls(self) -> list[tuple]:
        return list(self._calls)

    def reset(self) -> None:
        self._calls.clear()
        self._fail_next_edit = False

    def of(self, name: str) -> list[tuple]:
        return [call for call in self._calls if call[0] == name]

    async def initialize(self) -> None:
        self._calls.append(("initialize", (), {}))

    async def set_my_commands(self, *args, **kwargs):
        self._calls.append(("set_my_commands", args, kwargs))
        return True

    async def send_message(self, *args, **kwargs):
        self._calls.append(("send_message", args, kwargs))
        return None

    async def edit_message_text(self, *args, **kwargs):
        if self._fail_next_edit:
            # Exactly what Telegram answers for a PHOTO message.
            self._fail_next_edit = False
            raise BadRequest("There is no text in the message to edit")
        self._calls.append(("edit_message_text", args, kwargs))
        return None

    async def answer_callback_query(self, *args, **kwargs):
        self._calls.append(("answer_callback_query", args, kwargs))
        return True

    async def delete_message(self, *args, **kwargs):
        self._calls.append(("delete_message", args, kwargs))
        return True

    async def send_photo(self, *args, **kwargs):
        self._calls.append(("send_photo", args, kwargs))
        return None

    async def set_chat_menu_button(self, *args, **kwargs):
        self._calls.append(("set_chat_menu_button", args, kwargs))
        return True


def run(coro):
    return asyncio.run(coro)


def text_of(call: tuple) -> str:
    """The message text of a recorded send/edit, wherever it sits."""

    _, args, kwargs = call
    if isinstance(kwargs.get("text"), str):
        return kwargs["text"]
    for value in args:
        if isinstance(value, str):
            return value
    return ""


def data_for(keyboard, label_mark: str) -> str:
    """Callback data of the REAL keyboard button whose label contains text."""

    for row in keyboard.inline_keyboard:
        for button in row:
            if label_mark in button.text and button.callback_data:
                return button.callback_data
    raise AssertionError(f"No button labelled {label_mark!r} in the keyboard")


def callback_update(
    bot: Bot,
    data: str,
    *,
    text: str = "текст сообщения",
    photo: bool = False,
    message_id: int = 9,
    update_id: int = 1,
) -> Update:
    """A real ``Update`` carrying one callback press (offline)."""

    message: dict = {
        "message_id": message_id,
        "date": int(datetime.now(timezone.utc).timestamp()),
        "chat": {"id": 555, "type": "private"},
    }
    if photo:
        message["photo"] = [
            {
                "file_id": "photo-file",
                "file_unique_id": "photo-unique",
                "width": 1,
                "height": 1,
            }
        ]
    else:
        message["text"] = text

    return Update.de_json(
        {
            "update_id": update_id,
            "callback_query": {
                "id": f"q{update_id}",
                "from": {"id": 777, "is_bot": False, "first_name": "Tester"},
                "chat_instance": "ci",
                "data": data,
                "message": message,
            },
        },
        bot,
    )


def matched_handler(application, update: Update):
    """The handler PTB would run for this update.

    Same rule as ``Application.process_update``: the first handler of any
    group whose ``check_update`` does not answer ``None``/``False`` wins.
    """

    for handlers in [group.copy() for group in application.handlers.values()]:
        for handler in handlers:
            check = handler.check_update(update)
            if check is None or check is False:
                continue
            return handler
    return None


class DispatchTestCase(unittest.TestCase):
    """Base: a real Application + offline bot + temporary database."""

    def setUp(self) -> None:
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp_dir.cleanup)
        self.bot = OfflineBot()
        settings = Settings(
            bot_token=FAKE_TOKEN,
            gemini_api_key=None,
            database_path=str(Path(self._tmp_dir.name) / "dispatch.db"),
            log_level="CRITICAL",
            aliexpress=MarketplaceCredentials(),
        )
        self.application = build_application(settings)
        self.application.bot = self.bot
        # Polling bootstrapping would initialize a second, real Bot; the
        # dispatch path only needs the initialization flag and a database
        # whose tables exist - both are set up here explicitly.
        run(self.application.bot_data["database"].init_db())
        self.application._initialized = True

    def press(self, update: Update, *, fail_next_edit: bool = False) -> None:
        """Run one press through the registered handlers, offline.

        ``fail_next_edit`` makes the next edit attempt fail exactly like
        Telegram does for a photo message (set AFTER the reset).
        """

        self.bot.reset()
        self.bot._fail_next_edit = fail_next_edit
        run(self.application.process_update(update))

    def assert_no_internal_error(self) -> None:
        """The global error handler must never have fired."""

        for call in self.bot.of("send_message") + self.bot.of("edit_message_text"):
            if "внутренняя ошибка" in text_of(call).lower():
                self.fail(f"An internal-error screen was shown: {text_of(call)!r}")

    def assert_answered(self) -> list[tuple]:
        """The press was acknowledged (Telegram stops spinning)."""

        answers = self.bot.of("answer_callback_query")
        self.assertTrue(answers, "The press was not acknowledged by the bot")
        return answers


class FavoritesDeliveryTests(DispatchTestCase):
    """§26: «❤️ Избранное» must really open the screen in real Telegram."""

    def test_the_menu_button_renders_the_favorites_screen(self) -> None:
        data = data_for(main_menu_keyboard(), FAVORITES_LABEL_MARK)
        self.assertEqual(data, CB_FAVORITES)

        update = callback_update(self.bot, data)
        handler = matched_handler(self.application, update)
        # The dedicated menu handler wins - the catch-all never sees it.
        self.assertIs(handler.callback, menu_callback)

        self.press(update)

        self.assert_answered()
        edits = self.bot.of("edit_message_text")
        self.assertTrue(edits, "The favorites screen was never rendered")
        self.assertIn("Избранное", text_of(edits[-1]))
        keyboard = edits[-1][2].get("reply_markup")
        self.assertIsNotNone(keyboard, "The screen was rendered without a keyboard")
        # The way out is part of the screen (no dead ends).
        home = data_for(keyboard, "Главное меню")
        self.assertEqual(home, CB_BACK)
        self.assert_no_internal_error()

    def test_a_press_on_a_photo_card_sends_before_removing_the_old_one(self) -> None:
        """§27: an un-editable message must never leave an empty dialog."""

        data = data_for(main_menu_keyboard(), FAVORITES_LABEL_MARK)
        update = callback_update(self.bot, data, photo=True)

        # Telegram refuses to turn a photo message into text.
        self.press(update, fail_next_edit=True)

        self.assert_answered()
        sends = self.bot.of("send_message")
        self.assertTrue(sends, "No fresh favorites screen was delivered")
        self.assertIn("Избранное", text_of(sends[0]))

        # Send first, delete second: the chat is never left without a screen.
        sequence = [
            call[0]
            for call in self.bot.calls
            if call[0] in {"send_message", "delete_message"}
        ]
        self.assertEqual(sequence, ["send_message", "delete_message"])
        self.assert_no_internal_error()

    def test_an_empty_favorites_screen_still_reaches_the_user(self) -> None:
        self.press(callback_update(self.bot, CB_FAVORITES))

        delivered = [
            text_of(call)
            for call in self.bot.of("edit_message_text") + self.bot.of("send_message")
        ]
        self.assertTrue(
            any(EMPTY_FAVORITES_TEXT in text for text in delivered), delivered
        )
        self.assert_no_internal_error()


class StaleCallbackTests(DispatchTestCase):
    """§26: «нажал и ничего не произошло» must be structurally impossible."""

    def test_an_unmatched_button_is_answered_and_lands_on_a_screen(self) -> None:
        # A callback no pattern claims: not a current menu entry, not a
        # removed screen, not any prefix with its own handler.
        update = callback_update(self.bot, "menu:legacy")
        handler = matched_handler(self.application, update)
        self.assertIsNotNone(handler, "No handler at all claimed the press")
        self.assertIs(handler.callback, stale_callback)

        self.press(update)

        answers = self.assert_answered()
        self.assertIn("устарела", text_of(answers[-1]))
        rendered = self.bot.of("edit_message_text") + self.bot.of("send_message")
        self.assertTrue(rendered, "The stale press produced no screen at all")
        self.assert_no_internal_error()

    def test_the_dedicated_handlers_always_win_over_the_catch_all(self) -> None:
        for data in (CB_FAVORITES, "nav:back", "menu:back", f"{CB_PREFIX_NAV}:abc:1"):
            with self.subTest(data=data):
                update = callback_update(self.bot, data)
                handler = matched_handler(self.application, update)
                self.assertIsNotNone(handler, f"Nobody handles {data}")
                self.assertIsNot(
                    handler.callback,
                    stale_callback,
                    f"{data} fell through to the catch-all",
                )

    def test_the_catch_all_is_registered_last_in_its_group(self) -> None:
        callbacks = [
            getattr(handler, "callback", None)
            for handler in self.application.handlers.get(0, [])
            if handler.__class__.__name__ == "CallbackQueryHandler"
        ]
        self.assertGreater(len(callbacks), 5)
        self.assertIs(callbacks[-1], stale_callback)


class CardNavigationTests(DispatchTestCase):
    """§28: «➡️ Следующий»/«◀️ Назад» move exactly one product, no counters."""

    def _products(self) -> list[Product]:
        return [
            Product(
                marketplace=Marketplace.ALIEXPRESS,
                product_id=f"nav-{index}",
                title=title,
                product_url=f"https://www.aliexpress.com/item/nav-{index}.html",
                price=10.0 + index,
                currency="USD",
            )
            for index, title in enumerate(
                ("Механическая клавиатура 68", "Механическая клавиатура 84")
            )
        ]

    def _seed_session(self) -> tuple[str, list[Product]]:
        products = self._products()
        outcome = SearchOutcome(
            request=SearchRequest(original_query="механическая клавиатура"),
            products=products,
        )
        update = callback_update(self.bot, CB_FAVORITES)
        context = self.application.context_types.context.from_update(
            update, self.application
        )
        session_id = common.remember_outcome(context, outcome, header=None)
        # The session must live in the very dict the later press reads.
        self.assertIn(
            common.OUTCOME_CACHE_KEY, self.application.user_data.get(777, {})
        )
        return session_id, products

    def test_next_then_back_walk_the_products_with_no_counter(self) -> None:
        session_id, products = self._seed_session()

        next_data = data_for(
            browse_card_keyboard(session_id, 0, len(products), products[0]),
            NEXT_LABEL_MARK,
        )
        update = callback_update(self.bot, next_data)
        self.assertIs(
            matched_handler(self.application, update).callback, browse_callback
        )

        self.press(update)
        edits = self.bot.of("edit_message_text")
        self.assertTrue(edits, "«Следующий» did not move the card")
        shown = text_of(edits[-1])
        self.assertIn("Механическая клавиатура 84", shown)
        self.assertNotIn("Механическая клавиатура 68", shown)
        # §28: the internal pool size is never a UI element.
        self.assertNotIn("1/2", shown)
        self.assertNotIn("2/2", shown)
        self.assert_no_internal_error()

        # «◀️ Назад» on the second card is an absolute pnav back to index 0.
        back_data = data_for(
            browse_card_keyboard(session_id, 1, len(products), products[1]),
            BACK_LABEL_MARK,
        )
        self.assertTrue(back_data.startswith(f"{CB_PREFIX_NAV}:"))

        self.press(callback_update(self.bot, back_data, update_id=2))
        edits = self.bot.of("edit_message_text")
        self.assertTrue(edits, "«Назад» did not move the card back")
        self.assertIn("Механическая клавиатура 68", text_of(edits[-1]))
        self.assert_no_internal_error()

    def test_navigation_without_a_session_says_so_and_stays_alive(self) -> None:
        """An expired session is answered honestly - never a dead spinner."""

        update = callback_update(self.bot, f"{CB_PREFIX_NAV}:gone:0")
        handler = matched_handler(self.application, update)
        self.assertIsNotNone(handler, "The nav button has no handler")

        self.press(update)

        answers = self.assert_answered()
        self.assertTrue(
            any("устарел" in text_of(answer) for answer in answers),
            [text_of(answer) for answer in answers],
        )
        self.assert_no_internal_error()


class BackNavigationTests(DispatchTestCase):
    def test_nav_back_is_answered_and_renders_a_screen(self) -> None:
        update = callback_update(self.bot, "nav:back")
        self.assertIs(
            matched_handler(self.application, update).callback, nav_back_callback
        )

        self.press(update)

        self.assert_answered()
        self.assertTrue(
            self.bot.of("edit_message_text") + self.bot.of("send_message"),
            "«◀️ Назад» produced no screen",
        )
        self.assert_no_internal_error()


class TwentyCardCarouselTests(DispatchTestCase):
    """§4: a 20-card feed is walked inside ONE message, up to the last card.

    The AI selection keeps at most 20 cards; this is the UI half of that
    promise: the SAME Telegram message is edited in place all the way to the
    20th product, «◀️ Назад» walks the list backwards, the last card offers the
    restart instead of a "next", and the 🤖 AI button resolves the 20th card
    exactly like the first one.
    """

    COUNT = 20

    def _seed(self) -> tuple[str, list[Product]]:
        products = [
            Product(
                marketplace=Marketplace.ALIEXPRESS,
                product_id=f"card-{index}",
                title=f"Наушники спортивные {index}",
                product_url=f"https://www.aliexpress.com/item/card-{index}.html",
                price=10.0 + index,
                currency="USD",
            )
            for index in range(self.COUNT)
        ]
        outcome = SearchOutcome(
            request=SearchRequest(original_query="наушники"),
            products=products,
        )
        update = callback_update(self.bot, CB_FAVORITES)
        context = self.application.context_types.context.from_update(
            update, self.application
        )
        session_id = common.remember_outcome(context, outcome, header=None)
        return session_id, products

    def _keyboard(self, session_id: str, index: int, products: list[Product]):
        return browse_card_keyboard(session_id, index, len(products), products[index])

    def test_the_same_message_walks_all_twenty_cards(self) -> None:
        session_id, products = self._seed()

        for step in range(1, self.COUNT):
            data = data_for(
                self._keyboard(session_id, step - 1, products), NEXT_LABEL_MARK
            )
            update = callback_update(self.bot, data, update_id=step)
            self.assertIs(
                matched_handler(self.application, update).callback,
                browse_callback,
                f"«Следующий» of card {step} has no handler",
            )

            self.press(update)

            edits = self.bot.of("edit_message_text")
            self.assertEqual(len(edits), 1, f"card {step} was not edited in place")
            self.assertIn(f"Наушники спортивные {step}", text_of(edits[-1]))
            # No new message, no photo: the feed keeps living in ONE message.
            self.assertEqual(self.bot.of("send_message"), [])
            self.assertEqual(self.bot.of("send_photo"), [])
            self.assert_no_internal_error()

        # The 20th card is reachable and it says so honestly.
        last = self._keyboard(session_id, self.COUNT - 1, products)
        labels = [button.text for row in last.inline_keyboard for button in row]
        self.assertNotIn(LABEL_CARD_NEXT, labels)
        self.assertIn(LABEL_CARD_RESTART, labels)
        self.assertEqual(
            data_for(last, LABEL_CARD_RESTART), f"{CB_PREFIX_NAV}:{session_id}:0"
        )

    def test_the_last_card_walks_back_to_the_nineteenth(self) -> None:
        session_id, products = self._seed()

        back = data_for(
            self._keyboard(session_id, self.COUNT - 1, products), BACK_LABEL_MARK
        )
        self.assertEqual(back, f"{CB_PREFIX_NAV}:{session_id}:{self.COUNT - 2}")

        self.press(callback_update(self.bot, back, update_id=2))

        edits = self.bot.of("edit_message_text")
        self.assertTrue(edits, "«◀️ Назад» did not move the card")
        self.assertIn(f"Наушники спортивные {self.COUNT - 2}", text_of(edits[-1]))
        self.assert_no_internal_error()


    def test_the_ai_menu_works_on_the_twentieth_card(self) -> None:
        """The 🤖 AI button must resolve the LAST card, not the first one."""

        session_id, products = self._seed()
        last_index = self.COUNT - 1
        keyboard = self._keyboard(session_id, last_index, products)

        menu_data = data_for(keyboard, "AI")
        self.assertEqual(menu_data, f"{CB_PREFIX_AI}:{session_id}:{last_index}")
        menu_update = callback_update(self.bot, menu_data, update_id=2)
        self.assertIs(
            matched_handler(self.application, menu_update).callback,
            ai_assistant.ai_menu_callback,
        )

        self.press(menu_update)

        self.assert_answered()
        context = self.application.context_types.context.from_update(
            menu_update, self.application
        )
        resolved = common.get_cached_product(context, session_id, last_index)
        self.assertIsNotNone(resolved, "The 20th card has no cached product")
        self.assertEqual(resolved.product_id, f"card-{last_index}")
        self.assert_no_internal_error()

        # «⭐ Стоит ли покупать?» runs the detailed analysis OF THAT card.
        advice_data = f"{CB_PREFIX_AI_ACTION}:advice:{session_id}:{last_index}"
        advice_update = callback_update(self.bot, advice_data, update_id=3)
        self.assertIs(
            matched_handler(self.application, advice_update).callback,
            ai_assistant.ai_action_callback,
        )

        self.press(advice_update)

        self.assert_answered()
        self.assertTrue(
            self.bot.of("send_message"),
            "«Стоит ли покупать?» delivered no screen for the 20th card",
        )
        self.assert_no_internal_error()

        # ...on the model roles this deployment configured: one generation for
        # parsing, selection and the analysis of a card.
        service = self.application.bot_data["ai_service"]
        self.assertEqual(service.smart_model, "gemini-3.8-flash")
        self.assertEqual(service.fast_model, "gemini-3.8-flash")
        self.assertEqual(service.vision_model, "gemini-3.8-flash")
        self.assertEqual(service.fallback_model, "gemini-3.1-flash-lite")


if __name__ == "__main__":
    unittest.main()
