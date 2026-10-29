"""Offline tests for the result screen, pagination and navigation wiring.

No Telegram request and no marketplace call is made - the handlers get
small fakes, so a broken callback chain (a button no handler matches, a
page cursor that never advances, an action button that is never sent)
fails a test instead of silently doing nothing in production.

Removed screens (Сравнить / Выгодные находки / Промо / Случайное) are
covered too: their legacy callbacks must reach the tombstone handler,
never a dead end.
"""

from __future__ import annotations

import asyncio
import json
import unittest

from handlers import discovery
from handlers import favorites
from handlers import region
from handlers import search as search_handlers
from handlers import settings as settings_handlers
from handlers import start
from keyboards.main import (
    CB_CATEGORY_PREFIX,
    CB_CURRENCY_PREFIX,
    CB_NEXT_PREFIX,
    MAIN_MENU_CALLBACKS,
    categories_keyboard,
    currencies_keyboard,
    currency_screen_keyboard,
    main_menu_keyboard,
    back_to_menu_keyboard,
    photo_retry_keyboard,
    notifications_keyboard,
    onboarding_keyboard,
    settings_keyboard,
)
from keyboards.admin import (
    admin_main_keyboard,
    admin_period_keyboard,
    admin_screen_keyboard,
)
from keyboards.hot_discounts import hot_discounts_keyboard
from keyboards.products import (
    CB_PREFIX_FAVORITE,
    CB_PREFIX_TRACK,
    CARD_ACTION_LABELS,
    CARD_NAV_LABELS,
    LABEL_BUY,
    ai_menu_keyboard,
    browse_card_keyboard,
    favorites_list_keyboard,
    tracked_list_keyboard,
)
from models.product import Marketplace, Product
from models.search_request import SearchRequest
from services.search_service import DEFAULT_PAGE_SIZE, SearchOutcome


def make_product(index: int) -> Product:
    return Product(
        marketplace=Marketplace.ALIEXPRESS,
        product_id=f"p{index}",
        title=f"Товар {index}",
        product_url=f"https://example.com/p{index}",
        price=10.0 + index,
        currency="USD",
    )


def make_outcome(count: int = 12, **overrides) -> SearchOutcome:
    data = {
        "request": SearchRequest(original_query="товар"),
        "products": [make_product(index) for index in range(count)],
        "searched_marketplaces": ["AliExpress"],
    }
    data.update(overrides)
    return SearchOutcome(**data)


class FakeMessage:
    """Records every message the bot would send to the chat."""

    def __init__(self, chat_id: int = 1) -> None:
        self.chat_id = chat_id
        #: photos attached to the message (empty for a plain text message)
        self.photo: list = []
        self.texts: list[str] = []
        self.kwargs: list[dict] = []

    async def reply_text(self, text: str, **kwargs) -> None:
        self.texts.append(text)
        self.kwargs.append(kwargs)


class FakeQuery:
    """Records callback answers and message edits."""

    def __init__(self, data: str) -> None:
        self.data = data
        self.answers: list[str] = []
        self.edits: list[tuple[str, dict]] = []

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        if text:
            self.answers.append(text)

    async def edit_message_text(self, text: str, **kwargs) -> None:
        self.edits.append((text, kwargs))


class FakeChat:
    id = 47


class FakeUser:
    id = 555
    username = "tester"
    first_name = "Tester"


class FakeUpdate:
    def __init__(self, *, callback_query=None, message=None, user=None) -> None:
        self.callback_query = callback_query
        self.effective_message = message
        self.effective_chat = FakeChat() if (message or callback_query) else None
        self.effective_user = user or FakeUser()


