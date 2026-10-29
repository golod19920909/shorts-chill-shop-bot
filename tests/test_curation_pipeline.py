"""Tests for ``curation.pipeline``: the quality gate and the family dedup.

The layer must show FEWER, BETTER listings without ever inventing one:

- a candidate that cannot be shown honestly (no id, no title, no real price)
  is dropped and the reason names the offending field;
- reworded twins of ONE product family become ONE card - the family's best
  offer, judged by the marketplace's own signals;
- genuinely different products survive, however similar their titles sound.

Everything here is hand-written data: no network, no marketplace, no model.
"""

from __future__ import annotations

import copy
import unittest
from collections.abc import Sequence
from types import SimpleNamespace

from curation import (
    DEFAULT_MODE,
    FRESH_ENOUGH,
    KNOWN_MODES,
    MAX_PRICE_USD,
    MIN_PRICE_USD,
    MODE_CATEGORY,
    MODE_DEALS,
    MODE_POPULAR,
    MODE_RECOMMENDATION,
    MODE_SEARCH,
    QUALITY_PROFILES,
    REQUIRED_FIELDS,
    SIMILARITY_THRESHOLD,
    CurationPipeline,
    DeduplicationFilter,
    DiversityFilter,
    HistoryFilter,
    ModeRanker,
    QualityFilter,
)
from curation.pipeline import (
    DIVERSITY_BLOCK,
    MAX_PER_TYPE,
    MIN_TOKEN_LENGTH,
    REASON_MALFORMED,
    REASON_ORDERS,
    REASON_PRICE,
    REASON_PRICE_VALUE,
    REASON_PRODUCT_ID,
    REASON_RATING,
    REASON_TITLE,
    _TAXONOMY_LABEL_RELATIONAL_WORDS,
    discount_percent,
    known_signals,
    normalize_mode,
    quality_mode,
    recommendation_score,
    similarity,
    title_tokens,
)
from models.product import Marketplace

#: The example from the curation spec: reordered words, one product family.
TWIN_TITLE = "Механическая клавиатура RGB Red Switch"
TWIN_TITLE_REORDERED = "RGB Механическая клавиатура Red Switch"


def candidate(
    product_id: str = "1005001",
    *,
    title: str = TWIN_TITLE,
    price: float | None = 42.0,
    orders: int | None = None,
    rating: float | None = None,
    **extra: object,
) -> dict:
    """A provider-shaped candidate; optional signals are omitted when ``None``.

    Omitting (rather than nulling) is what a real provider response looks like,
    and the two cases are deliberately different to the filter.
    """

    listing: dict = {"product_id": product_id, "title": title, "price": price}
    if orders is not None:
        listing["orders"] = orders
    if rating is not None:
        listing["rating"] = rating
    listing.update(extra)
    return listing


def without(field_name: str, **kwargs: object) -> dict:
    """A valid candidate missing one field ENTIRELY (absent, not ``None``)."""

    listing = candidate(**kwargs)
    listing.pop(field_name)
    return listing


class QualityFilterTests(unittest.TestCase):
    """Stage 1: the honest-showing gate."""

    def setUp(self) -> None:
        self.filter = QualityFilter()

    def test_a_complete_candidate_passes(self) -> None:
        listing = candidate()

        self.assertTrue(self.filter.is_valid(listing))
        self.assertIsNone(self.filter.reason(listing))

    def test_the_required_fields_are_the_published_contract(self) -> None:
        self.assertEqual(REQUIRED_FIELDS, ("product_id", "title", "price"))
        self.assertEqual(self.filter.required_fields, REQUIRED_FIELDS)

    def test_a_candidate_without_an_id_is_rejected(self) -> None:
        for listing in (
            without("product_id"),
            candidate(product_id=""),
            candidate(product_id="   "),
            candidate(product_id=None),
        ):
            with self.subTest(listing=listing):
                self.assertFalse(self.filter.is_valid(listing))
                self.assertEqual(self.filter.reason(listing), REASON_PRODUCT_ID)

    def test_a_candidate_without_a_title_is_rejected(self) -> None:
        for listing in (
            without("title"),
            candidate(title=""),
            candidate(title="   "),
            candidate(title=None),
            candidate(title="!!!"),
        ):
            with self.subTest(listing=listing):
                self.assertEqual(self.filter.reason(listing), REASON_TITLE)

    def test_a_candidate_without_a_price_is_rejected(self) -> None:
        for listing in (without("price"), candidate(price=None)):
            with self.subTest(listing=listing):
                self.assertEqual(self.filter.reason(listing), REASON_PRICE)

    def test_impossible_prices_are_rejected(self) -> None:
        for price in (
            "дешёво",
            0,
            0.0,
            -1.0,
            float("nan"),
            float("inf"),
            True,
            "42,5",
        ):
            with self.subTest(price=price):
                self.assertEqual(
                    self.filter.reason(candidate(price=price)), REASON_PRICE_VALUE
                )

    def test_a_numeric_string_price_is_accepted(self) -> None:
        self.assertTrue(self.filter.is_valid(candidate(price="19.99")))

    def test_broken_optional_signals_are_rejected(self) -> None:
        for listing, reason in (
            (candidate(rating=9.0), REASON_RATING),
            (candidate(rating=-1.0), REASON_RATING),
            (candidate(rating="стабильно"), REASON_RATING),
            (candidate(orders=-5), REASON_ORDERS),
            (candidate(orders="много"), REASON_ORDERS),
        ):
            with self.subTest(listing=listing):
                self.assertEqual(self.filter.reason(listing), reason)

    def test_absent_optional_signals_are_perfectly_fine(self) -> None:
        # A marketplace that reports nothing extra still sells a real product,
        # and zero orders is a fact - not corruption.
        self.assertTrue(self.filter.is_valid(candidate()))
        self.assertTrue(self.filter.is_valid(candidate(orders=0, rating=0.0)))

    def test_provider_spellings_of_the_same_facts_are_read(self) -> None:
        listing = {"id": "1005001", "name": "Наушники", "min_price": 9.5}

        self.assertTrue(self.filter.is_valid(listing))

    def test_junk_that_is_not_a_candidate_is_rejected(self) -> None:
        for junk in (None, "товар", 42, 4.5, [], ()):
            with self.subTest(junk=junk):
                self.assertEqual(self.filter.reason(junk), REASON_MALFORMED)

    def test_a_model_object_is_read_too(self) -> None:
        product = SimpleNamespace(
            product_id="1005001", title="Наушники беспроводные", price=15.0
        )

        self.assertTrue(self.filter.is_valid(product))

    def test_survivors_keep_their_order(self) -> None:
        first = candidate("1")
        second = candidate("2")

        kept = self.filter.apply([first, without("price"), second, candidate(rating=9)])

        self.assertEqual(kept, [first, second])

    def test_empty_input(self) -> None:
        self.assertEqual(self.filter.apply([]), [])


