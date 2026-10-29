"""Common interface every marketplace provider must implement.

Architectural contract:
- Providers are the ONLY source of real product data. Gemini is never
  used to fill in products, prices, ratings, availability or URLs.
- A provider MUST NOT raise just because its API credentials are
  missing. ``is_configured`` reports that state, and ``search_products``
  returns an empty list instead, so one unavailable marketplace never
  breaks a search (see ``services/search_service.py``).
- Providers must not invent endpoints or parameters. Integrations that
  are not implemented yet return an empty result and log a warning,
  with a TODO explaining exactly what has to be wired up.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import ClassVar

import httpx

from config.settings import MarketplaceCredentials
from models.product import Marketplace, Product
from models.search_request import SearchRequest

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 10.0


class MarketplaceProvider(ABC):
    """Base class for all marketplace integrations.

    Subclasses set ``marketplace`` / ``display_name`` and implement
    ``search_products``, ``get_product`` and ``generate_affiliate_link``.
    """

    marketplace: ClassVar[Marketplace]
    display_name: ClassVar[str]

    #: Set to True in a subclass once a real image-search API exists.
    supports_image_search: ClassVar[bool] = False

    #: Set to True in a subclass once a verified official product-feed /
    #: recommendation endpoint is wired up. While it is False the provider is
    #: simply skipped by "✨ Находки" instead of returning invented products.
    supports_feed: ClassVar[bool] = False

    def __init__(
        self,
        credentials: MarketplaceCredentials,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.credentials = credentials
        self.timeout = timeout
        self._external_client = http_client is not None
        self._http_client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout)
        )

    @property
    def is_configured(self) -> bool:
        """Whether usable API credentials are available."""

        return self.credentials.is_configured

    @property
    def http_client(self) -> httpx.AsyncClient:
        """Shared async HTTP client with a timeout and no secret logging."""

        return self._http_client

    async def aclose(self) -> None:
        """Close the internal HTTP client.

        Does nothing if the client was supplied by the caller (shared
        clients are owned by whoever created them).
        """

        if not self._external_client:
            await self._http_client.aclose()

    # -- required operations ------------------------------------------------

    @abstractmethod
    async def search_products(self, request: SearchRequest) -> list[Product]:
        """Search the marketplace and return normalized products.

        Must:
        - return ``[]`` (not raise) when the provider is not configured;
        - catch and log its own HTTP/API errors, returning whatever it
          successfully retrieved so far;
        - never fabricate product data.
        """

    @abstractmethod
    async def get_product(
        self, product_id: str, currency: str | None = None
    ) -> Product | None:
        """Fetch a single product by marketplace-specific id.

        ``currency`` is the user's persisted preference: it is forwarded
        to the API only when the marketplace documents that parameter
        (never invented). Returns ``None`` when the product cannot be
        retrieved.
        """

    @abstractmethod
    def generate_affiliate_link(self, product: Product) -> str | None:
        """Build an affiliate URL for a product, or ``None``.

        Returning ``None`` means affiliate links are not available for
        this provider (no program/credentials/tracking id) - callers
        must then fall back to ``product.product_url``.
        """

    # -- optional / future operations ---------------------------------------

    async def search_by_image(self, image_bytes: bytes) -> list[Product]:
        """Image search extension point.

        Not implemented anywhere yet on purpose: no fake image
        recognition. A provider that gains a real image-search API
        should override this and set ``supports_image_search = True``.
        """

        raise NotImplementedError(
            f"{self.display_name} does not provide an image search API yet."
        )

    async def fetch_feed_page(
        self,
        *,
        page: int = 1,
        currency: str | None = None,
        country: str | None = None,
    ) -> list[Product]:
        """One documented page of the marketplace's own product feed.

        Default implementation returns nothing and sets no flag: a
        provider that has no verified official feed endpoint must not
        pretend it can produce a discovery feed. Override this and set
        ``supports_feed = True`` once implemented.

        ``page`` selects the documented result page of the feed (1-based),
        so a caller can ask for fresh items once the first page has been
        fully shown. ``currency``/``country`` are the user's preferences;
        a provider forwards them only when its API documents them.
        """

        self._log_not_implemented("popular products feed")
        return []

    # -- helpers for subclasses ---------------------------------------------

    def _log_not_configured(self, action: str) -> None:
        logger.warning(
            "%s: %s skipped - API credentials are not configured",
            self.display_name,
            action,
        )

    def _log_not_implemented(self, action: str) -> None:
        logger.warning(
            "%s: %s is not implemented yet (no verified public API integration)",
            self.display_name,
            action,
        )
