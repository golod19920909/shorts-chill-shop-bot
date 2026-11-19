"""Price tracking screen and user settings/statistics.

The periodic price-check job is real (``services/price_tracker.py`` +
the application JobQueue); this module manages what the user tracks and
reports the actual automation status honestly.
"""

from __future__ import annotations

import logging
from typing import Any

from telegram import Update
from telegram.ext import ContextTypes

from config.catalog import currency_label
from handlers import common
from keyboards.main import (
    CB_NOTIFY_TOGGLE,
    back_to_menu_keyboard,
    notifications_keyboard,
    settings_keyboard,
)
from keyboards.products import CB_PREFIX_TRACK_STOP, CB_TRACK_PAGE, tracked_list_keyboard
from services import analytics
from utils.emoji import INFO, SETTINGS
from utils.formatting import format_price

logger = logging.getLogger(__name__)

TRACKING_PAGE_SIZE = 10
TRACKING_PAGE_KEY = "tracking_page"

EMPTY_TRACKING_TEXT = (
    "📉 <b>Отслеживание цен</b>\n\n"
    "Список пуст. Найдите товар и нажмите «📉 Отслеживать» — бот будет "
    "периодически проверять цену и сообщит, когда она изменится."
)

TRACKING_HEADER = (
    "📉 <b>Отслеживание цен</b>\n\n"
    "{status}\n"
)

TRACKING_STATUS_AUTO = "🟢 Автоматическая проверка цен включена: {cadence}."
TRACKING_STATUS_MANUAL = (
    "🟡 Автоматическая проверка цен сейчас недоступна (нет настроенного "
    "провайдера, очереди задач или включённого интервала): список сохраняется."
)


def _cadence_phrase(minutes: int) -> str:
    """The check cadence in the unit that reads naturally.

    The scheduler works in minutes, but the screen shows what a person
    expects: the 24-hour default must read "раз в 1 дн.", never "раз в
    1440 мин.".
    """

    if minutes % 1440 == 0:
        return f"раз в {minutes // 1440} дн."
    if minutes % 60 == 0:
        return f"раз в {minutes // 60} ч."
    return f"раз в {minutes} мин."


def tracking_status_line(context: ContextTypes.DEFAULT_TYPE) -> str:
    """Honest automation status based on the real scheduler wiring."""

    bot_data = context.application.bot_data
    tracker = bot_data.get("price_tracker")
    interval = int(
        getattr(bot_data.get("settings"), "tracking_interval_minutes", 1440) or 0
    )
    scheduled = bot_data.get("price_tracking_scheduled") is True
    if tracker is not None and interval > 0 and scheduled:
        return TRACKING_STATUS_AUTO.format(cadence=_cadence_phrase(interval))
    return TRACKING_STATUS_MANUAL


async def tracking_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_tracking(update, context, page=0)