class DeduplicationTests(unittest.TestCase):
    """Stage 2: one offer per product family."""

    def setUp(self) -> None:
        self.filter = DeduplicationFilter()

    def test_reworded_titles_become_one_family(self) -> None:
        first = candidate("1", title=TWIN_TITLE, orders=120)
        second = candidate("2", title=TWIN_TITLE_REORDERED, orders=900)

        kept = self.filter.apply([first, second])

        self.assertEqual(len(kept), 1)
        # The stronger offer represents the family...
        self.assertEqual(kept[0]["product_id"], "2")

    def test_the_most_ordered_offer_wins(self) -> None:
        weak = candidate("1", title=TWIN_TITLE, orders=3)
        strong = candidate("2", title=TWIN_TITLE_REORDERED, volume=700)

        self.assertIs(self.filter.best(weak, strong), strong)
        self.assertIs(self.filter.best(strong, weak), strong)

    def test_the_rating_decides_when_no_orders_are_known(self) -> None:
        plain = candidate("1", title="Наушники беспроводные Bluetooth", rating=4.2)
        praised = candidate("2", title="Беспроводные наушники Bluetooth", rating=4.9)

        kept = self.filter.apply([plain, praised])

        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["product_id"], "2")

    def test_a_tie_keeps_the_first_offer(self) -> None:
        first = candidate("1", title=TWIN_TITLE, orders=500)
        second = candidate("2", title=TWIN_TITLE_REORDERED, orders=500)

        kept = self.filter.apply([first, second])

        self.assertEqual(len(kept), 1)
        self.assertIs(kept[0], first)

    def test_the_family_keeps_the_slot_of_its_first_member(self) -> None:
        twin_a = candidate("1", title="Наушники беспроводные Bluetooth", orders=5)
        unrelated = candidate("2", title="Чехол силиконовый прозрачный iPhone", orders=1)
        twin_b = candidate("3", title="Беспроводные наушники Bluetooth", orders=99)

        kept = self.filter.apply([twin_a, unrelated, twin_b])

        self.assertEqual([item["product_id"] for item in kept], ["3", "2"])

    def test_markup_and_case_differences_are_one_family(self) -> None:
        marked = candidate("1", title="<b>RGB</b> механическая клавиатура")
        plain = candidate("2", title="Механическая клавиатура RGB")

        self.assertEqual(len(self.filter.apply([marked, plain])), 1)

    def test_titles_below_the_threshold_stay_separate_products(self) -> None:
        iphone = candidate("1", title="Чехол силиконовый прозрачный iPhone")
        samsung = candidate("2", title="Чехол силиконовый прозрачный Samsung")

        # 0.6 of the tokens are shared - close, but two different products.
        self.assertAlmostEqual(
            similarity(iphone["title"], samsung["title"]), 0.6, places=6
        )
        self.assertLess(
            similarity(iphone["title"], samsung["title"]), SIMILARITY_THRESHOLD
        )
        self.assertEqual(len(self.filter.apply([iphone, samsung])), 2)

    def test_products_of_different_categories_are_never_merged(self) -> None:
        products = [
            candidate("1", title=TWIN_TITLE),
            candidate("2", title="Чехол силиконовый прозрачный iPhone"),
            candidate("3", title="Наушники беспроводные Bluetooth"),
            candidate("4", title="Настольная лампа светодиодная"),
        ]

        kept = self.filter.apply(products)

        self.assertEqual([item["product_id"] for item in kept], ["1", "2", "3", "4"])

    def test_listings_without_comparable_tokens_are_kept_separately(self) -> None:
        short_a = candidate("1", title="TV HD")
        short_b = candidate("2", title="PC X")

        self.assertEqual(title_tokens(short_a["title"]), frozenset())
        self.assertEqual(len(self.filter.apply([short_a, short_b])), 2)

    def test_a_number_is_part_of_the_product_identity(self) -> None:
        # Live titles name the model/size with a number: two listings that
        # differ only in that number are TWO products, never one family. The
        # number token survives the filler filter for exactly this reason, and
        # a number MISMATCH vetoes similarity outright - the shared words
        # ("Чехол силиконовый для iPhone ...") would otherwise be enough to
        # merge them.
        models = [
            candidate("1", title="Чехол силиконовый для iPhone 13"),
            candidate("2", title="Чехол силиконовый для iPhone 14"),
        ]

        self.assertEqual(
            title_tokens(models[0]["title"]),
            frozenset({"чехол", "силиконовый", "для", "iphone", "13"}),
        )
        # The words alone would score 0.667 - above the threshold.
        self.assertGreater(
            len(title_tokens(models[0]["title"]) & title_tokens(models[1]["title"]))
            / len(title_tokens(models[0]["title"]) | title_tokens(models[1]["title"])),
            SIMILARITY_THRESHOLD,
        )
        self.assertEqual(similarity(models[0]["title"], models[1]["title"]), 0.0)
        self.assertEqual(
            [item["product_id"] for item in self.filter.apply(models)], ["1", "2"]
        )

    def test_a_numberless_title_makes_no_claim_about_numbers(self) -> None:
        # Only a title that STATES a number can disagree with another one: here
        # the ordinary token index decides (0.6 - below the threshold), so the
        # two listings stay separate and nothing is vetoed.
        with_number = candidate("1", title="Наушники беспроводные Bluetooth 5.3")
        without = candidate("2", title="Наушники беспроводные Bluetooth")

        self.assertAlmostEqual(
            similarity(with_number["title"], without["title"]), 0.6, places=6
        )
        self.assertEqual(len(self.filter.apply([with_number, without])), 2)

    def test_the_same_number_does_not_block_a_family(self) -> None:
        first = candidate("1", title="Чехол силиконовый для iPhone 13")
        second = candidate("2", title="Для iPhone 13 чехол силиконовый")

        self.assertEqual(len(self.filter.apply([first, second])), 1)

    def test_the_threshold_is_configurable(self) -> None:
        iphone = candidate("1", title="Чехол силиконовый прозрачный iPhone")
        samsung = candidate("2", title="Чехол силиконовый прозрачный Samsung")

        loose = DeduplicationFilter(threshold=0.5)

        self.assertEqual(len(self.filter.apply([iphone, samsung])), 2)
        self.assertEqual(len(loose.apply([iphone, samsung])), 1)

    def test_an_impossible_threshold_is_refused(self) -> None:
        for bad in (0.0, -0.1, 1.5):
            with self.subTest(threshold=bad):
                with self.assertRaises(ValueError):
                    DeduplicationFilter(bad)

    def test_empty_and_single_input(self) -> None:
        only = candidate("1")

        self.assertEqual(self.filter.apply([]), [])
        self.assertEqual(self.filter.apply([only]), [only])

    def test_a_bare_string_never_pollutes_a_family(self) -> None:
        # ``str`` really has a ``.title`` attribute: reading it would turn the
        # bound method's repr into fake tokens. A non-candidate must stay a
        # non-candidate, alone in its own slot.
        junk = "Механическая клавиатура RGB Red Switch"
        twin = candidate("2", title=TWIN_TITLE)

        self.assertEqual(self.filter.apply([junk, twin]), [junk, twin])


def mixed_batch() -> list[dict]:
    """One provider-style batch: twins, junk and genuinely different products.

    Order counts are healthy real-world numbers so the batch reads like a
    provider answer; the default chain judges the mechanics this batch is
    about - ids, prices, families (the direct-search profile has no order
    floor, see QUALITY_PROFILES).
    """

    return [
        candidate("1", title=TWIN_TITLE, orders=120),
        candidate("2", title=TWIN_TITLE_REORDERED, orders=900),
        candidate("", title="Клавиатура без ID", orders=150),
        {"product_id": "4", "title": "Клавиатура без цены"},
        candidate("5", title="Чехол силиконовый прозрачный iPhone", orders=180),
        candidate("6", title="Чехол силиконовый прозрачный Samsung", orders=200),
        candidate("7", title="Наушники беспроводные Bluetooth", orders=110),
    ]


class CurationPipelineTests(unittest.TestCase):
    """The whole chain over one batch of provider-shaped candidates."""

    def setUp(self) -> None:
        self.pipeline = CurationPipeline()

    def test_the_default_chain_is_the_documented_order(self) -> None:
        self.assertEqual(
            [stage.name for stage in self.pipeline.filters],
            ["quality", "deduplication", "history", "mode", "diversity"],
        )

    def test_quality_runs_first_so_a_broken_listing_cannot_win_a_family(self) -> None:
        honest = candidate("1", title=TWIN_TITLE, orders=150)
        # The corrupt twin has an enormous order count - and no price at all.
        corrupt = {
            "product_id": "2",
            "title": TWIN_TITLE_REORDERED,
            "orders": 999_999,
        }

        kept = self.pipeline.process([honest, corrupt])

        self.assertEqual(len(kept), 1)
        self.assertIs(kept[0], honest)

    def test_invalid_candidates_are_dropped_and_families_are_merged(self) -> None:
        batch = mixed_batch()

        kept = self.pipeline.process(batch)

        # 1 and 2 are one family (2 has more orders), 3/4 are broken, and
        # 5/6 sit below the similarity threshold, so both stay.
        self.assertEqual(
            [item["product_id"] for item in kept], ["2", "5", "6", "7"]
        )
        # Every returned object is one of the objects it received.
        for item in kept:
            self.assertTrue(any(item is original for original in batch))

    def test_unique_products_of_different_categories_survive_in_order(self) -> None:
        batch = [
            candidate("10", title=TWIN_TITLE),
            candidate("11", title="Чехол силиконовый прозрачный iPhone"),
            candidate("12", title="Наушники беспроводные Bluetooth"),
        ]

        kept = self.pipeline.process(batch)

        self.assertEqual([item["product_id"] for item in kept], ["10", "11", "12"])
        for item, original in zip(kept, batch):
            self.assertIs(item, original)

    def test_the_report_counts_every_stage(self) -> None:
        self.pipeline.process(mixed_batch())

        report = self.pipeline.last_report

        self.assertEqual(
            [stage.name for stage in report.stages],
            ["quality", "deduplication", "history", "mode", "diversity"],
        )
        # Two broken listings and one reworded twin leave a feed of four.
        self.assertEqual(report.stages[0].received, 7)
        self.assertEqual(report.stages[0].kept, 5)
        self.assertEqual(report.dropped("quality"), 2)
        self.assertEqual(report.stages[1].received, 5)
        self.assertEqual(report.stages[1].kept, 4)
        self.assertEqual(report.dropped("deduplication"), 1)
        # Without a history and without a stated relevance the last three
        # stages have nothing to remove: they may only reorder.
        for stage in report.stages[2:]:
            self.assertEqual(stage.received, 4)
            self.assertEqual(stage.kept, 4)
            self.assertEqual(stage.dropped, 0)
        self.assertEqual(report.kept, 4)
        self.assertEqual(report.dropped("no-such-stage"), 0)

    def test_a_custom_chain_can_be_passed(self) -> None:
        class FirstOnly:
            name = "first-only"

            def apply(self, candidates):
                return list(candidates)[:1]

        batch = mixed_batch()
        pipeline = CurationPipeline([FirstOnly()])

        self.assertEqual(pipeline.process(batch), [batch[0]])
        self.assertEqual(
            [stage.name for stage in pipeline.last_report.stages], ["first-only"]
        )

    def test_a_stage_without_a_name_is_reported_by_its_class(self) -> None:
        class Nameless:
            def apply(self, candidates):
                return list(candidates)

        pipeline = CurationPipeline([Nameless()])
        pipeline.process([candidate()])

        self.assertEqual(pipeline.last_report.stages[0].name, "Nameless")

    def test_model_objects_flow_through_the_whole_chain(self) -> None:
        first = SimpleNamespace(
            product_id="1",
            title=TWIN_TITLE,
            price=42.0,
            orders_count=100,
            rating=4.5,
        )
        second = SimpleNamespace(
            product_id="2",
            title=TWIN_TITLE_REORDERED,
            price=39.9,
            orders_count=900,
            rating=4.9,
        )

        kept = self.pipeline.process([first, second])

        self.assertEqual(len(kept), 1)
        self.assertIs(kept[0], second)

    def test_the_pipeline_is_deterministic_and_never_mutates_its_input(self) -> None:
        batch = mixed_batch()
        snapshot = copy.deepcopy(batch)

        first_run = self.pipeline.process(batch)
        second_run = self.pipeline.process(batch)

        self.assertEqual(
            [item["product_id"] for item in first_run],
            [item["product_id"] for item in second_run],
        )
        self.assertEqual(batch, snapshot)

    def test_empty_and_none_input(self) -> None:
        for raw in ([], None):
            with self.subTest(raw=raw):
                self.assertEqual(self.pipeline.process(raw), [])
                self.assertEqual(self.pipeline.last_report.kept, 0)
                self.assertEqual(
                    [stage.name for stage in self.pipeline.last_report.stages],
                    ["quality", "deduplication", "history", "mode", "diversity"],
                )


