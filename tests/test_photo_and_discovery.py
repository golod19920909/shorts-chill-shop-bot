"""Photo search and discovery routing - the documented fallbacks.

The required user-visible text is fixed by the spec, so it is asserted
verbatim: a failed recognition, an unavailable vision model and an empty
search all show the same short, natural Russian message, and no technical
explanation (Gemini, API, traceback) ever reaches the chat.
"""

from __future__ import annotations

import unittest

from handlers import discovery
from handlers.search import is_discovery_query
from models.product import Availability, Marketplace, Product
from models.search_request import SearchMode, SearchRequest
from services.search_service import SearchOutcome
from tests.test_ai_menu_handler import (
    FakeChat,
    FakeContext,
    FakeDatabase,
    FakeUpdate,
    FakeUser,
    run,
)


class VisionAI:
    """``describe_image_query`` stub: a query, or nothing at all."""

    is_available = True

    def __init__(self, answer: str | None) -> None:
        self.answer = answer
        self.image_requests = 0

    async def describe_image_query(self, image_bytes: bytes):
        self.image_requests += 1
        return self.answer


class UnavailableAI:
    is_available = False

    async def describe_image_query(self, image_bytes: bytes):
        raise AssertionError("vision must not be called when it is unavailable")


class FakeSearchService:
    """Records every search the photo flow runs (query + seen history)."""

    def __init__(self, outcome: SearchOutcome | None = None, error=None) -> None:
        self.outcome = outcome
        self.error = error
        self.queries: list[str] = []
        self.excluded: list = []
        self.broadened: list[bool] = []
        #: The documented page each call asked for (``page_no``).
        self.pages: list[int] = []
        #: Per-call outcomes; when empty, ``self.outcome`` answers instead.
        self.replies: list[SearchOutcome] = []

    async def find_products(self, request, *, exclude=None, broaden=True):
        self.queries.append(request.original_query)
        self.excluded.append(exclude)
        self.pages.append(request.page_no)
        #: What the flow really asked for: photo search must never add
        #: text-level retrieval variants on top of its own hypotheses.
        self.broadened.append(broaden)
        if self.error is not None:
            raise self.error
        if self.replies:
            return self.replies.pop(0)
        if self.outcome is not None:
            return self.outcome
        return SearchOutcome(request=request)

    async def find_products_from_text(
        self, text: str, *, currency=None, exclude=None
    ):
        # Kept for older callers: the photo flow itself now drives
        # ``find_products`` with an explicitly built concept request.
        return await self.find_products(
            SearchRequest(original_query=text, mode=SearchMode.SEARCH),
            exclude=exclude,
        )


class FakePhoto:
    def __init__(self, size: int = 1000) -> None:
        self.file_size = size

    async def get_file(self):
        return self

    async def download_as_bytearray(self) -> bytes:
        return b"\xff\xd8\xff\xe0fake-jpeg"


class PhotoMessage:
    def __init__(self, chat_id: int = 47, size: int = 1000) -> None:
        self.chat_id = chat_id
        self.photo = [FakePhoto(size)]
        self.texts: list[str] = []
        self.kwargs: list[dict] = []

    async def reply_text(self, text: str, **kwargs) -> None:
        self.texts.append(text)
        self.kwargs.append(kwargs)


def photo_update(message: PhotoMessage) -> FakeUpdate:
    update = FakeUpdate(message=message)
    update.effective_chat = FakeChat()
    update.effective_user = FakeUser()
    return update


def make_context(ai_service, search_service=None, settings=None) -> FakeContext:
    """A context double: the vision step, the search service and the settings.

    ``settings`` is optional - a flow that reads a budget must fall back to its
    documented default when the wiring (or a test) does not provide one.
    """

    bot_data = {
        "database": FakeDatabase(),
        "ai_service": ai_service,
        "search_service": search_service,
    }
    if settings is not None:
        bot_data["settings"] = settings
    return FakeContext(bot_data)


