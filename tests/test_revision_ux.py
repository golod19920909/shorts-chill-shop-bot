"""Tests for the UX revision: one-card back navigation, analytics events,
honest compare, tracking status and error event recording."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from handlers import common
from handlers import search as search_handlers
from handlers import settings as settings_handlers
from handlers.settings import (
    TRACKING_STATUS_AUTO,
    tracking_status_line,
)
from keyboards.products import CB_PREFIX_NAV, browse_card_keyboard
from models.product import Marketplace, Product
from models.search_request import SearchRequest
from services import analytics
from services.price_tracker import build_price_tracker
from services.search_service import SearchOutcome
from bot import error_handler
from telegram.error import BadRequest


def make_product(index: int = 0, *, affiliate: bool = True) -> Product:
    return Product(
        marketplace=Marketplace.ALIEXPRESS,
        product_id=str(100500 + index),
        title=f"Тестовый товар {index}",
        product_url=f"https://www.aliexpress.com/item/{100500 + index}.html",
        price=10.0 + index,
        currency="USD",
        orders_count=100 + index,
        image_url=f"https://img.example/{100500 + index}.jpg",
        affiliate_url=(
            f"https://s.click.aliexpress.com/e?af={100500 + index}"
            if affiliate
            else None
        ),
    )


def make_outcome(count: int = 3) -> SearchOutcome:
    return SearchOutcome(
        request=SearchRequest(original_query="тест"),
        products=[make_product(i) for i in range(count)],
        searched_marketplaces=["AliExpress"],
    )


class RecordingDatabase:
    """Captures analytics events without touching SQLite."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    async def get_or_create_user(self, telegram_id, username=None, first_name=None):
        return 1

    async def record_event(self, event_type, **kwargs):
        self.events.append({"type": event_type, **kwargs})

    async def get_tracked_products(self, user_id) -> list[dict]:
        """No tracking by default: cards show "🔔 Следить за ценой"."""

        return []

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


class FakeQuery:
    def __init__(self, data: str, message=None) -> None:
        self.data = data
        self.message = message
        self.answers: list[str] = []
        self.edits: list[tuple[str, dict]] = []
        self.captions: list[tuple[str, dict]] = []
        self.markup_edits: list = []
        self.media_edits: list[tuple[object, dict]] = []

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        if text:
            self.answers.append(text)

    async def edit_message_text(self, text: str, **kwargs) -> None:
        self.edits.append((text, kwargs))

    async def edit_message_caption(self, caption: str, **kwargs) -> None:
        self.captions.append((caption, kwargs))

    async def edit_message_reply_markup(self, reply_markup=None, **kwargs) -> None:
        self.markup_edits.append(reply_markup)

    async def edit_message_media(self, media, **kwargs) -> None:
        self.media_edits.append((media, kwargs))


class FakeChat:
    id = 47


class FakeUser:
    id = 555
    username = "tester"
    first_name = "Tester"


class FakeUpdate:
    def __init__(self, *, callback_query=None) -> None:
        self.callback_query = callback_query
        self.effective_message = None
        self.effective_chat = FakeChat() if callback_query else None
        self.effective_user = FakeUser()


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str, dict]] = []
        self.photos: list[tuple[int, str, dict]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
        self.messages.append((chat_id, text, kwargs))

    async def send_photo(self, chat_id, photo, caption=None, **kwargs) -> None:
        self.photos.append((chat_id, photo, kwargs))

    @property
    def texts(self) -> list[str]:
        return [text for _, text, _ in self.messages]


class FakeContext:
    def __init__(self, bot_data: dict | None = None, bot: FakeBot | None = None) -> None:
        self.bot = bot or FakeBot()
        self.user_data: dict = {}
        self.application = SimpleNamespace(bot_data=bot_data or {})


def make_context(database: RecordingDatabase | None = None, **bot_data) -> FakeContext:
    data = {"database": database or RecordingDatabase()}
    data.update(bot_data)
    return FakeContext(bot_data=data)


def keyboard_data(markup) -> list[str]:
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data is not None
    ]


