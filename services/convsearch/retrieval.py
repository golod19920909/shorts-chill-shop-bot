"""Candidate retrieval routes over the bot's EXISTING AliExpress pipeline.

Adapted from the reference ``src/retrieval.py``: complementary routes are
fused deterministically, candidates are NOT validated here (that is
``services.convsearch.ranking``), and the pool is deliberately broad. The
routes are the bot's own real retrieval mechanisms — never a second
provider, never fabricated products.
"""

from __future__ import annotations

import logging
import re

from models.product import Product
from services.convsearch.state import ConversationEngineState, Slot

logger = logging.getLogger(__name__)


def _canonical_id(product: Product) -> str:
    """Stable product identity: product_id, else the canonical product URL."""
    return product.product_id or (product.product_url or "").split("?")[0]


def _dedup_by_identity(products: list[Product]) -> list[Product]:
    """Deterministic dedup by stable product id / canonical URL."""
    out: list[Product] = []
    seen: set[str] = set()
    for product in products:
        key = _canonical_id(product)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(product)
    return out


def _terms(text: str) -> list[str]:
    from services.convsearch.text import tokens
    return tokens(text)


def exclude_shown(products: list[Product], state: ConversationEngineState) -> list[Product]:
    """Drop candidates already shown to this user (the «покажи другие» case).

    Only products with a reliable identifier are excluded; rows without one
    stay in the pool rather than being dropped on a guess.
    """

    shown = {pid for pid in state.shown_product_ids if pid}
    if not shown:
        return list(products)
    return [
        product for product in products
        if (_canonical_id(product) or "") not in shown
    ]


def merge_route_results(
    *routes: list[Product],
    exclude: set[str] | None = None,
) -> list[Product]:
    """Deterministic multi-route merge (reference RRF spirit, simplified).

    Route order is the fusion weight: a product in an earlier route keeps
    the earlier position. Dedup by stable identity; explicit ``exclude`` ids
    are dropped.
    """

    out: list[Product] = []
    seen: set[str] = set()
    for route in routes:
        for product in route:
            key = _canonical_id(product)
            if not key or key in seen:
                continue
            if exclude and key in exclude:
                continue
            seen.add(key)
            out.append(product)
    return out


def _excluded_fragments(state: ConversationEngineState) -> list[str]:
    return [slot.value for slot in state.active() if slot.kind == "exclusion"]


def _has_fragments(text: str, fragments: list[str]) -> bool:
    """Whether the text carries ALL tokens of any exclusion fragment.

    Tokens compare by stem (``type_validation.stems_match``), so an
    exclusion «не ударный» still rejects «ударная» / «ударным».
    """

    from services.convsearch.type_validation import stems_match

    joined = _terms(text)
    for fragment in fragments:
        fragment_tokens = _terms(fragment)
        if fragment_tokens and all(
            any(stems_match(token, want) for token in joined)
            for want in fragment_tokens
        ):
            return True
    return False


def filter_by_exclusions(
    products: list[Product],
    state: ConversationEngineState,
) -> tuple[list[Product], list[Product]]:
    """Split the pool into (survivors, rejected) on the active exclusions.

    A candidate matching an exclusion is rejected, not re-ranked; the caller
    can distinguish «пусто» from «пусто из-за фильтров» via the second list.
    """

    fragments = _excluded_fragments(state)
    if not fragments:
        return list(products), []
    survivors, rejected = [], []
    for product in products:
        text = _product_text_for_match(product)
        if _has_fragments(text, fragments):
            rejected.append(product)
        else:
            survivors.append(product)
    return survivors, rejected


def _product_text_for_match(product: Product) -> str:
    return product.title or ""


def filter_by_budget(
    products: list[Product],
    state: ConversationEngineState,
) -> tuple[list[Product], list[Product]]:
    """Split the pool on the active budget slot; rows without a price pass."""

    budget = state.active_by_kind("budget")
    if budget is None:
        return list(products), []
    try:
        limit = float(str(budget.value).split()[0])
    except ValueError:
        return list(products), []
    survivors, rejected = [], []
    for product in products:
        price = product.price
        if price is None or price <= limit:
            survivors.append(product)
        else:
            rejected.append(product)
    return survivors, rejected


