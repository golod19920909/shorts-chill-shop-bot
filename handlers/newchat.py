"""The ``/new`` command: start a fresh shopping conversation.

A shopping assistant must give the user an explicit way to drop the current
context - the product they were looking at, the accumulated constraints and any
pending clarifying question - without guessing. ``/new`` is that reset. It only
clears the per-user in-memory conversation state (the SAME keys the search
handler owns); it does not touch the database, another user's state, or the
search pipeline, and it costs no API call.
"""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from handlers import common
from handlers.search import reset_conversation
from keyboards.main import back_to_menu_keyboard

RESET_TEXT = (
    "🧹 Начинаем новый диалог или подбор. Напишите, что хотите обсудить или найти — "
    "например: «беспроводные наушники для пробежек до 3000»."
)


async def new_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Forget the current shopping context and invite a fresh request."""

    message = update.effective_message
    if message is None:
        return

    common.clear_pending_product_question(context)
    reset_conversation(context)
    await message.reply_text(RESET_TEXT, reply_markup=back_to_menu_keyboard())
