"""The "🔥 Горячие скидки" screen.

The final spec fixes EXACTLY four sections, each carrying its real
verified affiliate URL (see ``config.hot_discounts``): the screen
renders four direct URL buttons plus ``◀️ Назад`` / ``🏠 Главное меню``.
No campaign data is shown or invented - only the fixed destinations.

``hot_discount_section_callback`` exists solely for stale
``hdis:<code>`` buttons on messages sent by an earlier version: ANY
such press re-renders the current four-button screen (or answers that
the code no longer exists), so an old message can never dead-end.
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from config.hot_discounts import get_section
from keyboards.hot_discounts import hot_discounts_keyboard

logger = logging.getLogger(__name__)

#: Short by design: the four destinations ARE the screen (each button opens
#: its real verified AliExpress collection), so the text only says what the
#: buttons do and who owns the content.
HOT_DISCOUNTS_TEXT = (
    "\U0001F525 <b>Горячие скидки</b>\n\n"
    "Кнопки открывают готовые подборки товаров со скидками AliExpress.\n"
    "Цены и состав подборок задаёт маркетплейс."
)


async def hot_discounts_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Render the "Горячие скидки" screen."""

    query = update.callback_query
    if query is not None:
        await query.answer()
        await query.edit_message_text(
            HOT_DISCOUNTS_TEXT,
            parse_mode="HTML",
            reply_markup=hot_discounts_keyboard(),
        )
        return

    message = update.effective_message
    if message is not None:
        await message.reply_text(
            HOT_DISCOUNTS_TEXT,
            parse_mode="HTML",
            reply_markup=hot_discounts_keyboard(),
        )


async def hot_discount_section_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """A stale ``hdis:<code>`` press: re-render the current screen.

    Sections removed since the message was sent answer honestly;
    existing sections simply land on the four-button screen where their
    URL lives as a direct button. Answered exactly once: a second
    ``answer()`` on the same callback query makes Telegram reject it.
    """

    query = update.callback_query
    if query is None:
        return

    code = (query.data or "").partition(":")[2]
    section = get_section(code)
    if section is None:
        await query.answer("Этот раздел больше не существует.", show_alert=True)
    else:
        await query.answer()

    await query.edit_message_text(
        HOT_DISCOUNTS_TEXT,
        parse_mode="HTML",
        reply_markup=hot_discounts_keyboard(),
    )
