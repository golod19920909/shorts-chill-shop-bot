"""Bot assembly: application factory and dependency wiring.

``build_application`` is the single place that turns settings into a
running ``telegram.ext.Application``. Keeping it here (instead of in
``main.py``) lets tests import and inspect the wiring without starting
the bot, and keeps ``main.py`` limited to process lifecycle.
"""

from __future__ import annotations

import logging

from telegram import BotCommand, Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from config.settings import Settings, settings as default_settings
from database.database import Database
from services import price_tracker as price_tracking
from services import analytics
from handlers import (
    admin,
    ai_assistant,
    discovery,
    favorites,
    hot_discounts,
    newchat,
    promo_codes,
    region,
    sales,
    search,
    start,
)
from handlers import settings as settings_handlers
from keyboards.admin import CB_ADMIN_PREFIX
from keyboards.hot_discounts import CB_HOT_SECTION_PREFIX
from keyboards.sales import CB_SALE_PREFIX
from keyboards.main import (
    CB_CATEGORY_PREFIX,
    CB_CURRENCY_PREFIX,
    CB_NAV_BACK,
    CB_NEXT_PREFIX,
    CB_FEED_RESET,
    CB_NOTIFY_TOGGLE,
    CB_ONBOARDING_PREFIX,
    CB_POPULAR_RESET,
    CB_PROMO_CAMPAIGN_LEGACY_PREFIX,
    MAIN_MENU_CALLBACKS,
    REMOVED_CALLBACK_PATTERN,
)
from keyboards.products import (
    CB_PREFIX_AI,
    CB_PREFIX_AI_ACTION,
    CB_PREFIX_CARD_UNTRACK,
    CB_PREFIX_FAVORITE,
    CB_PREFIX_FAVORITE_REMOVE,
    CB_PREFIX_NAV,
    CB_PREFIX_TRACK,
    CB_PREFIX_TRACK_STOP,
)
from services.ai_service import AIService
from services.marketplaces import AliExpressProvider
from services.marketplaces.base import MarketplaceProvider
from services.product_assistant import ProductAssistant
from services.search_service import SearchService

logger = logging.getLogger(__name__)


#: Commands published to Telegram's command menu (the "Menu" button next to
#: the message field), so ``/start`` and the other commands are reachable
#: without typing them by hand.
#:
#: This list is the single source of truth for the menu: it contains exactly
#: the commands that have a ``CommandHandler`` in ``_register_handlers``, so
#: Telegram can never advertise something the bot does not answer. No second
#: menu mechanism (custom keyboard, extra ``/help`` tree) is registered.
BOT_COMMANDS: tuple[BotCommand, ...] = (
    BotCommand("start", "Start bot"),
    BotCommand("help", "How to use the bot"),
    BotCommand("new", "New search / reset context"),
    BotCommand("favorites", "Saved products"),
    BotCommand("tracking", "Price tracking"),
    BotCommand("stats", "Status"),
    BotCommand("admin", "Admin statistics"),
)


async def publish_command_menu(bot) -> bool:
    """Publish :data:`BOT_COMMANDS` to Telegram's command menu.

    Returns ``True`` when Telegram accepted the list. A failure (no network,
    revoked token) is logged and reported instead of raised: a missing menu
    entry must never stop the bot from starting.
    """

    try:
        await bot.set_my_commands(list(BOT_COMMANDS))
    except Exception:
        logger.warning("Could not publish the Telegram command menu", exc_info=True)
        return False

    logger.info(
        "Telegram command menu published: %s",
        ", ".join(f"/{command.command}" for command in BOT_COMMANDS),
    )
    return True


def build_providers(settings: Settings) -> list[MarketplaceProvider]:
    """Create the marketplace provider with its own credentials.

    The provider is always constructed; without credentials it reports
    ``is_configured == False`` and is skipped during search instead of
    breaking it.
    """

    return [AliExpressProvider(settings.aliexpress)]


def build_search_service(settings: Settings) -> tuple[SearchService, AIService]:
    """Create the AI service and the search service around the providers.

    Each AI role gets its own configured model (fast / smart / vision /
    fallback). Nothing here contacts Gemini: availability is only ever
    discovered at request time, and a missing model never stops the bot
    (see ``AIService`` and ``services.product_assistant``).
    """

    ai_service = AIService(
        api_key=settings.gemini_api_key,
        model=settings.gemini_smart_model,
        fast_model=settings.gemini_fast_model,
        vision_model=settings.gemini_vision_model,
        fallback_model=settings.gemini_fallback_model,
    )
    search_service = SearchService(
        providers=build_providers(settings),
        ai_service=ai_service,
        max_search_pages=settings.search_max_pages,
    )
    return search_service, ai_service


def build_rate_limiter():
    """Return a client-side rate limiter, or ``None`` if unavailable.

    ``AIORateLimiter`` belongs to the optional ``rate-limiter`` extra of
    python-telegram-bot (it pulls in ``aiolimiter``). The bot must run
    with the base install, so a missing extra degrades to no client-side
    throttling instead of failing at startup.
    """

    try:
        from telegram.ext import AIORateLimiter

        return AIORateLimiter()
    except Exception:
        logger.warning(
            "Client-side rate limiting is disabled: install "
            "'python-telegram-bot[rate-limiter]' to enable it."
        )
        return None


