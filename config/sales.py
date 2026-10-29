"""Reusable configuration of AliExpress sales events (e.g. «Чёрная пятница»).

This module is the STATIC, code-level half of the promotional section: which
events exist, their internal id and user-facing title, and the pure helpers
that turn a stored value/URL into a safe decision. It contains NO secrets and
NO operational state.

The other, operational half - whether an event is enabled and the affiliate
destination URL - lives in the bot's own persistent database (the ``bot_config``
key/value table) so an administrator can flip it from the command line without
a code change, a redeploy or a restart (see ``manage_sales.py``). Keeping the
two halves apart is what makes the section reusable: a future 11.11 / 12.12 /
seasonal sale reuses every handler and screen here and only needs a new entry
in :data:`SALE_EVENTS` plus a runtime config flip - never a second search
engine or a copied set of handlers.

Nothing here ever invents a discount, a price or a deal: the first version is a
promotional landing section that opens the administrator-configured affiliate
destination. When that URL is not (yet) configured the section shows a clear,
friendly message instead of a broken link.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class SaleEvent:
    """Static description of one reusable sales event.

    Only the operational settings are runtime data; an event's identity
    (``event_id``) and its user-facing ``title`` are fixed in code, which is
    why a new sale reuses this dataclass instead of adding a new module.
    """

    #: Stable internal identifier used in callbacks and config keys.
    event_id: str
    #: User-facing event title shown on the menu button and the section screen.
    title: str


#: The registry of known sales events, keyed by internal id. Adding a future
#: event means adding ONE entry here; every handler and screen already works
#: with any event in this registry.
SALE_EVENTS: dict[str, SaleEvent] = {
    "black_friday": SaleEvent("black_friday", "Чёрная пятница"),
}

#: The event the standalone sales section currently represents. Future events
#: reuse the same infrastructure and only change this default / registry.
DEFAULT_EVENT_ID = "black_friday"

#: Values an administrator (or a config file) may use to mean "enabled".
_ENABLED_VALUES = frozenset({"1", "true", "yes", "on"})


def get_event(event_id: str) -> SaleEvent | None:
    """Resolve an internal event id into its :class:`SaleEvent` (or ``None``)."""

    return SALE_EVENTS.get(event_id)


def enabled_key(event_id: str) -> str:
    """The ``bot_config`` key holding an event's enabled/disabled state."""

    return f"sale.{event_id}.enabled"


def url_key(event_id: str) -> str:
    """The ``bot_config`` key holding an event's affiliate destination URL."""

    return f"sale.{event_id}.url"


def is_enabled_value(value: str | None) -> bool:
    """Interpret a stored flag as enabled/disabled, defaulting to DISABLED.

    Fail-safe by design: a missing, empty or unrecognised value means the
    section stays hidden, so an ordinary user never sees it by accident.
    """

    return (value or "").strip().lower() in _ENABLED_VALUES


def safe_http_url(url: str | None) -> str | None:
    """Validate a destination URL, allowing only well-formed http/https.

    Returns the cleaned URL when it is a usable http(s) destination with a
    host, and ``None`` otherwise. Unsafe or malformed schemes (``javascript:``,
    ``data:``, ``ftp:`` …), empty values and host-less URLs are all rejected,
    so the section can never open a dangerous or broken link.
    """

    if not url:
        return None
    candidate = url.strip()
    if not candidate:
        return None
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return None
    if parsed.scheme.lower() not in ("http", "https"):
        return None
    if not parsed.netloc:
        return None
    return candidate