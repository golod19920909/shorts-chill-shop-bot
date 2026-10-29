"""Measurement-only Gemini usage instrumentation (no behavior change).

Every application-issued remote model attempt is recorded exactly once, at
the lowest layer that issues it: :meth:`AIService._generate` and the agent
loop's direct ``generate_content`` call. Aggregates per search/turn are
context-scoped via :class:`contextvars.ContextVar`, so concurrent Telegram
users and concurrent searches never mix accounting.

Design rules:

* A remote attempt counts ONCE - primary + fallback-model retry is two
  attempts, each agent round is one attempt. Nothing is counted again at a
  higher layer.
* Token counts come ONLY from ``response.usage_metadata``; missing metadata
  is recorded as ``None`` and never guessed (a timeout may still have
  consumed billable tokens, so it is recorded with ``tokens=None``).
* No prompts, request contents, credentials or personal data are recorded.
"""

from __future__ import annotations

import contextvars
import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: Fields we look for on ``response.usage_metadata`` (google-genai 2.24.0).
#: Each is recorded only when present and numeric - a missing field stays
#: ``None`` in the record and is simply omitted from aggregates.
_TOKEN_FIELDS = (
    "prompt_token_count",
    "candidates_token_count",
    "total_token_count",
    "cached_content_token_count",
    "thoughts_token_count",
)


def extract_usage(response: object) -> dict[str, int] | None:
    """Defensively read token fields off ``response.usage_metadata``.

    Returns ``None`` when the SDK response carries no usable metadata at
    all; otherwise a dict with only the fields actually present. Numbers
    are never invented and non-integers are dropped.
    """

    metadata = getattr(response, "usage_metadata", None)
    if metadata is None:
        return None
    tokens: dict[str, int] = {}
    for name in _TOKEN_FIELDS:
        value = getattr(metadata, name, None)
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        if value < 0:
            continue
        tokens[name] = value
    return tokens or None


@dataclass
class GeminiAttempt:
    """One remote model attempt exactly as it was issued."""

    operation: str
    model: str
    duration_ms: float
    outcome: str  # "ok" | "error" | "timeout" | "empty"
    tokens: dict[str, int] | None = None  # None = metadata unavailable


@dataclass
class TurnUsage:
    """Aggregate of the attempts attributed to one search/turn."""

    attempts: list[GeminiAttempt] = field(default_factory=list)

    @property
    def tokens_partial(self) -> bool:
        """True when at least one attempt lacks usage metadata."""
        return any(attempt.tokens is None for attempt in self.attempts)

    def totals(self) -> dict[str, int]:
        """Sum of token fields over attempts that reported them."""
        totals: dict[str, int] = {}
        for attempt in self.attempts:
            if not attempt.tokens:
                continue
            for name, value in attempt.tokens.items():
                totals[name] = totals.get(name, 0) + value
        return totals

    def summary(self) -> str:
        """One-line aggregate; empty string when nothing was attempted."""
        if not self.attempts:
            return ""
        totals = self.totals()
        token_part = ""
        if totals:
            fields = " ".join(
                f"{name}={value}" for name, value in sorted(totals.items())
            )
            token_part = " " + fields
        return (
            f"attempts={len(self.attempts)}"
            f" partial={str(self.tokens_partial).lower()}{token_part}"
        )


#: The collector for the turn currently being served. ``None`` outside a
#: :func:`turn_scope` - attempts are still logged per-attempt, just not
#: aggregated. Each async task inherits the var value it was started with,
#: so nested agent tool calls that run inside the same task chain see the
#: same collector without any explicit plumbing.
_current_turn: contextvars.ContextVar[TurnUsage | None] = contextvars.ContextVar(
    "gemini_turn_usage", default=None
)


class turn_scope:
    """Async-context-scoped accounting for one search/turn.

    Usage::

        with turn_scope("search"):
            ... await the whole search/turn ...

    On exit it logs ONE structured summary with the attempt count and the
    available token aggregates (marked ``partial`` when any attempt had no
    usage metadata). Nested scopes are supported for tests; the innermost
    collector receives the attempts issued inside it.
    """

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
            logger.info("gemini_usage %s", summary)

    def summary(self) -> str:
        """One-line aggregate; empty string when nothing was attempted."""
        usage = self.usage
        if not usage.attempts:
            return ""
        totals = usage.totals()
        token_part = ""
        if totals:
            fields = " ".join(
                f"{name}={value}" for name, value in sorted(totals.items())
            )
            token_part = " " + fields
        return (
            f"turn={self.turn} attempts={len(usage.attempts)}"
            f" partial={str(usage.tokens_partial).lower()}{token_part}"
        )


def current_collector() -> TurnUsage | None:
    """The active turn collector, or ``None`` outside any turn scope."""
    return _current_turn.get()


def record_attempt(
    *,
    operation: str,
    model: str,
    duration_ms: float,
    outcome: str,
    response: object = None,
    error: bool = False,
) -> None:
    """Record one remote attempt: always a per-attempt log line, and add it
    to the active turn collector when one is open.

    ``response`` is the SDK response object (its ``usage_metadata`` is read
    defensively); on failure paths leave it ``None`` - no tokens are then
    claimed, and the aggregate is correctly marked partial.
    """

    tokens = None if error else extract_usage(response)
    attempt = GeminiAttempt(
        operation=operation,
        model=model,
        duration_ms=duration_ms,
        outcome=outcome,
        tokens=tokens,
    )
    logger.info(
        "gemini_attempt op=%s model=%s dur_ms=%.0f outcome=%s tokens=%s",
        operation,
        model,
        duration_ms,
        outcome,
        tokens if tokens is not None else "unavailable",
    )
    collector = current_collector()
    if collector is not None:
        collector.attempts.append(attempt)
