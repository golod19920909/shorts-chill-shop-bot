"""End-to-end tests for the curation layer inside the live delivery flows.

The three halves of step 3 are exercised together: a MOCK AliExpress provider
answers ``SearchService`` with a real candidate pool, the feed goes through
``handlers.search.send_search_results`` - the single choke point every screen of
the bot uses (search, categories, popular, photo) - and the cards that reach the
user are written to a REAL in-memory SQLite database. The interesting questions
are all end-to-end ones:

* is the opened card recorded in ``user_view_history``?
* does the next open skip (or defer) it instead of repeating it as new?
* does the ➡️ «Следующий» walk the CURATED list and record each card?
* does one mode's history break another mode's screen?

Nothing here reaches the network: the provider is a stub, the pool is
hand-written, and the database lives in memory.
"""

from __future__ import annotations

import unittest

import httpx

from config.settings import MarketplaceCredentials
from curation import MODE_CATEGORY, MODE_DEALS, MODE_RECOMMENDATION, MODE_SEARCH
from curation.integration import (
    CurationGate,
    mode_for_feed,
    mode_for_request,
    view_ids,
)
from database.database import MEMORY_DB_PATH, Database
from database.models import FLOW_FEED, FLOW_SEARCH
from handlers import common as handler_common
from handlers import search as search_handlers
from models.product import Marketplace, Product
from models.search_request import SearchMode, SearchRequest, SortPreference
from services.marketplaces.base import MarketplaceProvider
from services.search_service import SearchService

#: The query the stub marketplace answers (every title states it, so the
#: relevance gate of ``SearchService`` keeps the whole pool).
QUERY = "клавиатура"

#: Eight listings of ONE kind but eight different products: no two of them are
#: near-duplicates, and none carries another product's number.
TITLES = (
    "Механическая клавиатура RGB Red Switch",
    "Игровая клавиатура беспроводная с подсветкой",
    "Клавиатура механическая компактная 60%",
    "Клавиатура для ноутбука USB",
    "Клавиатура беспроводная для планшета",
    "Клавиатура игровая мембранная",
    "Клавиатура офисная тихая",
    "Клавиатура мини с подсветкой",
)


def catalogue(count: int = len(TITLES)) -> list[Product]:
    """The pool the stub marketplace answers with (real fields, no invention).

    Meets BOTH published quality profiles (spec) so the tests watch history
    and ranking rather than the gates: prices $10..$17 (inside the
    recommendation window), orders >= 300 (recommendation floor) and stated
    ratings >= 4.5 (both floors; the strictest fixture value wins).
    """

    return [
        Product(
            marketplace=Marketplace.ALIEXPRESS,
            product_id=f"p{index}",
            title=TITLES[index],
            product_url=f"https://www.aliexpress.com/item/p{index}.html",
            price=10.0 + index,
            currency="USD",
            orders_count=1000 - index,
            rating=4.5 + index / 40,
            positive_rate=90.0 + index,
        )
        for index in range(count)
    ]


class StubAliExpress(MarketplaceProvider):
    """A mock AliExpress provider: answers with the pool, counts the calls."""

    marketplace = Marketplace.ALIEXPRESS
    display_name = "AliExpress (stub)"
    supports_feed = True

    def __init__(self, products: list[Product] | None = None) -> None:
        super().__init__(MarketplaceCredentials("key", "secret"), httpx.AsyncClient())
        self.pool = list(products if products is not None else catalogue())
        self.search_calls = 0

    async def search_products(self, request: SearchRequest) -> list[Product]:
        self.search_calls += 1
        return list(self.pool)

    async def get_product(
        self, product_id: str, currency: str | None = None
    ) -> Product | None:
        return next((p for p in self.pool if p.product_id == product_id), None)

    def generate_affiliate_link(self, product: Product) -> str | None:
        return None


