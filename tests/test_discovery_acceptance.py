"""Acceptance tests for the discovery-pipeline revision.

Pinned here:

1. the «⭐ Избранное» screen never breaks - not on a legacy/malformed row,
   not on a product whose stored link is unusable, not on a long list, and
   not when Telegram refuses the edit (verified live: Telegram rejects a
   whole message when one button URL is empty or relative, which used to
   surface as «⚠️ Произошла внутренняя ошибка»);
2. the shortened screen texts of Горячие скидки / Промокоды / Поиск по фото,
   with no technical wording (API, Gemini, «партнёрские ссылки»);
3. the per-user shown-product history shared by every flow: it is written on
   every card, read by search/category/photo/popular, keyed by the canonical
   identity, isolated per user, persistent, and it never replaces a relevant
   product with an irrelevant one;
4. the reported relevance case (a pliers set must not answer a query about a
   furniture-cleaning set);
5. the menu wiring: every discovery screen opened from the menu hands the
   SAME per-user history into the search service.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import httpx
from telegram.error import BadRequest

from config.catalog import CATEGORIES
from config.settings import MarketplaceCredentials
from database.database import Database
from handlers import common, discovery, favorites
from handlers.hot_discounts import HOT_DISCOUNTS_TEXT
from handlers.promo_codes import PROMO_TEXT
from handlers.search import (
    EMPTY_RESULT_TEXT,
    send_search_results,
)
from handlers.start import PHOTO_PROMPT, dispatch_menu
from keyboards.main import (
    CB_CATEGORIES,
    CB_CATEGORY_PREFIX,
    CB_PHOTO,
    CB_POPULAR,
)
from keyboards.products import browse_card_keyboard, favorites_list_keyboard
from models.product import Marketplace, Product
from models.search_request import SearchMode, SearchRequest
from services import relevance
from services.marketplaces.base import MarketplaceProvider
from services.product_identity import identity_pair
from services.search_service import SearchOutcome, SearchService
from utils.formatting import is_openable_url, share_button_url

CONFIGURED = MarketplaceCredentials("key", "secret")

TITLE = "Набор для чистки мебели, щетка и спрей"
LINK = "https://www.aliexpress.com/item/100.html"
AFFILIATE = "https://s.click.aliexpress.com/e/_abc"


def run(coro):
    return asyncio.run(coro)


def make_product(
    product_id: str = "100",
    title: str = TITLE,
    *,
    product_url: str = LINK,
    affiliate_url: str | None = AFFILIATE,
) -> Product:
    return Product(
        marketplace=Marketplace.ALIEXPRESS,
        product_id=product_id,
        title=title,
        product_url=product_url,
        affiliate_url=affiliate_url,
        price=10.0,
        currency="USD",
    )


class StubProvider(MarketplaceProvider):
    """Provider returning fixed real-shaped products (no network)."""

    marketplace = Marketplace.ALIEXPRESS
    display_name = "AliExpress"
    supports_feed = True

    def __init__(self, products: list[Product]) -> None:
        super().__init__(CONFIGURED, httpx.AsyncClient())
        self._products = products
        self.requests: list[SearchRequest] = []

    async def search_products(self, request: SearchRequest) -> list[Product]:
        self.requests.append(request)
        return list(self._products)

    async def get_product(self, product_id: str) -> Product | None:
        return next((p for p in self._products if p.product_id == product_id), None)

    def generate_affiliate_link(self, product: Product) -> str | None:
        return None

    async def fetch_feed_page(self, limit=None, currency=None, page=1):
        return list(self._products)


# -- minimal Telegram doubles (same shape as tests/test_handlers_ui.py) ------


class FakeMessage:
    def __init__(self, chat_id: int = 47) -> None:
        self.chat_id = chat_id
        self.texts: list[str] = []
        self.photo: list = []
        self.deleted = False

    async def reply_text(self, text: str, **kwargs) -> None:
        self.texts.append(text)

    async def delete(self) -> None:
        self.deleted = True


class FakeQuery:
    def __init__(self, data: str, *, fail_edit: bool = False) -> None:
        self.data = data
        self.answers: list[str] = []
        self.edits: list[tuple[str, dict]] = []
        self.fail_edit = fail_edit
        self.message = FakeMessage()

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        self.answers.append(text or "")

    async def edit_message_text(self, text: str, **kwargs) -> None:
        if self.fail_edit:
            raise BadRequest("There is no text in the message to edit")
        self.edits.append((text, kwargs))


class FakeUser:
    id = 555
    username = "tester"
    first_name = "Tester"


class FakeUpdate:
    def __init__(self, *, callback_query=None, message=None, user=None) -> None:
        self.callback_query = callback_query
        self.effective_message = message
        self.effective_chat = FakeChat()
        self.effective_user = user or FakeUser()


class FakeChat:
    id = 47


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str, dict]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
        self.messages.append((chat_id, text, kwargs))

    async def send_photo(self, chat_id: int, **kwargs) -> None:  # pragma: no cover
        self.messages.append((chat_id, kwargs.get("caption", ""), kwargs))


class FakeContext:
    """Bare context: only the database is needed by the code under test."""

    class _Application:
        def __init__(self, bot_data: dict) -> None:
            self.bot_data = bot_data

    def __init__(self, database, **bot_data) -> None:
        self.bot = FakeBot()
        self.user_data: dict = {}
        data: dict = {"database": database}
        data.update(bot_data)
        self.application = FakeContext._Application(data)

        return None


class FavoritesScreenTests(unittest.TestCase):
    """The favorites screen must render for ANY stored data."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "favorites.db")
        self.db = Database(self.db_path)
        run(self.db.initialize())
        # The history/favorites tables are foreign-keyed on ``users``: a row
        # always belongs to a real user, exactly as in production. The id must
        # be the one ``register_user`` derives from ``FakeUser.id`` (555).
        self.user_id = run(self.db.get_or_create_user(555, "tester", "Tester"))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _render(self, *, fail_edit: bool = False):
        context = FakeContext(self.db)
        query = FakeQuery("menu:favorites", fail_edit=fail_edit)
        run(favorites.show_favorites(FakeUpdate(callback_query=query), context))
        return context, query

    def _insert_raw(self, marketplace: str, product_id: str, payload) -> None:
        connection = sqlite3.connect(self.db_path)
        connection.execute(
            "INSERT INTO favorites (user_id, marketplace, product_id, product_json) "
            "VALUES (?, ?, ?, ?)",
            (self.user_id, marketplace, product_id, payload),
        )
        connection.commit()
        connection.close()

    def _store_valid_favorite(self, product: Product | None = None) -> None:
        product = product or make_product()
        run(
            self.db.add_favorite(
                user_id=self.user_id,
                marketplace="aliexpress",
                product_id=product.product_id,
                product_json=product.model_dump(mode="json"),
            )
        )

    def test_empty_state_uses_the_documented_text(self) -> None:
        _, query = self._render()

        self.assertEqual(query.edits[0][0], favorites.EMPTY_FAVORITES_TEXT)

    def test_valid_favorite_is_listed_with_real_actions(self) -> None:
        self._store_valid_favorite()

        _, query = self._render()

        text, kwargs = query.edits[0]
        self.assertIn("Набор для чистки мебели", text)
        buttons = [b for row in kwargs["reply_markup"].inline_keyboard for b in row]
        self.assertIn(AFFILIATE, [b.url for b in buttons if b.url])
        self.assertIn("fdel:aliexpress:100", [b.callback_data for b in buttons])

    def test_a_product_without_a_usable_link_gets_no_url_button(self) -> None:
        """Telegram rejects the WHOLE message for an empty/relative URL."""

        keyboard = favorites_list_keyboard(
            [
                {
                    "index": 0,
                    "product": make_product(product_url="", affiliate_url=None),
                }
            ]
        )

        buttons = [b for row in keyboard.inline_keyboard for b in row]
        self.assertFalse([b for b in buttons if b.url])
        # The item is still deletable - the screen did not fail.
        self.assertIn("fdel:aliexpress:100", [b.callback_data for b in buttons])

    def test_the_card_keyboard_never_emits_an_unusable_url(self) -> None:
        broken = make_product(product_url="", affiliate_url="/relative/path")

        keyboard = browse_card_keyboard("s1", 0, 1, broken)
        buttons = [b for row in keyboard.inline_keyboard for b in row]

        self.assertFalse([b for b in buttons if b.url])
        self.assertIn("pfav:s1:0", [b.callback_data for b in buttons])
        self.assertIn("menu:back", [b.callback_data for b in buttons])

    def test_url_helper_rejects_what_telegram_rejects(self) -> None:
        self.assertFalse(is_openable_url(None))
        self.assertFalse(is_openable_url(""))
        self.assertFalse(is_openable_url("   "))
        self.assertFalse(is_openable_url("/item/1.html"))
        self.assertFalse(is_openable_url("mailto:a@b.c"))
        self.assertTrue(is_openable_url(LINK))
        self.assertTrue(is_openable_url("tg://user?id=1"))
        self.assertTrue(
            is_openable_url("https://s.click.aliexpress.com/s/fwx" + "a" * 1000)
        )
        self.assertIsNone(
            share_button_url(make_product(product_url="", affiliate_url=None))
        )
        self.assertIn("t.me/share/url", share_button_url(make_product()) or "")


    def test_malformed_and_legacy_rows_never_break_the_list(self) -> None:
        self._insert_raw("aliexpress", "broken", "{not json at all")
        self._insert_raw(
            "temu",
            "removed",
            json.dumps(
                {
                    "marketplace": "temu",
                    "product_id": "removed",
                    "title": "Товар удалённого маркетплейса",
                    "product_url": "https://www.temu.com/item/1.html",
                }
            ),
        )
        self._insert_raw(
            "aliexpress", "100", json.dumps(make_product().model_dump(mode="json"))
        )

        _, query = self._render()

        text, kwargs = query.edits[0]
        self.assertIn("Набор для чистки мебели", text)
        self.assertNotIn("удалённого маркетплейса", text)
        buttons = [b for row in kwargs["reply_markup"].inline_keyboard for b in row]
        deletes = [b.callback_data for b in buttons if (b.callback_data or "").startswith("fdel:")]
        self.assertEqual(deletes, ["fdel:aliexpress:100"])

    def test_only_unreadable_rows_show_the_stale_screen(self) -> None:
        self._insert_raw("aliexpress", "broken", "{not json at all")

        _, query = self._render()

        self.assertEqual(query.edits[0][0], favorites.STALE_FAVORITES_TEXT)

    def test_a_long_list_stays_inside_telegram_limits(self) -> None:
        for index in range(30):
            self._store_valid_favorite(
                make_product(
                    product_id=str(1000 + index),
                    title=TITLE + " " + "очень длинное название " * 4,
                )
            )

        _, query = self._render()

        text, kwargs = query.edits[0]
        self.assertLessEqual(len(text), 4096)
        self.assertIn("…и ещё", text)
        buttons = [b for row in kwargs["reply_markup"].inline_keyboard for b in row]
        deletes = [b for b in buttons if (b.callback_data or "").startswith("fdel:")]
        self.assertEqual(len(deletes), favorites.MAX_LISTED_FAVORITES)

    def test_a_refused_edit_is_recovered_by_a_fresh_message(self) -> None:
        self._store_valid_favorite()

        context, query = self._render(fail_edit=True)

        self.assertEqual(query.edits, [])
        self.assertEqual(len(context.bot.messages), 1)
        self.assertIn("Набор для чистки мебели", context.bot.messages[0][1])