class DiversityFilterTests(unittest.TestCase):
    """Stage 5: no block of cards is one product type repeated."""

    KEYBOARDS = (
        "Механическая клавиатура RGB Red Switch",
        "Игровая клавиатура беспроводная с подсветкой",
        "Клавиатура механическая компактная 60%",
    )
    MICE = (
        "Беспроводная мышь для компьютера",
        "Игровая мышь оптическая USB",
        "Мышь вертикальная эргономичная",
    )
    HEADPHONES = (
        "Наушники беспроводные Bluetooth",
        "Наушники накладные с микрофоном",
        "Наушники проводные студийные",
    )

    def setUp(self) -> None:
        self.filter = DiversityFilter()

    @staticmethod
    def listings(titles: Sequence[str]) -> list[dict]:
        return [candidate(str(index), title=title) for index, title in enumerate(titles)]

    def three_types(self) -> list[dict]:
        return self.listings([*self.KEYBOARDS, *self.MICE, *self.HEADPHONES])

    def test_one_type_fills_at_most_two_slots_of_every_block(self) -> None:
        batch = self.three_types()
        labels = self.filter.labels(batch)

        kept = self.filter.apply(batch)
        types = {id(item): labels[index] for index, item in enumerate(batch)}

        self.assertEqual(len(kept), len(batch))
        for start in range(0, len(kept), DIVERSITY_BLOCK):
            block = [types[id(item)] for item in kept[start : start + DIVERSITY_BLOCK]]
            for label in set(block):
                self.assertLessEqual(
                    block.count(label),
                    MAX_PER_TYPE,
                    f"block at {start} repeats {label}: {block}",
                )

    def test_the_first_block_mixes_the_three_categories(self) -> None:
        kept = self.filter.apply(self.three_types())
        titles = [item["title"] for item in kept]

        self.assertEqual(titles[:2], list(self.KEYBOARDS[:2]))
        self.assertEqual(titles[2:4], list(self.MICE[:2]))
        self.assertEqual(titles[4:6], list(self.HEADPHONES[:2]))

    def test_reworded_titles_are_one_type(self) -> None:
        batch = self.listings([TWIN_TITLE, TWIN_TITLE_REORDERED])

        self.assertEqual(self.filter.labels(batch), ["механическая", "механическая"])

    def test_the_type_word_comes_from_the_batch_wording(self) -> None:
        batch = self.listings(
            [
                *self.KEYBOARDS,
                "Чехол силиконовый для телефона",
                "Чехол прозрачный для телефона",
            ]
        )

        labels = self.filter.labels(batch)

        # The word the BATCH repeats is the type: all three keyboards group
        # under it, the two cases under their own shared word, and the two
        # groups never mix.
        self.assertEqual(labels[0], "клавиатура")
        self.assertEqual(labels[1], "клавиатура")
        self.assertEqual(labels[3], labels[4])
        self.assertNotEqual(labels[0], labels[3])

    def test_nothing_is_lost_when_the_pool_holds_one_type_only(self) -> None:
        batch = self.listings(list(self.KEYBOARDS) * 3)

        kept = self.filter.apply(batch)

        self.assertEqual(len(kept), len(batch))
        self.assertEqual([item["product_id"] for item in kept[:2]], ["0", "1"])

    def test_a_title_without_comparable_words_is_never_limited(self) -> None:
        batch = self.listings(["TV HD", "PC X", "AB"])

        self.assertEqual(self.filter.labels(batch), [None, None, None])
        self.assertEqual(self.filter.apply(batch), batch)

    def test_the_block_size_and_the_limit_are_configurable(self) -> None:
        batch = self.listings(
            [
                "Механическая клавиатура RGB Red Switch",
                "Беспроводная мышь для компьютера",
                "Наушники беспроводные Bluetooth",
                "Настольная лампа светодиодная",
                "Бутылка для воды спортивная",
                "Рюкзак городской водостойкий",
            ]
        )
        strict = DiversityFilter(max_per_type=1, block_size=3)
        labels = strict.labels(batch)

        kept = strict.apply(batch)
        types = {id(item): labels[index] for index, item in enumerate(batch)}
        first_block = [types[id(item)] for item in kept[:3]]

        self.assertEqual(len(kept), len(batch))
        # With one slot per type, the first screen shows three different kinds.
        self.assertEqual(len(first_block), 3)
        self.assertEqual(len(set(first_block)), 3)

    def test_an_impossible_configuration_is_refused(self) -> None:
        for kwargs in ({"block_size": 0}, {"max_per_type": 0}, {"block_size": -3}):
            with self.subTest(**kwargs):
                with self.assertRaises(ValueError):
                    DiversityFilter(**kwargs)

    # --- G24.8 P1.6-FIX: the taxonomy-label candidacy filter ---------------

    #: The relational class the label filter must exclude (class-based, not
    #: a one-word patch for "для").
    RELATIONAL_PROBES = (
        "для",
        "без",
        "под",
        "над",
        "при",
        "через",
        "после",
        "между",
        "около",
        "внутри",
    )

    def test_the_shared_stream_still_carries_relational_words(self) -> None:
        # A: title_tokens() is the SHARED stream (similarity, dedup) and keeps
        # a 3-character token - MIN_TOKEN_LENGTH must not move for the label
        # fix, so "для" stays here forever.
        tokens = title_tokens("Чехол силиконовый для iPhone 13")

        self.assertEqual(MIN_TOKEN_LENGTH, 3)
        self.assertIn("для", tokens)

    def test_relational_word_is_never_selected_as_the_taxonomy_label(self) -> None:
        # B: "для" is the ONLY word these titles share - by raw frequency it
        # would win every label (the P1.6 production bug: one filler label for
        # the whole feed).
        batch = self.listings(
            [
                "Чехол для телефона",
                "Кабель для зарядки",
                "Держатель для велосипеда",
            ]
        )
        token_sets = [title_tokens(item["title"]) for item in batch]
        shared = set(token_sets[0]).intersection(*token_sets[1:])

        labels = self.filter.labels(batch)

        self.assertEqual(shared, {"для"})
        self.assertNotIn("для", labels)
        self.assertTrue(all(label is not None for label in labels))

    def test_the_relational_class_is_filtered_not_only_one_word(self) -> None:
        # C: the fix targets the grammatical CLASS of prepositions - any of
        # them dominating a pool must lose the label candidacy.
        for word in self.RELATIONAL_PROBES:
            with self.subTest(word=word):
                batch = self.listings(
                    [
                        f"Чехол {word} телефона",
                        f"Кабель {word} зарядки",
                        f"Держатель {word} велосипеда",
                    ]
                )

                labels = self.filter.labels(batch)

                self.assertIn(word, title_tokens(batch[0]["title"]))
                self.assertNotIn(word, labels)
                self.assertTrue(all(label is not None for label in labels))
    def test_technical_three_letter_tokens_stay_label_candidates(self) -> None:
        # D: USB / SSD / RGB share the 3-character class of the filtered
        # prepositions but are content words - they must still win labels.
        pools = {
            "usb": (
                "Кабель USB Type-C",
                "Переходник USB OTG",
                "Удлинитель USB 3м",
            ),
            "ssd": (
                "Адаптер SSD USB",
                "Крепление SSD настенное",
                "Бокс SSD внешний",
            ),
            "rgb": (
                "Подсветка RGB длина",
                "Лампа RGB контроллер",
                "Лента RGB герметичная",
            ),
        }
        for token, titles in pools.items():
            with self.subTest(token=token):
                labels = self.filter.labels(self.listings(list(titles)))

                self.assertEqual(labels, [token, token, token])

    def test_a_number_can_still_win_the_taxonomy_label(self) -> None:
        # E: numeric tokens are never relational - their candidacy (and the
        # number-veto identity downstream) is unchanged by the filter.
        batch = self.listings(
            [
                "Стекло 13 закалённое",
                "Плёнка 13 антибликовая",
                "Кейс 13 компактный",
            ]
        )

        self.assertEqual(self.filter.labels(batch), ["13", "13", "13"])

    def test_similarity_still_counts_relational_words(self) -> None:
        # F: the filter runs AFTER title_tokens() and only in labels() -
        # similarity reads the untouched stream where "для" is 1 of 5 tokens
        # (3 shared / 5 total = 0.6; a leaked filter would give 0.5).
        self.assertEqual(
            similarity(
                "Чехол силиконовый для телефона",
                "Чехол силиконовый для планшета",
            ),
            0.6,
        )

    def test_the_number_veto_is_untouched(self) -> None:
        # G: two titles stating different numbers are two products - the veto
        # still returns 0.0 regardless of the label filter.
        self.assertEqual(
            similarity(
                "Чехол силиконовый для iPhone 13",
                "Чехол силиконовый для iPhone 14",
            ),
            0.0,
        )

    def test_p15_still_merges_reworded_titles_with_relational_words(self) -> None:
        # H: dedup keeps merging reworded twins of one family - the token
        # stream the family rule reads is byte-identical to before.
        first = candidate("1", title="Кабель USB-C для зарядки 100W", orders=50)
        second = candidate("2", title="Кабель для зарядки USB-C 100W", orders=500)

        kept = DeduplicationFilter().apply([first, second])

        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["product_id"], "2")
    def test_ordinary_product_nouns_stay_label_candidates(self) -> None:
        # I: the filter is a closed preposition class - product nouns,
        # including generic "набор", are never members of it.
        for noun in ("отвертка", "набор", "кабель", "лампа", "держатель", "горшок"):
            with self.subTest(noun=noun):
                batch = self.listings(
                    [
                        f"{noun} красный",
                        f"{noun} синий",
                        f"{noun} зелёный",
                    ]
                )

                self.assertEqual(self.filter.labels(batch), [noun, noun, noun])

    def test_the_taxonomy_constant_never_reaches_search_relevance(self) -> None:
        # J: services/relevance.py keeps its own STOPWORDS untouched; the new
        # constant exists only inside curation.pipeline and shares no object
        # with the search/category semantics.
        from services import relevance

        self.assertFalse(hasattr(relevance, "_TAXONOMY_LABEL_RELATIONAL_WORDS"))
        self.assertIsNot(_TAXONOMY_LABEL_RELATIONAL_WORDS, relevance.STOPWORDS)
        # Search handles "для" through its OWN pre-existing mechanism.
        self.assertIn("для", relevance.STOPWORDS)

    def test_empty_and_single_input(self) -> None:
        only = candidate("1")

        self.assertEqual(self.filter.apply([]), [])
        self.assertEqual(self.filter.apply([only]), [only])