class CardBackKeyboardTests(unittest.TestCase):
    def test_back_on_the_first_product_leaves_the_feed(self):
        """There is no summary screen: back goes to a real screen."""

        markup = browse_card_keyboard("sess", 0, 3, make_product())
        data = keyboard_data(markup)
        self.assertIn("menu:back", data)
        self.assertFalse(any(item.startswith("pback:") for item in data))

    def test_back_on_the_first_product_prefers_the_callers_screen(self):
        markup = browse_card_keyboard(
            "sess", 0, 3, make_product(), back_callback="menu:categories"
        )
        self.assertIn("menu:categories", keyboard_data(markup))

    def test_back_on_later_products_moves_to_previous(self):
        markup = browse_card_keyboard("sess", 2, 3, make_product())
        data = keyboard_data(markup)
        self.assertIn(f"{CB_PREFIX_NAV}:sess:1", data)
        # ...and never a summary screen (it no longer exists).
        self.assertFalse(any(item.startswith("pback:") for item in data))

    def test_next_then_restart_on_the_last_product(self):
        first = browse_card_keyboard("sess", 0, 3, make_product())
        self.assertIn(f"{CB_PREFIX_NAV}:sess:1", keyboard_data(first))
        last = browse_card_keyboard("sess", 2, 3, make_product())
        self.assertIn(f"{CB_PREFIX_NAV}:sess:0", keyboard_data(last))

    def test_buy_button_only_with_a_real_affiliate_url(self):
        with_affiliate = browse_card_keyboard("sess", 0, 1, make_product())
        buy_urls = [
            button.url
            for row in with_affiliate.inline_keyboard
            for button in row
            if button.url is not None and button.text.endswith("Купить")
        ]
        self.assertEqual(buy_urls, [make_product().affiliate_url])

        without = browse_card_keyboard("sess", 0, 1, make_product(affiliate=False))
        no_urls = [
            button.url
            for row in without.inline_keyboard
            for button in row
            if button.url is not None and button.text.endswith("Купить")
        ]
        self.assertEqual(no_urls, [])


class SummaryCaptionTests(unittest.TestCase):
    """``_summary_caption`` adapts long HTML text to Telegram's caption cap.

    It is still used by the end-of-rotation screen of «🔥 Популярное», which
    can replace a PHOTO card with a text screen.
    """

    def test_a_short_text_keeps_its_markup(self):
        caption, parse_mode = search_handlers._summary_caption("🔥 <b>Популярное</b>")
        self.assertEqual(caption, "🔥 <b>Популярное</b>")
        self.assertEqual(parse_mode, "HTML")

    def test_overlong_text_is_clipped_without_broken_html(self):
        """A caption longer than the limit must not cut an HTML tag in half
        (Telegram would reject the whole edit)."""

        long_text = "🔥 <b>Популярное</b>\n" + "очень длинное описание ленты " * 80
        self.assertGreater(len(long_text), 1024)

        caption, parse_mode = search_handlers._summary_caption(long_text)

        self.assertLessEqual(len(caption), 1024)
        self.assertNotIn("<", caption)
        self.assertIsNone(parse_mode)
        self.assertIn("Популярное", caption)


class ProductShownTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_card_is_counted_as_product_shown(self):
        db = RecordingDatabase()
        context = make_context(db)
        outcome = make_outcome(count=3)
        session_id = common.remember_outcome(context, outcome)

        await search_handlers._send_product_cards(
            context, 47, outcome, session_id, user_id=1, telegram_id=555
        )

        shown = [e for e in db.events if e["type"] == analytics.EVENT_PRODUCT_SHOWN]
        self.assertEqual(len(shown), 1)
        self.assertEqual(shown[0]["product_id"], outcome.products[0].product_id)
        self.assertEqual(shown[0]["telegram_id"], 555)
        # Exactly one card message (a photo): no chat flooding.
        self.assertEqual(len(context.bot.photos), 1)
        self.assertEqual(len(context.bot.messages), 0)

    async def test_stepping_to_the_next_product_counts_a_new_show(self):
        db = RecordingDatabase()
        context = make_context(db)
        outcome = make_outcome(count=3)
        session_id = common.remember_outcome(context, outcome)

        query = FakeQuery(
            f"{CB_PREFIX_NAV}:{session_id}:1",
            message=SimpleNamespace(photo=["photo-bytes"]),
        )
        await search_handlers.browse_callback(FakeUpdate(callback_query=query), context)

        shown = [e for e in db.events if e["type"] == analytics.EVENT_PRODUCT_SHOWN]
        self.assertEqual(len(shown), 1)
        self.assertEqual(shown[0]["product_id"], outcome.products[1].product_id)

        # The stale-image regression: Next must replace the MEDIA (image
        # of product 1), the caption (text of product 1) and the keyboard
        # (Buy URL of product 1) - all together, never caption-only.
        self.assertEqual(len(query.media_edits), 1)
        media, kwargs = query.media_edits[0]
        self.assertEqual(media.media, "https://img.example/100501.jpg")
        self.assertIn("Тестовый товар 1", media.caption)
        markup = kwargs["reply_markup"].inline_keyboard
        buy_urls = [
            button.url
            for row in markup
            for button in row
            if button.url is not None and button.text.endswith("Купить")
        ]
        self.assertEqual(buy_urls, [outcome.products[1].affiliate_url])
        # The 🔔 tracking toggle is part of the same keyboard.
        track_labels = [
            button.text
            for row in markup
            for button in row
            if button.text and "Следить за ценой" in button.text
        ]
        self.assertEqual(track_labels, ["🔔 Следить за ценой"])


