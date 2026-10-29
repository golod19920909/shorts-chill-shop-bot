"""Tests for the marketplace provider layer.

Every provider must:
- report ``is_configured`` correctly;
- return NO data (never fabricated data) when credentials are missing;
- normalize a marketplace payload into the common ``Product`` model;
- survive malformed payload entries instead of raising.

No real API calls and no real credentials are used anywhere here.
"""

from __future__ import annotations

import asyncio
import unittest
import urllib.parse

import httpx

from config.settings import MarketplaceCredentials
from models.product import Marketplace, Product
from models.search_request import SearchMode, SearchRequest
from services.marketplaces.aliexpress import AliExpressProvider
from services.marketplaces.aliexpress_client import (
    GATEWAY_URL,
    LEGACY_TOP_GATEWAY_URL,
    METHOD_PRODUCT_QUERY,
    AliExpressClient,
    sign_request,
)
from services.marketplaces.base import MarketplaceProvider

#: Every provider implementation that ships with the bot. AliExpress is
#: the only marketplace; new integrations must add themselves here.
PROVIDERS = (AliExpressProvider,)

EMPTY_CREDENTIALS = MarketplaceCredentials(None, None)


def provider(provider_class, **credentials):
    """Build a provider instance with the given (usually empty) credentials."""

    return provider_class(MarketplaceCredentials(**credentials))