def make_listing(product_id: str, title: str, price: float) -> Product:
    """A real-shaped listing for the provider stubs below."""

    return Product(
        marketplace=Marketplace.ALIEXPRESS,
        product_id=product_id,
        title=title,
        product_url=f"https://www.aliexpress.com/item/{product_id}.html",
        price=price,
        currency="USD",
        availability=Availability.IN_STOCK,
    )


class PhotoFallbackTests(unittest.TestCase):
    def test_the_documented_fallback_text_is_used_verbatim(self) -> None:
        # §12: one short human sentence, no AliExpress/technical wording.
        self.assertEqual(
            discovery.IMAGE_EMPTY_TEXT,
            "📷 <b>Поиск по фото</b>\n\n"
            "Не удалось определить товар на фотографии или найти похожие "
            "товары. Попробуйте другое фото.",
        )

    def test_no_technical_details_leak_into_the_fallback(self) -> None:
        text = discovery.IMAGE_EMPTY_TEXT

        for forbidden in ("Gemini", "API", "alias", "Traceback", "HTTP"):
            self.assertNotIn(forbidden, text)

    def test_unavailable_vision_shows_the_documented_fallback(self) -> None:
        context = make_context(UnavailableAI())
        message = PhotoMessage()

        run(discovery.photo_message(photo_update(message), context))

        self.assertEqual(message.texts[0], discovery.PHOTO_RECEIVED_TEXT)
        self.assertEqual(message.texts[1], discovery.IMAGE_EMPTY_TEXT)
        self.assertEqual(context.bot.messages, [])

    def test_an_unrecognizable_photo_shows_the_documented_fallback(self) -> None:
        context = make_context(VisionAI(None))
        message = PhotoMessage()

        run(discovery.photo_message(photo_update(message), context))

        self.assertEqual(message.texts[-1], discovery.IMAGE_EMPTY_TEXT)

    def test_an_oversized_photo_is_refused_before_any_recognition(self) -> None:
        ai = VisionAI("наушники")
        context = make_context(ai)
        message = PhotoMessage(size=discovery.MAX_IMAGE_BYTES + 1)

        run(discovery.photo_message(photo_update(message), context))

        self.assertIn("слишком большая", message.texts[1])
        self.assertEqual(ai.image_requests, 0)

    def test_a_recognized_photo_runs_a_normal_text_search(self) -> None:
        ai = VisionAI("беспроводные наушники")
        search = FakeSearchService()
        context = make_context(ai, search)
        message = PhotoMessage()

        run(discovery.photo_message(photo_update(message), context))

        self.assertEqual(search.queries, ["беспроводные наушники"])
        self.assertEqual(ai.image_requests, 1)

    def test_a_failing_marketplace_search_hides_technical_details(self) -> None:
        ai = VisionAI("наушники")
        search = FakeSearchService(error=RuntimeError("api down"))
        context = make_context(ai, search)
        message = PhotoMessage()

        run(discovery.photo_message(photo_update(message), context))

        # The technical reason stays in the log: the chat gets the same
        # short, natural fallback an unsuccessful recognition gets.
        self.assertEqual(message.texts[-1], discovery.IMAGE_EMPTY_TEXT)
        self.assertNotIn("RuntimeError", message.texts[-1])
        self.assertNotIn("api down", message.texts[-1])

    def test_the_card_never_echoes_the_generated_query(self) -> None:
        """§11/§4: the photo card starts with product data - no «Запрос:»."""

        ai = VisionAI("синий кожаный рюкзак с молнией")
        search = FakeSearchService()
        search.outcome = SearchOutcome(
            request=SearchRequest(original_query="синий кожаный рюкзак с молнией"),
            products=[make_listing("1", "Рюкзак кожаный синий", 10.0)],
        )
        context = make_context(ai, search)
        message = PhotoMessage()

        run(discovery.photo_message(photo_update(message), context))

        card = context.bot.texts[0]
        self.assertIn("Рюкзак кожаный синий", card)
        self.assertNotIn("Запрос:", card)
        self.assertNotIn("синий кожаный рюкзак", card)
        # The one hint text AFTER the card never echoes the query either.
        self.assertEqual(len(context.bot.texts), 2)
        self.assertNotIn("синий кожаный рюкзак", context.bot.texts[1])

    def test_hypotheses_are_bounded_and_never_fragmented(self) -> None:
        """§8: at most MAX attempts, first = recognised phrase, no «чай в»."""

        # A legacy plain-string recognition is used as-is (cleaned: the
        # function word «с» goes, the content words stay) and is NEVER
        # truncated into a preposition-bounded fragment like «чай в».
        self.assertEqual(discovery.photo_hypotheses("чай в"), ["чай"])
        self.assertEqual(
            discovery.photo_hypotheses("синий кожаный рюкзак с молнией"),
            ["синий кожаный рюкзак молнией"],
        )
        self.assertIn(
            "молнией",
            discovery.photo_hypotheses("синий кожаный рюкзак с молнией")[0],
        )

        long_query = "очень длинный распознанный текст про синий кожаный рюкзак"
        variants = discovery.photo_hypotheses(long_query)
        self.assertTrue(variants)
        self.assertLessEqual(len(variants), discovery.MAX_PHOTO_ATTEMPTS)
        self.assertEqual(variants[0], discovery._clean_phrase(long_query))
        for variant in variants:
            self.assertNotIn(
                variant.split()[-1].lower(),
                discovery._PHOTO_EDGE_WORDS,
                f"a hypothesis must not end on a function word: {variant!r}",
            )

    def test_the_broad_core_tier_keeps_a_niche_photo_from_starving_the_pool(self) -> None:
        """§8: the same picture is also asked for in BROADER grounded words.

        A niche listing («Чай зеленый Тимофей пакет 100 г») answers a
        marketplace with a fuzzy handful; the same picture asked as the core
        head words of its type returns real listings of the very product shown.
        """

        analysis = {
            "product_name": "Чай зеленый Тимофей пакет 100 г",
            "product_type": "чай листовой зеленый",
            "visible_text": "Тимофей Зеленый чай, 100 г, Грузия",
        }

        variants = discovery.photo_hypotheses(analysis)

        # The exact name still leads (precision first)...
        self.assertEqual(variants[0], "Чай зеленый Тимофей пакет 100 г")
        # ...and the broad tier of the SAME picture is among the questions.
        self.assertIn("чай листовой", variants)
        self.assertIn("чай листовой зеленый", variants)
        self.assertLessEqual(len(variants), discovery.MAX_PHOTO_ATTEMPTS)

    def test_the_core_tier_never_collapses_to_one_vague_word(self) -> None:
        # The failure this pipeline exists for: ONE vague word answered a
        # photo of tea with a perfume.
        self.assertEqual(discovery._core_phrase("чай"), "")
        self.assertEqual(discovery._core_phrase("чай листовой"), "")
        self.assertEqual(discovery._core_phrase("чай листовой зеленый"), "чай листовой")
        self.assertEqual(
            discovery._core_phrase("зубная щётка электрическая с зарядкой"),
            "зубная щётка",
        )

    def test_a_photo_without_a_brand_still_gets_a_broad_hypothesis(self) -> None:
        # No brand and no model: the tiers still cover precision AND breadth
        # instead of repeating the same long phrase four times.
        variants = discovery.photo_hypotheses(
            {
                "product_name": "Термос Stanley 1000 мл",
                "product_type": "термос вакуумный",
            }
        )

        self.assertEqual(variants[0], "Термос Stanley 1000 мл")
        self.assertIn("термос вакуумный", variants)
        self.assertEqual(len(variants), 2)

    def test_structured_analysis_produces_grounded_hypotheses(self) -> None:
        """§7/§8: the hypotheses come from what the picture really showed."""

        analysis = {
            "product_type": "чай листовой зеленый",
            "brand": "Тимофей",
            "product_name": "Чай зеленый Тимофей пакет 100 г",
            "visible_text": "Тимофей Зеленый чай, 100 г, Грузия\nсостав: чай",
            "color": "зеленый",
        }
        variants = discovery.photo_hypotheses(analysis)

        self.assertTrue(variants)
        self.assertLessEqual(len(variants), discovery.MAX_PHOTO_ATTEMPTS)
        # First: the exact visible name; then brand+type; type+attribute...
        self.assertEqual(variants[0], "Чай зеленый Тимофей пакет 100 г")
        self.assertIn("Тимофей чай листовой зеленый", variants)
        for variant in variants:
            self.assertTrue(variant)
            self.assertNotIn(variant.split()[-1].lower(), discovery._PHOTO_EDGE_WORDS)

        # Validation keywords carry the whole concept (§9), not one word.
        keywords = discovery.photo_concept_keywords(analysis)
        self.assertIn("чай листовой зеленый", keywords)
        self.assertIn("Тимофей", keywords)

    def test_overlapping_hypotheses_are_deduplicated(self) -> None:
        """§6: the same listing returned by two queries is ONE product."""

        # Same canonical identity in both replies, different titles: dedup
        # must keep the FIRST occurrence, so the card can only show the
        # first title - a feed that naively concatenated the replies would
        # rank the second copy (with more matching words) first instead.
        ai = VisionAI("синий кожаный рюкзак с молнией и карманами")
        search = FakeSearchService()
        search.replies = [
            SearchOutcome(
                request=SearchRequest(original_query="a"),
                products=[make_listing("1", "Рюкзак простой", 10.0)],
            ),
            SearchOutcome(
                request=SearchRequest(original_query="b"),
                products=[make_listing("1", "Рюкзак кожаный синий с молнией", 10.0)],
            ),
        ]
        context = make_context(ai, search)
        message = PhotoMessage()

        run(discovery.photo_message(photo_update(message), context))

        # Both grounded hypotheses were tried (long recognition), and the whole
        # flow stayed inside its documented budget of one bounded search per
        # phrase and per page...
        self.assertEqual(
            set(search.queries),
            set(
                discovery.photo_hypotheses(
                    "синий кожаный рюкзак с молнией и карманами"
                )
            ),
        )
        self.assertLessEqual(
            len(search.queries),
            discovery.MAX_PHOTO_ATTEMPTS * discovery.PHOTO_MAX_PAGES,
        )
        # ...and the merged feed holds ONE copy of the shared listing: the
        # card first, then the count hint (which carries no product title).
        self.assertEqual(len(context.bot.texts), 2)
        card = context.bot.texts[0]
        self.assertIn("Рюкзак простой", card)
        self.assertNotIn("с молнией", card)
        self.assertNotIn("Рюкзак", context.bot.texts[1])

    def test_a_reworded_question_is_not_asked_twice(self) -> None:
        """§8: the same words in another order are ONE question, not two.

        The vision step routinely reads one product twice - as the visible
        name and as the readable packaging text - and the second phrasing
        would spend a bounded attempt on the answer the first one already
        returned.
        """

        variants = discovery.photo_hypotheses(
            {"product_name": "Тимофей чай", "visible_text": "чай Тимофей"}
        )

        self.assertEqual(variants, ["Тимофей чай"])

    def test_a_broader_question_is_never_treated_as_a_repeat(self) -> None:
        """Fewer words ask a WIDER question - that is what the tiers are for."""

        variants = discovery.photo_hypotheses(
            {
                "product_name": "Чай зеленый Тимофей пакет 100 г",
                "product_type": "чай листовой зеленый",
            }
        )

        # The visible type and its core pair use only words of the name, yet
        # they are BROADER questions about the same pictured product, so they
        # are asked - never deduplicated away as «already seen».
        self.assertEqual(variants[0], "Чай зеленый Тимофей пакет 100 г")
        self.assertEqual(variants[1:], ["чай листовой зеленый", "чай листовой"])

    def test_an_empty_result_reports_no_products_found(self) -> None:
        outcome = SearchOutcome(request=SearchRequest(original_query="x"))
        context = make_context(VisionAI("наушники"), FakeSearchService(outcome))
        message = PhotoMessage()

        run(discovery.photo_message(photo_update(message), context))

        self.assertIn("Не удалось определить товар", message.texts[-1])



