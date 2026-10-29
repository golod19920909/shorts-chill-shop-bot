"""Word-aware feature matching and the feature filter built on it.

Nothing here touches the network: the matcher is pure Python and the
filter is called with plain ``Product`` objects.
"""

from __future__ import annotations

import unittest

from models.product import Marketplace, Product
from models.search_request import SearchRequest
from services.comparison_service import filter_products, sort_products
from services.text_matching import (
    can_ever_match,
    is_context_clause,
    is_design_clause,
    matching_feature,
    normalize_phrase,
    phrase_in_text,
    script_of,
)


def make_product(title: str, description: str | None = None) -> Product:
    return Product(
        marketplace=Marketplace.ALIEXPRESS,
        product_id=title[:20],
        title=title,
        product_url="https://www.aliexpress.com/item/1.html",
        description=description,
        price=10.0,
        currency="USD",
    )


class PhraseMatchingTests(unittest.TestCase):
    def test_a_word_never_matches_inside_another_word(self) -> None:
        # The regression this module exists for: "проводные" must not match
        # "беспроводные", which plain substring tests did.
        self.assertFalse(phrase_in_text("Наушники беспроводные", "проводные"))
        self.assertFalse(phrase_in_text("Беспроводная гарнитура", "проводная"))
        self.assertTrue(phrase_in_text("Наушники проводные", "проводные"))

    def test_legitimate_matches_still_work(self) -> None:
        self.assertTrue(phrase_in_text("Наушники проводные с микрофоном", "проводные"))
        self.assertTrue(phrase_in_text("Чехол для iPhone 15", "iphone"))
        self.assertTrue(phrase_in_text("Power bank 20000mah", "power bank"))

    def test_russian_endings_match_by_word_start_prefix(self) -> None:
        for title, feature in (
            ("Наушники с шумоподавлением", "шумоподавление"),
            ("Активное шумоподавление и ANC", "шумоподавление"),
            ("Камера с ночным видением", "ночное видение"),
        ):
            with self.subTest(title=title, feature=feature):
                self.assertTrue(phrase_in_text(title, feature))

    def test_case_and_punctuation_do_not_matter(self) -> None:
        self.assertTrue(phrase_in_text("НАУШНИКИ, ПРОВОДНЫЕ!", "проводные"))
        self.assertTrue(phrase_in_text("usb-c кабель", "USB-C"))
        self.assertEqual(
            normalize_phrase("  С, Хорошим   Микрофоном! "),
            ["с", "хорошим", "микрофоном"],
        )

    def test_an_empty_feature_never_matches(self) -> None:
        self.assertFalse(phrase_in_text("Наушники", ""))
        self.assertFalse(phrase_in_text("Наушники", "   "))
        self.assertFalse(phrase_in_text(None, "наушники"))

    def test_multiword_phrases_require_every_word(self) -> None:
        self.assertTrue(phrase_in_text("Камера с ночным видением", "ночным видением"))
        self.assertFalse(phrase_in_text("Камера с видением", "ночным видением"))

    def test_matching_feature_reports_which_feature_matched(self) -> None:
        self.assertEqual(
            matching_feature("Наушники проводные", ["микрофон", "проводные"]),
            "проводные",
        )
        self.assertIsNone(
            matching_feature("Наушники беспроводные", ["микрофон", "проводные"])
        )



class CrossLanguageFeatureTests(unittest.TestCase):
    """A feature in another language is NOT evidence that nothing exists.

    Reproduced live: the query "Подарок до 34 летней жены" produced the
    catalog query "gift for wife"; the model's Russian characteristics
    ("для жены", "на 34 года") then matched none of the 50 English
    listings, and every one of them was dropped - the user saw an empty page
    built from real, relevant products.
    """

    def _listing(self, product_id: str, title: str) -> Product:
        return Product(
            marketplace=Marketplace.ALIEXPRESS,
            product_id=product_id,
            title=title,
            product_url=f"https://www.aliexpress.com/item/{product_id}.html",
            price=12.0,
            currency="USD",
        )

    def _english_listings(self) -> list[Product]:
        return [
            self._listing(f"en{index}", "Gift for Wife Love Heart Gift Set")
            for index in range(50)
        ]

    def test_a_foreign_language_feature_does_not_empty_the_result(self) -> None:
        request = SearchRequest(
            original_query="Подарок до 34 летней жены",
            normalized_query="gift for wife",
            required_features=["для жены", "на 34 года"],
        )

        outcome = filter_products(self._english_listings(), request)

        self.assertEqual(len(outcome), 50)
        self.assertEqual(request.ignored_features, ["для жены", "на 34 года"])

    def test_a_genuine_miss_in_the_same_language_still_filters(self) -> None:
        # The opposite case must NOT be weakened: a Russian feature that the
        # Russian listings really lack is a true "no such product".
        request = SearchRequest(
            original_query="наушники с шумоподавлением",
            normalized_query="Наушники беспроводные",
            required_features=["шумоподавление"],
        )
        russian = [self._listing("ru1", "Наушники беспроводные накладные")]

        self.assertEqual(filter_products(russian, request), [])
        self.assertEqual(request.ignored_features, [])

    def test_an_applicable_feature_keeps_filtering(self) -> None:
        request = SearchRequest(
            original_query="wireless headphones",
            normalized_query="Headphones",
            required_features=["wireless"],
        )
        english = [
            self._listing("a", "Wireless Headphones Bluetooth"),
            self._listing("b", "Headphone Stand Holder"),
        ]

        kept = filter_products(english, request)

        self.assertEqual([p.product_id for p in kept], ["a"])
        self.assertEqual(request.ignored_features, [])

    def test_mixed_language_candidates_keep_applying_the_feature(self) -> None:
        # As soon as ONE candidate carries the feature's language the feature
        # is applied normally: the check is about the result set, not about a
        # one-off guess.
        request = SearchRequest(original_query="x", required_features=["беспроводные"])
        mixed = [
            self._listing("en", "Wireless Headphones"),
            self._listing("ru", "Наушники беспроводные"),
        ]

        kept = filter_products(mixed, request)

        self.assertEqual([p.product_id for p in kept], ["ru"])
        self.assertEqual(request.ignored_features, [])

    def test_excluded_features_are_unaffected_by_the_language_check(self) -> None:
        # An exclusion in a foreign language simply matches nothing, which is
        # already the desired outcome and must not be reported as ignored.
        request = SearchRequest(original_query="x", excluded_features=["проводные"])

        kept = filter_products(self._english_listings(), request)

        self.assertEqual(len(kept), 50)
        self.assertEqual(request.ignored_features, [])


