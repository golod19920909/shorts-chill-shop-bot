"""Helpers for formatting normalized products as Telegram-friendly text.

These helpers only format data that already exists on a ``Product``
instance. They never invent missing values - if a field is ``None``, the
output omits it instead of guessing.

Deliberate product decision (see the UX spec): the product card does NOT
show shipping cost. Shipping depends on the destination and is not
reliably available from every API, so displaying it could mislead users.
``format_shipping`` is kept for price tracking, where the value is shown
with its own caveats.
"""

from __future__ import annotations

from urllib.parse import quote, urlsplit

from models.product import Availability, Product

_UNKNOWN = "нет данных"

#: Shown on result screens so a price is never read as a checkout price.
PRICE_NOTE = "Цена — из данных маркетплейса на момент поиска."

#: Telegram caption limit for photos.
CAPTION_LIMIT = 1024

_AVAILABILITY_LABELS = {
    Availability.IN_STOCK: "в наличии",
    Availability.OUT_OF_STOCK: "нет в наличии",
}

_MARKETPLACE_LABELS = {
    "aliexpress": "AliExpress",
}


def format_marketplace_name(marketplace: str) -> str:
    return _MARKETPLACE_LABELS.get(marketplace, marketplace.title())


def format_price(price: float | None, currency: str | None) -> str:
    if price is None:
        return _UNKNOWN
    currency_part = f" {currency}" if currency else ""
    return f"{price:.2f}{currency_part}"


def format_rating(rating: float | None) -> str:
    if rating is None:
        return _UNKNOWN
    return f"⭐ {rating:.1f}"


def format_orders(orders_count: int | None) -> str:
    if orders_count is None:
        return _UNKNOWN
    return f"{orders_count} заказ(ов)"


def format_shipping(shipping_cost: float | None, currency: str | None) -> str:
    if shipping_cost is None:
        return _UNKNOWN
    if shipping_cost == 0:
        return "бесплатно"
    currency_part = f" {currency}" if currency else ""
    return f"{shipping_cost:.2f}{currency_part}"


def format_total_cost(product: Product) -> str:
    total = product.total_known_cost
    if total is None:
        return _UNKNOWN
    currency_part = f" {product.currency}" if product.currency else ""
    return f"{total:.2f}{currency_part}"


def format_product_card(product: Product, index: int | None = None) -> str:
    """Render a single product as a Telegram message (HTML-safe plain text).

    Callers using HTML parse mode should escape ``product.title`` /
    ``product.seller_name`` themselves if they contain HTML-sensitive
    characters; this function keeps formatting logic separate from
    Telegram-specific escaping.
    """

    prefix = f"{index}. " if index is not None else ""
    marketplace = (
        product.marketplace.value
        if hasattr(product.marketplace, "value")
        else str(product.marketplace)
    )

    lines: list[str] = []
    lines.extend([
        f"{prefix}{product.title}",
        f"\U0001F3EC {format_marketplace_name(marketplace)}",
        f"\U0001F4B0 {format_price(product.price, product.currency)}",
    ])

    if product.rating is not None:
        lines.append(f"\u2B50 \u0420\u0435\u0439\u0442\u0438\u043d\u0433: {product.rating:.1f}")
    if product.positive_rate is not None:
        # Reported only when the marketplace really returned it. Kept
        # separate from "Рейтинг": a share of positive feedback is not a
        # 0-5 star rating and is never converted into one.
        lines.append(
            f"\U0001F44D \u041f\u043e\u043b\u043e\u0436\u0438\u0442\u0435\u043b\u044c\u043d\u044b\u0435 \u043e\u0442\u0437\u044b\u0432\u044b: "
            f"{product.positive_rate:.0f}%"
        )
    if product.orders_count is not None:
        lines.append(f"\U0001F9FE \u0417\u0430\u043a\u0430\u0437\u043e\u0432: {product.orders_count}")

    availability = format_availability(product)
    if availability:
        lines.append(f"\u2705 {availability}")

    if product.discount_percent:
        lines.append(f"\U0001F525 \u0421\u043a\u0438\u0434\u043a\u0430: {product.discount_percent:.0f}%")

    if product.promo_code:
        # Verbatim marketplace promo data (see ``extract_promo_info``):
        # the code, then the marketplace's own benefit sentence (the
        # real discount amount and minimum order) and the real validity
        # end - each line present ONLY when the API provided it.
        lines.append(f"\U0001F39F \u041f\u0440\u043e\u043c\u043e\u043a\u043e\u0434: {product.promo_code}")
        if product.promo_benefit:
            lines.append(f"   {product.promo_benefit}")
        if product.promo_valid_until:
            lines.append(
                f"   \u0414\u0435\u0439\u0441\u0442\u0432\u0443\u0435\u0442 \u0434\u043e: {product.promo_valid_until}"
            )
    elif product.promo_benefit:
        # A promo condition without a code is still real marketplace
        # text - shown without claiming a code exists.
        lines.append(f"\U0001F39F {product.promo_benefit}")

    return "\n".join(lines)


