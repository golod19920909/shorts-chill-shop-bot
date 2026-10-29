"""Product identity: canonical URLs and global deduplication.

The same listing reaches the bot more than once: AliExpress repeats items
inside one feed, and the identical item can come back through a different
affiliate/tracking link. Showing it twice wastes the user's attention and
makes the "3/20" position counter a lie, so deduplication lives here - in
the service layer - and every feed goes through it instead of each handler
solving it on its own.

Identity priority (as required by the spec). Exactly ONE identity is used
per product, the strongest one it can supply:

1. ``marketplace + product_id``  - the real identity of an item;
2. canonical product URL - ``https``, host without ``www`` and the path,
   with tracking parameters removed and the remaining query sorted, so a
   tracking/affiliate variant of a listing is the same product;
3. normalized URL - host plus path only, no scheme and no query, for a
   URL that cannot be canonicalized;
4. ``title + price + currency`` - a last resort for a product that carries
   neither an id nor a usable URL.

Weaker identities are deliberately NOT combined into an alias set: two
different listings that merely share a title and a price (perfectly common
for phone cases) are different products, and merging them would remove
legitimate results. Nothing here invents data - a missing field simply
moves the product to the next identity.
"""

from __future__ import annotations

from collections.abc import Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from models.product import Product

#: Query parameters that only ever carry tracking/session information.
#: Removing them is what makes two links to one listing compare equal.
TRACKING_PARAMETERS = frozenset(
    {
        "aff_platform",
        "aff_trace_key",
        "affiliate_id",
        "algo_expid",
        "algo_pvid",
        "btsid",
        "curpageloguid",
        "gatewayadapt",
        "pdp_npi",
        "shareid",
        "spm",
        "terminal_id",
        "ws_ab_test",
    }
)

#: Prefixes of query parameters that are always tracking (``utm_source``).
TRACKING_PARAMETER_PREFIXES = ("utm_", "aff_", "scm_")

#: Host parts that never identify an item.
_HOST_PREFIXES = ("www.", "m.")



def _split(url: str | None):
    """Parse an absolute ``http(s)`` URL, or return ``None``.

    Anything that is not a real web URL (empty, relative, another scheme)
    has no identity to contribute - callers then fall back to the next
    identity instead of comparing nonsense.
    """

    if not url:
        return None

    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        return None
    return parts


def _normalized_host(netloc: str) -> str:
    """Host without ``www``, without user info and without a default port."""

    host = netloc.rsplit("@", 1)[-1].lower().rstrip(".")
    if host.endswith(":80"):
        host = host[: -len(":80")]
    elif host.endswith(":443"):
        host = host[: -len(":443")]
    for prefix in _HOST_PREFIXES:
        if host.startswith(prefix):
            host = host[len(prefix) :]
            break
    return host


def _normalized_path(path: str) -> str:
    """Collapse repeated slashes and drop a trailing slash."""

    cleaned = "/" + "/".join(part for part in path.split("/") if part)
    return cleaned if cleaned != "/" else ""


def _is_tracking(name: str) -> bool:
    lowered = name.lower()
    return lowered in TRACKING_PARAMETERS or lowered.startswith(
        TRACKING_PARAMETER_PREFIXES
    )


def canonical_product_url(url: str | None) -> str | None:
    """Tracking-free canonical form of a product URL, or ``None``.

    Two URLs are the same product when their canonical forms are equal: the
    scheme is always ``https``, the host loses ``www``, the query keeps only
    meaningful parameters (sorted, so ordering cannot matter) and the
    fragment is dropped.
    """

    parts = _split(url)
    if parts is None:
        return None

    host = _normalized_host(parts.netloc)
    if not host:
        return None

    path = _normalized_path(parts.path)
    query = sorted(
        (name, value)
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking(name)
    )

    return urlunsplit(("https", host, path, urlencode(query, doseq=False), ""))


def normalized_product_url(url: str | None) -> str | None:
    """Coarser form: host + path only, ignoring the whole query.

    Used for a URL that has no canonical form, so the path is the only
    stable part left to compare.
    """

    parts = _split(url)
    if parts is None:
        return None

    host = _normalized_host(parts.netloc)
    if not host:
        return None

    path = _normalized_path(parts.path)
    return f"{host}{path}" if path else host


def normalize_title(title: str | None) -> str:
    """Case- and whitespace-insensitive title, for the last-resort identity."""

    return " ".join((title or "").split()).casefold()


def product_identity(product: Product) -> tuple[str, str]:
    """The single strongest identity of a product, as ``(kind, value)``.

    ``kind`` names which priority level produced the identity, so tests and
    logs can state why two items were treated as one.
    """

    product_id = (product.product_id or "").strip()
    if product_id:
        return ("id", f"{_marketplace_key(product)}:{product_id}")

    canonical = canonical_product_url(product.product_url)
    if canonical:
        return ("canonical_url", canonical)

    normalized = normalized_product_url(product.product_url)
    if normalized:
        return ("normalized_url", normalized)

    title = normalize_title(product.title)
    if title:
        price = "" if product.price is None else f"{product.price:.2f}"
        currency = (product.currency or "").upper()
        return (
            "title_price",
            f"{_marketplace_key(product)}:{title}:{price}:{currency}",
        )

    # Nothing at all to compare: keep it as its own product instead of
    # merging it with every other unidentifiable item.
    return ("unique", f"unique:{id(product)}")


def deduplicate_products(products: Sequence[Product]) -> list[Product]:
    """Drop duplicate listings, keeping the first occurrence.

    Order is preserved, so a feed keeps the ranking the marketplace (or the
    local value score) produced - only repetitions disappear.
    """

    seen: set[tuple[str, str]] = set()
    unique: list[Product] = []

    for product in products:
        identity = product_identity(product)
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(product)

    return unique


def identity_pair(product: Product) -> tuple[str, str]:
    """Persistent ``(marketplace, product_id)`` pair of a product's identity.

    This is the storage form of :func:`product_identity`: products WITH
    an id contribute exactly their real ``marketplace + product_id``;
    products WITHOUT one fall back to the canonical identity (URL or
    title/price) prefixed by its kind, so the pair is still stable
    across requests and never compares titles blindly.

    Used for the per-user «🔥 Популярное» history: the same helper both
    writes a row and filters the feed, so write and read can never
    disagree about what "already shown" means.
    """

    kind, value = product_identity(product)
    if kind == "id":
        marketplace, _, product_id = value.partition(":")
        return marketplace, product_id
    return _marketplace_key(product), f"{kind}:{value}"


def _marketplace_key(product: Product) -> str:
    marketplace = product.marketplace
    return str(getattr(marketplace, "value", marketplace))

