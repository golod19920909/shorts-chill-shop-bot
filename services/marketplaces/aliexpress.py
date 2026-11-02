"""AliExpress provider: a real integration with the Affiliate API.

Implemented methods (all documented - see
``services/marketplaces/aliexpress_client.py`` for the request layer):

- ``aliexpress.affiliate.product.query``      -> search, category browsing and
  the «✨ Находки» feed (the feed asks the SAME documented method WITHOUT
  ``keywords`` and without ``sort``: AliExpress itself orders its own broad
  product listing - live-verified for this app key: 50 rows per documented
  page, clean ``page_no`` pagination with zero overlap between pages, and
  ``promotion_link`` / ``product_main_image_url`` present on every row)
- ``aliexpress.affiliate.productdetail.get``  -> single product lookup

``aliexpress.affiliate.hotproduct.query`` and ``aliexpress.affiliate.product.
smartmatch`` are documented but this app key receives ``InsufficientPermission``
for BOTH (re-verified live, 2026), so the feed uses the keyword-less
``product.query`` answer - the marketplace's own default listing order. No
"popularity" is computed locally and no ranking is imposed over that feed:
the project only normalizes, deduplicates and technically validates what the
API returned.

The feed asks DEEP documented pages (``page_no``) in batches; the per-user
session decides how many pages one refill costs (see
``SearchService.find_feed_page``).

Fields are mapped one-to-one from the documented response and nothing is
inferred: a value the API did not return stays ``None``. In particular the
affiliate API exposes ``evaluate_rate`` (share of positive feedback, in
percent) and NOT a 0-5 star rating, so ``rating`` stays empty instead of
being derived from a different metric.

Promo data: only the per-product ``promo_code_info`` of a real listing is
read (``extract_promo_info`` -> the product card's promo block). The
marketplace's own promotion-campaign API is deliberately not called here any
more: the «🎟 Промокоды» section opens the configured partner/referral URL
(``config.promo``) instead of rendering campaign records.

Image search (``aliexpress.affiliate.image.search``) was verified LIVE for
this application (2026) and is NOT usable, so the provider does not claim
support for it and the bot answers honestly instead of faking a result:

- the documented request endpoint (``eco.taobao.com/router/rest``)
  answers ``isv.appkey-not-exists`` for this application's key - the key
  only exists on the AliExpress gateway (``api-sg.aliexpress.com/sync``);
- on the AliExpress gateway the method routes and the request
  authenticates (the gateway returns parameter validation, not an auth
  error), but the mandatory ``image_file_bytes`` (``byte[]``, ≤100 KB) is
  never accepted in any transport/encoding the gateway parses (form body,
  query string, GET; csv/base64/JSON) - always ``MissingParameter``;
- the mandatory ``app_signature`` ("API signature") is a platform
  capability this application does not have and cannot produce - the
  gateway demands it on every request;
- the current official affiliate SDK does not expose this method at all.

The refusal was re-verified live with the real key: on the very same
gateway, with the very same credentials, the control method
(``aliexpress.affiliate.product.query``) answers normally, while
``image.search`` answers ``MissingParameter`` for ``image_file_bytes``
(csv, base64 and JSON encodings alike) and demands ``app_signature`` when
it is omitted - so the refusal is a platform capability, never broken
credentials or a wrong request shape.

The photo flow therefore uses the strongest legitimate fallback:
Gemini Vision → structured image understanding → a few grounded AliExpress
text searches → the same strict relevance gate as any search (see
``handlers.discovery``). No native image search is ever claimed to the
user.
"""


from __future__ import annotations

import asyncio
import json
import logging
import math
from typing import Any

import httpx

from curation.pipeline import MAX_PRICE_USD, MIN_PRICE_USD
from config.settings import MarketplaceCredentials
from models.product import Marketplace, Product
from models.search_request import SearchMode, SearchRequest, SortPreference
from services.marketplaces.aliexpress_client import (
    DOCUMENTED_TARGET_CURRENCIES,
    DOCUMENTED_TARGET_LANGUAGES,
    METHOD_PRODUCT_DETAIL,
    METHOD_PRODUCT_QUERY,
    AliExpressApiError,
    AliExpressClient,
)
from services.marketplaces.base import DEFAULT_TIMEOUT_SECONDS, MarketplaceProvider
from services.product_normalizer import (
    clean_text,
    compute_discount_percent,
    get_first,
    to_float,
    to_int,
)
from services.text_matching import truncate_provider_phrase

