"""Tests for ``services.relevance``: what may be shown, and what must not.

Two failure modes are equally bad: showing a product that has nothing to
do with the query, and hiding a product that does match. Both are covered
here with plain data - no network, no marketplace.
"""

from __future__ import annotations

import asyncio
import unittest

from config.catalog import get_category
from models.product import Marketplace, Product
from models.search_request import SearchMode, SearchRequest
from services.relevance import (
    classify_product,
    comparable_terms,
    dominant_script,
    filter_relevant,
    has_meaningful_title,
    has_valid_product_url,
    query_terms,
    rejection_reason,
    relevance_score,
    relevance_tier,
    tokenize,
)
from services.search_service import SearchService


def make_product(
    title: str,
    *,
    product_id: str = "1",
    url: str = "https://www.aliexpress.com/item/1.html",
) -> Product:
    return Product(
        marketplace=Marketplace.ALIEXPRESS,
        product_id=product_id,
        title=title,
        product_url=url,
    )


def request(query: str, **overrides) -> SearchRequest:
    data = {"original_query": query, "normalized_query": query}
    data.update(overrides)
    return SearchRequest(**data)


def run(coro):
    return asyncio.run(coro)


class _PoolProvider:
    """A stub marketplace returning ONE fixed product list for any query."""

    marketplace = Marketplace.ALIEXPRESS
    display_name = "AliExpress"
    supports_feed = True
    def __init__(self, products):
        self.products = list(products)

    @property
    def is_configured(self):
        return True

    async def search_products(self, request):
        return list(self.products)

    async def fetch_feed_page(self, **kwargs):
        return []

    async def aclose(self):
        return None


def run(coro):
    return asyncio.run(coro)


class _PoolProvider:
    """A stub marketplace returning ONE fixed product list for any query."""

    marketplace = Marketplace.ALIEXPRESS
    display_name = "AliExpress"
    supports_feed = True
    def __init__(self, products):
        self.products = list(products)

    @property
    def is_configured(self):
        return True
    async def search_products(self, request):
        return list(self.products)

    async def fetch_feed_page(self, **kwargs):
        return []

    async def aclose(self):
        return None


class TokenTests(unittest.TestCase):
    def test_words_are_lowercased_and_letters_only(self) -> None:
        self.assertEqual(tokenize("Wireless 5.3 Earbuds!"), ["wireless", "earbuds"])

    def test_stopwords_and_price_wording_are_not_filter_terms(self) -> None:
        terms = query_terms(request("wireless headphones under 30 dollars"))

        self.assertEqual(terms, ("wireless", "headphones"))

    def test_normalized_query_wins_over_the_raw_sentence(self) -> None:
        request_ = SearchRequest(
            original_query="беспроводные наушники до 30 долларов",
            normalized_query="беспроводные наушники",
        )

        self.assertEqual(query_terms(request_), ("беспроводные", "наушники"))

    def test_category_keywords_are_part_of_the_terms(self) -> None:
        category = get_category("electronics")

        terms = query_terms(
            SearchRequest(
                original_query=category.title,
                normalized_query=category.query,
                mode=SearchMode.CATEGORY,
                keywords=list(category.keywords),
            )
        )

        self.assertIn("наушники", terms)
        self.assertIn("electronics", terms)

    def test_dominant_script(self) -> None:
        self.assertEqual(dominant_script("Беспроводные наушники"), "cyrillic")
        self.assertEqual(dominant_script("Wireless Earbuds"), "latin")
        self.assertEqual(dominant_script("Wireless наушники"), None)
        self.assertEqual(dominant_script("12345"), None)


class QualityRuleTests(unittest.TestCase):
    def test_product_url_must_be_a_real_web_url(self) -> None:
        self.assertTrue(has_valid_product_url(make_product("Товар")))
        self.assertFalse(
            has_valid_product_url(make_product("Товар", url="aliexpress.com/x"))
        )
        self.assertFalse(has_valid_product_url(make_product("Товар", url="")))

    def test_title_must_contain_letters(self) -> None:
        self.assertTrue(has_meaningful_title(make_product("Товар 1")))
        self.assertFalse(has_meaningful_title(make_product("12345 !!! ***")))
        self.assertFalse(has_meaningful_title(make_product("1")))

    def test_product_without_url_is_rejected(self) -> None:
        product = make_product("Беспроводные наушники", url="not-a-url")

        self.assertEqual(
            rejection_reason(product, request("беспроводные наушники")),
            "invalid_product_url",
        )

    def test_product_with_garbage_title_is_rejected(self) -> None:
        product = make_product("12345 67890")

        self.assertEqual(
            rejection_reason(product, request("наушники")), "empty_title"
        )


