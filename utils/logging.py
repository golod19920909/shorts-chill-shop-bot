"""Application-wide logging setup.

Rules enforced here:
- secrets (BOT_TOKEN, GEMINI_API_KEY, marketplace app secrets) must
  never appear in log output, even by accident (e.g. someone logging
  an exception that contains a request URL with a token in it).
"""

from __future__ import annotations

import logging
import re
import sys

_SECRET_PATTERNS = [
    re.compile(r"(bot\d+:)[A-Za-z0-9_-]{20,}", re.IGNORECASE),  # Telegram bot tokens
    re.compile(r"(api[_-]?key\s*[=:]\s*)[^\s&\"']+", re.IGNORECASE),
    re.compile(r"(app[_-]?secret\s*[=:]\s*)[^\s&\"']+", re.IGNORECASE),
    re.compile(r"(secret\s*[=:]\s*)[^\s&\"']+", re.IGNORECASE),
]

_MASK = "***"


class SafeLogBuffer(logging.Handler):
    """Ring buffer of masked log lines for the admin logs screen (§51).

    Every record passes :class:`SecretMaskingFilter` before formatting, so
    a token that somehow reached a log message never reaches the screen.
    Stack traces are NOT stored - only the formatted single line.
    """

    def __init__(self, capacity: int = 200) -> None:
        super().__init__()
        from collections import deque

        self.records: deque[str] = deque(maxlen=capacity)
        self.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        self.addFilter(SecretMaskingFilter())

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.records.append(self.format(record))
        except Exception:  # noqa: BLE001 - a log buffer must never crash logging
            pass


def get_recent_logs(page: int = 0, per_page: int = 10) -> tuple[list[str], int]:
    """Newest-first page of masked log lines: ``(lines, total)``."""

    everything = list(LOG_BUFFER.records)
    total = len(everything)
    newest_first = everything[::-1]
    start = max(0, page) * per_page
    return newest_first[start : start + per_page], total


def mask_secret(value: str | None) -> str:
    """Return a masked placeholder for a secret value.

    Use this instead of ever printing a real token/key, including in
    debug logs or error messages.
    """

    if not value:
        return "<empty>"
    return _MASK


class SecretMaskingFilter(logging.Filter):
    """Scrubs known secret patterns out of log messages defensively."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - defensive only
            return True

        masked = message
        for pattern in _SECRET_PATTERNS:
            masked = pattern.sub(lambda m: m.group(1) + _MASK, masked)

        if masked != message:
            record.msg = masked
            record.args = ()

        return True


#: Process-wide buffer; attached to the root logger by ``setup_logging``.
#: Instantiated AFTER ``SecretMaskingFilter`` is defined (the buffer's
#: ``__init__`` attaches one).
LOG_BUFFER = SafeLogBuffer()


def setup_logging(level: int = logging.INFO) -> None:
    """Configure root logging once, at application startup."""

    root = logging.getLogger()

    # The admin log buffer is attached exactly once, independently of
    # the stream handler below (tests may import without setup_logging).
    if not any(isinstance(h, SafeLogBuffer) for h in root.handlers):
        root.addHandler(LOG_BUFFER)

    if any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, SafeLogBuffer)
        for h in root.handlers
    ):
        # Already configured (e.g. re-imported in tests) - avoid duplicate handlers.
        return

    root.setLevel(level)

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    handler.addFilter(SecretMaskingFilter())
    root.addHandler(handler)

    # Quiet down noisy third-party libraries by default.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.INFO)