class ModeRankerTests(unittest.TestCase):
    """Stage 4: one order per mode, over real signals only."""

    def test_deals_puts_the_biggest_discount_first(self) -> None:
        batch = [
            {"product_id": "small", "title": "A", "price": 9.5, "discount": 5},
            {"product_id": "big", "title": "B", "price": 4.0, "discount": 60},
            {"product_id": "unstated", "title": "C", "price": 7.0},
        ]

        kept = ModeRanker(MODE_DEALS).apply(batch)

        self.assertEqual(
            [item["product_id"] for item in kept], ["big", "small", "unstated"]
        )

    def test_deals_breaks_a_discount_tie_by_orders(self) -> None:
        batch = [
            {
                "product_id": "quiet",
                "title": "A",
                "price": 5.0,
                "discount_percent": 20,
                "orders": 10,
            },
            {
                "product_id": "busy",
                "title": "B",
                "price": 5.0,
                "discount_percent": 20,
                "orders": 900,
            },
        ]

        kept = ModeRanker(MODE_DEALS).apply(batch)

        self.assertEqual([item["product_id"] for item in kept], ["busy", "quiet"])

    def test_deals_computes_the_discount_from_two_real_prices(self) -> None:
        computed = {
            "product_id": "computed",
            "title": "A",
            "price": 8.0,
            "old_price": 20.0,
            "orders": 1,
        }
        stated = {
            "product_id": "stated",
            "title": "B",
            "price": 9.0,
            "discount_percent": 30,
            "orders": 999,
        }

        self.assertAlmostEqual(discount_percent(computed), 60.0)
        self.assertEqual(
            [
                item["product_id"]
                for item in ModeRanker(MODE_DEALS).apply([stated, computed])
            ],
            ["computed", "stated"],
        )

    def test_a_discount_is_never_invented(self) -> None:
        self.assertIsNone(discount_percent({"price": 20.0, "old_price": 10.0}))
        self.assertIsNone(discount_percent({"price": 20.0}))
        self.assertIsNone(discount_percent({"price": 8.0, "old_price": 0.0}))

    def test_popular_puts_orders_first_and_the_rating_second(self) -> None:
        batch = [
            {
                "product_id": "rated",
                "title": "A",
                "price": 1.0,
                "orders": 10,
                "rating": 5.0,
            },
            {
                "product_id": "ordered",
                "title": "B",
                "price": 1.0,
                "orders": 900,
                "rating": 4.0,
            },
            {
                "product_id": "best",
                "title": "C",
                "price": 1.0,
                "orders": 900,
                "rating": 4.9,
            },
        ]

        kept = ModeRanker(MODE_POPULAR).apply(batch)

        self.assertEqual(
            [item["product_id"] for item in kept], ["best", "ordered", "rated"]
        )

    def test_search_keeps_the_api_order_when_nobody_states_relevance(self) -> None:
        batch = [
            candidate("1", title="Первый вариант"),
            candidate("2", title="Второй вариант"),
            candidate("3", title="Третий вариант"),
        ]

        kept = ModeRanker(MODE_SEARCH).apply(batch)

        self.assertEqual([item["product_id"] for item in kept], ["1", "2", "3"])
        self.assertEqual(kept, batch)

    def test_search_lifts_the_better_described_listing_when_relevance_is_equal(self) -> None:
        rich = {
            "product_id": "rich",
            "title": "B",
            "price": 1.0,
            "relevance": 1.0,
            "orders": 500,
            "rating": 4.8,
            "positive_rate": 98.0,
        }
        batch = [
            {"product_id": "thin", "title": "A", "price": 1.0, "relevance": 1.0},
            rich,
            {"product_id": "best", "title": "C", "price": 1.0, "relevance": 5.0},
        ]

        kept = ModeRanker(MODE_SEARCH).apply(batch)

        self.assertEqual(known_signals(rich), 3)
        self.assertEqual([item["product_id"] for item in kept], ["best", "rich", "thin"])

    def test_search_never_overrides_a_stated_relevance_with_quality(self) -> None:
        batch = [
            {"product_id": "first", "title": "A", "price": 1.0, "relevance": 9.0},
            {
                "product_id": "second",
                "title": "B",
                "price": 1.0,
                "relevance": 1.0,
                "orders": 9000,
                "rating": 5.0,
            },
        ]

        kept = ModeRanker(MODE_SEARCH).apply(batch)

        self.assertEqual([item["product_id"] for item in kept], ["first", "second"])

    def test_a_missing_signal_keeps_its_own_relative_order(self) -> None:
        batch = [
            {"product_id": "silent-a", "title": "A", "price": 1.0},
            {"product_id": "silent-b", "title": "B", "price": 1.0},
        ]

        kept = ModeRanker(MODE_POPULAR).apply(batch)

        self.assertEqual(
            [item["product_id"] for item in kept], ["silent-a", "silent-b"]
        )

    def test_unknown_modes_fall_back_to_search_and_never_reorder(self) -> None:
        batch = [candidate("1"), candidate("2")]

        self.assertEqual(ModeRanker("  DEALS ").mode, MODE_DEALS)
        self.assertEqual(normalize_mode(MODE_POPULAR), MODE_POPULAR)
        self.assertEqual(DEFAULT_MODE, MODE_SEARCH)
        for mode in ("nonsense", "", None, "popular-ish"):
            with self.subTest(mode=mode):
                self.assertEqual(ModeRanker(mode).mode, MODE_SEARCH)
                self.assertEqual(ModeRanker(mode).apply(batch), batch)

    def test_ranking_never_drops_a_candidate(self) -> None:
        batch = [
            candidate(str(index), orders=index, rating=float(index % 5))
            for index in range(10)
        ]

        for mode in (MODE_DEALS, MODE_POPULAR, MODE_SEARCH):
            with self.subTest(mode=mode):
                kept = ModeRanker(mode).apply(batch)
                self.assertEqual(len(kept), len(batch))
                for item in kept:
                    self.assertTrue(any(item is original for original in batch))

    def test_a_deferred_card_ranks_behind_every_fresh_one(self) -> None:
        batch = [
            {
                "product_id": "fresh-small",
                "title": "A",
                "price": 1.0,
                "discount_percent": 5,
                "orders": 1,
                "relevance": 1.0,
            },
            {
                "product_id": "seen-huge",
                "title": "B",
                "price": 1.0,
                "discount_percent": 90,
                "orders": 99999,
                "rating": 5.0,
                "relevance": 9.0,
            },
        ]
        seen = {"seen-huge"}

        for mode in (MODE_DEALS, MODE_POPULAR, MODE_SEARCH):
            with self.subTest(mode=mode):
                kept = ModeRanker(
                    mode, deferred=lambda item: item["product_id"] in seen
                ).apply(batch)

                self.assertEqual(
                    [item["product_id"] for item in kept],
                    ["fresh-small", "seen-huge"],
                )

    def test_without_a_predicate_nothing_is_deferred(self) -> None:
        batch = [
            {"product_id": "first", "title": "A", "price": 1.0, "discount": 1},
            {"product_id": "second", "title": "B", "price": 1.0, "discount": 90},
        ]

        kept = ModeRanker(MODE_DEALS).apply(batch)

        self.assertIsNone(ModeRanker().deferred)
        self.assertEqual([item["product_id"] for item in kept], ["second", "first"])