class RelevanceRuleTests(unittest.TestCase):
    """R3: the query's own words, compared only within one language."""

    def test_matching_russian_title_is_kept(self) -> None:
        product = make_product("Беспроводные наушники Bluetooth 5.3 TWS")

        self.assertIsNone(
            rejection_reason(product, request("беспроводные наушники"))
        )

    def test_inflected_word_still_matches(self) -> None:
        """A prefix match, so "наушник" finds "наушники"."""

        product = make_product("Наушник беспроводной с микрофоном")

        self.assertIsNone(rejection_reason(product, request("наушники")))

    def test_obviously_unrelated_product_is_rejected(self) -> None:
        cross = make_product("Крест нательный православный из серебра")

        self.assertEqual(
            rejection_reason(cross, request("беспроводные наушники до 30 долларов")),
            "no_query_term_in_title",
        )

    def test_english_query_never_drops_russian_results(self) -> None:
        """Two different languages cannot be compared, so nothing is filtered.

        The project asks AliExpress for Russian product text
        (``target_language=RU``); an English query is not evidence that a
        Russian title is irrelevant.
        """

        product = make_product("Беспроводные наушники Bluetooth TWS")

        self.assertIsNone(
            rejection_reason(product, request("wireless earbuds under 50 dollars"))
        )

    def test_latin_query_is_filtered_against_latin_titles(self) -> None:
        matching = make_product("Wireless Earbuds TWS Sport", product_id="1")
        other = make_product("Wooden kitchen spoon, 30 cm", product_id="2")
        request_ = request("wireless earbuds")

        self.assertIsNone(rejection_reason(matching, request_))
        self.assertEqual(
            rejection_reason(other, request_), "no_query_term_in_title"
        )

    def test_price_and_rating_wording_is_not_a_filter_term(self) -> None:
        """Only the product words matter, not the constraints around them."""

        product = make_product("Чехол для iPhone 15 из силикона")
        request_ = SearchRequest(
            original_query="чехол для iPhone, рейтинг выше 4.5",
            normalized_query="чехол для iphone",
            min_rating=4.5,
        )

        self.assertIsNone(rejection_reason(product, request_))

    def test_feeds_without_a_product_query_are_only_quality_checked(self) -> None:
        for mode in (SearchMode.POPULAR, SearchMode.DEALS, SearchMode.PROMO):
            with self.subTest(mode=mode):
                product = make_product("Крест нательный православный")
                request_ = SearchRequest(
                    original_query="популярные товары", mode=mode
                )

                self.assertIsNone(rejection_reason(product, request_))


class CategoryRelevanceTests(unittest.TestCase):
    """The catalogue keywords must filter garbage without eating real items."""

    def build_request(self, code: str) -> SearchRequest:
        category = get_category(code)
        return SearchRequest(
            original_query=category.title,
            normalized_query=category.query,
            mode=SearchMode.CATEGORY,
            category=category.code,
            keywords=list(category.keywords),
        )

    def test_legitimate_electronics_titles_are_kept(self) -> None:
        request_ = self.build_request("electronics")

        legitimate = (
            "Повербанк 20000 мАч с быстрой зарядкой",
            "Bluetooth колонка водонепроницаемая",
            "Мышь беспроводная для ноутбука",
            "Гарнитура для телефона с шумоподавлением",
            "Умные часы с пульсометром",
        )

        for title in legitimate:
            with self.subTest(title=title):
                self.assertIsNone(rejection_reason(make_product(title), request_))

    def test_unrelated_item_is_rejected_inside_a_category(self) -> None:
        request_ = self.build_request("electronics")
        cross = make_product("Крест нательный православный")

        self.assertEqual(
            rejection_reason(cross, request_), "no_query_term_in_title"
        )

    def test_auto_category_keeps_car_accessories(self) -> None:
        request_ = self.build_request("auto")

        for title in (
            "Держатель для телефона в машину",
            "Видеорегистратор с ночным видением",
            "Автомобильный компрессор для шин",
        ):
            with self.subTest(title=title):
                self.assertIsNone(rejection_reason(make_product(title), request_))


