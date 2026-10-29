"""Live smoke test against the real AliExpress Affiliate API.

Run manually (costs one real API call, never runs from the test suite):

    .venv/Scripts/python.exe _smoke_aliexpress.py

Prints a one-line verdict: SMOKE OK / SMOKE FAILED with the error class.
"""

from __future__ import annotations

import asyncio
import sys

from config.settings import load_settings
from services.marketplaces.aliexpress_client import AliExpressApiError, AliExpressClient


async def main() -> int:
    settings = load_settings()
    if not settings.aliexpress.is_configured:
        print("SMOKE SKIPPED: ALIEXPRESS_APP_KEY/SECRET not configured")
        return 0
    client = AliExpressClient(settings.aliexpress, timeout=15.0)
    try:
        result = await client.call(
            "aliexpress.affiliate.product.query",
            {
                "keywords": "wireless earbuds",
                "page_size": 5,
                "target_language": "RU",
                "target_currency": "USD",
            },
        )
    except AliExpressApiError as exc:
        print(f"SMOKE FAILED: {exc} (class={client.last_error_class})")
        return 1
    finally:
        await client.aclose()

    # ``call`` returns the ``resp_result`` node: {resp_code, resp_msg, result}.
    inner = result.get("result") if isinstance(result, dict) else None
    products = inner.get("products", {}).get("product") if isinstance(inner, dict) else None
    count = len(products) if isinstance(products, list) else 0
    print(
        f"SMOKE OK: resp answered, products={count}, "
        f"ok={client.calls_ok}, err={client.calls_err}, "
        f"retries={client.retries_spent}, empty={client.empty_results}, "
        f"result_keys={sorted(result)[:3] if isinstance(result, dict) else type(result).__name__}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