logger = logging.getLogger(__name__)


class AliExpressSearchResults(list[Product]):
    """List-compatible provider result that can carry a safe API failure class.

    The marketplace interface remains list-compatible. SearchService can
    distinguish a genuine empty answer from an API failure without relying
    on mutable provider-global error state.
    """

    def __init__(
        self,
        products=(),
        *,
        provider_error: str | None = None,
    ) -> None:
        super().__init__(products)
        self.provider_error = provider_error


#: The affiliate API is localizable; the bot's audience is Russian, so
#: product text is requested in Russian (a documented target_language).
TARGET_LANGUAGE = "RU"

#: Documented page size range is 1..50. 50 gives the local filters (price,
#: rating, orders) enough candidates to work with.
PAGE_SIZE = 50

#: Documented ``sort`` values mapped from the request preference. A
#: preference with no documented counterpart is left out on purpose: the
#: ranking is then applied locally by ``services/comparison_service``.
_SORT_BY_PREFERENCE = {
    SortPreference.PRICE_ASC: "SALE_PRICE_ASC",
    SortPreference.PRICE_DESC: "SALE_PRICE_DESC",
    SortPreference.ORDERS_DESC: "LAST_VOLUME_DESC",
    SortPreference.POPULARITY_DESC: "LAST_VOLUME_DESC",
    SortPreference.VALUE_DESC: "DISCOUNT_DESC",
}

#: Documented ``sort`` used when a RECOMMENDATION request (category
#: browsing) asks for an ordering of its own: the API answers by real order
#: volume - the only documented popularity signal this app key receives
#: (``hotproduct.query`` and ``product.smartmatch`` both answer
#: ``InsufficientPermission``, see the module docstring). The ✨ Находки
#: feed itself sends NO ``sort`` at all: AliExpress orders its own listing.
FEED_SORT_POPULAR = "LAST_VOLUME_DESC"


#: Keys of a ``promo_code_info`` mapping that can hold the actual coupon
#: code. Live-verified (this app key) the API uses ``promo_code``; the
#: alternates are common spellings kept for forward compatibility.
_PROMO_CODE_KEYS = ("promo_code", "code", "promotion_code", "coupon_code")


def extract_promo_info(raw: Any) -> tuple[str | None, str | None, str | None]:
    """Return ``(code, benefit, valid_until)`` from ``promo_code_info``.

    Live-verified shape (this app key)::

        {"promo_code": "3N41SN59P8VB",
         "code_value": "On order over USD 104.7 , get USD 10.48 off",
         "code_availabletime_end": "2026-09-29 23:59:59",
         "code_availabletime_start": "...", "code_mini_spend": "104.7", ...}

    Only these marketplace-provided values are read, each kept VERBATIM:

    - the coupon code itself (``promo_code``);
    - the marketplace's own benefit sentence (``code_value``) - the one
      field where AliExpress states the discount amount AND the minimum
      order together with its currency;
    - the real validity end (``code_availabletime_end``).

    A JSON-encoded mapping is unwrapped; a plain string stays the code
    verbatim (the API's free-text form); anything else yields all
    ``None``. Nothing is parsed out of unknown shapes and no code,
    amount, minimum order, expiry or currency is ever invented -
    ``code_mini_spend`` is deliberately NOT shown separately because it
    is a bare number whose currency only ``code_value`` states.
    """

    if isinstance(raw, str):
        text = clean_text(raw)
        if not text:
            return None, None, None
        if text[:1] in "{[":
            try:
                decoded: Any = json.loads(text)
            except ValueError:
                decoded = None
            if isinstance(decoded, dict):
                return extract_promo_info(decoded)
        return text, None, None

    if isinstance(raw, dict):
        code: str | None = None
        for key in _PROMO_CODE_KEYS:
            value = clean_text(raw.get(key))
            if value:
                code = value
                break
        return (
            code,
            clean_text(raw.get("code_value")),
            clean_text(raw.get("code_availabletime_end")),
        )

    return None, None, None


def extract_promo_code(raw: Any) -> str | None:
    """Return a usable promo code from ``promo_code_info``, or ``None``.

    Stable single-value interface for the ``Product.promo_code`` field;
    the amount, conditions and validity the API also provides are read
    by :func:`extract_promo_info` and reach the card as their own
    verbatim fields.
    """

    return extract_promo_info(raw)[0]


