"""Favorites: listing saved products and removing them."""

from __future__ import annotations

import json
import logging

from telegram import Update
from telegram.ext import ContextTypes

from handlers import common
from keyboards.main import back_to_menu_keyboard
from keyboards.products import CB_PREFIX_FAVORITE_REMOVE, favorites_list_keyboard
from models.product import Marketplace, Product
from services import analytics
from utils.formatting import format_marketplace_name, format_price

logger = logging.getLogger(__name__)

#: key in ``context.user_data`` caching the currently listed favorites
FAVORITES_CACHE_KEY = "favorites_items"

EMPTY_FAVORITES_TEXT = (
    "⭐ <b>Избранное</b>\n\n"
    "Пока пусто. Найдите товар и нажмите «⭐ В избранное» в карточке."
)

#: Shown when rows exist but none of them can be read any more (every
#: snapshot was written by a release whose schema no longer validates).
STALE_FAVORITES_TEXT = (
    "⭐ <b>Избранное</b>\n\n"
    "Сохранённые товары устарели и больше не отображаются.\n\n"
    "Найдите товар заново и нажмите «⭐ В избранное» в карточке."
)

#: One screen lists at most this many favorites. The rest is reported as a
#: count instead of being dropped silently, so the list always fits into
#: Telegram's 4096-character message limit.
MAX_LISTED_FAVORITES = 12

#: Title length inside the list. Favorites store the marketplace's full
#: title (often 150+ characters); without clipping, a dozen of them would
#: exceed the message limit and Telegram would reject the whole screen.
LIST_TITLE_LIMIT = 60


