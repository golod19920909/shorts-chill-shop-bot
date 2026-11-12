"""Deterministic quality gate for curated catalog subcategories.

A catalog route may be broad, but a product shown inside a subcategory must
carry positive title evidence for one of that shelf's declared product types.
This gate does not infer specifications or invent product data.
"""
from __future__ import annotations

import re
from collections.abc import Sequence

from config.subcategories import Subcategory
from models.product import Product
from models.search_request import SearchRequest
from services.relevance import rejection_reason


_WORD_RE = re.compile(r"[a-z0-9]+|[а-яё]+", re.IGNORECASE)


def _tokens(value: str | None) -> tuple[str, ...]:
    return tuple(
        token.replace("ё", "е").casefold()
        for token in _WORD_RE.findall(value or "")
    )


def _token_matches(token: str, term: str) -> bool:
    if token == term:
        return True
    if len(token) < 3 or len(term) < 3:
        return False
    if token.startswith(term) or term.startswith(token):
        return True
    if len(token) >= 4 and len(term) >= 4:
        if token[:4] == term[:4] and token[0] in "абвгдеёжзийклмнопрстуфхцчшщьыъэюя":
            return True
    return len(token) >= 5 and len(term) >= 5 and token[:5] == term[:5]


def _phrase_matches(title_tokens: tuple[str, ...], phrase: str) -> bool:
    wanted = _tokens(phrase)
    if not wanted:
        return False
    # Match a phrase by its component tokens, allowing harmless word-order
    # changes and Russian inflections while requiring every meaningful part.
    if len(wanted) == 1:
        term = wanted[0]
        if len(term) < 3:
            return False
        return any(_token_matches(token, term) for token in title_tokens)
    # Multi-word aliases may be reordered in marketplace titles; every
    # meaningful component must still be present.
    return all(
        any(_token_matches(token, term) for token in title_tokens)
        for term in wanted
    )


def subcategory_match_reason(product: Product, subcategory: Subcategory) -> str | None:
    """Return a rejection reason, or None when title evidence fits the shelf."""

    tokens = _tokens(product.title)
    if not tokens:
        return "empty_title"
    for phrase in subcategory.exclude_terms:
        if _phrase_matches(tokens, phrase):
            return "excluded_title_concept"
    aliases = subcategory.product_types or subcategory.keywords
    if not any(_phrase_matches(tokens, phrase) for phrase in aliases):
        return "no_subcategory_product_type"
    return None



def filter_subcategory_relevant(
    products: Sequence[Product],
    request: SearchRequest,
    subcategory: Subcategory,
) -> tuple[list[Product], dict[str, int]]:
    """Apply shared hard checks plus subcategory-specific positive evidence.

    A broad marketplace route is only a retrieval hypothesis. A product such
    as Electric Spin Scrubber can fit cleaning without repeating the route
    word cleaning. Its explicit product-type alias bypasses only the generic
    no_query_term_in_title rejection. Invalid URLs, empty titles, and every
    other shared relevance rejection remain in force.
    """
    kept: list[Product] = []
    dropped: dict[str, int] = {}

    for product in products:
        shared_reason = rejection_reason(product, request)
        if shared_reason in {"invalid_product_url", "empty_title"}:
            dropped[shared_reason] = dropped.get(shared_reason, 0) + 1
            continue

        shelf_reason = subcategory_match_reason(product, subcategory)
        if shelf_reason is not None:
            dropped[shelf_reason] = dropped.get(shelf_reason, 0) + 1
            continue

        if shared_reason is not None and shared_reason != "no_query_term_in_title":
            dropped[shared_reason] = dropped.get(shared_reason, 0) + 1
            continue

        kept.append(product)

    return kept, dropped

def filter_subcategory_products(
    products: Sequence[Product], subcategory: Subcategory
) -> tuple[list[Product], dict[str, int]]:
    """Keep only listings with positive title evidence for this subcategory."""

    kept: list[Product] = []
    dropped: dict[str, int] = {}
    for product in products:
        reason = subcategory_match_reason(product, subcategory)
        if reason is None:
            kept.append(product)
        else:
            dropped[reason] = dropped.get(reason, 0) + 1
    return kept, dropped