def format_availability(product: Product) -> str | None:
    """Availability label, or ``None`` when the marketplace did not say."""

    if product.availability is Availability.UNKNOWN:
        return None
    return _AVAILABILITY_LABELS.get(product.availability)


def format_share_text(product: Product, url: str | None = None) -> str:
    """Text used when a user shares a product with another chat.

    Contains only real fields plus the link that opens the product, so a
    shared card always leads back to the same listing.
    """

    link = url or product.best_url
    marketplace = (
        product.marketplace.value
        if hasattr(product.marketplace, "value")
        else str(product.marketplace)
    )
    parts = [
        product.title,
        f"\U0001F4B0 {format_price(product.price, product.currency)}",
        format_marketplace_name(marketplace),
    ]
    if product.rating is not None:
        parts.insert(2, f"\u2B50 \u0420\u0435\u0439\u0442\u0438\u043d\u0433: {product.rating:.1f}")
    return "\n".join(parts) + f"\n{link}"


def build_share_url(product: Product) -> str:
    """Official Telegram share link for a product.

    Uses Telegram's ``t.me/share`` link (``share`` is a reserved path in
    Telegram's deep-link documentation, so this is not an invented URL).
    The link opens Telegram's chat picker and pre-fills the product text
    with the URL that opens the listing. No bot inline mode is required,
    which is why this is preferred over ``switch_inline_query``.
    """

    text = format_share_text(product)
    return (
        "https://t.me/share/url"
        f"?url={quote(product.best_url, safe='')}&text={quote(text, safe='')}"
    )


def is_openable_url(url: str | None) -> bool:
    """Whether Telegram would accept this URL in an inline button.

    Telegram validates every button URL when a message is sent or edited and
    rejects the WHOLE message with ``Bad Request: ... URL is invalid / Text
    buttons are not allowed`` when one is unusable. Verified live against the
    Bot API: an empty string and a relative path are both refused, while a
    long (2816-character) tracking link is accepted. Callers must therefore
    only ever put a URL into a button when this returns ``True``.
    """

    value = (url or "").strip()
    if not value:
        return False
    parts = urlsplit(value)
    if parts.scheme.lower() not in {"http", "https", "tg"}:
        return False
    # ``tg://`` links carry the target in the netloc, ``https://`` needs a host.
    return bool(parts.netloc)


def share_button_url(product: Product) -> str | None:
    """The share link for ``product``, or ``None`` when it cannot be built.

    A product whose stored URLs are missing (a legacy favorite saved before
    the provider returned links, for example) has nothing to share: the
    caller must then render the card WITHOUT the share button instead of
    sending Telegram a button it will reject.
    """

    share_url = build_share_url(product) if is_openable_url(product.best_url) else None
    return share_url if is_openable_url(share_url) else None


def truncate_for_caption(text: str) -> str:
    """Clip text to Telegram's photo-caption limit."""

    if len(text) <= CAPTION_LIMIT:
        return text
    return text[: CAPTION_LIMIT - 1] + "\u2026"
