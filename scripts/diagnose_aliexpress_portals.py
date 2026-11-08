"""Read-only smoke test for selected AliExpress Portals Affiliate API methods.

Run from the repository root:
    python -m scripts.diagnose_aliexpress_portals

Optional featured-campaign product probe (times must be the campaign's
PST-local values as required by the API documentation). Order queries also use
PST and inspect all documented statuses over a bounded 30-day window:
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

import httpx

from config.settings import settings
from services.marketplaces.aliexpress_client import (
    AliExpressClient,
    GATEWAY_URL,
    unwrap_response,
)

METHOD_FEATURED_PROMO_GET = "aliexpress.affiliate.featuredpromo.get"
METHOD_FEATURED_PROMO_PRODUCTS_GET = (
    "aliexpress.affiliate.featuredpromo.products.get"
)
METHOD_ORDER_LIST = "aliexpress.affiliate.order.list"
PST = timezone(timedelta(hours=-8))
ORDER_STATUSES = (
    "Payment Completed",
    "Buyer Confirmed Receipt",
    "Completed Settlement",
    "Invalid",
)


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
    stats: dict[str, int],
    params: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Inspect the raw TOP envelope before the client normalizes empty results.

    Signing and gateway selection still use AliExpressClient. This diagnostic
    path reads the raw response only to distinguish API-reported empty results
    from unexpected empty payloads; it never prints request or record data.
    """
    print(f"\n[{label}]")
    print(f"  METHOD: {method}")
    try:
        request_params = client.build_params(method, params or {})
        response = await client._http_client.post(
            client.gateway,
            data=request_params,
            headers={
                "Content-Type": "application/x-www-form-urlencoded;charset=utf-8"
            },
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPStatusError as exc:
        stats["failed"] += 1
        print("  RESULT: HTTP_ERROR")
        print(f"  HTTP_STATUS: {exc.response.status_code}")
        return None
    except httpx.TimeoutException:
        stats["failed"] += 1
        print("  RESULT: CLIENT_ERROR (Timeout)")
        return None
    except httpx.HTTPError as exc:
        stats["failed"] += 1
        print(f"  RESULT: CLIENT_ERROR ({type(exc).__name__})")
        return None
    except ValueError:
        stats["failed"] += 1
        print("  RESULT: CLIENT_ERROR (NonJsonResponse)")
        return None
    except Exception as exc:
        stats["failed"] += 1
        print(f"  RESULT: CLIENT_ERROR ({type(exc).__name__})")
        return None

    if not isinstance(payload, dict):
        stats["failed"] += 1
        print("  RESULT: UNEXPECTED_PAYLOAD")
        print(f"  PAYLOAD_TYPE: {type(payload).__name__}")
        return None

    envelope_keys = [
        key for key, value in payload.items()
        if key.endswith("_response") and isinstance(value, dict)
    ]
    business = unwrap_response(payload)
    error = payload.get("error_response")
    if isinstance(error, dict):
        stats["failed"] += 1
        print("  RESULT: API_ERROR")
        print(f"  API_CODE: {error.get('code', 'not returned')}")
        print(f"  API_SUB_CODE: {error.get('sub_code', 'not returned')}")
        return None

    stats["ok"] += 1
    print("  RESULT: RESPONSE_RECEIVED")
    print(f"  HTTP_STATUS: {response.status_code}")
    print(f"  RESPONSE_ENVELOPES: {', '.join(envelope_keys) or '(none)'}")
    print(f"  BUSINESS_FIELDS: {', '.join(sorted(business.keys())) or '(empty)'}")
    if "resp_code" in business:
        print(f"  API_RESP_CODE: {business.get('resp_code')}")
    if "resp_msg" in business:
        message = " ".join(str(business.get("resp_msg") or "").split())
        print(f"  API_RESP_MSG: {message[:200] or '(empty)'}")

    resp_code = business.get("resp_code")
    if resp_code is not None and str(resp_code) == "405":
        print("  DATA_STATUS: EMPTY_RESULT_REPORTED_BY_API")
    elif resp_code is not None and str(resp_code) != "200":
        stats["failed"] += 1
        print("  DATA_STATUS: API_REPORTED_NON_SUCCESS")
    elif not business:
        print("  DATA_STATUS: EMPTY_OR_UNRECOGNIZED_BUSINESS_PAYLOAD")
    else:
        print("  DATA_STATUS: BUSINESS_PAYLOAD_PRESENT")
    return business


def _data(result: dict[str, Any] | None) -> dict[str, Any]:
    """Return the nested business result when the API wraps one."""
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
    stats = {"ok": 0, "failed": 0}
    try:
        promo_result = await _probe(
            client,
            "FEATURED_CAMPAIGNS",
            METHOD_FEATURED_PROMO_GET,
            stats,
        )
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
                    stats,
                    product_params,
                )
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

        now = datetime.now(PST)
        start_time = (now - timedelta(days=args.order_days)).strftime("%Y-%m-%d %H:%M:%S")
        end_time = now.strftime("%Y-%m-%d %H:%M:%S")
        for order_status in ORDER_STATUSES:
            order_result = await _probe(
                client,
                "AFFILIATE_ORDERS_" + order_status.upper().replace(" ", "_"),
                METHOD_ORDER_LIST,
                stats,
                {
                    "start_time": start_time,
                    "end_time": end_time,
                    "status": order_status,
                    "page_no": 1,
                    "page_size": 10,
                },
            )
            orders = _orders(order_result)
            print(f"  ORDER_STATUS: {order_status}")
            print(f"  ORDER_COUNT_RETURNED: {len(orders)}")
            print(
                "  TOTAL_RECORD_COUNT: "
                f"{_data(order_result).get('total_record_count', 'not returned') if order_result else 'not available'}"
            )
        print(
            "  NOTE: order rows and commercial details are deliberately not "
            "printed by this diagnostic. Times are PST; the window is bounded."
        )
    finally:
        await client.aclose()

    print("\nSUMMARY")
    print(f"  API_CALLS_OK: {stats['ok']}")
    print(f"  API_CALLS_FAILED: {stats['failed']}")
    print("  DATABASE_OPENED: no")
    print("  CONFIGURATION_FILES_CHANGED: no")
    return 1 if stats["failed"] else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only AliExpress Portals API smoke test."
    )
    parser.add_argument(
        "--order-days",
        type=int,
        default=30,
        choices=range(1, 91),
        metavar="1..90",
        help="bounded order-query window in days (default: 30; API times are PST).",
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