class FakeBot:
    """Records every ``send_message`` the handlers perform."""

    def __init__(self) -> None:
        self.messages: list[tuple[int, str, dict]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
        self.messages.append((chat_id, text, kwargs))

    @property
    def texts(self) -> list[str]:
        return [text for _, text, _ in self.messages]

    @property
    def keyboards(self) -> list[list]:
        return [
            kwargs["reply_markup"].inline_keyboard
            for _, _, kwargs in self.messages
            if kwargs.get("reply_markup") is not None
        ]


class _FakeApplication:
    def __init__(self, bot_data: dict) -> None:
        self.bot_data = bot_data


class FakeContext:
    """Minimal stand-in for ``telegram.ext.CallbackContext``."""

    def __init__(self, bot_data: dict | None = None, bot=None) -> None:
        self.bot = bot or FakeBot()
        self.user_data: dict = {}
        self.application = _FakeApplication(bot_data or {})


def callbacks_of(rows: list) -> list[str]:
    """Callback data of every button in a keyboard, flattened."""

    return [
        button.callback_data
        for row in rows
        for button in row
        if button.callback_data is not None
    ]


def labels_of(rows: list) -> list[str]:
    return [button.text for row in rows for button in row]


class FakeDatabase:
    """In-memory stand-in for ``database.database.Database``."""

    def __init__(
        self,
        region: dict | None = None,
        fail: set[str] | None = None,
        favorites: list[dict] | None = None,
    ) -> None:
        self.region = region or {"currency": None}
        self.fail = fail or set()
        self.favorite_rows = favorites or []
        self.updated: list[dict] = []
        self.config: dict[str, str] = {}

    async def get_or_create_user(self, telegram_id, username, first_name) -> int:
        return 1

    async def get_shown_products(self, user_id, *, flow=None) -> list[tuple[str, str]]:
        return []

    async def add_shown_product(
        self, *, user_id, marketplace, product_id, flow: str = "search"
    ) -> None:
        return None

    async def get_feed_page(self, user_id) -> int:
        return 1

    async def set_feed_page(self, user_id, step) -> None:
        return None

    async def record_event(self, event_type: str, **kwargs) -> None:
        return None

    async def get_user_region(self, user_id) -> dict:
        if "get_user_region" in self.fail:
            raise RuntimeError("db is down")
        return dict(self.region)

    async def update_user_region(self, user_id, **kwargs) -> None:
        if "update_user_region" in self.fail:
            raise RuntimeError("db is down")
        self.updated.append(kwargs)
        if kwargs.get("clear_currency"):
            self.region["currency"] = None
        if kwargs.get("currency"):
            self.region["currency"] = kwargs["currency"]

    async def get_favorites(self, user_id) -> list[dict]:
        if "get_favorites" in self.fail:
            raise RuntimeError("db is down")
        return [dict(row) for row in self.favorite_rows]

    async def get_tracked_products(self, user_id) -> list[dict]:
        if "get_tracked_products" in self.fail:
            raise RuntimeError("db is down")
        return []

    async def get_search_history(self, user_id, limit: int = 10) -> list[dict]:
        if "get_search_history" in self.fail:
            raise RuntimeError("db is down")
        return []

    async def invalidate_tracking_baselines(self, user_id) -> None:
        self.baselines_invalidated_for = user_id

    async def get_notification_pref(self, user_id) -> bool:
        if "get_notification_pref" in self.fail:
            raise RuntimeError("db is down")
        return getattr(self, "notify_enabled", True)

    async def set_notification_pref(self, user_id, enabled) -> None:
        if "set_notification_pref" in self.fail:
            raise RuntimeError("db is down")
        self.notify_enabled = bool(enabled)

    async def is_onboarded(self, user_id) -> bool:
        if "is_onboarded" in self.fail:
            raise RuntimeError("db is down")
        return getattr(self, "onboarded", False)

    async def mark_onboarded(self, user_id) -> None:
        if "mark_onboarded" in self.fail:
            raise RuntimeError("db is down")
        self.onboarded = True

    async def get_config(self, key: str):
        if "get_config" in self.fail:
            raise RuntimeError("db is down")
        return self.config.get(key)

    async def set_config(self, key: str, value: str) -> None:
        if "set_config" in self.fail:
            raise RuntimeError("db is down")
        self.config[key] = value


def make_context(database: FakeDatabase | None = None, **bot_data) -> FakeContext:
    data = {"database": database or FakeDatabase()}
    data.update(bot_data)
    return FakeContext(bot_data=data)


def run(coro):
    return asyncio.run(coro)


class SendSearchResultsTests(unittest.TestCase):
    """A feed opens ON a product card: no list, no counters, real actions."""

    def test_opens_directly_on_the_first_product_card(self) -> None:
        context = make_context()
        outcome = make_outcome(count=12)

        session_id = run(search_handlers.send_search_results(context, 47, outcome))

        self.assertIsNotNone(session_id)
        bot: FakeBot = context.bot
        # one card, then one summary line AFTER it: the rest of the feed is
        # reached by editing the card, never by sending more card messages
        self.assertEqual(len(bot.messages), 2)
        card = bot.texts[0]
        self.assertIn("Товар 0", card)
        self.assertIn("Нашёл 12 вариантов", bot.texts[1])
        # No list screen, no counter, no position, no page marker.
        self.assertNotIn("Найдено товаров", card)
        self.assertNotIn("Страница", card)
        self.assertNotIn("1/12", card)
        self.assertNotIn("1. Товар", card)

    def test_summary_is_sent_once_after_the_cards(self) -> None:
        """The count summary follows the card exactly once, never repeats."""

        context = make_context()
        session_id = run(
            search_handlers.send_search_results(context, 47, make_outcome(count=5))
        )

        self.assertIsNotNone(session_id)
        bot = context.bot
        self.assertEqual(len(bot.texts), 2)
        # Card FIRST, summary SECOND, and the card never carries the summary.
        self.assertIn("Товар 0", bot.texts[0])
        self.assertNotIn("Нашёл", bot.texts[0])
        self.assertIn("Нашёл 5 вариантов", bot.texts[1])

    def test_card_offers_the_next_product_instead_of_more_pages(self) -> None:
        context = make_context()
        session_id = run(
            search_handlers.send_search_results(context, 47, make_outcome(count=12))
        )

        # The card keyboard comes second (the hint's keyboard is first).
        rows = context.bot.keyboards[0]
        callbacks = callbacks_of(rows)
        # the whole feed is browsed from this card: next product, not a page
        self.assertIn(f"pnav:{session_id}:1", callbacks)
        self.assertFalse(any(str(cb).startswith("next:") for cb in callbacks))
        self.assertNotIn("➡️ Показать ещё", labels_of(rows))
        # no numbered link list anywhere
        self.assertFalse(
            any(label.split(".")[0].isdigit() for label in labels_of(rows))
        )

    def test_a_partial_result_keeps_plumbing_out_of_the_card(self) -> None:
        """API keys and failed sources are logged, not printed on a card."""

        context = make_context()
        outcome = make_outcome(
            count=3,
            unavailable_marketplaces=["AliExpress"],
            failed_marketplaces=["AliExpress"],
        )

        run(search_handlers.send_search_results(context, 47, outcome))

        card = context.bot.texts[0]
        self.assertIn("Товар 0", card)
        self.assertNotIn("API", card)
        self.assertNotIn("Не подключены", card)
        self.assertNotIn("Не удалось получить данные", card)

    def test_unapplied_constraint_is_logged_but_never_shown(self) -> None:
        """§11/§12: no «фильтр не применён» and no API wording on a card.

        The rating bound the marketplace cannot honour (it reports no
        0-5 star rating) is a fact of the BOT's plumbing, not of the
        user's product question: the results are returned unfiltered, the
        fact goes to the log, and the screen stays silent - the manual
        test that produced «⚠️ Фильтр ... не применён: AliExpress не
        передаёт оценку в звёздах» is the regression this asserts.
        """

        context = make_context()
        outcome = make_outcome(count=2)
        outcome.request = outcome.request.model_copy(
            update={
                "min_rating": 4.0,
                "rating_filter_applicable": False,
                "ignored_features": ["с чехлом"],
            }
        )

        run(search_handlers.send_search_results(context, 47, outcome))

        card = context.bot.texts[0]
        self.assertNotIn("не применён", card)
        self.assertNotIn("не проверялось", card)
        self.assertNotIn("AliExpress не передаёт", card)
        self.assertNotIn("⚠️", card)
        # The products themselves are still shown, unfiltered.
        self.assertIn("Товар 0", card)
        # ...and not one word of it appears above any card.
        for index in (0, 1, 2):
            caption = search_handlers._card_caption(outcome, index, None)
            self.assertNotIn("не применён", caption)
            self.assertNotIn("не проверялось", caption)

    def test_empty_result_shows_only_the_short_human_message(self) -> None:
        """§3: no API keys, no marketplace diagnostics - one honest line."""
        context = make_context()
        outcome = make_outcome(count=0, unavailable_marketplaces=["AliExpress"])

        session_id = run(search_handlers.send_search_results(context, 47, outcome))

        self.assertIsNone(session_id)
        text = context.bot.texts[0]
        self.assertEqual(text, search_handlers.EMPTY_RESULT_TEXT)
        self.assertNotIn("API", text)
        self.assertNotIn("Не подключены", text)
        self.assertNotIn("Возможные причины", text)
        self.assertNotIn("маркетплейс", text.lower())

    def test_result_screen_never_mentions_removed_marketplaces(self) -> None:
        context = make_context()

        run(search_handlers.send_search_results(context, 47, make_outcome(count=2)))

        summary = context.bot.texts[0]
        self.assertNotIn("Alibaba", summary)
        self.assertNotIn("Temu", summary)
        self.assertNotIn("опт", summary.lower())

    def test_header_replaces_the_generic_headline(self) -> None:
        context = make_context()

        run(
            search_handlers.send_search_results(
                context, 47, make_outcome(count=2), header="🔥 <b>Популярное</b>"
            )
        )

        summary = context.bot.texts[0]
        self.assertTrue(summary.startswith("🔥 <b>Популярное</b>"))
        self.assertNotIn("Найдено товаров", summary)

    def test_a_plain_search_starts_with_the_product_not_the_query(self) -> None:
        """§4: the user knows what they searched for - no query echo."""
        context = make_context()
        outcome = make_outcome(count=2)
        outcome.request = outcome.request.model_copy(
            update={"original_query": "чехол для телефона"}
        )

        run(search_handlers.send_search_results(context, 47, outcome))

        card = context.bot.texts[0]
        self.assertNotIn("чехол для телефона", card)
        self.assertNotIn("🔎", card)
        self.assertTrue(card.startswith("Товар 0"))
        self.assertNotIn("Найдено товаров", card)
        # The hint after the card never echoes the query either.
        self.assertNotIn("чехол для телефона", context.bot.texts[1])

    def test_back_on_the_first_card_leaves_the_feed(self) -> None:
        """There is no summary screen any more: back reaches a real screen."""

        context = make_context()
        run(search_handlers.send_search_results(context, 47, make_outcome(count=3)))
        callbacks = callbacks_of(context.bot.keyboards[0])
        self.assertIn("menu:back", callbacks)
        self.assertFalse(any(str(cb).startswith("pback:") for cb in callbacks))

        context = make_context()
        run(
            search_handlers.send_search_results(
                context, 47, make_outcome(count=3), back_callback="menu:categories"
            )
        )
        self.assertIn("menu:categories", callbacks_of(context.bot.keyboards[0]))

    def test_cards_carry_the_required_actions(self) -> None:
        """The card: Buy / favorite / share / tracking toggle / nav.

        There must be NO "🔍 Сравнить" on a product card, and the
        tracking button is present with its default (not-tracking) state.
        """

        context = make_context()

        run(search_handlers.send_search_results(context, 47, make_outcome(count=3)))

        card_rows = context.bot.keyboards[0]
        labels = labels_of(card_rows)
        # The documented card vocabulary (`keyboards.products`): every action
        # button except «🛒 Купить» (this product has no affiliate URL) and
        # BOTH navigation labels of a middle card of the feed.
        for label in CARD_ACTION_LABELS:
            if label != LABEL_BUY:
                self.assertIn(label, labels)
        for label in CARD_NAV_LABELS:
            self.assertIn(label, labels)
        self.assertNotIn("🔍 Сравнить", labels)
        callbacks = callbacks_of(card_rows)
        self.assertTrue(any(cb.startswith("pfav:") for cb in callbacks))
        self.assertTrue(any(cb.startswith("ptrack:") for cb in callbacks))
        self.assertFalse(any(cb.startswith("pcmp:") for cb in callbacks))
        self.assertTrue(any(cb.startswith("pnav:") for cb in callbacks))

    def test_buy_button_only_with_a_real_affiliate_url(self) -> None:
        """Affiliate-only rule: no affiliate link, no Buy button at all.

        Buy is a URL button carrying the product's exact affiliate URL -
        one press opens the AliExpress page, no bot round-trip.
        """

        without_affiliate = make_outcome(count=1)
        context = make_context()
        run(search_handlers.send_search_results(context, 47, without_affiliate))
        labels = labels_of(context.bot.keyboards[0])
        self.assertNotIn("🛒 Купить", labels)

        product = make_product(0).model_copy(
            update={"affiliate_url": "https://s.click.aliexpress.com/real1"}
        )
        with_affiliate = make_outcome(count=1, products=[product])
        context = make_context()
        run(search_handlers.send_search_results(context, 47, with_affiliate))
        labels = labels_of(context.bot.keyboards[0])
        self.assertEqual(labels[0], "🛒 Купить")
        buy_button = next(
            button
            for row in context.bot.keyboards[0]
            for button in row
            if button.text == "🛒 Купить"
        )
        # The URL button opens exactly the affiliate URL, not a callback
        # and not the plain product URL.
        self.assertEqual(buy_button.url, "https://s.click.aliexpress.com/real1")
        self.assertIsNone(buy_button.callback_data)

    def test_cached_meta_is_not_lost_when_the_cache_rotates(self) -> None:
        context = make_context()
        for _ in range(6):
            run(
                search_handlers.send_search_results(
                    context, 47, make_outcome(count=1), header="H"
                )
            )

        cache = context.user_data["outcome_cache"]
        meta = context.user_data["outcome_meta"]
        self.assertEqual(set(cache), set(meta))
        self.assertLessEqual(len(cache), 5)


class NextPageCallbackTests(unittest.TestCase):
    """Legacy «➡️ Показать ещё»: an old button must answer, never dead-end."""

    def _session(self, count: int = 7) -> tuple[FakeContext, str]:
        context = make_context()
        session_id = run(
            search_handlers.send_search_results(context, 47, make_outcome(count=count))
        )
        context.bot.messages.clear()
        return context, session_id

    def test_legacy_button_points_the_user_to_the_card(self) -> None:
        context, session_id = self._session(count=7)
        query = FakeQuery(f"{CB_NEXT_PREFIX}:{session_id}")

        run(
            search_handlers.next_page_callback(
                FakeUpdate(callback_query=query), context
            )
        )

        self.assertEqual(len(query.answers), 1)
        self.assertIn("Следующий", query.answers[0])
        # Nothing is rewritten and nothing is re-sent: the products live in
        # the card, and paged lists no longer exist.
        self.assertEqual(query.edits, [])
        self.assertEqual(context.bot.messages, [])
        # The cached session stays intact for the live card.
        self.assertIsNotNone(
            context.user_data["outcome_cache"].get(session_id)
        )

    def test_expired_session_is_reported_instead_of_crashing(self) -> None:
        context = make_context()
        query = FakeQuery(f"{CB_NEXT_PREFIX}:deadbeef")

        run(
            search_handlers.next_page_callback(
                FakeUpdate(callback_query=query), context
            )
        )

        self.assertEqual(len(query.answers), 1)
        self.assertIn("устарел", query.answers[0])
        self.assertEqual(query.edits, [])


class CurrencyHandlerTests(unittest.TestCase):
    """The currency is optional, and unset values are spelled out."""

    def test_screen_says_the_preference_is_optional(self) -> None:
        context = make_context()
        message = FakeMessage()

        run(region.show_currency(FakeUpdate(message=message), context))

        self.assertIn("необязательный", message.texts[0])
        self.assertIn("не указана", message.texts[0])

    def test_currency_is_uppercased_and_confirmed(self) -> None:
        database = FakeDatabase()
        context = make_context(database)
        query = FakeQuery(f"{CB_CURRENCY_PREFIX}:usd")

        run(region.currency_callback(FakeUpdate(callback_query=query), context))

        self.assertEqual(database.updated, [{"currency": "USD"}])
        self.assertEqual(query.answers, ["Валюта: USD"])
        self.assertEqual(len(query.edits), 1)

    def test_unsupported_currency_is_rejected_without_writing(self) -> None:
        database = FakeDatabase()
        context = make_context(database)
        query = FakeQuery(f"{CB_CURRENCY_PREFIX}:xyz")

        run(region.currency_callback(FakeUpdate(callback_query=query), context))

        self.assertEqual(database.updated, [])
        self.assertEqual(query.edits, [])
        self.assertIn("не поддерживается", query.answers[0])

    def test_bare_prefix_opens_the_picker(self) -> None:
        context = make_context()
        query = FakeQuery(CB_CURRENCY_PREFIX)

        run(region.currency_callback(FakeUpdate(callback_query=query), context))

        self.assertEqual(query.answers, [])
        rows = query.edits[0][1]["reply_markup"].inline_keyboard
        callbacks = callbacks_of(rows)
        # The one-screen picker: a flag grid opens right away (the old
        # intermediate "change/clear" step is gone with the revision).
        self.assertTrue(
            any(cb.startswith(f"{CB_CURRENCY_PREFIX}:") for cb in callbacks)
        )
        self.assertIn("nav:back", callbacks)
        self.assertIn("menu:back", callbacks)

    def test_clearing_the_preference_writes_a_null(self) -> None:
        database = FakeDatabase(region={"currency": "USD"})
        context = make_context(database)
        query = FakeQuery(f"{CB_CURRENCY_PREFIX}:clear")

        run(region.currency_callback(FakeUpdate(callback_query=query), context))

        self.assertEqual(database.updated, [{"clear_currency": True}])
        self.assertIsNone(database.region["currency"])

    def test_storage_failure_is_shown_to_the_user(self) -> None:
        database = FakeDatabase(fail={"update_user_region"})
        context = make_context(database)
        query = FakeQuery(f"{CB_CURRENCY_PREFIX}:usd")

        run(region.currency_callback(FakeUpdate(callback_query=query), context))

        self.assertIn("Не удалось сохранить.", query.answers[0])
        self.assertEqual(query.edits, [])


class SettingsHandlerTests(unittest.TestCase):
    """Settings must report stored data, never invent it."""

    def test_counts_are_shown_with_an_honest_explanation(self) -> None:
        context = make_context()
        message = FakeMessage()

        run(settings_handlers.show_settings(FakeUpdate(message=message), context))

        text = message.texts[0]
        self.assertIn("<b>Настройки</b>", text)
        self.assertIn("В избранном: 0 товаров", text)
        self.assertIn("не указана", text)
        self.assertIn("не дополняет список товарами", text)

    def test_unreadable_section_says_so_instead_of_showing_zero(self) -> None:
        context = make_context(FakeDatabase(fail={"get_favorites"}))
        message = FakeMessage()

        run(settings_handlers.show_settings(FakeUpdate(message=message), context))

        self.assertIn("В избранном: не удалось прочитать", message.texts[0])

    def test_settings_keyboard_matches_the_spec_layout(self) -> None:
        """§35: Валюта / Уведомления / Как пользоваться ботом / Назад / Меню."""

        rows = settings_keyboard().inline_keyboard
        labels = labels_of(rows)
        self.assertEqual(
            labels,
            [
                "\U0001F30D Валюта",
                "\U0001F514 Уведомления",
                "\U0001F4D6 Как пользоваться ботом",
                "◀️ Назад",
                "\U0001F3E0 Главное меню",
            ],
        )
        # No country, no Compare, no removed sections anywhere.
        joined = " ".join(labels)
        for forbidden in ("Страна", "Сравнить", "Случайное", "Temu", "Alibaba"):
            self.assertNotIn(forbidden, joined)

    def test_notifications_screen_shows_real_preference(self) -> None:
        context = make_context()
        message = FakeMessage()

        run(
            settings_handlers.show_notifications(FakeUpdate(message=message), context)
        )

        self.assertIn("<b>Уведомления</b>", message.texts[0])
        self.assertIn("включены", message.texts[0])

    def test_notifications_toggle_flips_and_persists(self) -> None:
        db = FakeDatabase()
        context = make_context(db)
        query = FakeQuery("notif:toggle")

        run(
            settings_handlers.notifications_toggle_callback(
                FakeUpdate(callback_query=query), context
            )
        )

        self.assertFalse(db.notify_enabled)
        self.assertIn("выключены", query.edits[0][0])

        run(
            settings_handlers.notifications_toggle_callback(
                FakeUpdate(callback_query=query), context
            )
        )
        self.assertTrue(db.notify_enabled)
        self.assertIn("включены", query.edits[-1][0])

    def test_notifications_toggle_reports_db_failure(self) -> None:
        context = make_context(FakeDatabase(fail={"set_notification_pref"}))
        query = FakeQuery("notif:toggle")

        run(
            settings_handlers.notifications_toggle_callback(
                FakeUpdate(callback_query=query), context
            )
        )

        self.assertIn("не удалось сохранить", query.edits[0][0].lower())


class OnboardingTests(unittest.TestCase):
    """First /start shows the concise welcome once, later /start opens the menu.

    The full seven-screen tutorial (§37) stays available from the settings
    screen (``menu:tutorial``) even for an already-onboarded user.
    """

    def _start(self, context):
        message = FakeMessage()
        run(start.start_command(FakeUpdate(message=message), context))
        return message

    def test_first_start_shows_the_concise_welcome_with_the_menu(self) -> None:
        context = make_context()
        message = self._start(context)

        self.assertIn("Добро пожаловать!", message.texts[0])
        self.assertIn("Умный поиск", message.texts[0])
        # the menu is attached right away: the user has a clear next step
        callbacks = callbacks_of(message.kwargs[0]["reply_markup"].inline_keyboard)
        self.assertIn("menu:search", callbacks)

    def test_first_start_marks_onboarded(self) -> None:
        db = FakeDatabase()
        self._start(make_context(db))

        self.assertTrue(db.onboarded)

    def test_pager_walks_through_all_seven_screens(self) -> None:
        context = make_context()
        for step in (1, 2, 3, 4, 5, 6):
            query = FakeQuery(f"onb:{step}")
            run(
                start.onboarding_callback(
                    FakeUpdate(callback_query=query), context
                )
            )
            text, kwargs = query.edits[0]
            self.assertIn(f"{step + 1}/7", text)
            expected = "onb:done" if step == 6 else f"onb:{step + 1}"
            self.assertIn(expected, callbacks_of(kwargs["reply_markup"].inline_keyboard))

    def test_done_marks_onboarded_and_opens_menu(self) -> None:
        db = FakeDatabase()
        context = make_context(db)
        query = FakeQuery("onb:done")

        run(start.onboarding_callback(FakeUpdate(callback_query=query), context))

        self.assertTrue(db.onboarded)
        text, kwargs = query.edits[0]
        self.assertIn("<b>Поиск товаров</b>", text)
        self.assertIn("menu:search", callbacks_of(kwargs["reply_markup"].inline_keyboard))

    def test_subsequent_start_goes_straight_to_the_menu(self) -> None:
        db = FakeDatabase()
        db.onboarded = True
        message = self._start(make_context(db))

        self.assertNotIn("1/7", message.texts[0])
        self.assertIn("<b>Поиск товаров</b>", message.texts[0])

    def test_tutorial_from_settings_replays_the_onboarding(self) -> None:
        context = make_context()
        query = FakeQuery("menu:tutorial")

        run(start.dispatch_menu("menu:tutorial", FakeUpdate(callback_query=query), context))

        text, kwargs = query.edits[0]
        self.assertIn("1/7", text)
        self.assertIn("onb:1", callbacks_of(kwargs["reply_markup"].inline_keyboard))


class DiscoveredScreensTests(unittest.TestCase):
    """Discovery screens render through the shared paged renderer."""

    def test_categories_screen_lists_real_categories(self) -> None:
        context = make_context()
        query = FakeQuery("menu:categories")

        run(discovery.show_categories(FakeUpdate(callback_query=query), context))

        text, kwargs = query.edits[0]
        rows = kwargs["reply_markup"].inline_keyboard
        self.assertIn("Категории", text)
        callbacks = callbacks_of(rows)
        self.assertTrue(callbacks)
        self.assertTrue(
            all(cb.startswith(f"{CB_CATEGORY_PREFIX}:") for cb in callbacks[:-1])
        )
        # the list is never a dead end
        self.assertIn("menu:back", callbacks)

    def test_unknown_category_is_refused(self) -> None:
        context = make_context()
        query = FakeQuery(f"{CB_CATEGORY_PREFIX}:nope")

        run(discovery.category_callback(FakeUpdate(callback_query=query), context))

        self.assertEqual(query.answers, ["Категория не найдена."])
        self.assertEqual(query.edits, [])


class PhotoMessageTests(unittest.TestCase):
    """A photo either runs a real image search or says it is unsupported."""

    def test_oversized_photo_is_refused_with_an_explanation(self) -> None:
        context = make_context()
        message = FakeMessage()
        photo = type(
            "Photo", (), {"file_size": discovery.MAX_IMAGE_BYTES + 1}
        )()
        update = FakeUpdate(message=message)
        update.effective_message.photo = [photo]

        run(discovery.photo_message(update, context))

        self.assertEqual(message.texts[0], discovery.PHOTO_RECEIVED_TEXT)
        self.assertIn("слишком большая", message.texts[1])

    def test_text_message_is_ignored(self) -> None:
        context = make_context()
        message = FakeMessage()
        update = FakeUpdate(message=message)

        run(discovery.photo_message(update, context))

        self.assertEqual(message.texts, [])


class NavigationWiringTests(unittest.TestCase):
    """Every button the bot can show must be handled by the application.

    This is the regression test for the class of bug where a screen sends
    a callback that no handler is registered for: the button would simply
    do nothing in production, and no unit test of the handler alone would
    ever notice.
    """

    @classmethod
    def setUpClass(cls) -> None:
        from config.settings import MarketplaceCredentials, Settings
        from bot import build_application

        credentials = MarketplaceCredentials()
        settings = Settings(
            bot_token="123:ABC",
            gemini_api_key=None,
            database_path=":memory:",
            log_level="CRITICAL",
            aliexpress=credentials,
        )
        application = build_application(settings)
        cls.patterns = [
            handler.pattern
            for handler in application.handlers[0]
            if getattr(handler, "pattern", None) is not None
        ]

    def _matches(self, data: str) -> bool:
        return any(pattern.search(data) for pattern in self.patterns)

    def test_all_static_keyboards_are_handled(self) -> None:
        product = make_product(0)
        tracked_product = make_product(1)
        keyboards = {
            "main_menu": main_menu_keyboard(),
            "back_to_menu": back_to_menu_keyboard(),
            "settings": settings_keyboard(),
            "currency_screen": currency_screen_keyboard(),
            "currencies": currencies_keyboard(),
            "categories": categories_keyboard(),
            # The card-first UX: the ONLY product screens left are the card
            # itself and the two list screens (favorites, tracking).
            "browse_card": browse_card_keyboard("abc123", 0, 2, product),
            "browse_card_tracked": browse_card_keyboard(
                "abc123", 0, 2, tracked_product, tracking=True
            ),
            "browse_card_last": browse_card_keyboard("abc123", 1, 2, product),
            "favorites": favorites_list_keyboard(
                [{"index": 0, "product": product}]
            ),
            "tracked": tracked_list_keyboard(
                [{"tracked_id": 1, "title": "Товар", "url": None, "price": 1.0,
                  "currency": "USD"}]
            ),
            # The screens that were added/changed by the UX revision: their
            # buttons must not become dead ends either.
            "hot_discounts": hot_discounts_keyboard(),
            "admin_period": admin_period_keyboard("7d"),
            "admin_main": admin_main_keyboard(),
            "admin_screen": admin_screen_keyboard("stats", "7"),
            "admin_screen_users": admin_screen_keyboard("users", "7", page=1, has_next=True),
            "photo_retry": photo_retry_keyboard(),
            "notifications_on": notifications_keyboard(True),
            "notifications_off": notifications_keyboard(False),
            "onboarding_first": onboarding_keyboard(0, 7),
            "onboarding_last": onboarding_keyboard(6, 7),
            # The AI product menu (card -> menu -> five actions) must be wired
            # exactly like every other screen: a callback with no handler is a
            # button that silently does nothing in production.
            "ai_menu": ai_menu_keyboard("abc123", 0),
            "ai_menu_next": ai_menu_keyboard("abc123", 1),
        }

        unhandled: list[str] = []
        for name, keyboard in keyboards.items():
            for data in callbacks_of(keyboard.inline_keyboard):
                if not self._matches(data):
                    unhandled.append(f"{name}: {data}")

        self.assertEqual(unhandled, [], f"Callback-кнопки без обработчика: {unhandled}")

    def test_currency_picker_from_the_real_catalog(self) -> None:
        data = callbacks_of(currencies_keyboard().inline_keyboard)
        self.assertTrue(data)
        for value in data:
            self.assertTrue(self._matches(value), f"{value} не обрабатывается")

    def test_clearing_the_currency_is_handled(self) -> None:
        self.assertTrue(self._matches(f"{CB_CURRENCY_PREFIX}:clear"))
        self.assertTrue(self._matches(CB_CURRENCY_PREFIX))


class LegacyMarketplaceDataTests(unittest.TestCase):
    """Snapshots saved before a marketplace was removed must never come back.

    Favorites store ``product_json`` verbatim, so a row written while the
    Temu or Alibaba provider still existed keeps its removed marketplace
    value. The marketplace enum no longer contains those values, so such
    rows must be skipped (and logged) instead of being rendered or
    crashing the screen.
    """

    #: A realistic favorite row as written by the removed Temu provider.
    LEGACY_ROW = {
        "id": 7,
        "marketplace": "temu",
        "product_id": "t-1",
        "product_json": json.dumps(
            {
                "marketplace": "temu",
                "product_id": "t-1",
                "title": "Старый товар Temu",
                "product_url": "https://example.com/t-1",
                "price": 5.0,
                "currency": "USD",
            }
        ),
    }

    #: A realistic favorite row as written by the removed Alibaba provider.
    LEGACY_ALIBABA_ROW = {
        "id": 8,
        "marketplace": "alibaba",
        "product_id": "a-1",
        "product_json": json.dumps(
            {
                "marketplace": "alibaba",
                "product_id": "a-1",
                "title": "Старый оптовый товар",
                "product_url": "https://example.com/a-1",
                "price": 2.0,
                "currency": "USD",
                "min_order_quantity": 100,
            }
        ),
    }

    #: A realistic AliExpress row written before a model field was removed.
    LEGACY_ALIEXPRESS_ROW = {
        "id": 9,
        "marketplace": "aliexpress",
        "product_id": "ae-1",
        "product_json": json.dumps(
            {
                "marketplace": "aliexpress",
                "product_id": "ae-1",
                "title": "Живой товар AliExpress",
                "product_url": "https://example.com/ae-1",
                "price": 12.5,
                "currency": "USD",
                # Removed from the model with the wholesale mode - it must
                # not swallow a perfectly usable AliExpress favourite.
                "min_order_quantity": 2,
            }
        ),
    }

    def _valid_row(self, marketplace: str, product_id: str) -> dict:
        return {
            "id": 1,
            "marketplace": marketplace,
            "product_id": product_id,
            "product_json": json.dumps(
                {
                    "marketplace": marketplace,
                    "product_id": product_id,
                    "title": "Товар",
                    "product_url": f"https://example.com/{product_id}",
                    "price": 9.0,
                    "currency": "USD",
                }
            ),
        }

    def test_legacy_removed_marketplace_snapshots_are_rejected_by_the_model(
        self,
    ) -> None:
        """The enum itself must refuse removed marketplaces - not just the UI."""

        from models.product import Product

        for row in (self.LEGACY_ROW, self.LEGACY_ALIBABA_ROW):
            with self.subTest(marketplace=row["marketplace"]):
                with self.assertRaises(Exception):
                    Product.model_validate(json.loads(row["product_json"]))

    def test_legacy_aliexpress_rows_with_removed_fields_still_show(self) -> None:
        """A removed *field* must not hide a living AliExpress favourite.

        Rows written by an earlier version contain keys the model no
        longer has (the wholesale fields lived on the shared product
        model). Those keys are filtered out when reading a snapshot, so
        existing favourites keep working - while a removed *marketplace*
        still fails validation and stays hidden.
        """

        database = FakeDatabase(favorites=[self.LEGACY_ALIEXPRESS_ROW])
        context = make_context(database)
        message = FakeMessage()

        run(favorites.show_favorites(FakeUpdate(message=message), context))

        items = context.user_data["favorites_items"]
        self.assertEqual(len(items), 1, "живая запись AliExpress потеряна")
        self.assertEqual(items[0]["product"].product_id, "ae-1")
        self.assertIn("Живой товар AliExpress", " ".join(message.texts))

    def test_legacy_rows_never_reach_the_screen(self) -> None:
        database = FakeDatabase(favorites=[self.LEGACY_ROW, self.LEGACY_ALIBABA_ROW])
        context = make_context(database)
        message = FakeMessage()

        run(favorites.show_favorites(FakeUpdate(message=message), context))

        self.assertTrue(message.texts, "избранное не ответило")
        shown = " ".join(message.texts)
        for removed in ("Temu", "temu", "Alibaba", "alibaba"):
            self.assertNotIn(removed, shown)
        # The unusable rows are dropped, so the honest "пока пусто" screen shows.
        self.assertIn("Избранное", shown)

    def test_only_aliexpress_rows_are_kept(self) -> None:
        """A mixed list keeps AliExpress and drops every removed marketplace."""

        database = FakeDatabase(
            favorites=[
                self._valid_row("aliexpress", "a-1"),
                self.LEGACY_ROW,
                self.LEGACY_ALIBABA_ROW,
            ]
        )
        context = make_context(database)
        message = FakeMessage()

        run(favorites.show_favorites(FakeUpdate(message=message), context))

        shown = " ".join(message.texts)
        self.assertNotIn("Temu", shown)
        self.assertNotIn("Alibaba", shown)
        self.assertEqual(len(context.user_data["favorites_items"]), 1)
        marketplaces = [
            getattr(item["product"].marketplace, "value", item["product"].marketplace)
            for item in context.user_data["favorites_items"]
        ]
        self.assertEqual(marketplaces, ["aliexpress"])


class MainMenuEndToEndTests(unittest.TestCase):
    """Every main-menu button renders a screen without raising.

    The menu dispatcher imports screens lazily, so a typo in a screen name
    would only surface as a NameError on a real button press. This walks
    the full callback table with fake Telegram objects instead.
    """

    def test_every_menu_button_is_reachable(self) -> None:
        context = make_context()

        for callback in MAIN_MENU_CALLBACKS:
            with self.subTest(callback=callback):
                query = FakeQuery(callback)
                update = FakeUpdate(callback_query=query)

                run(start.menu_callback(update, context))

                produced = bool(query.edits) or bool(context.bot.messages)
                self.assertTrue(produced, f"{callback} ничего не показал")


if __name__ == "__main__":
    unittest.main()

