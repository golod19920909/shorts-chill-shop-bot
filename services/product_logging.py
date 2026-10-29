"""Safe per-product INFO logging for the search pipeline.

Production logs currently carry only aggregate counts (``N raw -> M shown``),
so a bad feed cannot be diagnosed: nobody can see WHICH listings survived.
This module formats the one line per product the task asks for - rank, title,
price/currency, product id / URL - without touching ranking, retrieval or
selection logic.

Safety rules (the task explicitly forbids secrets in logs):

* only marketplace data already present on :class:`Product` is used
  (title, price, currency, product id, product URL);
* no API keys, tokens, user ids, chat ids or personal data are accepted -
  the helpers take no such arguments at all;
* titles are single-lined and truncated to :data:`MAX_TITLE_LEN`;
* per-product lines are capped by the caller's ``limit`` (default 20) and
  the remainder is summarized as ``... +N more``.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

logger = logging.getLogger(__name__)

#: Max title characters in one log line (titles are marketplace data and can
#: be long; the log keeps a readable prefix, never the whole blob).
MAX_TITLE_LEN = 90

#: Default cap for per-product lines in one log event (matches the AI feed
#: size of 20; callers may pass a smaller limit).
MAX_PRODUCTS_PER_EVENT = 20


def format_product_line(product, rank: int) -> str:
    """One safe log line for a product: rank, title, price, id/URL."""
    title = " ".join(str(getattr(product, "title", "") or "").split())
    if len(title) > MAX_TITLE_LEN:
        title = title[:MAX_TITLE_LEN].rstrip() + "…"
    price = getattr(product, "price", None)
    currency = getattr(product, "currency", None) or ""
    currency = str(currency).upper()
    if price is None:
        price_text = f"price=? {currency}".strip()
    else:
        try:
            price_text = f"{float(price):g} {currency}".strip()
        except (TypeError, ValueError):
            price_text = f"price=? {currency}".strip()
    product_id = str(getattr(product, "product_id", "") or "").strip()
    url = str(getattr(product, "product_url", "") or "").strip()
    identity = product_id or url or "?"
    line = f"#{rank} {title!r} [{price_text}] id={identity}"
    if url and url != identity:
        line += f" url={url[:160]}"
    return line


def product_lines(
    products: Sequence, *, limit: int = MAX_PRODUCTS_PER_EVENT
) -> list[str]:
    """Per-product lines for at most ``limit`` products (1-based ranks)."""
    lines: list[str] = []
    for index, product in enumerate(list(products)[: max(0, int(limit))], start=1):
        try:
            lines.append(format_product_line(product, index))
        except Exception:  # pragma: no cover - defensive: logging never breaks search
            lines.append(f"#{index} <unformattable product>")
    return lines


def log_final_pool(
    log: logging.Logger,
    products: Sequence,
    *,
    context: str,
    origin: str,
    limit: int = MAX_PRODUCTS_PER_EVENT,
) -> None:
    """INFO summary of the final product pool / Telegram list.

    ``origin`` must name which list this is (``"ai-selected"`` or
    ``"deterministic-fallback"``) so the two are never confused in logs.
    """
    items = list(products or [])
    log.info(
        "Final pool (%s, %s): %d product(s)",
        context,
        origin,
        len(items),
    )
    for line in product_lines(items, limit=limit):
        log.info("  %s: %s", origin, line)
    if len(items) > max(0, int(limit)):
        log.info("  %s: ... +%d more", origin, len(items) - max(0, int(limit)))


def log_ai_selection_outcome(
    log: logging.Logger,
    *,
    context: str,
    pool_size: int,
    chosen_ids: Sequence[str] | None,
    selected_size: int,
    fallback_reason: str | None = None,
) -> None:
    """One concise line distinguishing AI selection from fallback.

    ``fallback_reason`` is set when the deterministic list survived instead
    of the AI list (empty/invalid AI answer, exception, no selector).
    """
    if fallback_reason:
        log.info(
            "AI feed selection for %r: pool=%d -> DETERMINISTIC FALLBACK "
            "(%s; final=%d)",
            context,
            pool_size,
            fallback_reason,
            selected_size,
        )
    else:
        log.info(
            "AI feed selection for %r: pool=%d -> AI-SELECTED final=%d",
            context,
            pool_size,
            selected_size,
        )


def log_pipeline_stage_counts(
    log: logging.Logger,
    context: str,
    stages: Sequence[tuple[str, int, int]],
) -> None:
    """Concise per-stage diagnostics: ``(stage, entered, left)`` triples."""
    parts = []
    for name, entered, left in stages:
        try:
            dropped = max(0, int(entered) - int(left))
        except (TypeError, ValueError):
            continue
        parts.append(f"{name}: {entered}->{left} (dropped {dropped})")
    if parts:
        log.info("Pipeline stages for %r: %s", context, "; ".join(parts))
