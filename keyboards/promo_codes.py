"""Keyboard of the standalone «🎟 Промокоды» section.

The section has exactly ONE destination: the configured AliExpress
partner/referral page (``config.promo.PROMO_REFERRAL_URL``). It is rendered
as a Telegram URL button, so one press opens that exact page in the browser:
no callback, no intermediate screen, no rebuilt link.

Navigation is the shared pair ``◀️ Назад`` (real previous screen) +
``🏠 Главное меню``.
"""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from config.promo import PROMO_REFERRAL_URL
from keyboards.main import back_and_home_rows

#: Label of the URL button. The destination itself is never shown as text:
#: the button carries one exact, unmodified URL.
LABEL_PROMO_OPEN = "\U0001F39F Открыть промокоды AliExpress"


def promo_codes_keyboard() -> InlineKeyboardMarkup:
    """The configured referral URL button + back/home navigation."""

    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(LABEL_PROMO_OPEN, url=PROMO_REFERRAL_URL)]
    ]
    rows.extend(back_and_home_rows())
    return InlineKeyboardMarkup(rows)