def _price_bound(value: float, *, upper: bool = False) -> int:
    """Serialize one documented USD price bound the way the gateway accepts.

    Live-verified (2026, this app key): ``min_sale_price`` /
    ``max_sale_price`` accept only INTEGER spellings - ``1`` is answered
    with products, while ``1.0`` / ``1.5`` / ``0.99`` are rejected with
    ``InvalidParameter: The specified parameter "null#min_sale_price" is not
    valid``. That rejection is per call, so sending the decimal spelling of
    the ``$1`` floor made EVERY keyword search fail with zero rows (the
    provider logged ``AliExpress search failed`` and returned ``[]``) while
    the keyword-less G24 feed - which sends no price window - kept working.

    The published bounds ($1 / $50) are integral, so the conversion is
    lossless. A hypothetical fractional bound is widened to the next whole
    dollar (floor for a minimum, ceiling for a maximum, minimum never below
    ``1``): the retrieval window only ever errs toward SHOWING products,
    and the strict policy stays local (``curation.pipeline.QualityFilter``).
    """

    number = float(value)
    if number.is_integer():
        return int(number)
    if upper:
        return math.ceil(number)
    return max(1, math.floor(number))


class AliExpressProvider(MarketplaceProvider):
    """Consumer marketplace provider backed by the AliExpress Affiliate API."""

    marketplace = Marketplace.ALIEXPRESS
    display_name = "AliExpress"

    #: The ✨ Находки feed is built from the keyword-less ``product.query``
    #: answer - the marketplace's own default listing order, no local sort.
    supports_feed = True

    #: ``aliexpress.affiliate.image.search`` is documented but verified
    #: LIVE as unusable for this application: the documented endpoint
    #: rejects this key (``isv.appkey-not-exists``), the AliExpress
    #: gateway never accepts the mandatory ``image_file_bytes``/``app_signature``
    #: pair, and the current official SDK does not expose the method. See
    #: the module docstring for the full probe results.
    supports_image_search = False

    def __init__(
        self,
        credentials: MarketplaceCredentials,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        client: AliExpressClient | None = None,
    ) -> None:
        super().__init__(credentials, http_client, timeout)
        self._client = client or AliExpressClient(
            credentials, http_client=self.http_client, timeout=timeout
        )

    # -- searching ---------------------------------------------------------

    @staticmethod
    def _is_recommendation(request: SearchRequest) -> bool:
        """Whether the request feeds the impulse-buy RECOMMENDATION params.

        Only the legacy ``SearchMode.POPULAR`` value answers yes (kept so
        historical rows keep their documented window/sort). G24.8 split the
        rest apart on purpose: ``SearchMode.CATEGORY`` gets broad retrieval
        parameters (the P0.1 branch of :meth:`_search_params`) and its own
        curation policy (``MODE_CATEGORY``, P0.2), so it is NO LONGER
        classified here; an ordinary text or photo search
        (``SEARCH``/``IMAGE``) and a mood discovery query remain direct
        searches.
        """

        return request.mode is SearchMode.POPULAR

    @staticmethod
    def _price_window(currency: str | None, *, recommendation: bool) -> dict[str, Any]:
        """The documented USD price window of one request (``{}`` when N/A).

        ``min_sale_price``/``max_sale_price`` are documented ``Number``
        params of ``product.query`` (official reference: maximum/minimum
        sale price). Their unit follows the currency the answer is asked
        in, so bounds travel ONLY with a USD answer: an explicit USD
        preference, or no preference at all - where ``target_currency`` is
        pinned to the documented ``USD`` so the unit is never left to the
        API default. A stated NON-USD preference skips the window entirely
        (this project converts no currencies, so USD bounds would silently
        filter the wrong way); the local ``curation.pipeline.QualityFilter``
        then enforces its thresholds on USD-priced candidates only.

        * a recommendation request is bounded by :data:`MAX_PRICE_USD`
          ($50) - the feed's EMPTY-STATE FALLBACK ceiling, not the strict
          $20 policy: the retrieval pool must still hold the $20..$50 band
          so the documented relaxation (strict $1..$20 that kept nothing
          relaxes to $50, orders stay >= 300) has real candidates to fall
          back to. The strict $1..$20 window itself is enforced locally by
          ``QualityFilter``.
        * a direct search sends only the $1 floor (spec: "search: only
          minPrice 1.0"), never a ceiling.

        Both bounds travel through :func:`_price_bound`: the gateway
        rejects a DECIMAL spelling of these parameters
        (``InvalidParameter "null#min_sale_price"`` for ``"1.0"``, which
        made every keyword search fail with zero rows), so the wire format
        is always the accepted integer spelling - see the helper's
        live-verification note.
        """

        if currency and currency.upper() != "USD":
            logger.info(
                "AliExpress price window skipped: target_currency=%s is not USD "
                "(min_sale_price/max_sale_price unit would be ambiguous)",
                currency.upper(),
            )
            return {}

        window: dict[str, Any] = {"min_sale_price": _price_bound(MIN_PRICE_USD)}
        if recommendation:
            window["max_sale_price"] = _price_bound(MAX_PRICE_USD, upper=True)
        if not currency:
            window["target_currency"] = "USD"
        return window

    def _search_params(self, request: SearchRequest) -> dict[str, Any]:
        """Documented business parameters for ``product.query``.

        Mode-dependent (spec):

        * a CATEGORY request (G24.8 P0.1) asks for a BROAD candidate pool
          and sends NONE of the recommendation-oriented constraints: no
          ``min_sale_price``/``max_sale_price`` window and no default
          ``LAST_VOLUME_DESC`` sort. The G24.7 live audit showed those
          parameters damaged retrieval before relevance could judge a
          single card (the electronics phrase answered 0 rows with the
          window and 50 without it); quality and order are decided
          locally, after the relevance filter. The documented
          ``target_currency`` pin, the language, the page parameters and
          every other required parameter stay exactly as before;
        * a RECOMMENDATION request (the legacy ``POPULAR`` feed value)
          with no explicit sort of its own is asked for the documented
          popularity ordering ``LAST_VOLUME_DESC`` - the official reference
          lists exactly ``SALE_PRICE_ASC``, ``SALE_PRICE_DESC``,
          ``LAST_VOLUME_ASC``, ``LAST_VOLUME_DESC``; there is NO
          ``VOLUME_DESC`` value, so ``LAST_VOLUME_DESC`` is the documented
          spelling of the spec's "VOLUME_DESC" (and the very sort the
          popular feed is verified to return - see :data:`FEED_SORT_POPULAR`);
        * a DIRECT search keeps the request's own sort mapping and sends
          only the ``min_sale_price`` floor; the USD price window logic -
          including when bounds are sent at all - is
          :meth:`_price_window`.

        ``page_no`` is forwarded as the request carries it: ``1`` for every
        ordinary search, a deeper documented page when
        ``services.search_service`` is asking the SAME phrase again because
        the relevant pool is still thin. The keywords, the language and the
        currency never change with the page, so a deeper answer can only add
        real listings of the phrase the user asked for.
        """

        keywords = (request.normalized_query or request.original_query or "").strip()

        params: dict[str, Any] = {
            # Stage 3F: token-aware truncation keeps the tail (model,
            # compatibility, numeric specs) that a blind ``[:100]`` slice
            # drops; the full request stays on ``SearchRequest`` for
            # relevance/filtering. Limit itself is unchanged (≤100 chars).
            "keywords": truncate_provider_phrase(keywords),
            "page_no": max(1, int(request.page_no or 1)),
            "page_size": PAGE_SIZE,
            "target_language": (
                TARGET_LANGUAGE if TARGET_LANGUAGE in DOCUMENTED_TARGET_LANGUAGES
                else None
            ),
            "target_currency": self._currency_param(request.currency),
            "ship_to_country": request.country or None,
            "sort": _SORT_BY_PREFERENCE.get(request.sort_preference),
        }

        if request.mode is SearchMode.CATEGORY:
            # G24.8 P0.1: broad candidate retrieval for category browsing.
            # No price window, no default popularity sort - both are
            # recommendation-oriented constraints that damaged the RAW
            # answer before relevance could judge anything (G24.7 live
            # audit), so quality and order are decided locally instead.
            # An explicit sort the caller DID ask for still travels, and
            # the documented USD pin of the old window is kept so the
            # answer unit never changes with this patch.
            if not request.currency:
                params["target_currency"] = "USD"
            return params

        recommendation = self._is_recommendation(request)
        if recommendation and params["sort"] is None:
            # An unnamed order on an impulse feed reads by real order
            # volume; a sort the request DID ask for (cheapest first, the
            # discount ordering) still wins - the user's own words rule.
            params["sort"] = FEED_SORT_POPULAR
        params.update(
            self._price_window(request.currency, recommendation=recommendation)
        )
        return params

    @staticmethod
    def _currency_param(currency: str | None) -> str | None:
        """Forward a currency only when the API documents it.

        ``BYN``, ``KZT``, ``UZS`` and ``PLN`` are selectable in the bot but
        absent from the documented ``target_currency`` list, so they are not
        sent - the API then answers in its own default currency instead of
        receiving an invented code.
        """

        if not currency:
            return None
        code = currency.upper()
        if code not in DOCUMENTED_TARGET_CURRENCIES:
            logger.info(
                "AliExpress does not document target_currency=%s; "
                "using the API default currency",
                code,
            )
            return None
        return code

    async def search_products(self, request: SearchRequest) -> list[Product]:
        if not self.is_configured:
            self._log_not_configured("search")
            return []

        try:
            payload = await self._client.call(
                METHOD_PRODUCT_QUERY, self._search_params(request)
            )
        except AliExpressApiError as exc:
            # Preserve the provider contract (a list) while allowing the
            # shared SearchService to distinguish an API failure from a
            # legitimate empty marketplace answer. Keep diagnostics safe:
            # code/status and exception class only, never request params.
            error_code = exc.code or (str(exc.status) if exc.status else "unknown")
            error_class = f"AliExpressApiError:{error_code}"
            logger.error("AliExpress search failed (%s)", error_class)
            return AliExpressSearchResults(provider_error=error_class)

        return AliExpressSearchResults(self.parse_products(payload))

    async def get_product(
        self, product_id: str, currency: str | None = None
    ) -> Product | None:
        """Fetch one product, requesting the user's preferred currency.

        ``target_currency`` is a documented parameter of
        ``productdetail.get`` and is forwarded through the same
        guarded helper as searches (``_currency_param``): an
        undocumented code is never sent, the API then answers in its
        own default currency and the tracker's mismatch protection
        absorbs the difference.
        """

        if not self.is_configured:
            self._log_not_configured("get_product")
            return None

        try:
            payload = await self._client.call(
                METHOD_PRODUCT_DETAIL,
                {
                    "product_ids": str(product_id),
                    "target_language": TARGET_LANGUAGE,
                    "target_currency": self._currency_param(currency),
                },
            )
        except AliExpressApiError as exc:
            logger.error("AliExpress product lookup failed: %s", exc)
            return None

        products = self.parse_products(payload)
        return products[0] if products else None

    # -- product feed (✨ Находки) ----------------------------------------

    async def fetch_feed_page(
        self,
        *,
        page: int = 1,
        currency: str | None = None,
        country: str | None = None,
    ) -> list[Product]:
        """One documented page of AliExpress' own broad product listing.

        This is the OFFICIAL source of the ✨ Находки feed:
        ``aliexpress.affiliate.product.query`` asked WITHOUT ``keywords`` and
        WITHOUT ``sort``. The documented parameter reference marks ``keywords``
        as optional; live verification for this app key (2026) shows the
        gateway answering such a request with the marketplace's own broad
        listing - about 15-19 million rows, ``page_size`` up to 50, clean
        ``page_no`` pagination (pages 1, 2 and 40 verified to share ZERO ids),
        and ``promotion_link`` / ``product_main_image_url`` on every row.

        Nothing is imposed over that answer: no keyword of ours, no sort of
        ours, no price window - AliExpress decides what its feed holds and in
        which order. The caller only normalizes, deduplicates and validates
        the rows (see ``SearchService.find_feed_page``).

        ``page`` is the documented 1-based ``page_no``; ``currency``/``country``
        are the user's optional preferences, forwarded only through the
        documented ``target_currency``/``ship_to_country`` parameters.
        """

        if not self.is_configured:
            self._log_not_configured("product feed")
            return []

        params: dict[str, Any] = {
            "page_no": max(1, int(page or 1)),
            "page_size": PAGE_SIZE,
            "target_language": (
                TARGET_LANGUAGE if TARGET_LANGUAGE in DOCUMENTED_TARGET_LANGUAGES
                else None
            ),
            "target_currency": self._currency_param(currency),
            "ship_to_country": country or None,
        }

        try:
            payload = await self._client.call(METHOD_PRODUCT_QUERY, params)
        except AliExpressApiError as exc:
            logger.error("AliExpress product feed page %s failed: %s", page, exc)
            raise

        return self.parse_products(payload)

    # -- affiliate links ---------------------------------------------------

    def generate_affiliate_link(self, product: Product) -> str | None:
        """Return the real affiliate link the API already produced.

        ``aliexpress.affiliate.link.generate`` documents ``tracking_id`` as
        mandatory and this project has no affiliate tracking id configured,
        so no link is generated here. The ``promotion_link`` returned by a
        search is used instead - a genuine AliExpress affiliate URL, not a
        fabricated one. Callers fall back to ``product_url`` when it is
        absent.
        """

        if product.affiliate_url:
            return product.affiliate_url
        return None

    # -- response mapping --------------------------------------------------

    def parse_products(self, payload: dict[str, Any]) -> list[Product]:
        """Normalize a ``products`` payload into :class:`Product` objects."""

        products: list[Product] = []
        for raw in self._raw_products(payload):
            product = self._normalize(raw)
            if product is not None:
                products.append(product)

        logger.debug("AliExpress returned %d usable products", len(products))
        return products

    @staticmethod
    def _raw_products(payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Extract the product list from either documented nesting."""

        result = payload.get("result")
        if not isinstance(result, dict):
            result = payload if isinstance(payload, dict) else {}

        container = result.get("products")
        if isinstance(container, dict):
            container = container.get("product")
        if isinstance(container, dict):
            container = [container]
        if not isinstance(container, list):
            return []

        return [item for item in container if isinstance(item, dict)]

    def _normalize(self, raw: dict[str, Any]) -> Product | None:
        """Map one API product onto the project's ``Product`` model."""

        product_id = clean_text(raw.get("product_id"))
        title = clean_text(raw.get("product_title"))
        url = clean_text(raw.get("product_detail_url"))

        if not product_id or not title or not url:
            logger.debug("Skipping an AliExpress item without id/title/url")
            return None

        # Price and old price must come from the SAME API price pair:
        # the TARGET pair (the answer denominated in ``target_currency``)
        # when it is present, otherwise the BASE pair. Mixing them - a
        # target-currency price next to a base-currency original price -
        # would compute a discount from two different units, and the
        # project converts no currencies (see ``_price_window``).
        if get_first(raw, "target_sale_price") is not None:
            price = to_float(raw.get("target_sale_price"))
            currency = clean_text(raw.get("target_sale_price_currency"))
            old_price = to_float(raw.get("target_original_price"))
        else:
            price = to_float(raw.get("sale_price"))
            currency = clean_text(raw.get("sale_price_currency"))
            old_price = to_float(raw.get("original_price"))

        discount = to_float(raw.get("discount"))
        if discount is None:
            discount = compute_discount_percent(price, old_price)

        # ``evaluate_rate`` is a share of positive feedback in percent
        # ("96.0%"). AliExpress reports "0.0%" for listings where it has
        # NO share at all (the live G24 feed does not return the field for
        # most rows; the keyword answer uses "0.0%"), and the website at
        # the same time shows a normal star rating - so a rendered "0%"
        # would claim a measured zero instead of missing data. A real
        # measured share is never zero, therefore zero is stored as
        # ``None`` (the line is then not rendered at all).
        positive_rate = to_float(raw.get("evaluate_rate"))
        if positive_rate == 0:
            positive_rate = None

        promo_code, promo_benefit, promo_valid_until = extract_promo_info(
            raw.get("promo_code_info")
        )

        return Product(
            marketplace=Marketplace.ALIEXPRESS,
            product_id=product_id,
            title=title,
            product_url=url,
            image_url=clean_text(raw.get("product_main_image_url")),
            price=price,
            currency=currency,
            old_price=old_price,
            discount_percent=discount,
            positive_rate=positive_rate,
            orders_count=to_int(raw.get("lastest_volume")),
            delivery_estimate=clean_text(raw.get("ship_to_days")),
            affiliate_url=clean_text(raw.get("promotion_link")),
            promo_code=promo_code,
            promo_benefit=promo_benefit,
            promo_valid_until=promo_valid_until,
        )