# ---------------------------------------------------------------------------
# The retrieval adapter (AliExpress-specific, explicit — never a hidden
# delegation to a legacy orchestrator)
# ---------------------------------------------------------------------------

from dataclasses import dataclass, field

from models.search_request import SearchMode, SearchRequest
from services.search_service import SearchOutcome, SearchService


@dataclass
class RetrievalStrategy:
    """What the engine asks the provider for, and how the pool is judged."""

    request: SearchRequest
    #: The product type the candidates must BE (validated as a hard gate).
    product_type: str | None
    result_limit: int | None
    budget: dict = field(default_factory=dict)
    exclusions: list[str] = field(default_factory=list)


@dataclass
class BackendOutcome:
    """The honest retrieval result: ok / empty / provider failure."""

    ok: bool
    reason: str          # "ok" | "no_candidates" | "provider_error" | "empty_query"
    products: list[Product] = field(default_factory=list)
    #: The request that was attempted — reported even on failure, so a
    #: refinement survives an outage (D1 state consistency).
    request: SearchRequest | None = None


class RetrievalBackend:
    """The ONLY retrieval path the engine uses.

    Delegates the provider call to the EXISTING ``SearchService`` (its
    AliExpress provider adapter, retries, rate-limit handling and dedup are
    shared infrastructure and stay). This class builds the ONE
    ``SearchRequest`` from the engine's ledger — the legacy parse/merge/
    intent layers are not consulted — and translates the provider's answer
    into an honest ``BackendOutcome``.
    """

    def __init__(self, search_service: SearchService | None = None) -> None:
        self._search_service = search_service

    def bind(self, search_service: SearchService) -> None:
        """Bind the real SearchService (called by the handler at startup)."""

        self._search_service = search_service

    def _require_service(self) -> SearchService:
        if self._search_service is None:
            raise RuntimeError("RetrievalBackend has no SearchService bound")
        return self._search_service

    async def retrieve(self, strategy: RetrievalStrategy, *, exclude=None) -> BackendOutcome:
        service = self._require_service()
        request = strategy.request
        phrase = (request.normalized_query or request.original_query or "").strip()
        if not phrase:
            return BackendOutcome(ok=False, reason="empty_query", request=request)
        try:
            outcome: SearchOutcome = await service.find_products(
                request, exclude=exclude, raw=True
            )
        except Exception:
            logger.exception("RetrievalBackend: provider call failed")
            return BackendOutcome(ok=False, reason="provider_error", request=request)
        # Honest provider failure: every configured provider raised or none
        # is configured. An empty catalogue is a DIFFERENT outcome.
        if not outcome.products and (
            outcome.failed_marketplaces or outcome.unavailable_marketplaces
        ) and not outcome.searched_marketplaces:
            return BackendOutcome(ok=False, reason="provider_error", request=request)
        return BackendOutcome(
            ok=True,
            reason="ok" if outcome.products else "no_candidates",
            products=list(outcome.products),
            request=request,
        )


def rebuild_strategy_with_type(strategy: RetrievalStrategy, product_type: str):
    """The same strategy re-anchored on the JUST-NAMED product type."""

    strategy.product_type = product_type
    request = strategy.request
    # The phrase IS the product: a NEW_SEARCH turn must query the type it
    # just named, not the previous turn's phrase.
    strategy.request = request.model_copy(
        update={"intent_product_type": product_type, "normalized_query": product_type}
    )
    return strategy


