"""Discovery screens: ✨ Находки, 📂 Категории, 📷 Найти по фото.

Every screen goes through the same provider pipeline as a normal search,
so a failing or unconfigured marketplace degrades gracefully everywhere.

Nothing here invents products:
- «✨ Находки» (G24) is AliExpress' OWN keyword-less ``product.query``
  listing: ``SearchService.find_feed_page`` delivers one documented page
  (normalize -> dedup -> technical validity -> skip what this user has
  already been shown), and this handler only reads state, refills within
  the per-open page budget, renders and advances the cursor - there is no
  local popularity algorithm, no ranking and no quality judgement anywhere
  in this path;
- image search describes the picture via Gemini and then runs a normal
  AliExpress text search (the provider itself reports that image search
  is unsupported, see the module docstring of the provider).
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from curation.pipeline import MODE_CATEGORY
from database.models import FLOW_FEED
from handlers import common
from handlers.search import record_outcome_events, send_search_results
from keyboards.main import (
    CB_CATEGORIES,
    back_to_menu_keyboard,
    categories_keyboard,
    category_by_callback,
    feed_exhausted_keyboard,
    photo_retry_keyboard,
)
from models.product import Product
from models.search_request import SearchMode, SearchRequest
from services import analytics, feed_curation
from services.comparison_service import sort_products
from services.product_identity import deduplicate_products, identity_pair
from services.relevance import MIN_TERM_LENGTH, tokenize
from services.search_service import SearchOutcome
from utils.emoji import CATEGORIES, FINDINGS, PHOTO, WARNING

logger = logging.getLogger(__name__)

#: Engaging consumer categories pool for Popular category discovery
ENGAGING_CONSUMER_CATEGORIES: tuple[str, ...] = (
    "умный дом гаджеты",
    "автотовары аксессуары",
    "инструменты для дома",
    "туристическое снаряжение",
    "электроника для авто",
    "компьютерные аксессуары",
)
POPULAR_ENGAGING_CATEGORIES = ENGAGING_CONSUMER_CATEGORIES
POPULAR_CATEGORIES_POOL = ENGAGING_CONSUMER_CATEGORIES


#: key in ``context.user_data``: the AliExpress ``page_no`` the NEXT open
#: starts from. This is the per-session cursor of the feed, kept in RAM on
#: purpose - the state that must survive a restart is the per-user shown
#: history plus the persisted cursor (``popular_state``), and a lost RAM
#: cursor only costs one extra page. Advancing it is what makes two opens -
#: and an explicit «🔄 Начать заново» - ask a DIFFERENT part of the
#: marketplace listing instead of replaying the same first pages.
FEED_PAGE_KEY = "feed_page"

CATEGORIES_TEXT = (
    f"{CATEGORIES} <b>Категории</b>\n\n"
    "Выберите категорию — бот поищет товары в ней.\n"
    "Если нужного нет, просто опишите товар словами в сообщении."
)

FEED_EMPTY_TEXT = (
    f"{FINDINGS} <b>Находки</b>\n\n"
    "Не удалось получить подборку находок.\n\n"
    f"{WARNING} Товары не выдумываются: раздел показывает только то, что "
    "действительно вернул маркетплейс."
)

#: Shown when the feed works but every page of this open returned only
#: products this user has already been shown: an explicit end-of-session
#: screen with a real restart action - the same product is never re-sent
#: while pretending it is new. The wording promises nothing about the
#: marketplace and nothing about "everything": one open asks a bounded
#: number of documented pages, and «🔄 Начать заново» keeps the cursor and
#: continues from NEWER pages with a cleared history (see
#: ``feed_reset_callback``).
FEED_EXHAUSTED_TEXT = (
    f"{FINDINGS} <b>Находки</b>\n\n"
    "В этой части подборки новых для вас товаров нет: показывать те же "
    "товары повторно бот не будет.\n\n"
    f"{WARNING} Нажмите «🔄 Начать заново» — бот продолжит подборку "
    "с других товаров маркетплейса."
)

IMAGE_EMPTY_TEXT = (
    f"{PHOTO} <b>Поиск по фото</b>\n\n"
    "Не удалось определить товар на фотографии или найти похожие товары. "
    "Попробуйте другое фото."
)

#: Shown while the photo is being understood and searched for. No mention
#: of the model, the API or any implementation detail (§11 photo UX).
PHOTO_RECEIVED_TEXT = f"{PHOTO} Ищу товар по фотографии…"

#: Shown when recognition is unavailable at all (the vision step cannot
#: run right now). Ordinary users get the same short, natural message and
#: the same advice as an unsuccessful recognition - the technical reason
#: (AI service unavailable) is logged, never shown in the chat.
PHOTO_NO_AI_TEXT = IMAGE_EMPTY_TEXT

#: Photos are downloaded into memory, so oversized ones are refused with
#: an explanation instead of silently timing out.
MAX_IMAGE_BYTES = 5 * 1024 * 1024

#: How many marketplace searches ONE photo may cost (a limited number of
#: grounded hypotheses, never dozens). Every hypothesis must come from what
#: the image actually showed (see :func:`photo_hypotheses`), and the loop
#: stops early as soon as the merged result holds enough products. The
#: hypotheses are TIERED (exact visible name -> branded type -> whole visible
#: type -> the CORE head words of that type -> the pictured category), so a
#: niche listing cannot starve the pool while the broad question about the
#: same pictured product is still asking a real, grounded query. How many of
#: them a deployment is willing to pay for is ``PHOTO_SEARCH_HYPOTHESES``.
MAX_PHOTO_ATTEMPTS = 4

#: Hard ceiling on :data:`MAX_PHOTO_ATTEMPTS` when it comes from settings: the
#: number is a limit on real API calls, so a typo in ``.env`` must not turn one
#: photo into a crawl.
PHOTO_ATTEMPTS_LIMIT = 6

#: How many documented result PAGES one photo hypothesis may be asked for
#: (``PHOTO_SEARCH_PAGES``). Photo retrieval used to stop after the FIRST page
#: of every hypothesis, so a niche picture produced the few rows of one thin
#: page and the user got a handful of cards even though the marketplace held
#: many more of the pictured product. Deeper pages of the SAME grounded phrase
#: add real rows of the very product the picture showed - and never a word the
#: vision step did not read. Bounded (see :data:`PHOTO_PAGES_LIMIT`).
PHOTO_MAX_PAGES = 2

#: Hard ceiling on :data:`PHOTO_MAX_PAGES` from settings (API-call budget).
PHOTO_PAGES_LIMIT = 5

#: Unique validated products after which a photo stops asking for more.
#: Aligned with the 30-product result cap of every other flow: a photo is
#: supposed to open a POOL the user can browse, so the loop only stops
#: early when that pool is full - never after the first three products the
#: marketplace happened to return. The number of API calls stays bounded by
#: ``PHOTO_SEARCH_HYPOTHESES x PHOTO_SEARCH_PAGES``.
PHOTO_ENOUGH_RESULTS = 30

#: Two photo hypotheses of the SAME scope sharing this much of their wording
#: ask the marketplace the same question. The vision step routinely reads one
#: product twice («Чай зеленый Тимофей пакет 100 г» as the visible name,
#: «Тимофей Зеленый чай 100 г» as the readable packaging text), and spending
#: two of the bounded attempts on one question costs a genuinely different
#: representation - the broad one that fills the pool. Uses the same token-set
#: similarity (and value) the feed uses to call two listings one type.
PHOTO_PHRASE_DUPLICATE_SHARE = feed_curation.NEAR_DUPLICATE_THRESHOLD


#: Words a photo query must never END with: a fragment like «чай в» is a
#: parsing leftover, not a search a human would type (the observed bug:
#: a first-two-words truncation of a recognition answer produced exactly
#: such a phrase and sent it to the marketplace).
_PHOTO_EDGE_WORDS = frozenset(
    {
        "в", "во", "на", "с", "со", "для", "и", "к", "у", "о", "об", "из",
        "по", "за", "до", "от", "про", "что", "как", "the", "a", "an", "for",
        "of", "with", "and", "to", "in", "on", "at",
    }
)

#: Fields of the structured image analysis that NAME the product. One tuple
#: because both the hypotheses and the validation keywords read the same
#: analysis (§7 image understanding).
_ANALYSIS_NAME_FIELDS = (
    "product_name",
    "product_type",
    "brand",
    "model",
    "series",
    "visible_text",
)


#: Single-letter tokens that carry real meaning on packaging OCR and must
#: never be trimmed with the function words («100 г» is a weight, not a
#: preposition).
_PHOTO_UNITS = frozenset({"г"})


#: A broad photo hypothesis never goes below this many words: ONE vague word is
#: exactly the failure the whole photo flow exists to avoid (a single
#: recognised word answered a photo of a tea pack with a perfume).
PHOTO_MIN_CORE_WORDS = 2


def _clean_phrase(text: str | None) -> str:
    """One short line usable as a marketplace query, or ``''``.

    Collapses whitespace, drops stray punctuation at the edges, removes
    function words ANYWHERE («с», «для», «в» - they only noise up a
    marketplace query) and trims trailing ones a second time, so the phrase
    can never end in a preposition («чай в» → «чай»). Single-letter units
    like «г» are kept: a packaging weight is exactly the kind of detail
    the image showed.
    """

    cleaned = " ".join((text or "").replace("\n", " ").split())
    cleaned = cleaned.strip(" \t\"'«»()[]{},.;:!?")
    words = [
        word
        for word in cleaned.split()
        if word.lower().strip(".,;:!?") not in _PHOTO_EDGE_WORDS
        and (len(word) > 1 or word.lower() in _PHOTO_UNITS)
    ]
    while words and words[-1].lower().strip(".,;:!?)") in _PHOTO_EDGE_WORDS:
        words.pop()
    return " ".join(words)[:120].strip()


def _join_unique(*parts: str) -> str:
    """Join phrases into ONE query, repeating no word (case-insensitive)."""

    words: list[str] = []
    seen: set[str] = set()
    for part in parts:
        for word in part.split():
            if word.lower() not in seen:
                seen.add(word.lower())
                words.append(word)
    return " ".join(words)


def _first_clause(text: str | None) -> str:
    """The first line/comma clause of a longer text (packaging OCR)."""

    for line in (text or "").splitlines():
        if line.strip():
            return _clean_phrase(line.split(",")[0])
    return _clean_phrase((text or "").split(",")[0])


def _core_phrase(phrase: str) -> str:
    """The HEAD words of a visible type phrase, or ``''`` when it is already short.

    A marketplace answers a long, exact phrase («чай листовой зеленый»,
    «зубная щётка электрическая с зарядкой») with a thin, fuzzy first page, so
    the photo flow also asks the broad version of the same pictured product.
    Words are cut from the END, because the head noun of such a phrase stands
    first in Russian and in English alike, and the cut stops at
    :data:`PHOTO_MIN_CORE_WORDS`: a hypothesis can never collapse into the one
    vague word this pipeline exists to replace (the reported failure was a
    single recognised word answering a photo of tea with a perfume). Words the
    vision step did not write are never added.
    """

    words = _clean_phrase(phrase).split()
    if len(words) <= PHOTO_MIN_CORE_WORDS:
        return ""
    return " ".join(words[:PHOTO_MIN_CORE_WORDS])


def _bounded_setting(
    settings: object, name: str, default: int, maximum: int
) -> int:
    """Read ONE integer bound (page/hypothesis budget) from the settings.

    A missing settings object (a unit test, an older wiring) or a malformed
    value must never break a photo search: the documented default is used and
    the result is clamped to ``maximum``, because every unit here is a REAL
    marketplace call.
    """

    try:
        value = int(getattr(settings, name, default))
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, maximum))


def _question_words(phrase: str) -> frozenset[str]:
    """The words two questions are compared by (case-folded, meaningful length).

    The same view of a phrase the marketplace index works with: short function
    words («в», «г») prove nothing and are ignored, so «чай в» and «чай»
    compare as one question.
    """

    return frozenset(
        word for word in tokenize(phrase) if len(word) >= MIN_TERM_LENGTH
    )


def _repeats_an_earlier_question(phrase: str, asked: list[str]) -> bool:
    """Whether ``phrase`` asks the SAME question one of the ``asked`` ones did.

    Two similar-looking phrases mean opposite things for this pipeline:

    - a phrase of a DIFFERENT scope is a different question, and that is the
      whole point of asking several: fewer words ask a BROADER question (the
      visible type «чай листовой» for the packaging name «Чай зеленый Тимофей
      пакет 100 г»), which is exactly what keeps a photo pool from starving,
      while more words ask a narrower one. Containment either way is therefore
      never a repeat;
    - a phrase of the SAME scope made of the same words - the vision step
      reading one product twice, as a name and as packaging text - returns the
      rows the first phrasing already returned. Such an attempt is wasted and
      is dropped here.

    Similarity is the token-set rule the feed uses to call two listings one
    product type (``services.feed_curation.similarity``).
    """

    words = _question_words(phrase)
    if not words:
        return False
    for other in asked:
        other_words = _question_words(other)
        if not other_words:
            continue
        if words < other_words or other_words < words:
            continue
        if feed_curation.similarity(phrase, other) >= PHOTO_PHRASE_DUPLICATE_SHARE:
            return True
    return False


def photo_hypotheses(
    analysis: dict | str | None, limit: int | None = None
) -> list[str]:
    """Bounded, GROUNDED search hypotheses derived from one image (§8).

    With a structured analysis (``AIService.analyze_image``) the hypotheses
    follow the documented TIERS - the exact visible name, brand + product type,
    the whole visible product type, the CORE head words of that type, the
    pictured CATEGORY, the type plus a distinctive attribute, brand + model and
    the readable packaging text - each built ONLY from fields the vision step
    actually saw. The broad tiers are what keep a niche item from starving the
    pool: a query like «Чай зеленый Тимофей пакет 100 г» alone answers with a
    fuzzy handful, while the same picture asked as «чай листовой» or with its
    pictured category returns real listings of the very product shown.
    With a legacy plain-string answer (a test double or an older integration)
    the recognised phrase is used as-is, cleaned; it is never truncated into a
    vague fragment.

    A candidate that only REWORDS a question already in the list is dropped
    (:func:`_repeats_an_earlier_question`): the vision step routinely describes
    one product twice, and two near-identical phrases would spend two of the
    bounded attempts on the same answer while a genuinely different (and much
    more productive) representation is never asked.

    Returns at most ``limit`` (or :data:`MAX_PHOTO_ATTEMPTS`) unique non-empty
    phrases - the number of marketplace calls one photo may cost is fixed,
    never "one query per idea the model had".
    """

    cap = MAX_PHOTO_ATTEMPTS if limit is None else max(1, int(limit))

    if isinstance(analysis, dict):
        fields = {
            key: _clean_phrase(analysis.get(key))
            for key in _ANALYSIS_NAME_FIELDS
            + (
                "color",
                "material",
                "form_factor",
                "size_weight",
                "compatibility",
                "distinctive",
                "category",
            )
        }
        name = fields["product_name"]
        product_type = fields["product_type"]
        brand = fields["brand"]
        model = fields["model"]
        attribute = (
            fields["distinctive"]
            or fields["color"]
            or fields["material"]
            or fields["form_factor"]
            or fields["size_weight"]
            or fields["compatibility"]
            or fields["series"]
        )
        # TIERS, most precise first: exact visible name -> branded type -> the
        # whole visible type -> the CORE head words of that type (the broad
        # version of the SAME pictured product) -> the pictured CATEGORY (a
        # genuinely different, wider question about the same object) -> the
        # type plus a visible attribute -> the readable packaging text -> and
        # last brand + MODEL. A brand alone is not a second question (the brand
        # is already part of the branded-type tier), so that tier only counts
        # when a model/part number was actually read. The list is trimmed to
        # the budget, so this order decides which questions one photo may ask -
        # and the grounded broad ones have to be among them.
        candidates = [
            name,
            _join_unique(brand, product_type),
            product_type,
            _core_phrase(product_type or name),
            fields["category"],
            _join_unique(product_type, attribute),
            _first_clause(analysis.get("visible_text")),
            _join_unique(brand, model) if model else "",
        ]
    elif isinstance(analysis, str):
        cleaned = _clean_phrase(analysis)
        words = cleaned.split()
        candidates = [cleaned]
        if len(words) > 4:
            # Only when the recognised phrase is genuinely long, and only
            # whole content words - never a preposition-bounded fragment.
            candidates.append(_clean_phrase(" ".join(words[:4])))
    else:
        return []

    hypotheses: list[str] = []
    for candidate in candidates:
        cleaned = _clean_phrase(candidate)
        if not cleaned or cleaned in hypotheses:
            continue
        # A REWORDING of a question that was already asked is not a second
        # ground truth, it is the same query twice: dropping it keeps the
        # budget for a representation that can actually return new rows.
        if _repeats_an_earlier_question(cleaned, hypotheses):
            continue
        hypotheses.append(cleaned)
        if len(hypotheses) >= cap:
            break
    return hypotheses


def photo_concept_keywords(analysis: dict | None) -> list[str]:
    """Validation keywords for the image-derived product concept (§9).

    Candidates of every hypothesis are judged against THESE words - the
    product type, brand, model and readable text the picture really showed
    - so a perfume whose description merely contains «чай» can never answer
    a photo of a tea package just because one search word occurred in it.
    """

    if not isinstance(analysis, dict):
        return []
    keywords: list[str] = []
    for key in _ANALYSIS_NAME_FIELDS + ("manufacturer", "distinctive"):
        phrase = _clean_phrase(analysis.get(key))
        if phrase and phrase not in keywords:
            keywords.append(phrase)
    return keywords


async def _edit_or_reply(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, **kwargs
) -> None:
    """Edit the screen a callback came from, or reply to a message."""

    query = update.callback_query
    if query is not None:
        await query.edit_message_text(text, **kwargs)
        return

    message = update.effective_message
    if message is not None:
        await message.reply_text(text, **kwargs)


async def show_categories(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Render the compact category list."""

    await _edit_or_reply(
        update,
        context,
        CATEGORIES_TEXT,
        parse_mode="HTML",
        reply_markup=categories_keyboard(),
    )


