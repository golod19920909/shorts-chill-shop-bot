"""Credential-safe, read-only probe of the AliExpress Affiliate category tree.

Run from the repository root:
    .venv\\Scripts\\python.exe scripts\\diagnose_aliexpress_categories.py

The report contains only documented category fields and response metadata.
It never writes credentials, request signatures, raw request parameters, or
the complete HTTP payload. The probe is intentionally not called at startup
and does not change product-query routing or production category IDs.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config.settings import load_settings
from services.marketplaces.aliexpress_client import (
    AliExpressApiError,
    AliExpressClient,
    METHOD_CATEGORY_GET,
)


def _normalize_categories(result: dict[str, Any]) -> list[dict[str, Any]]:
    node = result.get("categories", result.get("category"))
    if isinstance(node, dict):
        raw = node.get("category", [])
    else:
        raw = node
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []

    categories: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        category_id = item.get("category_id")
        category_name = item.get("category_name")
        if category_id is None or not str(category_name or "").strip():
            continue
        parent_id = item.get("parent_category_id")
        categories.append(
            {
                "category_id": str(category_id),
                "category_name": str(category_name).strip(),
                "parent_category_id": (
                    str(parent_id) if parent_id is not None else None
                ),
            }
        )
    return categories


async def _fetch() -> dict[str, Any]:
    settings = load_settings()
    if not settings.aliexpress.is_configured:
        raise RuntimeError(
            "AliExpress credentials are not configured. Set "
            "ALIEXPRESS_APP_KEY and ALIEXPRESS_APP_SECRET in the local .env."
        )

    client = AliExpressClient(settings.aliexpress)
    try:
        response = await client.get_categories()
    finally:
        await client.aclose()

    result = response.get("result")
    if not isinstance(result, dict):
        result = response
    if not isinstance(result, dict):
        result = {}

    categories = _normalize_categories(result)
    return {
        "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
        "method": METHOD_CATEGORY_GET,
        "response_code": result.get("resp_code", response.get("resp_code")),
        "response_message": result.get("resp_msg", response.get("resp_msg")),
        "total_result_count": result.get("total_result_count"),
        "category_count": len(categories),
        "categories": categories,
        "response_keys": sorted(str(key) for key in response.keys()),
        "result_keys": sorted(str(key) for key in result.keys()),
    }


async def main() -> int:
    try:
        report = await _fetch()
    except (AliExpressApiError, RuntimeError) as exc:
        print(f"Category API diagnostic failed: {type(exc).__name__}: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001 - diagnostic must report a safe failure
        print(f"Category API diagnostic failed: {type(exc).__name__}")
        return 1

    reports_dir = Path("reports")
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    output = reports_dir / f"aliexpress_affiliate_category_tree_{stamp}.json"
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"Saved {report['category_count']} normalized categories "
        f"to {output.as_posix()}"
    )
    if not report["categories"]:
        print(
            "No normalized category records were returned. Inspect response_keys "
            "and result_keys in the report; do not infer category IDs."
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