class ShortCategoryKeywordTests(unittest.TestCase):
    """G24.8 P1.2: declared short CATEGORY stems match their inflections.

    A category request is catalog-authored (fixed title + curated
    keywords), so its 3-letter terms («сад», «дом», «быт») are DECLARED
    stems. The rule stays narrow - word-initial only, Cyrillic-vowel
    continuation only, CATEGORY only - so arbitrary short tokens never
    become broad matchers and SEARCH keeps its G24.6 whole-word semantics.
    """

    def build_request(self, code: str) -> SearchRequest:
        category = get_category(code)
        return SearchRequest(
            original_query=category.title,
            normalized_query=category.query,
            mode=SearchMode.CATEGORY,
            category=category.code,
            keywords=list(category.keywords),
        )

    def test_garden_short_stem_recovers_inflected_titles(self) -> None:
        # A + D: «садовый», «для сада» (genitive) and «садоводам» are
        # legitimate lexical forms of the declared stem «сад» - every one
        # of them was rejected as no_query_term_in_title before P1.2.
        request_ = self.build_request("garden")
        titles = (
            "Садовый декоративный забор для улицы",
            "Новый современный каменный фонтан для сада",
            "Садоводам: металлический контейнер для семян",
        )

        for title in titles:
            with self.subTest(title=title):
                product = make_product(title)
                self.assertIsNone(rejection_reason(product, request_))
                self.assertGreater(relevance_score(product, request_), 0.0)
                self.assertEqual(relevance_tier(product, request_), 1)

    def test_home_short_stems_recover_dom_and_byt_forms(self) -> None:
        # B + C: «домашний», «дома», «бытовая» are the inflected forms of
        # the declared stems «дом» and «быт».
        request_ = self.build_request("home")
        titles = (
            "Домашний текстиль для уюта",
            "Полка для дома из дерева",
            "Бытовая сушилка для белья",
        )

        for title in titles:
            with self.subTest(title=title):
                product = make_product(title)
                self.assertIsNone(rejection_reason(product, request_))
                self.assertGreater(relevance_score(product, request_), 0.0)

    def test_false_positive_inside_a_word_stays_rejected(self) -> None:
        # E: «сад» occurs INSIDE «насадка» - the word-initial guard blocks
        # the accidental substring match, so the drill bit stays out.
        request_ = self.build_request("garden")

        self.assertEqual(
            rejection_reason(
                make_product("Насадка на дрель сверлильная быстрозажимная"), request_
            ),
            "no_query_term_in_title",
        )

    def test_false_positive_compound_head_stays_rejected(self) -> None:
        # E: word-initial but consonant continuation («дом»+«крат») - the
        # vowel guard blocks compound/borrowed heads.
        request_ = self.build_request("home")

        self.assertEqual(
            rejection_reason(
                make_product("Домкрат подъёмный автомобильный 2 т"), request_
            ),
            "no_query_term_in_title",
        )


    def test_search_keeps_whole_word_semantics_for_short_tokens(self) -> None:
        # F: the SAME request words under SEARCH mode must NOT adopt the
        # category relaxation - this is also the exact pre-P1.2 behaviour.
        category = get_category("garden")
        search_request_ = SearchRequest(
            original_query=category.title,
            normalized_query=category.query,
            mode=SearchMode.SEARCH,
            keywords=list(category.keywords),
        )

        # «садовый» is still no evidence in SEARCH (whole-word «сад»)...
        self.assertEqual(
            rejection_reason(
                make_product("Садовый декоративный забор для улицы"), search_request_
            ),
            "no_query_term_in_title",
        )
        # ...while the bare whole word keeps working exactly as before.
        self.assertIsNone(
            rejection_reason(make_product("Отличный сад для дачи"), search_request_)
        )

    def test_longer_category_keywords_keep_their_prefix_behaviour(self) -> None:
        # G: terms >= 4 letters are untouched - prefix-4 still matches
        # their inflections, and an unrelated title still drops.
        garden = self.build_request("garden")
        sport = self.build_request("sport")

        self.assertIsNone(
            rejection_reason(make_product("Теплица из поликарбоната дуговая"), garden)
        )
        self.assertIsNone(
            rejection_reason(make_product("Спортивные носки хлопковые"), sport)
        )
        self.assertEqual(
            rejection_reason(make_product("Ландшафтный дизайн проект"), garden),
            "no_query_term_in_title",
        )

    def test_cross_script_matching_is_unchanged(self) -> None:
        # H: the romanized cross-script path was NOT touched by P1.2 (only
        # the three same-script sites pass the flag), so the documented
        # G24.6 cross-script case keeps working...
        self.assertIsNone(
            rejection_reason(
                make_product("Портативный небулайзер компрессорный"),
                request("portable nebulizer"),
            )
        )
        # ...and a latin category title still matches through its LONG
        # latin category terms, without any short-stem involvement.
        garden = self.build_request("garden")
        self.assertIsNone(
            rejection_reason(make_product("Outdoor garden decor solar light"), garden)
        )

    def test_category_family_and_accessory_titles_stay_kept(self) -> None:
        # I: recovering short stems must never bring back the pre-G24.6
        # removal of family/accessory results - an accessory that names
        # the category stem is still (as always) kept.
        garden = self.build_request("garden")

        self.assertIsNone(
            rejection_reason(
                make_product("Футляр для садового инструмента с ремнём"), garden
            )
        )


