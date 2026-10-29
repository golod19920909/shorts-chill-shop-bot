"""Analytics vocabulary: event names, their labels and time periods.

The bot records *events* (a search ran, a card was shown, a buy button was
pressed) and the admin panel aggregates them. This module holds no SQL and
no Telegram code - it only owns the names, so a typo cannot silently
create a second event type that never shows up in the statistics.

Privacy rules that the rest of the code must keep:

* no free-form user text is stored here - ``search_history`` already keeps
  the user's own queries, scoped to that user, and the analytics table
  intentionally does not duplicate them;
* the only string recorded is the product title, which comes from the
  marketplace API. The ``promotion_name`` column stays in the schema for
  historical rows written by the removed promo-campaign screens, but no
  new code writes it;
* nothing secret (token, API keys, signed URLs) may ever be an event
  value: the admin panel renders exactly what is stored.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

# -- event names -------------------------------------------------------------

EVENT_USER_STARTED = "user_started"

EVENT_SEARCH_STARTED = "search_started"
EVENT_SEARCH_SUCCESS = "search_success"
EVENT_SEARCH_FAILED = "search_failed"
EVENT_PRODUCTS_SHOWN = "products_shown"

#: One card was put on screen. ``products_shown`` counts a whole result set
#: (the search screens), ``product_shown`` counts the single card the user
#: is actually looking at - including every "➡️ Следующий" step.
EVENT_PRODUCT_SHOWN = "product_shown"

#: Buy-click events ("buy_clicked" / "product_opened" /
#: "affiliate_clicked" / "external_clicked") have no producer anymore:
#: "🛒 Купить" is a URL button now, and Telegram reports nothing about
#: URL-button presses. They stay in the vocabulary on purpose - the
#: admin panel keeps rendering historical rows, and ``get_top_products``
#: still aggregates ``affiliate_clicked``.
EVENT_BUY_CLICKED = "buy_clicked"

#: The product link was actually handed over to the user (dormant - see
#: the note above).
EVENT_PRODUCT_OPENED = "product_opened"

#: An unhandled failure reached the error handler. Only the exception class
#: name is stored - never a traceback, which could carry a token.
EVENT_ERRORS = "errors"

EVENT_FAVORITE_ADDED = "favorite_added"
EVENT_FAVORITE_REMOVED = "favorite_removed"
EVENT_TRACKING_ADDED = "tracking_added"
EVENT_TRACKING_STOPPED = "tracking_stopped"

#: A press on "🛒 Купить" for a product that really carries an affiliate
#: link (``promotion_link`` from the API).
EVENT_BUY_AFFILIATE = "affiliate_clicked"

#: The same press for a product without an affiliate link: the plain
#: marketplace URL was opened. Kept separate on purpose, so the admin
#: panel never counts a non-affiliate click as a monetized one.
EVENT_BUY_EXTERNAL = "external_clicked"

#: The comparison feature («🔍 Сравнить») was removed from the UI, so no
#: new ``compare_requested`` rows are written. It is kept ONLY as a tombstone
#: name so historical database rows keep validating against the vocabulary.
EVENT_COMPARE_REQUESTED = "compare_requested"
EVENT_IMAGE_SEARCH_REQUESTED = "image_search_requested"
EVENT_CURRENCY_CHANGED = "currency_changed"

#: A tracked product's price arrived in a different currency than the
#: stored baseline: recorded for admin diagnostics; the bot rebases the
#: baseline silently instead of sending a misleading alert.
EVENT_CURRENCY_MISMATCH = "currency_mismatch"

#: Every name the code may write. ``record_event`` values outside this
#: tuple are a programming error, not a new metric.
KNOWN_EVENTS: tuple[str, ...] = (
    EVENT_USER_STARTED,
    EVENT_SEARCH_STARTED,
    EVENT_SEARCH_SUCCESS,
    EVENT_SEARCH_FAILED,
    EVENT_PRODUCTS_SHOWN,
    EVENT_PRODUCT_SHOWN,
    EVENT_BUY_CLICKED,
    EVENT_PRODUCT_OPENED,
    EVENT_ERRORS,
    EVENT_FAVORITE_ADDED,
    EVENT_FAVORITE_REMOVED,
    EVENT_TRACKING_ADDED,
    EVENT_TRACKING_STOPPED,
    EVENT_BUY_AFFILIATE,
    EVENT_BUY_EXTERNAL,
    EVENT_COMPARE_REQUESTED,
    EVENT_IMAGE_SEARCH_REQUESTED,
    EVENT_CURRENCY_CHANGED,
    EVENT_CURRENCY_MISMATCH,
)

#: Russian labels for the admin panel. Only events listed here are shown,
#: so the panel never prints a raw internal identifier to a human.
EVENT_LABELS: dict[str, str] = {
    EVENT_USER_STARTED: "Запусков бота (/start)",
    EVENT_SEARCH_STARTED: "Поисковых запросов",
    EVENT_SEARCH_SUCCESS: "Успешных поисков",
    EVENT_SEARCH_FAILED: "Поисков без результатов",
    EVENT_PRODUCTS_SHOWN: "Показов результатов (наборов товаров)",
    EVENT_PRODUCT_SHOWN: "Показов карточек (поштучно)",
    EVENT_BUY_CLICKED: "Нажатий «Купить»",
    EVENT_PRODUCT_OPENED: "Переходов к товару (ссылка выдана)",
    EVENT_ERRORS: "Внутренних ошибок",
    EVENT_FAVORITE_ADDED: "Добавлено в избранное",
    EVENT_FAVORITE_REMOVED: "Удалено из избранного",
    EVENT_TRACKING_ADDED: "Включено отслеживание цены",
    EVENT_TRACKING_STOPPED: "Остановлено отслеживание",
    EVENT_BUY_AFFILIATE: "Клики по партнёрской ссылке",
    EVENT_BUY_EXTERNAL: "Клики по обычной ссылке",
    #: Label kept so historical ``compare_requested`` rows still render
    #: honestly in the panel; no new rows are written (feature removed).
    EVENT_COMPARE_REQUESTED: "Запросов сравнения (функция убрана)",
    EVENT_IMAGE_SEARCH_REQUESTED: "Запросов поиска по фото",
    EVENT_CURRENCY_CHANGED: "Изменений валюты",
    #: Diagnostics of the currency-mismatch protection (spec: tracking
    #: must never compare numbers across currencies).
    EVENT_CURRENCY_MISMATCH: "Конфликтов валют при отслеживании (защита)",
}

# -- periods -----------------------------------------------------------------

#: Period keys used in admin callback data and in the period keyboard, in
#: display order: ``(key, label)``.
PERIODS: tuple[tuple[str, str], ...] = (
    ("today", "Сегодня"),
    ("7", "7 дней"),
    ("30", "30 дней"),
    ("all", "Всё время"),
)

DEFAULT_PERIOD = "7"


def period_days(period: str) -> int | None:
    """Number of days a period covers, or ``None`` for "everything".

    ``None`` means "no time filter" and is a fully supported state: the
    statistics then describe the whole database.
    """

    if period == "all":
        return None
    if period == "today":
        return 1
    try:
        days = int(period)
    except (TypeError, ValueError):
        return 7
    return days if days > 0 else 7


def period_label(period: str) -> str:
    """Human label of a period key; unknown keys fall back to "7 дней"."""

    for key, label in PERIODS:
        if key == period:
            return label
    return dict(PERIODS)[DEFAULT_PERIOD]


def period_start(period: str, moment: datetime | None = None) -> str | None:
    """SQLite timestamp (UTC) a period starts at, or ``None`` for "all".

    ``created_at`` columns are written by SQLite's ``datetime('now')``,
    which is UTC, so the boundary is computed in UTC as well. "Today"
    means the current UTC day, and the panel says so instead of pretending
    to know the user's time zone.
    """

    days = period_days(period)
    if days is None:
        return None

    moment = (moment or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if period == "today":
        start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        start = moment - timedelta(days=days)

    return start.strftime("%Y-%m-%d %H:%M:%S")
