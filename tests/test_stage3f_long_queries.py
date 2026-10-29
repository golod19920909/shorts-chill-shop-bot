# -*- coding: utf-8 -*-
"""Stage 3F: preserve important trailing terms of long search queries.

Regression tests for the blind ``[:100]`` truncation that dropped the tail
(model, compatibility, numeric specs, required features) of long queries in
``AliExpressProvider._search_params`` and ``query_variants._own_phrase``.
Both now fit phrases with ``text_matching.truncate_provider_phrase``
(head + tail words, same 100-char provider limit). Tests inspect the actual
outgoing provider ``keywords`` and the final request state; no live
AliExpress calls.
"""

from __future__ import annotations

import unittest

from config.settings import MarketplaceCredentials
from models.search_request import SearchMode, SearchRequest
from services.marketplaces.aliexpress import AliExpressProvider
from services.query_variants import _own_phrase, product_query_variants
from services.text_matching import (
    PROVIDER_KEYWORDS_LIMIT,
    truncate_provider_phrase,
)

LONG_QUERY = (
    "наушники беспроводные с шумоподавлением и хорошим микрофоном "
    "для длительной комфортной работы в большом шумном офисе "
    "открытого типа с водозащитой"
)

LONG_COMPAT_QUERY = (
    "камера заднего вида для автомобиля Toyota Camry 70 с ночным "
    "видением и широким углом обзора для безопасной парковки ночью версия pro"
)


def provider() -> AliExpressProvider:
    return AliExpressProvider(MarketplaceCredentials(None, None))


def outgoing_keywords(text: str, **fields) -> tuple[str, SearchRequest]:
    request = SearchRequest(
        original_query=text, normalized_query=text, mode=SearchMode.SEARCH, **fields
    )
    params = provider()._search_params(request)
    return params["keywords"], request


class QueryLengthBoundariesTests(unittest.TestCase):
    """Short / exact / long queries all respect the provider limit."""

    def test_short_query_passes_through(self) -> None:
        keywords, _ = outgoing_keywords("наушники")
        self.assertEqual(keywords, "наушники")

    def test_exactly_100_chars_unchanged(self) -> None:
        text = "a" * 95 + " bbbb"
        self.assertEqual(len(text), 100)
        self.assertEqual(truncate_provider_phrase(text), text)

    def test_long_query_stays_within_limit(self) -> None:
        self.assertGreater(len(LONG_QUERY), 100)
        keywords, _ = outgoing_keywords(LONG_QUERY)
        self.assertLessEqual(len(keywords), PROVIDER_KEYWORDS_LIMIT)
        self.assertEqual(PROVIDER_KEYWORDS_LIMIT, 100)

class TrailingTermsPreservedTests(unittest.TestCase):
    """Terms near the end survive the provider fit."""

    def test_head_and_tail_survive(self) -> None:
        keywords, _ = outgoing_keywords(LONG_QUERY)
        self.assertTrue(keywords.startswith("наушники"))
        self.assertIn("водозащитой", keywords)

    def test_blind_slice_would_lose_the_tail(self) -> None:
        self.assertNotIn("водозащитой", LONG_QUERY[:100])

    def test_model_and_compat_tail_survive(self) -> None:
        keywords, _ = outgoing_keywords(LONG_COMPAT_QUERY)
        self.assertIn("camry", keywords.casefold())
        self.assertIn("pro", keywords.split())


class TailQualifierTests(unittest.TestCase):
    """Numeric specs, features and version qualifiers in the tail."""

    def test_version_qualifier_tail_survives(self) -> None:
        text = (
            "чехол для телефона с усиленными углами и мягкой подкладкой "
            "для повседневного ношения в сумке и кармане версия pro max"
        )
        self.assertGreater(len(text), 100)
        keywords, _ = outgoing_keywords(text)
        self.assertIn("pro", keywords.split())
        self.assertIn("max", keywords.split())


    def test_required_feature_tail_survives_own_phrase(self) -> None:
        own = _own_phrase(LONG_QUERY)
        self.assertLessEqual(len(own), PROVIDER_KEYWORDS_LIMIT)
        self.assertIn("водозащитой", own)


class BudgetAndConstraintsTests(unittest.TestCase):
    """Long queries with budget/currency keep constraints on the request."""

    def test_budget_currency_survive_long_query(self) -> None:
        keywords, request = outgoing_keywords(
            LONG_QUERY, max_price=30.0, currency="USD"
        )
        self.assertLessEqual(len(keywords), PROVIDER_KEYWORDS_LIMIT)
        self.assertIn("водозащитой", keywords)
        self.assertEqual(request.max_price, 30.0)
        self.assertEqual(request.currency, "USD")
        self.assertEqual(request.normalized_query, LONG_QUERY)


class DeterminismAndDedupTests(unittest.TestCase):
    """Repeated runs agree; variants stay unique and bounded."""

    def test_repeated_runs_agree(self) -> None:
        first, _ = outgoing_keywords(LONG_QUERY)
        for _ in range(5):
            again, _ = outgoing_keywords(LONG_QUERY)
            self.assertEqual(again, first)

    def test_variants_unique_and_bounded(self) -> None:
        request = SearchRequest(
            original_query=LONG_QUERY,
            normalized_query=LONG_QUERY,
            mode=SearchMode.SEARCH,
        )
        variants = product_query_variants(request)
        folded = [v.casefold() for v in variants]
        self.assertEqual(len(folded), len(set(folded)))
        self.assertLessEqual(len(variants), 3)


class MalformedInputTests(unittest.TestCase):
    """Empty / whitespace / malformed input never crashes."""

    def test_empty_and_whitespace(self) -> None:
        self.assertEqual(truncate_provider_phrase(""), "")
        self.assertEqual(truncate_provider_phrase("   "), "")
        self.assertEqual(truncate_provider_phrase(None), "")

    def test_single_overlong_word_hard_cut(self) -> None:
        word = "x" * 150
        result = truncate_provider_phrase(word)
        self.assertLessEqual(len(result), PROVIDER_KEYWORDS_LIMIT)

    def test_request_without_queries(self) -> None:
        keywords, _ = outgoing_keywords("")
        self.assertEqual(keywords, "")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

