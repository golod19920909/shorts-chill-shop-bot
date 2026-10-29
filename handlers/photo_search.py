"""Photo search handler and pipeline.

Processes user photo uploads by querying Gemini Vision for consumer keywords
and executing AliExpress search with quality filtering, image deduplication,
and smart fallback on broad categories.
"""

from __future__ import annotations

import logging
from typing import Any, Sequence

from telegram import Update
from telegram.ext import ContextTypes

from curation.pipeline import MODE_SEARCH, ImageDeduplicationFilter, QualityFilter
from handlers import common
from handlers.search import send_search_results
from keyboards.main import photo_retry_keyboard
from models.product import Product
from models.search_request import SearchMode, SearchRequest
from services import analytics
from services.product_identity import deduplicate_products
from services.search_service import SearchOutcome
from services.vision_search import (
    analyze_photo_with_vision,
    parse_vision_response,
)
from utils.emoji import PHOTO, WARNING

logger = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5 MB

from handlers.discovery import (
    IMAGE_EMPTY_TEXT,
    PHOTO_NO_AI_TEXT,
    PHOTO_RECEIVED_TEXT,
)

#: Route raw vision results through the SEARCH quality profile:
#: - price floor of $1 (USD prices; no ceiling in search mode)
#: - rating >= 4.5 when the marketplace states a star rating
#: - NO orders floor: a direct search never drops a real product on its
#:   recent-window ``lastest_volume`` (see curation.pipeline.QUALITY_PROFILES)
photo_quality_filter = QualityFilter(mode=MODE_SEARCH)
photo_image_dedup_filter = ImageDeduplicationFilter()


def filter_photo_results(products: Sequence[Product]) -> list[Product]:
    """Route raw vision results through QualityFilter and ImageDeduplicationFilter."""
    q_filtered = photo_quality_filter.apply(products)
    return photo_image_dedup_filter.apply(q_filtered)


def should_trigger_fallback(products: Sequence[Product]) -> bool:
    """Smart Fallback condition: < 3 items or only items with 0 orders."""
    if len(products) < 3:
        return True
    if all((p.orders_count or 0) == 0 for p in products):
        return True
    return False


async def execute_photo_search(
    query: str,
    fallback_category: str | None,
    search_service: Any,
    *,
    currency: str | None = None,
    exclude: set[tuple[str, str]] | None = None,
) -> list[Product]:
    """Execute primary vision search with smart fallback on fallback_category."""
    primary_req = SearchRequest(
        original_query=query,
        normalized_query=query,
        mode=SearchMode.SEARCH,
        currency=currency,
    )
    outcome = await search_service.find_products(primary_req, exclude=exclude, broaden=False)
    primary_products = filter_photo_results(outcome.products)

    if should_trigger_fallback(primary_products) and fallback_category:
        logger.info(
            "Primary photo search for %r returned %d usable items; triggering fallback for %r",
            query,
            len(primary_products),
            fallback_category,
        )
        fallback_req = SearchRequest(
            original_query=fallback_category,
            normalized_query=fallback_category,
            mode=SearchMode.SEARCH,
            currency=currency,
        )
        try:
            fb_outcome = await search_service.find_products(
                fallback_req, exclude=exclude, broaden=False
            )
            fallback_products = filter_photo_results(fb_outcome.products)
            merged = deduplicate_products(primary_products + fallback_products)
            return photo_image_dedup_filter.apply(merged)
        except Exception:
            logger.exception("Fallback search failed for category %r", fallback_category)

    return primary_products



async def photo_message_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle sent photo: vision query extraction, search, quality filtering, smart fallback."""
    message = update.effective_message
    if message is None or not message.photo:
        return

    photo = message.photo[-1]
    await message.reply_text(PHOTO_RECEIVED_TEXT)

    if photo.file_size and photo.file_size > MAX_IMAGE_BYTES:
        await message.reply_text(
            f"{WARNING} Фотография слишком большая для поиска "
            f"(лимит {MAX_IMAGE_BYTES // (1024 * 1024)} МБ).",
            reply_markup=photo_retry_keyboard(),
        )
        return

    try:
        telegram_file = await photo.get_file()
        image_bytes = bytes(await telegram_file.download_as_bytearray())
    except Exception:
        logger.exception("Failed to download a photo for image search")
        await message.reply_text(
            f"{WARNING} Не удалось загрузить фотографию. Отправьте её ещё раз.",
            reply_markup=photo_retry_keyboard(),
        )
        return

    user_id = await common.register_user(update, context)
    region = await common.get_user_region(context, user_id)

    await common.record_event(
        context,
        analytics.EVENT_IMAGE_SEARCH_REQUESTED,
        user_id=user_id,
        telegram_id=update.effective_user.id if update.effective_user else None,
        metadata={"bytes": len(image_bytes)},
    )

    ai_service = common.get_ai_service(context)
    if ai_service is None or not getattr(ai_service, "is_available", False):
        logger.info("Photo search skipped: no AI service is configured")
        await message.reply_text(PHOTO_NO_AI_TEXT, reply_markup=photo_retry_keyboard())
        return

    # Try reform vision search prompt first
    vision_res = await analyze_photo_with_vision(image_bytes, ai_service)
    search_service = common.get_search_service(context)
    seen = await common.seen_products(context, user_id)

    if vision_res and vision_res.get("query"):
        query = vision_res["query"]
        fallback_cat = vision_res.get("fallback_category")
        products = await execute_photo_search(
            query,
            fallback_cat,
            search_service,
            currency=region.get("currency"),
            exclude=seen,
        )
        if not products:
            await message.reply_text(
                IMAGE_EMPTY_TEXT, parse_mode="HTML", reply_markup=photo_retry_keyboard()
            )
            return

        chat_id = update.effective_chat.id if update.effective_chat else None
        if chat_id is None:
            return

        req = SearchRequest(
            original_query=query,
            normalized_query=query,
            mode=SearchMode.SEARCH,
            currency=region.get("currency"),
        )
        outcome = SearchOutcome(
            request=req,
            products=products,
            searched_marketplaces=["AliExpress"],
        )
        await send_search_results(
            context,
            chat_id,
            outcome,
            header=f"{PHOTO} <b>Поиск по фото:</b> {query}",
            user_id=user_id,
            back_callback="menu:photo",
        )
        return

    # Legacy fallback path for existing tests
    from handlers.discovery import _legacy_photo_message
    await _legacy_photo_message(update, context, image_bytes=image_bytes)
