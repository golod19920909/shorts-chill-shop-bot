"""Run bounded real AliExpress quality checks for every catalog subcategory.

This is an operator smoke test, not an offline unit test. It uses the same
SearchService, AliExpress Affiliate API and single Gemini selector as the bot.
The default run checks every configured subcategory with at most two provider
pages per route and writes a JSON report for review.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

# Support direct execution as `python scripts/validate_catalog_subcategories.py`:
# Python otherwise places only the scripts/ directory on sys.path.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot import build_search_service
from config.settings import settings
from config.subcategories import get_all_subcategories
from services.subcategory_quality import subcategory_product_type

logger = logging.getLogger("catalog_subcategory_live")


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run real AliExpress searches for all catalog subcategories."
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=2,
        choices=range(1, 6),
        metavar="{1,2,3,4,5}",
        help="Maximum documented pages per configured route (default: 2).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Optional number of subcategories to check (default: all).",
    )
    parser.add_argument(
        "--country",
        default=None,
        help="Optional ISO-2 ship-to country, for example UA.",
    )
    parser.add_argument(
        "--currency",
        default=None,
        help="Optional documented target currency, for example USD.",
    )
    parser.add_argument(
        "--output-dir",
        default="reports",
        help="Directory for the JSON report (default: reports).",
    )
    return parser.parse_args()


def _product_report(product, subcategory) -> dict:
    return {
        "product_id": product.product_id,
        "title": product.title,
        "product_type": subcategory_product_type(product, subcategory),
        "price": product.price,
        "currency": product.currency,
        "rating": product.rating,
        "positive_rate": product.positive_rate,
        "orders_count": product.orders_count,
        "has_image": bool(product.image_url),
        "has_affiliate_url": bool(product.has_affiliate_url),
    }


async def _run(args: argparse.Namespace) -> int:
    subcategories = list(get_all_subcategories())
    if args.limit:
        if args.limit < 1:
            raise ValueError("--limit must be positive")
        subcategories = subcategories[: args.limit]

    country = (args.country or "").strip().upper() or None
    if country is not None and len(country) != 2:
        raise ValueError("--country must be an ISO-2 country code")
    currency = (args.currency or "").strip().upper() or None

    search_service, _ai_service = build_search_service(settings)
    search_service.max_search_pages = min(
        max(1, int(args.max_pages)),
        max(1, int(search_service.max_search_pages)),
    )

    started_at = datetime.now(timezone.utc)
    started = time.perf_counter()
    results: list[dict] = []
    summary = Counter()

    logger.info(
        "Starting real catalog validation: subcategories=%d max_pages=%d "
        "selector_available=%s country=%s currency=%s",
        len(subcategories),
        search_service.max_search_pages,
        search_service.has_feed_selector,
        country or "provider-default",
        currency or "provider-default",
    )

    try:
        for index, subcategory in enumerate(subcategories, start=1):
            item_started = time.perf_counter()
            try:
                outcome = await search_service.find_by_subcategory(
                    subcategory, country=country, currency=currency
                )
                products = list(outcome.products)
                selection_diagnostics = (
                    dict(search_service.last_subcategory_selection_stats)
                    if search_service.last_subcategory_selection_stats is not None
                    else None
                )
                type_counts = Counter(
                    subcategory_product_type(product, subcategory) or "unclassified"
                    for product in products
                )
                if len(products) >= 20:
                    status = "20_cards"
                elif products:
                    status = "short"
                elif outcome.failed_marketplaces and not outcome.searched_marketplaces:
                    status = "provider_error"
                else:
                    status = "empty"

                # Shelf result count and provider health are separate facts.
                # A route can return useful rows while another route fails;
                # don't hide that partial failure behind "short" or "empty".
                if outcome.failed_marketplaces and outcome.searched_marketplaces:
                    provider_status = "partial_failure"
                elif outcome.failed_marketplaces:
                    provider_status = "failed"
                elif outcome.unavailable_marketplaces:
                    provider_status = "unavailable"
                else:
                    provider_status = "ok"

                if len(products) >= 20:
                    shortfall_reason = None
                elif provider_status in {"failed", "unavailable"}:
                    shortfall_reason = "provider_unavailable_or_failed"
                elif provider_status == "partial_failure":
                    shortfall_reason = "provider_partial_failure"
                elif (selection_diagnostics or {}).get("candidate_pool", 0) < 20:
                    shortfall_reason = "filtered_candidate_pool_below_20"
                elif (
                    (selection_diagnostics or {}).get("gemini_approved_candidates") is not None
                    and (selection_diagnostics or {}).get("gemini_approved_candidates") < 20
                ):
                    shortfall_reason = "selector_approved_fewer_than_20"
                else:
                    shortfall_reason = "finalizer_or_deduplication_shortfall"

                row = {
                    "code": subcategory.code,
                    "category_code": subcategory.category_code,
                    "title": subcategory.title,
                    "query_routes": list(subcategory.query_routes),
                    "route_count": len(subcategory.query_routes),
                    "status": status,
                    "provider_status": provider_status,
                    "shortfall_reason": shortfall_reason,
                    "raw_candidates": outcome.products_before_filtering,
                    "dropped_as_irrelevant": outcome.products_dropped_as_irrelevant,
                    "relevance_drop_reasons": dict(outcome.relevance_drop_reasons),
                    "after_constraints": outcome.products_after_constraints,
                    "final_count": len(products),
                    "type_counts": dict(type_counts),
                    "selection_diagnostics": selection_diagnostics,
                    "searched_marketplaces": outcome.searched_marketplaces,
                    "failed_marketplaces": outcome.failed_marketplaces,
                    "unavailable_marketplaces": outcome.unavailable_marketplaces,
                    "duration_ms": round((time.perf_counter() - item_started) * 1000),
                    "products": [
                        _product_report(product, subcategory)
                        for product in products
                    ],
                }
            except Exception as exc:  # one shelf must not stop the 96-shelf run
                logger.exception(
                    "Live catalog validation failed: %s (%d/%d)",
                    subcategory.code,
                    index,
                    len(subcategories),
                )
                row = {
                    "code": subcategory.code,
                    "category_code": subcategory.category_code,
                    "title": subcategory.title,
                    "status": "exception",
                    "error": f"{type(exc).__name__}: {exc}",
                    "duration_ms": round((time.perf_counter() - item_started) * 1000),
                    "final_count": 0,
                    "products": [],
                }

            results.append(row)
            summary[row["status"]] += 1
            logger.info(
                "LIVE SUBCATEGORY %d/%d code=%s status=%s raw=%d "
                "dropped=%d final=%d provider_status=%s types=%s duration_ms=%d",
                index,
                len(subcategories),
                row["code"],
                row["status"],
                row.get("raw_candidates", 0),
                row.get("dropped_as_irrelevant", 0),
                row.get("final_count", 0),
                row.get("provider_status", "exception"),
                row.get("type_counts", {}),
                row["duration_ms"],
            )
    finally:
        for provider in search_service.providers:
            close = getattr(provider, "aclose", None)
            if callable(close):
                try:
                    await close()
                except Exception:
                    logger.warning("Could not close provider %s", provider, exc_info=True)

    finished = time.perf_counter()
    report = {
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": round(finished - started, 2),
        "max_pages_per_route": search_service.max_search_pages,
        "country": country,
        "currency": currency,
        "total_subcategories": len(subcategories),
        "summary": dict(summary),
        "provider_status_summary": dict(Counter(
            row.get("provider_status", "exception") for row in results
        )),
        "rating_semantics": (
            "AliExpress Affiliate product.query exposes evaluate_rate "
            "(positive feedback percentage), not a 0-5 star rating. "
            "rating may therefore be null while positive_rate is populated."
        ),
        "results": results,
    }

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / (
        "catalog_subcategory_live_"
        + started_at.strftime("%Y%m%d_%H%M%S")
        + ".json"
    )
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    logger.info(
        "CATALOG LIVE VALIDATION COMPLETE checked=%d summary=%s "
        "duration_s=%.2f report=%s",
        len(subcategories),
        dict(summary),
        finished - started,
        output_path,
    )
    print(f"Checked {len(subcategories)} subcategories: {dict(summary)}")
    print(f"Report: {output_path}")
    return 0 if summary.get("exception", 0) == 0 else 1


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )
    return asyncio.run(_run(_arguments()))


if __name__ == "__main__":
    raise SystemExit(main())
