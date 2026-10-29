"""Search-quality regressions: the failures real users reported.

Every class below reproduces ONE observed problem the way it was reported,
and pins the honest answer to it:

- a keyboard search answered with a toothbrush (and the reverse);
- «электрическая зубная щетка» answered with a case FOR a toothbrush;
- «зелёный чай» answered with a perfume whose title mentions tea;
- a very popular irrelevant listing leading the feed over the product the
  user actually asked for;
- the empty-result screen explaining the bot's internals instead of saying
  one short human sentence;
- a product card echoing the query, a counter or a position;
- a photo search collapsed into one vague keyword, or the same listing
  shown twice because two hypotheses both returned it;
- the favorites / price-tracking screens, which could only be closed by
  deleting the user's own products.

Nothing here touches the network: providers, the vision step and the
database are stubs from the other test modules, and every product is a
plain :class:`Product`.
"""

from __future__ import annotations

import unittest

from config.settings import MarketplaceCredentials
from handlers import discovery
from handlers import favorites as favorites_handler
from handlers import search as search_handler
from keyboards.main import CB_BACK, LABEL_HOME
from keyboards.products import (
    LABEL_CARD_BACK,
    LABEL_CARD_NEXT,
    LABEL_CARD_RESTART,
    browse_card_keyboard,
    favorites_list_keyboard,
    tracked_list_keyboard,
)
from models.product import Availability, Marketplace, Product
from models.search_request import SearchMode, SearchRequest
from services.ai_service import AIService
from services.comparison_service import sort_products
from services.product_identity import identity_pair
from services.relevance import (
    classify_product,
    filter_relevant,
    leading_noun,
    query_noun_terms,
    rejection_reason,
    relevance_score,
    relevance_tier,
)
from services.search_service import SearchOutcome, SearchService
from tests.test_ai_menu_handler import (
    FakeBot,
    FakeContext,
    FakeDatabase,
    FakeUpdate,
    FakeUser,
    run,
)
from tests.test_favorites_stale import FakeApplication
from tests.test_favorites_stale import FakeDatabase as FavoritesDatabase
from tests.test_favorites_stale import favorite_row
from tests.test_photo_and_discovery import (
    FakeSearchService,
    PhotoMessage,
    VisionAI,
    make_context as photo_context,
    photo_update,
)
from tests.test_search_service import CONFIGURED_CREDENTIALS, StubProvider

ALIEXPRESS = Marketplace.ALIEXPRESS


def listing(
    title: str,
    *,
    product_id: str = "1",
    price: float = 10.0,
    orders: int | None = None,
) -> Product:
    """A listing shaped like a real AliExpress answer for this project."""

    return Product(
        marketplace=ALIEXPRESS,
        product_id=product_id,
        title=title,
        product_url=f"https://www.aliexpress.com/item/{product_id}.html",
        price=price,
        currency="USD",
        orders_count=orders,
        availability=Availability.IN_STOCK,
    )


def request(query: str, **overrides) -> SearchRequest:
    """A request built from one sentence (both query fields, like the bot)."""

    data = {"original_query": query, "normalized_query": query}
    data.update(overrides)
    return SearchRequest(**data)


def classify(title: str, query: str) -> str:
    """The product-type verdict of one title for one query."""

    return classify_product(listing(title), request(query))


