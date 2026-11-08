"""Read-only smoke test for selected AliExpress Portals Affiliate API methods.

Run from the repository root:
    python -m scripts.diagnose_aliexpress_portals

Optional featured-campaign product probe (times must be the campaign's
PST-local values as required by the API documentation):
    python -m scripts.diagnose_aliexpress_portals \
        --promo-name "EXACT_NAME_FROM_API" \
        --promo-start-time "YYYY-MM-DD HH:MM:SS" \
        --promo-end-time "YYYY-MM-DD HH:MM:SS"

The script reads the existing ALIEXPRESS_APP_KEY / ALIEXPRESS_APP_SECRET
from the environment (normally .env via config.settings), uses the existing
AliExpressClient signer and current Singapore gateway, and never prints
credentials, signatures, raw orders, buyer data, or full product records.

It does not write to the database, edit .env, or change bot configuration.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from config.settings import settings
from services.marketplaces.aliexpress_client import (
    AliExpressClient,
    GATEWAY_URL,
)

METHOD_FEATURED_PROMO_GET = "aliexpress.affiliate.featuredpromo.get"
METHOD_FEATURED_PROMO_PRODUCTS_GET = (
    "aliexpress.affiliate.featuredpromo.products.get"
)
METHOD_ORDER_LIST = "aliexpress.affiliate.order.list"
GMT_PLUS_8 = timezone(timedelta(hours=8))


def _items(container: Any, key: str) -> list[dict[str, Any]]:
    """Normalize the TOP list wrapper without assuming one response shape."""
    if isinstance(container, list):
        return [item for item in container if isinstance(item, dict)]
    if isinstance(container, dict):
        value = container.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            # Some TOP responses wrap a repeated item in a singular key.
            nested = value.get(key[:-1]) if key.endswith("s") else None
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, dict)]
            if isinstance(nested, dict):
                return [nested]
    return []


async def _probe(
    client: AliExpressClient,
    label: str,
    method: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    print(f"\n[{label}]")
    print(f"  METHOD: {method}")
    try:
        result = await client.call(method, params or {})
    except Exception as exc:  # diagnostic tool: report one probe and continue
        _print_failure(label, exc)
        return None

    print("  RESULT: SUCCESS")
    print(f"  RESPONSE_FIELDS: {', '.join(sorted(result.keys())) or '(empty)'}")
    return result


def _data(result: dict[str, Any] | None) -> dict[str, Any]:
    """The client returns resp_result; business records live in its result."""
    if not result:
        return {}
    nested = result.get("result")
    return nested if isinstance(nested, dict) else result


def _campaigns(result: dict[str, Any] | None) -> list[dict[str, Any]]:
    return _items(_data(result).get("promos"), "promos")


def _orders(result: dict[str, Any] | None) -> list[dict[str, Any]]:
    return _items(_data(result).get("orders"), "orders")


def _products(result: dict[str, Any] | None) -> list[dict[str, Any]]:
    return _items(_data(result).get("products"), "products")


async def run(args: argparse.Namespace) -> int:
    credentials = settings.aliexpress
    if not credentials.is_configured:
        print(
            "AliExpress credentials are not configured. "
            "Set ALIEXPRESS_APP_KEY and ALIEXPRESS_APP_SECRET in your existing "
            ".env file, then rerun. This script does not edit .env."
        )
        return 2

    print("AliExpress Portals read-only API diagnostic")
    print(f"Gateway: {GATEWAY_URL}")
    print("Credentials: configured (values hidden)")
    print("Database: not opened")
    print("Retries: disabled for this diagnostic")

    client = AliExpressClient(credentials, timeout=15.0, max_retries=0)
    failures = 0
    try:
        promo_result = await _probe(
            client,
            "FEATURED_CAMPAIGNS",
            METHOD_FEATURED_PROMO_GET,
        )
        if promo_result is None:
            failures += 1
        campaigns = _campaigns(promo_result)
        print(f"  CAMPAIGN_COUNT: {len(campaigns)}")
        for index, campaign in enumerate(campaigns[:20], start=1):
            name = str(campaign.get("promo_name") or "(name not returned)")
            description = str(campaign.get("promo_desc") or "")
            product_count = campaign.get("product_num", "unknown")
            print(f"  CAMPAIGN_{index}: {name[:160]}")
            print(f"    PRODUCT_COUNT: {product_count}")
            if description:
                print(f"    DESCRIPTION: {description[:200]}")

        if args.promo_name or args.promo_start_time or args.promo_end_time:
            if not (
                args.promo_name
                and args.promo_start_time
                and args.promo_end_time
            ):
                print(
                    "\n[FEATURED_CAMPAIGN_PRODUCTS]\n"
                    "  SKIPPED: pass --promo-name, --promo-start-time and "
                    "--promo-end-time together."
                )
            else:
                product_params = {
                    "promotion_name": args.promo_name,
                    "promotion_start_time": args.promo_start_time,
                    "promotion_end_time": args.promo_end_time,
                    "page_no": 1,
                    "page_size": 5,
                    "target_language": "RU",
                    "target_currency": "USD",
                }
                products_result = await _probe(
                    client,
                    "FEATURED_CAMPAIGN_PRODUCTS",
                    METHOD_FEATURED_PROMO_PRODUCTS_GET,
                    product_params,
                )
                if products_result is None:
                    failures += 1
                products = _products(products_result)
                print(f"  PRODUCT_COUNT_RETURNED: {len(products)}")
                for index, product in enumerate(products[:5], start=1):
                    print(
                        f"  PRODUCT_{index}: "
                        f"{str(product.get('product_title') or '(title not returned)')[:140]}"
                    )
                    print(
                        "    HAS_PRODUCT_ID: "
                        f"{bool(product.get('product_id'))}"
                    )
                    print(
                        "    HAS_PROMOTION_LINK: "
                        f"{bool(product.get('promotion_link'))}"
                    )
                    print(
                        "    HAS_PRICE_DATA: "
                        f"{any(product.get(key) is not None for key in ('app_sale_price', 'sale_price', 'original_price'))}"
                    )
        else:
            print(
                "\n[FEATURED_CAMPAIGN_PRODUCTS]\n"
                "  SKIPPED: campaign API must first return an exact promo name; "
                "the product probe also requires its exact start/end times. "
                "No dates or campaign names are guessed."
            )

        now = datetime.now(GMT_PLUS_8)
        start_time = (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        end_time = now.strftime("%Y-%m-%d %H:%M:%S")
        order_result = await _probe(
            client,
            "AFFILIATE_ORDERS_LAST_24H",
            METHOD_ORDER_LIST,
            {
                "start_time": start_time,
                "end_time": end_time,
                "status": "Payment Completed",
                "page_no": 1,
                "page_size": 10,
            },
        )
        if order_result is None:
            failures += 1
        orders = _orders(order_result)
        print(f"  ORDER_COUNT_RETURNED: {len(orders)}")
        print(
            "  TOTAL_RECORD_COUNT: "
            f"{_data(order_result).get('total_record_count', 'not returned') if order_result else 'not available'}"
        )
        print(
            "  NOTE: order rows and commercial details are deliberately not "
            "printed by this diagnostic."
        )
    finally:
        await client.aclose()

    print("\nSUMMARY")
    print(f"  API_CALLS_OK: {client.calls_ok}")
    print(f"  API_CALLS_FAILED: {client.calls_err}")
    print(f"  PROBE_FAILURES: {failures}")
    print("  No database or configuration files were changed.")
    return 1 if failures else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only AliExpress Portals API smoke test."
    )
    parser.add_argument(
        "--promo-name",
        help="Exact promo_name returned by featuredpromo.get.",
    )
    parser.add_argument(
        "--promo-start-time",
        help="Campaign start time in PST, format YYYY-MM-DD HH:MM:SS.",
    )
    parser.add_argument(
        "--promo-end-time",
        help="Campaign end time in PST, format YYYY-MM-DD HH:MM:SS.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parse_args())))
