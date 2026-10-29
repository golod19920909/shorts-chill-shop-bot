"""The 🤖 AI product menu: five actions over ONE real product card.

This module is a thin Telegram layer. All reasoning lives in
``services.product_assistant`` (real fields, validated model output) and
every candidate comes from the SAME ``SearchService`` the rest of the bot
uses - there is no second search architecture, and no model can create a
product.

User-facing promises kept here:

* the menu opens on the card the user is looking at, and «◀️ Назад»
  restores that very card;
* an AI screen always shows the marketplace facts it was based on;
* when AI is unavailable, the same screen appears with a short note that
  the ordering is deterministic - never an error, never invented data.
"""

from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from handlers import common
from keyboards.main import CB_BACK, back_to_menu_keyboard
from keyboards.products import (
    CB_PREFIX_AI_ACTION,
    LABEL_CARD_BACK,
    LABEL_HOME,
    ai_menu_keyboard,
)
from models.search_request import SearchMode, SearchRequest
from services.search_service import SearchOutcome, SearchService

logger = logging.getLogger(__name__)

#: ``context.user_data`` key holding the product a typed question belongs
#: to: ``{"session": str, "index": int}``. Cleared as soon as the question
#: is answered (or cancelled), so a stray text message never becomes a
#: question by accident.
AI_QUESTION_KEY = "ai_pending_question"

AI_MENU_HINT = "🤖 AI: выберите, что сделать с этим товаром."

AI_QUESTION_PROMPT = (
    "💬 <b>Задать свой вопрос</b>\n\n"
    "Напишите вопрос об этом товаре одним сообщением — например, "
    "«какой из вариантов дешевле?» или «есть ли здесь версия с микрофоном?».\n\n"
    "Помощник отвечает только по данным AliExpress и говорит прямо, "
    "если чего-то в них нет."
)

EXPIRED_TEXT = "Результат устарел — повторите поиск."

#: Honest explanation per "nothing to show" reason, so a screen never
#: looks like a silent failure.
EMPTY_REASONS = {
    "no_comparable_price": (
        "💰 <b>Найти дешевле</b>\n\n"
        "Сравнить цены не получится: AliExpress не передал цену или валюту "
        "этого товара, а выдумывать курс или цену бот не будет."
    ),
    "no_criteria": (
        "🎯 <b>Подобрать лучше</b>\n\n"
        "Сравнивать не по чему: в данных AliExpress у этого товара нет ни "
        "отзывов, ни числа заказов. Выдумывать характеристики бот не станет."
    ),
    "no_candidates": (
        "🔎 <b>Найти похожий</b>\n\n"
        "😕 По вашему запросу ничего не найдено. "
        "Попробуйте изменить запрос или параметры поиска."
    ),
}

_GOAL_TITLES = {
    "cheaper": "💰 <b>Найти дешевле</b>",
    "similar": "🔎 <b>Найти похожий</b>",
    "better": "🎯 <b>Подобрать лучше</b>",
}


def _parse_session_index(data: str) -> tuple[str, int] | None:
    """Parse ``<prefix>:<session_id>:<index>``."""

    parts = (data or "").split(":")
    if len(parts) != 3:
        return None
    try:
        return parts[1], int(parts[2])
    except ValueError:
        return None


def _parse_action(data: str) -> tuple[str, str, int] | None:
    """Parse ``<prefix>:<action>:<session_id>:<index>``."""

    parts = (data or "").split(":")
    if len(parts) != 4:
        return None
    try:
        return parts[1], parts[2], int(parts[3])
    except ValueError:
        return None


def _result_keyboard(session_id: str, index: int) -> InlineKeyboardMarkup:
    """«◀️ Назад» to the product card + «🏠 Главное меню»."""

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    LABEL_CARD_BACK,
                    callback_data=f"pnav:{session_id}:{index}",
                )
            ],
            [InlineKeyboardButton(LABEL_HOME, callback_data=CB_BACK)],
        ]
    )



def _assistant(context: ContextTypes.DEFAULT_TYPE):
    """The shared product assistant (built once in ``build_application``)."""

    return context.application.bot_data.get("product_assistant")


def _products_for_question(
    context: ContextTypes.DEFAULT_TYPE, session_id: str, index: int, product
) -> list:
    """The asked product plus the other candidates of the same session.

    Several questions ("какой из этих вариантов дешевле?") only make sense
    over a set, so the rest of the shown session is included while it is
    still cached. The asked product always comes first.
    """

    products = [product]
    outcome = common.get_cached_outcome(context, session_id)
    if outcome is not None:
        for candidate in outcome.products[:8]:
            if candidate.product_id != product.product_id:
                products.append(candidate)
    return products[:8]