def build_application(
    settings: Settings | None = None, token: str | None = None
) -> Application:
    """Create the configured ``Application`` with all handlers registered."""

    settings = settings or default_settings
    bot_token = token or settings.bot_token
    if not bot_token:
        raise RuntimeError(
            "BOT_TOKEN is not configured. Set it in .env before starting the bot."
        )

    database = Database(settings.database_full_path)
    search_service, ai_service = build_search_service(settings)

    builder = ApplicationBuilder().token(bot_token)
    rate_limiter = build_rate_limiter()
    if rate_limiter is not None:
        builder = builder.rate_limiter(rate_limiter)

    application = (
        builder.post_init(_post_init).post_shutdown(_post_shutdown).build()
    )

    # Shared objects live in bot_data so handlers can reach them via
    # context.application.bot_data (see handlers/common.py).
    application.bot_data["settings"] = settings
    application.bot_data["database"] = database
    application.bot_data["search_service"] = search_service
    application.bot_data["ai_service"] = ai_service
    # The AI product menu reasons over the SAME search service, so every
    # candidate it offers comes from the existing marketplace pipeline.
    application.bot_data["product_assistant"] = ProductAssistant(
        ai_service=ai_service, search_service=search_service
    )

    _register_handlers(application)
    return application


def _register_handlers(application: Application) -> None:
    """Register commands, callbacks and the free-text search handler."""

    application.add_handler(CommandHandler("start", start.start_command))
    application.add_handler(CommandHandler("help", start.help_command))
    application.add_handler(CommandHandler("new", newchat.new_command))
    application.add_handler(CommandHandler("favorites", favorites.favorites_command))
    application.add_handler(
        CommandHandler("tracking", settings_handlers.tracking_command)
    )
    application.add_handler(CommandHandler("stats", settings_handlers.stats_command))
    application.add_handler(CommandHandler("admin", admin.admin_command))

    application.add_handler(
        CallbackQueryHandler(
            start.menu_callback,
            pattern=f"^({'|'.join(MAIN_MENU_CALLBACKS)})$",
        )
    )
    # «◀️ Назад» restores the real previous screen; legacy buttons of
    # removed screens get an honest tombstone instead of a dead spinner.
    application.add_handler(
        CallbackQueryHandler(start.nav_back_callback, pattern=f"^{CB_NAV_BACK}$")
    )
    application.add_handler(
        CallbackQueryHandler(
            start.removed_screen_callback, pattern=REMOVED_CALLBACK_PATTERN
        )
    )
    application.add_handler(
        CallbackQueryHandler(admin.admin_callback, pattern=f"^{CB_ADMIN_PREFIX}:")
    )
    application.add_handler(
        CallbackQueryHandler(search.favorite_callback, pattern=f"^{CB_PREFIX_FAVORITE}:")
    )
    application.add_handler(
        CallbackQueryHandler(
            search.track_price_callback, pattern=f"^{CB_PREFIX_TRACK}:"
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            search.card_untrack_callback, pattern=f"^{CB_PREFIX_CARD_UNTRACK}:"
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            favorites.favorite_remove_callback,
            pattern=f"^{CB_PREFIX_FAVORITE_REMOVE}:",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            settings_handlers.track_stop_callback, pattern=f"^{CB_PREFIX_TRACK_STOP}:"
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            hot_discounts.hot_discount_section_callback,
            pattern=f"^{CB_HOT_SECTION_PREFIX}:",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            discovery.category_callback, pattern=f"^{CB_CATEGORY_PREFIX}:"
        )
    )
    # The exhausted «✨ Находки» screen: an explicit restart of the
    # per-user history (never automatic). The legacy ``pop:reset`` of the
    # removed «🔥 Популярное» screen maps to the very same handler.
    application.add_handler(
        CallbackQueryHandler(
            discovery.feed_reset_callback,
            pattern=f"^({'|'.join((CB_FEED_RESET, CB_POPULAR_RESET))})$",
        )
    )
    # «🎟 Промокоды»: campaign buttons of an earlier version are tombstoned -
    # the section now opens the configured partner/referral URL directly.
    application.add_handler(
        CallbackQueryHandler(
            promo_codes.promo_legacy_callback,
            pattern=f"^{CB_PROMO_CAMPAIGN_LEGACY_PREFIX}:",
        )
    )
    # The reusable sales section (initially «Чёрная пятница»): a dedicated,
    # server-side gated handler. It is registered BEFORE the catch-all
    # ``stale_callback`` so a press is always enforced here (enabled, or an
    # admin testing) - a non-admin pressing a disabled event is refused even
    # when the callback data is forged, never a dead-end and never a leak.
    application.add_handler(
        CallbackQueryHandler(
            sales.sale_callback, pattern=f"^{CB_SALE_PREFIX}:"
        )
    )
    application.add_handler(
        CallbackQueryHandler(search.next_page_callback, pattern=f"^{CB_NEXT_PREFIX}:")
    )
    application.add_handler(
        CallbackQueryHandler(search.browse_callback, pattern=f"^{CB_PREFIX_NAV}:")
    )
    # The 🤖 AI menu of a product card (§7): the menu itself and its five
    # actions - both work on the product the card shows, never on a new one.
    application.add_handler(
        CallbackQueryHandler(
            ai_assistant.ai_menu_callback, pattern=f"^{CB_PREFIX_AI}:"
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            ai_assistant.ai_action_callback, pattern=f"^{CB_PREFIX_AI_ACTION}:"
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            region.currency_callback, pattern=rf"^{CB_CURRENCY_PREFIX}(:|$)"
        )
    )
    application.add_handler(CallbackQueryHandler(start.noop_callback, pattern="^noop:"))
    # Notifications preference (§36) and the onboarding pager (§37).
    application.add_handler(
        CallbackQueryHandler(
            settings_handlers.notifications_toggle_callback,
            pattern=f"^{CB_NOTIFY_TOGGLE}$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            start.onboarding_callback, pattern=f"^{CB_ONBOARDING_PREFIX}:"
        )
    )
    # Last resort, registered LAST inside this group: a callback nobody
    # else claimed (an old message, a retired button) must never be a
    # silent no-op - the real-Telegram test where «❤️ Избранное» did
    # NOTHING is this hole. Every dedicated handler above wins first; this
    # one only sees what they refused (see handlers.start.stale_callback).
    application.add_handler(CallbackQueryHandler(start.stale_callback))

    # A sent photo is a product search by image - answered honestly when no
    # provider supports image search yet (see handlers.discovery).
    application.add_handler(MessageHandler(filters.PHOTO, discovery.photo_message))

    # Any plain text message is treated as a product search query.
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, search.search_message)
    )

    application.add_error_handler(error_handler)


