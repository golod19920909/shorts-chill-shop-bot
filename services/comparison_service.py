"""Programmatic filtering, ranking and comparison of normalized products.

Everything numeric in this module is plain Python arithmetic. Gemini is
never asked whether a product "fits under $30" - such constraints are
applied here so results are deterministic and explainable.

Two rules the project explicitly requires:
1. Missing data is reported as missing, never guessed.
2. The lowest price is never presented as "the best offer" - an
   incomplete cost basis is never compared against a complete one.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any

from models.product import Availability, Product
from models.search_request import SearchRequest, SortPreference
from services.relevance import relevance_score, relevance_tier
from services.text_matching import (
    can_ever_match,
    is_context_clause,
    is_design_clause,
    is_negation_clause,
    matching_feature,
    phrase_in_text,
)

logger = logging.getLogger(__name__)


#: Price-comparison constants (not learned, not LLM-produced) so the ranking
#: is fully deterministic and explainable.
VALUE_WEIGHT_DISCOUNT = 40.0
VALUE_WEIGHT_RATING = 30.0
VALUE_WEIGHT_ORDERS = 20.0
VALUE_WEIGHT_AVAILABILITY = 10.0

#: Order count at which the orders signal is considered saturated.
ORDERS_REFERENCE = 100_000.0


@dataclass
class ProductComparison:
    """Comparison view of a single product, plus what is unknown about it."""

    product: Product
    comparable_cost: float | None
    total_known_cost: float | None
    missing_fields: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def comparable_unit_cost(product: Product) -> float | None:
    """Cost basis used for price comparison: the observed item price.

    Returns ``None`` when the price is unknown, so an incomplete cost is
    never treated as a number that can be compared with others.
    """

    if product.price is None:
        return None
    return round(product.price, 2)


def value_score(product: Product) -> float:
    """Deterministic "attractive offer" score built only from real fields.

    Signals, each with a fixed maximum weight:

    - ``discount_percent``  up to 40 points (only if the marketplace
      actually reported a discount);
    - ``rating``            up to 30 points (rating / 5);
    - ``orders_count``      up to 20 points (log-scaled);
    - ``availability``      10 points when the marketplace says the item
      is in stock.

    A product with unknown fields simply scores lower - nothing is
    guessed and nothing is imputed. This function only orders
    candidates; wording such as "cheapest" is never produced from it.
    """

    score = 0.0

    if product.discount_percent is not None:
        score += VALUE_WEIGHT_DISCOUNT * min(product.discount_percent, 100.0) / 100.0

    if product.rating is not None:
        score += VALUE_WEIGHT_RATING * min(product.rating, 5.0) / 5.0

    if product.orders_count is not None and product.orders_count > 0:
        ratio = math.log10(product.orders_count + 1) / math.log10(ORDERS_REFERENCE + 1)
        score += VALUE_WEIGHT_ORDERS * min(ratio, 1.0)

    if product.availability is Availability.IN_STOCK:
        score += VALUE_WEIGHT_AVAILABILITY

    return round(score, 2)


def popularity_score(product: Product) -> float:
    """Deterministic popularity score from marketplace-provided signals.

    Orders count dominates (it is the closest thing to a real popularity
    metric a marketplace exposes), rating breaks ties. No invented
    "trending" indicator is ever used.
    """

    score = 0.0

    if product.orders_count is not None and product.orders_count > 0:
        ratio = math.log10(product.orders_count + 1) / math.log10(ORDERS_REFERENCE + 1)
        score += 80.0 * min(ratio, 1.0)

    if product.rating is not None:
        score += 20.0 * min(product.rating, 5.0) / 5.0

    return round(score, 2)


def rating_preference_signal(product: Product) -> tuple[int, float]:
    """Real quality signal, used ONLY when the user asked for good ratings.

    The AliExpress Affiliate API reports ``evaluate_rate`` (the share of
    positive feedback in percent) and no 0-5 star rating, so this is the
    honest reading of "с хорошим рейтингом": the real field the
    marketplace returned, never a converted or invented star value.

    ``(0, -rate)`` for a product the marketplace rated, ``(1, 0.0)`` for
    one it did not - a product with no quality data sorts after every
    rated one instead of being treated as the best.
    """
    if product.positive_rate is not None:
        return (0, -float(product.positive_rate))
    return (1, 0.0)


def _normalize_currency_code(value: Any) -> str | None:
    """Normalize a currency code to a comparable ``XXX`` form.

    ``None``/empty/whitespace-only/malformed values become ``None``
    (unknown); anything else is stripped, upper-cased and accepted only
    when it looks like an ISO-4217 alphabetic code (exactly three ASCII
    letters). No guessing: a symbol, a number, a locale string or a
    display format never maps to a currency here - that mapping already
    lives in the query parsers (``query_intent.normalize_currency``,
    ``ai_service._detect_currency``), not in the filter.
    """

    if value is None:
        return None
    text = str(value).strip().upper()
    if len(text) != 3 or not text.isalpha() or not text.isascii():
        return None
    return text


def _price_currency_mismatch_reason(
    product: Product, request: SearchRequest
) -> str | None:
    """Why a price bound must not judge this product, if it must not.

    Returns a short structured reason (``"no_product_currency"`` /
    ``"currency_mismatch"``) when the request HAS an explicit price bound
    in a known currency and the product price cannot be reconciled with
    it, otherwise ``None``. This helper is the single source of the currency
    verdict: ``_passes_numeric_filters`` rejects exactly when it returns a
    reason, and ``filter_products`` counts its drops with the same helper, so
    the verdict, the counter and the log can never disagree.
    """

    if request.max_price is None and request.min_price is None:
        return None
    request_currency = _normalize_currency_code(request.currency)
    if request_currency is None:
        return None
    product_currency = _normalize_currency_code(product.currency)
    if product_currency is None:
        return "no_product_currency"
    if product_currency != request_currency:
        return "currency_mismatch"
    return None


def _passes_numeric_filters(product: Product, request: SearchRequest) -> bool:
    """Apply all numeric constraints from the request to one product.

    A product whose value for an *active* constraint is unknown is
    rejected: it cannot be verified to satisfy the constraint, and the
    project rule is to avoid guessing.

    Currency coherence (Stage 2): a price bound is compared only when the
    product price and the request bound demonstrably share a unit. When
    the request names an explicit price constraint, a product whose
    currency cannot be reconciled with the request currency is rejected:
    raw amounts in different currencies are never compared, and the
    project performs no currency conversion. Without any price
    constraint, currency alone never rejects a product.

    The one exception is a rating constraint: the AliExpress Affiliate API
    returns no 0-5 star rating at all, so requiring it would empty every
    result set. When *no* candidate carries a rating, the constraint is
    treated as not applicable (``rating_filter_applicable``) and the fact
    is logged. A candidate is kept instead of being silently dropped, and
    the user is never shown an explanation of the marketplace API.
    """

    if request.max_price is not None or request.min_price is not None:
        reason = _price_currency_mismatch_reason(product, request)
        if reason is not None:
            logger.debug(
                "Price filter skipped a product in %r against a %r budget: "
                "%s (currencies cannot be reconciled without conversion)",
                product.currency,
                request.currency,
                reason,
            )
            return False

    if request.max_price is not None:
        if product.price is None or product.price > request.max_price:
            return False

    if request.min_price is not None:
        if product.price is None or product.price < request.min_price:
            return False

    if request.min_rating is not None and request.rating_filter_applicable:
        if product.rating is None or product.rating < request.min_rating:
            return False

    if request.min_orders is not None:
        if product.orders_count is None or product.orders_count < request.min_orders:
            return False

    return True


def filter_products(
    products: list[Product], request: SearchRequest
) -> list[Product]:
    """Return only products satisfying the request's numeric constraints."""

    # A rating filter can only be honoured when the marketplace actually
    # reported ratings; otherwise it is reported as not applied (in the
    # log and on the outcome, never on the screen).
    if request.min_rating is not None:
        request.rating_filter_applicable = any(
            product.rating is not None for product in products
        )
        if not request.rating_filter_applicable:
            logger.info(
                "Rating filter (%s) not applied: no product carries a rating",
                request.min_rating,
            )

    filtered: list[Product] = []
    currency_dropped = 0
    for product in products:
        if not _passes_numeric_filters(product, request):
            if _price_currency_mismatch_reason(product, request) is not None:
                currency_dropped += 1
            continue
        filtered.append(product)
    if currency_dropped:
        logger.info(
            "Price filter dropped %d of %d products on currency "
            "coherence (budget currency=%r; no conversion is performed)",
            currency_dropped,
            len(products),
            request.currency,
        )

    if request.required_features or request.excluded_features:
        filtered = _apply_feature_filters(filtered, request)

    logger.debug(
        "Filtering kept %d of %d products", len(filtered), len(products)
    )
    return filtered