class BuyDirectUrlTests(unittest.IsolatedAsyncioTestCase):
    """Buy is a URL button: one press, direct open, no bot round-trip.

    These tests replaced the previous callback-flow tests: Telegram gives
    the bot no signal about URL-button presses, so a Buy press cannot be
    recorded as analytics - and it can never send an extra message.
    """

    async def test_buy_opens_the_exact_affiliate_url_directly(self):
        context = make_context(RecordingDatabase())
        outcome = make_outcome(count=2)
        session_id = common.remember_outcome(context, outcome)

        markup = browse_card_keyboard(session_id, 1, 2, outcome.products[1])
        buy_urls = [
            button.url
            for row in markup.inline_keyboard
            for button in row
            if button.url is not None and button.text.endswith("Купить")
        ]
        self.assertEqual(buy_urls, [outcome.products[1].affiliate_url])

        # A Buy press is handled by the Telegram client itself: the bot
        # sends nothing in response - no extra card, no link message.
        self.assertFalse(hasattr(search_handlers, "buy_callback"))
        self.assertEqual(context.bot.texts, [])

    async def test_products_without_affiliate_url_have_no_buy_button(self):
        context = make_context(RecordingDatabase())
        outcome = make_outcome(count=2)
        outcome.products = [make_product(0, affiliate=False)]
        session_id = common.remember_outcome(context, outcome)

        markup = browse_card_keyboard(session_id, 0, 1, outcome.products[0])
        buy_buttons = [
            button
            for row in markup.inline_keyboard
            for button in row
            if button.text.endswith("Купить")
        ]
        self.assertEqual(buy_buttons, [])
        # And the plain product URL is never promoted into a Buy link.
        self.assertNotIn(
            outcome.products[0].product_url,
            [button.url for row in markup.inline_keyboard for button in row],
        )


class TrackingStatusTests(unittest.TestCase):
    def test_auto_when_tracker_and_interval(self):
        context = FakeContext(
            bot_data={
                "price_tracker": object(),
                "settings": SimpleNamespace(tracking_interval_minutes=30),
            }
        )
        line = tracking_status_line(context)
        self.assertEqual(line, TRACKING_STATUS_AUTO.format(cadence="раз в 30 мин."))
        self.assertIn("🟢", line)
        self.assertIn("30 мин", line)

    def test_the_24_hour_default_reads_as_a_day(self):
        """The scheduler works in minutes; the screen must not say 1440 мин."""

        context = FakeContext(
            bot_data={
                "price_tracker": object(),
                "settings": SimpleNamespace(tracking_interval_minutes=1440),
            }
        )
        line = tracking_status_line(context)
        self.assertIn("раз в 1 дн.", line)
        self.assertNotIn("1440", line)

    def test_whole_hours_are_shown_as_hours(self):
        context = FakeContext(
            bot_data={
                "price_tracker": object(),
                "settings": SimpleNamespace(tracking_interval_minutes=360),
            }
        )
        self.assertIn("раз в 6 ч.", tracking_status_line(context))

    def test_manual_without_tracker(self):
        context = FakeContext(
            bot_data={
                "price_tracker": None,
                "settings": SimpleNamespace(tracking_interval_minutes=1440),
            }
        )
        self.assertIn("🟡", tracking_status_line(context))

    def test_manual_on_zero_interval(self):
        context = FakeContext(
            bot_data={
                "price_tracker": object(),
                "settings": SimpleNamespace(tracking_interval_minutes=0),
            }
        )
        self.assertIn("🟡", tracking_status_line(context))