class ScreenTextTests(unittest.TestCase):
    """The shortened texts, verbatim, with no technical wording."""

    def test_hot_discounts_text(self) -> None:
        self.assertEqual(
            HOT_DISCOUNTS_TEXT,
            "🔥 <b>Горячие скидки</b>\n\n"
            "Кнопки открывают готовые подборки товаров со скидками AliExpress.\n"
            "Цены и состав подборок задаёт маркетплейс.",
        )

    def test_promo_text(self) -> None:
        self.assertEqual(
            PROMO_TEXT,
            "🎟 <b>Промокоды AliExpress</b>\n\n"
            "Здесь можно найти актуальные промокоды и скидки AliExpress.",
        )

    def test_photo_prompt_text(self) -> None:
        # §11: one short instruction, no AliExpress/technical wording.
        self.assertEqual(
            PHOTO_PROMPT,
            "📷 <b>Поиск по фото</b>\n\n"
            "Пришлите фотографию товара.",
        )

    def test_photo_fallback_text(self) -> None:
        # §12: the exact short human sentence, no API/model explanation.
        self.assertEqual(
            discovery.IMAGE_EMPTY_TEXT,
            "📷 <b>Поиск по фото</b>\n\n"
            "Не удалось определить товар на фотографии или найти похожие "
            "товары. Попробуйте другое фото.",
        )

    def test_no_screen_mentions_internal_plumbing(self) -> None:
        for text in (
            HOT_DISCOUNTS_TEXT,
            PROMO_TEXT,
            PHOTO_PROMPT,
            discovery.IMAGE_EMPTY_TEXT,
        ):
            for forbidden in ("Gemini", "API", "партнёрск", "Traceback", "HTTP"):
                self.assertNotIn(forbidden, text)


