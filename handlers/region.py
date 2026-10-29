"""User preference screen: the preferred currency.

The final spec's currency screen in ONE step: the current selection,
then the flag grid of all supported currencies, then
``◀️ Назад`` (the screen it was opened from - the menu or the settings)
and ``🏠 Главное меню``. Changing the currency invalidates the cached
search sessions of this user so stale product cards cannot keep showing
the old currency.

There is deliberately NO country selector: the bot cannot know the user's
country, Telegram does not provide it, and the project forbids IP
geolocation. ``users.country`` therefore stays a spare column that
nothing in the UI writes.

The bot never converts prices and never invents an exchange rate: the
choice is only forwarded to a marketplace whose API documents that
currency (see ``services/marketplaces/aliexpress_client.py``).
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from config.catalog import currency_label, is_supported_currency
from handlers import common
from keyboards.main import CB_BACK, CB_CURRENCY_PREFIX, currencies_keyboard
from services import analytics

logger = logging.getLogger(__name__)

#: key in ``context.user_data``: callback data of the screen the currency
#: screen was opened from, restored by "◀️ Назад".
NAV_BACK_KEY = "nav_back"
CURRENCY_TEXT = (
    "🌍 <b>Валюта</b>\n\n"
    "Текущая валюта: <b>{currency}</b>\n\n"
    "Пункт необязательный — бот не пересчитывает цены и не использует "
    "курсы валют: выбранная валюта просто передаётся маркетплейсу, "
    "если он умеет в ней показывать цены. AliExpress показывает цены в "
    "USD, EUR, UAH, RUB и GBP; для остальных валют вернётся та, "
    "которую поддерживает сам маркетплейс."
)


async def show_currency(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    back_callback: str = CB_BACK,
) -> None:
    """Render the currency screen and remember where "◀️ Назад" returns."""

    user_id = await common.register_user(update, context)
    region = await common.get_user_region(context, user_id)
    context.user_data[NAV_BACK_KEY] = back_callback

    await _edit_or_reply(
        update,
        context,
        CURRENCY_TEXT.format(
            currency=common.escape(currency_label(region.get("currency")))
        ),
        parse_mode="HTML",
        reply_markup=currencies_keyboard(),
    )


async def currency_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Flag button pressed: store the code and re-render the screen."""

    query = update.callback_query
    if query is None:
        return

    data = query.data or ""
    parts = data.split(":", 1)
    code = parts[1] if len(parts) == 2 else ""

    if not code:
        # A stale bare "cur" button from an older screen: just re-render
        # the current one-screen picker.
        await query.answer()
        await show_currency(update, context)
        return

    user_id = await common.register_user(update, context)
    if user_id is None:
        await query.answer("Ошибка базы данных.", show_alert=True)
        return

    database = common.get_database(context)
    try:
        if code == "clear":
            # Kept only for stale "Не указывать" buttons of earlier
            # versions; the new screen has no clear button.
            await database.update_user_region(user_id, clear_currency=True)
            await query.answer("Валюта не указана")
        elif not is_supported_currency(code):
            await query.answer("Такая валюта не поддерживается.", show_alert=True)
            return
        else:
            await database.update_user_region(user_id, currency=code.upper())
            await query.answer(f"Валюта: {code.upper()}")
    except Exception:
        logger.exception("Failed to store currency %r for user %s", code, user_id)
        await query.answer("Не удалось сохранить.", show_alert=True)
        return

    # The stored tracking baseline is in the OLD currency and must never
    # be compared with prices in the new one: drop it so the first new
    # observation becomes the silent baseline in the new currency.
    try:
        await database.invalidate_tracking_baselines(user_id)
    except Exception:
        logger.exception(
            "Failed to invalidate tracking baselines for user %s", user_id
        )

    await common.record_event(
        context,
        analytics.EVENT_CURRENCY_CHANGED,
        user_id=user_id,
        telegram_id=update.effective_user.id if update.effective_user else None,
        metadata={"currency": code},
    )

    # Cached product sessions still hold prices in the OLD currency:
    # drop them so a stale card can never be browsed with a fresh
    # expectation (its buttons then answer "Результат устарел").
    context.user_data.pop(common.OUTCOME_CACHE_KEY, None)
    context.user_data.pop(common.OUTCOME_META_KEY, None)

    await show_currency(
        update,
        context,
        back_callback=context.user_data.get(NAV_BACK_KEY, CB_BACK),
    )


async def _edit_or_reply(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, **kwargs
) -> None:
    """Edit the screen a callback came from, or reply to a command."""

    query = update.callback_query
    if query is not None:
        await query.edit_message_text(text, **kwargs)
        return

    message = update.effective_message
    if message is not None:
        await message.reply_text(text, **kwargs)