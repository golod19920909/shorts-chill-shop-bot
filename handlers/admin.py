"""Admin panel: button-driven diagnostics over real data (§39-53).

Access rules (the whole point of the panel being gated):

* only Telegram ids listed in ``ADMIN_IDS`` may see anything;
* an unconfigured ``ADMIN_IDS`` grants access to nobody, and the panel
  says that instead of showing data;
* a denied user gets a plain refusal - no counters, no hints about what
  is stored, and the attempt is logged (not counted as an event, so the
  statistics cannot be influenced from outside).

The panel is a grid of 13 sections (§39); every section renders numbers
read from the database or from live service diagnostics. What it never
renders: the bot token, API keys, signed URLs, authorization headers or
free-form user text beyond the admin-only top queries of §42.
"""

from __future__ import annotations

import logging
import platform
import sys
from typing import Any

from telegram import Update
from telegram.ext import ContextTypes

from config.hot_discounts import HOT_DISCOUNT_SECTIONS
from handlers import common
from keyboards.admin import (
    ADMIN_SECTIONS,
    CB_ADMIN_API_TEST_KEY,
    CB_ADMIN_MENU_KEY,
    CB_ADMIN_PREFIX,
    CB_ADMIN_REFRESH_KEY,
    CB_ADMIN_SALE_DISABLE_KEY,
    CB_ADMIN_SALE_ENABLE_KEY,
    admin_main_keyboard,
    admin_period_keyboard,
    admin_sales_keyboard,
    admin_screen_keyboard,
)
from config.sales import DEFAULT_EVENT_ID, SALE_EVENTS, enabled_key, get_event, is_enabled_value
from services import analytics
from utils.logging import get_recent_logs

logger = logging.getLogger(__name__)

#: key in ``context.user_data`` remembering the selected period
ADMIN_PERIOD_KEY = "admin_period"
#: key remembering the section that period/refresh buttons re-read
ADMIN_SCREEN_KEY = "admin_screen"
#: key remembering the page of a paginated section
ADMIN_PAGE_KEY = "admin_page"
#: key holding the result of the last safe API smoke test (§49)
ADMIN_API_TEST_KEY = "admin_api_test"

NO_ADMINS_TEXT = (
    "⛔️ <b>Админ-панель недоступна</b>\n\n"
    "В настройках бота не указан ни один администратор (<code>ADMIN_IDS</code>).\n"
    "Пока список пуст, панель не показывает данные никому."
)

DENIED_TEXT = "⛔️ Раздел доступен только администратору бота."

ERROR_TEXT = "⚠️ Не удалось прочитать статистику. Попробуйте позже."

#: How many rows of a top list the panel shows.
TOP_LIMIT = 5
#: Pagination sizes for the users and logs sections.
USERS_PER_PAGE = 8
LOGS_PER_PAGE = 8

#: Section keys that understand the period row (§40/§42/§43/§44).
PERIOD_SECTIONS = frozenset({"stats", "searches", "favs", "track"})

_SECTION_LABELS = dict(ADMIN_SECTIONS)

MENU_TEXT = (
    "\U0001F510 <b>Админ-панель</b>\n\n"
    "Выберите раздел. Все числа читаются из базы и живых диагностика "
    "сервисов в реальном времени; ключи и секреты не отображаются."
)