class RelevanceRegressionTests(unittest.TestCase):
    """The reported «this has nothing to do with my search» cases."""

    def test_a_keyboard_query_is_not_answered_by_a_toothbrush(self) -> None:
        title = "Электрическая зубная щетка Oral B с зарядкой"

        reason = rejection_reason(listing(title), request("механическая клавиатура"))

        self.assertEqual(reason, "no_query_term_in_title")
        self.assertEqual(classify(title, "механическая клавиатура"), "noun_miss")

    def test_a_toothbrush_query_is_not_answered_by_a_keyboard(self) -> None:
        title = "Механическая клавиатура RGB для игр"

        reason = rejection_reason(listing(title), request("электрическая зубная щетка"))

        self.assertEqual(reason, "no_query_term_in_title")

    def test_a_case_for_the_toothbrush_is_not_the_toothbrush(self) -> None:
        """G24.6: sharing a word is not relevance - but it is EVIDENCE.

        The case still is NOT the toothbrush (verdict stays ``mentioned``);
        the difference is what happens next: instead of being silently
        removed from the funnel it is KEPT at tier 1 and ranked below every
        exact match - a related/accessory result must remain reachable
        (module docstring R5). See the ranking assertions below.
        """

        case = "Футляр для электрической зубной щетки, дорожный"

        self.assertIsNone(
            rejection_reason(listing(case), request("электрическая зубная щетка"))
        )
        self.assertEqual(classify(case, "электрическая зубная щетка"), "mentioned")
        # ...and it can never outrank the real toothbrush.
        brush = "Электрическая зубная щетка Oral B"
        request_ = request("электрическая зубная щетка")
        self.assertGreater(
            relevance_tier(listing(brush), request_),
            relevance_tier(listing(case), request_),
        )

    def test_the_toothbrush_itself_survives_the_gate(self) -> None:
        brush = "Электрическая зубная щетка Oral B с зарядкой"

        self.assertIsNone(
            rejection_reason(listing(brush), request("электрическая зубная щетка"))
        )
        self.assertEqual(classify(brush, "электрическая зубная щетка"), "exact")

    def test_a_mechanical_mat_is_not_a_mechanical_keyboard(self) -> None:
        """Only the ADJECTIVE matched - no product noun is in the title."""

        mat = "Мягкий коврик механический для мыши"

        self.assertEqual(classify(mat, "механическая клавиатура"), "noun_miss")
        self.assertEqual(
            rejection_reason(listing(mat), request("механическая клавиатура")),
            "no_product_noun_in_title",
        )

    def test_a_mat_for_the_keyboard_is_not_the_keyboard(self) -> None:
        """G24.6: the mat shares the PRODUCT noun - kept, tier below.

        «коврик … для клавиатуры» explicitly references the requested
        product, so it is a related/use-case result (verdict
        ``mentioned``), not junk: kept at tier 1, never above a real
        keyboard (tier 2).
        """

        mat = "Коврик механический для клавиатуры, большой"

        self.assertIsNone(
            rejection_reason(listing(mat), request("механическая клавиатура"))
        )
        self.assertEqual(classify(mat, "механическая клавиатура"), "mentioned")
        keyboard = "Механическая клавиатура Kailh"
        request_ = request("механическая клавиатура")
        self.assertGreater(
            relevance_tier(listing(keyboard), request_),
            relevance_tier(listing(mat), request_),
        )

    def test_a_perfume_that_mentions_tea_is_not_tea(self) -> None:
        """The reported case: an ingredient word is not the product.

        G24.6: verdict stays ``mentioned`` (it is not the tea), but the
        result is KEPT at tier 1 instead of being deleted - ranked below
        any real tea, never above it.
        """

        perfume = "Парфюмерная вода аромат чай 50 мл"

        self.assertEqual(classify(perfume, "зеленый чай"), "mentioned")
        self.assertIsNone(rejection_reason(listing(perfume), request("зеленый чай")))
        tea = "Чай зеленый листовой Тимофей 100 г"
        request_ = request("зеленый чай")
        self.assertGreater(
            relevance_tier(listing(tea), request_),
            relevance_tier(listing(perfume), request_),
        )

    def test_a_perfume_that_only_writes_tea_differently_is_dropped_too(self) -> None:
        perfume = "Парфюмерная вода с ароматом чая"

        self.assertEqual(
            rejection_reason(listing(perfume), request("зеленый чай")),
            "no_query_term_in_title",
        )

    def test_the_tea_itself_survives_the_gate(self) -> None:
        tea = "Чай зеленый листовой Тимофей 100 г"

        self.assertEqual(classify(tea, "зеленый чай"), "exact")
        self.assertIsNone(rejection_reason(listing(tea), request("зеленый чай")))

    def test_the_head_noun_is_read_from_the_users_own_words(self) -> None:
        """«электрическая зубная щетка» -> «щетка», not the adjectives."""

        query = request("электрическая зубная щетка")

        self.assertEqual(query_noun_terms(query, "cyrillic"), ("щетка",))
        self.assertEqual(
            leading_noun("Футляр для электрической зубной щетки", "cyrillic"), "футляр"
        )
        self.assertEqual(
            leading_noun("Электрическая зубная щетка Oral B", "cyrillic"), "щетка"
        )

    def test_an_english_query_never_hides_a_russian_title(self) -> None:
        """Two languages cannot be compared - so nothing is filtered."""

        self.assertIsNone(
            rejection_reason(
                listing("Механическая клавиатура RGB"), request("mechanical keyboard")
            )
        )

    def test_a_category_is_never_narrowed_by_the_product_type_gate(self) -> None:
        """A category IS many product types: one category word is enough."""

        category_request = SearchRequest(
            original_query="Электроника",
            normalized_query="electronics",
            mode=SearchMode.CATEGORY,
            category="electronics",
            keywords=["наушники"],
        )
        accessory = listing("Футляр для наушников, защитный чехол")

        self.assertEqual(classify_product(accessory, category_request), "unverified")
        self.assertIsNone(rejection_reason(accessory, category_request))

    def test_a_feed_without_a_product_query_is_left_alone(self) -> None:
        """Popular/hot-deal feeds have no query of their own - R3 cannot judge."""

        for mode in (SearchMode.POPULAR, SearchMode.DISCOVERY):
            with self.subTest(mode=mode):
                feed_request = SearchRequest(original_query="популярное", mode=mode)
                self.assertIsNone(
                    rejection_reason(listing("Крест нательный"), feed_request)
                )


