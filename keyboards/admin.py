"""Keyboards of the button-driven admin panel (spec §39, §53).

Callback scheme (all under the single ``adm:`` prefix the router matches):

* ``adm:menu`` - the panel's main screen with all 13 sections;
* ``adm:<section>`` - one section (stats/users/searches/...);
* ``adm:<section>:p<N>`` - a paginated section on page N;
* ``adm:<period>`` - switch the period of the CURRENT screen
  (``today`` / ``7`` / ``30`` / ``all``);
* ``adm:refresh`` - re-read the current screen;
* ``adm:apitest`` - run the minimal safe AliExpress smoke request.

Every screen keeps the pair "◀️ Назад (panel root) + 🏠 Главное меню", so
the admin context is never lost (§53).
"""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from keyboards.main import CB_BACK, LABEL_BACK, LABEL_HOME
from services.analytics import PERIODS

#: Callback data is ``adm:<key>`` - one prefix for the whole panel.
CB_ADMIN_PREFIX = "adm"
CB_ADMIN_REFRESH_KEY = "refresh"
CB_ADMIN_MENU_KEY = "menu"
CB_ADMIN_API_TEST_KEY = "apitest"
#: Toggle actions for the «Акции» promotional section. Distinct from the
#: section keys above so a toggle never collides with a screen name; both are
#: handled in ``handlers.admin`` and write through to the SAME ``bot_config``
#: store the ``manage_sales.py`` CLI uses.
CB_ADMIN_SALE_ENABLE_KEY = "sale_on"
CB_ADMIN_SALE_DISABLE_KEY = "sale_off"

LABEL_ADMIN_REFRESH = "\U0001F504 Обновить"
LABEL_ADMIN_PANEL = "\U0001F510 Админ-панель"

#: The 13 sections of the main screen, in the exact spec §39 order:
#: ``(callback_key, label)``.
ADMIN_SECTIONS: tuple[tuple[str, str], ...] = (
    ("stats", "\U0001F4CA Статистика"),
    ("users", "\U0001F465 Пользователи"),
    ("searches", "\U0001F50E Поиски"),
    ("favs", "❤️ Избранное"),
    ("track", "\U0001F514 Отслеживание"),
    ("products", "\U0001F6D2 Товары"),
    ("feed", "\u2728 Находки"),
    ("discounts", "\U0001F4B0 Скидки"),
    #: The reusable promotional section (Black Friday / 11.11 / …): a live,
    #: self-describing enable/disable control. The state is read from the
    #: bot's own ``bot_config`` (same store the ``manage_sales.py`` CLI uses),
    #: so the panel and the CLI never disagree and the setting survives
    #: restarts (see ``handlers.admin._screen_sales``).
    ("sales", "\U0001F3F7 Акции"),
    ("ai", "\U0001F916 AI"),
    ("api", "\U0001F4E1 AliExpress API"),
    ("db", "\U0001F5C4 База данных"),
    ("logs", "\U0001F4DD Логи"),
    ("system", "⚙️ Система"),
)

#: Sections that make sense with the period row (§40/§42/§43/§44).
PERIOD_SECTIONS = frozenset({"stats", "searches", "favs", "track"})


def _label_for(key: str) -> str:
    for section_key, label in ADMIN_SECTIONS:
        if section_key == key:
            return label
    raise KeyError(key)