class CurationModeTests(unittest.TestCase):
    """Which mode each flow asks the pipeline for."""

    def test_the_tracked_feed_is_recommendation_and_deals_is_opt_in(
        self,
    ) -> None:
        # A tracked (``track_shown``) session is the impulse-buy
        # RECOMMENDATION feed by default; a caller that KNOWS the cycle
        # asked the marketplace for its documented discount ordering
        # passes ``deals_cycle`` explicitly (G24 no longer rotates sorts -
        # only categories/photo pass an explicit mode).
        self.assertEqual(mode_for_feed(track_shown=True), MODE_RECOMMENDATION)
        self.assertEqual(
            mode_for_feed(track_shown=True, deals_cycle=True), MODE_DEALS
        )

    def test_a_feed_that_is_not_the_rotation_is_a_search(self) -> None:
        self.assertEqual(mode_for_feed(track_shown=False), MODE_SEARCH)
        self.assertEqual(
            mode_for_feed(track_shown=False, deals_cycle=True), MODE_SEARCH
        )

    def test_a_request_that_asked_for_discounts_is_a_deals_feed(self) -> None:
        discount = SearchRequest(
            original_query="клавиатура", sort_preference=SortPreference.VALUE_DESC
        )

        self.assertEqual(mode_for_request(discount), MODE_DEALS)
        self.assertEqual(mode_for_request(self.request()), MODE_SEARCH)
        # A request that is not a request at all cannot ask for a reorder.
        self.assertEqual(mode_for_request(None), MODE_SEARCH)
        self.assertEqual(mode_for_request(object()), MODE_SEARCH)

    @staticmethod
    def request() -> SearchRequest:
        return SearchRequest(original_query=QUERY)

    def test_view_ids_keep_the_order_and_drop_junk(self) -> None:
        products = catalogue(3) + [None, Product(**catalogue(1)[0].model_dump())]

        self.assertEqual(view_ids(products), ["p0", "p1", "p2"])
        self.assertEqual(view_ids([]), [])
        self.assertEqual(view_ids(None), [])


class CurationGateTests(unittest.IsolatedAsyncioTestCase):
    """The gate reads a history, never invents one and never breaks a feed."""

    async def test_no_database_or_no_user_means_no_history(self) -> None:
        gate = CurationGate()

        self.assertEqual(await gate.seen_ids(7), set())
        # Writing without a database or a user is a silent no-op, not a crash.
        await gate.remember(7, catalogue(2))
        await gate.remember(None, catalogue(2))

    async def test_a_database_without_the_history_api_is_not_a_failure(self) -> None:
        class AncientDatabase:
            """A layer from before the view history existed."""

        gate = CurationGate(AncientDatabase())

        self.assertEqual(await gate.seen_ids(7), set())
        await gate.remember(7, catalogue(2))

    async def test_a_broken_history_degrades_to_showing_products(self) -> None:
        class BrokenDatabase:
            async def get_user_seen_ids(self, user_id):
                raise RuntimeError("db is down")

            async def add_user_views(self, user_id, product_ids):
                raise RuntimeError("db is down")

        gate = CurationGate(BrokenDatabase())

        self.assertEqual(await gate.seen_ids(7), set())
        await gate.remember(7, catalogue(2))

    async def test_the_gate_runs_the_pipeline_over_the_history_it_read(self) -> None:
        db = Database(MEMORY_DB_PATH)
        await db.initialize()
        try:
            user = await db.get_or_create_user(555, "tester", "Tester")
            await db.add_user_views(user, ["p0", "p1"])
            gate = CurationGate(db)

            curated = await gate.curate(
                catalogue(), user_id=user, mode=MODE_SEARCH
            )

            # Six fresh listings can fill a screen, so the seen two are gone...
            self.assertEqual([p.product_id for p in curated], [f"p{i}" for i in range(2, 8)])
            # ...and the pipeline report says so.
            self.assertEqual(gate.pipeline.last_report.dropped("history"), 2)
        finally:
            await db.close()

    async def test_category_curation_ranks_by_the_request_relevance(self) -> None:
        # G24.8 P1.1 wiring: the gate builds the relevance map from the
        # SAME services.relevance functions with the request the handler
        # passed, and Stage A (tier/score) decides the order - commercial
        # metrics never outrank topical fit, while quality orders the ties.
        request = SearchRequest(
            original_query="Компьютеры",
            normalized_query="computer laptop",
            mode=SearchMode.CATEGORY,
            category="computers",
            keywords=["ноутбук", "мышь"],
        )

        def product(product_id: str, title: str, price: float, orders: int | None):
            return Product(
                marketplace=Marketplace.ALIEXPRESS,
                product_id=product_id,
                title=title,
                product_url=f"https://www.aliexpress.com/item/{product_id}.html",
                price=price,
                currency="USD",
                orders_count=orders,
            )

        leader = product("leader", "Ноутбук с мышью в комплекте", 49.0, 0)
        sibling = product("sibling", "Мышь беспроводная", 3.0, 9000)
        outsider = product("outsider", "Абракадабра волшебная", 1.0, 99999)

        gate = CurationGate()
        curated = await gate.curate(
            [outsider, sibling, leader],
            user_id=None,
            mode=MODE_CATEGORY,
            request=request,
        )

        # leader matches 2/3 category words, sibling 1/3, outsider none
        # (tier 0) - and the outsider's 99999 orders change nothing.
        self.assertEqual(
            [p.product_id for p in curated], ["leader", "sibling", "outsider"]
        )

    async def test_search_curation_keeps_the_received_order_despite_a_request(
        self,
    ) -> None:
        # The request travels through the same signature, but MODE_SEARCH
        # ignores it: no map is built and the received order stays.
        gate = CurationGate()
        curated = await gate.curate(
            catalogue(),
            user_id=None,
            mode=MODE_SEARCH,
            request=SearchRequest(original_query=QUERY),
        )

        self.assertEqual(
            [p.product_id for p in curated], [p.product_id for p in catalogue()]
        )