class RelevanceRankingTests(unittest.TestCase):
    """Identity above popularity - the reported «irrelevant item on top»."""

    def test_a_verified_product_type_outranks_marketplace_popularity(self) -> None:
        popular = listing(
            "Bluetooth наушники Xiaomi", product_id="popular", orders=50000
        )
        verified = listing(
            "Беспроводные наушники TWS с кейсом", product_id="verified", orders=2
        )

        ordered = sort_products([popular, verified], request("беспроводные наушники"))

        self.assertEqual(
            [product.product_id for product in ordered], ["verified", "popular"]
        )

    def test_the_tier_counts_the_verified_product_type(self) -> None:
        query = request("беспроводные наушники")

        self.assertEqual(relevance_tier(listing("Наушники беспроводные TWS"), query), 2)
        self.assertEqual(relevance_tier(listing("Носки хлопковые мужские"), query), 0)

    def test_a_brand_led_latin_title_keeps_its_word_level_match(self) -> None:
        """A latin title may lead with the brand - it is not hidden for that."""

        brand_led = listing("Xiaomi Wireless Headphones Pro")

        self.assertEqual(
            relevance_tier(brand_led, request("wireless headphones")), 1
        )
        self.assertEqual(
            classify_product(brand_led, request("wireless headphones")), "unverified"
        )

    def test_a_feed_without_a_product_query_has_no_tier(self) -> None:
        """0 means «no evidence»: the feed order must stay untouched."""

        feed_request = SearchRequest(
            original_query="популярное", mode=SearchMode.POPULAR
        )

        self.assertEqual(relevance_tier(listing("Весы кухонные"), feed_request), 0)


class SearchPipelineRegressionTests(unittest.TestCase):
    """The gate and the ranking exactly as the search service composes them."""

    def setUp(self) -> None:
        self.providers: list[StubProvider] = []

    def tearDown(self) -> None:
        async def close_all() -> None:
            for provider in self.providers:
                await provider.aclose()

        if self.providers:
            run(close_all())

    def service(self, products: list[Product]) -> SearchService:
        provider = StubProvider(
            marketplace=ALIEXPRESS,
            display_name="AliExpress",
            products=products,
            credentials=CONFIGURED_CREDENTIALS,
        )
        self.providers.append(provider)
        return SearchService([provider])

    def test_the_pipeline_keeps_only_the_requested_product(self) -> None:
        """G24.6 pipeline contract: the exact product LEADS the result set.

        The accessory («футляр», verdict ``mentioned``, tier 1) survives
        the funnel now - but the real toothbrush (tier 2) must come
        first even though the accessory has 9000 orders and the brush 5:
        relevance tiers outrank marketplace popularity, and an accessory
        can never sit above the exact product.
        """
        service = self.service(
            [
                listing(
                    "Футляр для электрической зубной щетки",
                    product_id="case",
                    price=3.0,
                    orders=9000,
                ),
                listing(
                    "Электрическая зубная щетка Oral B",
                    product_id="brush",
                    price=25.0,
                    orders=5,
                ),
            ]
        )

        outcome = run(service.find_products(request("электрическая зубная щетка")))

        self.assertEqual(
            [product.product_id for product in outcome.products], ["brush", "case"]
        )
        self.assertEqual(outcome.products_before_filtering, 2)
        self.assertEqual(outcome.products_dropped_as_irrelevant, 0)
        self.assertEqual(outcome.searched_marketplaces, ["AliExpress"])

    def test_the_pipeline_puts_the_requested_product_first(self) -> None:
        service = self.service(
            [
                listing(
                    "Bluetooth наушники Xiaomi", product_id="popular", orders=50000
                ),
                listing("Беспроводные наушники TWS", product_id="exact", orders=2),
            ]
        )

        outcome = run(service.find_products(request("беспроводные наушники")))

        self.assertEqual(
            [product.product_id for product in outcome.products], ["exact", "popular"]
        )

    def test_one_listing_returned_twice_is_shown_once(self) -> None:
        service = self.service(
            [
                listing("Наушники беспроводные TWS", product_id="dup"),
                listing("Наушники беспроводные TWS", product_id="dup", price=11.0),
            ]
        )

        outcome = run(service.find_products(request("беспроводные наушники")))

        self.assertEqual(len(outcome.products), 1)
        self.assertEqual(outcome.products_before_filtering, 1)

    def test_a_seen_product_is_not_repeated_while_others_remain(self) -> None:
        first = listing("Наушники беспроводные TWS", product_id="p1")
        second = listing("Наушники беспроводные накладные", product_id="p2")
        service = self.service([first, second])

        outcome = run(
            service.find_products(
                request("беспроводные наушники"), exclude={identity_pair(first)}
            )
        )

        self.assertEqual([product.product_id for product in outcome.products], ["p2"])
        self.assertEqual(outcome.products_hidden_as_seen, 1)


