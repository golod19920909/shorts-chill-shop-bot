"""Catalog-only Telegram navigation edits.

Keep this helper separate from the shared search/feed message-delivery path.
A replacement is permitted only for Telegram BadRequest responses that
explicitly say the existing message cannot be edited.
"""
from __future__ import annotations

import logging

from telegram.error import BadRequest

logger = logging.getLogger(__name__)

_EDIT_NOT_POSSIBLE_MARKERS = (
    "message can't be edited",
    "message can not be edited",
    "there is no text in the message to edit",
    "there is no caption in the message to edit",
    "message to edit not found",
)


async def telegram_edit_is_unsupported(exc: BaseException) -> bool:
    \"\"\"Whether Telegram explicitly rejects editing this message type/state.\"\"\"

    if not isinstance(exc, BadRequest):
        return False
    reason = str(exc).lower()
    return any(marker in reason for marker in _EDIT_NOT_POSSIBLE_MARKERS)


def edit_catalog_message_or_replace(
    context,
    query,
    text: str,
    **kwargs,
) -> bool:
    """Edit a catalog/theme screen, replacing it only when Telegram forbids edit."""

    message = getattr(query, "message", None)
    if message is None:
        return False

    try:
        if getattr(message, "photo", None):
            await query.edit_message_caption(caption=text, **kwargs)
        else:
            await query.edit_message_text(text, **kwargs)
        return True
    except BadRequest as exc:
        reason = str(exc).lower()
        if "message is not modified" in reason:
            return True
        if not telegram_edit_is_unsupported(exc):
            logger.warning(
                "Catalog navigation edit failed; leaving the existing message intact",
                exc_info=True,
            )
            return False
        logger.info(
            "Telegram cannot edit this catalog message; sending a replacement"
        )
    except Exception:
        logger.exception(
            "Catalog navigation edit failed; leaving the existing message intact"
        )
        return False

    chat_id = getattr(message, "chat_id", None)
    if chat_id is None:
        return False
    try:
        await context.bot.send_message(chat_id, text, **kwargs)
    except Exception:
        logger.exception(
            "Could not send catalog navigation replacement; old message kept"
        )
        return False

    try:
        await message.delete()
    except Exception:
        logger.debug(
            "Could not delete replaced catalog navigation message",
            exc_info=True,
        )
    return True