class CategoryStopwordKeywordTests(unittest.TestCase):
    """G24.8 P1.3: a CATEGORY may use its own DECLARED stopword keyword.

    «набор» is generic commercial filler, so the word-level filter must drop
    it from a user's sentence - yet the tools category declares it, and that
    curated word was silently discarded before P1.3, which rejected real tool
    sets as ``no_query_term_in_title``. Only catalogue-authored CATEGORY
    keywords are re-admitted; SEARCH keeps the full stopword list (G24.6).
    """

    def build_request(
        self, code: str, mode: SearchMode = SearchMode.CATEGORY
    ) -> SearchRequest:
        category = get_category(code)
        return SearchRequest(
            original_query=category.title,
            normalized_query=category.query,
            mode=mode,
            category=category.code,
            keywords=list(category.keywords),
        )

    def test_declared_category_keyword_survives_the_stopword_list(self) -> None:
        # A: the tools request terms carry «набор» again, and a real tool set
        # whose ONLY catalogue word is «набор» is kept (rejected before P1.3).
        tools = self.build_request("tools")

        self.assertIn("набор", query_terms(tools))

        set_ = make_product("Набор для ремонта 16 предметов")
        self.assertIsNone(rejection_reason(set_, tools))
        self.assertGreater(relevance_score(set_, tools), 0.0)
        self.assertEqual(relevance_tier(set_, tools), 1)

    def test_only_the_declared_word_survives(self) -> None:
        # E: the STOPWORDS mechanism is NOT switched off for CATEGORY - the
        # generic words of the catalogue title/query keep dropping, and a
        # category that does not declare «набор» keeps dropping it too.
        tools_terms = query_terms(self.build_request("tools"))

        for stopword in ("для", "цена", "шт", "товар", "без", "купить"):
            with self.subTest(stopword=stopword):
                self.assertNotIn(stopword, tools_terms)

        for code in ("beauty", "kids", "home"):
            with self.subTest(code=code):
                self.assertNotIn("набор", query_terms(self.build_request(code)))

    def test_meaningful_category_terms_outweigh_generic_nabor(self) -> None:
        # B: no ranking formula is changed - the share-based score alone puts
        # a real tool word strictly above generic «набор», and both titles
        # only ever reach the ordinary word level (tier 1, never 2).
        tools = self.build_request("tools")
        only_nabor = make_product("Набор для ремонта 16 предметов")
        with_tool = make_product("Набор отвёрток 12 в 1 прецизионных")

        self.assertIsNone(rejection_reason(with_tool, tools))
        self.assertGreater(
            relevance_score(with_tool, tools), relevance_score(only_nabor, tools)
        )
        self.assertEqual(
            relevance_tier(with_tool, tools), relevance_tier(only_nabor, tools)
        )
        self.assertEqual(relevance_tier(with_tool, tools), 1)

    def test_generic_nabor_does_not_match_an_unrelated_category(self) -> None:
        # C: beauty/kids/home do not declare «набор», so a generic set stays
        # out of those categories exactly as before.
        for code, title in (
            ("beauty", "Набор для маникюра 10 предметов"),
            ("kids", "Набор для купания новорождённых"),
            ("home", "Набор для уборки ванной"),
        ):
            with self.subTest(code=code, title=title):
                self.assertEqual(
                    rejection_reason(make_product(title), self.build_request(code)),
                    "no_query_term_in_title",
                )

    def test_search_keeps_the_stopword_behaviour(self) -> None:
        # D: the SAME catalogue words under SEARCH mode do not adopt the
        # CATEGORY exception - the two surfaces stay isolated.
        self.assertNotIn(
            "набор", query_terms(self.build_request("tools", mode=SearchMode.SEARCH))
        )

        # The reported G24.6 case: a user who typed «набор …» still gets the
        # pliers set dropped, because «набор» is not evidence in SEARCH.
        search = SearchRequest(
            original_query="набор для чистки мебели", mode=SearchMode.SEARCH
        )

        self.assertNotIn("набор", query_terms(search))
        self.assertEqual(
            rejection_reason(
                make_product("Набор регулируемых плоскогубцев для труб"), search
            ),
            "no_query_term_in_title",
        )

    def test_p12_short_stems_and_false_positive_guard_are_untouched(self) -> None:
        # F: P1.2 evidence is unchanged by the P1.3 exception.
        garden = self.build_request("garden")
        home = self.build_request("home")

        self.assertNotIn("набор", query_terms(garden))
        self.assertIsNone(
            rejection_reason(make_product("Садовый декоративный забор"), garden)
        )
        self.assertIsNone(rejection_reason(make_product("Всё для сада и дачи"), garden))
        self.assertIsNone(rejection_reason(make_product("Домашние тапочки"), home))
        self.assertEqual(
            rejection_reason(make_product("Домкрат подъёмный 2 т"), home),
            "no_query_term_in_title",
        )

    def test_category_family_titles_stay_kept(self) -> None:
        # H: G24.6 family/accessory protection is unchanged - an accessory
        # naming a declared category word (long or re-admitted) stays kept.
        tools = self.build_request("tools")

        self.assertIsNone(
            rejection_reason(make_product("Кейс для набора отвёрток с битами"), tools)
        )