def _apply_feature_filters(
    products: list[Product], request: SearchRequest
) -> list[Product]:
    """Word-aware inclusion/exclusion of parsed required/excluded features.

    Matching lives in ``services.text_matching``: a feature matches only
    whole words (long words by prefix, so Russian endings still work).
    Plain substring matching used to hide valid products - the excluded
    feature "проводные" also matched "беспроводные". See
    ``services/text_matching`` for the exact rules.

    Returns the SURVIVING products. (The function used to fall through
    without returning anything, so any request whose parsed features were
    non-empty made ``filter_products`` return ``None`` and the whole search
    crash with ``TypeError`` - see ``FeatureFilterTests``.)
    """

    required = [feature for feature in request.required_features if feature]
    excluded = [feature for feature in request.excluded_features if feature]

    # A shape/style phrase ("в виде X") describes the DESIGN the user wants,
    # not literal text every title must repeat: titles name the shape with
    # their own words, so enforcing the phrase verbatim hides exactly the
    # requested products (verified live: 1 of 24 relevant keychains
    # survived). Such a feature is reported as NOT applied - the same honest
    # path as a cross-language feature - while its words stay in
    # keywords/normalized_query, where relevance and ranking use them.
    design = [feature for feature in required if is_design_clause(feature)]
    if design:
        logger.info(
            "Feature filter treats shape/style phrases as intent, not literal "
            "text: %s (the products were NOT filtered out on them)",
            design,
        )
        required = [feature for feature in required if feature not in design]

    # A context-of-use phrase («для подарка», «для жены», «на день рождения»)
    # is the SAME kind of intent: it says WHY the product is bought, and a
    # title answers that with its own words («Подарок женщине»), so enforcing
    # it verbatim empties a page of exactly the requested products. It is
    # reported as not applied and turns into a ranking bonus instead
    # (``context_feature_hits``), never into a filter.
    context = [feature for feature in required if is_context_clause(feature)]
    if context:
        logger.info(
            "Feature filter treats context-of-use phrases as intent, not literal "
            "text: %s (the products were NOT filtered out on them)",
            context,
        )
        required = [feature for feature in required if feature not in context]

    # A NEGATION («без мешка», «without ANC», «не чехол») states an ABSENCE and
    # is the same kind of intent: no marketplace title repeats the user's
    # negated phrase verbatim («пылесос без мешка» is titled «Пылесос
    # циклонный»), so enforcing it literally empties a page of exactly the
    # requested products (verified live: «пылесос без мешка» produced
    # required_features=['без мешка'] and hid every bagless vacuum). It is
    # reported as NOT applied - the same honest path as a design/context or
    # cross-language feature - while its words stay in keywords /
    # normalized_query and the structured intent, where relevance and ranking
    # use them. It is deliberately NOT turned into an exclusion: the negated
    # word often appears literally in the correct title («Пылесос БЕЗ МЕШКА»),
    # so excluding «мешок» would drop the very product that was wanted.
    negated = [feature for feature in required if is_negation_clause(feature)]
    if negated:
        logger.info(
            "Feature filter treats negation phrases ('без X'/'without X') as "
            "intent, not literal text: %s (the products were NOT filtered out "
            "on them)",
            negated,
        )
        required = [feature for feature in required if feature not in negated]

    # A feature written in a language that appears NOWHERE in the candidates
    # can never match - it is not a property of the products, it is a language
    # the catalogue did not answer in. Verified live: a Russian feature
    # ("для жены") against an English result set ("gift for wife") removed
    # 50 of 50 perfectly relevant real listings. Such a feature is reported
    # as NOT APPLIED (and named in ``request.ignored_features``) instead of
    # silently emptying the page. A feature in the SAME language that matches
    # nothing is a genuine miss and keeps filtering as before.
    haystacks = [
        " ".join(filter(None, [p.title or "", p.description or ""]))
        for p in products
    ]
    applicable = [feature for feature in required if can_ever_match(feature, haystacks)]
    language_ignored = [feature for feature in required if feature not in applicable]
    if language_ignored:
        logger.info(
            "Feature filter not applied for %s: the result set contains no "
            "text in the same language; the products were NOT filtered out "
            "on it",
            language_ignored,
        )
    ignored = [*design, *context, *negated, *language_ignored]
    if ignored:
        request.ignored_features = ignored
    required = applicable

    def matches(product: Product) -> bool:
        haystack = " ".join(
            filter(None, [product.title or "", product.description or ""])
        )
        if any(not phrase_in_text(haystack, feature) for feature in required):
            return False
        if matching_feature(haystack, excluded) is not None:
            return False
        return True

    kept = [product for product in products if matches(product)]
    logger.debug(
        "Feature filter kept %d of %d products (required=%s, excluded=%s, "
        "ignored=%s)",
        len(kept),
        len(products),
        required,
        excluded,
        ignored,
    )
    return kept