async def _post_init(application: Application) -> None:
    """Initialize the database once the event loop is running."""

    database: Database = application.bot_data["database"]
    await database.init_db()

    # Real automatic price tracking. Built here because the notifier
    # needs the live bot instance; it is a no-op when no provider is
    # configured (the tracking UI then reports manual tracking).
    tracker = price_tracking.build_price_tracker(
        database, application.bot_data["search_service"], application.bot
    )
    application.bot_data["price_tracker"] = tracker
    price_tracking.schedule_price_tracking(application)

    # Telegram shows /start (and the rest) in its menu button from now on.
    await publish_command_menu(application.bot)

    search_service: SearchService = application.bot_data["search_service"]
    configured = [
        provider.display_name
        for provider in search_service.providers
        if provider.is_configured
    ]
    logger.info(
        "Bot started. Configured marketplaces: %s",
        ", ".join(configured) or "none (add API credentials to .env)",
    )

    ai_service: AIService = application.bot_data["ai_service"]
    logger.info("Gemini AI: %s", "enabled" if ai_service.is_available else "disabled")


async def _post_shutdown(application: Application) -> None:
    """Release provider HTTP clients on shutdown."""

    search_service: SearchService | None = application.bot_data.get("search_service")
    if search_service is not None:
        await search_service.aclose()


def _is_noop_edit(error: object) -> bool:
    """Whether Telegram rejected an edit because nothing would change.

    ``BadRequest("Message is not modified: ...")`` means the screen the
    user is looking at is already exactly what the handler wanted to draw.
    Treating it as an error would show an "internal error" alert for a
    perfectly working button.
    """

    if not isinstance(error, BadRequest):
        return False
    return "Message is not modified" in str(error)


async def error_handler(update: object, context) -> None:
    """Log every unhandled exception and keep the bot running."""

    # "Message is not modified" is not a failure: Telegram raises it when a
    # button press asks to re-render a screen with exactly the content it
    # already shows - pressing the currency that is already selected, or
    # opening the same menu section twice on one message. The screen is
    # already correct, so the user must not be told about an "internal
    # error" (verified live: choosing the active currency crashed this way).
    if _is_noop_edit(context.error):
        logger.debug("Ignored a no-op message edit: %s", context.error)
        return

    logger.error(
        "Unhandled exception while processing update: %s",
        context.error,
        exc_info=context.error,
    )

    # The failure itself is a statistic: only the exception class name is
    # stored - never a traceback, which could carry a token (see
    # services/analytics.py).
    try:
        database = context.application.bot_data.get("database")
        if database is not None:
            await database.record_event(
                analytics.EVENT_ERRORS,
                metadata={"exception": type(context.error).__name__},
            )
    except Exception:  # pragma: no cover - analytics must never raise here
        logger.debug("Failed to record the errors event", exc_info=True)

    if isinstance(update, Update) and update.effective_message is not None:
        try:
            await update.effective_message.reply_text(
                "⚠️ Произошла внутренняя ошибка. Попробуйте ещё раз."
            )
        except Exception:  # pragma: no cover - Telegram/network failure
            logger.exception("Failed to notify the user about the error")