class HistoryFilterTests(unittest.TestCase):
    """Stage 3: novelty, applied without ever emptying the feed."""

    @staticmethod
    def feed(count: int) -> list[dict]:
        """A pool of ``count`` distinct candidates (titles are irrelevant here)."""

        return [candidate(str(index)) for index in range(count)]

    def test_seen_products_are_dropped_when_enough_fresh_ones_remain(self) -> None:
        kept = HistoryFilter({"0", "1", "2"}).apply(self.feed(9))

        self.assertEqual(
            [item["product_id"] for item in kept],
            [str(index) for index in range(3, 9)],
        )

    def test_seen_products_come_back_at_the_tail_when_fresh_ones_are_too_few(self) -> None:
        batch = self.feed(5)

        kept = HistoryFilter({"0", "1"}).apply(batch)

        self.assertEqual(
            [item["product_id"] for item in kept], ["2", "3", "4", "0", "1"]
        )
        self.assertEqual(len(kept), len(batch))

    def test_the_boundary_is_exactly_the_fresh_minimum(self) -> None:
        enough = self.feed(7)  # six fresh, one seen
        too_few = self.feed(6)  # five fresh, one seen

        self.assertEqual(len(HistoryFilter({"6"}).apply(enough)), FRESH_ENOUGH)

        kept = HistoryFilter({"5"}).apply(too_few)

        self.assertEqual(len(kept), 6)
        self.assertEqual(kept[-1]["product_id"], "5")

    def test_the_fresh_minimum_is_configurable(self) -> None:
        kept = HistoryFilter({"5"}, min_fresh=3).apply(self.feed(6))

        self.assertEqual(
            [item["product_id"] for item in kept], ["0", "1", "2", "3", "4"]
        )

    def test_it_reads_the_marketplace_pairs_the_database_returns(self) -> None:
        batch = [
            candidate("100", marketplace="aliexpress"),
            candidate("200", marketplace="aliexpress"),
            candidate("300", marketplace="aliexpress"),
        ]

        kept = HistoryFilter([("aliexpress", "300")], min_fresh=0).apply(batch)

        self.assertEqual([item["product_id"] for item in kept], ["100", "200"])

    def test_a_marketplace_enum_matches_its_own_pair_only(self) -> None:
        stored = HistoryFilter([("aliexpress", "300")])

        self.assertTrue(
            stored.is_seen(candidate("300", marketplace=Marketplace.ALIEXPRESS))
        )
        self.assertFalse(stored.is_seen(candidate("300", marketplace="other")))

    def test_a_plain_id_list_matches_whatever_the_marketplace_is(self) -> None:
        self.assertTrue(
            HistoryFilter({"300"}).is_seen(candidate("300", marketplace="other"))
        )

    def test_a_callable_snapshot_receives_the_user_id(self) -> None:
        by_user = {7: {"1"}, 8: {"2"}}

        stored = HistoryFilter(lambda user_id: by_user.get(user_id, set()), user_id=7)

        self.assertTrue(stored.is_seen(candidate("1")))
        self.assertFalse(stored.is_seen(candidate("2")))

    def test_the_snapshot_may_be_the_rows_the_caller_already_loaded(self) -> None:
        stored = HistoryFilter([{"product_id": "1"}, SimpleNamespace(product_id="2")])

        self.assertTrue(stored.is_seen(candidate("1")))
        self.assertTrue(stored.is_seen(candidate("2")))

    def test_blank_entries_are_ignored(self) -> None:
        stored = HistoryFilter(["", "   ", None, "42"])

        self.assertTrue(stored.is_seen(candidate("42")))
        self.assertFalse(stored.is_seen(candidate("")))

    def test_an_absent_or_empty_history_changes_nothing(self) -> None:
        batch = self.feed(3)

        for seen in (None, set(), []):
            with self.subTest(seen=seen):
                self.assertEqual(HistoryFilter(seen).apply(batch), batch)

    def test_a_candidate_without_an_id_is_never_seen(self) -> None:
        self.assertFalse(
            HistoryFilter({"1"}).is_seen({"title": "Без идентификатора", "price": 1.0})
        )


class PipelineWiringTests(unittest.TestCase):
    """How ``process`` passes a per-call mode and history to the stages."""

    #: Eight titles of eight different kinds: quality, dedup and diversity all
    #: leave them alone, so a wiring test watches the stage it asks about.
    TITLES = (
        "Механическая клавиатура RGB Red Switch",
        "Наушники беспроводные Bluetooth",
        "Чехол силиконовый для телефона",
        "Мышь вертикальная эргономичная",
        "Настольная лампа светодиодная",
        "Бутылка для воды спортивная",
        "Рюкзак городской водостойкий",
        "Термокружка с крышкой",
    )

    def setUp(self) -> None:
        self.pipeline = CurationPipeline()

    @classmethod
    def distinct(cls, count: int, **extra: object) -> list[dict]:
        return [
            candidate(str(index), title=title, **extra)
            for index, title in enumerate(cls.TITLES[:count])
        ]

    def test_the_chain_is_built_per_call_in_the_documented_order(self) -> None:
        chain = self.pipeline.chain(mode=MODE_DEALS, seen_ids={"1"}, user_id=5)

        self.assertEqual(
            [stage.name for stage in chain],
            ["quality", "deduplication", "history", "mode", "diversity"],
        )
        self.assertEqual(chain[2].user_id, 5)
        self.assertEqual(chain[2].min_fresh, FRESH_ENOUGH)
        self.assertEqual(chain[3].mode, MODE_DEALS)

    def test_seen_ids_reach_the_history_stage_of_this_call(self) -> None:
        batch = self.distinct(8)

        kept = self.pipeline.process(batch, user_id=7, seen_ids={"0", "1"})

        self.assertEqual(
            [item["product_id"] for item in kept], [str(index) for index in range(2, 8)]
        )
        self.assertEqual(self.pipeline.last_report.dropped("history"), 2)

    def test_the_history_of_one_call_never_leaks_into_the_next(self) -> None:
        batch = self.distinct(3)

        deferred = self.pipeline.process(batch, seen_ids={"0"})
        plain = self.pipeline.process(batch)

        self.assertEqual([item["product_id"] for item in deferred], ["1", "2", "0"])
        self.assertEqual(plain, batch)
        self.assertEqual(self.pipeline.last_report.dropped("history"), 0)

    def test_a_callable_history_receives_the_user_id_of_the_call(self) -> None:
        asked: list[int | None] = []

        def history(user_id: int | None) -> set[str]:
            asked.append(user_id)
            return {"0"}

        kept = self.pipeline.process(self.distinct(3), user_id=42, seen_ids=history)

        self.assertEqual(asked, [42])
        self.assertEqual([item["product_id"] for item in kept], ["1", "2", "0"])

    def test_the_mode_of_one_call_never_leaks_into_the_next(self) -> None:
        # Prices inside the recommendation window ($1..$20) and order counts
        # above both profiles' floors, so BOTH the deals call and the default
        # search call keep the pool and only the RANKING differs.
        batch = [
            candidate(
                "1", title=self.TITLES[0], price=12.0,
                discount_percent=5, orders=900,
            ),
            candidate(
                "2", title=self.TITLES[1], price=12.0,
                discount_percent=80, orders=500,
            ),
        ]

        by_discount = self.pipeline.process(batch, mode=MODE_DEALS)
        default = self.pipeline.process(batch)

        self.assertEqual([item["product_id"] for item in by_discount], ["2", "1"])
        self.assertEqual([item["product_id"] for item in default], ["1", "2"])
        self.assertEqual(self.pipeline.ranker.mode, DEFAULT_MODE)

    def test_a_seen_card_stays_behind_the_fresh_ones_in_every_mode(self) -> None:
        # Both cards satisfy the recommendation profile ($1..$20, orders >=
        # 300): what the test watches is the history, not the quality gate.
        batch = [
            candidate(
                "fresh", title=self.TITLES[0], price=12.0,
                discount_percent=1, orders=400,
            ),
            candidate(
                "seen", title=self.TITLES[1], price=12.0,
                discount_percent=90, orders=9999,
            ),
        ]

        for mode in (MODE_DEALS, MODE_POPULAR, MODE_RECOMMENDATION):
            with self.subTest(mode=mode):
                kept = self.pipeline.process(batch, mode=mode, seen_ids={"seen"})

                # The seen card would win on both signals - and still goes last,
                # because the ranking honours the history of this call.
                self.assertEqual(
                    [item["product_id"] for item in kept], ["fresh", "seen"]
                )

    def test_a_custom_chain_is_used_verbatim(self) -> None:
        class KeepAll:
            name = "keep-all"

            def apply(self, candidates):
                return list(candidates)

        pipeline = CurationPipeline([KeepAll()])
        batch = [
            candidate("1", title=self.TITLES[0]),
            {"product_id": "2", "title": "Без цены"},
        ]

        kept = pipeline.process(batch, mode=MODE_DEALS, seen_ids={"1"})

        self.assertEqual(kept, batch)
        self.assertEqual(
            [stage.name for stage in pipeline.last_report.stages], ["keep-all"]
        )

    def test_the_quality_gate_reads_the_mode_of_this_call(self) -> None:
        # One gate per call: the thresholds of ONE feed must never leak into
        # another's: popular/deals share the recommendation profile, the
        # search feed keeps its own, and category browsing (G24.8 P0.2)
        # reads its own tolerant profile.
        recommendation = self.pipeline.chain(mode=MODE_RECOMMENDATION)
        search = self.pipeline.chain(mode=MODE_SEARCH)
        deals = self.pipeline.chain(mode=MODE_DEALS)
        popular = self.pipeline.chain(mode=MODE_POPULAR)
        category = self.pipeline.chain(mode=MODE_CATEGORY)

        self.assertEqual(recommendation[0].mode, MODE_RECOMMENDATION)
        self.assertEqual(deals[0].mode, MODE_RECOMMENDATION)
        self.assertEqual(popular[0].mode, MODE_RECOMMENDATION)
        self.assertEqual(search[0].mode, MODE_SEARCH)
        self.assertEqual(category[0].mode, MODE_CATEGORY)