def build_retrieval_strategy(
    state: ConversationEngineState,
    text: str,
    *,
    currency: str | None = None,
    result_limit: int | None = None,
) -> RetrievalStrategy | None:
    """Turn the ledger + the user's words into ONE retrieval strategy.

    Returns ``None`` when there is nothing to search with yet (no product
    words, no constraints) — the engine then answers with a clarification.
    """

    from services.convsearch.text import tokens

    type_slot = state.active_by_kind("type")
    product_type = type_slot.value if type_slot else None
    from services.convsearch.type_validation import stems_match

    type_tokens = tokens(product_type or "")

    def _covered_by_type(token: str) -> bool:
        return any(stems_match(token, t) for t in type_tokens)

    # Feature spans travel into the phrase MINUS the tokens the product
    # type already covers («катушки для спиннинга» on an active «катушка»
    # search contributes «спиннинга», not a second «катушки»).
    features = []
    for feature in state.active_values("feature"):
        kept = [token for token in tokens(feature) if not _covered_by_type(token)]
        if kept:
            features.append(" ".join(kept))
    exclusions = state.active_values("exclusion")
    budget = state.active_by_kind("budget")

    # The retrieval phrase: the type (the product itself), plus positive
    # feature words. Exclusion words never enter the provider query — they
    # are enforced post-retrieval, where evidence can be judged.
    phrase_parts: list[str] = []
    if product_type:
        phrase_parts.append(product_type)
    for feature in features:
        if feature not in phrase_parts:
            phrase_parts.append(feature)
    # A compatibility constraint stated in THIS message («для hyundai
    # tucson») rides along with the product type: the phrase becomes
    # «<type> для hyundai tucson» and the constraint tokens are validated
    # against the returned titles. Category-general: the words are whatever
    # the user wrote, never a curated list. Scaffolding/qualifier/budget/
    # count/colour words are NOT constraints — they would pollute the
    # marketplace query («катушка долларов») and dilute its relevance.
    from services.convsearch.state import (
        _BUDGET_FRAMING_WORDS, _CHARACTERISTIC_NOUNS, _COUNT_WORDS,
        _QUALIFIER_WORDS, is_colour_word,
    )

    covered = set(tokens(" ".join(phrase_parts)))
    from services.convsearch.engine import _DISCOURSE_MARKERS

    def _already_covered(token: str) -> bool:
        return (
            _covered_by_type(token)
            or any(stems_match(token, seen) for seen in covered)
            or any(
                stems_match(token, t)
                for feature in features
                for t in tokens(feature)
            )
        )

    def _new_word(token: str) -> bool:
        return (
            len(token) >= 3
            and token not in _BUDGET_FRAMING_WORDS
            and token not in _COUNT_WORDS
            and token not in _CHARACTERISTIC_NOUNS
            and token not in _QUALIFIER_WORDS
            and token not in _DISCOURSE_MARKERS
            and not is_colour_word(token)
            and not _already_covered(token)
            and not any(stems_match(token, kept) for kept in constraint_words)
        )

    constraint_words: list[str] = []
    for token in tokens(text):
        if _new_word(token):
            constraint_words.append(token)
    if constraint_words:
        for word in constraint_words:
            if word not in phrase_parts:
                phrase_parts.append(word)
    # Fall back to the user's own words when the ledger has no type yet
    # (first message that has not produced an intent_product_type).
    if not phrase_parts:
        content = [word for word in tokens(text) if len(word) >= 3]
        if not content and not features and not exclusions and budget is None:
            return None
        phrase_parts = content

    phrase = " ".join(phrase_parts).strip()
    if not phrase:
        return None

    request = SearchRequest(
        original_query=text or phrase,
        normalized_query=phrase,
        mode=SearchMode.SEARCH,
        result_limit=result_limit,
        currency=currency,
        required_features=[*list(state.active_values("feature")), *constraint_words],
        excluded_features=list(exclusions),
    )
    if budget is not None:
        try:
            request = request.model_copy(
                update={"max_price": float(str(budget.value).split()[0])}
            )
        except (ValueError, IndexError):
            pass
    return RetrievalStrategy(
        request=request,
        product_type=product_type,
        result_limit=result_limit,
        budget=(budget.value or "").split(maxsplit=1) if budget else [],
        exclusions=list(exclusions),
    )