class ShownHistoryTests(unittest.IsolatedAsyncioTestCase):
    """One persistent, per-user history shared by every discovery flow."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "history.db")
        self.db = Database(self.db_path)
        await self.db.initialize()

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_history_is_persistent_and_per_user(self) -> None:
        first = await self.db.get_or_create_user(1, "a", "A")
        second = await self.db.get_or_create_user(2, "b", "B")
        await self.db.add_shown_product(first, "aliexpress", "100")
        await self.db.add_shown_product(first, "aliexpress", "100")  # idempotent
        await self.db.add_shown_product(second, "aliexpress", "200")

        reopened = Database(self.db_path)
        await reopened.initialize()

        self.assertEqual(await reopened.get_shown_products(first), [("aliexpress", "100")])
        self.assertEqual(await reopened.get_shown_products(second), [("aliexpress", "200")])

        await reopened.clear_shown_products(first)
        self.assertEqual(await reopened.get_shown_products(first), [])
        self.assertEqual(await reopened.get_shown_products(second), [("aliexpress", "200")])

    async def test_legacy_aliases_share_the_same_history(self) -> None:
        user_id = await self.db.get_or_create_user(3, "c", "C")
        await self.db.add_feed_shown(user_id, "aliexpress", "300")

        self.assertEqual(
            await self.db.get_shown_products(user_id), [("aliexpress", "300")]
        )

    async def test_service_hides_seen_products_and_reports_the_count(self) -> None:
        seen_product = make_product("100")
        other = make_product("200", TITLE + " премиум")
        provider = StubProvider([seen_product, other])
        service = SearchService([provider], ai_service=None)

        request = SearchRequest(
            original_query="набор для чистки мебели", mode=SearchMode.SEARCH
        )
        outcome = await service.find_products(
            request, exclude={identity_pair(seen_product)}
        )
        await provider.aclose()

        self.assertEqual([p.product_id for p in outcome.products], ["200"])
        self.assertEqual(outcome.products_hidden_as_seen, 1)

    async def test_history_never_empties_a_valid_result_set(self) -> None:
        product = make_product("100")
        provider = StubProvider([product])
        service = SearchService([provider], ai_service=None)

        request = SearchRequest(
            original_query="набор для чистки мебели", mode=SearchMode.SEARCH
        )
        outcome = await service.find_products(request, exclude={identity_pair(product)})
        await provider.aclose()

        self.assertEqual([p.product_id for p in outcome.products], ["100"])
        self.assertEqual(outcome.products_hidden_as_seen, 0)

    async def test_relevance_is_applied_before_the_history(self) -> None:
        """A seen relevant product is repeated; junk never fills its place."""

        relevant = make_product("100")
        junk = make_product("200", "Набор регулируемых плоскогубцев для труб")
        provider = StubProvider([relevant, junk])
        service = SearchService([provider], ai_service=None)

        request = SearchRequest(
            original_query="набор для чистки мебели", mode=SearchMode.SEARCH
        )
        outcome = await service.find_products(request, exclude={identity_pair(relevant)})
        await provider.aclose()

        self.assertEqual([p.product_id for p in outcome.products], ["100"])
        self.assertEqual(outcome.products_hidden_as_seen, 0)

    async def test_category_search_takes_the_history(self) -> None:
        category = CATEGORIES[0]
        seen_product = make_product("100", "Набор для чистки мебели щетка")
        unseen = make_product("200", "Набор для чистки мебели спрей")
        provider = StubProvider([seen_product, unseen])
        service = SearchService([provider], ai_service=None)

        outcome = await service.find_by_category(
            category, exclude={identity_pair(seen_product)}
        )
        await provider.aclose()

        self.assertNotIn(
            seen_product.product_id, [p.product_id for p in outcome.products]
        )

    async def test_common_helpers_record_and_read(self) -> None:
        user_id = await self.db.get_or_create_user(555, "tester", "Tester")
        other_id = await self.db.get_or_create_user(777, "other", "Other")
        context = FakeContext(self.db)
        product = make_product()

        self.assertEqual(await common.seen_products(context, user_id), set())
        await common.remember_shown(context, user_id, product)

        marketplace, product_id = identity_pair(product)
        self.assertIn(
            (marketplace, product_id), await common.seen_products(context, user_id)
        )
        # A different user's feed is untouched, and a missing id is no error.
        self.assertEqual(await common.seen_products(context, other_id), set())
        self.assertEqual(await common.seen_products(context, None), set())

    async def test_search_wiring_passes_the_history_into_the_pipeline(self) -> None:
        class RecordingService:
            def __init__(self) -> None:
                self.exclude = "unset"

            async def find_products(self, request, *, exclude=None, broaden=True,
                                    feed_selection=False):
                self.exclude = exclude
                return SearchOutcome(request=request)

            async def find_products_from_text(self, text, *, currency=None, exclude=None):
                self.exclude = exclude
                return SearchOutcome(
                    request=SearchRequest(original_query=text, mode=SearchMode.SEARCH)
                )

        service = RecordingService()
        seen = {identity_pair(make_product())}

        from services.convsearch.retrieval import (
            RetrievalBackend,
            RetrievalStrategy,
        )

        backend = RetrievalBackend(service)
        await backend.retrieve(
            RetrievalStrategy(
                request=SearchRequest(original_query="щетка", mode=SearchMode.SEARCH),
                product_type=None,
                result_limit=None,
            ),
            exclude=seen,
        )

        self.assertEqual(service.exclude, seen)



class EngineInstrumentationTests(unittest.TestCase):
    """§7: the engine turn is measurable from the log alone."""

    def test_engine_turn_logs_intent_and_reason(self) -> None:
        import logging

        from services.convsearch.engine import ConversationSearchEngine
        from services.convsearch.state import ConversationEngineState

        class RecordingService:
            async def find_products(self, request, *, exclude=None, feed_selection=False):
                return SearchOutcome(request=request)

        engine = ConversationSearchEngine()
        engine.bind_search_service(RecordingService())
        state = ConversationEngineState(user_key="logtest")
        with self.assertLogs("services.convsearch.engine", level="INFO") as logs:
            run(
                engine.handle_message(
                    "logtest", "наушники беспроводные", state=state
                )
            )
        line = next(entry for entry in logs.output if "convsearch" in entry)
        self.assertIn("action=", line)
        self.assertIn("reason=", line)



class RelevanceRegressionTests(unittest.TestCase):
    """The reported case: a pliers set must not answer this query."""

    QUERY = "Набор для чистки мебели недорогой"

    def _request(self, query: str = QUERY) -> SearchRequest:
        return SearchRequest(original_query=query, mode=SearchMode.SEARCH)

    def test_a_pliers_set_is_rejected(self) -> None:
        pliers = make_product(
            "9",
            "Набор регулируемых плоскогубцев с двойной головкой 57QF для "
            "крепежных деталей различного размера при занятии трубами",
        )

        self.assertEqual(
            relevance.rejection_reason(pliers, self._request()),
            "no_query_term_in_title",
        )
        self.assertEqual(relevance.relevance_score(pliers, self._request()), 0.0)

    def test_a_furniture_cleaning_set_is_kept(self) -> None:
        cleaner = make_product(
            "1",
            "Набор для чистки мебели: щетка и спрей для дивана и ковров, недорогой",
        )

        self.assertIsNone(relevance.rejection_reason(cleaner, self._request()))
        self.assertEqual(relevance.relevance_score(cleaner, self._request()), 1.0)

    def test_the_filter_keeps_only_the_relevant_product(self) -> None:
        pliers = make_product("9", "Набор регулируемых плоскогубцев для труб")
        cleaner = make_product("1", "Набор для чистки мебели щетка и спрей")

        kept, dropped = relevance.filter_relevant([pliers, cleaner], self._request())

        self.assertEqual([p.product_id for p in kept], ["1"])
        self.assertEqual(dropped, {"no_query_term_in_title": 1})

    def test_a_product_without_a_real_url_is_never_shown(self) -> None:
        broken = make_product("1", TITLE, product_url="", affiliate_url=None)

        self.assertEqual(
            relevance.rejection_reason(broken, self._request()), "invalid_product_url"
        )

    def test_an_unrelated_script_cannot_be_compared_and_hides_nothing(self) -> None:
        """An English-only request cannot judge Russian titles - no fake filter."""

        russian_title = make_product("1", "Набор для чистки мебели щетка")
        request = self._request("furniture cleaning set")

        self.assertEqual(relevance.comparable_terms(russian_title.title, request), [])
        self.assertIsNone(relevance.rejection_reason(russian_title, request))


class _RecordingDiscoveryService:
    """Search-service double recording the ``exclude`` every flow hands in."""

    def __init__(self, product: Product | None = None) -> None:
        self.calls: list[tuple[str, set | None]] = []
        self.broadened: list[bool] = []
        self.category_kwargs: list[dict] = []
        self._product = product

    def _result(self, query: str) -> SearchOutcome:
        products = [] if self._product is None else [self._product]
        return SearchOutcome(
            request=SearchRequest(original_query=query, mode=SearchMode.SEARCH),
            products=products,
        )

    async def find_by_category(
        self, category, *, country=None, currency=None, exclude=None
    ):
        self.calls.append(("category", exclude))
        # G24.8 P1.4: the handler's country/currency arguments are the
        # evidence under test - recorded, not acted on.
        self.category_kwargs.append({"country": country, "currency": currency})
        return self._result(category.query)

    async def find_feed_page(
        self, *, currency=None, country=None, page=1, exclude=None, limit=None
    ):
        self.calls.append(("feed", exclude))
        return self._result("находки")

    async def find_products_from_text(self, text, *, currency=None, exclude=None):
        self.calls.append(("photo", exclude))
        return self._result(text)

    async def find_products(self, request, *, exclude=None, broaden=True):
        # The photo flow builds its own concept request and searches with
        # it directly (handlers.discovery.photo_message).
        self.calls.append(("photo", exclude))
        self.broadened.append(broaden)
        return self._result(request.original_query)


class _FakeVision:
    is_available = True

    async def describe_image_query(self, image_bytes: bytes) -> str:
        return "щетка для дивана"


class _FakePhotoFile:
    async def download_as_bytearray(self) -> bytearray:
        return bytearray(b"\xff\xd8fake-jpeg")


class _FakePhoto:
    file_size = 1024

    async def get_file(self) -> _FakePhotoFile:
        return _FakePhotoFile()


class MenuWiringTests(unittest.TestCase):
    """Every discovery screen opened from the menu shares ONE history."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "wiring.db")
        self.db = Database(self.db_path)
        run(self.db.initialize())
        self.user_id = run(self.db.get_or_create_user(555, "tester", "Tester"))
        # The user has already been shown product 100 in some earlier feed.
        run(self.db.add_shown_product(self.user_id, "aliexpress", "100"))
        self.seen = {("aliexpress", "100")}

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _context(self, service: _RecordingDiscoveryService) -> FakeContext:
        return FakeContext(
            self.db,
            search_service=service,
            ai_service=_FakeVision(),
        )

    def test_category_screen_passes_the_history_into_the_search(self) -> None:
        new_product = make_product("200", TITLE + " премиум")
        service = _RecordingDiscoveryService(new_product)
        context = self._context(service)
        query = FakeQuery(f"{CB_CATEGORY_PREFIX}:{CATEGORIES[0].code}")

        run(discovery.category_callback(FakeUpdate(callback_query=query), context))

        self.assertEqual(service.calls, [("category", self.seen)])
        # The card just shown joins the SAME history of the same user.
        rows = run(self.db.get_shown_products(self.user_id))
        self.assertIn(("aliexpress", "100"), rows)
        self.assertIn(("aliexpress", "200"), rows)
        self.assertTrue(context.bot.messages)

    def test_category_results_are_gated_by_the_category_profile(self) -> None:
        """The category screen hands its CATEGORY mode to the gate (G24.8 P0.2).

        A valid $60 listing used to be dropped by the impulse-buy profile
        ($20 ceiling, $50 empty-state fallback) and the user saw an empty
        screen. The category policy judges only the structural contract
        after relevance has accepted the card, so the very same pool now
        opens a card - while a plain search keeps its own profile (no
        ceiling) exactly as before.
        """
        pricey = Product(
            marketplace=Marketplace.ALIEXPRESS,
            product_id="300",
            title=TITLE,
            product_url="https://www.aliexpress.com/item/300.html",
            affiliate_url=AFFILIATE,
            price=60.0,
            currency="USD",
            orders_count=500,
        )

        context = self._context(_RecordingDiscoveryService(pricey))
        query = FakeQuery(f"{CB_CATEGORY_PREFIX}:{CATEGORIES[0].code}")

        run(discovery.category_callback(FakeUpdate(callback_query=query), context))

        # The card opened: category eligibility is no longer the $1..$20
        # recommendation window.
        texts = [text for _, text, _ in context.bot.messages]
        self.assertTrue(texts)
        self.assertNotIn(EMPTY_RESULT_TEXT, texts)

        # The same product, the same database, the same card - only the
        # flow differs: no curation_mode override means the search profile
        # (no ceiling), and the card opens.
        outcome = SearchOutcome(
            request=SearchRequest(
                original_query="набор для чистки мебели", mode=SearchMode.SEARCH
            ),
            products=[pricey],
        )
        session_id = run(
            send_search_results(context, 47, outcome, user_id=self.user_id)
        )

        self.assertIsNotNone(session_id)
        self.assertTrue(context.bot.messages)

    def test_popular_screen_passes_its_own_history_into_the_feed(self) -> None:
        """The feed skips what the ROTATION showed - nothing else.

        One shared history let an ordinary search empty the popular feed and
        make it claim «нет новых товаров» (the reported production bug), so the
        two scopes are asserted to be separate here: the search row of the
        setUp is NOT part of what the feed excludes.
        """

        new_product = make_product("300", TITLE + " новинка")
        service = _RecordingDiscoveryService(new_product)
        context = self._context(service)
        query = FakeQuery(CB_POPULAR)
        run(self.db.add_feed_shown(self.user_id, "aliexpress", "rotated"))

        run(discovery.show_feed(FakeUpdate(callback_query=query), context))

        self.assertEqual(service.calls, [("feed", {("aliexpress", "rotated")})])
        # The card just shown joins the same per-user history (both scopes).
        rows = run(self.db.get_shown_products(self.user_id))
        self.assertIn(("aliexpress", "100"), rows)
        self.assertIn(("aliexpress", "300"), rows)
        self.assertTrue(context.bot.messages)

    def test_photo_search_passes_the_history_into_the_search(self) -> None:
        new_product = make_product("400", TITLE + " фото")
        service = _RecordingDiscoveryService(new_product)
        context = self._context(service)
        message = FakeMessage()
        message.photo = [_FakePhoto()]

        run(discovery.photo_message(FakeUpdate(message=message), context))

        self.assertEqual(
            [flow for flow, _ in service.calls],
            ["photo"],
        )
        self.assertEqual(service.calls[0][1], self.seen)
        rows = run(self.db.get_shown_products(self.user_id))
        self.assertIn(("aliexpress", "400"), rows)

    def test_menu_buttons_reach_the_discovery_screens(self) -> None:
        service = _RecordingDiscoveryService(make_product("500"))
        context = self._context(service)

        photo = FakeQuery(CB_PHOTO)
        handled = run(
            dispatch_menu(CB_PHOTO, FakeUpdate(callback_query=photo), context)
        )
        self.assertTrue(handled)
        self.assertEqual(photo.edits[0][0], PHOTO_PROMPT)

        categories = FakeQuery(CB_CATEGORIES)
        handled = run(
            dispatch_menu(CB_CATEGORIES, FakeUpdate(callback_query=categories), context)
        )
        self.assertTrue(handled)
        self.assertEqual(categories.edits[0][0], discovery.CATEGORIES_TEXT)

        popular = FakeQuery(CB_POPULAR)
        handled = run(
            dispatch_menu(CB_POPULAR, FakeUpdate(callback_query=popular), context)
        )
        self.assertTrue(handled)
        # The feed opened through the menu applied its OWN history: this user
        # only searched, and a search is a different scope (the leak that used
        # to make the feed claim there was nothing new).
        self.assertIn(("feed", set()), service.calls)

        self.assertFalse(
            run(dispatch_menu("menu:does-not-exist", FakeUpdate(), context))
        )


