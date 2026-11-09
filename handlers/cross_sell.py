"""Consent-based complementary-product follow-up.

The user explicitly opts in after requesting an affiliate link. The next
free-text message starts a fresh ordinary search; it never injects products
or silently reuses the previous product's conversational constraints.
"""
from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

PENDING_OFFER_KEY = "cross_sell_offer"
PENDING_SEARCH_KEY = "cross_sell_search_pending"
CB_PREFIX = "xsell"


def remember_offer(context, product) -> None:
    title = " ".join((getattr(product, "title", "") or "").split())[:180]
    context.user_data[PENDING_OFFER_KEY] = {"title": title}


def clear_offer(context) -> None:
    context.user_data.pop(PENDING_OFFER_KEY, None)
    context.user_data.pop(PENDING_SEARCH_KEY, None)


async def offer_after_link(message, context, product) -> None:
    """Ask once, only after the user explicitly requested an affiliate link."""
    remember_offer(context, product)
    title = " ".join((getattr(product, "title", "") or "").split())[:100]
    label = f" к товару «{title}»" if title else ""
    await message.reply_text(
        f"Хотите подобрать полезные аксессуары или дополнения{label}?",
        reply_markup=InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("Да, подобрать", callback_data=f"{CB_PREFIX}:yes"),
                InlineKeyboardButton("Нет, спасибо", callback_data=f"{CB_PREFIX}:no"),
            ]]
        ),
    )


async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    action = (query.data or "").split(":", 1)
    if len(action) != 2 or action[0] != CB_PREFIX or action[1] not in {"yes", "no"}:
        await query.answer("Некорректная кнопка.", show_alert=True)
        return
    pending = context.user_data.get(PENDING_OFFER_KEY)
    if not isinstance(pending, dict):
        await query.answer("Предложение уже устарело.", show_alert=True)
        return
    if action[1] == "no":
        clear_offer(context)
        await query.answer("Хорошо")
        message = getattr(query, "message", None)
        if message is not None:
            await message.reply_text("Хорошо, продолжаем без дополнительных подборок.")
        return
    context.user_data.pop(PENDING_OFFER_KEY, None)
    context.user_data[PENDING_SEARCH_KEY] = True
    await query.answer("Продолжим")
    message = getattr(query, "message", None)
    if message is not None:
        await message.reply_text(
            "Напишите, какие дополнительные товары или аксессуары вас интересуют. "
            "Я подберу подходящие варианты."
        )


def consume_search_request(context) -> bool:
    """Consume the one-shot opt-in marker when the user sends a search phrase."""
    if not context.user_data.pop(PENDING_SEARCH_KEY, False):
        return False
    context.user_data.pop(PENDING_OFFER_KEY, None)
    return True
