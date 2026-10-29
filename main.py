"""Entry point for the AI product-search Telegram bot.

This module only handles process lifecycle: logging setup, building the
application and running polling. All wiring lives in ``bot``.
"""

from __future__ import annotations

import logging
import sys

from bot import build_application
from config.settings import settings
from utils.logging import setup_logging

logger = logging.getLogger(__name__)


def main() -> int:
    """Start the bot in long polling mode."""

    setup_logging(settings.logging_level)

    if not settings.is_bot_token_configured:
        logger.error(
            "BOT_TOKEN is missing. Copy .env.example to .env and set BOT_TOKEN "
            "to the token received from @BotFather."
        )
        return 1

    if not settings.is_gemini_configured:
        logger.warning(
            "GEMINI_API_KEY is not set: natural-language parsing falls back to "
            "the local parser. The bot still starts."
        )

    logger.info("Starting bot (long polling). Press Ctrl+C to stop.")

    try:
        application = build_application(settings)
    except Exception:
        logger.exception("Failed to create the application")
        return 1

    try:
        application.run_polling(drop_pending_updates=True)
    except KeyboardInterrupt:  # pragma: no cover - manual stop
        logger.info("Bot stopped by user")
    except Exception:
        logger.exception("Bot stopped because of an unexpected error")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