class EmptyResultUxTests(unittest.TestCase):
    """One short human sentence - never the bot's internals."""

    def context(self) -> FakeContext:
        return FakeContext({"database": FakeDatabase(), "search_service": None})

    def empty_outcome(self, query: str, **overrides) -> SearchOutcome:
        data: dict = {"request": request(query), "products": []}
        data.update(overrides)
        return SearchOutcome(**data)

    def test_the_documented_message_is_one_short_sentence(self) -> None:
        text = search_handler.EMPTY_RESULT_TEXT

        self.assertEqual(
            text,
            "😕 По вашему запросу ничего не найдено. "
            "Попробуйте изменить запрос или параметры поиска.",
        )
        for forbidden in (
            "AliExpress",
            "API",
            "Gemini",
            "Traceback",
            "HTTP",
            "ключ",
            "токен",
            "фильтр",
            "релевант",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, text)

    def test_an_empty_search_sends_that_message_and_nothing_else(self) -> None:
        context = self.context()

        session_id = run(
            search_handler.send_search_results(
                context, 47, self.empty_outcome("сложный запрос с деталями")
            )
        )

        self.assertIsNone(session_id)
        self.assertEqual(context.bot.texts, [search_handler.EMPTY_RESULT_TEXT])
        self.assertEqual(len(context.bot.messages), 1)
        chat_id, _, kwargs = context.bot.messages[0]
        self.assertEqual(chat_id, 47)
        self.assertEqual(
            kwargs["reply_markup"].inline_keyboard[-1][0].callback_data, CB_BACK
        )

    def test_the_message_never_echoes_the_query_or_explains_a_failure(self) -> None:
        """A failing source, dropped candidates and counters stay in the log.

        B3: this outcome is a TOTAL provider failure (nothing was searched),
        so the honest unavailability line is shown - and it still carries no
        query, no source name, no dropped-candidate talk and no counters.
        """

        context = self.context()
        outcome = self.empty_outcome(
            "механическая клавиатура",
            unavailable_marketplaces=["AliExpress"],
            failed_marketplaces=["AliExpress"],
            products_before_filtering=20,
            products_dropped_as_irrelevant=18,
            products_hidden_as_seen=2,
        )

        run(search_handler.send_search_results(context, 47, outcome))

        _, text, _ = context.bot.messages[0]
        # The failure itself is now stated honestly and generically...
        self.assertEqual(text, search_handler.SEARCH_UNAVAILABLE_TEXT)
        # ...while everything specific stays in the log only.
        for forbidden in (
            "механическая",
            "клавиатура",
            "AliExpress",
            "отброш",
            "18",
            "20",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, text)


class UnrelatedProductIsNeverTheAnswerTests(unittest.TestCase):
    """The reported «Плазморезка» -> «Защитный ремешок для умных часов».

    The reported pair is reproduced with the REAL gate and the REAL service,
    and no literal word of the reported query is special-cased: what is pinned
    is the general principle that a candidate with no defensible semantic
    relationship to the requested product cannot reach the screen - whatever
    its popularity, discount or that it came back from the marketplace.
    """

    QUERY = "плазморезка"
    STRAP_TITLE = "Защитный ремешок для умных часов, силиконовый браслет"

    def request(self) -> SearchRequest:
        """The request the bot really builds for that one-word query."""

        return AIService._fallback_parse(self.QUERY)

    def test_the_watch_strap_is_rejected_by_every_stage(self) -> None:
        strap = listing(self.STRAP_TITLE, product_id="strap", orders=9000)

        self.assertEqual(
            rejection_reason(strap, self.request()), "no_query_term_in_title"
        )
        self.assertEqual(relevance_tier(strap, self.request()), 0)
        self.assertEqual(relevance_score(strap, self.request()), 0.0)
        kept, dropped = filter_relevant([strap], self.request())
        self.assertEqual(kept, [])
        self.assertEqual(dropped, {"no_query_term_in_title": 1})

    def test_an_unrelated_accessory_category_or_product_never_passes(self) -> None:
        unrelated = (
            "Чехол для умных часов силиконовый",
            "Сумка для ноутбука 15.6 дюймов",
            "Корм для кошек сухой 2 кг",
            "Настольная лампа светодиодная",
        )

        for title in unrelated:
            with self.subTest(title=title):
                product = listing(title, product_id=title[:4], orders=50000)
                kept, _ = filter_relevant([product], self.request())
                self.assertEqual(kept, [], title)

    def test_the_pipeline_returns_the_empty_result_instead(self) -> None:
        """High sales and a discount cannot buy a place in the results."""

        strap = listing(
            self.STRAP_TITLE + " со скидкой 70%",
            product_id="strap",
            orders=99999,
            price=1.99,
        )
        provider = StubProvider(
            marketplace=ALIEXPRESS,
            display_name="AliExpress",
            products=[strap],
            credentials=CONFIGURED_CREDENTIALS,
        )
        self.providers = [provider]
        service = SearchService([provider])

        outcome = run(service.find_products(self.request()))

        self.assertEqual(outcome.products, [])
        self.assertEqual(outcome.products_dropped_as_irrelevant, 1)
        context = FakeContext({"database": FakeDatabase(), "search_service": service})

        run(search_handler.send_search_results(context, 47, outcome))

        self.assertEqual(context.bot.texts, [search_handler.EMPTY_RESULT_TEXT])

    def test_a_genuinely_relevant_listing_still_passes(self) -> None:
        """The gate is not blanket-strict: the real product survives."""

        for title in (
            "Плазморезка CUT-50 плазменный резак 220В",
            "Плазморез CUT-60 аппарат плазменной резки",
        ):
            with self.subTest(title=title):
                product = listing(title, product_id=title[:4], orders=3)
                self.assertIsNone(rejection_reason(product, self.request()))
                self.assertEqual(classify_product(product, self.request()), "exact")
                self.assertEqual(relevance_tier(product, self.request()), 2)
                self.assertEqual(relevance_score(product, self.request()), 1.0)

    def tearDown(self) -> None:
        for provider in getattr(self, "providers", []):
            run(provider.aclose())


