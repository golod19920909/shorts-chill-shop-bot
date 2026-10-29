"""Unified Discovery & Curation Layer - the curation stage (step 1 of 3).

``SearchService`` answers ONE question by asking the marketplace providers and
returning everything they sent back. That contract is right for search and is
deliberately NOT touched here: curation is a separate, pure post-processing
stage that decides which of the returned candidates deserve to be shown.

This module is isolated on purpose:

- its input is plain ``dict`` candidates (the shape a provider produces) and
  its output is a SUBSET of those very objects - nothing is copied, renamed,
  invented or completed;
- it imports nothing from ``handlers``, from the database, from the providers
  or from the AI service, so it can be unit-tested with hand-written data and
  wired into any feed later without editing this file;
- only the standard library is used (``re``, ``math``, ``set``): no embeddings,
  no NumPy, no vector store. Similarity is the Jaccard index of the titles'
  token sets - deterministic, explainable and reproducible.

Pipeline order (:meth:`CurationPipeline.process`):

1. :class:`QualityFilter` - drop anything that cannot be shown honestly (no
   id, no title, no usable price) or that carries contradictory data;
2. :class:`DeduplicationFilter` - collapse the listings of ONE product family
   (reworded twins: "RGB Механическая клавиатура ..." / "Механическая
   клавиатура RGB ...") into the family's single best offer, judged by the
   marketplace's own signals (orders, then rating);
3. :class:`HistoryFilter` - keep the feed novel: what this user has already
   been shown is removed, or pushed to the tail when the unseen rest cannot
   fill a screen;
4. :class:`ModeRanker` - order the survivors the way ONE mode means it
   (``deals`` / ``popular`` / ``search``), over real signals only;
5. :class:`DiversityFilter` - keep one product type from taking a whole block
   of cards ("не больше двух клавиатур на экран").

Every stage is pure and order-stable: the FIRST occurrence of a family keeps
the family's slot in the output, so the ranking a caller produced survives
curation untouched - only rejects, repeats and (past a screen's worth of
novelty) already-seen cards disappear, and even those come back at the tail
rather than vanish.

The database boundary is deliberately on the CALLER's side: the stages take
plain data (``seen_ids=await db.get_user_seen_ids(user_id)``), which keeps this
module synchronous, model-free and testable without a live SQLite file - the
handler reads the history and hands the answer in.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

#: Two titles sharing this fraction of their tokens describe ONE product
#: family: "Механическая клавиатура RGB Red Switch" and "RGB Механическая
#: клавиатура Red Switch" score exactly 1.0. Above the threshold the better
#: offer of the family wins, below it the listings stay separate products.
SIMILARITY_THRESHOLD = 0.65

#: Fields a candidate must carry to be showable at all: without an id it
#: cannot be recognized or opened, without a title it cannot be described,
#: without a price it cannot be compared with anything.
REQUIRED_FIELDS: tuple[str, ...] = ("product_id", "title", "price")

#: Provider spellings of the same marketplace facts. Order matters: the first
#: present field wins, so the canonical name of a fact beats its aliases.
PRODUCT_ID_FIELDS: tuple[str, ...] = ("product_id", "id")
TITLE_FIELDS: tuple[str, ...] = ("title", "name")
PRICE_FIELDS: tuple[str, ...] = ("price", "min_price")
ORDER_FIELDS: tuple[str, ...] = ("orders", "volume", "orders_count")
RATING_FIELDS: tuple[str, ...] = ("rating", "score")
POSITIVE_RATE_FIELDS: tuple[str, ...] = ("positive_rate",)

#: Rejection reasons of :meth:`QualityFilter.reason`. They name the offending
#: field so a log line can be explained without guessing.
REASON_MALFORMED = "malformed_candidate"
REASON_PRODUCT_ID = "missing_product_id"
REASON_TITLE = "missing_title"
REASON_PRICE = "missing_price"
REASON_PRICE_VALUE = "invalid_price"
REASON_RATING = "invalid_rating"
REASON_ORDERS = "invalid_orders"

#: A rating is a 0..5 scale. A value outside it is not a rating.
MAX_RATING = 5.0

#: Built-in scalars and containers that can never be a candidate. They are
#: rejected by TYPE, not by attribute probing, because a plain ``str`` really
#: does have a ``.title`` attribute - probing alone would read the bound method
#: of that name and treat the string as a product.
NOT_A_CANDIDATE_TYPES = (
    str,
    bytes,
    bytearray,
    bool,
    int,
    float,
    complex,
    list,
    tuple,
    set,
    frozenset,
)

#: Similarity compares tokens of at least this length: shorter words
#: ("с", "для", "1м") appear in unrelated listings and would make two different
#: products look like one family. The one exception is a token that carries a
#: DIGIT (see :func:`title_tokens`): a number is never filler - it is the model,
#: the size or the generation of the product.
MIN_TOKEN_LENGTH = 3

#: A token that states a number ("13", "4k", "60w").
_DIGIT_RE = re.compile(r"\d")

#: The feed modes :class:`ModeRanker` knows. ``search`` is the default: the
#: API's own relevance order IS the answer, so nothing is reordered unless the
#: caller states a relevance of its own. ``recommendation`` is the
#: impulse-buy feed («🔥 Популярное»): it reorders by the real value score of
#: :func:`recommendation_score` and - like the rotation's own
#: ``popular``/``deals`` cycles - filters under the recommendation quality
#: profile (see :data:`QUALITY_PROFILES`). ``category`` (G24.8 P0.2/P1.1) is
#: category browsing: it filters under its OWN tolerant profile and ranks by
#: its own staged policy - relevance first (the wiring layer's map), then
#: stated quality signals - so the impulse-buy gate and the impulse-buy
#: score never touch a category result.
MODE_DEALS = "deals"
MODE_POPULAR = "popular"
MODE_RECOMMENDATION = "recommendation"
MODE_SEARCH = "search"
MODE_CATEGORY = "category"
DEFAULT_MODE = MODE_SEARCH
KNOWN_MODES = (MODE_DEALS, MODE_POPULAR, MODE_RECOMMENDATION, MODE_SEARCH, MODE_CATEGORY)

#: The quality gate's published thresholds, in USD (spec): a direct search
#: starts at $1 with no ceiling, an impulse-buy recommendation window is
#: $1..$20. All of them are USD-denominated - the project converts no
#: currencies (README), so a price the marketplace stated in another
#: currency is never judged against these numbers (see
#: :meth:`QualityFilter._price_bounds_apply`).
MIN_PRICE_USD = 1.0
RECOMMENDATION_MAX_PRICE_USD = 20.0

#: When the strict $1..$20 recommendation window answers with NOTHING, the
#: feed relaxes its ceiling to this many dollars - and only its ceiling:
#: the >= 300 orders floor and every other threshold stay exactly as they
#: were (spec: the empty-state fallback of a recommendation feed).
MAX_PRICE_USD = 50.0

#: The recommendation profile's order floor (an order count the marketplace
#: STATES; see ``QualityFilter.lenient_missing`` for absent facts). A DIRECT
#: search deliberately has NO order floor: the keyword answer's
#: ``lastest_volume`` is a recent-window sales figure whose ordinary values
#: are 0..tens even for legitimate products (live audit 2026: a «небулайзер»
#: page of relevant hits read 34/9/0/0/0; for «наушники» 41 of 44 relevant
#: listings were below 100), so an orders >= 100 gate in search removed every
#: candidate of an ordinary query - the second cause of the production
#: «ничего не найдено» bug. Popularity is not proven wrongness: it stays in
#: the ORDERING (the relevance tier is tie-broken by
#: ``comparison_service.popularity_score``) while impulse-buy feeds keep
#: their documented >= 300 floor.
RECOMMENDATION_MIN_ORDERS = 300

#: Both profiles require this star rating - enforced whenever the
#: marketplace states a 0..5 rating. The AliExpress affiliate API reports
#: no star rating at all (only ``evaluate_rate``, which this project
#: deliberately never converts into stars), so an absent rating is not a
#: rejection: the same stance as ``services.comparison_service``.
QUALITY_MIN_RATING = 4.5


@dataclass(frozen=True)
class QualityProfile:
    """The published thresholds of one quality mode (price in USD).

    ``min_orders`` is ``None`` when the profile has no order floor (the
    direct-search profile - see :data:`QUALITY_PROFILES`); all four fields
    are ``None`` for the ``category`` profile (G24.8 P0.2), where category
    discovery has no monetary or popularity thresholds at all - only the
    structural contract of :class:`QualityFilter` applies.
    """

    min_price: float | None
    max_price: float | None
    min_orders: int | None
    min_rating: float | None


#: Three published profiles: ``recommendation`` = impulse-buy feeds,
#: ``search`` = a direct text/photo search, ``category`` = broad category
#: discovery (G24.8 P0.2). Every other feed mode (``popular``, ``deals``)
#: reads the recommendation profile through :func:`quality_mode`.
QUALITY_PROFILES: dict[str, QualityProfile] = {
    MODE_RECOMMENDATION: QualityProfile(
        min_price=MIN_PRICE_USD,
        max_price=RECOMMENDATION_MAX_PRICE_USD,
        min_orders=RECOMMENDATION_MIN_ORDERS,
        min_rating=QUALITY_MIN_RATING,
    ),
    MODE_SEARCH: QualityProfile(
        min_price=MIN_PRICE_USD,
        max_price=None,
        # NO orders floor for a direct search (live-verified 2026, see
        # RECOMMENDATION_MIN_ORDERS above): the orders field of a keyword
        # answer is a recent-window volume, so an orders threshold in search
        # emptied ordinary queries instead of proving anything wrong. The
        # strict recommendation profile ($1..$20 / >= 300) is untouched.
        min_orders=None,
        min_rating=QUALITY_MIN_RATING,
    ),
    # G24.8 P0.2: category discovery is NOT an impulse-buy window. After
    # the relevance filter has accepted a candidate, this policy rejects
    # only what cannot be shown honestly (the structural contract above):
    # no $1 floor, no $20 ceiling, no >= 300 orders floor, no >= 4.5
    # rating. Those numbers belong to the feeds they were written for, and
    # category eligibility must not depend on the request's currency.
    MODE_CATEGORY: QualityProfile(
        min_price=None,
        max_price=None,
        min_orders=None,
        min_rating=None,
    ),
}

#: How many UNSEEN candidates make the already-seen ones unnecessary: at this
#: point a full screen of cards can be built from novel products alone, so the
#: seen ones are dropped instead of merely deferred. A page of cards is 5-6
#: (``search_service.DEFAULT_PAGE_SIZE``).
FRESH_ENOUGH = 6

#: At most this many products of ONE type inside a block of
#: :data:`DIVERSITY_BLOCK` cards - "не больше двух клавиатур на экран".
MAX_PER_TYPE = 2

#: The size of one "block of cards" for the diversity rule. The task states
#: 6-10; 6 is the smallest honest block (a page of cards), so the guarantee is
#: the strictest of the range.
DIVERSITY_BLOCK = 6

#: Fields carrying an explicit relevance/rank score. Curation never INVENTS
#: one: the field is only read when the caller (or the API) supplied it.
RELEVANCE_FIELDS: tuple[str, ...] = ("relevance", "rank_score")

#: Discount spellings, and the two prices a discount can be computed from.
DISCOUNT_FIELDS: tuple[str, ...] = ("discount_percent", "discount", "discount_rate")
OLD_PRICE_FIELDS: tuple[str, ...] = ("old_price", "original_price")

#: The marketplace's own quality facts, as groups of field spellings. "A
#: quality product" is not a judgement made here: it is the number of these
#: facts a listing actually states.
QUALITY_SIGNAL_FIELDS = (RATING_FIELDS, ORDER_FIELDS, POSITIVE_RATE_FIELDS)

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
_WORD_RE = re.compile(r"[^\W_]", re.UNICODE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def field(candidate: Any, *names: str, default: Any = None) -> Any:
    """Read a field from a mapping candidate, or from a model object.

    The layer is defined over ``dict`` candidates - the shape a provider
    produces - but the very same facts are carried by
    ``models.product.Product``. Reading both shapes here keeps ONE set of
    rules instead of a dict version and an object version that can disagree.
    """

    if isinstance(candidate, Mapping):
        for name in names:
            if name in candidate:
                return candidate[name]
        return default

    for name in names:
        if hasattr(candidate, name):
            return getattr(candidate, name)
    return default


def has_field(candidate: Any, *names: str) -> bool:
    """Whether the candidate carries at least one of ``names`` at all.

    Presence and value are different facts: ``{"price": None}`` HAS a price
    field that states nothing, which is a missing amount - not a malformed one.
    """

    if isinstance(candidate, Mapping):
        return any(name in candidate for name in names)
    return any(hasattr(candidate, name) for name in names)


def _text(value: Any) -> str:
    """Trimmed text of a field, or ``""`` when it carries nothing.

    Only strings and numbers ARE text here. Booleans are not (``True`` as a
    title or as an id is corrupt data, not the string ``"True"``), and neither
    is any other object: an attribute that turned out to be a method or a
    nested structure states nothing about a product, so it reads as empty
    instead of leaking its ``repr`` into a title comparison.
    """

    if value is None or isinstance(value, bool) or callable(value):
        return ""
    if not isinstance(value, (str, int, float)):
        return ""
    return str(value).strip()


def _number(value: Any) -> float | None:
    """Finite float of a numeric field, or ``None`` when it is not one.

    Numbers arrive as numbers and occasionally as numeric strings, and only
    those two forms are read. A locale-formatted string is NOT re-interpreted
    (``"1,5"`` is not silently turned into ``15``): guessing here would invent
    a price, which this layer never does.
    """

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) else None


def _count(value: Any) -> int | None:
    """Whole non-negative count of an ``orders``-like field."""

    number = _number(value)
    if number is None or number < 0.0:
        return None
    return int(number)


def title_tokens(title: Any) -> frozenset[str]:
    """Token set of a title: HTML stripped, lower-cased, short words dropped.

    Deliberately dumb and explainable: ``<b>RGB</b> Механическая клавиатура``
    and ``механическая клавиатура rgb`` produce the same set, and the set is the
    ONLY thing this layer compares (similarity and the diversity type). Nothing
    here depends on the WORD ORDER: two reworded listings of one product must be
    recognised as one kind of thing.

    A SHORT token is dropped as filler ("с", "для") - unless it states a number.
    "iPhone 13" and "iPhone 14" are two different products, and a similarity that
    cannot see the number would declare them one family and hide one of them from
    the user; the same is true of "Redmi Note 10/12" or "Чехол 6.7/6.1".
    """

    plain = _HTML_TAG_RE.sub(" ", _text(title)).casefold()
    return frozenset(
        token
        for token in _TOKEN_RE.findall(plain)
        if len(token) >= MIN_TOKEN_LENGTH or _DIGIT_RE.search(token)
    )


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    """Jaccard index of two token sets (``0.0`` when either is empty)."""

    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _numbers_of(tokens: frozenset[str]) -> frozenset[str]:
    """The number tokens of a title: the model, the size, the generation."""

    return frozenset(token for token in tokens if _DIGIT_RE.search(token))


def _numbers_disagree(left: frozenset[str], right: frozenset[str]) -> bool:
    """Whether two titles state DIFFERENT numbers - and are different products.

    "Чехол силиконовый для iPhone 13" and the same title for iPhone 14 share
    every other word, so a token index alone calls them one family and one of
    two real products disappears. A number in a marketplace title is an
    IDENTITY, not a decoration ("Redmi Note 10"/"12", "60W"/"100W"), so titles
    that state different numbers are never similar. A title that states NO
    number makes no such claim, and the ordinary comparison decides.
    """

    left_numbers = _numbers_of(left)
    right_numbers = _numbers_of(right)
    if not left_numbers or not right_numbers:
        return False
    return left_numbers != right_numbers


def similarity(left: Any, right: Any) -> float:
    """Similarity of two TITLES (not token sets), 0.0 .. 1.0.

    The Jaccard index of their token sets, with one veto: titles stating
    DIFFERENT numbers are different products (:func:`_numbers_disagree`). ``0.0``
    when a side has no comparable token - with nothing to compare, two listings
    are not declared one family.
    """

    left_tokens = title_tokens(left)
    right_tokens = title_tokens(right)
    if _numbers_disagree(left_tokens, right_tokens):
        return 0.0
    return _jaccard(left_tokens, right_tokens)


def quality_key(candidate: Any) -> tuple[float, float, float]:
    """The marketplace's own signals, strongest first: orders, rating, % positive.

    Used only to choose BETWEEN the members of one family - never to sort a
    feed and never to invent a missing number: an absent signal is ``-1.0``, so
    it loses to any real value instead of competing with it.
    """

    orders = _count(field(candidate, *ORDER_FIELDS))
    rating = _number(field(candidate, *RATING_FIELDS))
    positive = _number(field(candidate, *POSITIVE_RATE_FIELDS))
    return (
        float(orders) if orders is not None else -1.0,
        rating if rating is not None else -1.0,
        positive if positive is not None else -1.0,
    )


def recommendation_score(candidate: Any) -> float:
    """The impulse-buy score of ONE candidate, over real marketplace facts.

    ``(orders * rating^2) / max(price, 1.0)`` (spec): a heavily ordered,
    highly rated listing wins - and the cheaper it is, the fewer orders it
    needs to prove the same pull. Nothing here is invented:

    * a rating the marketplace never stated counts as ``1.0`` - neither a
      boost nor a penalty - because the AliExpress affiliate API reports no
      0..5 stars at all, and every product would otherwise score 0;
    * a price below $1 (or a missing one, which :class:`QualityFilter`
      already rejects before ranking) is floored at ``1.0`` - no division
      by zero;
    * without a stated order count there is nothing to recommend on, so
      the score is ``0.0`` and the candidate keeps its input position
      among its own kind (Python's sort is stable).
    """

    orders = _count(field(candidate, *ORDER_FIELDS))
    rating = _number(field(candidate, *RATING_FIELDS))
    price = _number(field(candidate, *PRICE_FIELDS))

    order_count = float(orders) if orders is not None and orders > 0 else 0.0
    rating_factor = rating if rating is not None else 1.0
    price_factor = max(price, 1.0) if price is not None else 1.0
    return (order_count * rating_factor * rating_factor) / price_factor


def normalize_mode(mode: Any) -> str:
    """The known feed mode closest to ``mode``; anything else means ``search``.

    An unknown value (a typo, a mode of a future release) must never silently
    reorder a feed into something nobody asked for, so the fallback is the mode
    that does not reorder at all.
    """

    value = str(mode or "").strip().lower()
    return value if value in KNOWN_MODES else DEFAULT_MODE


def quality_mode(mode: Any) -> str:
    """The quality profile a curation ``mode`` filters under.

    Three profiles exist (spec): ``recommendation`` for impulse-buy feeds -
    ``recommendation`` itself and the rotation's ``popular``/``deals``
    cycles - ``search`` for a direct text/photo search, and ``category``
    (G24.8 P0.2) for broad category discovery, which must NOT inherit the
    recommendation $1..$20 / >= 300 gate. An unknown mode normalizes to
    ``search`` through :func:`normalize_mode`, so a typo can never apply
    the stricter $20 window by accident.
    """

    normalized = normalize_mode(mode)
    if normalized == MODE_CATEGORY:
        return MODE_CATEGORY
    return MODE_SEARCH if normalized == MODE_SEARCH else MODE_RECOMMENDATION


def discount_percent(candidate: Any) -> float | None:
    """The discount the marketplace reported, or arithmetic over its own prices.

    A stated discount (``discount_percent`` / ``discount`` / ``discount_rate``)
    always wins. When it is absent but BOTH prices came back and ``old_price``
    is genuinely higher, the percentage is computed from them - arithmetic over
    returned fields, never a guess. With either price missing the answer is
    ``None``, and the candidate simply ranks by its other real signals.
    """

    reported = _number(field(candidate, *DISCOUNT_FIELDS))
    if reported is not None:
        return reported

    old_price = _number(field(candidate, *OLD_PRICE_FIELDS))
    price = _number(field(candidate, *PRICE_FIELDS))
    if old_price is None or price is None:
        return None
    if old_price <= 0.0 or price <= 0.0 or old_price <= price:
        return None
    return round((old_price - price) / old_price * 100.0, 2)


def normalize_image_url(url: str | None) -> str:
    """Normalize image URL: strip size/compression tags, query params, whitespace and lower-case.

    Examples:
        https://ae01.alicdn.com/kf/S123.jpg_640x640.jpg?hash=... -> https://ae01.alicdn.com/kf/s123.jpg
        https://ae01.alicdn.com/kf/S123.png_220x220.png -> https://ae01.alicdn.com/kf/s123.png
        https://ae01.alicdn.com/kf/S123_640x640.jpg -> https://ae01.alicdn.com/kf/s123.jpg
    """
    if not url or not isinstance(url, str):
        return ""

    cleaned = url.split("?")[0].split("#")[0].strip().lower()
    # Strip double-extension size/compression tags: .jpg_640x640.jpg -> .jpg
    cleaned = re.sub(
        r"(\.(?:jpg|jpeg|png|webp|gif|heic|bmp))_(?:\d+x\d+|q\d+)\.[a-z0-9]+$",
        r"\1",
        cleaned,
    )
    # Strip single-extension size tags: _640x640.jpg -> .jpg
    cleaned = re.sub(r"_(?:\d+x\d+|q\d+)(\.[a-z0-9]+)$", r"\1", cleaned)
    # Strip trailing Alibaba cdn tags: _.webp -> empty
    cleaned = re.sub(r"_\.[a-z0-9]+$", "", cleaned)
    return cleaned


BANNED_KEYWORDS: tuple[str, ...] = (
    "100 шт",
    "500 шт",
    "1000 шт",
    "одноразов",
    "щёточки",
    "щеточки",
    "для ресни",
    "для наращиван",
    "салон",
    "патчи",
    "носки мужские",
    "носки женские",
    "базовые штаны",
    "простые трусы",
    "кабель usb",
    "защитное стекло",
)


def known_signals(candidate: Any) -> int:
    """How many of the marketplace's own quality facts this listing states.

    ``0`` for a listing the API described with nothing but a title and a price -
    which is not a verdict about the product, only about how much the
    marketplace said.
    """

    return sum(
        1
        for names in QUALITY_SIGNAL_FIELDS
        if _number(field(candidate, *names)) is not None
    )


def has_relevance(candidate: Any) -> bool:
    """Whether the candidate carries a relevance/rank score of its own."""

    return _number(field(candidate, *RELEVANCE_FIELDS)) is not None


def _marketplace_key(value: Any) -> str:
    """Lower-cased marketplace name of a value that may be an enum."""

    if value is None:
        return ""
    return str(getattr(value, "value", value)).strip().lower()


def _dominant_token(
    tokens: frozenset[str] | set[str], frequency: Mapping[str, int]
) -> str | None:
    """The batch's most-used token of ONE title; ties by the longer word.

    The tie-break is deliberately order-INDEPENDENT (longest word first, then
    alphabetically): the word order of a marketplace title says nothing about
    its kind, and two reworded listings of one product must be typed alike. With
    no usable signal at all they still land in the same type, because the rule
    never looks at where a word happens to stand.
    """

    if not tokens:
        return None
    best = max(tokens, key=lambda token: (frequency[token], len(token)))
    tied = [
        token
        for token in tokens
        if (frequency[token], len(token)) == (frequency[best], len(best))
    ]
    return min(tied)


class CandidateFilter(Protocol):
    """What :class:`CurationPipeline` needs from one stage."""

    #: Stage name, used by the report and by the logs.
    name: str

    def apply(self, candidates: Sequence[Any]) -> list[Any]:
        """Return the candidates that survived this stage, order preserved."""


class QualityFilter:
    """Stage 1: keep only the candidates that can be shown honestly.

    A candidate is rejected when a REQUIRED field is missing or empty
    (``product_id``, ``title``, ``price``) or when a field carries a value that
    cannot be true: a price that is not a finite positive number, a rating
    outside 0..5, a negative order count.

    Optional fields stay optional: their ABSENCE is normal (marketplaces leave
    facts out, and a card simply shows less), their corruption is not - a
    rating of ``900`` or a price of ``"дешёво"`` is broken data, not a product.

    On top of that contract a ``mode`` selects one of the published
    quality profiles (:data:`QUALITY_PROFILES`, spec):

    * ``recommendation`` (impulse-buy feeds, and the rotation's
      ``popular``/``deals`` cycles through :func:`quality_mode`): price
      $1..$20, orders >= 300, rating >= 4.5;
    * ``search`` (a direct text/photo search): price from $1 with no
      ceiling, rating >= 4.5 - and NO orders floor. The orders field of a
      keyword answer (``lastest_volume``) is a recent-window sales figure,
      so an orders threshold here would hide ordinary relevant products
      instead of proving anything wrong (live-verified 2026; popularity
      stays in the ordering, see :data:`QUALITY_PROFILES`);
    * ``category`` (G24.8 P0.2): the structural contract only - broad
      category discovery keeps every honestly-showable product whatever
      its price, order count or (absent) rating. The relevance filter has
      already judged topical fit BEFORE this gate runs, and eligibility
      never depends on the request currency.

    Mode-driven thresholds judge only facts the marketplace STATES: an
    absent order count or star rating cannot be verified, and this layer
    never turns an unstated fact into an empty feed (the AliExpress
    affiliate API states no star rating at all - see
    :data:`QUALITY_MIN_RATING`). Explicit constructor thresholds keep the
    original strict behaviour: an absent fact IS rejected there. Explicit
    arguments always win over the profile's defaults.

    Price bounds are USD-denominated and only judge USD-priced candidates
    (``currency`` absent or ``USD``): the project converts no currencies,
    so a price stated in another unit is left to the marketplace's own
    facts instead of being compared against foreign numbers.
    """

    name = "quality"

    def __init__(
        self,
        *,
        required_fields: Sequence[str] = REQUIRED_FIELDS,
        min_orders: int | None = None,
        min_rating: float | None = None,
        max_price: float | None = None,
        min_price: float | None = None,
        mode: Any = None,
    ) -> None:
        #: The quality profile this filter enforces (``None``: only the
        #: thresholds the caller stated explicitly - the original contract).
        self.mode = quality_mode(mode) if mode is not None else None
        profile = QUALITY_PROFILES.get(self.mode) if self.mode else None
        #: The published contract of this stage; the reads below are what
        #: actually enforces it, with the field aliases of this module.
        self.required_fields = tuple(required_fields)
        self.min_orders = (
            min_orders if min_orders is not None
            else profile.min_orders if profile else None
        )
        self.min_rating = (
            min_rating if min_rating is not None
            else profile.min_rating if profile else None
        )
        self.min_price = (
            min_price if min_price is not None
            else profile.min_price if profile else None
        )
        self.max_price = (
            max_price if max_price is not None
            else profile.max_price if profile else None
        )
        #: Mode-driven bounds apply only to facts the marketplace STATES
        #: (see the class docstring); an explicit threshold without a mode
        #: keeps the strict "absent fact is rejected" contract.
        self.lenient_missing = self.mode is not None
        #: The recommendation feed's documented empty-state relaxation: when
        #: the strict $1..$20 window keeps NOTHING, :meth:`apply` retries the
        #: pool once at this ceiling - every other threshold stays put.
        self.fallback_max_price = (
            MAX_PRICE_USD
            if self.mode == MODE_RECOMMENDATION and self.max_price is not None
            else None
        )

    @staticmethod
    def _price_bounds_apply(candidate: Any) -> bool:
        """Whether the USD price bounds may judge this candidate's price.

        The thresholds are USD (spec) and the project converts no
        currencies, so only a price the marketplace stated in USD - or in
        no currency at all (the API's documented default is USD) - is
        compared against them. A price in the user's preferred currency
        keeps its own marketplace facts: orders and rating still apply.
        """

        currency = _text(field(candidate, "currency", "price_currency"))
        return currency.upper() in ("", "USD")

    def reason(self, candidate: Any) -> str | None:
        """The rejection reason, or ``None`` when the candidate passes.

        Exposed next to :meth:`is_valid` so a caller can log WHY a listing was
        dropped instead of only that it was. The check is single-pass: the
        empty-state relaxation of :meth:`apply` never hides a reason.
        """

        if candidate is None or isinstance(candidate, NOT_A_CANDIDATE_TYPES):
            # ``None``, a bare string, a number, a list: not a candidate at all.
            return REASON_MALFORMED

        if not isinstance(candidate, Mapping) and not has_field(
            candidate, *(PRODUCT_ID_FIELDS + TITLE_FIELDS + PRICE_FIELDS)
        ):
            # A structure that carries none of the facts is not a candidate
            # either, whatever it is.
            return REASON_MALFORMED

        if not _text(field(candidate, *PRODUCT_ID_FIELDS)):
            return REASON_PRODUCT_ID

        title = _text(field(candidate, *TITLE_FIELDS))
        if not title or not _WORD_RE.search(title):
            # Empty, whitespace-only or pure punctuation ("!!!") states nothing.
            return REASON_TITLE

        raw_price = field(candidate, *PRICE_FIELDS)
        if raw_price is None:
            return REASON_PRICE
        price = _number(raw_price)
        if price is None or price <= 0.0:
            return REASON_PRICE_VALUE
        if self._price_bounds_apply(candidate):
            if self.min_price is not None and price < self.min_price:
                return "price_too_low"
            if self.max_price is not None and price > self.max_price:
                return "price_too_high"

        raw_rating = field(candidate, *RATING_FIELDS)
        if raw_rating is not None:
            rating = _number(raw_rating)
            if rating is None or not 0.0 <= rating <= MAX_RATING:
                return REASON_RATING
            if self.min_rating is not None and rating < self.min_rating:
                return "rating_too_low"
        elif self.min_rating is not None and not self.lenient_missing:
            return "missing_rating"

        raw_orders = field(candidate, *ORDER_FIELDS)
        if raw_orders is not None:
            orders = _count(raw_orders)
            if orders is None:
                return REASON_ORDERS
            if self.min_orders is not None and orders < self.min_orders:
                return "orders_too_low"
        elif self.min_orders is not None and not self.lenient_missing:
            return "missing_orders"

        return None

    def is_valid(self, candidate: Any) -> bool:
        """Whether the candidate passes the gate."""

        return self.reason(candidate) is None

    def apply(self, candidates: Sequence[Any]) -> list[Any]:
        """Drop every invalid candidate; the survivors keep their order.

        One documented exception: a RECOMMENDATION feed whose strict
        $1..$20 window (``mode="recommendation"``) kept nothing is retried
        once at the fallback ceiling (:data:`MAX_PRICE_USD`, $50) - only
        the ceiling relaxes; the >= 300 orders floor, the $1 floor, the
        rating bound and every other rule stay exactly as strict. When the
        retry also finds nothing, the pool really is empty and the caller
        decides what an empty feed shows.
        """

        kept = [candidate for candidate in candidates if self.is_valid(candidate)]
        if kept or not candidates or self.fallback_max_price is None:
            return kept
        relaxed = QualityFilter(
            required_fields=self.required_fields,
            min_orders=self.min_orders,
            min_rating=self.min_rating,
            min_price=self.min_price,
            max_price=self.fallback_max_price,
            mode=self.mode,
        )
        return [candidate for candidate in candidates if relaxed.is_valid(candidate)]


class ImageDeduplicationFilter:
    """Deduplicate candidates based on normalize_image_url(product.image_url).

    When matching URLs are found, keep only 1 card with the highest (orders, rating) score.
    """

    name = "image_deduplication"

    def apply(self, candidates: Sequence[Any]) -> list[Any]:
        result: list[Any] = []
        url_to_index: dict[str, int] = {}

        for candidate in candidates:
            raw_url = field(candidate, "image_url", "product_main_image_url", "image")
            norm_url = normalize_image_url(raw_url)
            if not norm_url:
                result.append(candidate)
                continue

            if norm_url in url_to_index:
                idx = url_to_index[norm_url]
                current_best = result[idx]
                if quality_key(candidate)[:2] > quality_key(current_best)[:2]:
                    result[idx] = candidate
            else:
                url_to_index[norm_url] = len(result)
                result.append(candidate)

        return result


class BoredomFilter:
    """Stage to suppress B2B supplies, salon consumables, and generic boring items in curated feeds.

    - Banned keywords: "100 шт", "500 шт", "1000 шт", "одноразов", "щёточки", "щеточки",
      "для ресни", "для наращиван", "салон", "патчи", "носки мужские", "носки женские",
      "базовые штаны", "простые трусы", "кабель usb", "защитное стекло".
    - Price Floor: Filter out items with price < 0.99 in curated feeds (mode="popular", mode="deals").
    - Strict Constraint: Allow all items if mode == "search".
    """

    name = "boredom"

    def __init__(self, mode: Any = DEFAULT_MODE, *, price_floor: float = 0.99) -> None:
        self.mode = normalize_mode(mode)
        self.price_floor = price_floor

    def reason(self, candidate: Any, mode: Any = None) -> str | None:
        current_mode = normalize_mode(mode) if mode is not None else self.mode
        if current_mode == MODE_SEARCH:
            return None

        # Price Floor check
        raw_price = field(candidate, "price_usd", *PRICE_FIELDS)
        if raw_price is not None:
            price = _number(raw_price)
            if price is not None and price < self.price_floor:
                return "price_floor"

        # Banned keywords check
        title = _text(field(candidate, *TITLE_FIELDS)) or ""
        title_lower = title.lower()
        for banned in BANNED_KEYWORDS:
            if banned in title_lower:
                return f"banned_keyword:{banned}"

        return None

    def is_valid(self, candidate: Any, mode: Any = None) -> bool:
        return self.reason(candidate, mode=mode) is None

    def apply(self, candidates: Sequence[Any], mode: Any = None) -> list[Any]:
        return [c for c in candidates if self.is_valid(c, mode=mode)]


class DeduplicationFilter:
    """Stage 2: one offer per product family - the family's best one.

    A marketplace search answers one intent with many wordings of the same item
    (different stores, another colour, the words reordered, HTML tags around the
    brand). Exact id dedup cannot see them: their ids really are different. The
    Jaccard index of their token sets can, and above
    :data:`SIMILARITY_THRESHOLD` they are ONE family.

    The user is then shown the family's best offer: the one with the most
    orders (``orders`` / ``volume``), with the rating as an honest tie-break.
    The family keeps the slot of its FIRST member, so the caller's ranking
    survives; nothing is invented, and a listing with no comparable token is
    never merged - with nothing to compare, it is simply another product.
    """

    name = "deduplication"

    def __init__(self, threshold: float = SIMILARITY_THRESHOLD) -> None:
        if not 0.0 < threshold <= 1.0:
            raise ValueError("threshold must be inside (0, 1]")
        #: Sharing this fraction of tokens makes two titles one family.
        self.threshold = threshold

    def tokens(self, candidate: Any) -> frozenset[str]:
        """Token set of a candidate's title."""

        return title_tokens(field(candidate, *TITLE_FIELDS))

    def score(self, left: Any, right: Any) -> float:
        """How similar two candidates' titles are (0.0 = different products)."""

        return self._score_tokens(self.tokens(left), self.tokens(right))

    @staticmethod
    def _score_tokens(left: frozenset[str], right: frozenset[str]) -> float:
        """Similarity of two ALREADY tokenized titles - one rule, one place.

        The number veto comes first: two listings of one product for DIFFERENT
        models/sizes ("iPhone 13" vs "iPhone 14") are two products, however many
        words they share. Every caller (``score`` here and the family pass in
        ``apply``) goes through this method, so no path can bypass the rule.
        """

        if _numbers_disagree(left, right):
            return 0.0
        return _jaccard(left, right)

    def similar(self, left: Any, right: Any) -> bool:
        """Whether two candidates describe one product family."""

        return self.score(left, right) >= self.threshold

    def best(self, left: Any, right: Any) -> Any:
        """The stronger offer of one family; ``left`` wins a tie (first seen)."""

        return right if quality_key(right) > quality_key(left) else left

    def apply(self, candidates: Sequence[Any]) -> list[Any]:
        """Collapse families, keeping each family's best offer in place."""

        families: list[Any] = []
        family_tokens: list[frozenset[str]] = []

        for candidate in candidates:
            tokens = self.tokens(candidate)
            if not tokens:
                # Nothing to compare: unrecognizable listings stay their own
                # product instead of merging with every other silent one.
                families.append(candidate)
                family_tokens.append(tokens)
                continue

            for index, other in enumerate(family_tokens):
                if not other or self._score_tokens(tokens, other) < self.threshold:
                    continue
                winner = self.best(families[index], candidate)
                families[index] = winner
                family_tokens[index] = self.tokens(winner)
                break
            else:
                families.append(candidate)
                family_tokens.append(tokens)

        return families


class HistoryFilter:
    """Stage 3: keep the feed novel - without ever emptying it.

    The bot's per-user view history (``user_view_history``) is a NOVELTY signal,
    not a blacklist:

    * when the unseen candidates alone can fill a screen (:data:`FRESH_ENOUGH`),
      the already-seen ones are removed - a feed of new cards is strictly more
      useful than a repeat of yesterday's;
    * when they cannot, the seen ones RETURN at the tail, so a user who has
      already met everything still gets a full feed instead of an empty screen,
      and the order of the fresh part is left exactly as the ranking made it.

    ``seen_product_ids`` is a SNAPSHOT of what the user has been shown - the
    async caller reads it (``await db.get_user_seen_ids(user_id)``) because this
    layer stays pure and synchronous - or a callable receiving ``user_id`` (an
    in-process cache, for example). Entries may be plain ids, ``(marketplace,
    product_id)`` pairs (exactly what ``Database.get_shown_products`` returns) or
    candidate-shaped mappings/objects; the snapshot is normalised ONCE in
    ``__init__``, so a feed never re-reads it per candidate.
    """

    name = "history"

    def __init__(
        self,
        seen_product_ids: Any = None,
        *,
        user_id: int | None = None,
        min_fresh: int = FRESH_ENOUGH,
    ) -> None:
        self.user_id = user_id
        #: Below this many unseen candidates the seen ones come back.
        self.min_fresh = max(0, int(min_fresh))
        self.source = seen_product_ids
        self._ids, self._pairs = self._snapshot()

    def _snapshot(self) -> tuple[frozenset[str], frozenset[tuple[str, str]]]:
        """Normalise the history into ids and ``(marketplace, id)`` pairs."""

        source = self.source
        if source is None:
            return frozenset(), frozenset()
        if callable(source):
            source = source(self.user_id)

        ids: set[str] = set()
        pairs: set[tuple[str, str]] = set()
        for entry in source or ():
            if isinstance(entry, str):
                product_id = _text(entry)
                if product_id:
                    ids.add(product_id)
                continue
            if isinstance(entry, (tuple, list)) and len(entry) == 2:
                marketplace = _marketplace_key(entry[0])
                product_id = _text(entry[1])
                if marketplace and product_id:
                    pairs.add((marketplace, product_id))
                continue
            product_id = _text(field(entry, *PRODUCT_ID_FIELDS))
            if product_id:
                ids.add(product_id)
        return frozenset(ids), frozenset(pairs)

    def is_seen(self, candidate: Any) -> bool:
        """Whether this candidate is in the user's history."""

        product_id = _text(field(candidate, *PRODUCT_ID_FIELDS))
        if not product_id:
            # A candidate without an id cannot be matched to anything, and
            # guessing a match would hide a product for no reason.
            return False
        if product_id in self._ids:
            return True
        marketplace = _marketplace_key(field(candidate, "marketplace"))
        return bool(marketplace) and (marketplace, product_id) in self._pairs

    def apply(self, candidates: Sequence[Any]) -> list[Any]:
        """Drop the already-seen candidates, or move them to the tail."""

        pool = list(candidates)
        if not self._ids and not self._pairs:
            return pool

        fresh = [candidate for candidate in pool if not self.is_seen(candidate)]
        if len(fresh) >= self.min_fresh:
            return fresh
        seen = [candidate for candidate in pool if self.is_seen(candidate)]
        return fresh + seen


class ModeRanker:
    """Stage 4: order the feed the way ONE mode means it, over real signals only.

    * ``deals``   - the biggest discount first, then the most ordered listing
      (a stated ``discount_percent`` / ``discount``, or the arithmetic of
      ``old_price`` and ``price`` when both were returned);
    * ``popular`` - the most ordered listing first, the rating breaking the tie
      (the same :func:`quality_key` the family dedup uses, so "popular" means
      ONE thing in this layer);
    * ``search``  - the API's relevance order IS the answer, so the list comes
      back untouched... unless the candidates carry a relevance of their own
      (``relevance`` / ``rank_score``, supplied by the caller - curation never
      invents one). Then the order is relevance first and the number of stated
      quality facts second, so a well-described listing lifts above a thin one
      only when the relevance is equal - exactly "при прочих равных".
    * ``category`` (G24.8 P1.1) - RELEVANCE FIRST, quality/value second:
      the wiring layer hands the stage a per-candidate ``(tier, score)``
      relevance map computed by the SAME ``services.relevance`` functions
      the service layer sorted with (curation never recomputes or invents
      relevance itself), and the order becomes ``group -> tier -> score ->
      stated quality facts -> positive_rate -> rating -> orders -> price``.
      A missing signal loses to any real one, an unknown price sorts last,
      price only decides fully-tied neighbours, and ties keep their
      arrival order (the sort is stable) - so no single metric (orders,
      price or rating) can order the list by itself. The impulse-buy score
      of ``recommendation`` is never used here and no signal is a
      threshold. Without a relevance map (unit callers, older wiring) the
      P0.2 contract holds: the received order is kept.

    A missing signal is ``-1.0``: it loses to any real value but keeps its input
    position among its own kind, because Python's sort is stable. Ranking only
    REORDERS: nothing is dropped, copied or completed here.

    ``deferred`` is an optional predicate (the history stage's ``is_seen``) that
    marks the cards the user has already met. They rank BEHIND every fresh one
    inside EVERY mode: no discount or order count may promote a card the user
    has seen above a new one, so the novelty the history stage established
    survives the re-sorting this stage exists to do.
    """

    name = "mode"

    def __init__(
        self,
        mode: Any = DEFAULT_MODE,
        *,
        deferred: Any = None,
        relevance: Any = None,
    ) -> None:
        self.mode = normalize_mode(mode)
        #: Callable marking a candidate as "already seen" (or ``None``).
        self.deferred = deferred
        #: Per-candidate ``(tier, score)`` relevance keyed by
        #: ``id(candidate)`` - supplied by the wiring layer for
        #: ``MODE_CATEGORY`` only (G24.8 P1.1). ``None`` means "no map":
        #: category then keeps the received order (the P0.2 contract) and
        #: every other mode behaves exactly as before.
        self.relevance = relevance

    def group(self, candidate: Any) -> float:
        """``1.0`` for a candidate that may be shown, ``0.0`` for a deferred one."""

        if self.deferred is None or not self.deferred(candidate):
            return 1.0
        return 0.0

    def sort_key(self, candidate: Any) -> tuple[float, ...]:
        """The sort key of this mode, largest first (fresh before deferred)."""

        group = self.group(candidate)

        if self.mode == MODE_DEALS:
            discount = discount_percent(candidate)
            orders = _count(field(candidate, *ORDER_FIELDS))
            rating = _number(field(candidate, *RATING_FIELDS))
            return (
                group,
                discount if discount is not None else -1.0,
                float(orders) if orders is not None else -1.0,
                rating if rating is not None else -1.0,
            )

        if self.mode == MODE_POPULAR:
            orders, rating, positive = quality_key(candidate)
            return (group, orders, rating, positive)

        if self.mode == MODE_CATEGORY:
            # G24.8 P1.1: RELEVANCE FIRST, quality/value second. Stage A is
            # the wiring layer's per-candidate (tier, score) - the SAME
            # services.relevance values the service layer sorted with.
            # Stages B/C/D follow as one staged chain: stated quality
            # facts, then each real signal (missing -> -1, which loses to
            # any stated value), price LAST as a pure tie-break (unknown
            # price -> -inf sorts behind every stated one on this reverse
            # sort). Stable sort: a full tie keeps the arrival order, so
            # no single metric (orders, price, rating) can order the list
            # by itself. The recommendation formula is never used here.
            mapped = self.relevance.get(id(candidate)) if self.relevance else None
            tier, score = mapped if mapped is not None else (0, 0.0)
            positive = _number(field(candidate, *POSITIVE_RATE_FIELDS))
            rating = _number(field(candidate, *RATING_FIELDS))
            orders = _count(field(candidate, *ORDER_FIELDS))
            price = _number(field(candidate, *PRICE_FIELDS))
            return (
                group,
                float(tier),
                float(score),
                float(known_signals(candidate)),
                positive if positive is not None else -1.0,
                rating if rating is not None else -1.0,
                float(orders) if orders is not None else -1.0,
                -price if price is not None else -math.inf,
            )

        if self.mode == MODE_RECOMMENDATION:
            # The impulse-buy order (spec): (orders * rating^2) / max(price, 1)
            # over real facts only.
            return (group, recommendation_score(candidate))

        relevance = _number(field(candidate, *RELEVANCE_FIELDS))
        return (
            group,
            relevance if relevance is not None else -1.0,
            float(known_signals(candidate)),
        )

    def apply(self, candidates: Sequence[Any]) -> list[Any]:
        """Return the same candidates in this mode's order."""

        pool = list(candidates)
        keep_order = (
            self.mode == MODE_SEARCH
            or (self.mode == MODE_CATEGORY and self.relevance is None)
        ) and not any(has_relevance(candidate) for candidate in pool)
        if keep_order:
            if self.deferred is None:
                # Nobody stated a relevance (and a category pool without a
                # relevance map): the order we received IS the answer, and
                # there is no group to move anywhere.
                return pool
            # The received order is still the answer, but a card the user has
            # already met belongs behind the fresh ones; the sort is stable, so
            # each group keeps the order it arrived in.
            return sorted(pool, key=self.group, reverse=True)
        return sorted(pool, key=self.sort_key, reverse=True)


#: Relational (preposition) words: the taxonomy-label candidacy filter of
#: :meth:`DiversityFilter.labels` (G24.8 P1.6-FIX). A relational word never
#: NAMES a kind of product, yet it repeats in nearly every title of a category
#: (live audit 2026: ``"для"`` in 192 of 201 real titles) - counted as a
#: batch-wide frequency it beats every content word and degenerates the whole
#: feed's labels into one filler. Deliberately a CLOSED grammatical class of
#: 3+ character words (shorter ones never reach candidacy, see
#: :data:`MIN_TOKEN_LENGTH`), NOT a general stopword list: it is subtracted
#: ONLY when taxonomy-label candidates are built. The shared token stream
#: (:func:`title_tokens`, :func:`similarity`, deduplication) keeps every one
#: of these words, and this set is never imported by search, category or
#: ``services.relevance`` - their semantics are out of this layer's scope.
_TAXONOMY_LABEL_RELATIONAL_WORDS = frozenset(
    {
        # Russian prepositions.
        "для", "без", "над", "под", "про", "при", "через", "после",
        "между", "около", "вместо", "вокруг", "среди", "против", "внутри",
        # English relational words seen in live titles.
        "for", "with", "the", "and", "from", "into", "onto", "that",
    }
)


class DiversityFilter:
    """Stage 5: keep ONE product type from taking a whole block of cards.

    A marketplace answers "клавиатура" with keyboards, and the third keyboard on
    a screen tells the user nothing new. The type of a listing is decided by the
    listing's own words - the token the BATCH uses for the category (the word
    appearing in the most titles, which unifies reworded listings; among equally
    used words the longer one wins), so a feed needs no hand-written list of
    categories: the only exclusion is the closed grammatical class of relational
    words (:data:`_TAXONOMY_LABEL_RELATIONAL_WORDS`) - a part of speech that
    never names a product, never a forbidden content word.
    The type never depends on the WORD ORDER: two reworded listings of one
    product are one type.

    At most :data:`MAX_PER_TYPE` products of one type fit into a block of
    :data:`DIVERSITY_BLOCK` cards. The excess is NOT deleted: it moves to a
    later block (the next screen of the same feed), and only when nothing else
    can fill the gap does the excess come back - a short feed is worse than a
    repeated family, and no valid product may be lost by reordering.
    """

    name = "diversity"

    def __init__(
        self,
        *,
        max_per_type: int = MAX_PER_TYPE,
        block_size: int = DIVERSITY_BLOCK,
        tier_of: Any | None = None,
    ) -> None:
        if block_size <= 0:
            raise ValueError("block_size must be positive")
        if max_per_type <= 0:
            raise ValueError("max_per_type must be positive")
        self.max_per_type = int(max_per_type)
        self.block_size = int(block_size)
        #: G25.2-C Stage 7: optional semantic-dominance hook. ``None``
        #: keeps the original whole-list behaviour byte-identical (every
        #: legacy caller, every mode except text search); a callable
        #: returning the relevance tier (2 exact / 1 mentioned / 0
        #: unverified) makes diversity operate INSIDE one tier band, so
        #: subtype spacing can never lift a strictly weaker tier above a
        #: strictly stronger one while diversity WITHIN a tier stays.
        self.tier_of: Any | None = tier_of

    def labels(self, candidates: Sequence[Any]) -> list[str | None]:
        """The type word of every candidate (``None`` when it has no words).

        Computed over the WHOLE batch, once: the batch is what tells the layer
        which word means "the same kind of thing" here.

        A RELATIONAL word never names a kind of product, and it repeats in
        nearly every title of a category - as a batch-wide frequency it would
        beat every content word and type the whole feed as one filler. So the
        candidates come from the shared token stream MINUS
        :data:`_TAXONOMY_LABEL_RELATIONAL_WORDS`, and only HERE: the shared
        stream itself (``title_tokens``, similarity, deduplication) keeps every
        token exactly as before. A title left with nothing eligible stays
        ``None`` and is therefore never limited (see :meth:`apply`).
        """

        token_sets = [
            frozenset(title_tokens(field(candidate, *TITLE_FIELDS)))
            - _TAXONOMY_LABEL_RELATIONAL_WORDS
            for candidate in candidates
        ]
        frequency: dict[str, int] = {}
        for tokens in token_sets:
            for token in tokens:
                frequency[token] = frequency.get(token, 0) + 1
        return [_dominant_token(tokens, frequency) for tokens in token_sets]

    def apply(self, candidates: Sequence[Any]) -> list[Any]:
        """Reorder so no block of cards is one type repeated.

        G25.2-C Stage 7 semantic-dominance invariant (only when
        ``tier_of`` is set - text search): diversity reorders WITHIN one
        relevance band, and bands are concatenated strongest-first
        (2 -> 1 -> 0, unknown last). A materially stronger semantic
        result can therefore never be displaced by weaker products
        merely because of subtype spacing, while diversity among
        near-equivalent candidates is untouched. With ``tier_of`` unset
        the original whole-list algorithm runs byte-identically.
        """
        pool = list(candidates)
        if len(pool) < 2:
            return pool
        if self.tier_of is None:
            return self._spread(pool)

        bands: dict[Any, list] = {}
        for candidate in pool:
            try:
                tier: Any = self.tier_of(candidate)
            except Exception:  # a bad tier hook must never break a feed
                tier = None
            bands.setdefault(tier, []).append(candidate)

        def band_key(value: Any) -> tuple[int, int]:
            return (1, 0) if not isinstance(value, int) else (0, -value)

        ordered: list = []
        for band in sorted(bands, key=band_key):
            ordered.extend(self._spread(bands[band]))
        return ordered

    def _spread(self, pool: list) -> list:
        """The original block-diversity algorithm over ONE band (unchanged)."""
        labels = self.labels(pool)
        accepted: list[int] = []

        def place(index: int) -> bool:
            """Append the candidate when its type still has room in the block."""

            label = labels[index]
            if label is None:
                # A title with no comparable word cannot be typed, and an
                # untyped listing must not be limited on a guess.
                accepted.append(index)
                return True
            block_start = (len(accepted) // self.block_size) * self.block_size
            taken = sum(
                1 for other in accepted[block_start:] if labels[other] == label
            )
            if taken >= self.max_per_type:
                return False
            accepted.append(index)
            return True

        pending = list(range(len(pool)))
        while pending:
            deferred: list[int] = []
            for index in pending:
                if not place(index):
                    deferred.append(index)
            if len(deferred) == len(pending):
                # Nothing fits anywhere: the pool is one type only, so the
                # rest returns at the tail in its own order instead of being
                # dropped.
                accepted.extend(deferred)
                break
            pending = deferred

        return [pool[index] for index in accepted]


@dataclass(frozen=True)
class StageStats:
    """How many candidates one stage received and how many it kept."""

    name: str
    received: int
    kept: int

    @property
    def dropped(self) -> int:
        """Candidates this stage removed."""

        return self.received - self.kept


@dataclass(frozen=True)
class CurationReport:
    """Per-stage counters of the most recent :meth:`CurationPipeline.process`."""

    stages: tuple[StageStats, ...] = ()

    @property
    def kept(self) -> int:
        """How many candidates the whole chain returned."""

        return self.stages[-1].kept if self.stages else 0

    def dropped(self, name: str) -> int:
        """How many candidates the stage called ``name`` removed."""

        for stage in self.stages:
            if stage.name == name:
                return stage.dropped
        return 0


class CurationPipeline:
    """The curation chain, in ONE order: quality, dedup, history, mode, diversity.

    ``process`` returns a SUBSET of the input objects in input order - it never
    mutates, copies or completes a candidate - so it is safe to run over live
    provider data and trivial to assert on with hand-written dicts.

    The order carries meaning: a broken listing is dropped BEFORE dedup (so
    corrupt orders can never win a family), novelty is applied before ranking (a
    seen product must not push a fresh one off the screen) and the ranking is
    told which cards were deferred, so a re-sorting mode cannot lift them back to
    the top; diversity runs LAST, over the final order the ranking produced.
    """

    def __init__(
        self,
        filters: Sequence[CandidateFilter] | None = None,
        *,
        mode: Any = DEFAULT_MODE,
        min_fresh: int = FRESH_ENOUGH,
    ) -> None:
        self.quality = QualityFilter()
        self.deduplication = DeduplicationFilter()
        self.history = HistoryFilter(min_fresh=min_fresh)
        self.ranker = ModeRanker(mode)
        self.diversity = DiversityFilter()
        self._custom = list(filters) if filters is not None else None
        #: The default chain, in order. A caller may pass its own chain (a
        #: different order, an extra stage, a stub in a test) instead.
        self.filters: list[CandidateFilter] = (
            self._custom
            if self._custom is not None
            else [
                self.quality,
                self.deduplication,
                self.history,
                self.ranker,
                self.diversity,
            ]
        )
        self.last_report = CurationReport()

    def chain(
        self,
        *,
        mode: Any = None,
        seen_ids: Any = None,
        user_id: int | None = None,
        relevance: Any = None,
    ) -> list[CandidateFilter]:
        """The stages ONE call runs, with this call's own mode and history.

        The history and ranking stages are built FRESH per call on purpose: two
        feeds (two users, or two screens of one user) must never share a
        seen-ids snapshot or a mode, and the pipeline stays stateless and safe
        to use from concurrent tasks.
        """

        if self._custom is not None:
            return list(self._custom)
        history = HistoryFilter(
            seen_ids, user_id=user_id, min_fresh=self.history.min_fresh
        )
        current_mode = mode or self.ranker.mode
        diversity: CandidateFilter = self.diversity
        # RAW equality on purpose (Stage-7 section 7): normalize_mode
        # maps unknown strings («discovery», «feed», «image») to
        # 'search', and those flows must keep the shared legacy filter.
        if current_mode == MODE_SEARCH and relevance:
            # G25.2-C Stage 7: text search gets the semantic-dominance
            # invariant - the wiring layer hands the (tier, score) map it
            # already builds for category, diversity reads TIER ONLY and
            # bands the list strongest-first. Every other mode (category,
            # popular, recommendation, discovery) keeps the shared legacy
            # filter above, byte-identically.
            tier_map = relevance

            def tier_of(candidate: Any) -> Any:
                mapped = tier_map.get(id(candidate))
                return mapped[0] if isinstance(mapped, tuple) else None

            diversity = DiversityFilter(tier_of=tier_of)
        return [
            # Built FRESH per call, like the history and ranking stages: the
            # thresholds of ONE feed must never leak into another's (the
            # central gate hands every flow its own mode, and each mode
            # reads its published profile from ``QUALITY_PROFILES``).
            QualityFilter(mode=current_mode),
            self.deduplication,
            history,
            # The ranker is handed the SAME predicate the history stage used, so
            # a re-sorting mode cannot lift a card the user has already met back
            # above a fresh one (one source of truth, no duplicated rule). The
            # relevance map (G24.8 P1.1) is read by MODE_CATEGORY only.
            ModeRanker(current_mode, deferred=history.is_seen, relevance=relevance),
            diversity,
        ]

    def process(
        self,
        candidates: Sequence[Any] | None,
        *,
        user_id: int | None = None,
        mode: Any = None,
        seen_ids: Any = None,
        relevance: Any = None,
    ) -> list[Any]:
        """Run the chain over ``candidates`` and return the survivors, in order.

        ``mode`` overrides the pipeline's own mode for this call; ``seen_ids`` is
        the already-shown snapshot, normally ``await db.get_user_seen_ids(
        user_id)`` read by the async caller; ``user_id`` labels the history stage
        and is handed to a callable ``seen_ids`` provider. ``relevance`` is the
        optional per-candidate ``(tier, score)`` map for ``MODE_CATEGORY``
        (G24.8 P1.1) - every other mode ignores it. Every parameter is
        optional: without them the chain is the plain deterministic pipeline.
        """

        current = list(candidates or [])
        stages: list[StageStats] = []

        for stage in self.chain(
            mode=mode, seen_ids=seen_ids, user_id=user_id, relevance=relevance
        ):
            received = len(current)
            current = list(stage.apply(current))
            stages.append(
                StageStats(
                    name=getattr(stage, "name", type(stage).__name__),
                    received=received,
                    kept=len(current),
                )
            )

        self.last_report = CurationReport(stages=tuple(stages))
        return current
