"""Marketplace-independent product model.

This model is the single common representation that every
marketplace provider must convert its raw API response into. Gemini
never produces instances of this model from scratch - it only ever
receives already-normalized ``Product`` instances for comparison, or
helps build a ``SearchRequest`` used to *ask* providers for them.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class Marketplace(str, Enum):
    """Known marketplace identifiers.

    Adding a new marketplace later means adding a new value here and
    a new provider - nothing else in this module changes.

    Removed marketplaces (Temu, Alibaba) are deliberately absent: a
    stored snapshot that still carries one of those values no longer
    validates, so legacy records are skipped instead of being displayed
    as if they were still available.
    """

    ALIEXPRESS = "aliexpress"


class Availability(str, Enum):
    """Stock availability, when a marketplace exposes it."""

    IN_STOCK = "in_stock"
    OUT_OF_STOCK = "out_of_stock"
    UNKNOWN = "unknown"


class Product(BaseModel):
    """A single product normalized from any marketplace.

    Only ``marketplace``, ``product_id``, ``title`` and ``product_url``
    are required - everything else may legitimately be missing
    depending on what a given marketplace API exposes, and must be
    left as ``None`` rather than guessed.
    """

    model_config = ConfigDict(use_enum_values=False, extra="forbid")

    marketplace: Marketplace
    product_id: str
    title: str
    product_url: str

    description: str | None = None
    image_url: str | None = None

    price: float | None = Field(default=None, ge=0)
    currency: str | None = None
    old_price: float | None = Field(default=None, ge=0)
    discount_percent: float | None = Field(default=None, ge=0, le=100)

    rating: float | None = Field(default=None, ge=0, le=5)
    orders_count: int | None = Field(default=None, ge=0)

    #: Share of positive feedback in percent, when the marketplace reports
    #: it (AliExpress: ``evaluate_rate``). This is deliberately NOT
    #: ``rating``: it is a different metric, so it is shown under its own
    #: name instead of being converted into stars.
    positive_rate: float | None = Field(default=None, ge=0, le=100)

    shipping_cost: float | None = Field(default=None, ge=0)
    delivery_estimate: str | None = None

    seller_name: str | None = None

    affiliate_url: str | None = None

    #: Promo code attached to a product by the marketplace, when the
    #: affiliate API reports one (``promo_code_info``). It is stored and
    #: shown verbatim: the field's exact format is not documented, so
    #: nothing is parsed out of it and no code is ever invented.
    promo_code: str | None = None

    #: The marketplace's own benefit sentence of that promo code
    #: (``code_value`` of ``promo_code_info``), verbatim - the one field
    #: where AliExpress states the discount amount and the minimum order
    #: together with its currency (e.g. "On order over USD 104.7, get
    #: USD 10.48 off"). Shown only when the API provided it.
    promo_benefit: str | None = None

    #: Real validity end of the promo code (``code_availabletime_end``),
    #: verbatim. Never derived, never guessed; ``None`` when absent.
    promo_valid_until: str | None = None

    availability: Availability = Availability.UNKNOWN

    @property
    def best_url(self) -> str:
        """The URL to open when the user presses "🛒 Купить".

        The affiliate URL is used only when a provider really produced
        one for THIS product (see ``generate_affiliate_link``). The bot
        never fabricates affiliate links, so falling back to the plain
        marketplace URL is the honest default.
        """

        return self.affiliate_url or self.product_url

    @property
    def has_affiliate_url(self) -> bool:
        """Whether this product actually carries an affiliate link."""

        return bool(self.affiliate_url)

    @property
    def total_known_cost(self) -> float | None:
        """Price + shipping, when both are known.

        This intentionally returns ``None`` (not a guess) if either
        component is unavailable, so callers must display "unknown"
        instead of silently comparing incomplete costs.

        Note: shipping is deliberately NOT shown on product cards (it
        depends on destination and is not reliably available), but this
        property stays for price tracking.
        """

        if self.price is None:
            return None
        if self.shipping_cost is None:
            return None
        return round(self.price + self.shipping_cost, 2)