def context_feature_hits(product: Product, request: SearchRequest) -> int:
    """How many context-of-use phrases of the request this product carries.

    Context phrases («для подарка», «для жены», «на день рождения») are
    INTENT, not literal filters (see ``services.text_matching``): they never
    hide a product, but a title that words the same intention with its own
    words («Подарок для жены», «подарочный набор») is a better answer for
    that user than an identical product that does not. Counting them turns
    that intention into a ranking signal INSIDE one relevance tier, so it can
    only reorder products that already passed the same gate: it never
    filters, never invents a field and never overrides relevance.

    ``0`` for any request without context phrases, so every other search
    keeps exactly the order it had before.
    """

    phrases = [
        feature
        for feature in request.required_features
        if feature and is_context_clause(feature)
    ]
    if not phrases:
        return 0

    haystack = " ".join(
        filter(None, [product.title or "", product.description or ""])
    )
    return sum(1 for phrase in phrases if phrase_in_text(haystack, phrase))


def _sort_key(
    product: Product, request: SearchRequest
) -> tuple[int, ...]:
    """Build a (has_value, value, ...) tuple so unknown values sort last.

    ``0`` for known values and ``1`` for unknown keeps every sort order
    deterministic while avoiding the classic bug of treating a missing
    price as ``0`` and showing it as the cheapest option. The optional
    trailing element is a tie-break (relevance, number of known fields).
    """

    preference = request.sort_preference

    if preference is SortPreference.PRICE_ASC:
        value = comparable_unit_cost(product)
    elif preference is SortPreference.PRICE_DESC:
        return (0 if product.price is not None else 1, -(product.price or 0.0))
    elif preference is SortPreference.RATING_DESC:
        # "Лучший рейтинг": the affiliate response carries NO 0-5 star
        # rating, so the order uses the real quality field the API does
        # return (``evaluate_rate`` = share of positive feedback) and
        # keeps unrated products last instead of pretending they are the
        # best. Popularity breaks ties between equally rated products.
        known, signal = rating_preference_signal(product)
        return (known, signal, -popularity_score(product))
    elif preference is SortPreference.ORDERS_DESC:
        return (
            0 if product.orders_count is not None else 1,
            float(-(product.orders_count or 0)),
        )
    elif preference is SortPreference.TOTAL_COST_ASC:
        total = product.total_known_cost
        return (0 if total is not None else 1, total if total is not None else math.inf)
    elif preference is SortPreference.POPULARITY_DESC:
        score = popularity_score(product)
        # A product with no popularity signal at all is not "the most
        # popular", so it sorts after everything that has real data.
        has_signal = product.orders_count is not None or product.rating is not None
        return (0 if has_signal else 1, -score)
    elif preference is SortPreference.VALUE_DESC:
        score = value_score(product)
        has_signal = any(
            value is not None
            for value in (
                product.discount_percent,
                product.rating,
                product.orders_count,
            )
        )
        return (0 if has_signal else 1, -score)
    else:
        # RELEVANCE (the default of a product search): what the user asked
        # for comes FIRST, in three deterministic layers:
        #   1. the product-type tier (2 = the head noun was verified, 1 =
        #      word-level match only) - an unrelated product can never win
        #      here, however many orders it has;
        #   2. the share of the request's own words found in the title;
        #   3. the CONTEXT-OF-USE intention the user expressed («для жены»,
        #      «для подарка»): such a phrase is never a filter, but a title
        #      that words the same intention leads the tier;
        #   4. the real marketplace signals (orders/rating) and data
        #      completeness - only as a tie-break WITHIN one relevance tier,
        #      so popularity orders peers but never overrides identity.
        # When the user asked for a good rating in WORDS ("с хорошим
        # рейтингом"), the real quality field of the marketplace orders
        # candidates INSIDE one tier - after the tier and the query-word
        # share, so it can never lift an unrelated product above a
        # relevant one, and it never removes anything (no filter, no
        # invented rating). Without that wish the tuple is unchanged.
        tier = relevance_tier(product, request)
        score = relevance_score(product, request)
        context = context_feature_hits(product, request)
        quality = (
            rating_preference_signal(product)
            if request.prefers_good_rating
            else (0, 0.0)
        )
        known = sum(
            1
            for value in (product.price, product.rating, product.orders_count)
            if value is not None
        )
        return (
            0,
            -tier,
            -score,
            -context,
            quality[0],
            quality[1],
            -popularity_score(product),
            float(-known),
        )

    if value is None:
        return (1, math.inf)
    return (0, value)


