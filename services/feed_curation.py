"""Deterministic curation pipeline for the "🔥 Популярное" feed.

The feed spec demands a REAL candidate pipeline over live AliExpress
data: exact-ID dedup (``services.product_identity``) -> near-duplicate
detection -> relevance/quality -> ordering by real marketplace signals ->
diversity -> recent-product penalty -> affiliate validation. This module owns
the middle stages as PURE functions over already-normalized
:class:`~models.product.Product` objects:

- nothing here invents or mutates marketplace facts - functions only
  reorder or drop whole products;
- every heuristic is deterministic, so tests can assert behaviour
  without a model or a network;
- Gemini participates only through ``AIService.rank_candidates``, used by the
  keyword-driven discovery feed; its resulting *order* is validated in
  :func:`apply_ranking` - unknown ids are discarded, missing products keep
  their original relative order.

Heuristics (all constants here, all honest about what they are):

- near-duplicate: token-set (Jaccard) similarity of normalized titles
  above :data:`NEAR_DUPLICATE_THRESHOLD` - later duplicates are dropped;
- diversity: :func:`diversity_select` accepts a candidate only when it
  is sufficiently dissimilar to everything already accepted, then fills
  remaining slots from the deferred tail - same-type products do not
  cluster at the top;
- spreading: :func:`spread_similar` is the same idea applied to one search
  result set (where nothing may be dropped): an almost exact copy of an
  already accepted listing is moved behind the different candidates
  instead of occupying the first page next to its own twin;
- recent penalty: products whose ids are in the user's recent-history
  ring move to the tail (a penalty, NOT a blacklist: they age out of the
  ring and may return later);
- popularity: NOT decided here. What "popular" means is a marketplace
  fact (real order volume) and lives in ``services.comparison_service``
  as arithmetic over returned fields, while WHAT gets asked from the
  marketplace lives in the provider's topic rotation. There is
  deliberately no keyword blocklist of "bad" product types in this
  module: a hand-written list of forbidden words hides valid listings
  and has to be extended forever, so the feed is fixed at the query
  level (catalogue topics) and ordered by real signals instead.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from models.product import Product

#: Two titles sharing this fraction of tokens are the same listing type.
NEAR_DUPLICATE_THRESHOLD = 0.75

#: A candidate is "too similar to what is already selected" above this
#: Jaccard similarity - it gets deferred to the fill-up pass.
DIVERSITY_THRESHOLD = 0.5

#: How many products of ONE product family a feed may lead with
#: (:func:`family_select`). Two, not one: the same item from two stores is a
#: legitimate pair of offers, while five near-copies of it are one card
#: repeated five times. Never a product-specific rule - a family is decided by
#: the titles' own words.
FAMILY_PER_FEED = 2

#: A feed shorter than this is worse than a repeated family, so the fill-up
#: pass of :func:`family_select` restores deferred near-copies until the feed
#: reaches it (one page of cards - see ``search_service.DEFAULT_PAGE_SIZE``).
#: Without the bound, a pool holding ONE family would turn a 30-slot feed into
#: fifty wordings of the same item, which is exactly the reported repetition.
FAMILY_FILL_MINIMUM = 5

#: Token length filter for similarity: shorter words ("для", "с", "1м")
#: create false matches on their own.
_MIN_TOKEN_LENGTH = 3

_TOKEN_RE = re.compile(r"[\wа-яё]+", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def title_tokens(title: str) -> frozenset[str]:
    """Normalized token set of a title (tags stripped, case-folded)."""

    plain = _HTML_TAG_RE.sub(" ", title or "").casefold()
    return frozenset(token for token in _TOKEN_RE.findall(plain))


def _comparable_tokens(title: str) -> frozenset[str]:
    """Tokens long enough to prove similarity between two titles."""

    return frozenset(
        token for token in title_tokens(title) if len(token) >= _MIN_TOKEN_LENGTH
    )


def similarity(left: str, right: str) -> float:
    """Jaccard similarity of two titles' comparable token sets (0..1)."""

    a = _comparable_tokens(left)
    b = _comparable_tokens(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def drop_near_duplicates(
    products: Sequence[Product], threshold: float = NEAR_DUPLICATE_THRESHOLD
) -> list[Product]:
    """Drop later products whose titles are near-identical to a kept one.

    Order is preserved (the feed's ranking survives); only repeats go.
    Exact product-ID dedup already happened earlier in
    ``services.product_identity``.
    """

    kept: list[Product] = []
    for product in products:
        if any(
            similarity(product.title, other.title) >= threshold for other in kept
        ):
            continue
        kept.append(product)
    return kept


def apply_ranking(
    products: Sequence[Product], order: Sequence[str] | None
) -> list[Product]:
    """Reorder products by an id order produced by Gemini (or anyone).

    Validation makes mutation impossible: ids that are not in
    ``products`` are discarded, duplicates are ignored, and products the
    order forgot keep their original relative order at the tail. ``None``
    (Gemini failed / disabled) returns the input unchanged - the
    deterministic ranking is the fallback.
    """

    if not order:
        return list(products)

    by_id = {product.product_id: product for product in products}
    seen: set[str] = set()
    reordered: list[Product] = []
    for product_id in order:
        product = by_id.get(product_id)
        if product is None or product_id in seen:
            continue
        seen.add(product_id)
        reordered.append(product)
    reordered.extend(
        product for product in products if product.product_id not in seen
    )
    return reordered


def apply_selection(
    products: Sequence[Product],
    chosen: Sequence[str] | None,
    *,
    limit: int | None = None,
) -> list[Product]:
    """Keep only the products an AI selection approved, in its own order.

    ``apply_ranking`` reorders a feed; this one DROPS from it, which is what the
    strict filter of a selection means: an id the model did not name is a listing
    it rejected (a low rating, an accessory that is not the searched product,
    cheap spam, a duplicate), so it must not reach a card. Validation is exactly
    as unforgiving as in :func:`apply_ranking` - an id absent from ``products`` is
    discarded (the model can never inject a product), duplicates are ignored, and
    nothing is invented or repeated. ``None``/empty (AI disabled or failed)
    returns the input unchanged, so the deterministic list stays the fallback, and
    ``limit`` is a hard ceiling: a chatty answer cannot widen the feed.
    """

    if not chosen:
        return list(products)

    by_id = {product.product_id: product for product in products}
    selected: list[Product] = []
    seen: set[str] = set()
    for product_id in chosen:
        product = by_id.get(product_id)
        if product is None or product_id in seen:
            continue
        seen.add(product_id)
        selected.append(product)
        if limit is not None and len(selected) >= limit:
            break
    return selected


def diversity_select(
    products: Sequence[Product],
    limit: int,
    threshold: float = DIVERSITY_THRESHOLD,
) -> list[Product]:
    """Spread different product types across the first ``limit`` slots.

    Greedy pass: take the next candidate only when it is dissimilar to
    everything already accepted; deferred candidates fill any remaining
    slots in their original order. No product is invented or lost -
    only the order changes.
    """

    pool = list(products)
    selected: list[Product] = []
    deferred: list[Product] = []

    while pool and len(selected) < limit:
        candidate = pool.pop(0)
        if any(
            similarity(candidate.title, chosen.title) >= threshold
            for chosen in selected
        ):
            deferred.append(candidate)
            continue
        selected.append(candidate)

    # Fill-up pass: the feed prefers more products over staying short.
    while deferred and len(selected) < limit:
        selected.append(deferred.pop(0))
    selected.extend(pool)
    selected.extend(deferred)
    return selected[:limit] if limit > 0 else selected


def spread_similar(
    products: Sequence[Product],
    threshold: float = NEAR_DUPLICATE_THRESHOLD,
) -> list[Product]:
    """Reorder so near-identical products do not sit next to each other.

    A marketplace search is routinely answered with several listings of the
    SAME product family (the same item from different stores, the same
    variant in another colour, the same title with the words reordered). They
    are all relevant and none of them is a duplicate by product id, so they
    all survive dedup - and then occupy the first page of cards, telling the
    user nothing new.

    This pass walks the ranked list and keeps every product where the ranking
    put it UNLESS it is an almost exact title copy of one already accepted:
    such a copy is moved behind the other candidates instead. The kept twin
    keeps representing the family at the top, the copy is still returned (a
    later page of the same result set), so diversity never removes a relevant
    product - it only stops one family from taking the whole first screen.
    Relevance decides WHICH products are returned and in which order they are
    considered; this function only spreads the near-copies apart.

    Reorder ONLY: no product is invented, dropped or modified.
    """

    pool = list(products)
    if len(pool) < 2:
        return pool
    # ``diversity_select`` with ``limit=len(pool)`` consumes the whole list,
    # so nothing is cut off here; the copies simply end up behind the
    # different candidates instead of next to their twin.
    return diversity_select(pool, len(pool), threshold)


def family_select(
    products: Sequence[Product],
    limit: int,
    *,
    per_family: int = FAMILY_PER_FEED,
    threshold: float = DIVERSITY_THRESHOLD,
) -> list[Product]:
    """Pick a feed of ``limit`` products, at most ``per_family`` per family.

    A marketplace feed is dominated by one product FAMILY rather than by one
    listing: after exact duplicates and near-identical titles are gone
    (:func:`drop_near_duplicates`), the remaining cards are still the same item
    from five stores, in five wordings, in five variants - "reworded twins"
    whose Jaccard similarity sits between :data:`DIVERSITY_THRESHOLD` and
    :data:`NEAR_DUPLICATE_THRESHOLD`. :func:`diversity_select` cannot fix that
    on its own: its greedy pass stops as soon as ``limit`` products are
    accepted, so with a pool larger than the feed every twin inside those
    ``limit`` slots survives.

    This selector walks the WHOLE ranked pool instead: a candidate joins an
    existing family as soon as it is similar enough to that family's
    representative (the same word-level rule the feed already uses), and only
    the first :data:`FAMILY_PER_FEED` members of a family take a slot. The rest
    are deferred - and used to fill the feed only while it is shorter than
    :data:`FAMILY_FILL_MINIMUM`, because a two-card feed is worse than a
    repeated family, while fifty wordings of one item are worse than both.

    Reorder/re-select ONLY: no product is invented, dropped or modified, so a
    product that failed relevance can never enter the feed through diversity.
    """

    pool = list(products)
    if limit <= 0 or not pool:
        return pool if limit <= 0 else []

    selected: list[Product] = []
    deferred: list[Product] = []
    families: list[tuple[Product, int]] = []

    for candidate in pool:
        for index, (representative, count) in enumerate(families):
            if similarity(candidate.title, representative.title) < threshold:
                continue
            if count < max(1, per_family):
                families[index] = (representative, count + 1)
                selected.append(candidate)
            else:
                deferred.append(candidate)
            break
        else:
            families.append((candidate, 1))
            selected.append(candidate)

    # Fill-up pass: a pool holding fewer different families than the feed has
    # slots still returns real products - the deferred twins come back, but
    # only until the feed reaches one page of cards.
    for candidate in deferred:
        if len(selected) >= limit or len(selected) >= FAMILY_FILL_MINIMUM:
            break
        selected.append(candidate)

    return selected[:limit]


def penalize_recent(
    products: Sequence[Product], recent_ids: Iterable[str]
) -> list[Product]:
    """Push recently-shown products to the tail (penalty, not a blacklist).

    ``recent_ids`` is a bounded per-user ring: once an id falls out of
    it, the product may appear high in the feed again.
    """

    recent = set(recent_ids)
    if not recent:
        return list(products)
    fresh = [product for product in products if product.product_id not in recent]
    shown = [product for product in products if product.product_id in recent]
    return fresh + shown