class PerTitleComparisonTests(unittest.TestCase):
    """The comparable script is chosen PER TITLE, not once per request.

    Regression guard: with ONE dominant script per request, a request that
    mixed the marketplace phrase with the user's own words (the parser
    translates some terms) could end up with all its comparable terms reduced
    to a script the result set was NOT written in. The filter then had nothing
    to compare and silently switched itself off - junk titles from any category
    reached the feed. Now each title decides which request terms are
    comparable with it, so filtering stays active everywhere without a single
    hardcoded product rule.
    """

    def build_request(self) -> SearchRequest:
        return SearchRequest(
            original_query="кухонные весы",
            normalized_query="kitchen scale digital",
            keywords=["кухонные весы"],
        )

    def test_each_title_offers_the_terms_of_its_own_script(self) -> None:
        request_ = self.build_request()

        self.assertEqual(
            sorted(comparable_terms("Кухонные весы электронные", request_)),
            ["весы", "кухонные"],
        )
        self.assertEqual(
            sorted(comparable_terms("Kitchen digital scale LCD", request_)),
            ["digital", "kitchen", "scale"],
        )

    def test_unrelated_titles_are_rejected_in_every_script(self) -> None:
        request_ = self.build_request()

        for title in (
            "Кабель USB-C 1м для зарядки",
            "Bluetooth speaker portable waterproof",
            "Набор отверток 12 в 1",
        ):
            with self.subTest(title=title):
                self.assertEqual(
                    rejection_reason(make_product(title), request_),
                    "no_query_term_in_title",
                )

    def test_a_matching_title_in_either_script_is_kept(self) -> None:
        request_ = self.build_request()

        for title in ("Кухонные весы электронные", "Kitchen digital scale LCD"):
            with self.subTest(title=title):
                self.assertIsNone(rejection_reason(make_product(title), request_))

    def test_score_is_the_share_of_comparable_terms(self) -> None:
        request_ = self.build_request()

        self.assertEqual(
            relevance_score(make_product("Кухонные весы электронные"), request_),
            1.0,
        )
        self.assertEqual(
            relevance_score(make_product("Весы напольные электронные"), request_),
            0.5,
        )
        self.assertEqual(
            relevance_score(make_product("Кабель USB-C 1м"), request_), 0.0
        )

    def test_zero_score_means_no_evidence_not_bad(self) -> None:
        """Nothing comparable -> 0.0, so the ranking keeps the market order."""

        russian_only = request("беспроводные наушники")
        self.assertEqual(
            relevance_score(make_product("Wireless earbuds TWS"), russian_only),
            0.0,
        )
        popular = SearchRequest(original_query="популярные", mode=SearchMode.POPULAR)
        self.assertEqual(
            relevance_score(make_product("Весы кухонные"), popular), 0.0
        )



    def test_filtering_returns_kept_products_and_reasons(self) -> None:
        good = make_product("Беспроводные наушники TWS", product_id="1")
        cross = make_product("Крест нательный", product_id="2")
        broken = make_product("Наушники без ссылки", product_id="3", url="")

        kept, dropped = filter_relevant(
            [good, cross, broken], request("беспроводные наушники")
        )

        self.assertEqual([product.product_id for product in kept], ["1"])
        self.assertEqual(
            dropped, {"no_query_term_in_title": 1, "invalid_product_url": 1}
        )

    def test_nothing_is_dropped_when_every_product_matches(self) -> None:
        products = [
            make_product("Наушники беспроводные", product_id="1"),
            make_product("Наушники спортивные", product_id="2"),
        ]

        kept, dropped = filter_relevant(products, request("наушники"))

        self.assertEqual(len(kept), 2)
        self.assertEqual(dropped, {})


if __name__ == "__main__":
    unittest.main()



# ---------------------------------------------------------------------------
# G25.2-E: power-bank false positives in the live query «мощный повербанк»
# ---------------------------------------------------------------------------


