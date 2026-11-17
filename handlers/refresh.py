"""Explicit refresh action for every cached product-card browsing flow.

The callback reuses the validated request already attached to the visible
session. It never asks Gemini to reinterpret the query and never weakens the
existing retrieval, relevance, ranking, selector, or curation pipeline.
"""
from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from handlers import common
from handlers.search import _curation_mode, record_outcome_events, send_search_results
from keyboards.main import back_to_menu_keyboard
from models.search_request import SearchMode
from services import analytics
from services.product_identity import identity_pair
from services.search_service import SearchOutcome

logger = logging.getLogger(__name__)

REFRESH_SEEN_KEY = "_refresh_seen_by_scope"
REFRESH_USED_KEY = "_refresh_used_sessions"
REFRESH_BUSY_KEY = "_refresh_busy_scopes"


def get_refresh_seen(context: ContextTypes.DEFAULT_TYPE, scope: str | None) -> set[tuple[str, str]]:
    """Return the in-memory products already returned by refreshes in a scope."""
    if not isinstance(scope, str) or not scope:
        return set()
    stored = context.user_data.get(REFRESH_SEEN_KEY, {})
    if not isinstance(stored, dict):
        return set()
    values = stored.get(scope, set())
    return set(values) if isinstance(values, (set, list, tuple)) else set()


async def _refresh_findings(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int | None,
    telegram_id: int | None,
    previous: SearchOutcome,
    exclude: set[tuple[str, str]],
) -> SearchOutcome:
    """Fetch the next bounded slice of the official Findings feed."""
    from handlers.discovery import (
        FEED_CANDIDATE_PAGE_SIZE,
        FEED_PAGE_KEY,
        _bounded_setting,
        _feed_page_numbers,
        _feed_start_page,
        _merge_feed_outcome,
        _next_feed_page,
        _remember_feed_page,
    )

    region = await common.get_user_region(context, user_id)
    country = (region.get("country") or "").strip().upper()
    country = country if len(country) == 2 else None
    settings_obj = common.get_settings(context)
    open_size = _bounded_setting(settings_obj, "feed_open_size", 20, 100)
    max_pages = _bounded_setting(settings_obj, "feed_max_pages_per_open", 2, 10)
    start_page = context.user_data.get(FEED_PAGE_KEY)
    if not start_page:
        start_page = await _feed_start_page(context, user_id)
    start_page = max(1, int(start_page or previous.request.page_no or 1))

    search_service = common.get_search_service(context)
    candidate_target = min(
        FEED_CANDIDATE_PAGE_SIZE * max_pages,
        max(open_size, 80),
    )
    merged: SearchOutcome | None = None
    collected = []
    pages_to_fetch = _feed_page_numbers(start_page, max_pages)
    page_index = 0
    pages_asked = 0
    reached_end = False
    restarted_from_first_page = False
    recovery_page_requested = False

    while (
        page_index < len(pages_to_fetch)
        and pages_asked < max_pages
        and len(collected) < candidate_target
    ):
        page = pages_to_fetch[page_index]
        if restarted_from_first_page and page == 1:
            recovery_page_requested = True
        page_exclude = set(exclude)
        page_exclude.update(identity_pair(product) for product in collected)
        page_outcome = await search_service.find_feed_page(
            page=page,
            country=country,
            currency=region.get("currency"),
            exclude=page_exclude,
            limit=FEED_CANDIDATE_PAGE_SIZE,
        )
        pages_asked += 1
        if merged is None:
            merged = page_outcome
        else:
            _merge_feed_outcome(merged, page_outcome)
        collected.extend(page_outcome.products)

        if not page_outcome.products_before_filtering:
            # Match the regular Findings opener: a stale distant cursor is
            # retried from page 1 within the same bounded API-call budget.
            if (
                page_index == 0
                and page > 1
                and not collected
                and not restarted_from_first_page
            ):
                restarted_from_first_page = True
                remaining = max_pages - pages_asked
                pages_to_fetch[page_index + 1 :] = (
                    _feed_page_numbers(1, remaining) if remaining else []
                )
                page_index += 1
                continue
            if page == 1 and not collected:
                reached_end = True
                break
            page_index += 1
            continue

        if len(collected) >= candidate_target:
            break
        page_index += 1

    if merged is None:
        merged = SearchOutcome(
            request=previous.request.model_copy(update={"page_no": start_page})
        )

    await search_service.curate_findings(merged, wanted=open_size)
    next_page = _next_feed_page(
        start_page,
        reached_end=reached_end,
        restarted_from_first_page=restarted_from_first_page,
        recovery_page_requested=recovery_page_requested,
    )
    context.user_data[FEED_PAGE_KEY] = next_page
    await _remember_feed_page(context, user_id, next_page)
    await common.record_event(
        context,
        analytics.EVENT_FEED_CURATED,
        user_id=user_id,
        telegram_id=telegram_id,
        metadata=search_service.last_findings_stats or {},
    )
    return merged


