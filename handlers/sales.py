"""The reusable sales section (initially «Чёрная пятница»).

ONE promotional landing screen per event, reusing the same machinery as the
standalone «🎟 Промокоды» section: the administrator-configured AliExpress
affiliate destination opened by a Telegram URL button. There is deliberately
NO second search engine and NO event-specific product feed here - the current
AliExpress integration exposes no verified Black-Friday feed, no reliable
historical price and no trustworthy discount field, so the section never
invents a deal. It is useful even before the final link is configured: without
a URL the screen shows a clear, friendly "not available yet" note and points
the user at «Умный поиск» instead of a broken link.

Reusability: the future 11.11 / 12.12 / seasonal sales reuse EVERY handler and
screen in this module. A new event is one new entry in
``config.sales.SALE_EVENTS`` plus a runtime config flip (``manage_sales.py``) -
never a copied handler.

Access control (the point of a hidden section):

* the main-menu button is appended ONLY when the caller may see it - the event
  is enabled for everyone, or the caller is an administrator - so an ordinary
  user never gets a dead-end button;
* the server-side gate :func:`sale_callback` independently enforces this on
  every press: a disabled event is refused to a non-admin even when the
  callback data is forged. Hiding a button is NOT the access check;
* a refusal is deliberately vague ("section unavailable") so it never reveals
  that an administrator-only feature exists.
"""

from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, Update
from telegram.ext import ContextTypes

from config.sales import (
    SALE_EVENTS,
    SaleEvent,
    enabled_key,
    get_event,
    is_enabled_value,
    safe_http_url,
    url_key,
)
from handlers import common
from keyboards.sales import sale_menu_button, sale_screen_keyboard

logger = logging.getLogger(__name__)

#: Answer shown to anyone who presses a section that is not enabled for them.
#: Intentionally generic: it must not hint that an admin-only feature exists.
DENIED_TEXT = "Этот раздел сейчас недоступен."

#: Answer for a callback whose event id is not in the registry (forged/stale).
UNKNOWN_TEXT = "Такой акции нет. Откройте главное меню."


def _available_text(event: SaleEvent) -> str:
    """Screen text when a valid destination URL is configured."""

    return (
        f"\U0001F3F7 <b>{event.title}</b>\n\n"
        "Мы собрали специальные предложения AliExpress. "
        "Нажмите кнопку ниже, чтобы открыть подборку на AliExpress.\n\n"
        "Цены, скидки и условия покажет сама страница акции у маркетплейса."
    )


def _unavailable_text(event: SaleEvent) -> str:
    """Screen text when the destination URL is not (yet) configured.

    No link is shown, the wording stays honest (nothing is promised), and the
    user is pointed at the product search so the screen is still useful.
    """

    return (
        f"\U0001F3F7 <b>{event.title}</b>\n\n"
        "Раздел скоро откроется: мы ещё готовим подборку специальных "
        "предложений AliExpress. Загляните позже.\n\n"
        "А нужный товар можно найти прямо сейчас в разделе "
        "«Умный поиск»."
    )


def _is_admin(context: ContextTypes.DEFAULT_TYPE, telegram_id: int | None) -> bool:
    """Whether this Telegram id may see a disabled event (admin-only access).

    Reuses the existing ``ADMIN_IDS`` authorization exactly like the admin
    panel does - no new credentials, no hardcoded ids. Unconfigured settings
    (``None``) grant access to nobody.
    """

    settings = common.get_settings(context)
    if settings is None:
        return False
    return settings.is_admin(telegram_id)


async def _event_enabled(context: ContextTypes.DEFAULT_TYPE, event_id: str) -> bool:
    """Read an event's enabled flag; fail safe (disabled) on any error."""

    try:
        value = await common.get_database(context).get_config(enabled_key(event_id))
    except Exception:  # noqa: BLE001 - a missing/absent config must hide it
        logger.exception("Failed to read sales config for %s", event_id)
        return False
    return is_enabled_value(value)


async def _destination(context: ContextTypes.DEFAULT_TYPE, event_id: str) -> str | None:
    """The validated destination URL, or ``None`` when missing/invalid.

    Validation (http/https, with a host) happens here at render time, so an
    invalid stored value degrades to the "not available yet" screen instead of
    opening a broken or unsafe link. The value itself is never logged.
    """

    try:
        value = await common.get_database(context).get_config(url_key(event_id))
    except Exception:  # noqa: BLE001 - absence simply means "no destination"
        logger.exception("Failed to read sales URL for %s", event_id)
        return None
    return safe_http_url(value)


async def sale_menu_buttons(
    context: ContextTypes.DEFAULT_TYPE, telegram_id: int | None
) -> list[InlineKeyboardButton]:
    """Main-menu buttons for the events THIS caller may see.

    Returns the button for an event when it is enabled for everyone, or when
    the caller is an administrator (so a disabled event can be tested). Every
    DB read is fail-safe: on error an event is treated as disabled, so an
    ordinary user is never shown the button by accident. This produces the
    button only; the enforced check lives in :func:`sale_callback`.
    """

    admin = _is_admin(context, telegram_id)
    buttons: list[InlineKeyboardButton] = []
    for event in SALE_EVENTS.values():
        enabled = await _event_enabled(context, event.event_id)
        if enabled or admin:
            buttons.append(sale_menu_button(event))
    return buttons


async def sale_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``sale:<event_id>`` - the server-side gate plus the section screen.

    A disabled event is refused to a non-admin even if the callback data was
    forged; the refusal never reveals the admin-only feature. An enabled event
    (or an administrator testing) opens the landing screen.
    """

    query = update.callback_query
    if query is None:
        return

    event_id = (query.data or "").partition(":")[2]
    event = get_event(event_id)
    if event is None:
        await query.answer(UNKNOWN_TEXT)
        return

    enabled = await _event_enabled(context, event_id)
    admin = _is_admin(context, getattr(query.from_user, "id", None))

    if not enabled and not admin:
        logger.info("Sales section %r refused for a non-admin", event_id)
        await query.answer(DENIED_TEXT)
        return

    await query.answer()
    destination = await _destination(context, event_id)
    text = _available_text(event) if destination else _unavailable_text(event)
    await query.edit_message_text(
        text, parse_mode="HTML", reply_markup=sale_screen_keyboard(destination)
    )