class ScriptDetectionTests(unittest.TestCase):
    def test_scripts_are_told_apart(self) -> None:
        self.assertEqual(script_of("наушники"), "cyrillic")
        self.assertEqual(script_of("Headphones"), "latin")
        self.assertIsNone(script_of("24 1"))
        self.assertIsNone(script_of(""))

    def test_a_phrase_can_be_compared_against_a_result_set(self) -> None:
        self.assertTrue(can_ever_match("микрофон", ["Наушники с микрофоном"]))
        self.assertFalse(can_ever_match("микрофон", ["Wireless Headphones"]))
        # No letters at all: nothing to translate, so never "inapplicable".


class ContextClauseTests(unittest.TestCase):
    """A context-of-use phrase is INTENT, never a literal inclusion filter.

    Regression (found live): the parsed features of «Подарок до 34 летней
    жены» («для жены», «на 34 года») were enforced verbatim, and a result set
    that worded the same intention in its own words («Подарок женщине»,
    «подарочный набор») was emptied - even though every one of those listings
    was the product the user asked for.
    """

    def _listing(self, product_id: str, title: str) -> Product:
        return Product(
            marketplace=Marketplace.ALIEXPRESS,
            product_id=product_id,
            title=title,
            product_url=f"https://www.aliexpress.com/item/{product_id}.html",
            price=9.0,
            currency="USD",
        )

    def test_gift_and_recipient_phrases_are_context_clauses(self) -> None:
        for phrase in (
            "для подарка",
            "на подарок",
            "в подарок",
            "для жены",
            "подарок для жены",
            "для мужа",
            "для женщины",
            "для девушки",
            "для детей",
            "для мамы",
            "для коллег",
            "на день рождения",
            "на новый год",
            "на юбилей",
            "as a gift",
            "for gift",
            "gift for wife",
            "for husband",
            "for kids",
            "for birthday",
        ):
            with self.subTest(phrase=phrase):
                self.assertTrue(is_context_clause(phrase))

    def test_literal_attributes_are_not_context_clauses(self) -> None:
        for phrase in (
            "титановый",
            "для iPhone 15",
            "с микрофоном",
            "для дома",
            "для авто",
            "для кухни",
            "для работы",
            "на 34 года",
            "напольная лампа",
            "wireless",
        ):
            with self.subTest(phrase=phrase):
                self.assertFalse(is_context_clause(phrase))

    def test_an_empty_phrase_is_not_a_context_clause(self) -> None:
        for phrase in ("", None, "   "):
            with self.subTest(phrase=phrase):
                self.assertFalse(is_context_clause(phrase))

    def test_a_context_feature_does_not_empty_the_result_set(self) -> None:
        # No title repeats «для жены» - each words the same intention its own
        # way, and every one of them is the requested product.
        request = SearchRequest(
            original_query="подарок для жены",
            normalized_query="gift for wife",
            required_features=["для жены"],
        )
        listings = [
            self._listing("w1", "Подарок женщине набор косметики"),
            self._listing("w2", "Серьги женские с камнем, подарочный набор"),
            self._listing("w3", "Подарочный набор для женщин"),
        ]

        kept = filter_products(listings, request)

        self.assertEqual(len(kept), 3)
        self.assertEqual(request.ignored_features, ["для жены"])

    def test_a_literal_feature_still_filters_alongside_a_context_ignored_one(
        self,
    ) -> None:
        request = SearchRequest(
            original_query="титановый брелок для жены",
            normalized_query="titanium keychain",
            required_features=["титановый", "для жены"],
        )
        listings = [
            self._listing("a", "Брелок молоток титановый сплав"),
            self._listing("b", "Брелок отвёртка стальная"),
        ]

        kept = filter_products(listings, request)

        # The context phrase is ignored, the literal material still filters.
        self.assertEqual([p.product_id for p in kept], ["a"])
        self.assertEqual(request.ignored_features, ["для жены"])

    def test_the_context_wording_is_a_ranking_bonus_inside_one_tier(self) -> None:
        # Both listings carry every word of the request, so the relevance tier
        # and the query-word share are identical: only the wording of the
        # user's INTENT separates them («для жены» is never a filter, but a
        # title that words it is the better answer).
        request = SearchRequest(
            original_query="брелок подарочный",
            normalized_query="брелок подарочный",
            required_features=["для жены"],
        )
        with_wording = self._listing("w", "Брелок подарочный для жены")
        without = self._listing("p", "Брелок подарочный набор")

        ordered = sort_products([without, with_wording], request)

        self.assertEqual([p.product_id for p in ordered], ["w", "p"])
        # ...and the same bonus never removes anything.
        self.assertEqual(len(ordered), 2)

    def test_the_context_bonus_never_outranks_the_requested_product(self) -> None:
        request = SearchRequest(
            original_query="брелок подарочный",
            normalized_query="брелок подарочный",
            required_features=["для жены"],
        )
        unrelated = self._listing("x", "Настольная лампа для жены")
        relevant = self._listing("r", "Брелок подарочный набор")

        ordered = sort_products([unrelated, relevant], request)

        self.assertEqual([p.product_id for p in ordered], ["r", "x"])