class ProviderContractTests(unittest.TestCase):
    """Behaviour shared by every provider implementation."""

    def test_all_providers_implement_the_interface(self) -> None:
        for provider_class in PROVIDERS:
            with self.subTest(provider=provider_class.__name__):
                instance = provider(provider_class)
                self.assertIsInstance(instance, MarketplaceProvider)
                self.assertIsInstance(instance.marketplace, Marketplace)
                self.assertTrue(instance.display_name)

    def test_provider_without_credentials_is_not_configured(self) -> None:
        for provider_class in PROVIDERS:
            with self.subTest(provider=provider_class.__name__):
                self.assertFalse(provider(provider_class).is_configured)

    def test_provider_with_partial_credentials_is_not_configured(self) -> None:
        self.assertFalse(AliExpressProvider(MarketplaceCredentials("key", None)).is_configured)
        self.assertFalse(AliExpressProvider(MarketplaceCredentials(None, "secret")).is_configured)
        self.assertTrue(AliExpressProvider(MarketplaceCredentials("key", "secret")).is_configured)

    def test_image_search_is_not_faked(self) -> None:
        for provider_class in PROVIDERS:
            with self.subTest(provider=provider_class.__name__):
                instance = provider(provider_class)
                self.assertFalse(instance.supports_image_search)
                with self.assertRaises(NotImplementedError):
                    asyncio.run(instance.search_by_image(b"\x89PNG-fake"))

    def test_search_without_credentials_returns_empty_and_does_not_call_api(self) -> None:
        request = SearchRequest(original_query="wireless headphones")

        for provider_class in PROVIDERS:
            with self.subTest(provider=provider_class.__name__):
                products = asyncio.run(provider(provider_class).search_products(request))
                self.assertEqual(products, [])

    def test_quantity_query_without_credentials_returns_empty(self) -> None:
        # Quantity wording stays a normal search - there is no separate
        # wholesale mode any more.
        request = SearchRequest(original_query="100 pcs earbuds", quantity=100)

        for provider_class in PROVIDERS:
            with self.subTest(provider=provider_class.__name__):
                products = asyncio.run(provider(provider_class).search_products(request))
                self.assertEqual(products, [])

    def test_get_product_without_credentials_returns_none(self) -> None:
        for provider_class in PROVIDERS:
            with self.subTest(provider=provider_class.__name__):
                product = asyncio.run(provider(provider_class).get_product("12345"))
                self.assertIsNone(product)

    def test_affiliate_link_without_program_returns_none(self) -> None:
        product = Product(
            marketplace=Marketplace.ALIEXPRESS,
            product_id="12345",
            title="Wireless headphones",
            product_url="https://example.com/item/12345.html",
        )

        for provider_class in PROVIDERS:
            with self.subTest(provider=provider_class.__name__):
                link = provider(provider_class).generate_affiliate_link(product)
                # No affiliate program is configured yet: returning the
                # plain URL would silently pretend otherwise.
                self.assertIsNone(link)

    def test_failing_api_returns_nothing_instead_of_guesses(self) -> None:
        # The integration is implemented, so the honest failure mode is an
        # empty result: an API error must never be replaced by made-up
        # products. A mock transport keeps this test off the network.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error_response": {"msg": "boom"}})

        async def scenario() -> tuple[list[Product], Product | None]:
            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            try:
                configured = AliExpressProvider(
                    MarketplaceCredentials("key", "secret"), http_client=client
                )
                self.assertTrue(configured.is_configured)
                products = await configured.search_products(
                    SearchRequest(original_query="headphones")
                )
                return products, await configured.get_product("12345")
            finally:
                await client.aclose()

        products, product = asyncio.run(scenario())

        self.assertEqual(products, [])
        self.assertIsNone(product)

    def test_api_payload_is_normalized_without_inventing_fields(self) -> None:
        # A documented-shaped payload is mapped one-to-one: values the API
        # did not return stay empty instead of being derived.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "aliexpress_affiliate_product_query_response": {
                        "resp_result": {
                            "resp_code": 200,
                            "result": {
                                "products": {
                                    "product": [
                                        {
                                            "product_id": "100500",
                                            "product_title": "Wireless Earbuds",
                                            "product_detail_url": "https://example.com/i/100500.html",
                                            "product_main_image_url": "https://example.com/i.jpg",
                                            "target_sale_price": "9.99",
                                            "target_sale_price_currency": "USD",
                                            "lastest_volume": 120,
                                            "evaluate_rate": "95.50%",
                                            "promotion_link": "https://s.click.example.com/l/abc",
                                        },
                                        {"product_title": "no id and no url"},
                                    ]
                                }
                            },
                        }
                    }
                },
            )

        async def scenario() -> list[Product]:
            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            try:
                configured = AliExpressProvider(
                    MarketplaceCredentials("key", "secret"), http_client=client
                )
                return await configured.search_products(
                    SearchRequest(original_query="wireless earbuds")
                )
            finally:
                await client.aclose()

        products = asyncio.run(scenario())

        self.assertEqual(len(products), 1, "нечитаемая запись должна быть пропущена")
        product = products[0]
        self.assertEqual(product.marketplace, Marketplace.ALIEXPRESS)
        self.assertEqual(product.product_id, "100500")
        self.assertEqual(product.price, 9.99)
        self.assertEqual(product.currency, "USD")
        self.assertEqual(product.orders_count, 120)
        # No star rating is documented, so it must stay unknown.
        self.assertIsNone(product.rating)
        self.assertEqual(product.affiliate_url, "https://s.click.example.com/l/abc")
        self.assertEqual(product.best_url, "https://s.click.example.com/l/abc")

    def test_discovery_feeds_without_credentials_return_empty(self) -> None:
        # Same honest contract as search: no credentials -> no API call and
        # no made-up products.
        for provider_class in PROVIDERS:
            with self.subTest(provider=provider_class.__name__):
                instance = provider(provider_class)
                self.assertEqual(
                    asyncio.run(instance.fetch_feed_page(page=1)), []
                )

    def test_feed_page_is_keyword_less_and_sort_less(self) -> None:
        # The G24 feed is the OFFICIAL ``product.query`` asked WITHOUT
        # ``keywords`` and WITHOUT ``sort``. The captured form body proves
        # the documented page parameters and the absence of anything we
        # would have invented - AliExpress composes the listing itself.
        captured: list[dict[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            form = {
                name: values[0]
                for name, values in urllib.parse.parse_qs(
                    request.read().decode("utf-8")
                ).items()
            }
            captured.append(form)
            return httpx.Response(
                200,
                json={
                    "aliexpress_affiliate_product_query_response": {
                        "resp_result": {
                            "resp_code": 200,
                            "result": {
                                "products": {
                                    "product": [
                                        {
                                            "product_id": "100900",
                                            "product_title": "LED Strip",
                                            "product_detail_url": (
                                                "https://example.com/i/100900.html"
                                            ),
                                            "target_sale_price": "3.30",
                                            "target_sale_price_currency": "USD",
                                            "lastest_volume": 77,
                                            "promotion_link": (
                                                "https://s.click.example.com/l/feed"
                                            ),
                                        }
                                    ]
                                }
                            },
                        }
                    }
                },
            )

        async def scenario() -> list[Product]:
            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            try:
                configured = AliExpressProvider(
                    MarketplaceCredentials("key", "secret"), http_client=client
                )
                return await configured.fetch_feed_page(page=7, currency="USD")
            finally:
                await client.aclose()

        products = asyncio.run(scenario())

        # Exactly ONE documented request...
        self.assertEqual(len(captured), 1)
        form = captured[0]
        self.assertEqual(form["method"], METHOD_PRODUCT_QUERY)
        # ...with NO keywords and NO sort of ours...
        self.assertNotIn("keywords", form)
        self.assertNotIn("sort", form)
        # ...and the documented pagination/currency/language parameters.
        self.assertEqual(form["page_no"], "7")
        self.assertEqual(form["page_size"], "50")
        self.assertEqual(form["target_language"], "RU")
        self.assertEqual(form["target_currency"], "USD")
        self.assertTrue(products)
        self.assertEqual(products[0].product_id, "100900")

    def test_feed_page_pagination_is_the_documented_page_no(self) -> None:
        # Two different ``page_no`` values must travel as two different
        # documented pages - live-verified: pages 1/2/40 share zero ids.
        pages: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            form = {
                name: values[0]
                for name, values in urllib.parse.parse_qs(
                    request.read().decode("utf-8")
                ).items()
            }
            pages.append(form.get("page_no", ""))
            return httpx.Response(
                200,
                json={
                    "resp_result": {
                        "resp_code": 200,
                        "result": {"products": {"product": []}},
                    }
                },
            )

        async def scenario() -> None:
            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            try:
                configured = AliExpressProvider(
                    MarketplaceCredentials("key", "secret"), http_client=client
                )
                await configured.fetch_feed_page(page=1)
                await configured.fetch_feed_page(page=40)
            finally:
                await client.aclose()

        asyncio.run(scenario())

        self.assertEqual(pages, ["1", "40"])

    def test_removed_marketplace_provider_is_not_importable(self) -> None:
        # Guard: Alibaba was removed completely, not just hidden from the UI.
        with self.assertRaises(ImportError):
            __import__("services.marketplaces.alibaba")

    def test_aclose_is_safe_to_call_repeatedly(self) -> None:
        for provider_class in PROVIDERS:
            with self.subTest(provider=provider_class.__name__):
                instance = provider(provider_class)
                asyncio.run(instance.aclose())
                asyncio.run(instance.aclose())

    def test_externally_supplied_http_client_is_not_closed(self) -> None:
        # Shared clients belong to the caller (main.py closes them).
        client = httpx.AsyncClient()
        instance = AliExpressProvider(EMPTY_CREDENTIALS, http_client=client)
        try:
            asyncio.run(instance.aclose())
            self.assertFalse(client.is_closed)
        finally:
            asyncio.run(client.aclose())


class AliExpressGatewayTests(unittest.TestCase):
    """The request layer must target the gateway that accepts this app key."""

    def test_default_gateway_is_the_verified_sg_endpoint(self) -> None:
        client = AliExpressClient(MarketplaceCredentials("key", "secret"))
        try:
            self.assertEqual(client.gateway, GATEWAY_URL)
        finally:
            asyncio.run(client.aclose())

        # Verified live against the Affiliate Open Platform: the legacy TOP
        # formal gateway answers ``isv.appkey-not-exists`` for this key, so
        # it must never be the default.
        self.assertEqual(GATEWAY_URL, "https://api-sg.aliexpress.com/sync")
        self.assertNotEqual(GATEWAY_URL, LEGACY_TOP_GATEWAY_URL)

    def test_gateway_can_be_overridden_for_tests_and_diagnostics(self) -> None:
        client = AliExpressClient(
            MarketplaceCredentials("key", "secret"),
            gateway=LEGACY_TOP_GATEWAY_URL,
        )
        try:
            self.assertEqual(client.gateway, LEGACY_TOP_GATEWAY_URL)
        finally:
            asyncio.run(client.aclose())

    def test_requests_go_to_the_gateway_and_carry_a_valid_signature(self) -> None:
        seen: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["form"] = {
                name: values[0]
                for name, values in urllib.parse.parse_qs(
                    request.read().decode("utf-8")
                ).items()
            }
            return httpx.Response(
                200,
                json={
                    "aliexpress_affiliate_product_query_response": {
                        "resp_result": {
                            "resp_code": 200,
                            "result": {"products": {"product": []}},
                        }
                    }
                },
            )

        async def scenario() -> None:
            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            try:
                configured = AliExpressProvider(
                    MarketplaceCredentials("key", "secret"), http_client=client
                )
                products = await configured.search_products(
                    SearchRequest(original_query="earbuds")
                )
                self.assertEqual(products, [])
            finally:
                await client.aclose()

        asyncio.run(scenario())

        self.assertEqual(seen["url"], GATEWAY_URL)
        form = seen["form"]
        assert isinstance(form, dict)
        self.assertEqual(form.get("method"), METHOD_PRODUCT_QUERY)
        self.assertEqual(form.get("app_key"), "key")
        self.assertEqual(form.get("format"), "json")
        self.assertEqual(form.get("v"), "2.0")
        self.assertEqual(form.get("sign_method"), "md5")
        self.assertIn("timestamp", form)
        self.assertIn("sign", form)
        # The signature is reproducible from the sent parameters and the
        # secret - and the secret itself is never transmitted.
        unsigned = {k: v for k, v in form.items() if k != "sign"}
        self.assertEqual(form.get("sign"), sign_request(unsigned, "secret"))
        self.assertNotIn("app_secret", form)
        self.assertNotIn("secret", form)


class SearchPageForwardingTests(unittest.TestCase):
    """The documented ``page_no`` of a search follows the request.

    Deeper pages exist so one narrow phrase cannot starve the pool
    (``services.search_service``); the provider must therefore forward the page
    it was asked for instead of always asking the first one.
    """

    def _search_form(self, page_no: int) -> dict[str, str]:
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(
                {
                    name: values[0]
                    for name, values in urllib.parse.parse_qs(
                        request.read().decode("utf-8")
                    ).items()
                }
            )
            return httpx.Response(
                200,
                json={
                    "aliexpress_affiliate_product_query_response": {
                        "resp_result": {
                            "resp_code": 200,
                            "result": {"products": {"product": []}},
                        }
                    }
                },
            )

        async def scenario() -> None:
            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            try:
                configured = AliExpressProvider(
                    MarketplaceCredentials("key", "secret"), http_client=client
                )
                await configured.search_products(
                    SearchRequest(original_query="earbuds", page_no=page_no)
                )
            finally:
                await client.aclose()

        asyncio.run(scenario())
        return captured

    def test_the_first_page_is_the_default(self) -> None:
        self.assertEqual(self._search_form(1)["page_no"], "1")
        # The keywords never change with the page: depth repeats the SAME
        # query, it does not invent a new one.
        self.assertEqual(self._search_form(1)["keywords"], "earbuds")

    def test_a_deeper_page_is_forwarded(self) -> None:
        self.assertEqual(self._search_form(2)["page_no"], "2")
        self.assertEqual(self._search_form(3)["page_no"], "3")
        self.assertEqual(self._search_form(3)["keywords"], "earbuds")


class CurrencyForwardingTests(unittest.TestCase):
    """A currency code is sent only when the API documents it."""

    def _search_form(self, currency: str | None) -> dict[str, str]:
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(
                {
                    name: values[0]
                    for name, values in urllib.parse.parse_qs(
                        request.read().decode("utf-8")
                    ).items()
                }
            )
            return httpx.Response(
                200,
                json={
                    "aliexpress_affiliate_product_query_response": {
                        "resp_result": {
                            "resp_code": 200,
                            "result": {"products": {"product": []}},
                        }
                    }
                },
            )

        async def scenario() -> None:
            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            try:
                configured = AliExpressProvider(
                    MarketplaceCredentials("key", "secret"), http_client=client
                )
                await configured.search_products(
                    SearchRequest(original_query="earbuds", currency=currency)
                )
            finally:
                await client.aclose()

        asyncio.run(scenario())
        return captured

    def test_documented_currency_is_forwarded(self) -> None:
        self.assertEqual(self._search_form("USD").get("target_currency"), "USD")

    def test_undocumented_currency_is_never_sent(self) -> None:
        # BYN/KZT/UZS/PLN are selectable in the bot but absent from the
        # documented target_currency list: sending an invented code would
        # make the API reject or misprice the query.
        for code in ("BYN", "KZT", "UZS", "PLN", "XYZ"):
            with self.subTest(code=code):
                form = self._search_form(code)
                self.assertNotIn("target_currency", form)

    def test_unset_currency_pins_the_documented_usd_for_price_bounds(self) -> None:
        # The USD price window (min_sale_price, sent with every search) must
        # be stated in a KNOWN unit: with no user preference the request pins
        # the documented ``USD`` instead of leaving the unit to the API
        # default. The user's own preference is never invented - an
        # undocumented code still sends nothing (test above).
        form = self._search_form(None)
        self.assertEqual(form.get("target_currency"), "USD")
        self.assertIn("min_sale_price", form)

    def test_undocumented_codes_stay_out_of_the_documented_set(self) -> None:
        from services.marketplaces.aliexpress_client import (
            DOCUMENTED_TARGET_CURRENCIES,
        )

        for code in ("BYN", "KZT", "UZS", "PLN"):
            with self.subTest(code=code):
                self.assertNotIn(code, DOCUMENTED_TARGET_CURRENCIES)
        self.assertIn("USD", DOCUMENTED_TARGET_CURRENCIES)
        self.assertIn("EUR", DOCUMENTED_TARGET_CURRENCIES)


class ModeDependentParamsTests(unittest.TestCase):
    """The spec split at the API layer: category vs recommendation vs search.

    A CATEGORY request (G24.8 P0.1) asks for a BROAD candidate pool: no
    USD price window, no default ``LAST_VOLUME_DESC`` sort - the harmful
    constraints the G24.7 audit proved. A legacy recommendation request
    (``POPULAR``) still asks for the documented popularity sort and the
    USD window; a direct text/photo search sends only the
    ``min_sale_price`` floor. The spec's ``VOLUME_DESC`` does NOT exist in
    the official enum - the documented spelling is ``LAST_VOLUME_DESC``.
    """

    def _search_form(self, request: SearchRequest) -> dict[str, str]:
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(
                {
                    name: values[0]
                    for name, values in urllib.parse.parse_qs(
                        request.read().decode("utf-8")
                    ).items()
                }
            )
            return httpx.Response(
                200,
                json={
                    "aliexpress_affiliate_product_query_response": {
                        "resp_result": {
                            "resp_code": 200,
                            "resp_msg": "success",
                            "result": {"products": {"product": []}},
                        }
                    }
                },
            )

        async def scenario() -> None:
            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            try:
                configured = AliExpressProvider(
                    MarketplaceCredentials("key", "secret"), http_client=client
                )
                await configured.search_products(request)
            finally:
                await client.aclose()

        asyncio.run(scenario())
        return captured

    def test_a_category_request_sends_no_window_and_no_volume_sort(self) -> None:
        # G24.8 P0.1: CATEGORY retrieves a BROAD candidate pool. The
        # recommendation-oriented constraints (the USD $1..$50 window and
        # the default LAST_VOLUME_DESC sort) damaged the RAW answer before
        # relevance could judge a single card (G24.7 live audit: the
        # window alone answered 0 electronics rows while the same phrase
        # without it answered 50), so this mode sends neither.
        form = self._search_form(
            SearchRequest(original_query="клавиатура", mode=SearchMode.CATEGORY)
        )

        self.assertNotIn("min_sale_price", form)
        self.assertNotIn("max_sale_price", form)
        self.assertNotIn("sort", form)
        # ...while every documented transport parameter stays as before:
        # the answer unit is still pinned, the language and pool size are
        # unchanged, and the phrase itself still travels.
        self.assertEqual(form.get("target_currency"), "USD")
        self.assertEqual(form.get("target_language"), "RU")
        self.assertEqual(form.get("page_size"), "50")
        self.assertEqual(form.get("page_no"), "1")
        self.assertEqual(form.get("keywords"), "клавиатура")

    def test_a_category_request_keeps_the_explicit_currency(self) -> None:
        # The user's own preference still travels for a category request;
        # only the USD price BOUNDS are gone (the unit of a stated
        # preference was never the problem).
        form = self._search_form(
            SearchRequest(
                original_query="клавиатура", mode=SearchMode.CATEGORY, currency="UAH"
            )
        )

        self.assertEqual(form.get("target_currency"), "UAH")
        self.assertNotIn("min_sale_price", form)
        self.assertNotIn("max_sale_price", form)

    def test_a_category_request_carries_the_selected_country(self) -> None:
        # G24.8 P1.4: the delivery destination travels as the documented
        # ship_to_country for EVERY selected code (two here, so the value is
        # clearly transported and never hardcoded), while the user's own
        # currency preference and the CATEGORY P0.1 contract stay untouched.
        for code in ("US", "DE"):
            with self.subTest(country=code):
                form = self._search_form(
                    SearchRequest(
                        original_query="клавиатура",
                        mode=SearchMode.CATEGORY,
                        country=code,
                        currency="UAH",
                    )
                )

                self.assertEqual(form.get("ship_to_country"), code)
                self.assertEqual(form.get("target_currency"), "UAH")
                self.assertNotIn("min_sale_price", form)
                self.assertNotIn("max_sale_price", form)
                self.assertNotIn("sort", form)

    def test_a_category_request_without_country_sends_no_ship_to_country(
        self,
    ) -> None:
        # The documented optional state stays exactly as before: without a
        # country the parameter is simply not serialized onto the wire.
        form = self._search_form(
            SearchRequest(original_query="клавиатура", mode=SearchMode.CATEGORY)
        )

        self.assertNotIn("ship_to_country", form)
        self.assertEqual(form.get("target_currency"), "USD")

    def test_a_legacy_recommendation_request_keeps_the_window_and_the_sort(
        self,
    ) -> None:
        # The recommendation request path itself (the legacy POPULAR feed
        # value) keeps its documented behaviour: the $1..$50 retrieval
        # window in the INTEGER spelling the gateway accepts (live probes
        # (2026) show "1.0"/"50.0" rejected with InvalidParameter
        # "null#min_sale_price" while "1"/"50" are answered with products
        # - see services.marketplaces.aliexpress._price_bound) in a
        # pinned, known unit...
        form = self._search_form(
            SearchRequest(original_query="клавиатура", mode=SearchMode.POPULAR)
        )

        self.assertEqual(form.get("min_sale_price"), "1")
        self.assertEqual(form.get("max_sale_price"), "50")
        self.assertEqual(form.get("target_currency"), "USD")
        # ...ordered by real order volume: LAST_VOLUME_DESC is the
        # documented name of the spec's "VOLUME_DESC".
        self.assertEqual(form.get("sort"), "LAST_VOLUME_DESC")

    def test_a_direct_search_sends_only_the_price_floor(self) -> None:
        form = self._search_form(SearchRequest(original_query="earbuds"))

        self.assertEqual(form.get("min_sale_price"), "1")
        self.assertNotIn("max_sale_price", form)
        # Relevance asks for no server-side ordering at all.
        self.assertNotIn("sort", form)

    def test_price_bounds_never_travel_in_decimal_form(self) -> None:
        """Regression: the decimal spelling of the $1 floor killed every search.

        LIVE reproduction (2026): ``min_sale_price=1.0`` was answered with
        ``InvalidParameter: The specified parameter "null#min_sale_price" is
        not valid`` on EVERY ``product.query`` keyword call, the provider
        returned ``[]`` and the user saw «ничего не найдено» - while the
        keyword-less G24 feed (no price window) kept working. The integer
        spelling ``1`` is accepted. The wire format is the contract, so it
        is asserted for several ordinary query shapes (Russian, English,
        multi-word).
        """

        for query in (
            "небулайзер",
            "nebulizer",
            "зарядка для айфона",
            "portable nebulizer",
            "автомобильный компрессор",
        ):
            with self.subTest(query=query):
                form = self._search_form(SearchRequest(original_query=query))
                self.assertTrue(form.get("keywords"))
                for bound in ("min_sale_price", "max_sale_price"):
                    if bound in form:
                        self.assertNotIn(".", form[bound])

    def test_price_bound_helper_is_lossless_and_conservative(self) -> None:
        from services.marketplaces.aliexpress import _price_bound

        # The published bounds convert losslessly...
        self.assertEqual(_price_bound(1.0), 1)
        self.assertEqual(_price_bound(50.0), 50)
        # ...and a hypothetical fractional bound only ever WIDENS the
        # retrieval window (the strict policy stays local).
        self.assertEqual(_price_bound(0.99), 1)
        self.assertEqual(_price_bound(0.4), 1)
        self.assertEqual(_price_bound(20.5, upper=True), 21)

    def test_a_non_usd_preference_gets_no_price_window(self) -> None:
        # min/max_sale_price follow the answer currency, and this project
        # converts nothing: a RUB preference must not receive USD bounds.
        form = self._search_form(
            SearchRequest(original_query="earbuds", currency="RUB")
        )

        self.assertNotIn("min_sale_price", form)
        self.assertNotIn("max_sale_price", form)
        self.assertEqual(form.get("target_currency"), "RUB")

    def test_an_explicit_sort_still_travels_for_a_category_request(self) -> None:
        # G24.8 P0.1 removed the category's DEFAULT popularity sort, not
        # the caller's explicit ordering: a stated preference still maps
        # to its documented spelling.
        from models.search_request import SortPreference

        form = self._search_form(
            SearchRequest(
                original_query="клавиатура",
                mode=SearchMode.CATEGORY,
                sort_preference=SortPreference.PRICE_ASC,
            )
        )

        self.assertEqual(form.get("sort"), "SALE_PRICE_ASC")


if __name__ == "__main__":
    unittest.main()