async def show_tracking(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    page: int | None = None,
) -> None:
    """Render the list of price-tracked products."""

    user_id = await common.register_user(update, context)
    if user_id is None:
        await _reply(update, context, "⚠️ Не удалось получить данные пользователя.")
        return

    try:
        rows = await common.get_database(context).get_tracked_products(user_id)
    except Exception:
        logger.exception("Failed to load tracked products for user %s", user_id)
        await _reply(update, context, "⚠️ Ошибка чтения базы данных.")
        return

    if not rows:
        await _reply(
            update,
            context,
            EMPTY_TRACKING_TEXT,
            parse_mode="HTML",
            reply_markup=back_to_menu_keyboard(),
        )
        return

    total_pages = max(1, (len(rows) + TRACKING_PAGE_SIZE - 1) // TRACKING_PAGE_SIZE)
    if page is None:
        page = context.user_data.get(TRACKING_PAGE_KEY, 0)
    try:
        page = int(page)
    except (TypeError, ValueError):
        page = 0
    page = min(max(0, page), total_pages - 1)
    context.user_data[TRACKING_PAGE_KEY] = page

    start = page * TRACKING_PAGE_SIZE
    visible_rows = rows[start : start + TRACKING_PAGE_SIZE]
    items: list[dict] = []
    lines: list[str] = [TRACKING_HEADER.format(status=tracking_status_line(context))]
    if total_pages > 1:
        lines.append(f"Страница {page + 1}/{total_pages} · всего товаров: {len(rows)}")
        lines.append("")

    for row in visible_rows:
        items.append(
            {
                "tracked_id": row["id"],
                "title": row["title"],
                "url": row["product_url"],
                "price": (row["last_checked_price"] if row["last_checked_price"] is not None else row["initial_price"]),
                "currency": row["currency"],
            }
        )
        displayed_price = (
            row["last_checked_price"]
            if row["last_checked_price"] is not None
            else row["initial_price"]
        )
        price = format_price(displayed_price, row["currency"])
        lines.append(
            f"• {common.escape(row['title'] or 'Без названия')} — "
            f"{common.escape(price)} "
            f"({common.escape(str(row['marketplace']))})"
        )

    await _reply(
        update,
        context,
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=tracked_list_keyboard(
            items, page=page, total_pages=total_pages
        ),
    )


async def tracking_page_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Move between bounded pages of the user's tracked-product list."""

    query = update.callback_query
    if query is None:
        return
    parts = (query.data or "").split(":")
    if len(parts) != 2 or parts[0] != CB_TRACK_PAGE:
        await query.answer("Некорректная кнопка.", show_alert=True)
        return
    try:
        page = int(parts[1])
    except ValueError:
        await query.answer("Некорректная кнопка.", show_alert=True)
        return
    if page < 0:
        await query.answer("Некорректная кнопка.", show_alert=True)
        return
    await query.answer()
    await show_tracking(update, context, page=page)


async def track_stop_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """``⏹ Остановить`` button in the tracking list."""

    query = update.callback_query
    if query is None:
        return

    parts = (query.data or "").split(":")
    if len(parts) != 2 or parts[0] != CB_PREFIX_TRACK_STOP:
        await query.answer("Некорректная кнопка.", show_alert=True)
        return

    try:
        tracked_id = int(parts[1])
    except ValueError:
        await query.answer("Некорректная кнопка.", show_alert=True)
        return

    user_id = await common.register_user(update, context)
    if user_id is None:
        await query.answer("Ошибка базы данных.", show_alert=True)
        return

    try:
        await common.get_database(context).deactivate_tracked_product(
            tracked_product_id=tracked_id, user_id=user_id
        )
    except Exception:
        logger.exception("Failed to stop tracking %s for user %s", tracked_id, user_id)
        await query.answer("Не удалось остановить отслеживание.", show_alert=True)
        return

    await common.record_event(
        context,
        analytics.EVENT_TRACKING_STOPPED,
        user_id=user_id,
        telegram_id=update.effective_user.id if update.effective_user else None,
        metadata={"tracked_id": tracked_id},
    )
    await query.answer("⏹ Отслеживание остановлено")
    await show_tracking(update, context)


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Small diagnostic command: what is configured and stored."""

    message = update.effective_message
    if message is None:
        return

    search_service = common.get_search_service(context)
    ai_service = common.get_ai_service(context)

    lines = ["⚙️ <b>Состояние бота</b>", ""]

    lines.append(
        "AI-парсинг запросов: "
        + ("подключён (GEMINI_API_KEY задан)" if ai_service and ai_service.is_available
           else "не настроен — используется локальный парсер")
    )
    lines.append("")
    lines.append("<b>Маркетплейсы:</b>")
    for provider in search_service.providers:
        status = "подключён" if provider.is_configured else "нет API-ключей"
        lines.append(f"• {common.escape(provider.display_name)} — {status}")

    await message.reply_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=back_to_menu_keyboard()
    )


NOTIFICATIONS_TEXT = (
    "\U0001F514 <b>Уведомления</b>\n\n"
    "Уведомления о изменении цены: <b>{status}</b>.\n\n"
    "Бот проверяет отслеживаемые товары периодически и сообщает, когда "
    "маркетплейс вернул новую цену в той же валюте. При смене валюты или "
    "отсутствии корректной цены уведомление не отправляется — сравнение "
    "чисел из разных валют вводило бы в заблуждение."
)


async def show_notifications(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Current notification preference + a real toggle (spec §36)."""

    user_id = await common.register_user(update, context)
    if user_id is None:
        await _reply(update, context, "⚠️ Не удалось получить данные пользователя.")
        return

    database = common.get_database(context)
    try:
        enabled = await database.get_notification_pref(user_id)
    except Exception:
        logger.exception("Failed to read notification pref for user %s", user_id)
        await _reply(update, context, "⚠️ Не удалось прочитать настройку уведомлений.")
        return

    await _reply(
        update,
        context,
        NOTIFICATIONS_TEXT.format(status="включены" if enabled else "выключены"),
        parse_mode="HTML",
        reply_markup=notifications_keyboard(enabled),
    )


async def notifications_toggle_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """``notif:toggle`` - flip the stored preference and re-render."""

    query = update.callback_query
    if query is None:
        return
    await query.answer()

    user_id = await common.register_user(update, context)
    if user_id is None:
        await _reply(update, context, "⚠️ Не удалось получить данные пользователя.")
        return

    database = common.get_database(context)
    try:
        enabled = await database.get_notification_pref(user_id)
        await database.set_notification_pref(user_id, not enabled)
    except Exception:
        logger.exception("Failed to toggle notification pref for user %s", user_id)
        await _reply(update, context, "⚠️ Не удалось сохранить настройку.")
        return

    await show_notifications(update, context)


SETTINGS_TEXT = (
    f"{SETTINGS} <b>Настройки</b>\n\n"
    "Здесь только личные предпочтения — поиск работает и без них. "
    "Валюта передаётся маркетплейсу, но бот не пересчитывает цены."
)


async def show_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Preferences plus what is currently stored for this user.

    Every number is read back from the database, and a section that
    cannot be read says so instead of being rendered as a confident zero.
    """

    user_id = await common.register_user(update, context)
    if user_id is None:
        await _reply(update, context, "⚠️ Не удалось получить данные пользователя.")
        return

    region = await common.get_user_region(context, user_id)
    database = common.get_database(context)

    favorites = await _safe_rows(database.get_favorites, user_id)
    tracked = await _safe_rows(database.get_tracked_products, user_id)
    history = await _safe_rows(database.get_search_history, user_id, 5)

    lines = [
        SETTINGS_TEXT,
        "",
        "• Валюта: " + common.escape(currency_label(region.get("currency"))),
        "• В избранном: " + _items_label(favorites),
        "• Отслеживается цен: " + _items_label(tracked),
    ]

    if history:
        queries = ", ".join(
            common.escape(str(row.get("original_query") or "без названия"))
            for row in history[:5]
        )
        lines.append("• Последние запросы: " + queries)

    lines.extend(
        [
            "",
            f"{INFO} Пустые разделы означают, что данных пока нет: бот не "
            "дополняет список товарами, которых не отдал маркетплейс.",
        ]
    )

    await _reply(
        update,
        context,
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=settings_keyboard(),
    )


async def _safe_rows(loader: Any, *args: Any) -> list[dict] | None:
    """Read a list from the database; ``None`` means "could not read"."""

    try:
        return await loader(*args)
    except Exception:
        logger.exception(
            "Failed to read settings data (%s)", getattr(loader, "__name__", loader)
        )
        return None


def _items_label(rows: list | None) -> str:
    """Human count for a stored list, honest about a failed read."""

    if rows is None:
        return "не удалось прочитать"
    count = len(rows)
    word = "товар" if count % 10 == 1 and count % 100 != 11 else "товаров"
    return f"{count} {word}"


async def _reply(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, **kwargs
) -> None:
    """Reply to a command or edit the message a callback came from."""

    query = update.callback_query
    if query is not None:
        await query.edit_message_text(text, **kwargs)
        return

    message = update.effective_message
    if message is not None:
        await message.reply_text(text, **kwargs)