def admin_main_keyboard() -> InlineKeyboardMarkup:
    """The §39 grid: 13 sections in pairs + the main-menu escape."""

    rows: list[list[InlineKeyboardButton]] = []
    pair: list[InlineKeyboardButton] = []
    for key, label in ADMIN_SECTIONS:
        pair.append(InlineKeyboardButton(label, callback_data=f"{CB_ADMIN_PREFIX}:{key}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([InlineKeyboardButton(LABEL_HOME, callback_data=CB_BACK)])
    return InlineKeyboardMarkup(rows)


def admin_period_keyboard(period: str) -> InlineKeyboardMarkup:
    """Period buttons, with the active one marked.

    The current period is marked in the label instead of being hidden, so
    the numbers on screen always say which window they describe.
    """

    row: list[InlineKeyboardButton] = []
    rows: list[list[InlineKeyboardButton]] = []

    for key, label in PERIODS:
        row.append(
            InlineKeyboardButton(
                f"• {label} •" if key == period else label,
                callback_data=f"{CB_ADMIN_PREFIX}:{key}",
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []

    if row:
        rows.append(row)

    rows.append(
        [
            InlineKeyboardButton(
                LABEL_ADMIN_REFRESH,
                callback_data=f"{CB_ADMIN_PREFIX}:{CB_ADMIN_REFRESH_KEY}",
            )
        ]
    )
    rows.append([InlineKeyboardButton(LABEL_BACK, callback_data=CB_BACK)])
    return InlineKeyboardMarkup(rows)


def admin_sales_keyboard(enabled: bool) -> InlineKeyboardMarkup:
    """The «Акции» screen: the current state is SHOWN in the toggle label, and
    the single button flips it to the opposite state. Reuses the shared
    refresh / panel-root / main-menu navigation so the admin context is never
    lost. ``enabled`` comes from the persisted ``bot_config`` value, so the
    label always reflects the real state, not just the last press.
    """

    toggle_label = "⏸ Выключить" if enabled else "▶️ Включить"
    toggle_key = CB_ADMIN_SALE_DISABLE_KEY if enabled else CB_ADMIN_SALE_ENABLE_KEY
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    toggle_label, callback_data=f"{CB_ADMIN_PREFIX}:{toggle_key}"
                )
            ],
            [
                InlineKeyboardButton(
                    LABEL_ADMIN_REFRESH,
                    callback_data=f"{CB_ADMIN_PREFIX}:{CB_ADMIN_REFRESH_KEY}",
                )
            ],
            [
                InlineKeyboardButton(
                    LABEL_ADMIN_PANEL,
                    callback_data=f"{CB_ADMIN_PREFIX}:{CB_ADMIN_MENU_KEY}",
                ),
                InlineKeyboardButton(LABEL_HOME, callback_data=CB_BACK),
            ],
        ]
    )


def admin_screen_keyboard(
    section: str,
    period: str,
    *,
    page: int = 0,
    has_next: bool = False,
) -> InlineKeyboardMarkup:
    """Navigation of one section screen: period (where useful),
    pagination (where useful), refresh, panel root, main menu."""

    rows: list[list[InlineKeyboardButton]] = []

    if section in PERIOD_SECTIONS:
        period_row: list[InlineKeyboardButton] = []
        for key, label in PERIODS:
            period_row.append(
                InlineKeyboardButton(
                    f"• {label} •" if key == period else label,
                    callback_data=f"{CB_ADMIN_PREFIX}:{key}",
                )
            )
        rows.append(period_row)

    if has_next or page > 0:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(
                InlineKeyboardButton(
                    "◀️ Назад",
                    callback_data=f"{CB_ADMIN_PREFIX}:{section}:p{page - 1}",
                )
            )
        if has_next:
            nav.append(
                InlineKeyboardButton(
                    "Вперёд ▶️",
                    callback_data=f"{CB_ADMIN_PREFIX}:{section}:p{page + 1}",
                )
            )
        if nav:
            rows.append(nav)

    rows.append(
        [
            InlineKeyboardButton(
                LABEL_ADMIN_REFRESH,
                callback_data=f"{CB_ADMIN_PREFIX}:{CB_ADMIN_REFRESH_KEY}",
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                LABEL_ADMIN_PANEL, callback_data=f"{CB_ADMIN_PREFIX}:{CB_ADMIN_MENU_KEY}"
            ),
            InlineKeyboardButton(LABEL_HOME, callback_data=CB_BACK),
        ]
    )
    return InlineKeyboardMarkup(rows)