class QualityProfileTests(unittest.TestCase):
    """The spec split: three published profiles, one gate per mode."""

    def test_the_profiles_match_the_published_spec(self) -> None:
        recommendation = QUALITY_PROFILES[MODE_RECOMMENDATION]
        search = QUALITY_PROFILES[MODE_SEARCH]

        self.assertEqual(
            (
                recommendation.min_price,
                recommendation.max_price,
                recommendation.min_orders,
                recommendation.min_rating,
            ),
            (1.0, 20.0, 300, 4.5),
        )
        self.assertEqual(
            (search.min_price, search.max_price, search.min_orders, search.min_rating),
            # The direct-search profile has NO orders floor: the keyword
            # answer's ``lastest_volume`` is a recent-window figure (0..tens
            # for ordinary listings), and a >=100 gate here emptied ordinary
            # user searches (live audit 2026, the «ничего не найдено» bug).
            (1.0, None, None, 4.5),
        )
        self.assertEqual(MIN_PRICE_USD, 1.0)
        self.assertEqual(MAX_PRICE_USD, 50.0)

    def test_every_feed_mode_reads_the_recommendation_profile(self) -> None:
        for mode in (MODE_RECOMMENDATION, MODE_POPULAR, MODE_DEALS):
            with self.subTest(mode=mode):
                self.assertEqual(quality_mode(mode), MODE_RECOMMENDATION)
        # A typo can never switch the stricter $20 window on by accident:
        # unknown modes normalize to the non-reordering search.
        for mode in (MODE_SEARCH, "nonsense", "", None):
            with self.subTest(mode=mode):
                self.assertEqual(quality_mode(mode), MODE_SEARCH)

    def test_normalize_mode_knows_the_recommendation_literal(self) -> None:
        self.assertEqual(normalize_mode("RECOMMENDATION"), MODE_RECOMMENDATION)
        self.assertIn(MODE_RECOMMENDATION, KNOWN_MODES)


class CategoryQualityPolicyTests(unittest.TestCase):
    """G24.8 P0.2: CATEGORY eligibility is not the recommendation gate.

    After the relevance filter has accepted a candidate, the category
    policy rejects only what cannot be shown honestly (the structural
    contract). The $1..$20 / >= 300 orders / >= 4.5 thresholds stay
    reserved for the recommendation profile - and category eligibility
    must not depend on the request currency.
    """

    def setUp(self) -> None:
        self.category = QualityFilter(mode=MODE_CATEGORY)
        self.recommendation = QualityFilter(mode=MODE_RECOMMENDATION)

    def test_the_category_profile_states_no_monetary_or_popularity_gate(self) -> None:
        profile = QUALITY_PROFILES[MODE_CATEGORY]

        self.assertIsNone(profile.min_price)
        self.assertIsNone(profile.max_price)
        self.assertIsNone(profile.min_orders)
        self.assertIsNone(profile.min_rating)
        # ...while the recommendation profile keeps its published numbers.
        impulse = QUALITY_PROFILES[MODE_RECOMMENDATION]
        self.assertEqual(
            (impulse.min_price, impulse.max_price, impulse.min_orders, impulse.min_rating),
            (1.0, 20.0, 300, 4.5),
        )

    def test_a_sub_dollar_product_stays_eligible(self) -> None:
        listing = candidate(price=0.5, currency="USD")

        self.assertIsNone(self.category.reason(listing))
        self.assertEqual(self.recommendation.reason(listing), "price_too_low")

    def test_a_premium_product_stays_eligible(self) -> None:
        listing = candidate(price=60.0, currency="USD", orders=500)

        self.assertIsNone(self.category.reason(listing))
        self.assertEqual(self.recommendation.reason(listing), "price_too_high")

    def test_few_orders_stay_eligible(self) -> None:
        listing = candidate(price=10.0, currency="USD", orders=5)

        self.assertIsNone(self.category.reason(listing))
        self.assertEqual(self.recommendation.reason(listing), "orders_too_low")

    def test_a_missing_rating_stays_eligible(self) -> None:
        # AliExpress states no 0..5 rating at all; an absent fact can never
        # be a rejection - the same stance every mode already takes.
        listing = candidate(price=10.0, currency="USD", orders=500)

        self.assertIsNone(self.category.reason(listing))

    def test_a_low_rating_stays_eligible(self) -> None:
        listing = candidate(price=10.0, currency="USD", orders=500, rating=3.0)

        self.assertIsNone(self.category.reason(listing))
        self.assertEqual(self.recommendation.reason(listing), "rating_too_low")

    def test_structurally_broken_data_is_still_rejected(self) -> None:
        for listing, reason in (
            (without("product_id"), REASON_PRODUCT_ID),
            (without("title"), REASON_TITLE),
            (candidate(price=0.0), REASON_PRICE_VALUE),
            (candidate(rating=9.0), REASON_RATING),
        ):
            with self.subTest(reason=reason):
                self.assertEqual(self.category.reason(listing), reason)

    def test_currency_never_changes_category_eligibility(self) -> None:
        # USD and UAH CATEGORY requests use the SAME quality semantics: the
        # category profile judges no monetary threshold at all, so the
        # answer currency cannot flip a verdict.
        for currency in ("USD", "UAH", None):
            with self.subTest(currency=currency):
                listing = candidate(price=0.5)
                if currency is not None:
                    listing["currency"] = currency
                self.assertIsNone(self.category.reason(listing))

    def test_category_and_recommendation_are_different_policies(self) -> None:
        self.assertEqual(quality_mode(MODE_CATEGORY), MODE_CATEGORY)
        self.assertEqual(quality_mode(MODE_RECOMMENDATION), MODE_RECOMMENDATION)
        self.assertNotEqual(
            QUALITY_PROFILES[MODE_CATEGORY], QUALITY_PROFILES[MODE_RECOMMENDATION]
        )
        # POPULAR/DEALS still read the recommendation profile unchanged.
        for mode in (MODE_POPULAR, MODE_DEALS):
            with self.subTest(mode=mode):
                self.assertEqual(quality_mode(mode), MODE_RECOMMENDATION)

    def test_category_without_a_relevance_map_keeps_the_received_order(self) -> None:
        # P0.2 fallback, still the contract for unit callers: with no
        # relevance map the received order IS the answer - while the
        # recommendation ranker WOULD flip it (cheap x orders), the very
        # behaviour category results never inherit (G24.8 P1.1 keeps that
        # isolation: the staged category key exists only with a map).
        expensive = candidate("1", price=20.0, orders=300)
        cheap = candidate("2", price=2.0, orders=100)

        kept = ModeRanker(MODE_CATEGORY).apply([expensive, cheap])
        self.assertEqual([item["product_id"] for item in kept], ["1", "2"])

        # The recommendation ranker WOULD flip them (cheap x orders) - the
        # very behaviour category results no longer inherit.
        flipped = ModeRanker(MODE_RECOMMENDATION).apply([expensive, cheap])
        self.assertEqual([item["product_id"] for item in flipped], ["2", "1"])

    def test_category_still_defers_cards_the_user_has_seen(self) -> None:
        batch = [candidate("1"), candidate("2")]
        kept = ModeRanker(
            MODE_CATEGORY, deferred=lambda item: item["product_id"] == "1"
        ).apply(batch)

        self.assertEqual([item["product_id"] for item in kept], ["2", "1"])