async def _send_refresh_status(context, chat_id: int, text: str, keyboard) -> None:
    await context.bot.send_message(
        chat_id,
        text,
        reply_markup=keyboard or back_to_menu_keyboard(),
    )


async def refresh_results_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """pref:<session_id> — replace the current card with a fresh batch."""
    query = update.callback_query
    if query is None:
        return

    parts = (query.data or "").split(":")
    if len(parts) != 2 or parts[0] != "pref" or not parts[1]:
        await query.answer("Некорректная кнопка.", show_alert=True)
        return

    session_id = parts[1]
    previous = common.get_cached_outcome(context, session_id)
    if previous is None or not previous.products:
        await query.answer(
            "Эта подборка устарела. Выполните поиск ещё раз.",
            show_alert=True,
        )
        return

    meta = common.get_outcome_meta(context, session_id)
    options = meta.get("refresh_options")
    if not isinstance(options, dict):
        options = {}
    scope = meta.get("refresh_scope")
    if not isinstance(scope, str) or not scope:
        scope = session_id

    used = context.user_data.setdefault(REFRESH_USED_KEY, set())
    if not isinstance(used, set):
        used = set()
        context.user_data[REFRESH_USED_KEY] = used
    if session_id in used:
        await query.answer(
            "Эта подборка уже обновлена. Используйте кнопку на текущей карточке.",
            show_alert=True,
        )
        return

    busy = context.user_data.setdefault(REFRESH_BUSY_KEY, set())
    if not isinstance(busy, set):
        busy = set()
        context.user_data[REFRESH_BUSY_KEY] = busy
    if scope in busy:
        await query.answer("Подборка уже обновляется.")
        return

    busy.add(scope)
    try:
        await query.answer("Обновляю подборку…")
        user_id = await common.register_user(update, context)
        telegram_id = update.effective_user.id if update.effective_user else None
        chat_id = update.effective_chat.id if update.effective_chat else None
        if chat_id is None:
            message = getattr(query, "message", None)
            chat_id = getattr(message, "chat_id", None)
        if chat_id is None:
            return

        seen_by_scope = context.user_data.setdefault(REFRESH_SEEN_KEY, {})
        if not isinstance(seen_by_scope, dict):
            seen_by_scope = {}
            context.user_data[REFRESH_SEEN_KEY] = seen_by_scope
        scope_seen = seen_by_scope.setdefault(scope, set())
        if not isinstance(scope_seen, set):
            scope_seen = set(scope_seen or ())
            seen_by_scope[scope] = scope_seen

        # A refresh skips the whole previous batch, not just the visible card.
        scope_seen.update(identity_pair(product) for product in previous.products)
        flow = common.session_flow(context, session_id)
        persistent_seen = await common.seen_products(
            context, user_id, flow=flow
        )
        exclude = set(persistent_seen) | set(scope_seen)

        if previous.request.mode is SearchMode.FEED:
            outcome = await _refresh_findings(
                context, user_id, telegram_id, previous, exclude
            )
        else:
            outcome = await common.get_search_service(context).find_products(
                previous.request,
                exclude=exclude,
                broaden=bool(options.get("refresh_broaden", True)),
            )
            subcategory_code = previous.request.subcategory_code
            if subcategory_code:
                from config.thematic_collections import THEME_MAX_PRICE_BY_CODE

                max_price = THEME_MAX_PRICE_BY_CODE.get(subcategory_code)
                if max_price is not None:
                    outcome.products = [
                        product
                        for product in outcome.products
                        if product.price is not None
                        and (product.currency or "").upper() == "USD"
                        and product.price <= max_price
                    ]

        await record_outcome_events(
            context,
            outcome,
            user_id=user_id,
            telegram_id=telegram_id,
            source="refresh",
        )

        track_shown = bool(options.get("track_shown", meta.get("track_shown")))
        pre_curated = bool(options.get("pre_curated", False))
        curation_history = bool(options.get("curation_history", True))
        curation_mode = options.get("curation_mode")
        if outcome.products and not pre_curated:
            outcome.products = await common.curate_products(
                context,
                outcome.products,
                user_id=user_id if curation_history else None,
                mode=curation_mode or _curation_mode(outcome, track_shown),
                request=outcome.request,
            )

        minimum_products = max(0, int(options.get("minimum_products") or 0))
        if not outcome.products or len(outcome.products) < minimum_products:
            if outcome.products and minimum_products:
                status = (
                    f"Пока нашлось только {len(outcome.products)} новых товаров. "
                    "Текущая подборка осталась доступна."
                )
            else:
                status = (
                    "Пока не удалось найти новые подходящие товары. "
                    "Текущая подборка осталась доступна."
                )
            await _send_refresh_status(
                context, chat_id, status, meta.get("wrap_up_keyboard")
            )
            return

        from services.convsearch.state import ConversationEngineState

        conversation_state = context.user_data.get("_convsearch_state")
        if (
            not isinstance(conversation_state, ConversationEngineState)
            or conversation_state.is_expired()
        ):
            conversation_state = None

        new_session_id = await send_search_results(
            context,
            chat_id,
            outcome,
            header=meta.get("header"),
            wrap_up_keyboard=meta.get("wrap_up_keyboard"),
            back_callback=meta.get("back_callback"),
            user_id=user_id,
            telegram_id=telegram_id,
            track_shown=track_shown,
            curation_mode=curation_mode,
            pre_curated=True,
            refresh_pre_curated=pre_curated,
            follow_up=False,
            conversation_state=conversation_state,
            origin_query=query,
            history_flow=meta.get("history_flow"),
            record_views=bool(options.get("record_views", meta.get("record_views", True))),
            home_callback=options.get("home_callback", meta.get("home_callback")),
            curation_history=curation_history,
            minimum_products=minimum_products,
            strict_navigation=bool(options.get("strict_navigation", meta.get("strict_navigation", False))),
            refresh_scope=scope,
            refresh_broaden=bool(options.get("refresh_broaden", True)),
        )
        if not new_session_id:
            return

        scope_seen.update(identity_pair(product) for product in outcome.products)
        while len(seen_by_scope) > 50:
            seen_by_scope.pop(next(iter(seen_by_scope)))
        used.add(session_id)
        while len(used) > 100:
            used.pop()
        context.user_data["last_shown_session"] = new_session_id
    except Exception:
        logger.exception("Product-card refresh failed for session %s", session_id)
        try:
            failure_chat_id = (
                getattr(getattr(query, "message", None), "chat_id", None)
                or (update.effective_chat.id if update.effective_chat else None)
            )
            if failure_chat_id is not None:
                await _send_refresh_status(
                    context,
                    failure_chat_id,
                    "Не удалось обновить подборку. Текущая карточка осталась доступна.",
                    meta.get("wrap_up_keyboard"),
                )
        except Exception:
            logger.exception("Could not report product-card refresh failure")
    finally:
        busy.discard(scope)