#: The live capability audit (§5): only methods this application really
#: verified against the API are listed as available. Extending this
#: table without a live check would be exactly the kind of claim the
#: spec forbids.
API_CAPABILITIES: tuple[str, ...] = (
    "product.query — подтверждён (живые вызовы поиска)",
    "productdetail.get — подтверждён (карточки товара, отслеживание)",
    "hotproduct.query — недоступен (InsufficientPermission у ключа); "
    "«✨ Находки» читает широкую выдачу product.query — бот не выполняет "
    "собственное ранжирование",
    "image.search — недоступен (нужен уровень app_signature); "
    "поиск по фото работает через Gemini Vision → обычный текстовый поиск",
    "link.generate — не используется (нет tracking_id); affiliate-ссылки "
    "берутся из задокументированных полей ответа product.query/productdetail.get",
)


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/admin`` - open the panel's main screen (§39)."""

    if not await _guard(update, context, answer_callback=False):
        return
    context.user_data[ADMIN_SCREEN_KEY] = CB_ADMIN_MENU_KEY
    await _edit_or_reply(
        update,
        context,
        MENU_TEXT,
        parse_mode="HTML",
        reply_markup=admin_main_keyboard(),
    )


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Router for every ``adm:`` button: menu / section / period / refresh."""

    query = update.callback_query
    if query is None:
        return

    if not _may_use(update, context):
        await query.answer(DENIED_TEXT, show_alert=True)
        return

    if not await _guard(update, context, answer_callback=True):
        return

    key = (query.data or "").partition(":")[2]

    period = context.user_data.get(ADMIN_PERIOD_KEY, analytics.DEFAULT_PERIOD)
    screen = context.user_data.get(ADMIN_SCREEN_KEY, CB_ADMIN_MENU_KEY)
    page = context.user_data.get(ADMIN_PAGE_KEY, 0)

    valid_periods = {pkey for pkey, _ in analytics.PERIODS}
    if key in valid_periods:
        # Period buttons live on period-aware sections; from the menu
        # they jump to the statistics screen (the historical default).
        period = key
        if screen not in PERIOD_SECTIONS:
            screen, page = "stats", 0
    elif key == CB_ADMIN_REFRESH_KEY or not key:
        pass  # re-read the current screen as-is
    elif key == CB_ADMIN_MENU_KEY:
        screen, page = CB_ADMIN_MENU_KEY, 0
    elif key == CB_ADMIN_API_TEST_KEY:
        screen, page = "api", 0
        context.user_data[ADMIN_API_TEST_KEY] = await _run_api_test(context)
    elif key in (CB_ADMIN_SALE_ENABLE_KEY, CB_ADMIN_SALE_DISABLE_KEY):
        # The «Акции» toggle: flip the persisted state and re-render the screen
        # from the DB, so the label always reflects the stored value (never the
        # last press). A write failure is logged and leaves the state unchanged
        # - the panel re-reads what is actually stored.
        target = key == CB_ADMIN_SALE_ENABLE_KEY
        await _set_sale_enabled(context, DEFAULT_EVENT_ID, target)
        screen, page = "sales", 0
    else:
        section, _, tail = key.partition(":")
        if section in _SECTION_LABELS:
            screen = section
            if tail.startswith("p") and tail[1:].isdigit():
                page = max(0, int(tail[1:]))
            else:
                page = 0
        else:
            # Unknown/stale button: never a dead end - back to the grid.
            logger.debug("Unknown admin callback key %r", key)
            screen, page = CB_ADMIN_MENU_KEY, 0

    context.user_data[ADMIN_PERIOD_KEY] = period
    context.user_data[ADMIN_SCREEN_KEY] = screen
    context.user_data[ADMIN_PAGE_KEY] = page

    await query.answer()
    await _render_screen(update, context, screen, period, page)


def _may_use(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Access check that also reports the reason for a refusal."""

    settings = common.get_settings(context)
    if settings is None or not settings.has_admins:
        return False

    allowed = common.is_admin(update, context)
    if not allowed:
        user = update.effective_user
        logger.warning(
            "Admin panel access denied for telegram id %s",
            user.id if user is not None else "unknown",
        )
    return allowed


async def _guard(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, answer_callback: bool
) -> bool:
    """Shared access gate for the command and the screens themselves."""

    settings = common.get_settings(context)
    if settings is None or not settings.has_admins:
        if not answer_callback:
            await _edit_or_reply(
                update, context, NO_ADMINS_TEXT, parse_mode="HTML", reply_markup=None
            )
        return False

    if not common.is_admin(update, context):
        user = update.effective_user
        logger.warning(
            "Admin panel access denied for telegram id %s",
            user.id if user is not None else "unknown",
        )
        if not answer_callback:
            await _edit_or_reply(
                update, context, DENIED_TEXT, parse_mode="HTML", reply_markup=None
            )
        return False
    return True


async def _render_screen(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    screen: str,
    period: str,
    page: int,
) -> None:
    """Dispatch one section screen; failures render an honest error."""

    builders: dict[str, Any] = {
        "menu": _screen_menu,
        "stats": _screen_stats,
        "users": _screen_users,
        "searches": _screen_searches,
        "favs": _screen_favorites,
        "track": _screen_tracking,
        "products": _screen_products,
        "feed": _screen_feed,
        # Legacy section key of the removed «🔥 Популярное» screen: an
        # already-sent admin message may still press it - same content now.
        "popular": _screen_feed,
        "discounts": _screen_discounts,
        "sales": _screen_sales,
        "ai": _screen_ai,
        "api": _screen_api,
        "db": _screen_database,
        "logs": _screen_logs,
        "system": _screen_system,
    }
    builder = builders.get(screen, _screen_menu)
    try:
        text, keyboard = await builder(context, period, page)
    except Exception:
        logger.exception("Admin screen %r failed", screen)
        text = ERROR_TEXT
        keyboard = (
            admin_main_keyboard()
            if screen == "menu"
            else admin_screen_keyboard(screen, period, page=page)
        )
    await _edit_or_reply(update, context, text, parse_mode="HTML", reply_markup=keyboard)


async def _screen_menu(
    context: ContextTypes.DEFAULT_TYPE, period: str, page: int
) -> tuple[str, Any]:
    """§39: the grid of 13 sections."""

    return MENU_TEXT, admin_main_keyboard()