class CategoryCountryPropagationTests(unittest.TestCase):
    """G24.8 P1.4: the selected country reaches the CATEGORY search.

    Invariant: stored country -> category handler -> ``find_by_category`` ->
    ``SearchRequest.country`` (the API layer's ``ship_to_country`` is
    asserted in tests/test_marketplace_providers.py). Before this stage the
    handler read the region but never handed the country over, so the value
    was dropped before SearchService while the FEED flow already forwarded
    it.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db_path = str(Path(self._tmp.name) / "country.db")
        self.db = Database(db_path)
        run(self.db.initialize())
        self.user_id = run(self.db.get_or_create_user(555, "tester", "Tester"))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _open_category(
        self, service: "_RecordingDiscoveryService", *, country: str | None = None
    ) -> "_RecordingDiscoveryService":
        if country is not None:
            run(
                self.db.update_user_region(
                    self.user_id, country=country, currency="UAH"
                )
            )
        context = FakeContext(
            self.db, search_service=service, ai_service=_FakeVision()
        )
        query = FakeQuery(f"{CB_CATEGORY_PREFIX}:{CATEGORIES[0].code}")
        run(discovery.category_callback(FakeUpdate(callback_query=query), context))
        return service

    def test_selected_country_travels_into_the_category_search(self) -> None:
        # Two different codes: the value is transported, never hardcoded,
        # and the currency preference stays exactly as it was (G24.8 P0.1).
        for code in ("US", "DE"):
            with self.subTest(country=code):
                service = self._open_category(
                    _RecordingDiscoveryService(), country=code
                )
                received = service.category_kwargs[-1]
                self.assertEqual(received["country"], code)
                self.assertEqual(received["currency"], "UAH")

    def test_stored_country_is_normalized_like_the_feed(self) -> None:
        # The stored value goes through the very same normalization the
        # «Находки» feed has always used: trimmed, upper-cased, ISO-2 only.
        service = self._open_category(_RecordingDiscoveryService(), country="  de ")
        self.assertEqual(service.category_kwargs[-1]["country"], "DE")

        broken = self._open_category(
            _RecordingDiscoveryService(), country="Germany"
        )
        self.assertIsNone(broken.category_kwargs[-1]["country"])

    def test_category_without_country_keeps_the_optional_state(self) -> None:
        service = self._open_category(_RecordingDiscoveryService())
        received = service.category_kwargs[-1]
        self.assertIsNone(received["country"])
        self.assertIsNone(received["currency"])

    def test_find_by_category_carries_the_country_into_the_provider_request(
        self,
    ) -> None:
        # Service hop: the real SearchService builds the request the real
        # provider serializes; country and currency both survive it.
        provider = StubProvider([make_product()])
        service = SearchService([provider], ai_service=None)

        outcome = run(
            service.find_by_category(
                CATEGORIES[0], country="US", currency="UAH"
            )
        )
        run(provider.aclose())

        self.assertEqual(outcome.request.country, "US")
        self.assertTrue(provider.requests)
        self.assertEqual(provider.requests[0].country, "US")
        self.assertEqual(provider.requests[0].currency, "UAH")