class FeedCurationFlowTests(unittest.IsolatedAsyncioTestCase):
    """Mock AliExpress -> SearchService -> curation -> SQLite -> one card."""

    async def asyncSetUp(self) -> None:
        self.db = Database(MEMORY_DB_PATH)
        await self.db.initialize()
        self.user_id = await self.db.get_or_create_user(555, "tester", "Tester")
        self.provider = StubAliExpress()
        self.service = SearchService(
            [self.provider], max_results=20, max_search_pages=1
        )
        self.context = FakeContext(
            bot_data={"database": self.db, "search_service": self.service}
        )

    async def asyncTearDown(self) -> None:
        await self.service.aclose()
        await self.provider.aclose()
        await self.db.close()

    @staticmethod
    def request() -> SearchRequest:
        return SearchRequest(original_query=QUERY, page_no=1)

    @staticmethod
    def title_of(product_id: str) -> str:
        """The title the catalogue gave this id."""

        return TITLES[int(product_id.lstrip("p"))]

    async def open_feed(self, **kwargs) -> tuple[str | None, FakeBot, list[str]]:
        """Answer one search and open the feed, exactly as a handler would.

        Returns the session, the bot and the ids in the order the SERVICE
        produced them - that order is the API's answer, and curation is expected
        to keep it (mode ``search``).
        """

        outcome = await self.service.find_products(self.request())
        served = [product.product_id for product in outcome.products]
        self.context.bot.messages.clear()
        session_id = await search_handlers.send_search_results(
            self.context, 47, outcome, user_id=self.user_id, **kwargs
        )
        return session_id, self.context.bot, served

    def curated_ids(self, session_id: str) -> list[str]:
        """The ids of the list the one-card browser of that session walks."""

        cached = handler_common.get_cached_outcome(self.context, session_id)
        return [product.product_id for product in cached.products]

    async def test_the_offered_card_is_recorded_in_the_view_history(self) -> None:
        session_id, bot, served = await self.open_feed()

        self.assertIsNotNone(session_id)
        self.assertIn(self.title_of(served[0]), bot.texts[0])
        self.assertEqual(await self.db.get_user_seen_ids(self.user_id), {served[0]})
        # The whole honest pool survived curation, in the order the service
        # produced: on a first open nothing is dropped and nothing is reordered.
        self.assertEqual(self.curated_ids(session_id), served)
        self.assertEqual(len(served), len(TITLES))

    async def test_the_next_open_skips_what_was_already_shown(self) -> None:
        _, _, served = await self.open_feed()

        session_id, bot, _ = await self.open_feed()

        # The card the user has already met is not offered again, and the feed
        # opens on the next real product of the marketplace pool.
        self.assertEqual(self.curated_ids(session_id), served[1:])
        self.assertIn(self.title_of(served[1]), bot.texts[0])
        self.assertEqual(
            await self.db.get_user_seen_ids(self.user_id), set(served[:2])
        )

    async def test_the_feed_never_opens_on_a_card_it_already_offered(self) -> None:
        offered: list[str] = []

        for _ in range(3):
            session_id, bot, _ = await self.open_feed()
            opened = self.curated_ids(session_id)[0]

            self.assertNotIn(opened, offered)
            self.assertIn(self.title_of(opened), bot.texts[0])
            offered.append(opened)

        self.assertEqual(len(set(offered)), 3)
        # Every card the user was opened on is in the history the next open reads.
        self.assertEqual(
            await self.db.get_user_seen_ids(self.user_id), set(offered)
        )

    async def test_a_short_fresh_pool_keeps_the_seen_cards_at_the_tail(self) -> None:
        # Five of the eight listings are already known: too few are new to fill
        # a screen, so the feed stays FULL and the known ones come back last.
        known = {f"p{index}" for index in range(len(TITLES) - 3)}
        await self.db.add_user_views(self.user_id, sorted(known))

        session_id, bot, served = await self.open_feed()
        curated = self.curated_ids(session_id)

        self.assertEqual(len(curated), len(TITLES))
        self.assertIn(curated[0], served)
        self.assertNotIn(curated[0], known)
        self.assertIn(self.title_of(curated[0]), bot.texts[0])

    async def test_browsing_records_every_card_it_shows(self) -> None:
        session_id, _, served = await self.open_feed()
        query = FakeQuery(f"pnav:{session_id}:1")

        await search_handlers.browse_callback(
            FakeUpdate(callback_query=query), self.context
        )

        # The ➡️ card is a card the user has seen: same history, same truth.
        self.assertEqual(
            await self.db.get_user_seen_ids(self.user_id), set(served[:2])
        )
        self.assertIn(self.title_of(served[1]), query.edits[-1][0])
        # It is the SAME message edited, not a new one: the browsing flow - and
        # therefore the card format - is untouched by curation. The ONE extra
        # message is the count hint sent right after the opening card.
        self.assertEqual(len(self.context.bot.messages), 2)

    async def test_the_popular_history_does_not_empty_a_search(self) -> None:
        popular_session, _, _ = await self.open_feed(track_shown=True)
        popular_card = self.curated_ids(popular_session)[0]

        session_id, bot, served = await self.open_feed()
        curated = self.curated_ids(session_id)

        # A search still opens on a real card, and the one card the rotation
        # already showed is not handed to it as if it were new.
        self.assertIsNotNone(session_id)
        self.assertNotIn(popular_card, curated)
        self.assertEqual(curated, [p for p in served if p != popular_card])
        self.assertIn(self.title_of(curated[0]), bot.texts[0])
        # Each flow keeps its OWN scope in the shown history, exactly as before
        # step 3: writing the rotation's card does not consume the search feed.
        self.assertEqual(
            await self.db.get_shown_products(self.user_id, flow=FLOW_FEED),
            [("aliexpress", popular_card)],
        )
        self.assertEqual(
            await self.db.get_shown_products(self.user_id, flow=FLOW_SEARCH),
            [("aliexpress", curated[0])],
        )

    async def test_the_rotation_ranks_by_popularity_a_search_keeps_the_api_order(
        self,
    ) -> None:
        popular_session, _, served = await self.open_feed(track_shown=True)

        # The rotation asked the pipeline for its own mode: the listing with the
        # most orders leads the screen.
        self.assertEqual(self.curated_ids(popular_session)[0], "p0")

        search_session, _, _ = await self.open_feed()

        # The search feed did NOT inherit that ranking: the order the service
        # returned stands, minus the single card the rotation already showed.
        self.assertEqual(
            self.curated_ids(search_session),
            [p for p in served if p != "p0"],
        )

    async def test_the_card_format_is_untouched_by_curation(self) -> None:
        _, bot, served = await self.open_feed()

        card = bot.texts[0]
        # The very fields AliExpress returned, rendered by the same renderer as
        # before step 3: curation drops and reorders listings, it never rewrites
        # a title, a price or a button.
        self.assertIn(self.title_of(served[0]), card)
        self.assertIn("17.00", card)
        self.assertIn("USD", card)
        self.assertIn("4.7", card)
        labels = bot.labels
        self.assertTrue(any("Следующий" in label for label in labels))
        self.assertTrue(any("Назад" in label for label in labels))
        self.assertTrue(any("В избранное" in label for label in labels))



