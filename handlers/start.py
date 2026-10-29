"""``/start``, help text and the main-menu router.

The menu is the only place that knows about the top-level sections.
Three navigation mechanisms live here:

- ``dispatch_menu`` - renders the screen for one menu callback (shared by
  the router and by "◀️ Назад");
- ``nav_back_callback`` - restores the REAL previous screen remembered in
  ``context.user_data`` (spec: Back is not Main Menu);
- ``removed_screen_callback`` - tombstone for buttons of removed screens
  still present in already-sent messages: honest answer + main menu.
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from handlers import common
from handlers import sales as sales_handlers
from handlers.region import NAV_BACK_KEY, show_currency
from keyboards.main import (
    CB_BACK,
    CB_CATEGORIES,
    CB_CURRENCY_FROM_SETTINGS,
    CB_CURRENCY_SCREEN,
    CB_FAVORITES,
    CB_FEED,
    CB_HELP,
    CB_HOT_DISCOUNTS,
    CB_NOTIFICATIONS,
    CB_PHOTO,
    CB_POPULAR,
    CB_PROMO_CODES,
    CB_SEARCH,
    CB_SETTINGS,
    CB_TUTORIAL,
    CB_TRACKING,
    MAIN_MENU_CALLBACKS,
    REMOVED_CALLBACK_PATTERN,
    back_to_menu_keyboard,
    main_menu_keyboard,
    onboarding_keyboard,
)
from services import analytics
from utils.emoji import (
    CATEGORIES,
    CURRENCY,
    FAVORITES,
    FINDINGS,
    HELP,
    PHOTO,
    SEARCH,
    SETTINGS,
    TICKET,
    TRACKING,
    WARNING,
    WAVE,
)

logger = logging.getLogger(__name__)

WELCOME_TEXT = (
    f"{WAVE} <b>Поиск товаров</b>\n\n"
    "Опишите словами, что хотите найти — бот покажет товары "
    "с ценой, рейтингом и количеством заказов.\n\n"
    "Например:\n"
    "• <i>беспроводные наушники до $30 с хорошими отзывами</i>\n"
    "• <i>видеорегистратор с ночным видением до $50</i>\n"
    "• <i>чехол для телефона, 2 штуки, с рейтингом выше 4.5</i>\n\n"
    "Просто отправьте сообщение с запросом.\n"
    f"{CURRENCY} Валюту можно указать позже — это необязательно."
)

#: The concise FIRST-launch welcome, shown once via ``/start`` and never again
#: (the persistent ``users.onboarded`` flag gates it). Natural, no technical
#: terms, no promises of the lowest price or guaranteed discounts - it only
#: says what the bot does and points the user at the AI product assistant.
WELCOME_MESSAGE = (
    f"{WAVE} <b>Добро пожаловать!</b>\n\n"
    "Здесь можно искать товары на AliExpress, просто описав, что вам нужно. "
    "Укажите бюджет и важные требования — бот поможет подобрать подходящие "
    "варианты.\n\n"
    f"{SEARCH} Начните с раздела <b>«Умный поиск»</b>!"
)

HELP_TEXT = (
    f"{HELP} <b>Как пользоваться</b>\n\n"
    f"{SEARCH} <b>Умный поиск</b> — опишите словами, что нужно, "
    "и бот найдёт товары на AliExpress\n"
    f"{PHOTO} <b>Найти по фото</b> — пришлите фото, найду похожие товары\n"
    f"{FINDINGS} <b>Находки</b> — подборка товаров с AliExpress: сразу "
    "открывается карточка первого товара\n"
    f"{CATEGORIES} <b>Категории</b> — поиск по категориям\n"
    f"{FAVORITES} <b>Избранное</b> — сохранённые товары\n"
    f"{TRACKING} <b>Отслеживание цен</b> — бот проверяет цену и сообщит "
    "об изменении\n"
    "\U0001F525 <b>Горячие скидки</b> — подборки товаров со скидками "
    "AliExpress\n"
    f"{TICKET} <b>Промокоды</b> — актуальные промокоды и скидки "
    "AliExpress\n"
    f"{CURRENCY} <b>Валюта</b> — необязательно; влияет на цены только там, "
    "где это поддерживает сам маркетплейс\n"
    f"{SETTINGS} <b>Настройки</b> — профиль и список возможностей\n\n"
    "<b>Карточка товара</b>\n"
    "Купить · В избранное · Следить за ценой · Поделиться · "
    "◀️ Назад / ➡️ Следующий · 🏠 Главное меню\n\n"
    "<b>Важно</b>\n"
    "Цены, рейтинги и ссылки приходят из данных маркетплейсов. Если данных "
    "нет, бот не додумывает их, а честно пропускает. Цена показана на момент "
    "поиска — итоговая цена и условия видны при открытии товара."
)

SEARCH_PROMPT = (
    "Привет! Я умный помощник Shorts Chill 🛍️\n\n"
    "Просто напишите, что вы ищете — своими словами. Например:\n"
    "• беспроводные наушники до 30$\n"
    "• мужской кошелёк без бренда\n"
    "• компактный пылесос для машины\n\n"
    "Можно указывать бюджет в долларах или в валюте своей страны, а также "
    "рейтинг, исключения («без…») и другие пожелания.\n"
    "Я подберу варианты, сравню их и помогу выбрать."
)

#: The screen behind «📷 Найти по фото» (§11): one short instruction, no
#: mention of models, APIs or how the recognition works.
PHOTO_PROMPT = (
    f"{PHOTO} <b>Поиск по фото</b>\n\n"
    "Пришлите фотографию товара."
)
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = await common.register_user(update, context)
    telegram_id = update.effective_user.id if update.effective_user else None
    await common.record_event(
        context,
        analytics.EVENT_USER_STARTED,
        user_id=user_id,
        telegram_id=telegram_id,
    )

    # Spec §37: the FIRST /start shows the concise welcome; later starts go
    # straight to the menu. A failed read falls back to the menu rather than
    # replaying the welcome forever. ``users.onboarded`` is the persistent
    # flag, so the choice survives restarts and needs no new storage.
    onboarded = True
    if user_id is not None:
        try:
            onboarded = await common.get_database(context).is_onboarded(user_id)
        except Exception:
            logger.exception("Failed to read onboarding flag for %s", user_id)

    if not onboarded:
        # Mark onboarded immediately: the concise welcome is shown exactly
        # once, whatever the user does next (a failed mark just means a repeat
        # welcome on the next /start, never a broken menu).
        if user_id is not None:
            try:
                await common.get_database(context).mark_onboarded(user_id)
            except Exception:
                logger.exception("Failed to mark user %s as onboarded", user_id)
        await _edit_to(
            update,
            WELCOME_MESSAGE,
            await _main_menu_markup(context, telegram_id),
        )
        return

    await update.effective_message.reply_text(
        WELCOME_TEXT,
        parse_mode="HTML",
        reply_markup=await _main_menu_markup(context, telegram_id),
    )


async def _main_menu_markup(context: ContextTypes.DEFAULT_TYPE, telegram_id: int | None):
    """The main menu, plus any enabled/admin-visible sales event buttons.

    Built asynchronously so the promotional section can be injected only when
    it is actually visible to THIS caller (enabled for everyone, or an admin
    testing a disabled event). A failure here degrades to the plain menu - the
    regular product-search sections are never affected.
    """

    try:
        sale_buttons = await sales_handlers.sale_menu_buttons(context, telegram_id)
    except Exception:  # noqa: BLE001 - the menu must render regardless
        logger.exception("Failed to build sales menu buttons")
        sale_buttons = []
    return main_menu_keyboard(sale_buttons)


#: (title, text) pages of the first-run onboarding (spec §37): search,
#: refinement, AI, photo, favorites, tracking, currency - seven short
#: screens, no country-specific examples, no UAH dependency.
ONBOARDING_SCREENS: tuple[tuple[str, str], ...] = (
    (
        "Поиск",
        f"{SEARCH} <b>1/7 Поиск</b>\n\n"
        "Опишите товар обычным текстом: «беспроводные наушники до 30 "
        "долларов», «механическая клавиатура». Бот найдёт реальные товары "
        "на AliExpress с ценой и рейтингом.",
    ),
    (
        "Уточнение",
        f"{SEARCH} <b>2/7 Уточнение</b>\n\n"
        "Опишите товар целиком в одном сообщении: бюджет («до 20 долларов»), "
        "рейтинг («выше 4.5»), количество заказов и количество штук бот "
        "разбирает из текста. Каждое сообщение — новый поиск: прежний "
        "запрос не подставляется молча, так что результаты всегда "
        "соответствуют тому, что вы написали.",
    ),
    (
        "AI",
        "\U0001F916 <b>3/7 AI</b>\n\n"
        "AI помогает понять запрос: разбирает бюджет, рейтинг и количество "
        "заказов из обычного текста, отвечает на вопросы о товаре прямо на "
        "карточке и описывает предмет на фотографии. Товары, цены, рейтинги и "
        "ссылки приходят только из данных AliExpress — AI их не выдумывает. "
        "Когда AI недоступен, поиск продолжает работать на встроенном "
        "разборе запроса.",
    ),
    (
        "Фото",
        f"{PHOTO} <b>4/7 Поиск по фото</b>\n\n"
        "Пришлите фотографию — бот определит товар и покажет похожие "
        "варианты с AliExpress.",
    ),
    (
        "Избранное",
        f"{FAVORITES} <b>5/7 Избранное</b>\n\n"
        "Кнопка ❤️ на карточке сохраняет товар — список всегда доступен "
        "в главном меню.",
    ),
    (
        "Отслеживание",
        f"{TRACKING} <b>6/7 Отслеживание цен</b>\n\n"
        "Кнопка 🔔 следит за ценой и сообщит об изменении — в той же "
        "валюте, в которой вы выбрали сравнивать.",
    ),
    (
        "Валюта",
        f"{CURRENCY} <b>7/7 Валюта</b>\n\n"
        "Выберите валюту в настройках — она сохранится и будет "
        "передаваться маркетплейсу при поиске. Готово!",
    ),
)


async def onboarding_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """``onb:<step>`` / ``onb:done`` - the onboarding pager (§37)."""

    query = update.callback_query
    if query is None:
        return
    await query.answer()

    data = query.data or ""
    _, _, arg = data.partition(":")

    if arg == "done":
        user_id = await common.register_user(update, context)
        if user_id is not None:
            try:
                await common.get_database(context).mark_onboarded(user_id)
            except Exception:
                logger.exception("Failed to mark user %s as onboarded", user_id)
        await _show_main_menu(query, context)
        return

    try:
        step = int(arg)
    except ValueError:
        step = 0
    if not 0 <= step < len(ONBOARDING_SCREENS):
        step = 0

    await query.edit_message_text(
        ONBOARDING_SCREENS[step][1],
        parse_mode="HTML",
        reply_markup=onboarding_keyboard(step, len(ONBOARDING_SCREENS)),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message is None:
        return
    await update.effective_message.reply_text(
        HELP_TEXT, parse_mode="HTML", reply_markup=back_to_menu_keyboard()
    )


async def dispatch_menu(
    data: str, update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    """Render the screen for one menu callback; ``False`` = unknown."""

    if data == CB_SEARCH:
        await _edit_to(update, SEARCH_PROMPT, back_to_menu_keyboard())
        return True

    if data == CB_PHOTO:
        await _edit_to(update, PHOTO_PROMPT, back_to_menu_keyboard())
        return True

    if data == CB_CATEGORIES:
        from handlers.discovery import show_categories

        await show_categories(update, context)
        return True

    if data in (CB_FEED, CB_POPULAR):
        from handlers.discovery import show_feed

        await show_feed(update, context)
        return True

    if data == CB_HOT_DISCOUNTS:
        from handlers.hot_discounts import hot_discounts_callback

        await hot_discounts_callback(update, context)
        return True

    if data == CB_PROMO_CODES:
        from handlers.promo_codes import show_promo_codes

        await show_promo_codes(update, context)
        return True

    if data == CB_FAVORITES:
        from handlers.favorites import show_favorites

        await show_favorites(update, context)
        return True

    if data == CB_TRACKING:
        from handlers.settings import show_tracking

        await show_tracking(update, context)
        return True

    if data == CB_CURRENCY_SCREEN:
        await show_currency(update, context)
        return True

    if data == CB_CURRENCY_FROM_SETTINGS:
        await show_currency(update, context, back_callback=CB_SETTINGS)
        return True

    if data == CB_SETTINGS:
        from handlers.settings import show_settings

        await show_settings(update, context)
        return True

    if data == CB_NOTIFICATIONS:
        from handlers.settings import show_notifications

        await show_notifications(update, context)
        return True

    if data == CB_TUTORIAL:
        # The full tutorial stays available from the settings screen
        # (§37) even for an already-onboarded user.
        await _edit_to(
            update,
            ONBOARDING_SCREENS[0][1],
            onboarding_keyboard(0, len(ONBOARDING_SCREENS)),
        )
        return True

    if data == CB_HELP:
        await _edit_to(update, HELP_TEXT, back_to_menu_keyboard())
        return True

    if data == CB_BACK:
        await _show_main_menu(update.callback_query, context)
        return True

    logger.debug("Unhandled menu callback: %s", data)
    return False


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle every main-menu button press."""

    query = update.callback_query
    if query is None:
        return
    await query.answer()
    await dispatch_menu(query.data or "", update, context)


