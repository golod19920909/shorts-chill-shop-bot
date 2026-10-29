"""Keyboards of the reusable sales section (initially «Чёрная пятница»).

The section is one promotional landing screen: the administrator-configured
AliExpress affiliate destination opened by a Telegram URL button, exactly like
the standalone «🎟 Промокоды» section - no campaign list, no second search
engine, no event-specific API dependency (see ``handlers/sales.py``).

Two keyboards live here:

* :func:`sale_menu_buttons` - the optional MAIN-MENU button. It is built ONLY
  when the caller may see the section (the event is enabled for everyone, or
  the caller is an administrator testing a disabled event), so an ordinary
  user never gets a dead-end button. Hiding a button is not the access check:
  the server-side gate in the handler is;
* :func:`sale_screen_keyboard` - the section screen itself (destination URL
  button when configured) plus the shared back/home navigation.
"""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from config.sales import SaleEvent
from keyboards.main import back_and_home_rows

#: Callback prefix of the section: ``sale:<event_id>``. Deliberately distinct
#: from the removed deals-hub plumbing (``promo:``/``promos``) and from
#: ``menu:`` so it never collides with the tombstone or the main-menu router.
CB_SALE_PREFIX = "sale"

#: Emoji prefix of the menu button / screen title.
SALE_EMOJI = "\U0001F3F7"  # 🏷

#: Label of the destination URL button on the section screen.
LABEL_SALE_OPEN = "\U0001F6D2 Открыть на AliExpress"  # 🛒


def sale_callback(event_id: str) -> str:
    """The ``sale:<event_id>`` callback data for one event's button."""

    return f"{CB_SALE_PREFIX}:{event_id}"


def sale_menu_button(event: SaleEvent) -> InlineKeyboardButton:
    """The single main-menu button for one sales event."""

    return InlineKeyboardButton(
        f"{SALE_EMOJI} {event.title}", callback_data=sale_callback(event.event_id)
    )


def sale_screen_keyboard(destination_url: str | None) -> InlineKeyboardMarkup:
    """The section screen: destination URL button (only when a valid URL is
    configured) + the shared back/home navigation.

    A missing/invalid URL simply means no destination button is rendered; the
    screen text then explains the section is not available yet, so the user
    never sees a broken link.
    """

    rows: list[list[InlineKeyboardButton]] = []
    if destination_url:
        rows.append([InlineKeyboardButton(LABEL_SALE_OPEN, url=destination_url)])
    rows.extend(back_and_home_rows())
    return InlineKeyboardMarkup(rows)
