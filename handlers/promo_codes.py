"""The standalone «🎟 Промокоды» section (main-menu button).

ONE screen, ONE destination: the configured AliExpress partner/referral page
(``config.promo.PROMO_REFERRAL_URL``) opened by a Telegram URL button. No
campaign list, no campaign id, no intermediate screen, no rebuilt URL.

Why there is nothing else here:

* the AliExpress API access this application has does not document a
  coupon-code list at all - a code, its amount, its minimum order and its
  expiry are therefore never invented, and a DISCOUNT PERCENTAGE is never
  turned into a code;
* the marketplace's own promotion campaign records (campaign ids and names)
  are internal plumbing and are not shown to a user;
* the configured referral page is the single controlled destination for promo
  offers, so it is passed to Telegram verbatim.

Product-level promo data is untouched by this: when AliExpress really attaches
``promo_code_info`` to a product, the product card renders that code exactly
as before (``services.marketplaces.aliexpress.extract_promo_info``).
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from keyboards.promo_codes import promo_codes_keyboard
from utils.emoji import TICKET

logger = logging.getLogger(__name__)

#: Short by design: the section has one destination (the configured
#: AliExpress promo page) and one button, so the text only names it.
PROMO_TEXT = (
    f"{TICKET} <b>Промокоды AliExpress</b>\n\n"
    "Здесь можно найти актуальные промокоды и скидки AliExpress."
)

#: Answer for a button of the removed campaign list, pressed on a message
#: sent by an earlier version of the bot.
LEGACY_CAMPAIGN_ANSWER = (
    "Список кампаний больше не показывается — промокоды AliExpress "
    "открываются кнопкой ниже."
)


async def show_promo_codes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Render the section: one text screen and the configured URL button."""

    query = update.callback_query
    if query is not None:
        await query.answer()
        await query.edit_message_text(
            PROMO_TEXT, parse_mode="HTML", reply_markup=promo_codes_keyboard()
        )
        return

    message = update.effective_message
    if message is not None:
        await message.reply_text(
            PROMO_TEXT, parse_mode="HTML", reply_markup=promo_codes_keyboard()
        )


async def promo_legacy_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """A stale ``pcamp:<index>`` press: re-render the current section.

    Old messages may still carry campaign buttons. Such a press must not be a
    dead end, and it must not resurrect the removed list either: the press is
    answered honestly and the message becomes the current screen with the
    configured URL button. Answered exactly once - a second ``answer()`` on
    the same callback query makes Telegram reject it.
    """

    query = update.callback_query
    if query is None:
        return

    await query.answer(LEGACY_CAMPAIGN_ANSWER, show_alert=True)
    await query.edit_message_text(
        PROMO_TEXT, parse_mode="HTML", reply_markup=promo_codes_keyboard()
    )