async def nav_back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``◀️ Назад``: restore the screen the current one was opened from.

    The target is remembered when a screen with a non-menu parent opens
    (the currency screen opened from the settings). Falls back to the
    main menu - the parent of every top-level screen.
    """

    query = update.callback_query
    if query is None:
        return
    await query.answer()

    target = context.user_data.pop(NAV_BACK_KEY, CB_BACK)
    if target not in MAIN_MENU_CALLBACKS:
        target = CB_BACK
    await dispatch_menu(target, update, context)


async def removed_screen_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Tombstone for buttons of REMOVED screens on already-sent messages.

    Honest answer plus the main menu - the removed feature itself is never
    rendered from here, so no old message can reach a hidden route.
    """

    query = update.callback_query
    if query is None:
        return
    await query.answer("Этот раздел убран из бота.", show_alert=False)
    await _show_main_menu(query, context)


async def _answer_once(query, text: str | None = None) -> None:
    """Acknowledge a callback exactly as often as Telegram allows.

    A screen a press already answered (an identical re-render, an inner
    fallback) must not turn the second ``answer()`` into an exception: the
    user's press was acknowledged either way.
    """

    try:
        if text:
            await query.answer(text)
        else:
            await query.answer()
    except Exception:  # noqa: BLE001 - an already-answered query is normal
        logger.debug("Callback query was already answered", exc_info=True)


