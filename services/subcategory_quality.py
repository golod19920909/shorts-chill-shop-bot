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
    # Marketplace titles concatenate product identity with long feature
    # lists. Requiring alias words anywhere in the title lets unrelated
    # fragments accidentally form a product type (e.g. "spot welding ...
    # car dent puller ... machine"). Keep the flexible word order and
    # inflections, but require all alias components to occur in one local
    # title window rather than across the whole listing.
    window_size = min(6, max(3, len(wanted) + 2))
    for start in range(len(title_tokens)):
        window = title_tokens[start : start + window_size]
        if all(
            any(_token_matches(token, term) for token in window)
            for term in wanted
        ):
            return True
    return False


def subcategory_product_type(
    product: Product, subcategory: Subcategory
) -> str | None:
    """Return the most specific configured product-type alias in the title.

    The alias vocabulary is already the deterministic identity gate for this
    shelf. Prefer aliases carrying rarer, more specific words (for example
    "neck massager" over the generic "massager") so the same vocabulary can
    guide diversity without adding a second hand-maintained taxonomy.
    """

    title_tokens = _tokens(product.title)
    aliases = tuple(subcategory.product_types or subcategory.keywords)
    token_frequency: dict[str, int] = {}
    for alias in aliases:
        for token in set(_tokens(alias)):
            token_frequency[token] = token_frequency.get(token, 0) + 1

    matches = [alias for alias in aliases if _phrase_matches(title_tokens, alias)]
    if not matches:
        return None

    def specificity(alias: str) -> tuple[float, int, int]:
        terms = set(_tokens(alias))
        score = sum(1.0 / token_frequency.get(term, 1) for term in terms)
        return score, len(terms), len(alias)

    return max(matches, key=specificity).casefold()


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
