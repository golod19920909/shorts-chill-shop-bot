"""Main menu, navigation labels and shared keyboards.

Final spec: the menu is exactly eleven buttons (ten callback sections +
the Shorts Chill channel URL). Navigation keeps TWO separate concepts:

- ``◀️ Назад`` (``nav:back``) - the real previous screen, restored from
  ``context.user_data`` (see ``handlers.start.nav_back_callback``);
- ``🏠 Главное меню`` (``menu:back``) - straight to the main menu.

Legacy callbacks of removed screens (the old Random Find / deals hub)
exist only as :data:`REMOVED_CALLBACK_PATTERN` - a tombstone answers
stale buttons on already-sent messages instead of leaving a dead spinner.

Emoji are written as escape sequences so the exact layout can be
asserted in tests and never depends on editor encoding.
"""

from __future__ import annotations

from collections.abc import Sequence

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from config.catalog import CATEGORIES, CURRENCIES, CURRENCY_FLAGS, Category

# -- callback data (kept short and stable: handlers match on these) --------
CB_SEARCH = "menu:search"
CB_PHOTO = "menu:photo"
#: «✨ Находки» (G24): the AliExpress feed section. New buttons carry this
#: callback; ``CB_POPULAR`` below stays routed to the same screen because
#: already-sent menu messages still hold the old value.
CB_FEED = "menu:feed"
#: Legacy callback of the removed «🔥 Популярное» button - handled by the
#: very same ``show_feed`` handler, so old messages never dead-end.
CB_POPULAR = "menu:popular"
CB_HOT_DISCOUNTS = "menu:hot"
#: The standalone «🎟 Промокоды» section: it opens the configured
#: AliExpress partner/referral page with a URL button (see
#: ``config.promo`` and ``handlers.promo_codes``). Callback prefix
#: deliberately does NOT match :data:`REMOVED_CALLBACK_PATTERN` - that
#: tombstone belongs to the old removed deals hub, not to this section.
CB_PROMO_CODES = "menu:pcodes"
CB_CATEGORIES = "menu:categories"
CB_FAVORITES = "menu:favorites"
CB_TRACKING = "menu:tracking"
CB_CURRENCY_SCREEN = "menu:currency"
#: The settings screen opens the SAME currency screen but remembers the
#: settings screen as the target of "◀️ Назад".
CB_CURRENCY_FROM_SETTINGS = "menu:currency_settings"
CB_SETTINGS = "menu:settings"
CB_HELP = "menu:help"
#: Notifications preferences screen (§36) and the full tutorial (§37).
CB_NOTIFICATIONS = "menu:notifications"
CB_TUTORIAL = "menu:tutorial"
CB_BACK = "menu:back"

#: Restore the real previous screen (see ``handlers.start.nav_back_callback``).
CB_NAV_BACK = "nav:back"

#: Explicit «начать заново» on the exhausted «✨ Находки» screen - the only
#: way the per-user feed history is ever cleared.
CB_FEED_RESET = "feed:reset"
#: Legacy reset of the removed «🔥 Популярное» screen; still routed to the
#: same handler so old exhausted screens keep their button working.
CB_POPULAR_RESET = "pop:reset"

#: Toggle of the price-notification preference (real DB write).
CB_NOTIFY_TOGGLE = "notif:toggle"

#: Every callback the live main-menu router handles.
MAIN_MENU_CALLBACKS = (
    CB_SEARCH,
    CB_PHOTO,
    CB_FEED,
    CB_POPULAR,
    CB_HOT_DISCOUNTS,
    CB_PROMO_CODES,
    CB_CATEGORIES,
    CB_FAVORITES,
    CB_TRACKING,
    CB_CURRENCY_SCREEN,
    CB_CURRENCY_FROM_SETTINGS,
    CB_SETTINGS,
    CB_HELP,
    CB_NOTIFICATIONS,
    CB_TUTORIAL,
    CB_BACK,
)

#: Buttons of REMOVED screens that older messages still carry
#: ("menu:random", the deals-hub children "dfeed"/"promos"/"rdeal"/
#: "promo:"): routed to a tombstone which explains the removal and opens
#: the menu - never a dead end, never the removed feature itself.
REMOVED_CALLBACK_PATTERN = r"^(menu:random|menu:deals|dfeed|promos|rdeal|promo:)"

#: The real Telegram channel of the project. A URL button - Telegram
#: opens it directly, no bot round-trip.
SHORTS_CHILL_URL = "https://t.me/Shorts_Chill"

CB_CATEGORY_PREFIX = "cat"
CB_CURRENCY_PREFIX = "cur"
#: Legacy only: buttons of the removed «Промокоды» campaign list still sit
#: on old messages as ``pcamp:<index>``. Nothing builds such a button any
#: more (the section opens the configured referral URL directly), the
#: prefix only feeds the tombstone handler ``promo_legacy_callback``.
CB_PROMO_CAMPAIGN_LEGACY_PREFIX = "pcamp"
#: Legacy only: old bot messages still carry a ``next:<session>`` button.
#: Nothing builds such a button any more (the card walks the whole cached
#: result set with ``pnav``), the prefix only feeds the tombstone handler.
CB_NEXT_PREFIX = "next"
#: Onboarding/tutorial pages: ``onb:<step>`` and ``onb:done`` (§37).
CB_ONBOARDING_PREFIX = "onb"