async def category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Run a search inside the selected category."""

    query = update.callback_query
    if query is None:
        return

    category = category_by_callback(query.data or "")
    if category is None:
        await query.answer("Категория не найдена.", show_alert=True)
        return

    await query.answer(f"{category.title}: ищу товары…")

    user_id = await common.register_user(update, context)
    region = await common.get_user_region(context, user_id)
    # G24.8 P1.4: the stored delivery country must reach the CATEGORY
    # AliExpress request as ship_to_country, exactly as the «Находки» feed
    # forwards it (see show_feed): the documented ISO-2 code is trimmed and
    # upper-cased, anything else stays the documented "not specified" state.
    country = (region.get("country") or "").strip().upper()
    country = country if len(country) == 2 else None
    telegram_id = update.effective_user.id if update.effective_user else None
    chat_id = update.effective_chat.id if update.effective_chat else None
    if chat_id is None:
        return

    try:
        outcome = await common.get_search_service(context).find_by_category(
            category,
            country=country,
            currency=region.get("currency"),
            exclude=await common.seen_products(context, user_id),
        )
    except Exception:
        logger.exception("Category search failed for %r", category.code)
        await common.record_event(
            context,
            analytics.EVENT_SEARCH_FAILED,
            user_id=user_id,
            telegram_id=telegram_id,
            metadata={"source": "category", "reason": "error"},
        )
        await context.bot.send_message(
            chat_id,
            f"{WARNING} Не удалось выполнить поиск по категории.",
            reply_markup=back_to_menu_keyboard(),
        )
        return

    await record_outcome_events(
        context,
        outcome,
        user_id=user_id,
        telegram_id=telegram_id,
        source="category",
    )

    header = f"{CATEGORIES} <b>{common.escape(category.title)}</b>"
    await send_search_results(
        context,
        chat_id,
        outcome,
        header=header,
        wrap_up_keyboard=categories_keyboard(),
        back_callback=CB_CATEGORIES,
        user_id=user_id,
        telegram_id=telegram_id,
        # Category browsing is NOT an impulse-buy feed (G24.8 P0.2): its
        # pool is gated by the tolerant CATEGORY quality policy - the
        # structural contract only - because the relevance filter above
        # has already judged topical fit. The recommendation profile
        # ($1..$20 / >= 300 orders / >= 4.5) stays reserved for the feeds
        # it was written for.
        curation_mode=MODE_CATEGORY,
    )


def _merge_feed_outcome(target: SearchOutcome, page_outcome: SearchOutcome) -> None:
    """Accumulate ONE more feed page into the outcome one open renders.

    Counters are summed and the marketplace lists are unioned, so analytics
    and the empty-screen diagnosis describe the WHOLE open, not just its
    last page. The order of the cards stays the pages' own order - nothing
    is ranked, re-scored or re-filtered here (G24).
    """
    target.products.extend(page_outcome.products)
    target.products_before_filtering += page_outcome.products_before_filtering
    target.products_dropped_as_irrelevant += page_outcome.products_dropped_as_irrelevant
    target.products_without_affiliate_url += page_outcome.products_without_affiliate_url
    target.products_hidden_as_seen += page_outcome.products_hidden_as_seen
    # B4 diagnostics: keep the stage/script counters of the WHOLE open.
    target.products_after_constraints += page_outcome.products_after_constraints
    target.titles_same_script += page_outcome.titles_same_script
    target.titles_other_script += page_outcome.titles_other_script
    target.titles_unknown_script += page_outcome.titles_unknown_script
    for name in (
        "searched_marketplaces",
        "failed_marketplaces",
        "unavailable_marketplaces",
    ):
        bucket = getattr(target, name)
        for item in getattr(page_outcome, name):
            if item not in bucket:
                bucket.append(item)
    target.comparisons.extend(page_outcome.comparisons)


async def show_feed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """«✨ Находки»: AliExpress' own feed, straight to the FIRST product card.

    No summary screen precedes the card (spec): the user lands on a real
    product immediately, and «◀️ Назад» from the first card restores the
    session summary on demand.

    G24: the composition and the order of this feed belong to AliExpress.
    ``SearchService.find_feed_page`` delivers ONE documented page of the
    keyword-less ``product.query`` (normalize -> dedup -> technical
    validity -> skip what this user has already been shown); this handler
    only:

    * reads the per-user state - the feed's shown-history
      (``popular_shown``, scope ``feed``: the ONLY hard no-repeat source
      for this feed) and the cursor (the next ``page_no``, RAM +
      ``popular_state``);
    * refills up to ``FEED_OPEN_SIZE`` new cards within
      ``FEED_MAX_PAGES_PER_OPEN`` documented pages, so one open is bounded
      REAL API work no matter what the marketplace answers;
    * advances the cursor by the contract ``next = last_attempted + 1``,
      so consecutive opens walk FURTHER into the listing across sessions,
      handler instances and restarts;
    * renders the outcome and records what this user is shown RIGHT NOW.

    The search-like flows keep their own scope, so a search can neither
    empty the feed nor make it claim it is over. Navigation INSIDE an
    already-open session (Back/Next) keeps the loaded list untouched;
    only a new open advances. When every page of the budget holds nothing
    new, the user gets an explicit end-of-session screen with «начать
    заново» - never the same product pretending to be new.
    """

    query = update.callback_query
    if query is not None:
        await query.answer("Собираю находки…")

    user_id = await common.register_user(update, context)
    region = await common.get_user_region(context, user_id)
    telegram_id = update.effective_user.id if update.effective_user else None
    chat_id = update.effective_chat.id if update.effective_chat else None
    if chat_id is None:
        return

    # ONE question, ONE scope: what the FEED itself has already shown this
    # user. The search-like flows keep their own scope, so a search neither
    # empties the feed nor makes it claim it is over - and the feed never
    # hides a search result.
    feed_history = await common.feed_seen(context, user_id)

    # The cursor of the feed: the next documented page_no. The memory of
    # the session is used first, the persisted cursor afterwards, so even a
    # restarted bot continues where the feed stopped instead of asking the
    # same first pages again.
    start_page = context.user_data.get(FEED_PAGE_KEY)
    if not start_page:
        start_page = await _feed_start_page(context, user_id)
    start_page = max(1, int(start_page))

    settings_obj = common.get_settings(context)
    open_size = _bounded_setting(settings_obj, "feed_open_size", 20, 100)
    max_pages = _bounded_setting(settings_obj, "feed_max_pages_per_open", 2, 10)

    country = (region.get("country") or "").strip().upper()
    country = country if len(country) == 2 else None

    collected: list[Product] = []
    merged: SearchOutcome | None = None
    saw_candidates = False
    page = start_page
    pages_asked = 0
    last_attempted = start_page

    while pages_asked < max_pages and len(collected) < open_size:
        last_attempted = page
        # The cards already collected THIS open count as "seen" too, so a
        # page boundary can never hand the same card to the user twice.
        exclude = set(feed_history)
        exclude.update(identity_pair(product) for product in collected)
        try:
            page_outcome = await common.get_search_service(context).find_feed_page(
                page=page,
                country=country,
                currency=region.get("currency"),
                exclude=exclude,
                limit=open_size - len(collected),
            )
        except Exception:
            logger.exception("Feed failed (page %s)", page)
            # The attempted page is consumed even on a hard failure - the
            # next open must not replay it (§12: next = last_attempted + 1).
            context.user_data[FEED_PAGE_KEY] = last_attempted + 1
            await _remember_feed_page(context, user_id, last_attempted + 1)
            await common.record_event(
                context,
                analytics.EVENT_SEARCH_FAILED,
                user_id=user_id,
                telegram_id=telegram_id,
                metadata={"source": "feed", "reason": "error"},
            )
            await context.bot.send_message(
                chat_id,
                f"{WARNING} Не удалось получить подборку.",
                reply_markup=back_to_menu_keyboard(),
            )
            return

        pages_asked += 1
        saw_candidates = saw_candidates or bool(
            page_outcome.products_before_filtering
        )
        if merged is None:
            merged = page_outcome
        else:
            _merge_feed_outcome(merged, page_outcome)
        collected.extend(page_outcome.products)

        if not page_outcome.products_before_filtering:
            # The API answered this page with NOTHING at all - the honest
            # end of the listing. Deeper pages cannot hold anything either.
            break
        page += 1

    outcome = (
        merged
        if merged is not None
        else SearchOutcome(
            request=SearchRequest(
                original_query="находки AliExpress",
                mode=SearchMode.FEED,
                country=country,
                currency=region.get("currency"),
                page_no=start_page,
            )
        )
    )

    # The cursor contract (§12): next = last_attempted_page + 1, where
    # "attempted" means the page the service really asked - success,
    # emptiness or failure alike. Two consecutive presses therefore cannot
    # replay the same pages, not even after «🔄 Начать заново».
    context.user_data[FEED_PAGE_KEY] = last_attempted + 1
    await _remember_feed_page(context, user_id, last_attempted + 1)

    await record_outcome_events(
        context,
        outcome,
        user_id=user_id,
        telegram_id=telegram_id,
        source="feed",
    )

    if not collected:
        # Did the MARKETPLACE actually answer? This separates a real
        # end-of-feed from a feed the API answered with nothing:
        # «показали всё» (with a history reset button) must never appear
        # just because the API returned an empty page.
        if saw_candidates and feed_history:
            # Nothing NEW to show. An honest screen - the same product is
            # never re-sent while pretending it is fresh.
            await _edit_or_reply(
                update,
                context,
                FEED_EXHAUSTED_TEXT,
                parse_mode="HTML",
                reply_markup=feed_exhausted_keyboard(),
            )
            return
        # Which sources could not answer is an internal fact: it is logged
        # for diagnosis and never appended to the screen (§3: no technical
        # explanations in front of the user).
        if outcome.unavailable_marketplaces or outcome.failed_marketplaces:
            logger.info(
                "Feed empty (unavailable=%s, failed=%s)",
                outcome.unavailable_marketplaces or "none",
                outcome.failed_marketplaces or "none",
            )
        await _edit_or_reply(
            update,
            context,
            FEED_EMPTY_TEXT,
            parse_mode="HTML",
            reply_markup=back_to_menu_keyboard(),
        )
        return

    # Persist exactly what this user is shown RIGHT NOW - before the card
    # reaches Telegram, so the next open (in this process or a restarted
    # one) skips it by its canonical identity. This is the FEED's own
    # scope: a search must not be emptied by what the feed displayed.
    await common.remember_shown(context, user_id, collected[0], flow=FLOW_FEED)

    header = f"{FINDINGS} <b>Находки</b>"
    await send_search_results(
        context,
        chat_id,
        outcome,
        header=header,
        wrap_up_keyboard=back_to_menu_keyboard(),
        user_id=user_id,
        telegram_id=telegram_id,
        track_shown=True,
        # G24: the list arrives exactly as AliExpress composed it - the
        # shared curation gate must not rank, score or quality-filter it a
        # second time; only the history recording of the normal path stays.
        pre_curated=True,
    )


async def _feed_start_page(
    context: ContextTypes.DEFAULT_TYPE, user_id: int | None
) -> int:
    """Which feed page this user's next open starts from (best effort).

    The cursor is persisted per user (``popular_state``), so the feed keeps
    moving forward after a restart as well. A missing user or a failed read
    degrades to the documented first page - the feed then simply walks
    forward on the per-user history, never to a wrong pool.
    """

    if user_id is None:
        return 1
    try:
        return await common.get_database(context).get_feed_page(user_id)
    except Exception:
        logger.warning(
            "Failed to read the feed cursor for %s", user_id, exc_info=True
        )
        return 1


async def _remember_feed_page(
    context: ContextTypes.DEFAULT_TYPE, user_id: int | None, page: int
) -> None:
    """Persist the feed cursor; a failure only costs the RAM memory."""

    if user_id is None:
        return
    try:
        await common.get_database(context).set_feed_page(user_id, page)
    except Exception:
        logger.warning(
            "Failed to persist the feed cursor for %s", user_id, exc_info=True
        )


async def feed_reset_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """``feed:reset`` (and the legacy ``pop:reset``): «начать заново».

    Resets the «✨ Находки» SESSION and nothing else:

    * the feed's own shown-product scope is cleared (never the novelty
      state of the ordinary flows - a restart of the feed must not make a
      search repeat itself);
    * the feed cursor is deliberately LEFT where it is, so the feed
      continues from NEWER pages of the marketplace listing instead of
      replaying the first ones (the reported «начать заново показывает те
      же товары» can no longer happen: history is empty, cursor moved on).

    Favorites, price tracking, currency and the conversation of a search are
    untouched: this is a feed session, not user data.
    """

    query = update.callback_query
    if query is None:
        return

    user_id = await common.register_user(update, context)
    if user_id is not None:
        try:
            await common.get_database(context).clear_feed_shown(user_id)
        except Exception:
            logger.warning(
                "Failed to reset the feed history for %s",
                user_id,
                exc_info=True,
            )

    # show_feed answers the callback query itself.
    await show_feed(update, context)


async def photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A photo was sent: find products that REALLY are the pictured item."""
    from handlers.photo_search import photo_message_handler

    await photo_message_handler(update, context)


