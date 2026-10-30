"""Search orchestration: natural language -> providers -> ranked products.

Responsibilities of this service:
- decide which marketplace providers participate in a search;
- run provider searches concurrently and tolerate individual failures;
- apply programmatic filtering/sorting (``services.comparison_service``);
- drive the discovery modes (feed / categories / image search)
  through the same provider interface;
- own one refill of the «✨ Находки» feed (``find_feed_page``): ONE
  documented page of AliExpress' own broad listing (``product.query``
  asked WITHOUT ``keywords`` and WITHOUT ``sort``) - normalize,
  deduplicate, keep only technically valid cards (real image + real
  affiliate URL) and skip what this user has already been shown. The
  composition and the order of that feed are AliExpress' own; this
  service never ranks, scores or quality-filters it (G24).

It never produces product data itself and never lets one failing
marketplace break the whole search.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field

from config.catalog import Category
from config.settings import settings as app_settings
from models.product import Product
from models.search_request import SearchMode, SearchRequest, SortPreference
from services.ai_service import AIService
from services.comparison_service import (
    ProductComparison,
    build_comparison,
    filter_products,
    sort_products,
)
from services import feed_curation, query_variants
from services.marketplaces.base import MarketplaceProvider
from services.product_identity import deduplicate_products, identity_pair
from services.product_logging import (
    log_ai_selection_outcome,
    log_final_pool,
    log_pipeline_stage_counts,
)
from services.query_intent import apply_intent, parse_query_intent
from services.relevance import (
    _mentions_only,
    confirms_numeric_spec,
    dominant_script,
    filter_relevant,
    has_meaningful_title,
    has_valid_product_url,
    relevance_tier,
)
from services.search_diagnostics import (
    SEARCH_DIAGNOSTICS,
    classify_outcome,
    safe_record,
)

logger = logging.getLogger(__name__)

#: Hard cap on how many products are kept in one outcome.
DEFAULT_MAX_RESULTS = 30

#: How many RAW marketplace listings one search gathers so the AI selector has
#: something to actually choose from - picking the best 20 out of 20 is not a
#: selection at all. The pool is filled from the documented result pages of the
#: SAME grounded phrase (the provider's own page size is 50, the documented
#: maximum), so 60-80 candidates normally cost two documented ``product.query``
#: calls. Only a search WITH an AI selector raises its target to this number:
#: without one the browse cap above stays the target, so the deterministic path
#: keeps both its breadth and its API cost.
RAW_POOL_TARGET = 80

#: How many cards an AI selection keeps for the carousel - the "строго 20 лучших"
#: rule of the feed. It applies to a real selection only; a deterministic list
#: keeps ``DEFAULT_MAX_RESULTS``, because an AI outage must not shrink a feed.
AI_FEED_RESULTS = 20
#: Hard wall-clock bound on ONE AI feed-selection request. The selector already
#: degrades to the deterministic list on any failure (``apply_feed_selection``),
#: but before this bound a slow Gemini answer (a hung request up to the 20s
#: ``AIService.timeout_seconds``) held the FINISHED product list hostage for the
#: whole span - the observed 26.5s search had 20.0s of it inside this one call.
#: The bound is deliberately shorter than the model timeout: a selection that
#: cannot answer in time loses its chance and the deterministic list ships
#: immediately. ``asyncio.TimeoutError`` is an alias of ``TimeoutError`` on
#: 3.11+ (the same convention ``services.ai_service`` documents).
FEED_SELECTION_TIMEOUT_SECONDS = 8.0

#: How many tier-0 (unverified-relevance) candidates may FILL the tail of
#: the agent path's final list after every verified product has been kept
#: (see ``_final_relevance_cut``). A bounded fill keeps a thin verified
#: pool from looking empty without shipping the unverified deep-pool tail
#: as if it were relevant.
TIER0_FILL_LIMIT = 5

#: How many MENTION-ONLY tier-1 candidates (a title that merely mentions the
#: requested noun while leading with something else - an accessory, another
#: device) may ship when NO genuinely related (head-noun) product exists in
#: the pool (G25.2-F). A direct product request must not be answered with a
#: full page of things that merely reference the noun - yet an EMPTY screen
#: is manufactured dishonesty too (the G25.2-E live regression: 15 verified
#: tier-1 candidates, 0 shipped), so a small bounded honest list ships.
MENTION_ONLY_FILL_LIMIT = 5

#: How many otherwise-cut candidates a CONFIRMED explicit numeric
#: specification may rescue from a pool truncation (G25.2-C Stage 12 / R3).
#: The Stage-9 audit showed correct numeric-spec candidates (C3 «повербанк
#: 20000 мАч 65W», E2, F2) sitting beyond the pool cutoff and dying before
#: final curation because of their position alone. The rescue is strictly
#: bounded by this constant, never grows the cut beyond its ``limit`` and
#: never moves a candidate above a higher relevance tier - a boundary
#: tie-break, not a new ranking layer (the Stage-11 score bonus keeps its
#: own meaning in ``services.relevance``).
NUMERIC_POOL_PROTECTION_LIMIT = 2

#: How many product cards are sent to Telegram at a time. Results are
#: paginated ("Показать ещё") instead of dumping everything at once.
DEFAULT_PAGE_SIZE = 5

#: How deep one product search may go into the marketplace result pages
#: (documented ``page_no``). Page 1 is the ordinary answer; while the relevant
#: pool is still short of the result cap the SAME phrase is asked for its next
#: page, so a thin first page cannot starve the pool the user browses.
#: Bounded on purpose: the price of depth is at most ``MAX_SEARCH_PAGES - 1``
#: extra documented ``product.query`` calls per search, never a loop that
#: keeps asking. ``SEARCH_MAX_PAGES`` is the deployment's own value (this is
#: only the default), and only a real product search goes deeper - a category,
#: a feed or a photo takes its breadth from its own pipeline (see
#: ``query_variants``).
MAX_SEARCH_PAGES = 3

#: Stems of MOOD adjectives ("прикольное", "необычные", "полезные") and
#: filler pronouns/adverbs. Stems, not full words, so inflections are
#: covered without a morphology dependency.
_DISCOVERY_MOOD_STEMS = (
    "прикольн", "необычн", "интересн", "странн", "удив", "полезн", "классн",
    "крут", "люб", "хорош", "лучш", "дешев", "дешёв", "стильн", "забавн",
    "популярн", "нов", "супер",
)

#: Words (exact) that describe no topic at all.
_DISCOVERY_FILLER_WORDS = frozenset(
    {
        "что", "нибудь", "что-нибудь", "что-то", "чтото", "хочу", "хотел",
        "хотела", "покажи", "показать", "найди", "найдите", "мне", "меня",
        "посоветуй", "варианты", "вариант", "идеи", "идея", "находки", "находка",
        "товары", "товар", "вещи", "вещь", "подборка", "штуки", "штука", "для",
        "про", "около", "которых", "которые", "который", "раньше", "видел",
        "видела", "еще", "ещё", "бы", "или", "но", "и", "в", "на", "с",
    }
)

#: Fallback seed when a discovery message contains no product words at all
#: ("удиви меня"): a broad query the marketplace answers with a real,
#: diverse set of listings.
DEFAULT_DISCOVERY_SEED = "популярные товары"

_DISCOVERY_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def discovery_seed(text: str) -> str:
    """Broad marketplace keywords derived from a discovery message.

    Only words the user actually wrote survive (minus mood words), so a
    request can never introduce a topic the user did not mention. When
    nothing but mood remains ("удиви меня"), a documented default seed is
    used and the caller still labels the screen as discovery.
    """

    words = []
    for word in _DISCOVERY_WORD_RE.findall(text or ""):
        lowered = word.lower()
        if len(lowered) <= 2 or lowered in _DISCOVERY_FILLER_WORDS:
            continue
        if any(lowered.startswith(stem) for stem in _DISCOVERY_MOOD_STEMS):
            continue
        words.append(lowered)
    seed = " ".join(words)[:100].strip()
    return seed or DEFAULT_DISCOVERY_SEED


#: G24.8 P1.9: the ONE validated extra retrieval keyword a category asks the
#: marketplace in addition to its normal category query. Keys are the stable
#: ``Category.code`` identifiers of ``config.catalog``; every value is a
#: keyword that same category ALREADY declares in its own ``keywords``
#: tuple, so no new wording is invented anywhere.
#:
#: Exactly the three candidates G24.8 P1.8-C validated end to end
#: («P1.8-CANDIDATE-VALIDATION PASS — PRODUCTION PATCH READY»): HOME «быт»,
#: ELECTRONICS «колонка», GARDEN «шланг». TOOLS is deliberately absent -
#: P1.8-B found no SAFE keyword for it and that absence is part of the
#: validated result. The map is a deterministic constant on purpose: no
#: fuzzy matching, no automatic keyword selection, no user terms, no model
#: output. Adding a category here is a deliberate product decision.
CATEGORY_EXPANSION_KEYWORDS: dict[str, str] = {
    "home": "быт",
    "electronics": "колонка",
    "garden": "шланг",
}


def category_expansion_phrase(request: SearchRequest) -> str | None:
    """The validated expansion keyword of a CATEGORY request, else ``None``.

    Only ``SearchMode.CATEGORY`` carrying a mapped ``category`` code may
    answer a phrase: SEARCH, POPULAR, FEED, DISCOVERY, IMAGE, PROMO and
    every category without a validated candidate behave exactly as before
    (G24.8 P1.9 mode isolation). The caller sends the keyword through the
    SAME provider request construction as the primary category query - the
    request is copied with only ``normalized_query`` replaced - so country,
    currency, language, page size, page number, ``ship_to_country`` and
    every other parameter stay identical (P1.4/P1.8-C behavior preserved).
    """

    if request.mode is not SearchMode.CATEGORY or not request.category:
        return None
    return CATEGORY_EXPANSION_KEYWORDS.get(request.category) or None


@dataclass
class SearchOutcome:
    """Everything the UI needs to render (and page through) one result set."""

    request: SearchRequest
    products: list[Product] = field(default_factory=list)
    comparisons: list[ProductComparison] = field(default_factory=list)
    searched_marketplaces: list[str] = field(default_factory=list)
    failed_marketplaces: list[str] = field(default_factory=list)
    unavailable_marketplaces: list[str] = field(default_factory=list)
    products_before_filtering: int = 0

    #: How many results the relevance filter removed (see
    #: ``services.relevance``) and how many were hidden because the
    #: marketplace returned no affiliate link for them. Both are reported
    #: instead of being silently swallowed.
    products_dropped_as_irrelevant: int = 0
    products_without_affiliate_url: int = 0

    #: How many products this open hid because THIS user had already been
    #: shown them (the persistent per-user history). Reported instead of
    #: being silent, and reset to 0 when the history is ignored because it
    #: would have emptied an otherwise valid feed.
    products_hidden_as_seen: int = 0

    #: B4 diagnostics: how many candidates remained after the EXPLICIT
    #: user constraints of ``services.comparison_service.filter_products``
    #: (price / rating / orders). Together with ``products_before_filtering``
    #: and ``products_dropped_as_irrelevant`` this attributes an empty
    #: result to the stage that actually emptied it; it never influences
    #: ranking, filtering or what the user sees.
    products_after_constraints: int = 0

    #: B4 cross-language diagnostics: the raw candidate titles split by
    #: writing system relative to the query's own dominant script
    #: (same / different / unknown-tie-or-no-letters, via
    #: ``services.relevance.dominant_script``). A lightweight aggregate
    #: proxy only - nothing here is used to judge relevance, and a
    #: different-script title is NOT treated as irrelevant by it.
    titles_same_script: int = 0
    titles_other_script: int = 0
    titles_unknown_script: int = 0

    page_size: int = DEFAULT_PAGE_SIZE
    page: int = 0

    @property
    def found_count(self) -> int:
        return len(self.products)

    @property
    def has_partial_failures(self) -> bool:
        return bool(self.failed_marketplaces or self.unavailable_marketplaces)

    @property
    def visible_products(self) -> list[Product]:
        """The slice of products belonging to the current page."""

        start = self.page * self.page_size
        return self.products[start : start + self.page_size]

    @property
    def has_more(self) -> bool:
        """Whether another page exists behind the current one."""

        return (self.page + 1) * self.page_size < len(self.products)

    def next_page(self) -> bool:
        """Advance the page cursor. Returns ``False`` if already at the end."""

        if not self.has_more:
            return False
        self.page += 1
        return True


def _cut_with_numeric_protection(
    products: list[Product],
    limit: int,
    request: SearchRequest,
) -> list[Product]:
    """Truncate ``products`` to ``limit``, rescuing confirmed numeric specs.

    The Stage-9 retrieval audit (C3/E2/F2): a candidate whose title confirms
    the user's explicit numeric specification was retrieved, relevant and
    ranked - and then discarded by the pool cut purely because of its
    position. This function IS that cut, with one bounded exception:

    - the output never exceeds ``limit`` (the pool is never expanded) and
      keeps the input's relative order except for the rescued boundary
      slots, so ranking semantics stay untouched;
    - only an already relevance-filtered, ranked input is expected (the
      pipeline passes ``sort_products``/``spread_similar`` output), so
      numeric evidence can NEVER make an ineligible product eligible;
    - gating is SEARCH mode only; the positive-evidence check delegates to
      ``relevance.confirms_numeric_spec`` - the very Stage-11 matcher
      («65W» ≡ «65 W», no conversions, a bare number never qualifies), so
      no second numeric parser exists and a missing spec is neutral;
    - at most :data:`NUMERIC_POOL_PROTECTION_LIMIT` tail candidates are
      rescued, in ranked order, each taking the slot of the worst-ranked
      kept card whose relevance tier is NOT higher than the rescuing
      candidate's own tier - a lower tier can never displace or outrank a
      higher one;
    - a near-identical title (``feed_curation.NEAR_DUPLICATE_THRESHOLD``)
      of an already kept or already rescued card never takes a protected
      slot, so duplicates and near-duplicates keep their existing
      ``spread_similar`` behaviour;
    - with no qualifying candidate (or any other mode / no structured
      numeric intent) the result is exactly ``products[:limit]``.
    """

    limit = max(0, int(limit))
    head = list(products[:limit])
    if len(products) <= limit:
        return head
    if request.mode is not SearchMode.SEARCH:
        return head

    # Positive evidence only, scanned past the cut in ranked order; the
    # helper answers False for every product when the request carries no
    # structured numeric attribute, so the scan simply finds nothing.
    rescued: list[Product] = []
    for candidate in products[limit:]:
        if not confirms_numeric_spec(candidate, request):
            continue
        if any(
            feed_curation.similarity(candidate.title, kept.title)
            >= feed_curation.NEAR_DUPLICATE_THRESHOLD
            for kept in [*head, *rescued]
        ):
            continue
        rescued.append(candidate)
        if len(rescued) >= NUMERIC_POOL_PROTECTION_LIMIT:
            break
    if not rescued:
        return head

    kept = list(head)
    placed: list[Product] = []
    for candidate in rescued:
        # The slot comes from the tail of the kept list, and only a card
        # whose tier is not higher than the rescuing candidate's may give
        # it up; the rescuing card is appended at the very end, so it
        # never outranks anything that stays in the pool.
        slot = next(
            (
                index
                for index in range(len(kept) - 1, -1, -1)
                if relevance_tier(kept[index], request)
                <= relevance_tier(candidate, request)
            ),
            None,
        )
        if slot is None:
            continue
        kept.pop(slot)
        placed.append(candidate)
    return kept + placed


def _final_relevance_cut(
    products: list[Product],
    limit: int,
    request: SearchRequest,
) -> list[Product]:
    """Agent-path final selection: VERIFIED relevance over the deep pool.

    When the AI feed selection is skipped (the agent tool path), the deep
    pool is no longer handed to a model - and a plain top-``limit`` cut is
    NOT a substitute for relevance selection: a broad marketplace answer
    fills the tail of the pool with tier-0 listings the relevance gate
    could not verify (cross-language titles without a romanized match,
    rental stations, accessories), and a raw cut ships them to the user
    next to the genuinely matching products.

    The deterministic evidence ALREADY exists on every candidate
    (:func:`relevance.relevance_tier`: 2 = the product-type gate verified
    the head noun, 1 = word-level or verified cross-writing match, 0 = no
    comparable evidence), so the final selection re-uses it at zero model
    cost:

    - every VERIFIED product (tier >= 1) keeps its ranked order, up to
      ``limit`` - a verified match can never be displaced by an unverified
      one;
    - tier-0 candidates fill at most :data:`TIER0_FILL_LIMIT` trailing
      slots, so a thin verified pool still opens on real products without
      pretending the unverified tail is relevant;
    - MENTION-ONLY tier-1 candidates (a title that merely mentions the
      requested noun while leading with an unrelated product - an accessory
      or another device, G25.2-F) NEVER headline the answer at full
      breadth: when the pool holds ONLY such candidates, a BOUNDED honest
      head (:data:`MENTION_ONLY_FILL_LIMIT`) ships instead of an empty
      screen (the G25.2-E live regression: 15 verified tier-1 candidates
      shipped 0). Beside related products they stay out of the selection
      entirely (they are tier-1 evidence, not product identity).
    - with NO verified product at all the previous deterministic cut
      (``_cut_with_numeric_protection``) applies unchanged - an empty
      screen is never manufactured, and a request the pipeline cannot
      verify keeps its previous behaviour.

    Nothing is invented: only products the deterministic pipeline already
    ranked are kept, dropped or reordered by their own tier.
    """

    limit = max(0, int(limit))
    if len(products) <= limit and not any(
        _mentions_only(product, request) for product in products
    ):
        return list(products)
    if request.mode is not SearchMode.SEARCH:
        return _cut_with_numeric_protection(products, limit, request)

    verified = [
        product for product in products
        if relevance_tier(product, request) >= 1
    ]
    if not verified:
        # Nothing the pipeline could verify: the honest previous behaviour.
        return _cut_with_numeric_protection(products, limit, request)

    # G25.2-F (mention-only fallback): tier-1 evidence is NOT one kind of
    # thing. A title whose HEAD noun is the requested product ("exact"
    # downgraded by a subtype/feature guard) is a RELATED product; a title
    # that merely MENTIONS the noun somewhere in the middle (classify
    # "mentioned" AND a leading noun unrelated to the request) is an
    # accessory or another device - fine as FILLER behind verified products,
    # never the headline of an answer to a DIRECT product request.
    related = [
        product for product in verified
        if not _mentions_only(product, request)
    ]
    mention_only = [
        product for product in verified if _mentions_only(product, request)
    ]
    if not related and not mention_only:
        # Tier-1 verified pool vanished (nothing matched) - the honest
        # previous behaviour.
        return _cut_with_numeric_protection(products, limit, request)
    if not related:
        # ONLY mention-only candidates survived: a direct product request is
        # NOT answered by things that merely reference the noun at full
        # breadth, but an EMPTY screen is manufactured dishonesty too - the
        # G25.2-E live regression (2026-10-25, «Мощный повербанк»: 15
        # verified tier-1 candidates, 0 shipped). Ship a BOUNDED, ranked
        # head of the mention-only list instead: the user still sees real
        # marketplace candidates (ranked below every genuine product by the
        # tier order), the empty-results/clarification flow stays available
        # whenever even these are gone. The existing ``limit`` is respected:
        # a smaller cap never grows the selection.
        selected = mention_only[: min(MENTION_ONLY_FILL_LIMIT, limit)]
    else:
        selected = related[:limit]
        if len(selected) < limit:
            chosen_ids = {id(product) for product in selected}
            filler = [
                product for product in products
                if id(product) not in chosen_ids
                and relevance_tier(product, request) < 1
            ][:TIER0_FILL_LIMIT]
            selected.extend(filler)
    # Truthful per-category counts of what is ACTUALLY in ``selected`` (the
    # available-pool arithmetic of the old "verified + unverified fill"
    # line reported capacity limits and negative fills once the
    # mention-only branch existed - G25.2-E log defect).
    selected_related = sum(1 for p in selected if not _mentions_only(p, request))
    selected_mention = sum(1 for p in selected if _mentions_only(p, request))
    selected_tier0 = len(selected) - selected_related - selected_mention
    logger.info(
        "Final relevance cut for %r: selected related=%d mention-only=%d "
        "tier0-fill=%d (available: related=%d mention-only=%d, pool %d -> %d)",
        request.original_query,
        selected_related,
        selected_mention,
        selected_tier0,
        len(related),
        len(mention_only),
        len(products),
        len(selected),
    )
    return selected


class SearchService:
    """Coordinates AI parsing and marketplace providers."""

    def __init__(
        self,
        providers: list[MarketplaceProvider],
        ai_service: AIService | None = None,
        max_results: int = DEFAULT_MAX_RESULTS,
        max_search_pages: int = MAX_SEARCH_PAGES,
    ) -> None:
        self.providers = providers
        self.ai_service = ai_service
        self.max_results = max_results

        #: The AI's FEED selector, when the configured service really has one
        #: (``AIService.select_feed_candidates``). It is looked up by name on
        #: purpose: a test double - or a service without the selector - simply
        #: has none, and then the deterministic pipeline runs exactly as it did
        #: before (no deeper retrieval, no AI round-trip).
        selector = getattr(ai_service, "select_feed_candidates", None)
        self._feed_selector = selector if callable(selector) else None

        #: How deep this deployment may walk the marketplace's documented result
        #: pages (``SEARCH_MAX_PAGES``). Bounded here rather than inside the
        #: provider so an operator can trade breadth against API usage; the
        #: default is the documented production depth.
        self.max_search_pages = max(1, int(max_search_pages or 1))

        #: Diagnostics of the LAST «✨ Находки» page fetch for the
        #: admin screen: rows received, exact duplicates, technical
        #: drops (no image / no affiliate link), history skips and the
        #: final count. ``None`` until the feed fetched a page once.
        self.last_feed_stats: dict | None = None

    @property
    def has_feed_selector(self) -> bool:
        """Whether an AI feed selection can really run right now.

        A selector exists (the service exposes it) AND the service reports itself
        as available. The availability check matters for cost, not for
        correctness: without an API key every retrieval would otherwise gather a
        deep pool that nothing is going to filter.
        """

        if self._feed_selector is None:
            return False
        return bool(getattr(self.ai_service, "is_available", True))

    @property
    def pool_target(self) -> int:
        """How many raw candidates one search gathers before the final cut.

        With an AI selector the pool must be DEEP (:data:`RAW_POOL_TARGET`,
        60-80 listings), because the model is asked to reject junk and keep the
        best 20: a pool of exactly 20 gives it nothing to choose between. Without
        a selector the browse cap IS the target, so the deterministic path keeps
        its previous breadth and its previous number of API calls.
        """

        if not self.has_feed_selector:
            return self.max_results
        return max(self.max_results, RAW_POOL_TARGET)

    def applicable_providers(self, request: SearchRequest) -> list[MarketplaceProvider]:
        """Providers that are allowed to serve this request.

        Honours an explicit marketplace preference; without one every
        registered provider participates.
        """

        selected: list[MarketplaceProvider] = []
        for provider in self.providers:
            if (
                request.preferred_marketplaces
                and provider.marketplace not in request.preferred_marketplaces
            ):
                continue
            selected.append(provider)
        return selected

    async def find_products(
        self,
        request: SearchRequest,
        *,
        exclude: set[tuple[str, str]] | None = None,
        broaden: bool = True,
        feed_selection: bool = True,
        raw: bool = False,
        raw_broaden: bool = False,
    ) -> SearchOutcome:
        """Run the full search pipeline for an already-parsed request.

        ``exclude`` carries the products this user has already been shown
        (see ``Database.get_shown_products``): they are removed after the
        relevance filter and before ranking, so a repeated search keeps
        returning NEW relevant products while any remain.

        ``broaden`` asks the request's GROUNDED retrieval variants
        (``services.query_variants``) until the relevant pool is full: one
        narrow phrase is a weak way to look for a product a human
        described in several words. The extra queries are bounded (at
        most ``MAX_PRODUCT_QUERY_VARIANTS`` documented marketplace calls
        per search), and EVERY candidate from EVERY variant is merged and
        then judged by the SAME relevance gate - broader retrieval never
        buys weaker relevance, and a variant never contains a word the
        user did not write.

        ``feed_selection=False`` skips ONLY the final AI feed selection
        (the deterministic cut ships instead - exactly the list an AI
        timeout or failure would have produced). Retrieval breadth, depth
        and every relevance/ranking stage are untouched; the agent path
        uses it because the agent turn already spends its own Gemini calls
        and re-curates the result afterwards.

        Flows that build their own breadth pass ``broaden=False`` (photo
        search runs its own grounded image hypotheses), and a request in
        a non-search mode (category, popular, discovery, image) keeps its
        single primary query by design - see ``query_variants``. The one
        validated exception is a MAPPED category (G24.8 P1.9): it
        additionally asks its single approved keyword - see
        ``CATEGORY_EXPANSION_KEYWORDS`` - whose raw rows join the same pool
        before the one relevance/ranking/curation pass over the union.

        RETRIEVAL DEPTH: one page of one phrase is a weak way to build a pool
        as well. The marketplace answers a narrow phrase with a thin first
        page, the relevance gate correctly removes everything that is not the
        requested product, and the user is left with a handful of cards. So
        the phrase the user actually asked with is asked AGAIN for its next
        documented page while the relevant pool is still short of the result
        cap, and never beyond the configured depth (:data:`MAX_SEARCH_PAGES` by
        default, ``SEARCH_MAX_PAGES`` at runtime). Depth can only add
        real listings of that same phrase - no new word, no weaker gate - and
        it costs at most ``SEARCH_MAX_PAGES - 1`` extra calls per search.

        AI SELECTION (the final step): when the configured AI service exposes
        ``select_feed_candidates``, the deep pool is handed to the model, which
        drops the junk of a broad keyword (accessories, spare parts, cheap spam,
        low-rated listings) and returns at most ``AI_FEED_RESULTS`` best cards -
        the list the carousel then walks. Without a selector, a failed request or
        an unusable answer the deterministic list survives, cut to the browse cap.
        """

        applicable = self.applicable_providers(request)
        active = [provider for provider in applicable if provider.is_configured]
        unavailable = [
            provider.display_name
            for provider in applicable
            if not provider.is_configured
        ]
        # RAW retrieval (the conversational engine's adapter): the provider
        # returns normalized and deduplicated rows only. No relevance gate,
        # ranking, curation or AI selection runs here. The conversational
        # adapter explicitly opts into the bounded grounded query variants;
        # other raw callers keep the historical one-phrase behavior.
        broaden = broaden and (not raw or raw_broaden)
        # B4: one monotonic timestamp per search; both return paths report
        # the finished outcome through ``_record_search_outcome``.
        t_search_started = time.perf_counter()
        # The phrase the marketplace is actually asked (B4 parse-failure
        # signal only: an EMPTY effective phrase means parsing/intent
        # produced nothing to search with).
        primary_phrase = " ".join(
            (request.normalized_query or request.original_query or "").split()
        )

        if not active:
            logger.warning(
                "No marketplace provider is configured; returning no products for %r",
                request.original_query,
            )
            outcome = SearchOutcome(request=request, unavailable_marketplaces=unavailable)
            self._record_search_outcome(
                outcome, t_search_started, query_empty=not primary_phrase
            )
            return outcome

        variants = (
            query_variants.product_query_variants(request) if broaden else []
        ) or [
            " ".join((request.normalized_query or request.original_query or "").split())
        ]
        primary = variants[0]

        # G24.8 P1.9: a category with a P1.8-C validated candidate asks its
        # ONE extra keyword as a second phrase of the SAME search («primary
        # query + validated keyword query -> union -> the existing
        # pipeline»). The extra phrase rides the ordinary variant loop below:
        # its raw rows land in ``pairs`` and are merged by the EXISTING
        # identity deduplication (``_deduplicate``), then relevance,
        # category ranking, spread and the curation gate all run ONCE over
        # the combined pool - never a second pipeline, never a re-sort of
        # the expansion pool alone. ``category_expansion_phrase`` restricts
        # this to mapped CATEGORY requests, so SEARCH / POPULAR / FEED /
        # DISCOVERY / IMAGE and every unmapped category are untouched; a
        # ``broaden=False`` flow that owns its own breadth is left alone.
        expansion = category_expansion_phrase(request) if broaden else None
        if expansion and expansion.casefold() == primary.casefold():
            expansion = None  # the primary already IS this question
        if expansion:
            variants = [*variants, expansion]
        # Raw rows each phrase returned (tracked while a category expansion
        # is active - only for the one runtime log line of that call).
        raw_rows_by_phrase: dict[str, int] = {}

        # RETRIEVAL POOL: how many raw candidates this search gathers before the
        # final cut. An AI selector needs a deep, junk-rich pool to pick the best
        # 20 from (``RAW_POOL_TARGET``); without one the browse cap is the target
        # and nothing about the previous breadth changes (see ``pool_target``).
        # Only a retrieval that is ALSO the one asking for the AI selection pays
        # for the depth: a flow that owns its own breadth (the photo hypotheses)
        # composes its pool from several of these calls and asks for the
        # selection itself, so its calls stay at the browse cap.
        pool_target = self.pool_target if broaden else self.max_results

        # RETRIEVAL DEPTH: the documented pages this search may ask, starting
        # at the page the request carries. A flow that owns its own breadth
        # (``broaden=False``, e.g. the photo hypotheses) or a non-search mode
        # keeps exactly one page - the same rule that keeps its phrase list at
        # one element - so the number of marketplace calls stays bounded by
        # whatever that flow already promised. How deep a deployment may walk
        # at all is its own setting (``SEARCH_MAX_PAGES``).
        first_page = min(max(1, int(request.page_no or 1)), self.max_search_pages)
        pages = (
            list(range(first_page, self.max_search_pages + 1))
            if broaden and not raw and request.mode is SearchMode.SEARCH
            else [first_page]
        )

        pairs: list[tuple[MarketplaceProvider, object]] = []
        outcome = SearchOutcome(
            request=request, unavailable_marketplaces=list(unavailable)
        )
        pages_used = 0
        all_failed = False

        for page_no in pages:
            # Page 1 asks every grounded phrase; a deeper page asks the PRIMARY
            # phrase again - the words the user actually asked with - so depth
            # never multiplies the variant list.
            page_variants = variants if page_no == pages[0] else variants[:1]
            pages_used = page_no

            for variant in page_variants:
                # The primary query on the request's own page keeps the
                # parser's request object; a further variant (or a deeper
                # page) only ever replaces the marketplace phrase / the page
                # number, so every constraint, price bound and keyword of the
                # request travels with it unchanged.
                if variant == primary and page_no == request.page_no:
                    variant_request = request
                else:
                    variant_request = request.model_copy(
                        update={"normalized_query": variant, "page_no": page_no}
                    )
                results = await asyncio.gather(
                    *(
                        self._timed_provider_call(
                            provider, provider.search_products(variant_request)
                        )
                        for provider in active
                    ),
                    return_exceptions=True,
                )
                pairs.extend(zip(active, results, strict=True))
                if all(isinstance(result, BaseException) for result in results):
                    # Every provider raised on this round: nothing was
                    # retrieved and nothing can be - deeper pages or further
                    # variants would only repeat the same failing calls.
                    logger.warning(
                        "All providers failed for %r on page %d; stopping this "
                        "search instead of retrying identical calls",
                        request.original_query,
                        page_no,
                    )
                    all_failed = True
                if expansion:
                    raw_rows_by_phrase[variant] = sum(
                        len(result)
                        for result in results
                        if isinstance(result, list)
                    )

                # The pipeline runs over everything retrieved SO FAR, so the
                # early stop below is decided on the RELEVANT pool instead of
                # on how many raw rows one narrow phrase happened to match.
                # RAW retrieval (``raw=True``) MUST NOT run this stage at all:
                # relevance/constraint filtering belongs to the conversational
                # engine, and running ``_collect`` here only burned the legacy
                # stages (and their "Relevance filter …" log lines) on results
                # that were then discarded by ``_collect_raw``.
                if not raw:
                    outcome = self._collect(
                        request=request,
                        pairs=pairs,
                        outcome=SearchOutcome(
                            request=request, unavailable_marketplaces=list(unavailable)
                        ),
                        exclude=exclude,
                        cap=pool_target,
                    )
                    if expansion and variant == expansion:
                        # G24.8 P1.9: one concise line per category expansion -
                        # counts only, no product lists, no user-visible text.
                        # A failed extra call stays non-fatal by construction
                        # (``gather`` already captured it, ``_collect`` logged
                        # the provider failure with the shared code path): the
                        # primary pool below is exactly what it would have been.
                        failure = next(
                            (result for result in results
                             if isinstance(result, BaseException)),
                            None,
                        )
                        if failure is not None:
                            logger.warning(
                                "Category %s expansion %r failed (%s): "
                                "primary=%d, expansion=%d, union=%d - "
                                "keeping the primary pool",
                                request.category,
                                expansion,
                                failure,
                                raw_rows_by_phrase.get(primary, 0),
                                raw_rows_by_phrase.get(expansion, 0),
                                outcome.products_before_filtering,
                            )
                        else:
                            logger.info(
                                "Category %s expansion %r: primary=%d, "
                                "expansion=%d, union=%d",
                                request.category,
                                expansion,
                                raw_rows_by_phrase.get(primary, 0),
                                raw_rows_by_phrase.get(expansion, 0),
                                outcome.products_before_filtering,
                            )
                    # A mapped category always asks its validated expansion
                    # BEFORE the pool counts as full: the point of the extra
                    # keyword is the DIVERSITY of the union, and only the ranked
                    # union is cut to the cap. Every other search stops at the
                    # first full pool exactly as before (G24.8 P1.9).
                    if all_failed or (
                        len(outcome.products) >= pool_target
                        and (expansion is None or variant == expansion)
                    ):
                        break
                if raw and broaden:
                    raw_count = len(self._deduplicate(pairs))
                    if raw_count >= pool_target:
                        break
                if all_failed:
                    break
            raw_count = len(self._deduplicate(pairs)) if raw else 0
            if (
                all_failed
                or (not raw and len(outcome.products) >= pool_target)
                or (raw and broaden and raw_count >= pool_target)
            ):
                break

        if raw:
            outcome = self._collect_raw(
                request=request,
                pairs=pairs,
                outcome=outcome,
                exclude=exclude,
                cap=pool_target,
            )
            self._record_search_outcome(
                outcome, t_search_started, query_empty=not primary_phrase
            )
            return outcome

        if broaden and feed_selection:
            await self.apply_feed_selection(outcome)
        elif broaden:
            # The agent tool path: the AI selection is skipped (the agent turn
            # already spends its own Gemini calls), but the deterministic cut
            # is NOT a substitute for relevance selection. The FINAL
            # relevance selection runs here, on the finished deep pool, with
            # the SAME deterministic tier evidence the ranking used - zero
            # extra model calls: verified products first, a bounded
            # unverified fill behind them.
            logger.info(
                "AI feed selection skipped (agent path); final relevance "
                "cut for %r (pool %d)",
                request.original_query,
                len(outcome.products),
            )
            outcome.products = _final_relevance_cut(
                outcome.products, self.max_results, request
            )
        else:
            # A flow that owns its own breadth composes its pool from SEVERAL of
            # these calls (the photo hypotheses) and asks for the selection once,
            # on the merged list: deciding per hypothesis would spend a request
            # on a list that is not the final one.
            outcome.products = outcome.products[: self.max_results]

        # The explicitly requested result count («покажи 3 варианта») is a
        # CEILING on the delivered list, applied at the very end so every
        # relevance/constraint/curation stage above keeps its full pool. A
        # smaller relevant pool stays smaller — nothing is padded or
        # invented. No count in the request -> no change, and the existing
        # default browse cap stays exactly as before.
        requested = request.result_limit
        if requested is not None and len(outcome.products) > requested:
            logger.info(
                "Result count requested (%d) caps %d delivered products for %r",
                requested,
                len(outcome.products),
                request.original_query,
            )
            outcome.products = outcome.products[:requested]
            outcome.comparisons = build_comparison(outcome.products)

        if len(variants) > 1 or pages_used > pages[0]:
            logger.info(
                "Search %r used %d grounded retrieval variant(s) up to page %d "
                "of %d (pool %d)",
                request.original_query,
                len(variants),
                pages_used,
                self.max_search_pages,
                outcome.found_count,
            )
        self._record_search_outcome(
            outcome, t_search_started, query_empty=not primary_phrase
        )
        return outcome

    @staticmethod
    async def _timed_provider_call(provider: MarketplaceProvider, coro):
        """Await one provider call, measuring its latency for B4 diagnostics.

        A pure observation wrapper: the coroutine is created by the caller
        exactly as before, exceptions propagate unchanged (``asyncio.gather``
        still sees them), and the recording itself is fail-safe - a
        diagnostics bug can never fail or delay a search beyond the
        negligible timing overhead.
        """

        started = time.perf_counter()
        try:
            result = await coro
        except BaseException as exc:  # noqa: BLE001 - re-raised unchanged
            safe_record(
                SEARCH_DIAGNOSTICS.record_provider_call,
                provider.display_name,
                latency_ms=(time.perf_counter() - started) * 1000.0,
                error=exc,
            )
            raise
        rows = len(result) if isinstance(result, list) else None
        safe_record(
            SEARCH_DIAGNOSTICS.record_provider_call,
            provider.display_name,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            rows=rows,
        )
        return result

    def _record_search_outcome(
        self,
        outcome: SearchOutcome,
        started: float,
        *,
        query_empty: bool = False,
    ) -> None:
        """Classify + record one finished search (B4; never raises)."""

        duration_ms = (time.perf_counter() - started) * 1000.0
        label = classify_outcome(outcome, query_empty=query_empty)
        safe_record(
            SEARCH_DIAGNOSTICS.record_search,
            outcome,
            label=label,
            duration_ms=duration_ms,
            query_empty=query_empty,
        )
        # One compact, query-free line for the operator log: the label and
        # the stage counts answer "why was this result empty" without any
        # user text in the record.
        logger.info(
            "Search outcome=%s mode=%s raw=%d after_relevance=%d "
            "after_constraints=%d final=%d ms=%.0f",
            label,
            outcome.request.mode.value,
            outcome.products_before_filtering,
            max(
                0,
                outcome.products_before_filtering
                - outcome.products_dropped_as_irrelevant,
            ),
            outcome.products_after_constraints,
            outcome.found_count,
            duration_ms,
        )

    async def apply_feed_selection(self, outcome: SearchOutcome) -> None:
        """Let the AI drop the junk of a deep pool, keeping the best cards.

        The pool was gathered deep on purpose (:attr:`pool_target`): a broad
        keyword of a real marketplace answers with accessories, spare parts and
        cheap spam next to the wanted product, and only a model can judge which
        listing is "not the product" in the user's own words. What comes back is
        a SELECTION, not a reorder - the ids the model left out stay hidden (see
        ``feed_curation.apply_selection``) - and the result is capped at
        ``AI_FEED_RESULTS`` (never above the deployment's own browse cap).

        Called once per displayed feed, by the flow that OWNS the finished pool:
        ``find_products`` does it inline, while a flow that composes its pool from
        several calls (the photo hypotheses) asks for it on the merged list, so
        one decision costs one request.

        Degrades exactly like every other AI step: no selector, an unavailable
        service, a failed request, an unusable answer or a selection slower than
        :data:`FEED_SELECTION_TIMEOUT_SECONDS` all leave the deterministic list
        in place, cut to the browse cap. A feed can therefore never be emptied,
        reordered wrongly, padded by an AI failure - or held up by a slow one.
        """

        limit = max(1, min(AI_FEED_RESULTS, self.max_results))
        context_query = outcome.request.original_query or ""
        if not self.has_feed_selector:
            before = len(outcome.products)
            outcome.products = _cut_with_numeric_protection(
                outcome.products, self.max_results, outcome.request
            )
            log_ai_selection_outcome(
                logger,
                context=context_query,
                pool_size=before,
                chosen_ids=None,
                selected_size=len(outcome.products),
                fallback_reason="no AI selector configured",
            )
            log_final_pool(
                logger,
                outcome.products,
                context=context_query,
                origin="deterministic-fallback",
            )
            return

        pool = list(outcome.products)
        chosen: list[str] | None = None
        selector_failed = False
        feed_timed_out = False
        t_feed = time.perf_counter()
        try:
            chosen = await asyncio.wait_for(
                self._feed_selector(
                    pool,
                    wanted=limit,
                    query=outcome.request.normalized_query
                    or outcome.request.original_query
                    or None,
                ),
                timeout=FEED_SELECTION_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 - an AI failure must not break a search
            selector_failed = True
            feed_timed_out = isinstance(exc, TimeoutError)
            if feed_timed_out:
                logger.warning(
                    "AI feed selection timed out after %.1fs; keeping the "
                    "deterministic list",
                    FEED_SELECTION_TIMEOUT_SECONDS,
                )
            else:
                logger.exception("AI feed selection failed; keeping the deterministic list")
        # B4 latency diagnostics: how long the AI selection held the finished
        # pool (measurement only - the flow, its fallbacks and its list are
        # exactly as before).
        logger.info(
            "feed.select pool=%d dur_ms=%.0f failed=%s timed_out=%s",
            len(pool),
            (time.perf_counter() - t_feed) * 1000,
            selector_failed,
            feed_timed_out,
        )

        if not chosen:
            # DETERMINISTIC FALLBACK: this top-``max_results`` cut is the
            # exact Stage-9 loss point (pool 80 -> pre-curation 30), so it
            # carries the same bounded numeric-spec pool protection.
            outcome.products = _cut_with_numeric_protection(
                pool, self.max_results, outcome.request
            )
            if selector_failed:
                reason = "AI selector raised an exception"
            elif chosen is None:
                reason = "AI selector returned None/unusable answer"
            else:
                reason = "AI selector returned an empty/invalid id list"
            log_ai_selection_outcome(
                logger,
                context=context_query,
                pool_size=len(pool),
                chosen_ids=chosen,
                selected_size=len(outcome.products),
                fallback_reason=reason,
            )
            log_final_pool(
                logger,
                outcome.products,
                context=context_query,
                origin="deterministic-fallback",
            )
            return

        selected = feed_curation.apply_selection(pool, chosen, limit=limit)
        if not selected:
            # The model named nothing usable: the honest deterministic list is
            # better than an empty screen - still cut with the same bounded
            # numeric-spec pool protection as every deterministic fallback.
            outcome.products = _cut_with_numeric_protection(
                pool, self.max_results, outcome.request
            )
            log_ai_selection_outcome(
                logger,
                context=context_query,
                pool_size=len(pool),
                chosen_ids=chosen,
                selected_size=len(outcome.products),
                fallback_reason=(
                    "AI ids matched no pool product "
                    f"(ai_ids={len(chosen)}, pool={len(pool)})"
                ),
            )
            log_final_pool(
                logger,
                outcome.products,
                context=context_query,
                origin="deterministic-fallback",
            )
            return

        log_ai_selection_outcome(
            logger,
            context=context_query,
            pool_size=len(pool),
            chosen_ids=chosen,
            selected_size=len(selected),
        )
        log_final_pool(
            logger, selected, context=context_query, origin="ai-selected"
        )
        outcome.products = selected
        outcome.comparisons = build_comparison(selected)

    def _collect_raw(
        self,
        request: SearchRequest,
        pairs: list[tuple[MarketplaceProvider, object]],
        outcome: SearchOutcome,
        exclude: set[tuple[str, str]] | None = None,
        cap: int | None = None,
    ) -> SearchOutcome:
        """RAW retrieval collect: normalize, dedup, apply NOTHING else.

        The conversational engine's adapter asks for candidates, not for a
        decision: relevance, constraints, ordering and the shown-history all
        belong to ``services.convsearch`` (they are judged against the
        conversation's own ledger there). Only identity dedup and the
        documented retrieval cap run here, so the engine sees the honest
        breadth of what the marketplace actually returned.
        """

        for provider, result in pairs:
            if isinstance(result, BaseException):
                logger.error(
                    "%s lookup failed: %s", provider.display_name, result, exc_info=result
                )
                if provider.display_name not in outcome.failed_marketplaces:
                    outcome.failed_marketplaces.append(provider.display_name)
                continue
            if provider.display_name not in outcome.searched_marketplaces:
                outcome.searched_marketplaces.append(provider.display_name)

        collected = self._deduplicate(pairs)
        outcome.products_before_filtering = len(collected)
        if exclude:
            collected = [
                product for product in collected
                if identity_pair(product) not in exclude
            ]
        limit = self.max_results if cap is None else max(1, int(cap))
        outcome.products = collected[:limit]
        outcome.comparisons = build_comparison(outcome.products)
        logger.info(
            "RAW retrieval %r (%s): %d raw, %d shown (sources: %s)",
            request.original_query,
            request.mode.value,
            outcome.products_before_filtering,
            outcome.found_count,
            ", ".join(outcome.searched_marketplaces) or "none",
        )
        return outcome

    def _collect(
        self,
        request: SearchRequest,
        pairs: list[tuple[MarketplaceProvider, object]],
        outcome: SearchOutcome,
        exclude: set[tuple[str, str]] | None = None,
        cap: int | None = None,
    ) -> SearchOutcome:
        """Record sources, then deduplicate/filter/sort/paginate results.

        ``cap`` is how many products this pass may keep (the deep retrieval pool
        while an AI selection is still ahead, the browse cap otherwise).
        """

        for provider, result in pairs:
            if isinstance(result, BaseException):
                # One marketplace being down must not affect the others.
                logger.error(
                    "%s lookup failed: %s", provider.display_name, result, exc_info=result
                )
                if provider.display_name not in outcome.failed_marketplaces:
                    outcome.failed_marketplaces.append(provider.display_name)
                continue

            # A provider name is recorded ONCE, however many retrieval
            # variants asked it: the list reports WHICH marketplaces
            # answered, not how many calls were made.
            if provider.display_name not in outcome.searched_marketplaces:
                outcome.searched_marketplaces.append(provider.display_name)

        collected = self._deduplicate(pairs)
        outcome.products_before_filtering = len(collected)

        # B4 cross-language diagnostics: split the RAW candidate titles by
        # writing system relative to the query's own dominant script. Pure
        # observation over data already in memory; nothing is filtered or
        # reordered here, and a different-script title is NOT judged
        # irrelevant by this (R3's cross-writing evidence owns relevance).
        query_script = dominant_script(
            (request.normalized_query or request.original_query or "") or None
        )
        for product in collected:
            title_script = dominant_script(product.title)
            if query_script is None or title_script is None:
                outcome.titles_unknown_script += 1
            elif title_script == query_script:
                outcome.titles_same_script += 1
            else:
                outcome.titles_other_script += 1

        relevant, dropped = filter_relevant(collected, request)
        outcome.products_dropped_as_irrelevant = sum(dropped.values())
        if dropped:
            logger.info(
                "Relevance filter for %r removed %d of %d products (%s)",
                request.original_query,
                outcome.products_dropped_as_irrelevant,
                len(collected),
                ", ".join(f"{reason}={count}" for reason, count in sorted(dropped.items())),
            )

        filtered = filter_products(relevant, request)
        # B4: how many candidates the EXPLICIT constraints (price/rating/
        # orders) let through - the boundary that separates "constraints
        # removed everything" from "relevance removed everything".
        outcome.products_after_constraints = len(filtered)

        # The per-user history is applied HERE, at a documented point of the
        # pipeline: AFTER relevance and the request's own constraints (so an
        # already-seen relevant product is never swapped for an irrelevant
        # one - junk cannot fill the hole a seen product leaves) and BEFORE
        # ranking (so ordering only spends effort on what can be returned).
        # When the history would empty an otherwise valid result set it is
        # ignored for this open: showing the feed again is honest, an empty
        # screen is not.
        if exclude:
            unseen = [
                product for product in filtered if identity_pair(product) not in exclude
            ]
            hidden = len(filtered) - len(unseen)
            if unseen:
                filtered = unseen
                outcome.products_hidden_as_seen = hidden

        ranked = sort_products(filtered, request)

        # DIVERSITY, but strictly AFTER relevance (§3 priority order): the
        # ranking is computed first and stays the source of the order, then an
        # almost exact copy of an already accepted listing is moved behind the
        # different candidates so one product family cannot fill the first page
        # of cards. Nothing is dropped or invented - the copy is still returned,
        # on a later page of the same result set (``feed_curation``).
        ranked = feed_curation.spread_similar(ranked)

        limit = self.max_results if cap is None else max(1, int(cap))
        # POOL PROTECTION (Stage 12): the cut keeps its exact size and the
        # ranking's order, but a bounded number of already-eligible tail
        # candidates whose titles confirm the request's explicit numeric
        # specification may take the boundary slots instead of being
        # discarded here - see ``_cut_with_numeric_protection``.
        outcome.products = _cut_with_numeric_protection(ranked, limit, request)

        # A page cannot be larger than the result cap, otherwise a small
        # max_results would produce a single oversized page.
        outcome.page_size = min(outcome.page_size, max(1, self.max_results))
        outcome.comparisons = build_comparison(outcome.products)

        logger.info(
            "Lookup %r (%s): %d raw, %d after filtering, %d shown (sources: %s)",
            request.original_query,
            request.mode.value,
            outcome.products_before_filtering,
            len(filtered),
            outcome.found_count,
            ", ".join(outcome.searched_marketplaces) or "none",
        )
        # Per-stage drop counts in ONE line: an operator sees where products
        # died (relevance/filters -> final cut) without reading a stack.
        # Counts only - no product data, no secrets.
        log_pipeline_stage_counts(
            logger,
            request.original_query,
            [
                ("relevant", outcome.products_before_filtering, len(filtered)),
                ("shown", len(filtered), outcome.found_count),
            ],
        )
        return outcome

    @staticmethod
    def _deduplicate(pairs: list[tuple[MarketplaceProvider, object]]) -> list[Product]:
        """Merge provider results and drop duplicate listings.

        Identity lives in ``services.product_identity`` and is shared by
        every feed: providers repeat items inside one feed, and the same
        listing can come back through a different tracking/affiliate link.
        Doing this in the service layer means no handler has to solve it
        again (and none of them can forget to).
        """

        merged: list[Product] = []
        for _, result in pairs:
            if isinstance(result, BaseException) or result is None:
                continue
            merged.extend(result)  # type: ignore[arg-type]

        return deduplicate_products(merged)

    @staticmethod
    def _keep_affiliate_only(outcome: SearchOutcome, action: str) -> SearchOutcome:
        """Hide feed products the marketplace gave no affiliate link for.

        A feed exists to send the user to a listing through a real affiliate
        URL; without one the button would lead to a plain marketplace page,
        which the project forbids. Such items are removed - and the number
        is kept on the outcome, so showing fewer products is visible instead
        of being a silent lie about how much the feed held.
        """

        keep = [product for product in outcome.products if product.has_affiliate_url]
        outcome.products_without_affiliate_url = outcome.found_count - len(keep)
        outcome.products = keep
        outcome.comparisons = build_comparison(keep)

        if outcome.products_without_affiliate_url:
            logger.info(
                "%s: %d products hidden because the API returned no affiliate link",
                action,
                outcome.products_without_affiliate_url,
            )
        return outcome

    async def find_products_from_text(
        self,
        user_text: str,
        *,
        currency: str | None = None,
        exclude: set[tuple[str, str]] | None = None,
    ) -> SearchOutcome:
        """Parse a natural-language query and run the search.

        Requires an ``AIService``. Parsing failures are handled by the
        AI service itself (local fallback parser), so this method only
        fails on genuinely empty input. ``exclude`` is the per-user
        shown-product history (see :meth:`find_products`).
        """

        if self.ai_service is None:
            raise RuntimeError("SearchService requires an AIService to parse text queries")

        request = await self.ai_service.parse_query(user_text)
        # G25.2-B Stage 2: the SAME structured-intent layer as
        # handlers.search._search (one parser, one apply_intent - never two
        # implementations). DUPLICATION GUARD: _search() reaches this method
        # only through its ai_service-is-None early return, which happens
        # BEFORE _search's own intent call, and find_products() itself never
        # parses intent - so a request is intent-parsed exactly once on
        # either entry point. FALLBACK: on any unexpected failure the
        # pre-G25.2-B request is searched unchanged.
        try:
            intent, _intent_meta = await parse_query_intent(
                user_text, self.ai_service
            )
            # Stage 57: the intent-parse source only matters when a model is
            # configured; with no model the deterministic intent reconstruction
            # is the best query and must keep applying (see apply_intent).
            source = (
                _intent_meta.source
                if getattr(self.ai_service, "is_available", False)
                else None
            )
            request = apply_intent(request, intent, intent_source=source)
        except Exception:  # pragma: no cover - defensive: never break search
            logger.exception("Intent enrichment failed; searching without intent")
        # The stored currency preference applies only when the query
        # itself named no currency: "under $30" must keep USD so the
        # price limit stays in the currency the user wrote.
        if currency and not request.currency:
            request = request.model_copy(update={"currency": currency.upper()})
        return await self.find_products(request, exclude=exclude)

    async def find_feed_page(
        self,
        *,
        page: int = 1,
        country: str | None = None,
        currency: str | None = None,
        exclude: set[tuple[str, str]] | None = None,
        limit: int | None = None,
    ) -> SearchOutcome:
        """One documented page of the OFFICIAL AliExpress feed («✨ Находки»).

        G24: AliExpress composes the feed, this method only delivers it.
        The provider asks ``aliexpress.affiliate.product.query`` WITHOUT
        ``keywords`` and WITHOUT ``sort`` (see
        ``AliExpressProvider.fetch_feed_page``) - the marketplace's own
        broad listing in the marketplace's own order. What happens here is
        strictly mechanical, in this order:

        * normalize (the provider already mapped the documented fields);
        * deduplicate by canonical identity (``services.product_identity``);
        * technical validity only: a real product image and a real affiliate
          URL must exist - a card without either cannot be shown;
        * skip what THIS user has already been shown (``exclude``).

        NO popularity, quality, rating, orders, price or category judgement
        is made: the bot never re-decides what AliExpress put into its feed
        (spec §7/§24). Empty pages are an ordinary outcome, never an error.

        ``page`` is the documented 1-based ``page_no``; pagination is the
        API's own (live-verified: pages 1/2/40 share zero ids). The honest
        counters of this fetch land in ``last_feed_stats`` for /admin.
        """

        request = SearchRequest(
            original_query="находки AliExpress",
            mode=SearchMode.FEED,
            country=country,
            currency=currency,
            page_no=max(1, int(page or 1)),
        )
        outcome = SearchOutcome(request=request)

        applicable = self.applicable_providers(request)
        capable = [
            provider
            for provider in applicable
            if getattr(provider, "supports_feed", False)
        ]
        outcome.unavailable_marketplaces = [
            provider.display_name
            for provider in applicable
            if provider not in capable or not provider.is_configured
        ]

        active = [provider for provider in capable if provider.is_configured]
        if not active:
            logger.info("No marketplace can answer the product feed yet")
            return outcome

        results = await asyncio.gather(
            *(
                provider.fetch_feed_page(
                    page=request.page_no, currency=currency, country=country
                )
                for provider in active
            ),
            return_exceptions=True,
        )

        merged: list[Product] = []
        for provider, result in zip(active, results, strict=True):
            if isinstance(result, BaseException):
                # One marketplace being down must not affect the others.
                logger.error(
                    "%s feed page %s failed: %s",
                    provider.display_name,
                    request.page_no,
                    result,
                    exc_info=result,
                )
                if provider.display_name not in outcome.failed_marketplaces:
                    outcome.failed_marketplaces.append(provider.display_name)
                continue
            if provider.display_name not in outcome.searched_marketplaces:
                outcome.searched_marketplaces.append(provider.display_name)
            if isinstance(result, list):
                merged.extend(result)

        raw_rows = len(merged)
        products = deduplicate_products(merged)
        exact_duplicates = raw_rows - len(products)

        with_image = [product for product in products if product.image_url]
        dropped_no_image = len(products) - len(with_image)

        with_affiliate = [
            product for product in with_image if product.has_affiliate_url
        ]
        outcome.products_without_affiliate_url = (
            len(with_image) - len(with_affiliate)
        )

        kept = with_affiliate
        if exclude:
            unseen = [
                product
                for product in kept
                if identity_pair(product) not in exclude
            ]
            outcome.products_hidden_as_seen = len(kept) - len(unseen)
            kept = unseen

        if limit is not None and len(kept) > limit:
            kept = kept[: max(1, int(limit))]

        outcome.products = kept
        outcome.products_before_filtering = raw_rows
        outcome.comparisons = build_comparison(kept)

        self.last_feed_stats = {
            "endpoint": "aliexpress.affiliate.product.query",
            "page": request.page_no,
            "raw_rows": raw_rows,
            "exact_duplicates": exact_duplicates,
            "dropped_no_image": dropped_no_image,
            "dropped_no_affiliate": outcome.products_without_affiliate_url,
            "hidden_as_seen": outcome.products_hidden_as_seen,
            "final": len(kept),
        }
        logger.info(
            "Feed page %s: %d rows -> %d unique -> %d shown "
            "(no-image %d, no-affiliate %d, seen %d)",
            request.page_no,
            raw_rows,
            len(products),
            len(kept),
            dropped_no_image,
            outcome.products_without_affiliate_url,
            outcome.products_hidden_as_seen,
        )
        return outcome

    async def find_discovery(
        self,
        text: str,
        currency: str | None = None,
        limit: int | None = None,
        exclude: set[tuple[str, str]] | None = None,
    ) -> SearchOutcome:
        """Discovery query: real candidates, curated like the popular feed.

        Used for mood queries ("что-нибудь прикольное для машины", "удиви
        меня"). The candidates come from ONE normal marketplace search for a
        broad seed derived from the user's own words - no new provider, no
        new pipeline. Because the words describe a mood rather than a
        product, no word-level relevance filter is applied (the request is
        ``SearchMode.DISCOVERY``); instead the SAME curation the popular feed
        uses orders the real results:

        deduplicate -> optional AI ordering (validated) -> diversity. AI only
        ever reorders or drops real products.
        """

        seed = discovery_seed(text)
        # A discovery message may still carry explicit constraints ("до $20").
        # They are read by the DETERMINISTIC parser - no model quota is spent
        # on a mood feed - and applied to the real candidates below, so the
        # budget the user typed is never silently ignored.
        stated = AIService._fallback_parse(text or "")
        request = SearchRequest(
            original_query=text,
            normalized_query=seed,
            mode=SearchMode.DISCOVERY,
            max_price=stated.max_price,
            min_price=stated.min_price,
            min_rating=stated.min_rating,
            min_orders=stated.min_orders,
            currency=currency or stated.currency,
            sort_preference=SortPreference.POPULARITY_DESC,
        )

        outcome = SearchOutcome(request=request)
        applicable = self.applicable_providers(request)
        active = [provider for provider in applicable if provider.is_configured]
        outcome.unavailable_marketplaces = [
            provider.display_name
            for provider in applicable
            if not provider.is_configured
        ]
        if not active:
            logger.info("No configured provider for a discovery query")
            return outcome

        results = await asyncio.gather(
            *(provider.search_products(request) for provider in active),
            return_exceptions=True,
        )
        collected = self._collect(
            request=request,
            pairs=list(zip(active, results, strict=True)),
            outcome=outcome,
        )

        wanted = limit or self.max_results
        products = feed_curation.drop_near_duplicates(collected.products)

        # Per-user history, exactly as in the ordinary search: applied after
        # the relevance/constraint stage and before ranking, ignored when it
        # would empty the feed.
        if exclude:
            unseen = [
                product for product in products if identity_pair(product) not in exclude
            ]
            if unseen:
                collected.products_hidden_as_seen = len(products) - len(unseen)
                products = unseen

        # The strict selection rules apply to a discovery feed as well: the model
        # drops accessories, spare parts and cheap spam, and the ids it did NOT
        # name stay hidden - so the feed shows the best of the real pool instead
        # of everything a broad seed returned. ``rank_candidates`` answers
        # ``None`` whenever it cannot judge (no client, quota, unusable JSON) and
        # then the deterministic order and the full pool survive, exactly as
        # before: an AI outage can never empty this screen.
        order: list[str] | None = None
        if self.ai_service is not None:
            ranker = getattr(self.ai_service, "rank_candidates", None)
            if callable(ranker):
                order = await ranker(
                    products,
                    query=request.normalized_query or request.original_query or None,
                )
        if order:
            products = feed_curation.apply_selection(
                products, order, limit=AI_FEED_RESULTS
            )
        products = feed_curation.diversity_select(products, wanted)
        collected.products = products[: self.max_results]
        collected.comparisons = build_comparison(collected.products)
        return collected

    async def find_by_category(
        self,
        category: Category,
        country: str | None = None,
        currency: str | None = None,
        exclude: set[tuple[str, str]] | None = None,
    ) -> SearchOutcome:
        """Search inside one catalog category.

        Categories are translated into a normal search request, so the
        exact same provider pipeline (and the same failure tolerance)
        applies. ``exclude`` is the per-user shown-product history: opening
        the same category again keeps moving forward through its products.
        """

        request = SearchRequest(
            original_query=category.title,
            normalized_query=category.query,
            mode=SearchMode.CATEGORY,
            category=category.code,
            keywords=list(category.keywords),
            country=country,
            currency=currency,
        )
        return await self.find_products(request, exclude=exclude)

    async def find_by_image(
        self,
        image_bytes: bytes,
        country: str | None = None,
        exclude: set[tuple[str, str]] | None = None,
    ) -> SearchOutcome:
        """Reverse image search across providers that really support it.

        Providers expose ``search_by_image`` and must set
        ``supports_image_search = True`` once a verified image-search API
        is wired up. Until then every provider is reported as
        unavailable - no fake recognition is performed anywhere.
        """

        request = SearchRequest(
            original_query="поиск по фото",
            mode=SearchMode.IMAGE,
            country=country,
        )
        outcome = SearchOutcome(request=request)

        applicable = self.applicable_providers(request)
        capable = [provider for provider in applicable if provider.supports_image_search]

        outcome.unavailable_marketplaces = [
            provider.display_name
            for provider in applicable
            if provider not in capable or not provider.is_configured
        ]

        if not capable:
            logger.info("Image search requested but no provider supports it yet")
            return outcome

        results = await asyncio.gather(
            *(provider.search_by_image(image_bytes) for provider in capable),
            return_exceptions=True,
        )
        return self._collect(
            request=request,
            pairs=list(zip(capable, results, strict=True)),
            outcome=outcome,
            exclude=exclude,
        )

    async def aclose(self) -> None:
        """Release provider HTTP clients (called on bot shutdown)."""

        for provider in self.providers:
            await provider.aclose()