async def _screen_stats(
    context: ContextTypes.DEFAULT_TYPE, period: str, page: int
) -> tuple[str, Any]:
    """§40: real aggregate metrics with period filters."""

    settings = common.get_settings(context)
    database = common.get_database(context)
    since = analytics.period_start(period)
    capabilities = _capabilities(context, settings)

    event_counts = await database.get_event_counts(since)
    event_users = await database.get_event_user_counts(since)
    total_users = await database.count_users()
    new_users = await database.count_users(since)
    top_products = await database.get_top_products(
        analytics.EVENT_BUY_AFFILIATE, since=since, limit=TOP_LIMIT
    )
    daily = await database.get_daily_event_counts(days=7)

    text = _build_text(
        period=period,
        since=since,
        event_counts=event_counts,
        event_users=event_users,
        total_users=total_users,
        new_users=new_users,
        top_products=top_products,
        daily=daily,
        admins_configured=len(settings.admin_ids),
        capabilities=capabilities,
    )
    return text, admin_screen_keyboard("stats", period)


async def _screen_users(
    context: ContextTypes.DEFAULT_TYPE, period: str, page: int
) -> tuple[str, Any]:
    """§41: user aggregates + a paginated operational list (no PII)."""

    database = common.get_database(context)
    since = analytics.period_start(period)

    rows, total = await database.list_users_page(page=page, per_page=USERS_PER_PAGE)
    active = await database.count_users_active(since)
    onboarded = await database.count_onboarded_users()
    currencies = await database.currency_distribution()
    fav_count = await database.count_favorites()
    tracked = await database.count_tracked_active()

    has_next = (page + 1) * USERS_PER_PAGE < total
    pages = max(1, (total + USERS_PER_PAGE - 1) // USERS_PER_PAGE)

    lines = [
        "\U0001F465 <b>Пользователи</b>",
        f"Период: {common.escape(analytics.period_label(period))}",
        "",
        f"• Всего: <b>{total}</b>",
        f"• Активных за период: <b>{active}</b>",
        f"• Онбординг пройден: <b>{onboarded}</b>",
        f"• Записей в избранном (всего): {fav_count}",
        f"• Отслеживается цен: {tracked}",
        "",
        "<b>Валюты:</b>",
    ]
    if currencies:
        for row in currencies:
            lines.append(
                f"• {common.escape(str(row['currency']))}: {row['total']}"
            )
    else:
        lines.append("• нет данных")

    lines.extend(
        [
            "",
            f"Страница {page + 1}/{pages}",
            "<b>Последние пользователи</b> (без имён — только операционные данные):",
        ]
    )
    if not rows:
        lines.append("• нет данных")
    for row in rows:
        onboarded_mark = "✓" if row.get("onboarded") else "—"
        lines.append(
            f"• id={row['id']} · валюта: "
            f"{common.escape(str(row.get('currency') or '—'))} · "
            f"онбординг: {onboarded_mark} · "
            f"активность: {common.escape(str(row.get('updated_at') or '—'))}"
        )

    return "\n".join(lines), admin_screen_keyboard(
        "users", period, page=page, has_next=has_next
    )



async def _screen_searches(
    context: ContextTypes.DEFAULT_TYPE, period: str, page: int
) -> tuple[str, Any]:
    """§42: real search analytics from history + events."""

    database = common.get_database(context)
    since = analytics.period_start(period)

    total = await database.count_search_history(since)
    top = await database.top_search_queries(since=since, limit=TOP_LIMIT)
    modes = await database.search_mode_distribution(since=since)
    event_counts = await database.get_event_counts(since)

    lines = [
        "\U0001F50E <b>Поиски</b>",
        f"Период: {common.escape(analytics.period_label(period))}",
        "",
        f"• Всего запросов: <b>{total}</b>",
        f"• Успешных (были товары): "
        f"<b>{event_counts.get(analytics.EVENT_SEARCH_SUCCESS, 0)}</b>",
        f"• Без результатов: "
        f"<b>{event_counts.get(analytics.EVENT_SEARCH_FAILED, 0)}</b>",
        f"• Поисков по фото: "
        f"<b>{event_counts.get(analytics.EVENT_IMAGE_SEARCH_REQUESTED, 0)}</b>",
        "",
        "<b>Режимы запросов</b> (по последним сохранённым):",
    ]
    if modes:
        for mode, count in sorted(modes.items(), key=lambda kv: -kv[1]):
            lines.append(f"• {common.escape(mode)}: {count}")
    else:
        lines.append("• нет данных")

    lines.extend(["", "<b>Популярные запросы</b> (админ-доступ, §42):"])
    if top:
        for row in top:
            lines.append(
                f"• {common.escape(str(row['query']))} — {row['total']}"
            )
    else:
        lines.append("• нет данных")

    return "\n".join(lines), admin_screen_keyboard("searches", period)


async def _screen_favorites(
    context: ContextTypes.DEFAULT_TYPE, period: str, page: int
) -> tuple[str, Any]:
    """§43: aggregate favorites; only AliExpress is ever named."""

    database = common.get_database(context)
    since = analytics.period_start(period)

    total = await database.count_favorites(since)
    unique = await database.count_favorites_unique()
    distribution = await database.favorite_marketplace_distribution()
    top = await database.top_favorites(limit=TOP_LIMIT)

    aliexpress_rows = sum(
        int(row["total"])
        for row in distribution
        if str(row.get("marketplace", "")).lower() == "aliexpress"
    )
    # Rows of removed marketplaces are counted but never named (§43).
    other_rows = sum(
        int(row["total"])
        for row in distribution
        if str(row.get("marketplace", "")).lower() != "aliexpress"
    )

    lines = [
        "❤️ <b>Избранное</b>",
        f"Период: {common.escape(analytics.period_label(period))}",
        "",
        f"• Записей за период: <b>{total}</b>",
        f"• Уникальных товаров: <b>{unique}</b>",
        f"• AliExpress: {aliexpress_rows}",
        f"• Легаси-записи (маркетплейс скрыт): {other_rows}",
        "",
        "<b>Чаще всего добавляемые:</b>",
    ]
    if top:
        for row in top:
            title = row.get("title") or "(название не сохранено)"
            lines.append(f"• {common.escape(str(title))} — {row['total']}")
    else:
        lines.append("• нет данных")

    return "\n".join(lines), admin_screen_keyboard("favs", period)



async def _screen_tracking(
    context: ContextTypes.DEFAULT_TYPE, period: str, page: int
) -> tuple[str, Any]:
    """§44: scheduler/tracking diagnostics incl. currency mismatches."""

    database = common.get_database(context)
    settings = common.get_settings(context)
    tracker = context.application.bot_data.get("price_tracker")
    since = analytics.period_start(period)

    active = await database.count_tracked_active()
    mismatches = await database.count_events(
        analytics.EVENT_CURRENCY_MISMATCH, since=since
    )
    added = await database.count_events(analytics.EVENT_TRACKING_ADDED, since=since)
    stopped = await database.count_events(
        analytics.EVENT_TRACKING_STOPPED, since=since
    )
    interval = int(getattr(settings, "tracking_interval_minutes", 1440) or 0)

    if tracker is not None and interval > 0:
        scheduler = f"\U0001F7E2 активен — каждые {interval} мин"
    else:
        scheduler = "⏸ выключен (интервал 0 или провайдер не настроен)"

    lines = [
        "\U0001F514 <b>Отслеживание цен</b>",
        "",
        f"• Планировщик: {scheduler}",
        f"• Активных позиций: <b>{active}</b>",
        f"• Включено отслеживания за период: {added}",
        f"• Остановлено за период: {stopped}",
        "",
        "<b>Диагностика циклов</b> (с момента запуска процесса):",
    ]

    if tracker is not None:
        lines.extend(
            [
                "• Последний запуск: "
                + common.escape(
                    str(getattr(tracker, "last_run_at", None) or "ещё не запускался")
                )
                + " UTC",
                f"• Циклов выполнено: {getattr(tracker, 'runs', 0)}",
                f"• Проверено строк: {getattr(tracker, 'rows_checked', 0)}",
                f"• Обнаружено изменений: {getattr(tracker, 'changes_found', 0)}",
                f"• Уведомлений отправлено: {getattr(tracker, 'notified', 0)}",
                "• Пропущено (настройка пользователя): "
                f"{getattr(tracker, 'notify_skipped', 0)}",
                f"• Ошибок строк: {getattr(tracker, 'row_errors', 0)}",
            ]
        )
    else:
        lines.append("• трекер не сконфигурирован")

    lines.extend(
        [
            "",
            "<b>Конфликты валют (защита §29)</b>:",
            f"• За период: <b>{mismatches}</b> — такие наблюдения не "
            "сравниваются и не шлют уведомления; базлайн переустанавливается.",
        ]
    )

    return "\n".join(lines), admin_screen_keyboard("track", period)



async def _screen_products(
    context: ContextTypes.DEFAULT_TYPE, period: str, page: int
) -> tuple[str, Any]:
    """§45: product snapshot diagnostics (AliExpress only, by name)."""

    database = common.get_database(context)
    diag = await database.product_diagnostics()
    counts = await database.table_row_counts()

    lines = [
        "\U0001F6D2 <b>Товары</b>",
        "",
        f"• Записей-снапшотов: <b>{diag['rows']}</b>",
        f"• Уникальных ID: <b>{diag['unique']}</b>",
        f"• AliExpress: {diag['marketplaces'].get('aliexpress', 0)}",
        f"• Легаси (маркетплейс скрыт): {diag['legacy_hidden']}",
        f"• Без product_url: {diag['missing_product_url']}",
        f"• Без affiliate_url: {diag['missing_affiliate_url']}",
        f"• Повреждённых JSON: {diag['malformed']}",
        "",
        "<b>Хранилища:</b>",
        f"• favorites: {counts.get('favorites', 0)}",
        f"• saved_products: {counts.get('saved_products', 0)}",
        f"• tracked_products: {counts.get('tracked_products', 0)}",
        f"• price_history: {counts.get('price_history', 0)}",
    ]
    return "\n".join(lines), admin_screen_keyboard("products", period)


async def _screen_feed(
    context: ContextTypes.DEFAULT_TYPE, period: str, page: int
) -> tuple[str, Any]:
    """§46: diagnostics of the last «✨ Находки» page fetch (G24)."""

    search_service = context.application.bot_data.get("search_service")
    stats = getattr(search_service, "last_feed_stats", None)

    lines = ["✨ <b>Находки</b>", ""]

    if stats is None:
        lines.append("• Лента ещё не запрашивалась в этом процессе — откройте "
                     "«Находки» в меню и вернитесь сюда.")
    else:
        # Honest per-open counters straight from
        # ``SearchService.find_feed_page``. Absent keys degrade to «—» so an
        # older dict never breaks the screen.
        def _field(key: str, default: str = "—") -> Any:
            value = stats.get(key)
            return default if value is None else str(value)

        lines.extend(
            [
                f"• Страница API: <b>{_field('page')}</b> "
                f"(<code>{_field('endpoint')}</code>)",
                f"• Строк в ответе: <b>{_field('raw_rows')}</b>, "
                f"уникальных: <b>{_field('final')}</b> "
                f"(точных дублей: {_field('exact_duplicates', '0')})",
                f"• Без картинки отброшено: {_field('dropped_no_image', '0')}, "
                f"без affiliate-ссылки: {_field('dropped_no_affiliate', '0')}",
                f"• Уже показано этому пользователю (пропущено): "
                f"{_field('hidden_as_seen', '0')}",
                f"• Итого отдано карточек: <b>{_field('final')}</b>",
            ]
        )

    lines.extend(
        [
            "",
            "<b>Источник кандидатов</b>:",
            "• product.query (aliexpress.affiliate.product.query) БЕЗ keywords "
            "и БЕЗ sort — собственная витрина AliExpress: ~15–19 млн строк, "
            "page_size до 50, документированная пагинация page_no.",
            "• hotproduct.query и product.smartmatch — недоступны "
            "(InsufficientPermission) для этого ключа.",
            "• Порядок и состав ленты решает AliExpress: бот не ранжирует, не "
            "оценяет качество и не выдумывает карточки (G24).",
        ]
    )
    return "\n".join(lines), admin_screen_keyboard("feed", period)


async def _screen_discounts(
    context: ContextTypes.DEFAULT_TYPE, period: str, page: int
) -> tuple[str, Any]:
    """§47: the exact four configured destinations + consistency check."""

    lines = ["\U0001F4B0 <b>Скидки</b>", ""]
    ok = len(HOT_DISCOUNT_SECTIONS) == 4
    for section in HOT_DISCOUNT_SECTIONS:
        lines.append(f"• {section.title}")
        lines.append(f"  <code>{common.escape(section.url)}</code>")

    lines.extend(
        [
            "",
            f"• Разделов: {len(HOT_DISCOUNT_SECTIONS)} (ожидается 4) — "
            + ("OK" if ok else "ОШИБКА КОНФИГУРАЦИИ"),
            "• Ссылки не модифицируются и не скрейпятся — только прямые "
            "URL-кнопки (§25).",
        ]
    )
    return "\n".join(lines), admin_screen_keyboard("discounts", period)



async def _screen_sales(
    context: ContextTypes.DEFAULT_TYPE, period: str, page: int
) -> tuple[str, Any]:
    """«Акции»: the reusable promotional section's live on/off control.

    The state shown is the one ACTUALLY persisted in ``bot_config`` (the same
    store the ``manage_sales.py`` CLI writes), re-read on every render, so a
    restart, a CLI change and the panel can never disagree. Enabling only
    exposes the «Чёрная пятница» menu button; disabling removes it and closes
    the callback - neither touches the ordinary product search. A DB read
    failure degrades to "выключена" (fail-safe: never advertise a section the
    bot cannot confirm is on).
    """

    event = get_event(DEFAULT_EVENT_ID)
    title = event.title if event is not None else "Чёрная пятница"
    try:
        raw = await common.get_database(context).get_config(
            enabled_key(DEFAULT_EVENT_ID)
        )
    except Exception:  # noqa: BLE001 - a read failure must not break the panel
        logger.exception("Failed to read sales state for the admin panel")
        raw = None
    enabled = is_enabled_value(raw)

    has_url = False
    try:
        from config.sales import url_key

        has_url = (
            await common.get_database(context).get_config(url_key(DEFAULT_EVENT_ID))
        ) is not None
    except Exception:  # noqa: BLE001 - best-effort diagnostic only
        logger.debug("Failed to read sales URL for the admin panel")

    state = "Включена ✅" if enabled else "Выключена ⏸"
    lines = [
        "\U0001F3F7 <b>Акции</b>",
        "",
        f"• Событие: «{title}» (<code>{DEFAULT_EVENT_ID}</code>)",
        f"• Состояние: <b>{state}</b>",
        "• Ссылка акции: "
        + ("задана" if has_url else "не задана"),
        "",
        "<b>Что делает включение</b>",
        "• В главном меню у обычных пользователей появляется кнопка акции.",
        "• Выключение убирает кнопку и закрывает доступ к разделу.",
        "• Обычный поиск товаров при этом не затрагивается.",
        "",
        "<b>То же с сервера</b> (без панели)",
        f"• <code>python manage_sales.py enable {DEFAULT_EVENT_ID}</code>",
        f"• <code>python manage_sales.py disable {DEFAULT_EVENT_ID}</code>",
    ]
    return "\n".join(lines), admin_sales_keyboard(enabled)


async def _set_sale_enabled(context: ContextTypes.DEFAULT_TYPE, event_id: str, enabled: bool) -> None:
    """Persist a sales event's enabled flag (the panel's toggle action).

    Writes through to the SAME ``bot_config`` table the ``manage_sales.py`` CLI
    uses, so the panel and the command line are two views of one setting. A
    write failure is logged and swallowed: the panel then re-reads the stored
    value and shows the real state, so a failed toggle can never claim success.
    """

    try:
        await common.get_database(context).set_config(
            enabled_key(event_id), "1" if enabled else "0"
        )
        logger.info("Sales event %r set to %s via admin panel", event_id, enabled)
    except Exception:  # noqa: BLE001 - the panel must not crash on a write error
        logger.exception("Failed to persist sales state for %r", event_id)



async def _screen_ai(
    context: ContextTypes.DEFAULT_TYPE, period: str, page: int
) -> tuple[str, Any]:
    """§48: AI diagnostics - statuses and counters, never keys."""

    settings = common.get_settings(context)
    ai_service = context.application.bot_data.get("ai_service")

    key_configured = bool(getattr(settings, "gemini_api_key", None))
    available = ai_service is not None and ai_service.is_available

    lines = [
        "\U0001F916 <b>AI</b>",
        "",
        "• Провайдер: Gemini (google-genai)",
        "• Ключ GEMINI_API_KEY: " + ("задан" if key_configured else "не задан"),
        "• Клиент активен: " + ("да" if available else "нет — локальный парсер"),
    ]
    if ai_service is not None:
        lines.extend(
            [
                f"• Модель: {common.escape(str(ai_service.model))}",
                "",
                "<b>Счётчики (с момента запуска)</b>:",
                f"• Успешных generate: {ai_service.generate_ok}",
                f"• Ошибок generate: {ai_service.generate_failed}",
                f"• Парсинг запросов OK: {ai_service.parse_ok}",
                f"• Парсинг ушёл в фолбэк: {ai_service.parse_fallback}",
                f"• Запросов ранжирования: {ai_service.rank_requests}",
                f"• Ранжирование откатилось: {ai_service.rank_fallback}",
                f"• Фото-запросов (vision): {ai_service.image_requests}",
            ]
        )
    else:
        lines.append("• AI-сервис не сконфигурирован в этом процессе")

    lines.extend(["", "Ключ и содержимое промптов здесь не отображаются."])
    return "\n".join(lines), admin_screen_keyboard("ai", period)


async def _screen_api(
    context: ContextTypes.DEFAULT_TYPE, period: str, page: int
) -> tuple[str, Any]:
    """§49: AliExpress capability status + counters + safe test."""

    search_service = context.application.bot_data.get("search_service")
    providers = list(getattr(search_service, "providers", None) or [])

    lines = ["\U0001F4E1 <b>AliExpress API</b>", ""]

    if not providers:
        lines.append("• Провайдер не сконфигурирован в этом процессе.")
    for provider in providers:
        configured = provider.is_configured
        lines.append(
            f"• {common.escape(provider.display_name)}: "
            + ("ключи заданы" if configured else "нет ключей")
        )
        client = getattr(provider, "_client", None)
        if client is not None:
            # "Пустых результатов" is shown separately from "Ошибок": a
            # documented empty answer (``resp_code=405``) is a real response
            # to a real query, so it must stay visible without pretending to
            # be an infrastructure failure.
            lines.extend(
                [
                    f"  • Успешных вызовов: {client.calls_ok}",
                    f"  • Ошибок: {client.calls_err}",
                    f"  • Транзиентных повторов: "
                    f"{getattr(client, 'retries_spent', 0)}",
                    f"  • Пустых результатов: "
                    f"{getattr(client, 'empty_results', 0)}",
                    "  • Последний успешный: "
                    + common.escape(str(client.last_ok_at or "—")),
                    "  • Последний класс ошибки: "
                    + common.escape(str(client.last_error_class or "—")),
                ]
            )

    lines.append("")
    lines.append("<b>Возможности (живой аудит §5)</b>:")
    lines.extend(f"• {common.escape(line)}" for line in API_CAPABILITIES)

    test_result = context.user_data.get(ADMIN_API_TEST_KEY)
    lines.extend(["", "<b>Тестовый запрос</b>:"])
    if test_result:
        lines.append(f"• {common.escape(str(test_result))}")
    else:
        lines.append("• ещё не выполнялся в этой сессии")

    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    from keyboards.main import CB_BACK, LABEL_HOME

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "\U0001F9EA Тест-запрос",
                    callback_data=f"{CB_ADMIN_PREFIX}:{CB_ADMIN_API_TEST_KEY}",
                )
            ],
            [
                InlineKeyboardButton(
                    "\U0001F510 Админ-панель",
                    callback_data=f"{CB_ADMIN_PREFIX}:{CB_ADMIN_MENU_KEY}",
                ),
                InlineKeyboardButton(LABEL_HOME, callback_data=CB_BACK),
            ],
        ]
    )
    return "\n".join(lines), keyboard


