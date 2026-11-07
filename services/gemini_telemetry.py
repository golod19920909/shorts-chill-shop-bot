"""Measurement-only Gemini usage instrumentation and durable event handoff.

Every remote model attempt is recorded exactly once at the lowest layer that
issues it. Token counts come only from response.usage_metadata. Persistence is
best-effort and must never interrupt a user-facing bot operation.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import wraps
from typing import Awaitable, Callable
from uuid import uuid4

from services.gemini_pricing import estimate_cost_usd

logger = logging.getLogger(__name__)

_TOKEN_FIELDS = (
    "prompt_token_count",
    "candidates_token_count",
    "total_token_count",
    "cached_content_token_count",
    "thoughts_token_count",
)


def extract_usage(response: object) -> dict[str, int] | None:
    """Read only numeric usage fields actually returned by the SDK."""

    metadata = getattr(response, "usage_metadata", None)
    if metadata is None:
        return None
    tokens: dict[str, int] = {}
    for name in _TOKEN_FIELDS:
        value = getattr(metadata, name, None)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            continue
        tokens[name] = value
    return tokens or None


@dataclass
class GeminiAttempt:
    """One remote model attempt exactly as it was issued."""

    operation: str
    model: str
    duration_ms: float
    outcome: str
    tokens: dict[str, int] | None = None
    attempt_id: str = field(default_factory=lambda: uuid4().hex)
    request_id: str | None = None
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    cost_usd: float | None = None
    cost_status: str = "unknown_usage"
    pricing_version: str | None = None

    def to_record(self) -> dict[str, object]:
        tokens = self.tokens or {}
        return {
            "attempt_id": self.attempt_id,
            "request_id": self.request_id,
            "operation": self.operation,
            "model": self.model,
            "duration_ms": self.duration_ms,
            "outcome": self.outcome,
            "prompt_token_count": tokens.get("prompt_token_count"),
            "candidates_token_count": tokens.get("candidates_token_count"),
            "total_token_count": tokens.get("total_token_count"),
            "cached_content_token_count": tokens.get("cached_content_token_count"),
            "thoughts_token_count": tokens.get("thoughts_token_count"),
            "cost_usd": self.cost_usd,
            "cost_status": self.cost_status,
            "pricing_version": self.pricing_version,
            "created_at": self.created_at,
        }


@dataclass
class TurnUsage:
    """Aggregate of attempts attributed to one Telegram update."""

    request_id: str = field(default_factory=lambda: uuid4().hex)
    attempts: list[GeminiAttempt] = field(default_factory=list)

    @property
    def tokens_partial(self) -> bool:
        return any(attempt.tokens is None for attempt in self.attempts)

    def totals(self) -> dict[str, int]:
        totals: dict[str, int] = {}
        for attempt in self.attempts:
            if not attempt.tokens:
                continue
            for name, value in attempt.tokens.items():
                totals[name] = totals.get(name, 0) + value
        return totals

    def summary(self) -> str:
        if not self.attempts:
            return ""
        totals = self.totals()
        token_part = " ".join(
            f"{name}={value}" for name, value in sorted(totals.items())
        )
        known_costs = [a.cost_usd for a in self.attempts if a.cost_usd is not None]
        cost_part = (
            f" estimated_cost_usd={sum(known_costs):.8f}"
            f" cost_unknown={len(self.attempts) - len(known_costs)}"
        )
        return (
            f"request_id={self.request_id} attempts={len(self.attempts)}"
            f" partial={str(self.tokens_partial).lower()}"
            + (f" {token_part}" if token_part else "")
            + cost_part
        )


_current_turn: contextvars.ContextVar[TurnUsage | None] = contextvars.ContextVar(
    "gemini_turn_usage", default=None
)
_attempt_writer: Callable[[dict[str, object]], Awaitable[None]] | None = None
_pending_writes: set[asyncio.Task] = set()


class turn_scope:
    """Context-scoped collector for all Gemini attempts in one handler."""

    def __init__(self, turn: str) -> None:
        self.turn = turn
        self._token = None
        self.usage = TurnUsage()

    def __enter__(self) -> TurnUsage:
        self._token = _current_turn.set(self.usage)
        return self.usage

    def __exit__(self, exc_type, exc, tb) -> None:
        _current_turn.reset(self._token)
        summary = self.summary()
        if summary:
            logger.info("gemini_usage turn=%s %s", self.turn, summary)

    def summary(self) -> str:
        return self.usage.summary()


def track_usage(turn: str):
    """Decorate an async Telegram handler with one request-scoped collector."""

    def decorate(handler):
        @wraps(handler)
        async def wrapped(*args, **kwargs):
            with turn_scope(turn):
                return await handler(*args, **kwargs)

        return wrapped

    return decorate


def current_collector() -> TurnUsage | None:
    return _current_turn.get()


def configure_persistence(
    writer: Callable[[dict[str, object]], Awaitable[None]] | None,
) -> None:
    """Install the async database writer after the application's DB is ready."""

    global _attempt_writer
    _attempt_writer = writer


async def _persist_attempt(attempt: GeminiAttempt) -> None:
    writer = _attempt_writer
    if writer is None:
        return
    try:
        await writer(attempt.to_record())
    except Exception:
        # Analytics is strictly best-effort; a database issue cannot break AI.
        logger.warning("Failed to persist Gemini usage attempt", exc_info=True)


def _schedule_persistence(attempt: GeminiAttempt) -> None:
    if _attempt_writer is None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("Gemini usage persistence skipped: no running event loop")
        return
    task = loop.create_task(_persist_attempt(attempt))
    _pending_writes.add(task)
    task.add_done_callback(_pending_writes.discard)


async def flush_pending_attempts() -> None:
    """Wait for queued best-effort writes before database shutdown."""

    while _pending_writes:
        await asyncio.gather(*tuple(_pending_writes), return_exceptions=True)


def record_attempt(
    *,
    operation: str,
    model: str,
    duration_ms: float,
    outcome: str,
    response: object = None,
    error: bool = False,
) -> GeminiAttempt:
    """Record one remote attempt, log it, and queue one durable DB write."""

    tokens = None if error else extract_usage(response)
    cost_usd, cost_status, pricing_version = estimate_cost_usd(model, tokens)
    collector = current_collector()
    attempt = GeminiAttempt(
        operation=operation,
        model=model,
        duration_ms=duration_ms,
        outcome=outcome,
        tokens=tokens,
        request_id=collector.request_id if collector is not None else None,
        cost_usd=cost_usd,
        cost_status=cost_status,
        pricing_version=pricing_version,
    )

    logger.info(
        "gemini_attempt request_id=%s op=%s model=%s dur_ms=%.0f "
        "outcome=%s prompt=%s candidates=%s thoughts=%s total=%s "
        "cost_usd=%s cost_status=%s",
        attempt.request_id or "unscoped",
        operation,
        model,
        duration_ms,
        outcome,
        (tokens or {}).get("prompt_token_count", "unavailable"),
        (tokens or {}).get("candidates_token_count", "unavailable"),
        (tokens or {}).get("thoughts_token_count", "unavailable"),
        (tokens or {}).get("total_token_count", "unavailable"),
        f"{cost_usd:.8f}" if cost_usd is not None else "unknown",
        cost_status,
    )

    if collector is not None:
        collector.attempts.append(attempt)
    _schedule_persistence(attempt)
    return attempt