def sort_products(products: list[Product], request: SearchRequest) -> list[Product]:
    """Sort products according to the request's sort preference."""

    return sorted(products, key=lambda product: _sort_key(product, request))


def build_comparison(
    products: list[Product], limit: int | None = None
) -> list[ProductComparison]:
    """Build comparison views for already-filtered products.

    The lowest price is deliberately NOT presented as "the best offer":
    each comparison exposes the known cost basis and which fields are
    unavailable so the user can judge for themselves.
    """

    comparisons: list[ProductComparison] = []

    for product in products[: limit if limit is not None else None]:
        comparison = ProductComparison(
            product=product,
            comparable_cost=comparable_unit_cost(product),
            total_known_cost=product.total_known_cost,
        )

        if product.price is None:
            comparison.missing_fields.append("price")
        if product.shipping_cost is None:
            comparison.missing_fields.append("shipping_cost")
        if product.rating is None:
            comparison.missing_fields.append("rating")
        if product.orders_count is None:
            comparison.missing_fields.append("orders_count")
        if product.delivery_estimate is None:
            comparison.missing_fields.append("delivery_estimate")

        if product.shipping_cost is None:
            comparison.notes.append("Стоимость доставки неизвестна.")
        if product.price is None:
            comparison.notes.append("Цена неизвестна.")
        if product.rating is None and product.orders_count is None:
            comparison.notes.append("Нет данных о рейтинге и количестве заказов.")

        comparisons.append(comparison)

    return comparisons