async def _run_api_test(context: ContextTypes.DEFAULT_TYPE) -> str:
    """§49: minimal safe smoke request - one 1-item product.query.

    Only the outcome class is returned: never parameters, never the
    signature, never the response body.
    """

    search_service = context.application.bot_data.get("search_service")
    providers = [
        p
        for p in (getattr(search_service, "providers", None) or [])
        if p.is_configured
    ]
    if not providers:
        return "нет настроенного провайдера — тест пропущен"

    from models.search_request import SearchRequest

    request = SearchRequest(original_query="test", normalized_query="test")
    try:
        products = await providers[0].search_products(request)
    except Exception as exc:  # noqa: BLE001 - report class only
        return f"тест завершился ошибкой: {type(exc).__name__}"
    return f"тест OK — получено товаров: {len(products)}"



async def _screen_database(
    context: ContextTypes.DEFAULT_TYPE, period: str, page: int
) -> tuple[str, Any]:
    """§50: SQLite health - tables, row counts, integrity, path status."""

    from pathlib import Path

    settings = common.get_settings(context)
    database = common.get_database(context)

    configured_path = str(getattr(settings, "database_path", "") or "")
    actual = Path(str(getattr(database, "db_path", configured_path)))

    tables = await database.list_tables()
    counts = await database.table_row_counts()
    integrity = await database.integrity_check()

    lines = [
        "\U0001F5C4 <b>База данных</b>",
        "",
        f"• Путь настроен: <code>{common.escape(configured_path)}</code>",
        f"• Файл существует: {'да' if actual.exists() else 'нет'}",
        f"• SQLite integrity: <b>{common.escape(integrity)}</b>",
        f"• Таблиц: {len(tables)}",
        "",
        "<b>Строк по таблицам:</b>",
    ]
    for table in tables:
        lines.append(f"• {common.escape(table)}: {counts.get(table, 0)}")
    lines.extend(["", "Деструктивных действий панель не выполняет (§50)."])
    return "\n".join(lines), admin_screen_keyboard("db", period)