class DesignClauseTests(unittest.TestCase):
    """Shape/style phrases are intent, never literal inclusion filters.

    Regression (found live): the parsed feature «в виде строительного
    инструмента» was enforced verbatim against AliExpress titles and kept
    1 of 24 relevant real keychains - a page of exactly the products the
    user asked for was almost emptied by the modifier describing their
    design. The phrase class is general, never query-specific.
    """

    def _listing(self, product_id: str, title: str) -> Product:
        return Product(
            marketplace=Marketplace.ALIEXPRESS,
            product_id=product_id,
            title=title,
            product_url=f"https://www.aliexpress.com/item/{product_id}.html",
            price=8.0,
            currency="USD",
        )

    def test_shape_and_style_constructions_are_design_clauses(self) -> None:
        for phrase in (
            "в виде строительного инструмента",
            "брелок в форме сердца",
            "в стиле ретро",
            "похожий на кота",
            "напоминающий робота",
            "shaped like a robot",
            "in the shape of a heart",
            "in the style of retro",
            "resembling a hammer",
        ):
            with self.subTest(phrase=phrase):
                self.assertTrue(is_design_clause(phrase))

    def test_literal_attributes_are_not_design_clauses(self) -> None:
        for phrase in (
            "титановый",
            "для iPhone 15",
            "с микрофоном",
            "шумоподавление",
            "проводные",
            "для жены",
            "на 34 года",
            "напольная лампа",
            "беспроводные",
        ):
            with self.subTest(phrase=phrase):
                self.assertFalse(is_design_clause(phrase))

    def test_an_empty_phrase_is_not_a_design_clause(self) -> None:
        self.assertFalse(is_design_clause(""))
        self.assertFalse(is_design_clause(None))
        self.assertFalse(is_design_clause("   "))

    def test_design_feature_does_not_empty_the_result_set(self) -> None:
        # Titles name the shape with their own words; none of them repeats
        # the user's construction verbatim - all must survive.
        request = SearchRequest(
            original_query="брелок в виде строительного инструмента",
            normalized_query="construction tool keychain",
            required_features=["в виде строительного инструмента"],
        )
        listings = [
            self._listing("k1", "Ключница молоток, брелок строительный"),
            self._listing("k2", "Брелок отвёртка мини-инструмент подарок"),
            self._listing("k3", "12 шт. брелоков строительная техника"),
        ]

        kept = filter_products(listings, request)

        self.assertEqual(len(kept), 3)
        self.assertEqual(
            request.ignored_features, ["в виде строительного инструмента"]
        )

    def test_a_literal_feature_still_filters_alongside_a_design_ignored_one(self) -> None:
        request = SearchRequest(
            original_query="брелок в виде инструмента из титана",
            normalized_query="tool keychain",
            required_features=["в виде инструмента", "титановый"],
        )
        listings = [
            self._listing("a", "Брелок молоток титановый сплав"),
            self._listing("b", "Брелок отвёртка стальная"),
        ]

        kept = filter_products(listings, request)

        # The design phrase is ignored, the literal material still filters.
        self.assertEqual([p.product_id for p in kept], ["a"])
        self.assertEqual(request.ignored_features, ["в виде инструмента"])

        self.assertTrue(can_ever_match("24 1", ["Wireless Headphones"]))