class CategoryRankingTests(unittest.TestCase):
    """G24.8 P1.1: CATEGORY orders RELEVANCE FIRST, quality/value second.

    The relevance map plays here exactly the role the wiring layer plays in
    production (``CurationGate._category_relevance`` builds it from the SAME
    ``services.relevance`` functions the service layer sorts with): these
    tests pin the ranker's staged key with hand-written values - no network,
    no model, no production fixture. Every test names the ranking invariant
    it proves.
    """

    @staticmethod
    def _ranked(items, relevance, *, deferred=None):
        return ModeRanker(
            MODE_CATEGORY, deferred=deferred, relevance=relevance
        ).apply(items)

    @staticmethod
    def _ids(products):
        return [item["product_id"] for item in products]

    def test_a_more_relevant_product_stays_above_stronger_metrics(self) -> None:
        # INVARIANT A (the most important): tier beats score beats every
        # commercial metric. The input order deliberately puts the
        # commercial juggernaut first - the staged key must fix it.
        relevant = candidate("relevant", price=49.0)
        strong = candidate("strong", price=1.0, orders=99999)
        popular = candidate("popular", price=1.0, orders=99999)
        relevance = {
            id(relevant): (1, 0.9),
            id(strong): (1, 0.1),
            id(popular): (0, 1.0),
        }

        ranked = self._ranked([popular, strong, relevant], relevance)

        # tier 1 first (even though "popular" states the biggest score),
        # then the higher score inside tier 1 - orders never enter Stage A.
        self.assertEqual(self._ids(ranked), ["relevant", "strong", "popular"])

    def test_quality_decides_within_comparable_relevance(self) -> None:
        # INVARIANT B: identical (tier, score) -> stated quality facts and
        # real signals order the pair; the stronger listing wins.
        rich = candidate("rich", price=10.0, orders=500)
        thin = candidate("thin", price=10.0, orders=5)
        relevance = {id(rich): (1, 0.5), id(thin): (1, 0.5)}

        ranked = self._ranked([thin, rich], relevance)

        self.assertEqual(self._ids(ranked), ["rich", "thin"])


    def test_no_single_metric_owns_the_quality_stage(self) -> None:
        # INVARIANT B + anti-monotony: positive_rate outranks raw volume,
        # so "highest orders first" can never be the whole story - and this
        # is precisely where the recommendation formula (orders*rating^2/
        # price) must NOT appear: with it, volume (500) would beat rated (0
        # orders) - here the rated listing leads the Stage-B chain instead.
        rated = candidate("rated", price=10.0, positive_rate=99.0)
        volume = candidate("volume", price=10.0, orders=500)
        relevance = {id(rated): (1, 0.5), id(volume): (1, 0.5)}

        ranked = self._ranked([volume, rated], relevance)

        self.assertEqual(self._ids(ranked), ["rated", "volume"])

    def test_price_is_only_a_late_tie_break(self) -> None:
        # INVARIANT C: price is secondary - a clearly more relevant $40
        # product beats a cheap weakly relevant one with huge orders...
        pricey_relevant = candidate("pricey", price=40.0, orders=3)
        cheap_weak = candidate("cheap", price=3.0, orders=5000)
        relevance = {id(pricey_relevant): (1, 1.0), id(cheap_weak): (0, 0.2)}

        ranked = self._ranked([cheap_weak, pricey_relevant], relevance)
        self.assertEqual(self._ids(ranked), ["pricey", "cheap"])

        # ...and among FULLY tied neighbours (same tier, score, facts and
        # signals) the cheaper one leads: price decides only exact ties.
        first = candidate("first", price=30.0, orders=10)
        second = candidate("second", price=5.0, orders=10)
        tied = {id(first): (1, 0.5), id(second): (1, 0.5)}
        self.assertEqual(
            self._ids(self._ranked([first, second], tied)), ["second", "first"]
        )


    def test_the_ranker_only_reorders_never_drops(self) -> None:
        # INVARIANT D/E/F/G/H at the ranker level: missing rating, missing
        # orders, zero orders, a $49.99 price and a $0.50 price all stay in
        # the list - the ranker has no eligibility thresholds at all.
        batch = [
            candidate("no-rating", price=49.99),
            candidate("no-orders", price=7.0),
            candidate("zero-orders", price=7.0, orders=0),
            candidate("sub-dollar", price=0.5, orders=0),
        ]
        relevance = {id(item): (1, 0.5) for item in batch}

        ranked = self._ranked(batch, relevance)

        self.assertEqual(len(ranked), len(batch))
        self.assertEqual(set(self._ids(ranked)), set(self._ids(batch)))

    def test_the_category_pipeline_keeps_extreme_and_thin_candidates(self) -> None:
        # INVARIANT D-H end-to-end through the REAL category chain
        # (quality -> dedup -> history -> ranker -> diversity): every
        # honestly-showable candidate survives; only the order may change.
        batch = [
            candidate("sub-dollar", price=0.5, orders=0, title="Кабель питания USB"),
            candidate("premium", price=49.99, title="Монитор игровой 27 дюймов"),
            candidate("no-signals", price=7.0, title="Коврик для мыши офисный"),
            candidate(
                "low-rating", price=7.0, orders=2, rating=3.0, title="Лампа настольная"
            ),
        ]
        relevance = {id(item): (1, 0.5) for item in batch}

        kept = CurationPipeline().process(
            batch, mode=MODE_CATEGORY, relevance=relevance
        )

        self.assertEqual(len(kept), len(batch))
        self.assertEqual(set(self._ids(kept)), set(self._ids(batch)))

    def test_a_seen_card_still_ranks_behind_every_fresh_one_with_a_map(self) -> None:
        # The group stage (history deferral) stays ABOVE the category key:
        # freshness survives the new ranking.
        fresh = candidate("fresh", price=40.0)
        seen = candidate("seen", price=2.0, orders=9000)
        relevance = {id(fresh): (1, 0.1), id(seen): (1, 0.9)}

        ranked = self._ranked(
            [fresh, seen], relevance, deferred=lambda item: item["product_id"] == "seen"
        )

        self.assertEqual(self._ids(ranked), ["fresh", "seen"])


    def test_other_modes_ignore_the_relevance_map(self) -> None:
        # INVARIANT I/J/K: the new map is read by MODE_CATEGORY ONLY.
        first = candidate("first", price=10.0, orders=5)
        second = candidate("second", price=10.0, orders=500)
        relevance = {id(first): (1, 1.0), id(second): (0, 0.0)}

        # RECOMMENDATION keeps its exact formula, with or without the map
        # (and that formula still flips them: cheap x orders).
        with_map = ModeRanker(
            MODE_RECOMMENDATION, relevance=relevance
        ).apply([first, second])
        without_map = ModeRanker(MODE_RECOMMENDATION).apply([first, second])
        self.assertEqual(self._ids(with_map), self._ids(without_map))
        self.assertEqual(self._ids(without_map), ["second", "first"])

        # SEARCH keeps the received order (nothing states a relevance on
        # the candidates) whether or not a map is handed in.
        search_with_map = ModeRanker(MODE_SEARCH, relevance=relevance).apply(
            [first, second]
        )
        self.assertEqual(self._ids(search_with_map), ["first", "second"])

        # POPULAR keeps its own quality_key (orders first) with or without.
        popular_with_map = ModeRanker(MODE_POPULAR, relevance=relevance).apply(
            [first, second]
        )
        popular_without_map = ModeRanker(MODE_POPULAR).apply([first, second])
        self.assertEqual(self._ids(popular_with_map), ["second", "first"])
        self.assertEqual(self._ids(popular_with_map), self._ids(popular_without_map))

    def test_the_same_input_produces_the_same_category_order(self) -> None:
        # INVARIANT L: the staged key is a pure function of the candidates
        # and the map; full ties fall back to the stable arrival order.
        items = [
            candidate(str(i), price=float(i), orders=i * 10) for i in range(5, 0, -1)
        ]
        relevance = {id(item): (1, 0.5) for item in items}

        first = self._ids(self._ranked(list(items), relevance))
        second = self._ids(self._ranked(list(items), relevance))

        self.assertEqual(first, second)


class CategoryDuplicateHandlingTests(unittest.TestCase):
    """G24.8 P1.5: the CATEGORY chain collapses duplicates, keeps families.

    The live duplicate audit of12 category result sets
    (tmp_g248_p15_results.json) finished with ZERO exact and ZERO near
    duplicate pairs in every FINAL list while thousands of family/accessory
    pairs survived untouched. These tests pin the wiring that produced it:
    MODE_CATEGORY runs the family DeduplicationFilter between quality and
    ranking (no production change was needed), and legitimate
    family/accessory products - related by shared category words - are
    NEVER merged.
    """

    @staticmethod
    def _ids(products: Sequence[object]) -> list[str]:
        return [item["product_id"] for item in products]

    def test_a_reworded_twin_takes_only_one_category_slot(self) -> None:
        # The two wordings are ONE family (Jaccard 1.0): they must not
        # occupy two final slots, while the unrelated product stays.
        twin_a = candidate("twin-a", title=TWIN_TITLE, price=12.0, orders=5)
        other = candidate("other", title="Настольная лампа светодиодная", price=20.0)
        twin_b = candidate("twin-b", title=TWIN_TITLE_REORDERED, price=10.0, orders=500)

        pipeline = CurationPipeline()
        kept = pipeline.process([twin_a, other, twin_b], mode=MODE_CATEGORY)

        self.assertEqual(pipeline.last_report.dropped("deduplication"), 1)
        # The stronger offer represents the family in its first member's slot
        # (the documented DeduplicationFilter behaviour), order otherwise kept.
        self.assertEqual(self._ids(kept), ["twin-b", "other"])

    def test_family_and_accessory_products_stay_separate(self) -> None:
        # Related by category words (and «набор» - the P1.3 tools keyword,
        # which is NOT a duplicate signal), yet four legitimately distinct
        # products: the number veto and the threshold both keep them apart.
        batch = [
            candidate("screwdrivers", title="Набор отвёрток 115 в 1", price=15.0),
            candidate("bits", title="Набор бит 100 в 1", price=9.0),
            candidate("drill", title="Дрель аккумуляторная 18 В", price=60.0),
            candidate("drill-bits", title="Сверла по металлу набор", price=7.0),
        ]

        pipeline = CurationPipeline()
        kept = pipeline.process(batch, mode=MODE_CATEGORY)

        self.assertEqual(pipeline.last_report.dropped("deduplication"), 0)
        self.assertEqual(self._ids(kept), ["screwdrivers", "bits", "drill", "drill-bits"])

    def test_the_relevance_ranking_of_the_survivors_is_untouched(self) -> None:
        # The collapse happens BEFORE the ranker (the documented chain), so
        # the G24.8 P1.1 relevance map decides the order of the SURVIVORS:
        # tier 2 above tier 1, regardless of the twin's stronger metrics.
        twin_a = candidate("twin-a", title=TWIN_TITLE, price=12.0, orders=5)
        twin_b = candidate("twin-b", title=TWIN_TITLE_REORDERED, price=10.0, orders=500)
        other = candidate("other", title="Настольная лампа светодиодная", price=20.0)
        relevance = {
            id(other): (2, 1.0),
            id(twin_a): (1, 0.4),
            id(twin_b): (1, 0.4),
        }

        kept = CurationPipeline().process(
            [twin_a, twin_b, other], mode=MODE_CATEGORY, relevance=relevance
        )

        self.assertEqual(self._ids(kept), ["other", "twin-b"])