async def _legacy_photo_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    image_bytes: bytes | None = None,
) -> None:
    """Legacy structured photo search pipeline kept for backwards compatibility."""
    message = update.effective_message
    if message is None or not message.photo:
        return

    photo = message.photo[-1]
    if image_bytes is None:
        await message.reply_text(PHOTO_RECEIVED_TEXT)

        if photo.file_size and photo.file_size > MAX_IMAGE_BYTES:
            await message.reply_text(
                f"{WARNING} Фотография слишком большая для поиска "
                f"(лимит {MAX_IMAGE_BYTES // (1024 * 1024)} МБ).",
                reply_markup=photo_retry_keyboard(),
            )
            return

        try:
            telegram_file = await photo.get_file()
            image_bytes = bytes(await telegram_file.download_as_bytearray())
        except Exception:
            logger.exception("Failed to download a photo for image search")
            await message.reply_text(
                f"{WARNING} Не удалось загрузить фотографию. Отправьте её ещё раз.",
                reply_markup=photo_retry_keyboard(),
            )
            return

    user_id = await common.register_user(update, context)
    region = await common.get_user_region(context, user_id)

    await common.record_event(
        context,
        analytics.EVENT_IMAGE_SEARCH_REQUESTED,
        user_id=user_id,
        telegram_id=update.effective_user.id if update.effective_user else None,
        metadata={"bytes": len(image_bytes)},
    )

    ai_service = common.get_ai_service(context)
    if ai_service is None or not ai_service.is_available:
        # Technical reason stays in the log; the chat gets the same short
        # natural message an unsuccessful recognition gets.
        logger.info("Photo search skipped: no AI service is configured")
        await message.reply_text(PHOTO_NO_AI_TEXT, reply_markup=photo_retry_keyboard())
        return

    # IMAGE UNDERSTANDING (§7): the structured analysis reads what the
    # picture really shows (type, brand, model, readable packaging text).
    # Only integrations without it fall back to the legacy single-phrase
    # recognition - never both (one vision step per photo).
    analysis: dict | None = None
    recognition: dict | str | None
    analyzer = getattr(ai_service, "analyze_image", None)
    if callable(analyzer):
        try:
            result = await analyzer(image_bytes)
        except Exception:
            logger.exception("Structured image analysis failed")
            result = None
        analysis = result if isinstance(result, dict) else None
        recognition = analysis
    else:
        recognition = await ai_service.describe_image_query(image_bytes)

    # RETRIEVAL BUDGETS come from the application settings, clamped to the
    # documented hard ceilings: both numbers are limits on REAL marketplace
    # calls, so a missing setting (a test double) or a typo in .env must fall
    # back to the documented default instead of crawling the catalogue.
    settings_obj = common.get_settings(context)
    attempt_budget = _bounded_setting(
        settings_obj,
        "photo_search_hypotheses",
        MAX_PHOTO_ATTEMPTS,
        PHOTO_ATTEMPTS_LIMIT,
    )
    page_budget = _bounded_setting(
        settings_obj,
        "photo_search_pages",
        PHOTO_MAX_PAGES,
        PHOTO_PAGES_LIMIT,
    )

    # MULTIPLE SEARCH HYPOTHESES (§8): bounded, grounded in the analysis,
    # never a preposition-bounded fragment («чай в») and never two phrases
    # that merely reword each other.
    hypotheses = photo_hypotheses(recognition, limit=attempt_budget)
    if not hypotheses:
        # Technical reason stays in the log; the chat gets the short human
        # message of §12 - no model names, no API explanations.
        logger.info("Photo search produced no usable product hypothesis")
        await message.reply_text(
            IMAGE_EMPTY_TEXT, parse_mode="HTML", reply_markup=photo_retry_keyboard()
        )
        return

    chat_id = update.effective_chat.id if update.effective_chat else None
    if chat_id is None:
        return
    telegram_id = update.effective_user.id if update.effective_user else None

    seen = await common.seen_products(context, user_id)
    search_service = common.get_search_service(context)

    # The CONCEPT request carries what the image showed as validation
    # keywords (§9): candidates are judged against the pictured product,
    # not against whichever short hypothesis retrieved them.
    base = SearchRequest(
        original_query=hypotheses[0],
        normalized_query=hypotheses[0],
        mode=SearchMode.SEARCH,
        keywords=photo_concept_keywords(analysis),
    )
    if region.get("currency") and not base.currency:
        base = base.model_copy(update={"currency": region["currency"].upper()})

    collected: list[Product] = []
    raw_total = 0
    dropped_total = 0
    hidden_total = 0
    searched: list[str] = []
    failed: list[str] = []
    unavailable: list[str] = []
    merged: list[Product] = []
    #: Phrases whose FIRST page answered with something. Only those are worth
    #: a deeper page (see the loop below).
    productive: set[str] = set()

    for page in range(1, page_budget + 1):
        for hypothesis in hypotheses:
            # A phrase that returned NOTHING on its first page cannot have
            # anything on a deeper one: the marketplace has no rows for those
            # words at all. Skipping its pages keeps the calls for the phrases
            # that DO answer.
            if page > 1 and hypothesis not in productive:
                continue

            # The SAME grounded phrase is asked for its next documented page:
            # deeper pages of the words the picture really showed add real
            # listings of the pictured product - and never a word the vision
            # step did not see. Only ``page_no`` changes; the concept keywords
            # and every constraint of the base request travel along.
            request = base.model_copy(
                update={
                    "original_query": hypothesis,
                    "normalized_query": hypothesis,
                    "page_no": page,
                }
            )
            try:
                # The photo flow builds its breadth from its OWN grounded
                # hypotheses (one bounded search per hypothesis and page), so
                # the service must not add text retrieval variants on top: the
                # candidates still pass through the same relevance gate, but
                # the number of marketplace calls stays bounded.
                candidate = await search_service.find_products(
                    request, exclude=seen, broaden=False
                )
            except Exception:
                logger.exception("Photo-based search failed for query %r", hypothesis)
                continue
            if candidate.products_before_filtering:
                # The phrase answers at all: its deeper pages are worth asking.
                productive.add(hypothesis)
            collected.extend(candidate.products)
            raw_total += candidate.products_before_filtering
            dropped_total += candidate.products_dropped_as_irrelevant
            hidden_total += candidate.products_hidden_as_seen
            searched = list(dict.fromkeys([*searched, *candidate.searched_marketplaces]))
            failed = list(dict.fromkeys([*failed, *candidate.failed_marketplaces]))
            unavailable = list(
                dict.fromkeys([*unavailable, *candidate.unavailable_marketplaces])
            )
            # DEDUPLICATION across hypotheses (§6): the same listing that two
            # queries both returned is ONE product, counted once.
            merged = deduplicate_products(collected)
            if len(merged) >= PHOTO_ENOUGH_RESULTS:
                break
        if len(merged) >= PHOTO_ENOUGH_RESULTS:
            break

    if page_budget > 1 and any(hypothesis in productive for hypothesis in hypotheses):
        logger.info(
            "Photo search asked up to %d page(s) of %d grounded hypothesis(es); "
            "pool %d",
            page_budget,
            len(hypotheses),
            len(merged),
        )

    if not merged:
        # Reasons belong to the log; the user gets §12 and nothing else.
        if failed or unavailable or dropped_total:
            logger.info(
                "Photo search empty (failed=%s, unavailable=%s, "
                "dropped_as_irrelevant=%d, hidden_as_seen=%d)",
                failed or "none",
                unavailable or "none",
                dropped_total,
                hidden_total,
            )
        await message.reply_text(
            IMAGE_EMPTY_TEXT, parse_mode="HTML", reply_markup=photo_retry_keyboard()
        )
        return

    # RANKING (§6): the surviving products are ordered by the same
    # relevance-first pipeline as any search - strong product matches
    # above weak keyword hits - and then spread so the near-identical
    # listings of one product family (the same item from several stores)
    # cannot occupy the whole first page of cards.
    outcome = SearchOutcome(
        request=base,
        products=feed_curation.spread_similar(sort_products(list(merged), base)),
        searched_marketplaces=searched,
        failed_marketplaces=failed,
        unavailable_marketplaces=unavailable,
        products_before_filtering=raw_total,
        products_dropped_as_irrelevant=dropped_total,
        products_hidden_as_seen=hidden_total,
    )

    # AI SELECTION (§3): the merged pool is handed to the model ONCE - the photo
    # flow owns its breadth (several hypothesised searches), so the very same
    # junk filter and the same 20-card ceiling every other feed gets are applied
    # here, on the finished list, instead of once per hypothesis. Without an
    # answer the deterministic pool survives, exactly as before. A search service
    # without the selection API (a replacement or a test double) simply has no
    # selection to run.
    selector = getattr(search_service, "apply_feed_selection", None)
    if callable(selector):
        await selector(outcome)

    await record_outcome_events(
        context,
        outcome,
        user_id=user_id,
        telegram_id=telegram_id,
        source="photo",
    )

    # The normal product card (§11): no «Запрос: …», no generated query,
    # no counters - the same feed every other flow opens.
    await send_search_results(
        context,
        chat_id,
        outcome,
        wrap_up_keyboard=back_to_menu_keyboard(),
        user_id=user_id,
        telegram_id=telegram_id,
    )

