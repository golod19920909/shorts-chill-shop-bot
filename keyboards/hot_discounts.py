"""Keyboards of the "🔥 Горячие скидки" screen.

The final spec fixes exactly four sections, each with its real verified
affiliate URL, so every section button is a direct URL button - no
callback, no placeholder flow. Navigation is the shared pair
``◀️ Назад`` (real previous screen) + ``🏠 Главное меню``.
"""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from config.hot_discounts import HOT_DISCOUNT_SECTIONS
from keyboards.main import back_and_home_rows

#: Kept ONLY so stale ``hdis:<code>`` buttons pressed on messages sent by
#: an earlier version still reach ``hot_discount_section_callback``.
CB_HOT_SECTION_PREFIX = "hdis"


def hot_discounts_keyboard() -> InlineKeyboardMarkup:
    """Four direct affiliate URL buttons + back/home navigation."""

    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(section.title, url=section.url)]
        for section in HOT_DISCOUNT_SECTIONS
    ]
    rows.extend(back_and_home_rows())
    return InlineKeyboardMarkup(rows)