async def ai_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``🤖 AI`` on a card: replace the card keyboard with the AI menu.

    Only the keyboard changes - the caption (image, price, product text)
    stays exactly as it was, so the user keeps seeing the product the menu
    belongs to.
    """

    query = update.callback_query
    if query is None:
        return

    parsed = _parse_session_index(query.data or "")
    if parsed is None:
        await query.answer("Некорректная кнопка.", show_alert=True)
        return

    session_id, index = parsed
    product = common.get_cached_product(context, session_id, index)
    if product is None:
        await query.answer(EXPIRED_TEXT, show_alert=True)
        return

    await query.answer(AI_MENU_HINT)
    try:
        await query.edit_message_reply_markup(
            reply_markup=ai_menu_keyboard(session_id, index)
        )
    except Exception:  # noqa: BLE001 - the menu must not fail the interaction
        logger.debug("Could not switch the card to the AI menu", exc_info=True)


async def ai_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``paia:<action>:<session>:<index>``: run one AI menu action."""

    query = update.callback_query
    if query is None:
        return

    parsed = _parse_action(query.data or "")
    if parsed is None:
        await query.answer("Некорректная кнопка.", show_alert=True)
        return

    action, session_id, index = parsed
    product = common.get_cached_product(context, session_id, index)
    if product is None:
        await query.answer(EXPIRED_TEXT, show_alert=True)
        return

    assistant = _assistant(context)
    if assistant is None:  # pragma: no cover - wiring guarantees it
        await query.answer("AI-помощник недоступен.", show_alert=True)
        return

    await query.answer()
    chat_id = query.message.chat_id if query.message is not None else None
    if chat_id is None:
        return

    if action == "question":
        context.user_data[AI_QUESTION_KEY] = {"session": session_id, "index": index}
        await context.bot.send_message(
            chat_id,
            AI_QUESTION_PROMPT,
            parse_mode="HTML",
            reply_markup=_result_keyboard(session_id, index),
        )
        return

    if action == "advice":
        advice = await assistant.analyze(product)
        await context.bot.send_message(
            chat_id,
            advice.text,
            parse_mode="HTML",
            reply_markup=_result_keyboard(session_id, index),
        )
        if not advice.used_ai:
            logger.info("Advice for %s rendered deterministically", product.product_id)
        return

    await _show_candidates(
        context, chat_id, assistant, action, session_id, index, product
    )



async def _show_candidates(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    assistant,
    action: str,
    session_id: str,
    index: int,
    product,
) -> None:
    """Search real candidates, filter deterministically, optionally order by AI."""

    candidates = await assistant.candidate_products(product)
    if action == "cheaper":
        selection = await assistant.cheaper(product, candidates)
    elif action == "better":
        selection = await assistant.better(product, candidates)
    else:
        selection = await assistant.similar(product, candidates)

    if not selection.products:
        text = EMPTY_REASONS.get(selection.reason, EMPTY_REASONS["no_candidates"])
        await context.bot.send_message(
            chat_id,
            text,
            parse_mode="HTML",
            reply_markup=_result_keyboard(session_id, index),
        )
        return

    header_lines = [
        line
        for line in (
            _GOAL_TITLES.get(action, ""),
            f"По товару: <i>{common.escape(product.title[:60])}</i>",
        )
        if line
    ]

    outcome = SearchOutcome(
        request=SearchRequest(
            original_query=product.title,
            normalized_query=product.title,
            mode=SearchMode.SEARCH,
            currency=product.currency,
        ),
        products=selection.products,
        searched_marketplaces=["AliExpress"],
    )

    from handlers.search import send_search_results  # local: avoids a cycle

    await send_search_results(
        context,
        chat_id,
        outcome,
        header="\n".join(header_lines),
        wrap_up_keyboard=back_to_menu_keyboard(),
    )


async def consume_pending_question(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    """Answer a typed product question, if one is pending.

    Called from ``handlers.search.search_message`` before a text message is
    treated as a new search: a question is only captured when the user
    explicitly opened «💬 Задать свой вопрос», so ordinary messages always
    stay ordinary searches.
    """

    pending = context.user_data.pop(AI_QUESTION_KEY, None)
    if not isinstance(pending, dict):
        return False

    message = update.effective_message
    question = (message.text or "").strip() if message is not None else ""
    session_id = str(pending.get("session") or "")
    try:
        index = int(pending.get("index") or 0)
    except (TypeError, ValueError):
        index = 0

    product = common.get_cached_product(context, session_id, index)
    if product is None or not question:
        if message is not None:
            await message.reply_text(EXPIRED_TEXT, reply_markup=back_to_menu_keyboard())
        return True

    assistant = _assistant(context)
    if assistant is None:  # pragma: no cover - wiring guarantees it
        return False

    advice = await assistant.answer(
        question, _products_for_question(context, session_id, index, product)
    )
    await message.reply_text(
        advice.text,
        parse_mode="HTML",
        reply_markup=_result_keyboard(session_id, index),
    )
    return True