async def _screen_logs(
    context: ContextTypes.DEFAULT_TYPE, period: str, page: int
) -> tuple[str, Any]:
    """§51: recent log lines, already masked, paginated."""

    lines_on_page, total = get_recent_logs(page=page, per_page=LOGS_PER_PAGE)
    pages = max(1, (total + LOGS_PER_PAGE - 1) // LOGS_PER_PAGE)
    has_next = (page + 1) * LOGS_PER_PAGE < total

    lines = [
        "\U0001F4DD <b>Логи</b>",
        f"Страница {page + 1}/{pages} (всего записей: {total})",
        "",
    ]
    if not lines_on_page:
        lines.append("• буфер логов пуст")
    for entry in lines_on_page:
        lines.append(f"<code>{common.escape(entry)}</code>")

    lines.extend(
        [
            "",
            "Секреты маскируются до записи в буфер; стектрейсы не сохраняются.",
        ]
    )
    return "\n".join(lines), admin_screen_keyboard(
        "logs", period, page=page, has_next=has_next
    )



async def _screen_system(
    context: ContextTypes.DEFAULT_TYPE, period: str, page: int
) -> tuple[str, Any]:
    """§52: runtime status - versions, scheduler, services."""

    settings = common.get_settings(context)
    bot_data = context.application.bot_data

    tracker = bot_data.get("price_tracker")
    interval = int(getattr(settings, "tracking_interval_minutes", 1440) or 0)
    if tracker is not None and interval > 0:
        scheduler = f"\U0001F7E2 активен (каждые {interval} мин)"
    else:
        scheduler = "⏸ выключен"

    ai_service = bot_data.get("ai_service")
    ai_status = (
        "подключён"
        if ai_service is not None and ai_service.is_available
        else "локальный парсер (Gemini недоступен)"
    )

    search_service = bot_data.get("search_service")
    configured = [
        p.display_name
        for p in (getattr(search_service, "providers", None) or [])
        if p.is_configured
    ]

    lines = [
        "⚙️ <b>Система</b>",
        "",
        f"• Python: {common.escape(sys.version.split()[0])}",
        f"• Платформа: {common.escape(platform.platform())}",
        "• Приложение: работает (экран отрисован)",
        f"• Планировщик цен: {scheduler}",
        f"• AI: {ai_status}",
        "• AliExpress: "
        + (
            ", ".join(common.escape(name) for name in configured)
            if configured
            else "нет ключей"
        ),
        f"• Администраторов настроено: {len(settings.admin_ids)}",
    ]
    return "\n".join(lines), admin_screen_keyboard("system", period)



def _capabilities(
    context: ContextTypes.DEFAULT_TYPE, settings: Any
) -> list[str]:
    """Honest capability/limitation report (no secrets, only statuses)."""

    bot_data = context.application.bot_data

    search_service = bot_data.get("search_service")
    providers = list(getattr(search_service, "providers", None) or [])
    configured = [
        provider.display_name for provider in providers if provider.is_configured
    ]
    lines = [
        "AliExpress API: "
        + (
            "подключён (Affiliate API)"
            if configured
            else "нет ключей API — поиск честно отвечает «не подключено»"
        )
    ]

    ai_service = bot_data.get("ai_service")
    lines.append(
        "Gemini (AI-парсер запросов и ранжирование «Популярного»): "
        + ("включён" if ai_service is not None and ai_service.is_available else "выключен")
    )

    tracker = bot_data.get("price_tracker")
    interval = int(getattr(settings, "tracking_interval_minutes", 1440) or 0)
    if tracker is not None and interval > 0:
        lines.append(f"Автопроверка цен: каждые {interval} мин")
    else:
        lines.append(
            "Автопроверка цен: отключена (TRACKING_INTERVAL_MINUTES=0 "
            "или провайдер не настроен)"
        )

    lines.append(
        "Поиск по фото: недоступен — метод требует уровень доступа "
        "app_signature, которого у ключа нет"
    )
    return lines


def _build_text(
    *,
    period: str,
    since: str | None,
    event_counts: dict[str, int],
    event_users: dict[str, int],
    total_users: int,
    new_users: int,
    top_products: list[dict],
    daily: list[dict],
    admins_configured: int,
    capabilities: list[str],
) -> str:
    """Render the panel.

    Every number is read from the database; a section with no rows says
    "no data" instead of showing a confident zero, and the period is
    always named because "Сегодня" means the current UTC day.
    """

    lines = [
        "🛠 <b>Статистика бота</b>",
        f"Период: {common.escape(analytics.period_label(period))}"
        + (" (с начала текущих суток по UTC)" if period == "today" else ""),
        "",
        "<b>Возможности и ограничения</b>",
    ]
    lines.extend(f"• {common.escape(line)}" for line in capabilities)
    lines.extend(
        [
            "",
            "<b>Пользователи</b>",
            f"• Всего в базе: {total_users}",
            f"• Новых за период: {new_users}",
        ]
    )

    if not event_counts:
        lines.extend(
            [
                "",
                "Событий за этот период нет.",
                "Панель считает только то, что бот записал: "
                "если пользователи не заходили, цифр быть не может.",
            ]
        )
    else:
        lines.append("")
        lines.append("<b>Действия</b>")
        for event_type, label in analytics.EVENT_LABELS.items():
            total = event_counts.get(event_type)
            if not total:
                continue
            users = event_users.get(event_type)
            users_part = f" (уникальных пользователей: {users})" if users else ""
            lines.append(f"• {common.escape(label)}: <b>{total}</b>{users_part}")

        affiliate = event_counts.get(analytics.EVENT_BUY_AFFILIATE, 0)
        external = event_counts.get(analytics.EVENT_BUY_EXTERNAL, 0)
        clicks = affiliate + external
        shown = event_counts.get(analytics.EVENT_PRODUCTS_SHOWN, 0)
        lines.extend(
            [
                "",
                "<b>Переходы в магазин</b>",
                f"• Партнёрские клики: <b>{affiliate}</b>",
                f"• Обычные ссылки (без партнёрской метки): <b>{external}</b>",
            ]
        )
        if clicks and shown:
            lines.append(
                f"• CTR к показанным карточкам: {clicks / shown * 100:.1f}%"
                f" ({clicks} из {shown})"
            )
        else:
            lines.append("• CTR: пока считать нечего (нет кликов или показов)")

    if top_products:
        lines.extend(["", f"<b>Топ-{len(top_products)} товаров по партнёрским кликам</b>"])
        for row in top_products:
            title = row.get("title") or "(название не сохранено)"
            lines.append(
                f"• {common.escape(str(title))} — <b>{row.get('total')}</b>"
            )

    if daily:
        lines.extend(["", "<b>События по дням (последние 7 дней)</b>"])
        for row in daily[:7]:
            lines.append(f"• {common.escape(str(row.get('day')))}: {row.get('total')}")

    lines.extend(
        [
            "",
            f"👤 Администраторов настроено: {admins_configured}.",
            "ℹ️ Хранятся только события и счётчики: текст запросов "
            "пользователей в статистику не попадает (он лежит в истории "
            "поиска каждого пользователя отдельно).",
        ]
    )
    if since is not None:
        lines.append(f"🕒 Граница периода: {common.escape(since)} UTC.")

    return "\n".join(lines)


async def _edit_or_reply(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, **kwargs
) -> None:
    """Edit the screen a callback came from, or reply to the command."""

    query = update.callback_query
    if query is not None:
        await query.edit_message_text(text, **kwargs)
        return

    message = update.effective_message
    if message is not None:
        await message.reply_text(text, **kwargs)
