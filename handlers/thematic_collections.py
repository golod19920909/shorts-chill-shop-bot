"""Independent editorial thematic collections for catalog discovery."""
from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from config.thematic_collections import (
    THEMATIC_COLLECTIONS,
    BUDGET_TIERS,
    THEME_MAX_PRICE_BY_CODE,
)
from config.subcategories import get_subcategory
from database.models import FLOW_THEMES
from handlers import common
from handlers.discovery import record_outcome_events
from handlers.search import send_search_results
from keyboards.main import (
    CB_CATEGORIES,
    CB_THEME_PREFIX,
    CB_CATALOG_HOME,
    thematic_budget_keyboard,
    thematic_collections_keyboard,
)
from services import analytics
from services.gemini_telemetry import track_usage
from curation.pipeline import MODE_CATEGORY
from models.search_request import SearchMode
from services.product_identity import identity_pair

logger = logging.getLogger(__name__)

THEME_MINIMUM_PRODUCTS = 12
THEME_MENU_TEXT = (
    "✨ <b>Тематические подборки</b>\n\n"
    "Готовые подборки реальных товаров с AliExpress — по идее, назначению "
    "или бюджету. Выберите интересующую тему:"
)
THEME_BUDGET_TEXT = (
    "💸 <b>Полезные мелочи до определённого бюджета</b>\n\n"
    "Выберите максимальную цену. В подборку попадут только товары с "
    "подтверждённой ценой в USD не выше выбранного лимита:"
)


def _theme_description(code: str) -> str:
    item = next((x for x in THEMATIC_COLLECTIONS if x["code"] == code), None)
    return item["description"] if item else ""


async def _edit_screen(
    context: ContextTypes.DEFAULT_TYPE, query, text: str, keyboard
) -> None:
    await common.edit_message_or_replace(
        context,
        query,
        text,
        parse_mode="HTML",
        reply_markup=keyboard,
    )


