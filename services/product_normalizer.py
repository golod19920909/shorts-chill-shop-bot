"""Shared helpers for converting raw marketplace API values into the
common :class:`~models.product.Product` model.

Every provider must use these helpers instead of its own ad-hoc
conversions, so that:
- a missing/invalid field becomes ``None`` instead of a guessed value
  or a crash;
- price/rating/ordering fields are consistently typed and rounded.

Nothing in this module performs filtering or ranking - that is
``services.comparison_service``'s job and must stay programmatic.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_NUMBER_RE = re.compile(r"-?\d+(?:[.,]\d+)?")

#: Characters marketplaces hide inside titles: a UTF-8 BOM/ZWNBSP glued to
#: the first character, zero-width joiners/spaces between words, bidi marks
#: and soft hyphens. They are invisible, so showing them wastes the user's
#: attention and breaks everything that compares titles: ``strip()`` does
#: NOT remove them (they are not whitespace), which is why a title that
#: arrived as ``"\ufeff Штаны"`` used to be a DIFFERENT product for the
#: last-resort identity (``title + price + currency``) and survived
#: deduplication as a duplicate.
#:
#: ``U+200D`` (zero-width joiner) is removed too: listings use it to glue a
#: brand name into an unsearchable/uncomparable blob, and a title is data,
#: not an emoji sequence - the worst case is that a multi-person emoji in a
#: title renders as its separate parts.
_INVISIBLE_RE = re.compile(
    "[\u00ad\u180e\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]"
)

#: Any run of whitespace, including the exotic space characters a payload
#: may use instead of ``U+0020`` (no-break, narrow no-break, ideographic)
#: and real newlines/tabs. Collapsing a run to ONE plain space keeps a title
#: on a single predictable line and, just as importantly, prevents the
#: removal of a zero-width character from leaving a visible double space.
_WHITESPACE_RE = re.compile(r"\s+")


def clean_text(value: Any) -> str | None:
    """Return a stripped string, or ``None`` for empty/missing values.

    Normalization steps, all of which only remove or re-code characters
    that carry no visible meaning:

    - invisible formatting characters are deleted;
    - any run of whitespace becomes one plain space;
    - the result is stripped, and an empty result becomes ``None``.

    No value is ever invented and visible text is never altered.
    """

    if value is None:
        return None
    text = _WHITESPACE_RE.sub(" ", _INVISIBLE_RE.sub("", str(value)))
    return text.strip() or None


def to_float(value: Any) -> float | None:
    """Best-effort conversion of a marketplace value to ``float``.

    Handles numeric types and strings such as ``"19.99"``, ``"$19.99"``
    or ``"19,99 USD"``. Returns ``None`` when nothing parseable is
    found - never a default like ``0``, which would silently distort
    price comparisons.
    """

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)

    match = _NUMBER_RE.search(str(value).replace(",", "."))
    if not match:
        logger.debug("Could not parse float from value: %r", value)
        return None
    try:
        return float(match.group(0))
    except ValueError:  # pragma: no cover - defensive
        return None


def to_int(value: Any) -> int | None:
    """Convert a marketplace value to ``int``, or ``None`` if not numeric."""

    number = to_float(value)
    if number is None:
        return None
    return int(number)


def compute_discount_percent(
    price: float | None, old_price: float | None
) -> float | None:
    """Calculate a discount percentage from price and old price.

    Returns ``None`` unless both values are usable and the old price is
    strictly greater than the current price (a "discount" of 0% is not
    treated as a discount).
    """

    if price is None or old_price is None:
        return None
    if old_price <= 0 or price >= old_price:
        return None
    return round((old_price - price) / old_price * 100, 1)


def get_first(data: dict[str, Any], *keys: str) -> Any:
    """Return the first present, non-empty value among ``keys``.

    Marketplace payloads are inconsistent (``price`` vs ``sale_price``
    vs ``min_price``), so providers list candidate keys instead of
    assuming one shape.
    """

    for key in keys:
        value = data.get(key)
        if value is not None and value != "":
            return value
    return None