# -- labels ----------------------------------------------------------------
#: The AI-assisted product search. The label reads «Умный поиск» to
#: communicate that users can DESCRIBE what they need in their own words
#: (budget, requirements) and use the existing AI-assisted search. The callback
#: (``menu:search``) and the whole flow are unchanged.
LABEL_SEARCH = "\U0001F9E0 Умный поиск"
LABEL_PHOTO = "\U0001F4F7 Найти по фото"
LABEL_FEED = "\u2728 Находки"
LABEL_HOT_DISCOUNTS = "\U0001F525 Горячие скидки"
LABEL_PROMO_CODES = "\U0001F39F Промокоды"
LABEL_CATEGORIES = "\U0001F4C2 Категории"
LABEL_FAVORITES = "\u2764\uFE0F Избранное"
LABEL_TRACKING = "\U0001F514 Отслеживание цен"
LABEL_CURRENCY = "\U0001F30D Валюта"
LABEL_SETTINGS = "\u2699\uFE0F Настройки"
LABEL_SHORTS_CHILL = "\U0001F4E2 Shorts Chill"
LABEL_NOTIFICATIONS = "\U0001F514 Уведомления"
LABEL_TUTORIAL = "\U0001F4D6 Как пользоваться ботом"
#: Straight to the main menu - available on every relevant screen.
LABEL_HOME = "\U0001F3E0 Главное меню"
#: The real previous screen (hot discounts, currency, product cards).
LABEL_BACK = "\u25C0\uFE0F Назад"

#: The callback sections of the menu, in display order. The URL button
#: (Shorts Chill) is appended by :func:`main_menu_keyboard`, making the
#: final ten-button menu of the spec.
MAIN_MENU_ITEMS: tuple[tuple[str, str], ...] = (
    (CB_SEARCH, LABEL_SEARCH),
    (CB_PHOTO, LABEL_PHOTO),
    (CB_FEED, LABEL_FEED),
    (CB_CATEGORIES, LABEL_CATEGORIES),
    (CB_FAVORITES, LABEL_FAVORITES),
    (CB_TRACKING, LABEL_TRACKING),
    (CB_CURRENCY_SCREEN, LABEL_CURRENCY),
    (CB_SETTINGS, LABEL_SETTINGS),
    (CB_HOT_DISCOUNTS, LABEL_HOT_DISCOUNTS),
    (CB_PROMO_CODES, LABEL_PROMO_CODES),
)

#: Rows of the main menu (two columns where the labels fit), in the exact
#: order of the spec: Умный поиск, Найти по фото, Находки,
#: Категории, Избранное, Отслеживание цен, Валюта, Настройки, Горячие скидки,
#: Промокоды. (Promotional event buttons, when enabled/admin, are appended
#: above the channel link by ``main_menu_keyboard``.)
MAIN_MENU_ROWS: tuple[tuple[str, ...], ...] = (
    (CB_SEARCH, CB_PHOTO),
    (CB_FEED, CB_CATEGORIES),
    (CB_FAVORITES, CB_TRACKING),
    (CB_CURRENCY_SCREEN, CB_SETTINGS),
    (CB_HOT_DISCOUNTS, CB_PROMO_CODES),
)


def _label_for(callback: str) -> str:
    for key, label in MAIN_MENU_ITEMS:
        if key == callback:
            return label
    raise KeyError(callback)


def main_menu_keyboard(
    sale_buttons: Sequence[InlineKeyboardButton] | None = None,
) -> InlineKeyboardMarkup:
    """The final sections plus the Shorts Chill channel link.

    ``sale_buttons`` is an optional row of promotional event buttons (e.g.
    «🏷 Чёрная пятница»). It is appended just above the channel link and is
    supplied ONLY by the async menu renderers, which compute which events THIS
    caller may see (``handlers.sales.sale_menu_buttons``). With ``None`` (the
    default, and how every existing caller and test uses it) the menu is
    exactly the accepted eleven-button layout - so a disabled event never
    adds a dead-end button for an ordinary user.
    """

    rows = [
        [InlineKeyboardButton(_label_for(cb), callback_data=cb) for cb in row]
        for row in MAIN_MENU_ROWS
    ]
    if sale_buttons:
        rows.append(list(sale_buttons))
    # A URL button: Telegram opens the real channel directly.
    rows.append(
        [InlineKeyboardButton(LABEL_SHORTS_CHILL, url=SHORTS_CHILL_URL)]
    )
    return InlineKeyboardMarkup(rows)


def home_row() -> list[InlineKeyboardButton]:
    """The shared "🏠 Главное меню" row used across secondary screens."""

    return [InlineKeyboardButton(LABEL_HOME, callback_data=CB_BACK)]