async def show_thematic_collections(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    await _edit_screen(context, query, THEME_MENU_TEXT, thematic_collections_keyboard())


async def _show_budget_tiers(context, query) -> None:
    await _edit_screen(context, query, THEME_BUDGET_TEXT, thematic_budget_keyboard())



_THEME_MORE_BUSY_KEY = "_theme_more_busy"


async def _show_more_thematic_products(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session_id: str,
) -> None:
    query = update.callback_query
    if query is None:
        return
    previous = common.get_cached_outcome(context, session_id)
    if (
        previous is None
        or previous.request.mode is not SearchMode.CATEGORY
        or not (previous.request.subcategory_code or "").startswith("theme_")
    ):
        await query.answer("Эта подборка уже недоступна. Откройте тему заново.", show_alert=True)
        return
    subcategory = get_subcategory(previous.request.subcategory_code)
    if subcategory is None:
        await query.answer("Подборка больше недоступна.", show_alert=True)
        return

    busy = context.user_data.setdefault(_THEME_MORE_BUSY_KEY, set())
    if session_id in busy:
        await query.answer("Подборка уже обновляется.")
        return
    busy.add(session_id)
    await query.answer("Ищу ещё подходящие товары…")
    try:
        user_id = await common.register_user(update, context)
        region = await common.get_user_region(context, user_id)
        country = (region.get("country") or "").strip().upper()
        country = country if len(country) == 2 else None
        telegram_id = update.effective_user.id if update.effective_user else None
        chat_id = update.effective_chat.id if update.effective_chat else None
        if chat_id is None:
            return

        excluded = set(
            await common.seen_products(context, user_id, flow=FLOW_THEMES)
        )
        excluded.update(identity_pair(product) for product in previous.products)
        max_price = THEME_MAX_PRICE_BY_CODE.get(subcategory.code)
        outcome = await common.get_search_service(context).find_by_subcategory(
            subcategory,
            country=country,
            currency="USD" if max_price is not None else region.get("currency"),
            exclude=excluded,
            max_price=max_price,
        )
        if max_price is not None:
            outcome.products = [
                product for product in outcome.products
                if product.price is not None
                and (product.currency or "").upper() == "USD"
                and product.price <= max_price
            ]
        await record_outcome_events(
            context,
            outcome,
            user_id=user_id,
            telegram_id=telegram_id,
            source="thematic_collection_more",
        )
        if len(outcome.products) < THEME_MINIMUM_PRODUCTS:
            await common.edit_message_or_replace(
                context,
                query,
                "Пока больше не набралось 12 подходящих товаров. "
                "Не буду добавлять случайные позиции. Выберите другую тему или попробуйте позже.",
                parse_mode="HTML",
                reply_markup=(
                    thematic_budget_keyboard()
                    if max_price is not None
                    else thematic_collections_keyboard()
                ),
            )
            return

        header = (
            f"✨ <b>{common.escape(subcategory.title)}</b>\n"
            f"<i>{common.escape(subcategory.description)}</i>"
        )
        await send_search_results(
            context,
            chat_id,
            outcome,
            header=header,
            wrap_up_keyboard=thematic_collections_keyboard(),
            back_callback=f"{CB_THEME_PREFIX}:menu",
            user_id=user_id,
            telegram_id=telegram_id,
            pre_curated=True,
            follow_up=False,
            origin_query=query,
            history_flow=FLOW_THEMES,
            record_views=False,
            curation_mode=MODE_CATEGORY,
            home_callback=CB_CATALOG_HOME,
        )
    except Exception:
        logger.exception("Thematic collection refresh failed: %s", subcategory.code)
        await common.edit_message_or_replace(
            context,
            query,
            "Не удалось обновить подборку. Предыдущая карточка останется доступна.",
            parse_mode="HTML",
            reply_markup=thematic_collections_keyboard(),
        )
    finally:
        busy.discard(session_id)


@track_usage("thematic_collection_search")
async def thematic_collection_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if query is None:
        return
    data = query.data or ""
    parts = data.split(":")
    if len(parts) == 2 and parts[0] == CB_THEME_PREFIX and parts[1] == "menu":
        await show_thematic_collections(update, context)
        return
    if len(parts) == 2 and parts[0] == CB_THEME_PREFIX and parts[1] == "budget":
        await query.answer()
        await _show_budget_tiers(context, query)
        return
    if len(parts) == 3 and parts[0] == CB_THEME_PREFIX and parts[1] == "more":
        await _show_more_thematic_products(update, context, parts[2])
        return
    if len(parts) != 3 or parts[0] != CB_THEME_PREFIX:
        await query.answer("Некорректная кнопка.", show_alert=True)
        return

    shelf_code = None
    if parts[1] == "item":
        collection = next(
            (item for item in THEMATIC_COLLECTIONS if item["code"] == parts[2]),
            None,
        )
        if collection is None or collection["code"] == "budget_finds":
            await query.answer("Подборка не найдена.", show_alert=True)
            return
        shelf_code = "theme_" + collection["code"]
    elif parts[1] == "tier":
        if parts[2] not in {code for code, _label in BUDGET_TIERS}:
            await query.answer("Бюджетная подборка не найдена.", show_alert=True)
            return
        shelf_code = parts[2]
    else:
        await query.answer("Некорректная кнопка.", show_alert=True)
        return

    subcategory = get_subcategory(shelf_code)
    if subcategory is None:
        await query.answer("Подборка пока недоступна.", show_alert=True)
        return

    await query.answer("Подбираю подходящие товары…")
    user_id = await common.register_user(update, context)
    region = await common.get_user_region(context, user_id)
    country = (region.get("country") or "").strip().upper()
    country = country if len(country) == 2 else None
    telegram_id = update.effective_user.id if update.effective_user else None
    chat_id = update.effective_chat.id if update.effective_chat else None
    if chat_id is None:
        return

    try:
        max_price = THEME_MAX_PRICE_BY_CODE.get(subcategory.code)
        outcome = await common.get_search_service(context).find_by_subcategory(
            subcategory,
            country=country,
            currency="USD" if max_price is not None else region.get("currency"),
            exclude=await common.seen_products(
                context, user_id, flow=FLOW_THEMES
            ),
            max_price=max_price,
        )
        if max_price is not None:
            # A budget shelf is a USD promise, not merely a numeric ceiling.
            # Reject unknown/mismatched currencies and missing prices.
            outcome.products = [
                product for product in outcome.products
                if product.price is not None
                and (product.currency or "").upper() == "USD"
                and product.price <= max_price
            ]
    except Exception:
        logger.exception("Thematic collection search failed: %s", shelf_code)
        await common.record_event(
            context,
            analytics.EVENT_SEARCH_FAILED,
            user_id=user_id,
            telegram_id=telegram_id,
            metadata={"source": "thematic_collection", "collection": shelf_code},
        )
        await _edit_screen(
            context,
            query,
            "Не удалось получить подборку. Попробуйте ещё раз позже.",
            thematic_collections_keyboard(),
        )
        return

    await record_outcome_events(
        context,
        outcome,
        user_id=user_id,
        telegram_id=telegram_id,
        source="thematic_collection",
    )
    if len(outcome.products) < THEME_MINIMUM_PRODUCTS:
        await _edit_screen(
            context,
            query,
            "Пока не удалось собрать достаточно подходящих товаров для этой подборки. "
            "Я не буду заполнять её случайными позициями. Попробуйте позже или выберите другую тему.",
            thematic_budget_keyboard() if max_price is not None
            else thematic_collections_keyboard(),
        )
        return

    header = (
        f"✨ <b>{common.escape(subcategory.title)}</b>\n"
        f"<i>{common.escape(subcategory.description)}</i>"
    )
    await send_search_results(
        context,
        chat_id,
        outcome,
        header=header,
        wrap_up_keyboard=thematic_collections_keyboard(),
        back_callback=f"{CB_THEME_PREFIX}:menu",
        user_id=user_id,
        telegram_id=telegram_id,
        pre_curated=True,
        follow_up=False,
        origin_query=query,
        history_flow=FLOW_THEMES,
        record_views=False,
        curation_mode=MODE_CATEGORY,
    )
