"""Cross-flow state isolation for searches, the popular feed and card buttons.

Three production reports are pinned down here:

1. «🔥 Популярное» showed its end-of-rotation screen while the user had just
   started or was searching normally - the rotation's exhausted state was
   decided by a history that ordinary searches also wrote to;
2. a normal search answered with a product of a PREVIOUS search (the reported
   «Плазморезка» -> «Защитный ремешок для умных часов»);
3. an old card's buttons operated on the state of a newer, unrelated search.

The fix is scope, not a new architecture:

* the ROTATION owns its shown-product scope and its own cursor
  (``popular_shown.flow`` / ``popular_state``), so a search can neither empty
  the feed nor make it claim it is over;
* the search-like flows own THEIR scope, so what the feed displayed never
  hides a search result;
* every cached session keeps its own product list and index, and a new search
  always starts a new one.

Everything runs offline against a real SQLite file and stub marketplaces.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from database.database import Database
from database.models import FLOW_FEED, FLOW_SEARCH
from handlers import common, discovery
from handlers import search as search_handlers
from handlers.search import search_message
from keyboards.products import CB_PREFIX_NAV
from models.product import Marketplace, Product
from services.ai_service import AIService
from services.search_service import SearchService


def run(coro):
    return asyncio.run(coro)


def listing(product_id: str, title: str, *, affiliate: bool = True) -> Product:
    """A real-shaped listing; the feed only shows picture+affiliate ones."""

    return Product(
        marketplace=Marketplace.ALIEXPRESS,
        product_id=product_id,
        title=title,
        product_url=f"https://www.aliexpress.com/item/{product_id}.html",
        image_url=f"https://ae01.alicdn.com/kf/{product_id}.jpg",
        price=10.0,
        currency="USD",
        affiliate_url=(
            f"https://s.click.aliexpress.com/e/_{product_id}" if affiliate else None
        ),
    )


class CatalogStub:
    """A marketplace double: per-phrase search plus one popular feed."""

    marketplace = Marketplace.ALIEXPRESS
    display_name = "AliExpress"
    supports_feed = True

    def __init__(
        self,
        by_phrase: dict[str, list[Product]] | None = None,
        feed: list[Product] | None = None,
    ) -> None:
        self.by_phrase = by_phrase or {}
        self.feed = feed or []
        self.queries: list[str] = []
        self.feed_calls: list[int] = []

    @property
    def is_configured(self) -> bool:
        return True

    async def search_products(self, request) -> list[Product]:
        phrase = request.normalized_query or request.original_query or ""
        self.queries.append(phrase)
        return list(self.by_phrase.get(phrase, []))

    async def fetch_feed_page(
        self,
        *,
        page: int = 1,
        currency: str | None = None,
        country: str | None = None,
        exclude=None,
        limit: int | None = None,
    ) -> list[Product]:
        self.feed_calls.append(page)
        products = list(self.feed)
        if limit is not None:
            products = products[: max(1, int(limit))]
        return products

    async def aclose(self) -> None:
        return None


class TextMessage:
    """A Telegram message usable as a search query and as a card target."""

    def __init__(self, text: str, chat_id: int = 47) -> None:
        self.text = text
        self.chat_id = chat_id
        self.photo: list = []
        self.texts: list[str] = []

    async def reply_text(self, text: str, **kwargs) -> None:
        self.texts.append(text)


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


class FakeSentMessage:
    """The card message currently on screen: every listing has an image,
    so the card is a PHOTO message and browsing rewrites its media."""

    chat_id = 47
    message_id = 1
    photo: list = [object()]


class FakeQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self.answers: list[str] = []
        self.edits: list[str] = []
        self.edit_kwargs: list[dict] = []
        self.message = FakeSentMessage()

    async def edit_message_media(self, media, reply_markup=None, **kwargs) -> None:
        self.edits.append(getattr(media, "caption", None) or "")
        self.edit_kwargs.append(kwargs)

    async def edit_message_caption(self, caption=None, **kwargs) -> None:
        self.edits.append(caption or "")
        self.edit_kwargs.append(kwargs)

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        if text:
            self.answers.append(text)

    async def edit_message_text(self, text: str, **kwargs) -> None:
        self.edits.append(text)
        self.edit_kwargs.append(kwargs)


class FakeChat:
    id = 47


class FakeUser:
    def __init__(self, user_id: int = 555) -> None:
        self.id = user_id
        self.username = f"user{user_id}"
        self.first_name = f"User {user_id}"


class FakeUpdate:
    def __init__(self, *, message=None, query=None, user_id: int = 555) -> None:
        self.callback_query = query
        self.effective_message = message
        self.effective_chat = FakeChat() if (message or query) else None
        self.effective_user = FakeUser(user_id)


class FakeApplication:
    def __init__(self, bot_data: dict) -> None:
        self.bot_data = bot_data


class FakeContext:
    def __init__(self, bot_data: dict) -> None:
        self.bot = FakeBot()
        self.user_data: dict = {}
        self.application = FakeApplication(bot_data)


class SessionIsolationTests(unittest.TestCase):
    """One user, one chat, one SQLite file - and separate sessions."""

    STRAP = "Защитный ремешок для умных часов, силиконовый браслет"
    STRAP_TWO = "Ремешок кожаный для часов 20 мм"
    PLASMA = "Плазморезка CUT-50 плазменный резак 220В"
    PLASMA_TWO = "Плазморезка инверторная 40А"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = str(Path(self._tmp.name) / "isolation.db")
        self.db = Database(self.db_path)
        run(self.db.initialize())
        run(self.db.get_or_create_user(555, "tester", "Tester"))
        self.provider = CatalogStub(
            by_phrase={
                "ремешок часов": [
                    listing("strap", self.STRAP),
                    listing("strap2", self.STRAP_TWO),
                ],
                "плазморезка": [
                    listing("plasma", self.PLASMA),
                    listing("plasma2", self.PLASMA_TWO),
                ],
            },
            feed=[
                listing("a1", "Беспроводные наушники TWS Pro", affiliate=True),
                listing("b2", "Механическая клавиатура RGB", affiliate=True),
            ],
        )

    # -- plumbing ---------------------------------------------------------

    def context(self) -> FakeContext:
        return FakeContext(
            bot_data={
                "database": self.db,
                "search_service": SearchService([self.provider]),
                "ai_service": AIService(api_key=None),
            }
        )

    def search(self, context: FakeContext, text: str) -> None:
        run(search_message(FakeUpdate(message=TextMessage(text)), context))

    def open_popular(self, context: FakeContext) -> FakeQuery:
        query = FakeQuery("menu:popular")
        run(discovery.show_feed(FakeUpdate(query=query), context))
        return query

    def sessions(self, context: FakeContext) -> dict:
        return context.user_data[common.OUTCOME_CACHE_KEY]

    def card(self, context: FakeContext) -> str:
        """The product card currently on screen.

        Every listing carries an image, so the card is a PHOTO message and
        its text lives in the caption of the last ``send_photo``.
        """
        for _, kwargs in reversed(context.bot.photos):
            caption = kwargs.get("caption")
            if caption:
                return caption
        self.assertTrue(context.bot.texts, "a product card must be on screen")
        return context.bot.texts[-1]

    def screen(self, context: FakeContext) -> str:
        """Everything this user can read right now (texts + card captions)."""
        return "\n".join(
            list(context.bot.texts)
            + [str(kwargs.get("caption") or "") for _, kwargs in context.bot.photos]
        )

    # -- A. the popular state does not leak into a search -----------------

    def test_a_search_after_an_exhausted_rotation_shows_its_own_product(self) -> None:
        context = self.context()
        # The rotation is exhausted for this user: its whole pool was shown.
        run(self.db.add_feed_shown(1, "aliexpress", "a1"))
        run(self.db.add_feed_shown(1, "aliexpress", "b2"))
        query = self.open_popular(context)
        self.assertTrue(query.edits)
        self.assertIn("повторно", query.edits[-1])

        # The user then searches something else and gets THAT product.
        self.search(context, "плазморезка")

        self.assertIn("Плазморезка", self.card(context))
        screen = self.screen(context)
        self.assertNotIn("Популярное</b>", screen)
        self.assertNotIn("повторно бот не будет", screen)

    def test_the_rotation_is_not_exhausted_by_a_search_history(self) -> None:
        """A search must never make the feed claim «новых товаров нет»."""

        context = self.context()
        # This user searched and saw the very product the feed returns: it is
        # recorded in the SEARCH scope only.
        run(self.db.add_shown_product(1, "aliexpress", "a1"))

        query = self.open_popular(context)

        self.assertIn("Беспроводные наушники TWS Pro", self.card(context))
        self.assertEqual(query.edits, [])

    def test_a_search_is_not_emptied_by_the_popular_rotation(self) -> None:
        """What the feed displayed must never hide a search result."""

        context = self.context()
        # The rotation showed the plasma cutter earlier...
        run(self.db.add_feed_shown(1, "aliexpress", "plasma"))

        # ...and a search for that product still finds it.
        self.search(context, "плазморезка")

        self.assertIn("Плазморезка", self.card(context))
        self.assertEqual(list(dict.fromkeys(self.provider.queries)), ["плазморезка"])

    def test_the_explicit_restart_resets_only_the_rotation(self) -> None:
        context = self.context()
        self.open_popular(context)
        self.search(context, "плазморезка")

        run(
            discovery.feed_reset_callback(
                FakeUpdate(query=FakeQuery("pop:reset")), context
            )
        )

        # The restarted feed continues from ANOTHER part of the selection
        # (never the same first cards). G24: one open is bounded by
        # ``FEED_MAX_PAGES_PER_OPEN`` (2), so the first press walks 1..2,
        # and the reset press - history cleared, cursor kept - continues
        # at the persisted cursor with 3..4.
        self.assertEqual(self.provider.feed_calls, [1, 2, 3, 4])
        self.assertEqual(context.user_data[discovery.FEED_PAGE_KEY], 5)
        # ...the search-like flows keep their own state - a search never
        # advances the rotation and the reset never wipes a search's novelty.
        self.assertIn(
            ("aliexpress", "plasma"),
            run(self.db.get_shown_products(1, flow=FLOW_SEARCH)),
        )
        self.assertNotIn(
            ("aliexpress", "plasma"),
            run(self.db.get_shown_products(1, flow=FLOW_FEED)),
        )

    def test_a_restarted_worker_continues_the_rotation(self) -> None:
        """A new handler instance (a restarted bot) is not back at step 1."""

        first = self.context()
        self.open_popular(first)  # steps 1..3 -> the next cursor is persisted
        restarted = self.context()  # fresh RAM, same user and same database

        self.open_popular(restarted)

        # G24: press 1 walked pages 1..2 (the ``FEED_MAX_PAGES_PER_OPEN``
        # bound), the restarted worker resumes at the persisted cursor and
        # walks 3..4 - never back at page 1.
        self.assertEqual(self.provider.feed_calls, [1, 2, 3, 4])
        self.assertIn("Механическая клавиатура RGB", self.card(restarted))
        self.assertEqual(run(self.db.get_feed_page(1)), 5)

    # -- B. a new search starts a new session -----------------------------

    def test_a_new_search_never_inherits_the_previous_query(self) -> None:
        context = self.context()

        self.search(context, "ремешок часов")
        first_card = self.card(context)
        first_session = next(iter(self.sessions(context)))
        first_queries = len(self.provider.queries)

        self.search(context, "плазморезка")
        second_card = self.card(context)
        sessions = self.sessions(context)

        self.assertIn("ремешок", first_card)
        self.assertIn("Плазморезка", second_card)
        self.assertNotIn("ремешок", second_card)
        # Every phrase the second search asked was grounded in ITS own query.
        second_queries = self.provider.queries[first_queries:]
        self.assertTrue(second_queries)
        for phrase in second_queries:
            with self.subTest(phrase=phrase):
                self.assertIn("плазморез", phrase)
        # Both sessions are cached separately, each with its own products.
        self.assertEqual(len(sessions), 2)
        self.assertIn(first_session, sessions)
        self.assertEqual(
            [p.product_id for p in sessions[first_session].products],
            ["strap", "strap2"],
        )

    def test_a_stale_card_browses_its_own_session_only(self) -> None:
        context = self.context()
        self.search(context, "ремешок часов")
        old_session = next(iter(self.sessions(context)))

        self.search(context, "плазморезка")
        sessions = self.sessions(context)
        new_session = [key for key in sessions if key != old_session][0]
        new_products = [p.product_id for p in sessions[new_session].products]

        # ➡️ pressed on the OLD card of the previous search.
        query = FakeQuery(f"{CB_PREFIX_NAV}:{old_session}:1")
        run(search_handlers.browse_callback(FakeUpdate(query=query), context))

        self.assertEqual(len(query.edits), 1)
        self.assertIn("кожаный", query.edits[-1])
        self.assertNotIn("Плазморезка", query.edits[-1])
        # The newer session kept its own position and its own products.
        self.assertEqual(
            [p.product_id for p in self.sessions(context)[new_session].products],
            new_products,
        )
        self.assertEqual(
            context.user_data[common.OUTCOME_META_KEY][new_session].get("max_index"),
            None,
        )


if __name__ == "__main__":
    unittest.main()