def back_and_home_rows() -> list[list[InlineKeyboardButton]]:
    """``◀️ Назад`` (real previous screen) + ``🏠 Главное меню``."""

    return [
        [InlineKeyboardButton(LABEL_BACK, callback_data=CB_NAV_BACK)],
        home_row(),
    ]


def back_to_menu_keyboard() -> InlineKeyboardMarkup:
    """Single "🏠 Главное меню" button for secondary screens."""

    return InlineKeyboardMarkup([home_row()])


def photo_retry_keyboard() -> InlineKeyboardMarkup:
    """Error screens of the photo search: try again or go home."""

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "\U0001F504 Попробовать снова", callback_data=CB_PHOTO
                )
            ],
            home_row(),
        ]
    )


def feed_exhausted_keyboard() -> InlineKeyboardMarkup:
    """The exhausted «✨ Находки» screen: explicit restart + home.

    This is the ONLY reset of the persistent per-user feed history - always
    pressed deliberately by the user, never triggered automatically.
    """

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "\U0001F504 Начать заново", callback_data=CB_FEED_RESET
                )
            ],
            home_row(),
        ]
    )


def categories_keyboard() -> InlineKeyboardMarkup:
    """Compact two-column category list plus the main menu."""

    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []

    for category in CATEGORIES:
        row.append(
            InlineKeyboardButton(
                category.title, callback_data=f"{CB_CATEGORY_PREFIX}:{category.code}"
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []

    if row:
        rows.append(row)

    rows.append(home_row())
    return InlineKeyboardMarkup(rows)


def _chunked_buttons(
    values: Sequence[tuple[str, str]], columns: int, prefix: str
) -> list[list[InlineKeyboardButton]]:
    """Build a grid of buttons from ``(callback_suffix, label)`` pairs."""

    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []

    for suffix, label in values:
        row.append(InlineKeyboardButton(label, callback_data=f"{prefix}:{suffix}"))
        if len(row) == columns:
            rows.append(row)
            row = []

    if row:
        rows.append(row)
    return rows


def currencies_keyboard() -> InlineKeyboardMarkup:
    """Flag grid of the supported currencies + back/home navigation.

    ONE screen (no intermediate "change" step): the buttons follow the
    spec's display order via ``CURRENCIES``/``CURRENCY_FLAGS``. Clearing
    the preference has no visible button any more; the ``cur:clear``
    callback is still handled for buttons sent by earlier versions.
    """

    values = [
        (code, f"{CURRENCY_FLAGS.get(code, '')} {code}".strip())
        for code in CURRENCIES
    ]
    rows = _chunked_buttons(values, 2, CB_CURRENCY_PREFIX)
    rows.extend(back_and_home_rows())
    return InlineKeyboardMarkup(rows)


def currency_screen_keyboard() -> InlineKeyboardMarkup:
    """The currency screen (alias for existing importers/tests)."""

    return currencies_keyboard()


def settings_keyboard() -> InlineKeyboardMarkup:
    """Settings screen exactly per spec §35.

    Currency / Notifications / Tutorial, then back + main menu. Tracking
    and Favorites are top-level menu sections and are not duplicated
    here; the Shorts Chill channel link lives in the main menu.
    """

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    LABEL_CURRENCY, callback_data=CB_CURRENCY_FROM_SETTINGS
                )
            ],
            [
                InlineKeyboardButton(
                    LABEL_NOTIFICATIONS, callback_data=CB_NOTIFICATIONS
                )
            ],
            [InlineKeyboardButton(LABEL_TUTORIAL, callback_data=CB_TUTORIAL)],
            [InlineKeyboardButton(LABEL_BACK, callback_data=CB_NAV_BACK)],
            home_row(),
        ]
    )


def notifications_keyboard(enabled: bool) -> InlineKeyboardMarkup:
    """Toggle for the real price-notification preference (§36)."""

    label = (
        "\U0001F515 Выключить уведомления о цене"
        if enabled
        else "\U0001F514 Включить уведомления о цене"
    )
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(label, callback_data=CB_NOTIFY_TOGGLE)],
            [InlineKeyboardButton(LABEL_BACK, callback_data=CB_NAV_BACK)],
            home_row(),
        ]
    )


def onboarding_keyboard(step: int, total: int) -> InlineKeyboardMarkup:
    """Далей through the onboarding screens; the last one finishes.

    The final button both marks the user as onboarded (so later
    ``/start`` goes straight to the menu) and opens the menu.
    """

    if step + 1 < total:
        button = InlineKeyboardButton(
            "\U0001F449 Далее", callback_data=f"{CB_ONBOARDING_PREFIX}:{step + 1}"
        )
    else:
        button = InlineKeyboardButton(
            "\U0001F680 Приступить", callback_data=f"{CB_ONBOARDING_PREFIX}:done"
        )
    return InlineKeyboardMarkup([[button]])


def category_by_callback(data: str) -> Category | None:
    """Resolve a ``cat:<code>`` callback into a category."""

    prefix, _, code = data.partition(":")
    if prefix != CB_CATEGORY_PREFIX:
        return None

    from config.catalog import get_category

    return get_category(code)
