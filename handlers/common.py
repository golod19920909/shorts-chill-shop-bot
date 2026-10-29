"""Shared helpers used by multiple Telegram handlers.

Kept in a dedicated module (rather than duplicated in each handler) so
that dependency lookup, per-user state and HTML safety are handled in
exactly one place.
"""

from __future__ import annotations

import html
import logging
import secrets
from typing import Any

from telegram import InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from database.database import Database
from curation.integration import CurationGate
from database.models import FLOW_FEED, FLOW_SEARCH
from models.product import Product
from models.search_request import SearchRequest
from services.ai_service import AIService
from services.product_identity import identity_pair
from services.search_service import SearchOutcome, SearchService
from utils.formatting import format_product_card, truncate_for_caption

logger = logging.getLogger(__name__)

#: key in ``context.user_data`` holding cached search outcomes
OUTCOME_CACHE_KEY = "outcome_cache"

#: key in ``context.user_data`` holding per-session screen details
#: (``header``, ``wrap_up_keyboard``) used when paging through results
OUTCOME_META_KEY = "outcome_meta"

#: how many recent search sessions to keep per user in memory
MAX_CACHED_SESSIONS = 5


def get_database(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.application.bot_data["database"]


def get_search_service(context: ContextTypes.DEFAULT_TYPE) -> SearchService:
    return context.application.bot_data["search_service"]


def get_ai_service(context: ContextTypes.DEFAULT_TYPE) -> AIService | None:
    return context.application.bot_data.get("ai_service")


def get_settings(context: ContextTypes.DEFAULT_TYPE):
    """The ``Settings`` object the application was built with."""

    return context.application.bot_data.get("settings")


def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Whether the user who sent this update may use the admin panel.

    With no ``ADMIN_IDS`` configured nobody is an administrator: the panel
    then says so rather than exposing statistics to everyone.
    """

    settings = get_settings(context)
    if settings is None:
        return False

    user = update.effective_user
    return settings.is_admin(user.id if user is not None else None)


async def record_event(
    context: ContextTypes.DEFAULT_TYPE,
    event_type: str,
    *,
    user_id: int | None = None,
    telegram_id: int | None = None,
    product: Product | None = None,
    promotion_name: str | None = None,
    is_affiliate: bool | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Write one analytics event; a failure is logged, never raised.

    Analytics sits on the user's hot path, so a locked or missing
    ``analytics_events`` table must not interrupt a search, a card or a
    button press. ``product`` fills the marketplace/product fields
    (and its title, which is marketplace data) in one step.
    """

    try:
        database = get_database(context)
        await database.record_event(
            event_type,
            user_id=user_id,
            telegram_id=telegram_id,
            marketplace=(
                str(getattr(product.marketplace, "value", product.marketplace))
                if product is not None
                else None
            ),
            product_id=product.product_id if product is not None else None,
            title=product.title if product is not None else None,
            promotion_name=promotion_name,
            is_affiliate=is_affiliate,
            metadata=metadata,
        )
    except Exception:
        logger.warning("Failed to record analytics event %s", event_type, exc_info=True)


async def register_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    """Ensure the Telegram user exists in the database, return its id."""

    user = update.effective_user
    if user is None:
        return None

    database = get_database(context)
    try:
        return await database.get_or_create_user(
            telegram_id=user.id, username=user.username, first_name=user.first_name
        )
    except Exception:
        logger.exception("Failed to register user %s", user.id)
        return None


def remember_outcome(
    context: ContextTypes.DEFAULT_TYPE,
    outcome: SearchOutcome,
    *,
    header: str | None = None,
    wrap_up_keyboard: InlineKeyboardMarkup | None = None,
    back_callback: str | None = None,
    track_shown: bool = False,
) -> str:
    """Cache a search outcome in memory and return a short session id.

    Telegram callback data is limited to 64 bytes, so buttons reference
    ``(session_id, index)`` instead of real product ids/URLs.

    ``header`` (a screen-specific headline replacing the generic summary)
    and ``wrap_up_keyboard`` (shown once the last page is reached) belong
    to the *screen*, not to the search, but pagination needs them again on
    every following page - so they are cached next to the outcome.

    ``track_shown`` marks a FEED session («✨ Находки»): every product card
    the user actually browses to is persisted to the per-user history
    (``popular_shown``, scope ``feed``), so the next open advances past it.
    Sessions without the flag (ordinary searches) never write history.
    """

    session_id = secrets.token_hex(3)
    cache: dict[str, SearchOutcome] = context.user_data.setdefault(
        OUTCOME_CACHE_KEY, {}
    )
    meta: dict[str, dict[str, Any]] = context.user_data.setdefault(
        OUTCOME_META_KEY, {}
    )

    cache[session_id] = outcome
    meta[session_id] = {
        "header": header,
        "wrap_up_keyboard": wrap_up_keyboard,
        "back_callback": back_callback,
        "track_shown": track_shown,
    }

    while len(cache) > MAX_CACHED_SESSIONS:
        oldest = next(iter(cache))
        cache.pop(oldest)
        meta.pop(oldest, None)

    return session_id


def get_outcome_meta(
    context: ContextTypes.DEFAULT_TYPE, session_id: str
) -> dict[str, Any]:
    """Return the cached screen details of a session (never ``None``)."""

    meta: dict[str, dict[str, Any]] = context.user_data.get(OUTCOME_META_KEY, {})
    return meta.get(session_id) or {"header": None, "wrap_up_keyboard": None}


def get_cached_outcome(
    context: ContextTypes.DEFAULT_TYPE, session_id: str
) -> SearchOutcome | None:
    cache: dict[str, SearchOutcome] = context.user_data.get(OUTCOME_CACHE_KEY, {})
    return cache.get(session_id)


def get_cached_products(
    context: ContextTypes.DEFAULT_TYPE, session_id: str
) -> list[Product]:
    outcome = get_cached_outcome(context, session_id)
    return outcome.products if outcome is not None else []


def get_cached_product(
    context: ContextTypes.DEFAULT_TYPE, session_id: str, index: int
) -> Product | None:
    products = get_cached_products(context, session_id)
    if 0 <= index < len(products):
        return products[index]
    return None


def escape(value: Any) -> str:
    """Escape dynamic text for Telegram HTML parse mode."""

    return html.escape(str(value), quote=False)


def product_to_dict(product: Product) -> dict[str, Any]:
    """JSON-serializable snapshot used for favorites/price tracking."""

    return product.model_dump(mode="json")


def product_from_snapshot(payload: dict[str, Any]) -> Product:
    """Rebuild a product from a snapshot written earlier by ``product_to_dict``.

    Snapshots are stored verbatim in the database, so a row saved by an
    earlier version may still carry keys that no longer exist in the
    model (for example a field that only ever came from a marketplace the
    bot no longer talks to). Dropping just those unknown keys keeps
    existing AliExpress favorites readable instead of silently losing
    them.

    This deliberately does not relax the model: a ``marketplace`` value
    that is no longer valid and a missing required field still fail
    validation, so a record of a removed marketplace can never reach the
    screen.
    """

    known = {
        key: value for key, value in payload.items() if key in Product.model_fields
    }
    return Product.model_validate(known)


async def get_user_region(
    context: ContextTypes.DEFAULT_TYPE, user_id: int | None
) -> dict[str, str | None]:
    """Read the user's optional country/currency.

    Returns ``{"country": None, "currency": None}`` when the user has not
    chosen anything (a fully supported state) or when the read fails -
    the bot must keep working with no region configured.
    """

    if user_id is None:
        return {"country": None, "currency": None}

    try:
        return await get_database(context).get_user_region(user_id)
    except Exception:
        logger.exception("Failed to read region for user %s", user_id)
        return {"country": None, "currency": None}


def product_card_text(
    product: Product,
    *,
    index: int | None = None,
    caption_prefix: str | None = None,
) -> str:
    """The exact text a product card renders (shared by send and edit).

    One renderer for both paths guarantees that an edited card shows the
    same layout as a freshly sent one - and that the product the user is
    LOOKING at is always the product the action buttons resolve.

    ``caption_prefix`` is the browsing headline built by
    ``handlers.search._card_caption``: it is ALREADY safe HTML (the feed
    name plus position), so it is inserted as markup - escaping it here
    would turn the intended ``<b>`` tags into visible ``&lt;b&gt;`` text.
    The product body itself is always escaped exactly once.
    """

    body = escape(format_product_card(product, index=index))
    return f"{caption_prefix}\n\n{body}" if caption_prefix else body


async def seen_products(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int | None,
    *,
    flow: str = FLOW_SEARCH,
) -> set[tuple[str, str]]:
    """Products this user has already been shown IN THIS SCOPE.

    The history lives in SQLite (``popular_shown``, one row per user, scope
    and product), so it survives a restart and is strictly per user. ``flow``
    selects the SCOPE of the state the caller may act on: the search-like flows
    read only what earlier searches showed them, and the «✨ Находки» feed reads
    its own (:func:`feed_seen`). One flow's transient state therefore never
    decides another flow's screen - the leak that made an ordinary search show
    the feed's end-of-session screen and the feed claim there was nothing new.

    A read failure returns an empty set: a missing history must degrade to
    "show the feed", never to an empty screen.
    """

    if user_id is None:
        return set()
    try:
        rows = await get_database(context).get_shown_products(user_id, flow=flow)
    except Exception:
        logger.exception("Failed to read the shown-product history of %s", user_id)
        return set()
    return {(str(marketplace), str(product_id)) for marketplace, product_id in rows}


async def feed_seen(
    context: ContextTypes.DEFAULT_TYPE, user_id: int | None
) -> set[tuple[str, str]]:
    """What the «✨ Находки» FEED itself has already shown this user.

    This - and only this - is the set the feed skips, the state its cursor
    advances on and the state an end-of-session screen may be based on. A
    normal search therefore cannot empty the feed or make it claim it is over
    (and the feed cannot hide a search result), which is what the production
    report «Популярное говорит, что всё просмотрено, сразу после старта» was.
    """

    return await seen_products(context, user_id, flow=FLOW_FEED)


def session_flow(context: ContextTypes.DEFAULT_TYPE, session_id: str) -> str:
    """Which scope a cached browsing session writes its cards to.

    A feed session (``track_shown``) writes the feed scope, every other
    session the search scope - derived from the session's own meta, so a card
    can never be recorded under a flow it does not belong to.
    """

    meta = get_outcome_meta(context, session_id)
    return FLOW_FEED if meta.get("track_shown") else FLOW_SEARCH


async def remember_shown(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int | None,
    product: Product | None,
    *,
    flow: str = FLOW_SEARCH,
) -> None:
    """Record that this user is now looking at ``product`` (in ``flow``).

    Written on every card a user is actually shown (opening a feed and each
    ➡️ Следующий step), keyed by the canonical identity from
    ``services.product_identity`` - the same key the exclusion uses, so one
    product can never be recorded twice under two spellings of one URL. A
    failure here is logged only: browsing must not break because the history
    could not be written.
    """

    if user_id is None or product is None:
        return
    marketplace, product_id = identity_pair(product)
    if not marketplace or not product_id:
        return
    try:
        await get_database(context).add_shown_product(
            user_id=user_id,
            marketplace=marketplace,
            product_id=product_id,
            flow=flow,
        )
    except Exception:
        logger.exception("Failed to record a shown product for user %s", user_id)


def get_curation_gate(context: ContextTypes.DEFAULT_TYPE) -> CurationGate:
    """The curation gate bound to THIS context's database.

    Built per call on purpose: it is two attribute assignments around the
    stateless pipeline (``curation.integration``), while the database comes from
    the same DI helper every other read uses - so a test that swaps the database
    swaps the gate with it.
    """

    return CurationGate(get_database(context))


async def curate_products(
    context: ContextTypes.DEFAULT_TYPE,
    products: list[Product],
    *,
    user_id: int | None,
    mode: str,
    request: SearchRequest | None = None,
) -> list[Product]:
    """Run the curation layer over one feed's final candidates.

    Called once per displayed result set, right before the cards are cached and
    materialized (see ``handlers.search.send_search_results``): the list it
    returns is what «➡️ Следующий» walks and what every later open compares its
    history against. ``request`` travels to the ranker: it is read for
    ``MODE_CATEGORY`` (G24.8 P1.1) and - since G25.2-C Stage 7 - for the
    ``MODE_SEARCH`` tier map that keeps the displayed list semantically
    ordered; every other mode ignores it.
    """

    return await get_curation_gate(context).curate(
        products, user_id=user_id, mode=mode, request=request
    )


async def remember_views(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int | None,
    products: list[Product],
) -> None:
    """Record the cards this user has just been SHOWN (the novelty skip list).

    Written at display time - the opened card and every card browsed to - so the
    history can only ever say "this user has really seen this product".
    """

    await get_curation_gate(context).remember(user_id, products)


async def is_product_tracked(
    context: ContextTypes.DEFAULT_TYPE, user_id: int | None, product_id: str
) -> bool:
    """Whether THIS user already tracks THIS product (card 🔔/🔕 state)."""

    if user_id is None or not product_id:
        return False
    try:
        rows = await get_database(context).get_tracked_products(user_id)
    except Exception:
        logger.exception("Failed to read tracked products for user %s", user_id)
        return False
    return any(row.get("product_id") == product_id for row in rows)


async def send_product_card(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    product: Product,
    index: int | None,
    reply_markup=None,
    caption_prefix: str | None = None,
    raw_caption: str | None = None,
) -> None:
    """Send one product card, as a photo when the marketplace gave an image.

    No image is generated or guessed: without a real ``image_url`` the
    card is sent as plain text. ``caption_prefix`` (the browsing session
    headline, e.g. ``🔥 Популярное · 1/20``) is prepended when given.
    ``raw_caption`` (used by the card-replacement path) carries a fully
    rendered text and takes precedence over both.
    """

    if raw_caption is not None:
        text = raw_caption
    else:
        text = product_card_text(product, index=index, caption_prefix=caption_prefix)

    if product.image_url:
        try:
            await context.bot.send_photo(
                chat_id,
                photo=product.image_url,
                caption=truncate_for_caption(text),
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
            return
        except Exception:
            # A broken/hotlink-protected image must not hide the product.
            logger.warning(
                "Could not send photo for %s/%s; falling back to text",
                product.marketplace,
                product.product_id,
            )

    await context.bot.send_message(
        chat_id, text, parse_mode="HTML", reply_markup=reply_markup
    )