def _clip(text: str | None, limit: int = LIST_TITLE_LIMIT) -> str:
    """One-line, length-limited title (never invented, only shortened)."""

    collapsed = " ".join((text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1] + "…"


async def favorites_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_favorites(update, context)


async def show_favorites(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Render the favorites screen (from ``/favorites`` or the menu button)."""

    user_id = await common.register_user(update, context)
    query = update.callback_query

    if user_id is None:
        await _reply(update, context, "⚠️ Не удалось получить данные пользователя.")
        return

    try:
        rows = await common.get_database(context).get_favorites(user_id)
    except Exception:
        logger.exception("Failed to load favorites for user %s", user_id)
        await _reply(update, context, "⚠️ Ошибка чтения базы данных.")
        return

    if not rows:
        await _reply(
            update,
            context,
            EMPTY_FAVORITES_TEXT,
            edit=query is not None,
            parse_mode="HTML",
            reply_markup=back_to_menu_keyboard(),
        )
        return

    items: list[dict] = []
    lines: list[str] = ["⭐ <b>Избранное</b>", ""]
    unreadable = 0
    listed = 0

    for row in rows:
        try:
            # Rows written by an earlier version may hold removed fields
            # of a marketplace that no longer exists; such rows (and only
            # such rows) are dropped, living AliExpress rows are kept.
            product = common.product_from_snapshot(json.loads(row["product_json"]))
        except Exception:
            # A stored snapshot may predate a schema change - skip it
            # instead of breaking the whole list. The identity is logged
            # from the columns that always exist (``get_favorites`` does
            # not select a row id).
            unreadable += 1
            logger.warning(
                "Skipping unreadable favorite row %s/%s",
                row.get("marketplace"),
                row.get("product_id"),
            )
            continue

        index = len(items)
        items.append({"index": index, "product": product})
        listed += 1
        if index < MAX_LISTED_FAVORITES:
            lines.append(
                f"{index + 1}. {common.escape(_clip(product.title))}"
            )
            lines.append(
                f"   {common.escape(format_marketplace_name(str(getattr(product.marketplace, 'value', product.marketplace))))} · "
                f"{common.escape(format_price(product.price, product.currency))}"
            )

    if not items:
        # Either nothing was ever saved, or every row is older than the
        # current schema. Both are honest states with different wording.
        await _reply(
            update,
            context,
            STALE_FAVORITES_TEXT if unreadable else EMPTY_FAVORITES_TEXT,
            edit=query is not None,
            parse_mode="HTML",
            reply_markup=back_to_menu_keyboard(),
        )
        return

    if listed > MAX_LISTED_FAVORITES:
        lines.append("")
        lines.append(f"…и ещё {listed - MAX_LISTED_FAVORITES}.")

    context.user_data[FAVORITES_CACHE_KEY] = items
    # Only the buttons of the listed items are rendered: an item beyond the
    # limit has no row on screen, so a button for it would be an orphan.
    await _reply(
        update,
        context,
        "\n".join(lines),
        edit=query is not None,
        parse_mode="HTML",
        reply_markup=favorites_list_keyboard(items[:MAX_LISTED_FAVORITES]),
    )


async def favorite_remove_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """``🗑 Удалить`` button in the favorites list.

    The modern callback is ``fdel:<marketplace>:<product_id>``: it names
    the product itself, so a STALE favorites screen can never delete a
    different product - the removal targets exactly the identity the
    button was rendered with, regardless of what the shared cache now
    holds. The cache is consulted only to enrich analytics (the product
    title); when it no longer holds the product the event is written
    without one instead of guessing.

    ``fdel:<index>`` (the legacy positional format of earlier releases)
    is still accepted so messages sent before the upgrade keep working;
    it resolves against the current cache and refuses to act when the
    position is out of range (the honest "stale screen" answer).
    """

    query = update.callback_query
    if query is None:
        return

    raw = query.data or ""
    parts = raw.split(":")
    if not parts or parts[0] != CB_PREFIX_FAVORITE_REMOVE:
        await query.answer("Некорректная кнопка.", show_alert=True)
        return

    items: list[dict] = context.user_data.get(FAVORITES_CACHE_KEY, [])
    product: Product | None = None
    marketplace: str | None = None
    product_id: str | None = None

    if len(parts) == 3:
        # Identity-based callback: delete THIS product, whatever the cache says.
        marketplace, product_id = parts[1], parts[2]
        if marketplace not in {item.value for item in Marketplace} or not product_id:
            await query.answer("Некорректная кнопка.", show_alert=True)
            return
        product = _find_cached_item(items, marketplace, product_id)
    elif len(parts) == 2:
        # Legacy positional callback from a message sent by an older
        # release: resolve it against the current cache exactly as before.
        try:
            index = int(parts[1])
        except ValueError:
            await query.answer("Некорректная кнопка.", show_alert=True)
            return
        item = items[index] if 0 <= index < len(items) else None
        if item is None:
            await query.answer(
                "Список устарел — откройте избранное заново.", show_alert=True
            )
            return
        product = item["product"]
        marketplace = str(getattr(product.marketplace, "value", product.marketplace))
        product_id = product.product_id
    else:
        await query.answer("Некорректная кнопка.", show_alert=True)
        return

    user_id = await common.register_user(update, context)
    if user_id is None:
        await query.answer("Ошибка базы данных.", show_alert=True)
        return

    try:
        await common.get_database(context).remove_favorite(
            user_id=user_id,
            marketplace=marketplace,
            product_id=product_id,
        )
    except Exception:
        logger.exception("Failed to remove favorite for user %s", user_id)
        await query.answer("Не удалось удалить.", show_alert=True)
        return

    await common.record_event(
        context,
        analytics.EVENT_FAVORITE_REMOVED,
        user_id=user_id,
        telegram_id=update.effective_user.id if update.effective_user else None,
        product=product,
        # The identity from the button is the fact of record; the title
        # is a bonus only when the (possibly stale) cache still has it.
        metadata=None if product else {"marketplace": marketplace, "product_id": product_id},
    )
    await query.answer("🗑 Удалено из избранного")
    context.user_data.pop(FAVORITES_CACHE_KEY, None)
    await show_favorites(update, context)


def _find_cached_item(
    items: list[dict], marketplace: str, product_id: str
) -> Product | None:
    """The cached product with this identity, or ``None``.

    Used purely for analytics enrichment: deletion itself never depends
    on the cache, so a replaced or missing cache cannot redirect it.
    """

    for item in items:
        candidate = item.get("product")
        if candidate is None:
            continue
        candidate_marketplace = str(
            getattr(candidate.marketplace, "value", candidate.marketplace)
        )
        if candidate_marketplace == marketplace and candidate.product_id == product_id:
            return candidate
    return None


def _markup_too_long(error: object) -> bool:
    """Whether Telegram refused the reply markup as a whole (oversized)."""

    return "markup is too long" in str(error).lower()


def _slim(kwargs: dict) -> dict:
    """``kwargs`` with the list keyboard replaced by the small menu keyboard.

    Used ONLY when Telegram rejected the original markup: the screen text
    (which lists the products) is still shown and the user keeps a way
    home. Callback rows are never the problem - the long URL buttons are.
    """

    slim = dict(kwargs)
    slim["reply_markup"] = back_to_menu_keyboard()
    return slim


async def _reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    edit: bool = False,
    **kwargs,
) -> None:
    """Reply to a command or edit the message a callback came from.

    An edit can legitimately be refused by Telegram: the message is a photo
    (there is no text to replace), the screen is already exactly this one,
    the text exceeds the 4096-character limit, or the reply markup is
    oversized (\"Reply markup is too long\" - the live bug where pressing
    ❤️ Избранное appeared to do NOTHING). A refusal must never reach the
    global error handler - that would show the user «Произошла внутренняя
    ошибка» on a screen that merely needs a different delivery. The screen
    is therefore retried with the small keyboard first and only then
    re-sent as a fresh message (the old one removed when possible).
    """

    query = update.callback_query
    if edit and query is not None:
        try:
            await query.edit_message_text(text, **kwargs)
            return
        except Exception as error:
            if "Message is not modified" in str(error):
                # The user is already looking at exactly this screen.
                await _answer(query)
                return
            if _markup_too_long(error):
                # The oversized keyboard was refused as a whole; the SAME
                # screen with the small menu keyboard still fits, so the
                # user stays on this message.
                try:
                    await query.edit_message_text(text, **_slim(kwargs))
                    return
                except Exception:
                    logger.debug(
                        "Slim favorites edit also failed; sending a fresh one",
                        exc_info=True,
                    )
            logger.debug(
                "Could not edit the favorites message (%s); sending a fresh one",
                type(error).__name__,
            )
        await _answer(query)
        chat_id = _chat_id(update, query)
        if chat_id is None:
            return
        # The fresh screen is sent FIRST and the previous message is only
        # removed AFTER it is really on screen. Deleting first (the old
        # behaviour) left the chat empty whenever the send was refused -
        # the interaction then looked closed with nothing to answer.
        try:
            await context.bot.send_message(chat_id=chat_id, text=text, **kwargs)
        except Exception as error:
            delivered = False
            if _markup_too_long(error):
                # Last chance: the same text with the small keyboard. The
                # user must always end up ON the favorites screen.
                try:
                    await context.bot.send_message(
                        chat_id=chat_id, text=text, **_slim(kwargs)
                    )
                    delivered = True
                except Exception:
                    logger.debug("Slim favorites send also failed", exc_info=True)
            if not delivered:
                logger.exception(
                    "Failed to send the favorites screen; keeping the old message"
                )
                return
        message = getattr(query, "message", None)
        if message is not None:
            try:
                await message.delete()
            except Exception:  # noqa: BLE001 - deletion is best-effort
                logger.debug("Could not delete the previous message", exc_info=True)
        return

    message = update.effective_message
    if message is not None:
        await message.reply_text(text, **kwargs)


async def _answer(query) -> None:
    """Acknowledge a callback press, tolerating an already-answered query."""

    try:
        await query.answer()
    except Exception:
        logger.debug("Callback query was already answered", exc_info=True)


def _chat_id(update: Update, query) -> int | None:
    """The chat a fallback message must go to, from whichever object has it."""

    message = getattr(query, "message", None)
    chat_id = getattr(message, "chat_id", None)
    if chat_id is not None:
        return chat_id
    chat = update.effective_chat
    return chat.id if chat is not None else None
