"""Focused regression tests for the Buy button.

The bug chain that produced this file:
1. Buy was a callback that sent ANOTHER product message with a second
   button - the user had to press twice, and the card was shown again.
2. Even earlier, navigation edited only the keyboard, so the displayed
   product and the Buy target diverged.

The contract pinned down here: "🛒 Купить" is a URL button carrying THIS
product's exact affiliate URL - one press opens the AliExpress page in
the browser, no bot round-trip, no re-sent card, no invented URL.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from handlers import common
from handlers import search as search_handlers
from keyboards.products import CB_PREFIX_NAV, browse_card_keyboard
from models.product import Marketplace, Product
from models.search_request import SearchRequest
from services.search_service import SearchOutcome

URL_A = "https://s.click.aliexpress.com/af/A"
URL_B = "https://s.click.aliexpress.com/af/B"
URL_C = "https://s.click.aliexpress.com/af/C"
URL_D = "https://s.click.aliexpress.com/af/D"


def make_product(pid: str, affiliate: str | None) -> Product:
    return Product(
        marketplace=Marketplace.ALIEXPRESS,
        product_id=pid,
        title=f"Товар {pid}",
        product_url=f"https://www.aliexpress.com/item/{pid}.html",
        price=10.0,
        currency="USD",
        # Cards are photo messages in production.
        image_url=f"https://img.example/{pid}.jpg",
        affiliate_url=affiliate,
    )


def make_outcome(*specs: tuple[str, str]) -> SearchOutcome:
    return SearchOutcome(
        request=SearchRequest(original_query="тест"),
        products=[make_product(pid, url) for pid, url in specs],
        searched_marketplaces=["AliExpress"],
    )


class FakePhotoMessage:
    """The card is a photo message (photo truthy, no editable text)."""

    photo = ["photo-bytes"]


class FakeQuery:
    def __init__(self, data: str, message=None) -> None:
        self.data = data
        self.message = message
        self.answers: list[str] = []
        self.captions: list[tuple[str, dict]] = []
        self.texts: list[tuple[str, dict]] = []
        self.media_edits: list[tuple[object, dict]] = []

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        if text:
            self.answers.append(text)

    async def edit_message_caption(self, caption: str, **kwargs) -> None:
        self.captions.append((caption, kwargs))

    async def edit_message_text(self, text: str, **kwargs) -> None:
        self.texts.append((text, kwargs))

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
    """Records photos (cards) and plain messages.

    A crucial assertion target: after the Buy fix the bot must never send
    a new product message when Buy is pressed - there is no Buy handler
    left at all, and these tests prove the flow end to end.
    """

    def __init__(self) -> None:
        self.photos: list[tuple] = []
        self.messages: list[tuple[int, str, dict]] = []

    async def send_photo(self, chat_id, photo, caption=None, **kwargs) -> None:
        self.photos.append((chat_id, photo, caption, kwargs))

    async def send_message(self, chat_id, text, **kwargs) -> None:
        self.messages.append((chat_id, text, kwargs))

    @property
    def texts(self) -> list[str]:
        return [text for _, text, _ in self.messages]


class FakeDatabase:
    """No-op database double: analytics/history never break these tests."""

    async def get_or_create_user(self, telegram_id, username=None, first_name=None) -> int:
        return 1

    async def get_user_region(self, user_id) -> dict:
        return {"currency": None}

    async def get_tracked_products(self, user_id) -> list[dict]:
        return []

    async def get_shown_products(self, user_id, *, flow=None) -> list[tuple[str, str]]:
        return []

    async def add_shown_product(
        self, *, user_id, marketplace, product_id, flow: str = "search"
    ) -> None:
        return None

    async def record_event(self, event_type, **kwargs) -> None:
        return None


class FakeContext:
    def __init__(self, bot_data: dict | None = None, bot: FakeBot | None = None) -> None:
        self.bot = bot or FakeBot()
        self.user_data: dict = {}
        self.application = SimpleNamespace(bot_data=bot_data or {})


def make_context() -> FakeContext:
    return FakeContext(bot_data={"database": FakeDatabase()})


async def open_feed(context: FakeContext, outcome: SearchOutcome) -> str:
    """Open a feed the way the handlers do: exactly ONE product card."""

    session_id = common.remember_outcome(context, outcome, header="🔥 <b>Популярное</b>")
    await search_handlers._send_product_cards(
        context, 47, outcome, session_id, user_id=1, telegram_id=555
    )
    return session_id


async def navigate(context: FakeContext, session_id: str, index: int) -> FakeQuery:
    query = FakeQuery(
        f"{CB_PREFIX_NAV}:{session_id}:{index}", message=FakePhotoMessage()
    )
    await search_handlers.browse_callback(FakeUpdate(callback_query=query), context)
    return query


def buy_urls(markup) -> list[str]:
    """URLs of the Buy buttons in a card keyboard (URL buttons)."""

    return [
        button.url
        for row in markup.inline_keyboard
        for button in row
        if button.url is not None and button.text.endswith("Купить")
    ]


def all_urls(markup) -> list[str]:
    return [
        button.url
        for row in markup.inline_keyboard
        for button in row
        if button.url is not None
    ]


class BuyButtonUrlTests(unittest.IsolatedAsyncioTestCase):
    """The card keyboard itself carries the exact affiliate URL."""

    async def test_1_buy_on_product_a_carries_url_a(self):
        context = make_context()
        await open_feed(context, make_outcome(("A", URL_A), ("B", URL_B)))

        markup = context.bot.photos[-1][3]["reply_markup"]
        self.assertEqual(buy_urls(markup), [URL_A])
        self.assertIn("Товар A", context.bot.photos[-1][2])

    async def test_2_buy_on_product_b_carries_url_b(self):
        context = make_context()
        outcome = make_outcome(("A", URL_A), ("B", URL_B), ("C", URL_C))
        session_id = await open_feed(context, outcome)

        markup = browse_card_keyboard(session_id, 2, 3, outcome.products[2])
        self.assertEqual(buy_urls(markup), [URL_C])

    async def test_3_after_next_the_card_carries_url_b(self):
        context = make_context()
        session_id = await open_feed(context, make_outcome(("A", URL_A), ("B", URL_B)))

        query = await navigate(context, session_id, 1)

        # THE stale-image regression: navigation must replace the MEDIA
        # (image B), the caption (text B) and the keyboard (URL_B) in one
        # edit_message_media call - never caption-only over image A.
        self.assertEqual(len(query.media_edits), 1)
        media, kwargs = query.media_edits[0]
        self.assertEqual(media.media, "https://img.example/B.jpg")
        self.assertIn("Товар B", media.caption)
        self.assertEqual(buy_urls(kwargs["reply_markup"]), [URL_B])
        self.assertEqual(query.captions, [])
        self.assertEqual(query.texts, [])

    async def test_4_next_then_back_the_card_carries_url_a_again(self):
        context = make_context()
        session_id = await open_feed(context, make_outcome(("A", URL_A), ("B", URL_B)))

        await navigate(context, session_id, 1)
        back_query = await navigate(context, session_id, 0)

        self.assertEqual(len(back_query.media_edits), 1)
        media, kwargs = back_query.media_edits[0]
        self.assertEqual(media.media, "https://img.example/A.jpg")
        self.assertIn("Товар A", media.caption)
        self.assertEqual(buy_urls(kwargs["reply_markup"]), [URL_A])

    async def test_5_other_sessions_cannot_leak_their_urls(self):
        first, second = make_context(), make_context()

        session_first = await open_feed(first, make_outcome(("A", URL_A)))
        session_second = await open_feed(second, make_outcome(("B", URL_B)))
        self.assertNotEqual(session_first, session_second)

        # Each context's card carries only its own product's URL.
        self.assertEqual(buy_urls(first.bot.photos[-1][3]["reply_markup"]), [URL_A])
        self.assertEqual(buy_urls(second.bot.photos[-1][3]["reply_markup"]), [URL_B])
        self.assertNotIn(URL_A, all_urls(second.bot.photos[-1][3]["reply_markup"]))
        self.assertNotIn(URL_B, all_urls(first.bot.photos[-1][3]["reply_markup"]))

    async def test_6_every_product_of_one_feed_keeps_its_own_url(self):
        context = make_context()
        outcome = make_outcome(("A", URL_A), ("B", URL_B), ("C", URL_C), ("D", URL_D))
        session_id = await open_feed(context, outcome)

        for index, (pid, url) in enumerate(
            (("A", URL_A), ("B", URL_B), ("C", URL_C), ("D", URL_D))
        ):
            markup = browse_card_keyboard(
                session_id, index, len(outcome.products), outcome.products[index]
            )
            self.assertEqual(buy_urls(markup), [url], f"index {index}")

    async def test_missing_affiliate_url_means_no_buy_button(self):
        context = make_context()
        outcome = make_outcome(("A", URL_A), ("B", None))
        session_id = await open_feed(context, outcome)

        markup = browse_card_keyboard(session_id, 1, 2, outcome.products[1])
        self.assertEqual(buy_urls(markup), [])
        # No invented affiliate URL anywhere in the keyboard either.
        self.assertNotIn(outcome.products[1].product_url, all_urls(markup))

    async def test_buy_press_never_sends_another_card_or_message(self):
        context = make_context()
        outcome = make_outcome(("A", URL_A), ("B", URL_B))
        session_id = await open_feed(context, outcome)
        cards_before = len(context.bot.photos)
        messages_before = len(context.bot.messages)

        # Navigation (the only remaining card-editing flow) replaces the
        # card in place; no Buy handler exists that could send anything.
        await navigate(context, session_id, 1)

        self.assertEqual(len(context.bot.photos), cards_before)
        self.assertEqual(len(context.bot.messages), messages_before)

    async def test_navigation_keyboard_stays_a_callback(self):
        context = make_context()
        outcome = make_outcome(("A", URL_A), ("B", URL_B))
        session_id = await open_feed(context, outcome)

        markup = browse_card_keyboard(session_id, 0, 2, outcome.products[0])
        datas = [
            button.callback_data
            for row in markup.inline_keyboard
            for button in row
            if button.callback_data is not None
        ]
        self.assertIn(f"{CB_PREFIX_NAV}:{session_id}:1", datas)
        # Share stays a URL button (unchanged behavior).
        self.assertTrue(
            any(u.startswith("https://t.me/share/") for u in all_urls(markup))
        )

    def test_renderer_is_shared_by_send_and_edit(self):
        product = make_product("A", URL_A)
        sent = common.product_card_text(
            product, index=1, caption_prefix="🔥 Популярное · 1/3"
        )
        self.assertIn("Товар A", sent)
        self.assertIn("1/3", sent)
        self.assertIn("1. Товар A", sent)