class CardCaptionTests(unittest.TestCase):
    """A card never echoes the query, a counter or a position."""

    def test_a_plain_search_card_has_no_line_above_the_product(self) -> None:
        outcome = SearchOutcome(
            request=request("беспроводные наушники"),
            products=[listing("Наушники беспроводные TWS")],
        )

        self.assertEqual(search_handler._card_caption(outcome, 0, None), "")

    def test_a_feed_screen_names_only_itself(self) -> None:
        outcome = SearchOutcome(
            request=request("популярное"), products=[listing("Товар")]
        )

        self.assertEqual(
            search_handler._card_caption(outcome, 0, "🔥 <b>Популярное</b>"),
            "🔥 <b>Популярное</b>",
        )

    def test_the_sent_card_carries_no_query_counter_or_position(self) -> None:
        context = FakeContext({"database": FakeDatabase(), "search_service": None})
        outcome = SearchOutcome(
            request=request("беспроводные наушники до 30 долларов"),
            # A real order count: this test watches the CAPTION, not the
            # quality gate.
            products=[listing("Наушники беспроводные TWS", orders=150)],
            searched_marketplaces=["AliExpress"],
        )

        session_id = run(search_handler.send_search_results(context, 47, outcome))

        self.assertIsNotNone(session_id)
        card = context.bot.texts[0]
        self.assertIn("Наушники беспроводные TWS", card)
        for forbidden in (
            "Запрос",
            "Найдено",
            "1/1",
            "Стр.",
            "беспроводные наушники до 30 долларов",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, card)


class StructuredVisionAI:
    """``analyze_image`` stub: what the vision step really saw in one photo."""

    is_available = True

    def __init__(self, analysis: dict | None) -> None:
        self.analysis = analysis
        self.image_requests = 0

    async def analyze_image(self, image_bytes: bytes) -> dict | None:
        self.image_requests += 1
        return self.analysis


#: A structured analysis of a photo of a tea pack (the reported case: the
#: same flow used to send one vague word, and a perfume answered it).
TEA_PHOTO = {
    "product_name": "Чай зеленый Тимофей пакет 100 г",
    "product_type": "чай листовой зеленый",
    "visible_text": "Тимофей Зеленый чай, 100 г, Грузия",
}


