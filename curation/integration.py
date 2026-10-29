"""Wiring the curation layer into the bot's live product flows.

``curation.pipeline`` is deliberately pure: it takes candidates and returns the
ones worth showing. Something has to bring the two halves together - READ what
the user has already been shown from SQLite, run the pipeline, and WRITE the
cards that actually reached the user back into that history - and this module is
that something, so the decisions live in ONE place instead of being re-made by
every handler:

* :meth:`CurationGate.curate` runs ONCE per result set, immediately before the
  feed is materialized. ``handlers.search.send_search_results`` is the single
  choke point every flow goes through (text search, discovery, category,
  popular, photo), so its curated list is what the one-card browser walks -
  including «➡️ Следующий» and every later open;
* :meth:`CurationGate.remember` records the card that ACTUALLY reached the user
  (the opened one and each browsed one) - the only moment a product may be
  called "seen".

Modes here mean exactly what the pipeline says they mean: ``search`` (the API's
relevance order, discounts only when the request asked for the discount sort),
``recommendation`` (the impulse-buy feed: real orders/rating/price value score,
$1..$20 quality window) and ``deals`` (the marketplace's own discount
ordering). Nothing in this module invents a product,
a price or a discount - it only reads a history and drops/reorders what
AliExpress returned. A failure of either half degrades to the behaviour before
this step (no history read, no history write) rather than breaking a feed.
"""

from __future__ import annotations

import logging

from curation.pipeline import (
    DEFAULT_MODE,
    MODE_CATEGORY,
    MODE_DEALS,
    MODE_RECOMMENDATION,
    MODE_SEARCH,
    CurationPipeline,
    normalize_mode,
)
from models.product import Product
from models.search_request import SearchMode, SearchRequest, SortPreference
from services.relevance import relevance_score, relevance_tier

logger = logging.getLogger(__name__)


def mode_for_request(request: object) -> str:
    """The mode a SEARCH-like request asks for.

    A request that asked for the marketplace's DOCUMENTED discount ordering
    (``SortPreference.VALUE_DESC`` -> ``DISCOUNT_DESC``) is ranked as ``deals``;
    every other request keeps the API's own relevance order (``search``).
    """

    if getattr(request, "sort_preference", None) is SortPreference.VALUE_DESC:
        return MODE_DEALS
    return MODE_SEARCH


def mode_for_feed(*, track_shown: bool, deals_cycle: bool = False) -> str:
    """The mode of a discovery feed.

    ``track_shown`` marks the «🔥 Популярное» rotation (``handlers.search`` uses
    the same flag to pick the history scope, so both decisions come from ONE
    signal). The rotation is the impulse-buy RECOMMENDATION feed (spec): its
    normal cycle is ``recommendation`` ($1..$20 / >= 300 orders quality gate,
    value-score ranking), and the cycle that asked the marketplace for its
    discount ordering is the real ``deals`` feed - ``deals_cycle`` tells this
    call which one it is. Any other feed without an explicit mode - photo
    search - is an ordinary ``search`` (categories pass their own
    ``recommendation`` mode at the call site).
    """

    if not track_shown:
        return MODE_SEARCH
    return MODE_DEALS if deals_cycle else MODE_RECOMMENDATION


def view_ids(products) -> list[str]:
    """The product ids of the cards that reached the user, order kept."""

    ids: dict[str, None] = {}
    for product in products or ():
        product_id = str(getattr(product, "product_id", "") or "").strip()
        if product_id:
            ids.setdefault(product_id, None)
    return list(ids)