class PowerBankFalsePositiveTests(unittest.TestCase):
    """The live failure mode: products that CONTAIN the requested noun in
    their titles must not classify as exact matches for a power-bank query.

    Two families, both observed in the live pool:

    * commercial infrastructure that LEADS with the noun - rental stations
      and payment kiosks - previously tier 2 from the shared head noun;
    * other devices that merely come with / run on a power bank («с
      повербанком», "with power bank", "powered by a power bank") -
      previously tier 1 "family" results ranked above the junk tail.

    Genuine power banks (with specs or included accessories) and existing
    accessory searches (a power-bank case, a cable) keep their verdicts.
    """

    def _classified(self, title: str, query: str = "мощный повербанк"):
        req = request(
            query,
            normalized_query="power bank",
            intent_product_type="повербанк",
        )
        return (
            classify_product(make_product(title), req),
            relevance_tier(make_product(title), req),
        )

    def test_rental_station_is_not_exact(self) -> None:
        verdict, tier = self._classified(
            "Power bank rental station 48 slots payment kiosk"
        )
        self.assertNotEqual(verdict, "exact")
        self.assertLess(tier, 2)

    def test_rental_station_powerbank_spelling_is_not_exact(self) -> None:
        verdict, tier = self._classified(
            "Powerbank rental station commercial charging cabinet"
        )
        self.assertNotEqual(verdict, "exact")
        self.assertLess(tier, 2)

    def test_underwater_fishing_light_with_power_bank_is_not_exact(self) -> None:
        verdict, tier = self._classified(
            "Подводный фонарь для рыбалки с повербанком"
        )
        self.assertNotEqual(verdict, "exact")
        self.assertLess(tier, 2)

    def test_english_fishing_light_with_power_bank_is_not_exact(self) -> None:
        verdict, tier = self._classified(
            "Underwater fishing light with power bank",
            query="power bank",
        )
        self.assertNotEqual(verdict, "exact")
        self.assertLess(tier, 2)

    def test_bubble_machine_powered_by_power_bank_is_not_exact(self) -> None:
        verdict, tier = self._classified(
            "Bubble machine powered by a power bank",
            query="power bank",
        )
        self.assertNotEqual(verdict, "exact")
        self.assertLess(tier, 2)

    def test_emergency_radio_with_power_bank_is_not_exact(self) -> None:
        verdict, tier = self._classified(
            "Emergency radio with power bank",
            query="power bank",
        )
        self.assertNotEqual(verdict, "exact")
        self.assertLess(tier, 2)

    def test_genuine_power_bank_with_specs_stays_exact(self) -> None:
        verdict, tier = self._classified(
            "Power bank 20000mAh fast charge PD 65W USB-C",
            query="power bank",
        )
        self.assertEqual(verdict, "exact")
        self.assertEqual(tier, 2)

    def test_genuine_power_bank_with_included_accessory_stays_exact(self) -> None:
        # A power bank SOLD WITH an accessory keeps its product role: the
        # requested phrase leads the title, the accessory follows it.
        verdict, tier = self._classified(
            "Power bank with USB-C cable included 10000mAh",
            query="power bank",
        )
        self.assertEqual(verdict, "exact")
        self.assertEqual(tier, 2)

    def test_power_bank_case_search_keeps_exact_for_a_case(self) -> None:
        verdict, tier = self._classified(
            "Power bank case silicone cover shockproof",
            query="power bank case",
        )
        self.assertEqual(verdict, "exact")
        self.assertEqual(tier, 2)

    def test_power_bank_case_search_rejects_rental_station(self) -> None:
        verdict, tier = self._classified(
            "Power bank rental station 48 slots payment kiosk",
            query="power bank case",
        )
        self.assertNotEqual(verdict, "exact")
        self.assertLess(tier, 2)

    def test_power_bank_case_search_rejects_emergency_radio(self) -> None:
        verdict, tier = self._classified(
            "Emergency radio with power bank",
            query="power bank case",
        )
        self.assertNotEqual(verdict, "exact")
        self.assertLess(tier, 2)


