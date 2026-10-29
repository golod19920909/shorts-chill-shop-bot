"""Structured representation of a parsed user search request.

Instances of this model are produced by ``services.ai_service`` from a
free-form natural-language message. They describe *what to search
for*, not the actual products - Gemini fills in these fields, but the
real product data always comes from marketplace providers.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from models.product import Marketplace


class SortPreference(str, Enum):
    """How the user (explicitly or implicitly) wants results ordered."""

    RELEVANCE = "relevance"
    PRICE_ASC = "price_asc"
    PRICE_DESC = "price_desc"
    RATING_DESC = "rating_desc"
    ORDERS_DESC = "orders_desc"
    TOTAL_COST_ASC = "total_cost_asc"

    #: Order by the marketplace popularity signals we actually have
    #: (orders count, then rating). Used by the legacy "🔥 Популярное"
    #: requests of stored history; no current screen produces it.
    POPULARITY_DESC = "popularity_desc"

    #: Order by the deterministic value score in
    #: ``services.comparison_service.value_score``. Used by
    #: "💰 Выгодные находки"; never decided by an LLM.
    VALUE_DESC = "value_desc"


class SearchMode(str, Enum):
    """Which kind of discovery produced this request.

    Providers may branch on this once a real API is wired up (for
    example a dedicated "popular listings" endpoint). Until then it only
    carries intent, and the ranking is still applied programmatically by
    the search service.
    """

    SEARCH = "search"
    #: The «✨ Находки» feed (G24): one documented page of AliExpress' own
    #: broad listing. Replaces the legacy ``POPULAR`` mode, which is kept
    #: ONLY so historical ``search_history`` rows still validate (the same
    #: reason ``DEALS``/``PROMO`` exist).
    FEED = "feed"
    #: Legacy mode of the removed «🔥 Популярное» rotation. Kept only for
    #: historical ``search_history`` rows (``mode: "popular"``); no UI
    #: screen and no service method produces it any more.
    POPULAR = "popular"
    #: A discovery query ("что-нибудь прикольное для машины", "удиви меня"):
    #: the words describe a MOOD, not a product, so the pipeline treats it
    #: like the popular feed - AI may order/diversify real candidates, but
    #: relevance is never enforced word-by-word (there is nothing to match).
    DISCOVERY = "discovery"
    #: Legacy discovery modes kept ONLY so historical ``search_history``
    #: rows (``mode: "deals" / "promo"``) still validate. No UI screen and
    #: no service method produces them any more - the features were
    #: removed by the final spec (tombstone callbacks answer old buttons).
    DEALS = "deals"
    CATEGORY = "category"
    IMAGE = "image"
    PROMO = "promo"


class SearchRequest(BaseModel):
    """Structured search parameters extracted from a natural-language query.

    This model is intentionally extensible: new optional fields can be
    added later (e.g. color, brand) without breaking existing callers,
    since every field beyond ``original_query`` has a default.
    """

    model_config = ConfigDict(extra="forbid")

    original_query: str
    normalized_query: str | None = None

    mode: SearchMode = SearchMode.SEARCH
    category: str | None = None
    keywords: list[str] = Field(default_factory=list)

    max_price: float | None = Field(default=None, ge=0)
    min_price: float | None = Field(default=None, ge=0)
    currency: str | None = None

    #: Optional delivery destination (ISO-3166 alpha-2). Only forwarded
    #: to providers that document destination support; the bot never
    #: claims a country changes a price unless the API says so.
    country: str | None = None

    min_rating: float | None = Field(default=None, ge=0, le=5)
    min_orders: int | None = Field(default=None, ge=0)

    #: Set by ``comparison_service.filter_products``: whether a
    #: ``min_rating`` constraint could really be applied. Marketplaces
    #: that do not report 0-5 star ratings (the AliExpress Affiliate API
    #: does not) leave this ``False``. The fact is used for internal
    #: diagnostics only: such a constraint is never turned into an empty
    #: page, and the user is never shown an explanation of what the API
    #: does or does not return (see ``handlers.search``).
    rating_filter_applicable: bool = True

    #: Set by the parser when the user asked for a well-rated product IN
    #: WORDS (``с хорошим рейтингом``, ``with good reviews``) without
    #: naming a number. A numeric bound is ``min_rating`` and stays a
    #: FILTER; this flag only reorders the result set - within one
    #: relevance tier, by the real quality fields the marketplace
    #: returned (``positive_rate``, orders). It never filters, never
    #: invents a rating and never overrides relevance.
    prefers_good_rating: bool = False

    quantity: int | None = Field(default=None, ge=1)

    #: How many RESULT CARDS the user explicitly asked to be shown
    #: («покажи 3 варианта», «show 5 earbuds»). Distinct from ``quantity``,
    #: which is units PER ITEM («100 pieces»). Set only by the deterministic
    #: count capture in ``AIService.parse_query`` — the LLM never fills it —
    #: and applied as a hard ceiling on the delivered list by
    #: ``SearchService.find_products``. When absent, the existing default
    #: browse cap (20 cards) applies unchanged; when fewer suitable products
    #: exist, fewer are shown — nothing is ever padded or invented.
    result_limit: int | None = Field(default=None, ge=1, le=100)

    #: Which documented result page of the marketplace a retrieval call asks
    #: for (``page_no``). ``1`` is the first page and the default, so nothing
    #: changes for a caller that does not care. ``services.search_service``
    #: asks deeper pages of the SAME phrase while the relevant pool is still
    #: thin: one page of one narrow phrase is a weak way to build a pool to
    #: browse, while a deeper page of the identical query never introduces a
    #: word the user did not write. Internal bookkeeping - the parser and the
    #: model never set it, and it is never shown to the user.
    page_no: int = Field(default=1, ge=1)

    preferred_marketplaces: list[Marketplace] = Field(default_factory=list)

    required_features: list[str] = Field(default_factory=list)
    excluded_features: list[str] = Field(default_factory=list)

    #: Set by ``comparison_service.filter_products`` for every inclusion
    #: feature that could not be applied at all - see
    #: ``text_matching.can_ever_match``. Such a feature is named here so the
    #: result screen can honestly say "not applied" instead of showing an
    #: empty page of real listings that were never filtered out at all.
    ignored_features: list[str] = Field(default_factory=list)

    sort_preference: SortPreference = SortPreference.RELEVANCE

    # -- G25.2-B structured query intent -----------------------------------
    # Filled by ``services.query_intent.apply_intent`` AFTER the
    # conversation merge (``handlers.search._search`` /
    # ``SearchService.find_products_from_text``). Optional with safe
    # defaults: every existing caller, stored payload and JSON round-trip
    # keeps working unchanged. Stage 2 carries them as structured state /
    # diagnostics only - no relevance code reads them yet (that is Stage 3).
    intent_product_type: str | None = None
    intent_brand: str | None = None
    intent_model: str | None = None
    intent_generation: str | None = None
    intent_required_attributes: list[str] = Field(default_factory=list)
    intent_compatibility: list[str] = Field(default_factory=list)
    intent_excluded_product_types: list[str] = Field(default_factory=list)