class CurationGate:
    """The pipeline plus the per-user view history, applied to one feed.

    The pipeline is shared and stateless (every call builds its own history and
    ranking stages), so one gate per context costs nothing and two users can
    never share a skip list.
    """

    def __init__(
        self, database: object = None, pipeline: CurationPipeline | None = None
    ) -> None:
        self.database = database
        self.pipeline = pipeline if pipeline is not None else CurationPipeline()

    async def seen_ids(self, user_id: int | None) -> set[str]:
        """What this user has already been shown (empty when unreadable).

        A guest (no ``user_id``), a missing database and a failing read all
        answer "nothing seen": a feed must degrade to SHOWING products, never to
        an empty screen.
        """

        if self.database is None or user_id is None:
            return set()
        read = getattr(self.database, "get_user_seen_ids", None)
        if read is None:
            # A database layer without the view-history API (a test double, or
            # an older layer) simply HAS no history - that is not a failure.
            return set()
        try:
            return await read(user_id)
        except Exception:
            logger.exception("Failed to read the view history of user %s", user_id)
            return set()

    async def curate(
        self,
        products,
        *,
        user_id: int | None = None,
        mode: str = DEFAULT_MODE,
        request: SearchRequest | None = None,
    ) -> list[Product]:
        """The products of ``products`` this flow should show, in order.

        Returns the SAME objects (the pipeline never copies or completes a
        candidate), so the cached outcome the one-card browser walks is exactly
        what the pipeline approved - including «➡️ Следующий».

        ``request`` is read for ``MODE_CATEGORY`` (G24.8 P1.1): this
        wiring layer computes the per-candidate relevance map there, with
        the SAME ``services.relevance`` functions the service layer sorted
        with, and hands it to the ranker. Since G25.2-C Stage 7 it also
        builds the tier map for ``MODE_SEARCH``, which the diversity stage
        uses to keep the semantic-dominance invariant (tier 2 always
        above tier 1 in the displayed list). Every other mode ignores the
        request entirely - their behaviour cannot change.
        """

        pool = list(products or ())
        if not pool:
            return pool

        seen = await self.seen_ids(user_id)
        curated = self.pipeline.process(
            pool,
            user_id=user_id,
            mode=mode,
            seen_ids=seen,
            relevance=self._relevance_map(pool, mode, request),
        )
        report = self.pipeline.last_report
        logger.info(
            "Curation (%s, user=%s): %d -> %d (history %d, stages %s)",
            mode,
            user_id,
            len(pool),
            len(curated),
            len(seen),
            ", ".join(f"{stage.name}:{stage.dropped}" for stage in report.stages),
        )
        return curated

    @staticmethod
    def _relevance_map(
        pool: list, mode: str, request: SearchRequest | None
    ) -> dict[int, tuple[int, float]] | None:
        """Per-candidate ``(tier, score)`` for MODE_CATEGORY (G24.8 P1.1)
        and for MODE_SEARCH since G25.2-C Stage 7.

        Computed by the SAME ``services.relevance`` functions the service
        layer sorts with, so the ranker's Stage A is exactly the relevance
        this pipeline already agreed on - the curation stage itself never
        recomputes, duplicates or invents relevance logic.

        * MODE_CATEGORY: full ``(tier, score)`` map - unchanged behaviour.
        * MODE_SEARCH (Stage 7): the same map, read by the diversity stage
          ONLY (tier banding); ModeRanker for search keeps the received
          order exactly as before, so nothing else about search changes.
        * Any other mode, a missing request, or a pool without ``Product``
          candidates answers ``None``: recommendation/popular/discovery
          feeds keep their exact previous behaviour and a pool without a
          map keeps its arrival order (the P0.2 contract).
        """
        normalized = normalize_mode(mode)
        if request is None or normalized not in (MODE_CATEGORY, MODE_SEARCH):
            return None
        if normalized == MODE_SEARCH and getattr(
            request, "mode", None
        ) is not SearchMode.SEARCH:
            # Stage-7 section 7: only NORMAL TEXT SEARCH gets the tier
            # map. Photo/discovery/feed requests reach this wiring with
            # their own SearchMode even when the curation mode string is
            # plain «search» - they keep the legacy behaviour entirely.
            return None
        relevance = {
            id(candidate): (
                relevance_tier(candidate, request),
                relevance_score(candidate, request),
            )
            for candidate in pool
            if isinstance(candidate, Product)
        }
        return relevance or None

    async def remember(self, user_id: int | None, products) -> None:
        """Record the cards the user has actually been shown.

        Called at DISPLAY time only. A missing user, a missing database or a
        failing write is logged and ignored: browsing must never break because
        the novelty history could not be updated.
        """

        if self.database is None or user_id is None:
            return
        write = getattr(self.database, "add_user_views", None)
        if write is None:
            # See ``seen_ids``: no API means no history to write.
            return
        ids = view_ids(products)
        if not ids:
            return
        try:
            await write(user_id, ids)
        except Exception:
            logger.exception("Failed to record views of user %s", user_id)