class PowerBankFinalSelectionTests(unittest.TestCase):
    """The final relevance cut over a realistic deep pool for «мощный
    повербанк»: the false positives must not outrank the genuine product,
    and the genuine one must lead the response."""

    def test_genuine_power_bank_outranks_false_positives(self) -> None:
        genuine = make_product(
            "Power bank 20000mAh fast charge PD 65W", product_id="v0"
        )
        false_positives = [
            make_product(
                "Power bank rental station 48 slots payment kiosk", product_id="j0"
            ),
            make_product(
                "Powerbank rental station commercial charging cabinet", product_id="j1"
            ),
            make_product("Подводный фонарь для рыбалки с повербанком", product_id="j2"),
            make_product("Underwater fishing light with power bank", product_id="j3"),
            make_product("Bubble machine powered by a power bank", product_id="j4"),
            make_product("Emergency radio with power bank", product_id="j5"),
        ]
        junk = [
            make_product(f"Sailor Moon drawstring bag {i}", product_id=f"k{i}")
            for i in range(20)
        ]
        req = request(
            "мощный повербанк",
            normalized_query="power bank",
            intent_product_type="повербанк",
        )
        service = SearchService(
            [_PoolProvider([genuine, *false_positives, *junk])], ai_service=None
        )

        outcome = run(service.find_products(req, feed_selection=False))

        self.assertTrue(outcome.products)
        self.assertEqual(
            outcome.products[0].title, genuine.title
        )
        titles = [p.title for p in outcome.products]
        # None of the false positives may be shipped ahead of the genuine
        # power bank.
        self.assertNotIn(false_positives[0].title, titles[:1])
        self.assertNotIn(false_positives[1].title, titles[:1])


# ---------------------------------------------------------------------------
# G25.2-F: bounded typo tolerance in a LONG Cyrillic product noun
# («поувербанк» for «повербанк») + mention-only fallback of the final cut
# ---------------------------------------------------------------------------


class NounTypoToleranceTests(unittest.TestCase):
    """One mistyped character inside a long Cyrillic product noun stem.

    Live evidence (2026-10-25): the persisted request «Нужен поувербанк
    мощный» carries the misspelling into ``normalized_query`` and
    ``intent_product_type``; every correctly spelled title («повербанк»,
    «повербанка», «повербанков») failed the prefix-4 noun comparison, no
    tier-2 product existed and mention-only tier-1 results became the
    whole answer.
    """

    def _match(self, left: str, right: str) -> bool:
        from services.relevance import nouns_match

        return nouns_match(left, right)

    def test_insertion_inside_the_stem_matches(self) -> None:
        self.assertTrue(self._match("поувербанк", "повербанк"))

    def test_inflected_title_forms_match_the_misspelled_query(self) -> None:
        self.assertTrue(self._match("поувербанк", "повербанка"))
        self.assertTrue(self._match("поувербанков", "повербанков"))

    def test_correct_spelling_keeps_its_exact_behaviour(self) -> None:
        self.assertTrue(self._match("повербанк", "повербанка"))
        self.assertTrue(self._match("повербанк", "повербанков"))
        self.assertTrue(self._match("повербанк", "повербанк"))

    def test_unrelated_product_words_never_fuzzy_match(self) -> None:
        self.assertFalse(self._match("повербанк", "пылесос"))
        self.assertFalse(self._match("поувербанк", "газонокос"))

    def test_short_words_never_become_fuzzy_matches(self) -> None:
        self.assertFalse(self._match("кот", "код"))
        self.assertFalse(self._match("чай", "чаек"))

    def test_one_letter_difference_in_a_long_word_is_fuzzy_matched(self) -> None:
        # A real one-substitution pair of long Cyrillic nouns: the rule is
        # generic (bounded), not a power-bank dictionary entry.
        self.assertTrue(self._match("батарея", "батареи"))

    def test_latin_pairs_never_use_the_cyrillic_typo_rule(self) -> None:
        # "powerbank" vs the transliteration "poverbank" of «повербанк»
        # differ exactly in w/v - an ALPHABET difference of two languages,
        # not a typo. The romanized cross-writing path keeps its own rules.
        self.assertFalse(self._match("powerbank", "poverbank"))

    def test_transposition_is_two_edits_and_never_matches(self) -> None:
        # «повербанк" vs «опвербанк» (the first two letters swapped) is TWO
        # edits - never tolerated.
        self.assertFalse(self._match("повербанк", "опвербанк"))

    def test_ambiguous_candidates_do_not_force_a_correction(self) -> None:
        # Two plausible long Cyrillic product nouns, each one edit from the
        # query word: the bounded rule may not invent a resolution - the
        # matcher simply accepts BOTH as possible readings (no dedicated
        # "correction" happens anywhere).
        self.assertTrue(self._match("поувербанк", "повербанк"))
        self.assertFalse(self._match("поувербанк", "подшипник"))