class PhotoSearchRegressionTests(unittest.TestCase):
    """The photo flow searches what the picture shows - and stops there."""

    def setUp(self) -> None:
        self.providers: list[StubProvider] = []

    def tearDown(self) -> None:
        async def close_all() -> None:
            for provider in self.providers:
                await provider.aclose()

        if self.providers:
            run(close_all())

    def service(self, products: list[Product]) -> tuple[SearchService, StubProvider]:
        provider = StubProvider(
            marketplace=ALIEXPRESS,
            display_name="AliExpress",
            products=products,
            credentials=CONFIGURED_CREDENTIALS,
        )
        self.providers.append(provider)
        return SearchService([provider]), provider

    def test_a_structured_analysis_yields_grounded_hypotheses(self) -> None:
        hypotheses = discovery.photo_hypotheses(TEA_PHOTO)

        self.assertEqual(hypotheses[0], TEA_PHOTO["product_name"])
        self.assertIn(TEA_PHOTO["product_type"], hypotheses)
        # The BROAD tier of the same picture is among the questions too: the
        # exact name alone answers a marketplace with a fuzzy handful.
        self.assertIn("чай листовой", hypotheses)
        self.assertLessEqual(len(hypotheses), discovery.MAX_PHOTO_ATTEMPTS)
        for phrase in hypotheses:
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase.split()[-1].lower(), {"в", "с", "для"})

    def test_the_flow_spends_at_most_the_fixed_number_of_searches(self) -> None:
        ai = StructuredVisionAI(TEA_PHOTO)
        search = FakeSearchService()
        context = photo_context(ai, search)

        run(discovery.photo_message(photo_update(PhotoMessage()), context))

        self.assertEqual(ai.image_requests, 1)
        self.assertLessEqual(len(search.queries), discovery.MAX_PHOTO_ATTEMPTS)
        self.assertEqual(
            set(search.queries), set(discovery.photo_hypotheses(TEA_PHOTO))
        )

    def test_a_phrase_that_answered_is_asked_for_its_next_page(self) -> None:
        """§8: a thin first page is not the whole answer for a pictured product."""

        ai = StructuredVisionAI(TEA_PHOTO)
        product = listing("Чай листовой Тимофей 100 г", product_id="tea")
        search = FakeSearchService(
            SearchOutcome(
                request=SearchRequest(original_query="x"),
                products=[product],
                products_before_filtering=1,
            )
        )
        context = photo_context(ai, search)

        run(discovery.photo_message(photo_update(PhotoMessage()), context))

        hypotheses = discovery.photo_hypotheses(TEA_PHOTO)
        # Every grounded question is asked, page 1 first...
        self.assertEqual(search.queries[: len(hypotheses)], hypotheses)
        # ...and a phrase that ANSWERS is asked for its next documented page
        # too: deeper pages of the words the picture really showed add real
        # listings of that very product, never a word the vision step missed.
        self.assertEqual(search.pages, [1] * len(hypotheses) + [2] * len(hypotheses))

    def test_a_phrase_that_found_nothing_is_never_asked_deeper(self) -> None:
        """A marketplace with no rows for those words has none on page 2 either."""

        search = FakeSearchService()
        context = photo_context(StructuredVisionAI(TEA_PHOTO), search)

        run(discovery.photo_message(photo_update(PhotoMessage()), context))

        self.assertEqual(search.queries, discovery.photo_hypotheses(TEA_PHOTO))
        self.assertEqual(set(search.pages), {1})

    def test_the_photo_retrieval_budgets_come_from_the_settings(self) -> None:
        class Settings:
            photo_search_hypotheses = 1
            photo_search_pages = 1

        product = listing("Чай листовой Тимофей 100 г", product_id="tea")
        search = FakeSearchService(
            SearchOutcome(
                request=SearchRequest(original_query="x"),
                products=[product],
                products_before_filtering=1,
            )
        )
        context = photo_context(
            StructuredVisionAI(TEA_PHOTO), search, settings=Settings()
        )

        run(discovery.photo_message(photo_update(PhotoMessage()), context))

        # One grounded question, one page of it: the numbers belong to the
        # deployment, and the flow still asks nothing the picture did not show.
        self.assertEqual(search.queries, [discovery.photo_hypotheses(TEA_PHOTO)[0]])
        self.assertEqual(search.pages, [1])

    def test_a_broken_budget_setting_never_turns_one_photo_into_a_crawl(self) -> None:
        class Settings:
            def __init__(self, pages) -> None:
                self.photo_search_pages = pages

        # Every unit is a REAL marketplace call: a missing settings object, a
        # typo in .env or a silly number fall back to / clamp at the documented
        # bound instead of spending the catalogue on one photo.
        self.assertEqual(
            discovery._bounded_setting(None, "photo_search_pages", 2, 5), 2
        )
        self.assertEqual(
            discovery._bounded_setting(Settings(0), "photo_search_pages", 2, 5), 1
        )
        self.assertEqual(
            discovery._bounded_setting(Settings("мусор"), "photo_search_pages", 2, 5), 2
        )
        self.assertEqual(
            discovery._bounded_setting(Settings(99), "photo_search_pages", 2, 5), 5
        )

    def test_a_photo_of_tea_is_not_answered_by_a_perfume(self) -> None:
        """The pictured product becomes the concept the relevance gate uses."""

        service, provider = self.service(
            [
                listing("Парфюмерная вода аромат чай 50 мл", product_id="perfume"),
                listing("Чай зеленый листовой Тимофей 100 г", product_id="tea"),
            ]
        )
        context = photo_context(StructuredVisionAI(TEA_PHOTO), service)

        run(discovery.photo_message(photo_update(PhotoMessage()), context))

        card = context.bot.texts[0]
        self.assertIn("Чай зеленый листовой Тимофей", card)
        self.assertNotIn("Парфюмерная вода", card)
        # Bounded by the documented budget: one grounded question per phrase
        # and per page (the phrases answer, so their deeper pages are asked).
        self.assertLessEqual(
            provider.search_calls,
            discovery.MAX_PHOTO_ATTEMPTS * discovery.PHOTO_MAX_PAGES,
        )

    def test_the_same_listing_from_two_hypotheses_is_shown_once(self) -> None:
        service, provider = self.service(
            [listing("Чай зеленый листовой Тимофей 100 г", product_id="tea")]
        )
        context = photo_context(StructuredVisionAI(TEA_PHOTO), service)

        run(discovery.photo_message(photo_update(PhotoMessage()), context))

        # Several hypotheses ran, each returned the same listing: the user
        # gets ONE card (plus the single deterministic count hint), not one
        # card per query.
        self.assertGreater(provider.search_calls, 1)
        self.assertEqual(len(context.bot.texts), 2)
        # Card FIRST, hint once AFTER it.
        self.assertEqual(context.bot.texts[0].count("Тимофей"), 1)
        self.assertNotIn("Тимофей", context.bot.texts[1])

    def test_the_search_stops_once_enough_unique_products_were_found(self) -> None:
        products = [
            listing(
                f"Чай зеленый листовой Тимофей {index} 100 г",
                product_id=f"tea{index}",
            )
            for index in range(discovery.PHOTO_ENOUGH_RESULTS + 1)
        ]
        service, provider = self.service(products)
        context = photo_context(StructuredVisionAI(TEA_PHOTO), service)

        run(discovery.photo_message(photo_update(PhotoMessage()), context))

        self.assertEqual(provider.search_calls, 1)

    def test_the_generated_query_is_never_echoed_on_the_card(self) -> None:
        service, _ = self.service(
            [listing("Чай зеленый листовой Тимофей 100 г", product_id="tea")]
        )
        context = photo_context(StructuredVisionAI(TEA_PHOTO), service)

        run(discovery.photo_message(photo_update(PhotoMessage()), context))

        card = context.bot.texts[0]
        for forbidden in ("Запрос", "пакет", "Грузия", "Найдено"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, card)

    def test_an_unreadable_photo_gets_the_documented_short_answer(self) -> None:
        context = photo_context(StructuredVisionAI(None), self.service([])[0])

        run(discovery.photo_message(photo_update(PhotoMessage()), context))

        self.assertEqual(context.bot.texts, [])
        self.assertEqual(
            discovery.PHOTO_NO_AI_TEXT, discovery.IMAGE_EMPTY_TEXT
        )