class DiscoveryRoutingTests(unittest.TestCase):
    def test_mood_queries_are_discovery(self) -> None:
        for text in (
            "Что-нибудь прикольное для машины",
            "Странные, но полезные вещи для дома",
            "Что-нибудь необычное до $20",
            "Интересные гаджеты, которых я раньше не видел",
            "Удиви меня",
        ):
            with self.subTest(text=text):
                self.assertTrue(is_discovery_query(text))

    def test_a_named_product_keeps_it_a_normal_search(self) -> None:
        # "интересные наушники до 30 долларов" names a concrete product, so
        # relevance must stay the priority instead of being rerouted to a
        # mood feed.
        self.assertFalse(is_discovery_query("интересные наушники до 30 долларов"))

    def test_a_budget_never_turns_a_mood_query_into_an_exact_search(self) -> None:
        # "до $20" is a constraint, not a product: the request stays a
        # discovery one and the budget is applied to the real candidates.
        self.assertTrue(is_discovery_query("Что-нибудь необычное до $20"))

    def test_an_exact_product_query_is_a_normal_search(self) -> None:
        for text in (
            "беспроводные наушники до $30 с хорошими отзывами",
            "чехол для телефона",
            "видеорегистратор",
        ):
            with self.subTest(text=text):
                self.assertFalse(is_discovery_query(text))

    def test_discovery_runs_through_the_same_provider_pipeline(self) -> None:
        import httpx

        from config.settings import MarketplaceCredentials
        from models.product import Marketplace
        from models.search_request import SearchMode
        from services.marketplaces.base import MarketplaceProvider
        from services.search_service import SearchService

        class StubProvider(MarketplaceProvider):
            def __init__(self):
                super().__init__(
                    MarketplaceCredentials("key", "secret"), httpx.AsyncClient()
                )
                self.marketplace = Marketplace.ALIEXPRESS
                self.display_name = "AliExpress"
                self.requests = []

            async def search_products(self, request):
                self.requests.append(request)
                return [make_listing("1", "Органайзер для авто", 12.0)]

            async def get_product(self, product_id):
                return None

            def generate_affiliate_link(self, product):
                return None

        provider = StubProvider()
        service = SearchService([provider])

        outcome = run(service.find_discovery("Что-нибудь прикольное для машины"))

        self.assertEqual(provider.requests[0].mode, SearchMode.DISCOVERY)
        self.assertEqual(outcome.request.mode, SearchMode.DISCOVERY)
        # The seed comes from the user's own words, not an invented topic.
        self.assertIn("машины", provider.requests[0].normalized_query)

    def test_an_unconfigured_provider_yields_no_fake_products(self) -> None:
        from services.search_service import SearchService

        service = SearchService([])

        outcome = run(service.find_discovery("Удиви меня"))

        self.assertEqual(outcome.products, [])

    def test_a_budget_in_a_mood_query_is_applied_to_real_candidates(self) -> None:
        import httpx

        from config.settings import MarketplaceCredentials
        from models.product import Marketplace
        from services.marketplaces.base import MarketplaceProvider
        from services.search_service import SearchService

        class BudgetProvider(MarketplaceProvider):
            def __init__(self):
                super().__init__(
                    MarketplaceCredentials("key", "secret"), httpx.AsyncClient()
                )
                self.marketplace = Marketplace.ALIEXPRESS
                self.display_name = "AliExpress"

            async def search_products(self, request):
                return [
                    make_listing("1", "Необычная кружка", 12.0),
                    make_listing("2", "Необычный брелок", 45.0),
                ]

            async def get_product(self, product_id):
                return None

            def generate_affiliate_link(self, product):
                return None

        service = SearchService([BudgetProvider()])

        outcome = run(service.find_discovery("Что-нибудь необычное до $20"))

        self.assertEqual(outcome.request.max_price, 20.0)
        self.assertEqual(outcome.request.currency, "USD")
        self.assertEqual([p.product_id for p in outcome.products], ["1"])


if __name__ == "__main__":
    unittest.main()