class CardCaptionEscapingTests(unittest.TestCase):
    """The browsing headline must reach the user as markup, not as text."""

    def test_feed_headline_is_not_double_escaped(self) -> None:
        outcome = make_outcome(count=7)
        caption = search_handlers._card_caption(outcome, 0, "🔥 <b>Популярное</b>")
        text = common.product_card_text(
            make_product(0), index=None, caption_prefix=caption
        )

        # Verified live: the header was escaped in _card_caption and again in
        # product_card_text, so the caption showed "&lt;b&gt;...\" to the user.
        self.assertNotIn("&lt;", text)
        self.assertNotIn("&amp;", text)
        self.assertIn("<b>Популярное</b>", text)

    def test_a_plain_search_carries_no_query_or_position(self) -> None:
        """§4: the user knows their own query - it is never echoed back.

        A plain search card has NO headline at all: the product data
        starts the message, and neither the query, a position («3/3») nor
        a page marker may appear anywhere in the caption.
        """
        outcome = make_outcome(count=3)
        caption = search_handlers._card_caption(outcome, 2, None)

        self.assertEqual(caption, "")
        self.assertNotIn("тест", caption)
        self.assertNotIn("3/3", caption)
        self.assertNotIn("Страница", caption)

    def test_cards_carry_no_numbered_prefix(self) -> None:
        text = common.product_card_text(make_product(0), index=None)

        self.assertTrue(text.startswith("Тестовый товар 0"))
        self.assertNotIn("1. Тестовый товар 0", text)

    def test_product_text_is_escaped_exactly_once(self) -> None:
        product = make_product(0).model_copy(update={"title": "Наушники <b>Pro</b> & Co"})
        text = common.product_card_text(product, index=0)

        self.assertIn("&lt;b&gt;Pro&lt;/b&gt;", text)
        self.assertNotIn("&amp;lt;", text)


class ErrorHandlerEventTests(unittest.IsolatedAsyncioTestCase):
    async def test_error_is_recorded_without_any_details(self):
        db = RecordingDatabase()
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={"database": db}),
            error=ValueError("boom with possible token abc123"),
        )

        await error_handler(None, context)  # update=None: no reply branch

        errors = [e for e in db.events if e["type"] == analytics.EVENT_ERRORS]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["metadata"]["exception"], "ValueError")
        # The message text (which could carry a token) is never stored.
        self.assertNotIn("boom", str(errors[0]["metadata"]))

    async def test_a_no_op_message_edit_is_not_an_error(self):
        """Pressing the already-selected currency must not raise an alert.

        Verified live: ``query.edit_message_text`` with unchanged content
        makes Telegram answer ``BadRequest("Message is not modified")``;
        the handler treated it as a failure and the user was told about an
        "internal error" while the screen was in fact already correct.
        """

        db = RecordingDatabase()
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={"database": db}),
            error=BadRequest(
                "Message is not modified: specified new message content and "
                "reply markup are exactly the same as a current content"
            ),
        )

        await error_handler(None, context)

        self.assertEqual(
            [e for e in db.events if e["type"] == analytics.EVENT_ERRORS], []
        )

    async def test_other_bad_requests_are_still_errors(self):
        db = RecordingDatabase()
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={"database": db}),
            error=BadRequest("Chat not found"),
        )

        await error_handler(None, context)

        errors = [e for e in db.events if e["type"] == analytics.EVENT_ERRORS]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["metadata"]["exception"], "BadRequest")


class AnalyticsVocabularyTests(unittest.TestCase):
    def test_labels_cover_every_known_event(self):
        self.assertEqual(set(analytics.EVENT_LABELS), set(analytics.KNOWN_EVENTS))

    def test_untrackable_share_event_is_gone(self):
        # "🔗 Поделиться" is a URL button: Telegram reports nothing about
        # its presses, so a share_clicked event can only be a lie.
        self.assertNotIn("share_clicked", analytics.KNOWN_EVENTS)
        self.assertNotIn("share_clicked", analytics.EVENT_LABELS)

    def test_new_events_are_registered(self):
        for name in (
            analytics.EVENT_PRODUCT_SHOWN,
            analytics.EVENT_BUY_CLICKED,
            analytics.EVENT_PRODUCT_OPENED,
            analytics.EVENT_ERRORS,
        ):
            self.assertIn(name, analytics.KNOWN_EVENTS)


class BuildTrackerTests(unittest.TestCase):
    def test_none_without_configured_provider(self):
        class Provider:
            is_configured = False

        service = SimpleNamespace(providers=[Provider()])
        self.assertIsNone(build_price_tracker(object(), service, object()))

    def test_uses_the_first_configured_provider(self):
        class Provider:
            def __init__(self, configured: bool) -> None:
                self.is_configured = configured

        service = SimpleNamespace(
            providers=[Provider(False), Provider(True), Provider(True)]
        )
        tracker = build_price_tracker(object(), service, object())
        self.assertIsNotNone(tracker)

    def test_none_without_any_provider(self):
        service = SimpleNamespace(providers=[])
        self.assertIsNone(build_price_tracker(object(), service, object()))