async def stale_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Last resort for a button press that no other handler matched (§26).

    A callback with no registered handler is answered by NOBODY: the
    client keeps showing the spinner, the message never changes and the
    press looks exactly like «нажал — и ничего не произошло», which is
    what the real Telegram test of «❤️ Избранное» reported when the
    pressed message had been sent by an earlier release (its callback
    data no longer matches any pattern). This handler makes that state
    impossible:

    * the press is ALWAYS answered, whatever happens next;
    * a callback the current router still understands is rendered right
      away - a screen added without its own dedicated handler keeps
      working instead of dying silently;
    * anything else gets one short human line and the main menu, so no
      message can ever become a dead end.

    It is registered LAST in the handler group, so every dedicated
    handler wins: this one only ever sees what nobody claimed.
    """

    query = update.callback_query
    if query is None:
        return

    data = query.data or ""
    if await dispatch_menu(data, update, context):
        await _answer_once(query)
        return

    logger.info("Callback %r has no handler; showing the main menu", data)
    await _answer_once(query, "Кнопка устарела — открываю главное меню.")
    await _show_main_menu(query, context)


async def _edit_to(update: Update, text: str, reply_markup) -> None:
    """Edit the message a callback came from, or reply to a command."""

    query = update.callback_query
    if query is not None:
        await query.edit_message_text(
            text, parse_mode="HTML", reply_markup=reply_markup
        )
        return
    message = update.effective_message
    if message is not None:
        await message.reply_text(text, parse_mode="HTML", reply_markup=reply_markup)


async def _show_main_menu(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the menu on the message a button was pressed on.

    ``edit_message_text`` fails on a PHOTO message (a product card), so
    the fallback sends a fresh menu and removes the old message only AFTER
    the new one is really on screen - if the send fails the user keeps the
    message they are looking at instead of an empty dialog (the only
    Telegram-supported way to replace media with text is delete+send, and
    the order must never dead-end the chat).
    """

    if query is None:
        return
    message = getattr(query, "message", None)
    reply_markup = await _main_menu_markup(
        context, getattr(getattr(query, "from_user", None), "id", None)
    )
    try:
        await query.edit_message_text(
            WELCOME_TEXT, parse_mode="HTML", reply_markup=reply_markup
        )
        return
    except Exception:  # noqa: BLE001 - media messages cannot be text-edited
        logger.debug("edit_message_text failed; sending a fresh menu", exc_info=True)

    chat_id = getattr(message, "chat_id", None)
    if chat_id is None:
        return
    try:
        await context.bot.send_message(
            chat_id,
            WELCOME_TEXT,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )
    except Exception:  # noqa: BLE001 - keep the old message usable
        logger.exception("Failed to send the main menu; keeping the old message")
        return
    if message is not None:
        try:
            await message.delete()
        except Exception:  # noqa: BLE001 - deletion is best-effort
            logger.debug("Could not delete the old message", exc_info=True)


async def noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Answer buttons that intentionally do nothing (e.g. missing links)."""

    query = update.callback_query
    if query is None:
        return
    await query.answer("Для этого товара нет ссылки.")