class CardNavigationTests(unittest.TestCase):
    """◀️/➡️ walk the loaded list; the first card leaves the feed."""

    def button(self, keyboard, label: str) -> str:
        for row in keyboard.inline_keyboard:
            for button in row:
                if button.text == label:
                    return button.callback_data
        raise AssertionError(f"Кнопка «{label}» отсутствует")

    def labels(self, keyboard) -> list[str]:
        return [button.text for row in keyboard.inline_keyboard for button in row]

    def test_next_and_back_stay_inside_the_loaded_list(self) -> None:
        middle = browse_card_keyboard("s1", 1, 3, listing("Товар", product_id="1"))

        self.assertEqual(self.button(middle, LABEL_CARD_NEXT), "pnav:s1:2")
        self.assertEqual(self.button(middle, LABEL_CARD_BACK), "pnav:s1:0")

    def test_the_first_card_leaves_the_feed_on_back(self) -> None:
        first = browse_card_keyboard("s1", 0, 3, listing("Товар", product_id="1"))

        self.assertEqual(self.button(first, LABEL_CARD_NEXT), "pnav:s1:1")
        self.assertEqual(self.button(first, LABEL_CARD_BACK), CB_BACK)

    def test_the_first_card_returns_to_the_screen_it_came_from(self) -> None:
        keyboard = browse_card_keyboard(
            "s1", 0, 2, listing("Товар"), back_callback="menu:popular"
        )

        self.assertEqual(self.button(keyboard, LABEL_CARD_BACK), "menu:popular")

    def test_the_last_card_restarts_instead_of_dead_ending(self) -> None:
        keyboard = browse_card_keyboard("s1", 2, 3, listing("Товар"))

        self.assertEqual(self.button(keyboard, LABEL_CARD_RESTART), "pnav:s1:0")
        self.assertNotIn(LABEL_CARD_NEXT, self.labels(keyboard))

    def test_a_single_product_has_no_next_and_no_restart(self) -> None:
        keyboard = browse_card_keyboard("s1", 0, 1, listing("Товар"))

        self.assertNotIn(LABEL_CARD_NEXT, self.labels(keyboard))
        self.assertNotIn(LABEL_CARD_RESTART, self.labels(keyboard))
        self.assertIn(LABEL_CARD_BACK, self.labels(keyboard))
        self.assertIn(LABEL_HOME, self.labels(keyboard))


class DeletableMessage:
    """The message a callback came from, with a recorded deletion."""

    def __init__(self, chat_id: int = 47, log: list | None = None) -> None:
        self.chat_id = chat_id
        self.log = log if log is not None else []
        self.deleted = False

    async def delete(self) -> None:
        self.deleted = True
        self.log.append("delete")


class OrderRecordingBot(FakeBot):
    """A bot double that records the order of its own messages."""

    def __init__(self, log: list) -> None:
        super().__init__()
        self.log = log

    async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
        self.log.append("send")
        await super().send_message(chat_id, text, **kwargs)


