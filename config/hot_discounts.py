"""Static configuration of the "🔥 Горячие скидки" sections.

The FINAL spec fixes exactly four affiliate destinations with exactly
these URLs (verified: they are the intended partner links provided by the
project). The bot never modifies, appends parameters to, scrapes or
reconstructs them, and never invents an additional promotional URL
(no Flash Deals, no Big Save).

Every section always has a URL, so the keyboard renders four direct URL
buttons - no placeholder flow exists any more. ``get_section`` stays for
stale ``hdis:<code>`` callbacks pressed on messages sent by an earlier
version of the screen.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HotDiscountSection:
    """One AliExpress discount destination (always with its real URL)."""

    code: str
    title: str
    url: str


HOT_DISCOUNT_SECTIONS: tuple[HotDiscountSection, ...] = (
    HotDiscountSection(
        "super", "\u26A1 Суперскидки", "https://s.click.aliexpress.com/e/_c4pP6ZHr"
    ),
    HotDiscountSection(
        "brands", "\U0001F3F7 Скидки на бренды", "https://s.click.aliexpress.com/e/_c2RTdyRJ"
    ),
    HotDiscountSection(
        "bundles", "\U0001F4E6 Наборы товаров", "https://s.click.aliexpress.com/e/_c4DCA0fR"
    ),
    HotDiscountSection(
        "coins", "\U0001FA99 Скидки за монеты", "https://s.click.aliexpress.com/e/_c3wu8oz7"
    ),
)


def get_section(code: str) -> HotDiscountSection | None:
    """Resolve a section by its short callback code (stale-button safety)."""

    for section in HOT_DISCOUNT_SECTIONS:
        if section.code == code:
            return section
    return None