class QualityFilterProfileTests(unittest.TestCase):
    """Recommendation vs search thresholds at the gate itself."""

    def setUp(self) -> None:
        self.recommendation = QualityFilter(mode=MODE_RECOMMENDATION)
        self.search = QualityFilter(mode=MODE_SEARCH)

    @staticmethod
    def listing(**extra: object) -> dict:
        base: dict = {"product_id": "1", "title": "Наушники беспроводные TWS"}
        base.update(extra)
        return base

    def test_the_filter_exposes_the_profile_it_enforces(self) -> None:
        self.assertEqual(self.recommendation.min_price, 1.0)
        self.assertEqual(self.recommendation.max_price, 20.0)
        self.assertEqual(self.recommendation.min_orders, 300)
        self.assertEqual(self.recommendation.min_rating, 4.5)
        self.assertIsNone(self.search.max_price)
        # No orders floor in a direct search (recent-window volume is not a
        # quality proof; see QUALITY_PROFILES) - the recommendation floor
        # stays strict.
        self.assertIsNone(self.search.min_orders)

    def test_the_recommendation_window_is_enforced(self) -> None:
        gate = self.recommendation
        self.assertEqual(
            gate.reason(self.listing(price=0.5, orders=400)), "price_too_low"
        )
        self.assertIsNone(gate.reason(self.listing(price=1.0, orders=300)))
        self.assertIsNone(gate.reason(self.listing(price=20.0, orders=300)))
        self.assertEqual(
            gate.reason(self.listing(price=20.01, orders=300)), "price_too_high"
        )
        self.assertEqual(
            gate.reason(self.listing(price=5.0, orders=299)), "orders_too_low"
        )
        self.assertEqual(
            gate.reason(self.listing(price=5.0, orders=400, rating=4.4)),
            "rating_too_low",
        )
        self.assertIsNone(gate.reason(self.listing(price=5.0, orders=300, rating=4.5)))

    def test_the_search_profile_has_a_floor_but_no_ceiling(self) -> None:
        gate = self.search
        self.assertEqual(
            gate.reason(self.listing(price=0.99, orders=150)), "price_too_low"
        )
        self.assertIsNone(gate.reason(self.listing(price=1.0, orders=100)))
        # No upper bound in search: a $9000 lens with enough orders is showable.
        self.assertIsNone(gate.reason(self.listing(price=9000.0, orders=150)))
        # No orders floor either: a real product with few recent sales is
        # still a real product (the orders field of a keyword answer is a
        # recent-window volume - see QUALITY_PROFILES). Popularity stays in
        # the ordering, not in the exclusion.
        self.assertIsNone(gate.reason(self.listing(price=5.0, orders=99)))
        self.assertIsNone(gate.reason(self.listing(price=5.0, orders=0)))
        self.assertEqual(
            gate.reason(self.listing(price=5.0, orders=150, rating=4.49)),
            "rating_too_low",
        )
        self.assertIsNone(gate.reason(self.listing(price=5.0, orders=100, rating=4.5)))

    def test_facts_the_marketplace_never_stated_are_not_fatal(self) -> None:
        # The AliExpress affiliate API states NO star rating (and can omit
        # the order count): profile bounds judge what is PRESENT - the same
        # stance as ``services.comparison_service`` (never an empty page).
        for gate in (self.recommendation, self.search):
            with self.subTest(mode=gate.mode):
                self.assertIsNone(gate.reason(self.listing(price=5.0)))

    def test_explicit_thresholds_keep_the_strict_contract(self) -> None:
        strict = QualityFilter(min_rating=4.5, min_orders=100)

        self.assertIsNone(strict.mode)
        self.assertEqual(strict.reason(self.listing(price=5.0)), "missing_rating")
        self.assertEqual(
            strict.reason(self.listing(price=5.0, rating=4.7)), "missing_orders"
        )

    def test_usd_bounds_only_judge_usd_prices(self) -> None:
        # The project converts no currencies: a price the marketplace
        # stated in RUB is left to its own facts (orders/rating), while a
        # USD price meets the USD window.
        expensive_rub = self.listing(price=5000.0, orders=400, currency="RUB")
        expensive_usd = self.listing(price=5000.0, orders=400, currency="USD")

        self.assertIsNone(self.recommendation.reason(expensive_rub))
        self.assertEqual(self.recommendation.reason(expensive_usd), "price_too_high")

    def test_an_empty_strict_window_relaxes_to_the_documented_fallback(self) -> None:
        within_fallback = self.listing(price=35.0, orders=400)
        beyond_fallback = self.listing(price=60.0, orders=400)

        # The single-pass reason stays strict...
        self.assertEqual(self.recommendation.reason(within_fallback), "price_too_high")
        # ...while apply() retries the pool once at the $50 ceiling.
        kept = self.recommendation.apply([within_fallback, beyond_fallback])
        self.assertEqual([item["product_id"] for item in kept], ["1"])

    def test_the_fallback_keeps_every_other_threshold(self) -> None:
        # Above $20 but at/below $50 with TOO FEW orders: the ceiling
        # relaxes, the 300-order floor does not.
        self.assertEqual(
            self.recommendation.apply([self.listing(price=30.0, orders=120)]), []
        )
        # Below $1 the fallback never rescues either.
        self.assertEqual(
            self.recommendation.apply([self.listing(price=0.5, orders=400)]), []
        )
        # Search has no ceiling to relax and no fallback configured.
        self.assertIsNone(self.search.fallback_max_price)
        self.assertEqual(self.search.apply([self.listing(price=0.4, orders=500)]), [])

    def test_a_strictly_satisfied_window_never_touches_the_fallback(self) -> None:
        inside = {"product_id": "inside", "title": "Лампа", "price": 15.0, "orders": 400}
        outside = {"product_id": "outside", "title": "Кресло", "price": 35.0, "orders": 400}

        kept = self.recommendation.apply([inside, outside])

        # One survivor inside $1..$20 keeps the ceiling strict.
        self.assertEqual([item["product_id"] for item in kept], ["inside"])


class RecommendationRankingTests(unittest.TestCase):
    """Stage 4 for the recommendation feed: the impulse-buy score."""

    @staticmethod
    def listing(product_id: str, **extra: object) -> dict:
        base: dict = {
            "product_id": product_id,
            "title": f"Товар {product_id}",
            "price": 10.0,
        }
        base.update(extra)
        return base

    def test_the_score_is_orders_times_rating_squared_over_price(self) -> None:
        score = recommendation_score(
            self.listing("1", price=10.0, orders=500, rating=4.0)
        )

        self.assertAlmostEqual(score, 500 * 16 / 10)

    def test_an_unstated_rating_counts_as_neutral(self) -> None:
        # AliExpress reports no stars: the score reduces to orders/price
        # and the feed still ranks by a real signal.
        score = recommendation_score(self.listing("1", price=5.0, orders=300))

        self.assertAlmostEqual(score, 300 / 5)

    def test_without_orders_there_is_nothing_to_recommend_on(self) -> None:
        self.assertEqual(recommendation_score(self.listing("1", price=5.0)), 0.0)

    def test_the_ranker_orders_by_the_score(self) -> None:
        pricey = self.listing("pricey", price=15.0, orders=600, rating=4.8)
        cheap_popular = self.listing("cheap", price=5.0, orders=400, rating=4.8)

        kept = ModeRanker(MODE_RECOMMENDATION).apply([pricey, cheap_popular])

        # Same orders-per-rating pull, three times the price: the cheaper
        # listing wins (400*4.8^2/5 > 600*4.8^2/15).
        self.assertEqual([item["product_id"] for item in kept], ["cheap", "pricey"])

    def test_an_is_bogo_key_is_not_a_ranking_signal(self) -> None:
        # G24.4: the +15% BOGO boost is removed - even a candidate that
        # literally carries is_bogo=True is ranked by its real score only.
        plain = self.listing("plain", orders=110, is_bogo=False)
        bogo = self.listing("bogo", orders=100, is_bogo=True)

        kept = ModeRanker(MODE_RECOMMENDATION).apply([plain, bogo])

        # Without the removed boost 100 orders can never outrank 110.
        self.assertEqual([item["product_id"] for item in kept], ["plain", "bogo"])

    def test_a_seen_card_ranks_behind_every_fresh_one(self) -> None:
        fresh = self.listing("fresh", orders=300)
        seen = self.listing("seen", orders=9000)
        ranker = ModeRanker(
            MODE_RECOMMENDATION,
            deferred=lambda item: item["product_id"] == "seen",
        )

        kept = ranker.apply([seen, fresh])

        self.assertEqual([item["product_id"] for item in kept], ["fresh", "seen"])


if __name__ == "__main__":
    unittest.main()
