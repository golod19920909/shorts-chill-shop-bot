"""Live, multi-category smoke test against the AliExpress Affiliate API.

Run manually from the project root (uses the local .env; never runs in CI):

    .venv/Scripts/python.exe smoke_aliexpress.py

Makes one small page request for each of eight broad product families, then
checks page-2 pagination for one family. It reports real normalized titles,
affiliate-link coverage and duplicate IDs without printing credentials or
affiliate URLs. This is an API/normalization smoke test, not a substitute for
the full Telegram conversation test.
"""

from __future__ import annotations

import asyncio
import sys

from config.settings import load_settings
from services.marketplaces.aliexpress import AliExpressProvider
from services.marketplaces.aliexpress_client import AliExpressApiError
from services.marketplaces.aliexpress_client import METHOD_PRODUCT_QUERY


# A deliberately cross-category sample: the live check must not be calibrated
# to one product or one seller vocabulary.
SEARCH_CASES = (
    "cordless drill",
    "power bank",
    "water filter",
    "security camera",
    "wireless headphones",
    "bagless vacuum cleaner",
    "garden hose",
    "headlamp",
)
PAGE_SIZE = 5
PAGINATION_QUERY = "wireless headphones"


async def fetch_page(provider: AliExpressProvider, query: str, page: int):
    payload = await provider._client.call(
        METHOD_PRODUCT_QUERY,
        {
            "keywords": query,
            "page_no": page,
            "page_size": PAGE_SIZE,
            "target_language": "RU",
            "target_currency": "USD",
        },
    )
    return provider.parse_products(payload)


async def main() -> int:
    settings = load_settings()
    if not settings.aliexpress.is_configured:
        print("SMOKE SKIPPED: ALIEXPRESS_APP_KEY/SECRET not configured")
        return 0

    provider = AliExpressProvider(settings.aliexpress, timeout=15.0)
    failures: list[str] = []
    page_one_by_query = {}

    try:
        for query in SEARCH_CASES:
            try:
                products = await fetch_page(provider, query, 1)
            except AliExpressApiError as exc:
                failures.append(f"{query}: API error ({provider._client.last_error_class})")
                print(f"FAIL {query}: API error ({provider._client.last_error_class})")
                continue

            page_one_by_query[query] = products
            affiliate_count = sum(bool(product.affiliate_url) for product in products)
            valid_count = sum(
                bool(product.product_id and product.title and product.product_url)
                for product in products
            )
            titles = " | ".join(product.title[:70] for product in products[:3])
            print(
                f"{'OK' if products and affiliate_count == len(products) else 'FAIL'} "
                f"{query}: normalized={len(products)}, "
                f"ids/titles/urls={valid_count}, affiliate_links={affiliate_count}; "
                f"sample={titles or 'no usable products'}"
            )
            if not products:
                failures.append(f"{query}: no normalized products")
            elif valid_count != len(products):
                failures.append(f"{query}: incomplete normalized product identity")
            if products and affiliate_count != len(products):
                failures.append(f"{query}: affiliate link missing on one or more products")

        first_page = page_one_by_query.get(PAGINATION_QUERY, [])
        if first_page:
            try:
                second_page = await fetch_page(provider, PAGINATION_QUERY, 2)
                first_ids = {product.product_id for product in first_page}
                second_id_list = [product.product_id for product in second_page]
                second_ids = set(second_id_list)
                overlap = first_ids & second_ids
                duplicate_within_page = len(second_id_list) - len(second_ids)
                new_ids = second_ids - first_ids
                # Keyword search rankings can shift between two live API calls,
                # so page boundaries are not guaranteed to be perfectly
                # disjoint. The provider/search pipeline deduplicates by ID;
                # this smoke check verifies that pagination actually adds
                # products instead of treating any harmless overlap as failure.
                pagination_ok = bool(second_page and new_ids)
                print(
                    f"{'OK' if pagination_ok and not duplicate_within_page else 'FAIL'} "
                    f"pagination {PAGINATION_QUERY}: page1={len(first_page)}, "
                    f"page2={len(second_page)}, overlap={len(overlap)}, "
                    f"new_on_page2={len(new_ids)}, "
                    f"duplicates_within_page2={duplicate_within_page}"
                )
                if not second_page:
                    failures.append("pagination: page 2 returned no normalized products")
                elif not new_ids:
                    failures.append(
                        "pagination: page 2 returned no products unseen on page 1"
                    )
                if duplicate_within_page:
                    failures.append(
                        f"pagination: {duplicate_within_page} duplicate IDs within page 2"
                    )
            except AliExpressApiError:
                failures.append(
                    f"pagination: API error ({provider._client.last_error_class})"
                )
                print(f"FAIL pagination: API error ({provider._client.last_error_class})")
        else:
            failures.append("pagination: skipped because page 1 had no products")
    finally:
        await provider.aclose()

    print(
        f"API totals: ok={provider._client.calls_ok}, "
        f"errors={provider._client.calls_err}, retries={provider._client.retries_spent}"
    )
    print("SMOKE OK" if not failures else f"SMOKE FAILED: {failures}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