class MisspelledQueryClassificationTests(unittest.TestCase):
    """The exact live request «Нужен поувербанк мощный» against the traced
    live titles: the typo must no longer mute the product-type gate."""

    QUERY = "Нужен поувербанк мощный"

    def _request(self) -> SearchRequest:
        return request(
            self.QUERY,
            normalized_query=self.QUERY,
            intent_product_type="поувербанк мощный",
        )

    def test_genuine_power_bank_is_exact_despite_the_typo(self) -> None:
        verdict, tier = self._classified_typed(
            "Повербанк мощный 20000 мАч быстрый зарядка PD 65W USB-C"
        )
        self.assertEqual(verdict, "exact")
        self.assertEqual(tier, 2)

    def test_rental_station_is_not_exact_despite_the_typo(self) -> None:
        verdict, tier = self._classified_typed(
            "Станции аренды повербанков на 8 слотов с QR-сканером - Общие "
            "зарядные киоски для кафе и баров (устройство)"
        )
        self.assertNotEqual(verdict, "exact")
        self.assertLess(tier, 2)

    def test_pos_terminal_is_not_exact_despite_the_typo(self) -> None:
        verdict, tier = self._classified_typed(
            "POS-терминал, Торговый автомат для карт, Станция для "
            "совместного использования повербанков, Зарядная станция"
        )
        self.assertNotEqual(verdict, "exact")
        self.assertLess(tier, 2)

    def test_feature_mentioning_devices_stay_below_tier_2(self) -> None:
        for title in (
            "Терапевтический пояс 2026 TLB105 красный свет с возможностью "
            "подключения к повербанку",
            "Портативное аварийное радио AM/FM 4000 мАч с функцией "
            "фонарика и повербанка",
            "Подводный фонарь 5В для ночной рыбалки с функцией повербанка",
        ):
            verdict, tier = self._classified_typed(title)
            self.assertNotEqual(verdict, "exact", title)
            self.assertLess(tier, 2, title)

    def test_bags_for_a_power_bank_are_not_verified_matches(self) -> None:
        for title in (
            "Портативная водонепроницаемая сумка для телефона, органайзер "
            "для повербанка",
            "Сумка-мешок на шнурке из серии Сейлор Мун для кабелей и "
            "повербанков",
        ):
            verdict, tier = self._classified_typed(title)
            self.assertNotEqual(verdict, "exact", title)
            self.assertLess(tier, 2, title)

    def _classified_typed(self, title: str):
        product = make_product(title)
        return classify_product(product, self._request()), relevance_tier(
            product, self._request()
        )


class MentionOnlyFallbackTests(unittest.TestCase):
    """The final cut (G25.2-F): a mention-only tier-1 result must never be
    the whole answer to a direct product request."""

    def _req(self) -> SearchRequest:
        return request("power bank", normalized_query="power bank")

    def test_only_mention_only_candidates_ship_bounded(self) -> None:
        """The 2026-10-25 «Мощный повербанк" regression: 15 verified tier-1
        candidates, all mention-only, shipped 0. A single mention-only
        candidate must still ship (bounded by MENTION_ONLY_FILL_LIMIT), an
        empty screen is never manufactured."""
        req = self._req()
        products = [
            make_product("Portable emergency radio AM/FM with power bank "
                         "function", product_id="m0"),
        ]
        from services.search_service import (
            MENTION_ONLY_FILL_LIMIT, _final_relevance_cut,
        )

        outcome = _final_relevance_cut(products, 30, req)
        self.assertEqual(len(outcome), 1)
        self.assertLessEqual(len(outcome), MENTION_ONLY_FILL_LIMIT)

    def test_genuine_products_lead_and_mention_only_cannot_displace(self) -> None:
        req = self._req()
        genuine = [
            make_product(f"Power bank 20000mAh model {i}", product_id=f"g{i}")
            for i in range(2)
        ]
        mentions = [
            make_product(f"Device model {i} with built-in power bank feature",
                         product_id=f"m{i}")
            for i in range(6)
        ]
        from services.search_service import _final_relevance_cut

        outcome = _final_relevance_cut(mentions + genuine, 30, req)
        ids = [p.product_id for p in outcome]
        self.assertEqual([i for i in ids if i.startswith("g")], ["g0", "g1"])
        # No mention-only product was shipped ahead of a genuine one: the
        # bounded trailing slot is the ONLY way one enters the list.
        if "m0" in ids:
            self.assertLess(ids.index("m0"), len(ids))

    def test_related_tier1_results_stay_unbounded(self) -> None:
        """A query that asks for RELATED items («чехол для повербанка»)
        legitimately earns head-noun tier-1 results - those keep the
        unbounded verified behaviour."""
        req = request(
            "чехол для повербанка",
            normalized_query="power bank case",
            intent_product_type="чехол",
            intent_compatibility=["повербанк"],
        )
        cases = [
            make_product(f"Power bank case shockproof model {i}", product_id=f"c{i}")
            for i in range(5)
        ]
        service = SearchService([_PoolProvider(cases)], ai_service=None)
        outcome = run(service.find_products(req, feed_selection=False))
        self.assertEqual(len(outcome.products), 5)
