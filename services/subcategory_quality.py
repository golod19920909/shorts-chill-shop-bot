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
    # Russian inflection can share a five-character stem, but a four-letter
    # prefix is too broad for catalog identities: «велосипедный» and
    # «велофонарь» both start with «вело» but name different products.
    if (
        len(token) >= 5
        and len(term) >= 5
        and token[:5] == term[:5]
        and token[0] in "абвгдеёжзийклмнопрстуфхцчшщьыъэюя"
    ):
        return True
    return False


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


def _phrase_position(title_tokens: tuple[str, ...], phrase: str) -> int | None:
    """Return the earliest local title window supporting an alias."""

    wanted = _tokens(phrase)
    if not wanted:
        return None
    if len(wanted) == 1:
        term = wanted[0]
        if len(term) < 3:
            return None
        return next(
            (index for index, token in enumerate(title_tokens)
             if _token_matches(token, term)),
            None,
        )
    window_size = min(6, max(3, len(wanted) + 2))
    for start in range(len(title_tokens)):
        window = title_tokens[start : start + window_size]
        if all(
            any(_token_matches(token, term) for token in window)
            for term in wanted
        ):
            return start
    return None


def subcategory_product_type(
    product: Product, subcategory: Subcategory
) -> str | None:
    """Classify by the earliest validated principal product identity.

    AliExpress titles often mention several products (for example, a speaker
    with a microphone). A matching word is not enough to assign a subtype:
    the shared principal-item validator must approve that alias, and earlier
    product identity evidence takes precedence over later add-ons.
    """

    primary_title = re.split(r"[,;|]", product.title or "", maxsplit=1)[0]
    title_tokens = _tokens(primary_title)
    aliases = tuple(subcategory.product_types or subcategory.keywords)
    token_frequency: dict[str, int] = {}
    for alias in aliases:
        for token in set(_tokens(alias)):
            token_frequency[token] = token_frequency.get(token, 0) + 1

    from services.convsearch.type_validation import validate_product_type

    principal_product = product.model_copy(update={"title": primary_title})
    matches: list[tuple[str, int]] = []
    for alias in aliases:
        position = _phrase_position(title_tokens, alias)
        if position is None:
            continue
        if validate_product_type(principal_product, alias).qualifies:
            matches.append((alias, position))
    if not matches:
        return None

    def specificity(alias: str) -> tuple[int, int, float]:
        terms = set(_tokens(alias))
        score = sum(1.0 / token_frequency.get(term, 1) for term in terms)
        return len(terms), len(alias), score

    # The first supported product identity wins. Specificity breaks ties
    # between aliases that describe the same title span.
    earliest = min(position for _, position in matches)
    leading_matches = [
        alias for alias, position in matches if position == earliest
    ]
    matched_alias = max(leading_matches, key=specificity)
    # Thematic shelves unify translated/synonymous aliases before balancing.
    # Ordinary catalog shelves retain their previous alias-level type keys.
    for group in subcategory.product_type_groups:
        if any(alias.casefold() == matched_alias.casefold() for alias in group):
            return group[0].casefold()
    return matched_alias.casefold()


def subcategory_match_reason(product: Product, subcategory: Subcategory) -> str | None:
    """Return a rejection reason, or None when title evidence fits the shelf."""

    # AliExpress often appends cross-sells after commas or separators.
    # Only the leading product identity may establish shelf membership;
    # a later mention of the requested type is not enough.
    primary_title = re.split(r"[,;|]", product.title or "", maxsplit=1)[0]
    tokens = _tokens(primary_title)
    if not tokens:
        return "empty_title"
    for phrase in subcategory.exclude_terms:
        if _phrase_matches(tokens, phrase):
            return "excluded_title_concept"
    aliases = subcategory.product_types or subcategory.keywords
    matching_aliases = [
        phrase for phrase in aliases if _phrase_matches(tokens, phrase)
    ]
    if not matching_aliases:
        return "no_subcategory_product_type"

    # A title may contain the configured product type while the listing is
    # actually a replacement part or accessory FOR that product. Reuse the
    # conversation engine's category-general principal-item validator rather
    # than maintaining a second list of accessory words for the catalog.
    # Import locally: SearchService imports this module, while the validator
    # belongs to the conversational-search package.
    from services.convsearch.type_validation import validate_product_type

    principal_product = product.model_copy(update={"title": primary_title})
    if not any(
        validate_product_type(principal_product, alias).qualifies
        for alias in matching_aliases
    ):
        return "accessory_or_component"

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