class FakeBot:
    """Records the messages the handlers send."""

    def __init__(self) -> None:
        self.messages: list[tuple[int, str, dict]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
        self.messages.append((chat_id, text, kwargs))

    async def send_photo(self, chat_id: int, photo: str, **kwargs) -> None:
        self.messages.append((chat_id, kwargs.get("caption", ""), kwargs))

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        return None

    @property
    def texts(self) -> list[str]:
        return [text for _, text, _ in self.messages]

    def _buttons(self) -> list:
        rows: list = []
        for _, _, kwargs in self.messages:
            markup = kwargs.get("reply_markup")
            if markup is not None:
                rows.extend(markup.inline_keyboard)
        return [button for row in rows for button in row]

    @property
    def callbacks(self) -> list[str]:
        return [b.callback_data for b in self._buttons() if b.callback_data]

    @property
    def labels(self) -> list[str]:
        return [button.text for button in self._buttons()]


class FakeMessage:
    """The message a real callback query carries."""

    chat_id = 47
    message_id = 99
    photo: list = []


class FakeQuery:
    """Records callback answers and in-place card edits."""

    def __init__(self, data: str) -> None:
        self.data = data
        self.message = FakeMessage()
        self.answers: list[str] = []
        self.edits: list[tuple[str, dict]] = []

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        if text:
            self.answers.append(text)

    async def edit_message_text(self, text: str, **kwargs) -> None:
        self.edits.append((text, kwargs))

    async def edit_message_caption(self, caption: str, **kwargs) -> None:
        self.edits.append((caption, kwargs))

    async def edit_message_media(self, media, **kwargs) -> None:
        self.edits.append((getattr(media, "caption", ""), kwargs))


class FakeUpdate:
    """The slice of a Telegram update the browsing handlers touch."""

    class _User:
        id = 555
        username = "tester"
        first_name = "Tester"

    class _Chat:
        id = 47

    def __init__(self, *, callback_query=None) -> None:
        self.callback_query = callback_query
        self.effective_message = None
        self.effective_chat = self._Chat() if callback_query else None
        self.effective_user = self._User()


class _FakeApplication:
    def __init__(self, bot_data: dict) -> None:
        self.bot_data = bot_data


class FakeContext:
    """Minimal stand-in for ``telegram.ext.CallbackContext``."""

    def __init__(
        self, bot_data: dict | None = None, bot: FakeBot | None = None
    ) -> None:
        self.bot = bot or FakeBot()
        self.user_data: dict = {}
        self.application = _FakeApplication(bot_data or {})