class RefusingBot(FakeBot):
    """A bot double whose send is refused (a blocked chat, a bad button URL)."""

    async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
        raise RuntimeError("Telegram refused the message")


class ScreenContext:
    """A context carrying a real bot double and the favorites database."""

    def __init__(self, database, bot=None) -> None:
        self.bot = bot if bot is not None else FakeBot()
        self.user_data: dict = {}
        self.application = FakeApplication({"database": database})


class MenuButtonQuery:
    """The «⭐ Избранное» button of the main menu (or any list callback)."""

    def __init__(
        self,
        data: str = "menu:favorites",
        *,
        fail_edit: Exception | None = None,
        log: list | None = None,
    ) -> None:
        self.data = data
        self.message = DeletableMessage(log=log)
        self.answers: list[tuple[str | None, bool]] = []
        self.edits: list[tuple[str, dict]] = []
        self.fail_edit = fail_edit

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text: str, **kwargs) -> None:
        if self.fail_edit is not None:
            raise self.fail_edit
        self.edits.append((text, kwargs))


class FavoritesDialogTests(unittest.TestCase):
    """The favorites and price-tracking screens: obtainable and closable."""

    def setUp(self) -> None:
        self.log: list[str] = []
        self.product = listing(
            "Набор для чистки мебели, щетка и спрей", product_id="100"
        )
        self.database = FavoritesDatabase([favorite_row(self.product)])

    def show(self, query: MenuButtonQuery, context: ScreenContext) -> None:
        run(favorites_handler.show_favorites(FakeUpdate(callback_query=query), context))

    def test_the_favorites_list_ends_with_the_way_home(self) -> None:
        keyboard = favorites_list_keyboard([{"index": 0, "product": self.product}])
        last_row = keyboard.inline_keyboard[-1]

        self.assertEqual([button.callback_data for button in last_row], [CB_BACK])
        self.assertEqual(last_row[0].text, LABEL_HOME)

    def test_the_tracking_list_ends_with_the_same_way_home(self) -> None:
        keyboard = tracked_list_keyboard(
            [
                {
                    "tracked_id": 1,
                    "title": "Товар",
                    "url": None,
                    "price": 1.0,
                    "currency": "USD",
                }
            ]
        )

        self.assertEqual(
            [button.callback_data for button in keyboard.inline_keyboard[-1]],
            [CB_BACK],
        )

    def test_the_menu_button_shows_the_list_without_a_second_message(self) -> None:
        query = MenuButtonQuery()
        context = ScreenContext(self.database)

        self.show(query, context)

        self.assertEqual(len(query.edits), 1)
        text, kwargs = query.edits[0]
        self.assertIn("⭐ <b>Избранное</b>", text)
        self.assertEqual(
            kwargs["reply_markup"].inline_keyboard[-1][0].callback_data, CB_BACK
        )
        self.assertEqual(context.bot.messages, [])

    def test_an_empty_list_ends_the_dialog_the_same_way(self) -> None:
        query = MenuButtonQuery()
        context = ScreenContext(FavoritesDatabase([]))

        self.show(query, context)

        _, kwargs = query.edits[0]
        self.assertEqual(
            kwargs["reply_markup"].inline_keyboard[-1][0].callback_data, CB_BACK
        )

    def test_a_refused_edit_re_sends_the_screen_and_keeps_the_old_message(self) -> None:
        """Telegram refuses to edit a photo message: the dialog must survive."""

        query = MenuButtonQuery(
            fail_edit=RuntimeError("Message is a photo"), log=self.log
        )
        context = ScreenContext(self.database, bot=OrderRecordingBot(self.log))

        self.show(query, context)

        # The fresh screen is sent FIRST and the previous message is removed
        # only AFTER it is really on screen - never the other way round.
        self.assertEqual(self.log, ["send", "delete"])
        self.assertEqual(len(context.bot.messages), 1)

    def test_a_failed_fallback_keeps_the_screen_the_user_is_looking_at(self) -> None:
        query = MenuButtonQuery(
            fail_edit=RuntimeError("Message is a photo"), log=self.log
        )
        context = ScreenContext(self.database, bot=RefusingBot())

        # The reason is logged (never shown): captured here so the honest
        # log line does not leak into the test output as noise.
        with self.assertLogs("handlers.favorites", level="ERROR"):
            self.show(query, context)

        self.assertEqual(self.log, [])  # nothing was deleted
        self.assertTrue(query.answers)  # ...and the press was acknowledged

    def test_pressing_the_same_button_twice_is_not_an_error(self) -> None:
        query = MenuButtonQuery(
            fail_edit=RuntimeError("Message is not modified"), log=self.log
        )
        context = ScreenContext(self.database)

        self.show(query, context)

        self.assertEqual(context.bot.messages, [])
        self.assertEqual(self.log, [])
        self.assertEqual(query.answers, [(None, False)])


if __name__ == "__main__":
    unittest.main()
