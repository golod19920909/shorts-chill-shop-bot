"""Run bounded live quality checks for all approved thematic collections.

This is an operator diagnostic, not an offline test. It uses the production
AliExpress Affiliate API SearchService, the configured Gemini selector and the
same category-mode CurationGate used by Telegram delivery. It does not call
Telegram, write shown-product history, or modify bot state.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot import build_search_service
from config.settings import settings
from config.subcategories import get_subcategory
from config.thematic_collections import THEME_SHELVES, THEMATIC_COLLECTIONS
from curation.integration import CurationGate
from curation.pipeline import MODE_CATEGORY
from services.subcategory_quality import subcategory_product_type

logger = logging.getLogger("thematic_collections_live")
MINIMUM_PRODUCTS = 12
TARGET_PRODUCTS = 20


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run real AliExpress/Gemini checks for all thematic collections."
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=2,
        choices=range(1, 6),
        metavar="{1,2,3,4,5}",
        help="Maximum documented pages per route (default: 2).",
    )
    parser.add_argument(
        "--country",
        default=None,
        help="Optional ISO-2 ship-to country, for example UA.",
    )
    parser.add_argument(
        "--currency",
        default=None,
        help="Optional target currency for non-budget collections.",
    )
    parser.add_argument(
        "--output-dir",
        default="reports",
        help="Directory for the JSON report (default: reports).",
    )
    return parser.parse_args()


def _identity(product) -> tuple[str, str]:
    marketplace = getattr(product.marketplace, "value", product.marketplace)
    return str(marketplace), str(product.product_id)


def _provider_status(outcome) -> str:
    if outcome.failed_marketplaces and outcome.searched_marketplaces:
        return "partial_failure"
    if outcome.failed_marketplaces:
        return "failed"
    if outcome.unavailable_marketplaces:
        return "unavailable"
    return "ok"


def _product_report(product, shelf, checked_at: str) -> dict:
    try:
        product_type = subcategory_product_type(product, shelf)
    except Exception:
        product_type = None
    return {
        "marketplace": str(getattr(product.marketplace, "value", product.marketplace)),
        "product_id": product.product_id,
        "title": product.title,
        "product_type": product_type,
        "product_url": product.product_url,
        "affiliate_url": product.affiliate_url,
        "price": product.price,
        "currency": product.currency,
        "price_checked_at": checked_at,
        "provider_price_timestamp_available": False,
        "rating": product.rating,
        "positive_rate": product.positive_rate,
        "orders_count": product.orders_count,
        "has_image": bool(product.image_url),
        "has_affiliate_url": bool(product.has_affiliate_url),
    }


def _curation_report(gate: CurationGate) -> list[dict]:
    report = getattr(gate.pipeline, "last_report", None)
    return [
        {
            "stage": stage.name,
            "received": stage.received,
            "kept": stage.kept,
            "dropped": stage.dropped,
        }
        for stage in getattr(report, "stages", ())
    ]


async def _run(args: argparse.Namespace) -> int:
    country = (args.country or "").strip().upper() or None
    if country is not None and len(country) != 2:
        raise ValueError("--country must be an ISO-2 country code")
    currency = (args.currency or "").strip().upper() or None

    search_service, _ai_service = build_search_service(settings)
    search_service.max_search_pages = min(
        max(1, int(args.max_pages)),
        max(1, int(search_service.max_search_pages)),
    )
    gate = CurationGate()
    started_at = datetime.now(timezone.utc)
    started = time.perf_counter()
    results: list[dict] = []
    summary: Counter = Counter()
    identities_by_group: dict[str, dict[tuple[str, str], str]] = defaultdict(dict)

    logger.info(
        "Starting thematic live validation: shelves=%d max_pages=%d "
        "selector_available=%s country=%s currency=%s",
        len(THEME_SHELVES),
        search_service.max_search_pages,
        search_service.has_feed_selector,
        country or "provider-default",
        currency or "provider-default",
    )

    try:
        for index, shelf in enumerate(THEME_SHELVES, start=1):
            item_started = time.perf_counter()
            checked_at = datetime.now(timezone.utc).isoformat()
            ceiling = shelf.max_price
            target_currency = "USD" if ceiling is not None else currency
            price_rejections = Counter()
            try:
                subcategory = get_subcategory(shelf.code)
                if subcategory is None:
                    raise RuntimeError(f"Configured thematic shelf is missing: {shelf.code}")
                outcome = await search_service.find_by_subcategory(
                    subcategory,
                    country=country,
                    currency=target_currency,
                    max_price=ceiling,
                )
                after_search = list(outcome.products)
                selection_diagnostics = (
                    dict(search_service.last_subcategory_selection_stats)
                    if search_service.last_subcategory_selection_stats is not None
                    else None
                )

                if ceiling is not None:
                    price_valid = []
                    for product in after_search:
                        if product.price is None:
                            price_rejections["missing_price"] += 1
                        elif (product.currency or "").upper() != "USD":
                            price_rejections["unconfirmed_or_non_usd_currency"] += 1
                        elif product.price > ceiling:
                            price_rejections["above_budget"] += 1
                        else:
                            price_valid.append(product)
                    after_search = price_valid

                # Match Telegram delivery: category-mode curation, no ordinary
                # user history and no write to global novelty history.
                curated = await gate.curate(
                    after_search,
                    user_id=None,
                    mode=MODE_CATEGORY,
                    request=outcome.request,
                )
                curation_stages = _curation_report(gate) if after_search else []
                unique_count = len({_identity(product) for product in curated})
                provider_status = _provider_status(outcome)
                if len(curated) >= MINIMUM_PRODUCTS:
                    status = "ready"
                    shortfall_reason = None
                else:
                    status = "not_ready"
                    candidate_pool = (selection_diagnostics or {}).get("candidate_pool", 0)
                    approved = (selection_diagnostics or {}).get(
                        "gemini_approved_candidates"
                    )
                    if provider_status in {"failed", "unavailable"}:
                        shortfall_reason = "provider_failed_or_unavailable"
                    elif provider_status == "partial_failure":
                        shortfall_reason = "provider_partial_failure"
                    elif candidate_pool < MINIMUM_PRODUCTS:
                        shortfall_reason = "candidate_pool_below_minimum"
                    elif approved is not None and approved < MINIMUM_PRODUCTS:
                        shortfall_reason = "gemini_approved_below_minimum"
                    elif len(after_search) >= MINIMUM_PRODUCTS:
                        shortfall_reason = "curation_gate_below_minimum"
                    else:
                        shortfall_reason = "search_identity_or_price_validation_below_minimum"

                group = (
                    "budget_finds"
                    if shelf.code.startswith("theme_budget_")
                    else shelf.code.removeprefix("theme_")
                )
                identities_by_group[group].update(
                    {_identity(product): shelf.code for product in curated}
                )
                type_counts = Counter(
                    subcategory_product_type(product, subcategory) or "unclassified"
                    for product in curated
                )
                results.append(
                    {
                        "code": shelf.code,
                        "collection_group": group,
                        "title": shelf.title,
                        "description": shelf.description,
                        "category_code": shelf.category_code,
                        "query_routes": list(shelf.query_routes),
                        "product_types": list(shelf.product_types),
                        "exclude_terms": list(shelf.exclude_terms),
                        "budget_ceiling_usd": ceiling,
                        "status": status,
                        "shortfall_reason": shortfall_reason,
                        "provider_status": provider_status,
                        "raw_candidates": outcome.products_before_filtering,
                        "dropped_as_irrelevant": outcome.products_dropped_as_irrelevant,
                        "relevance_drop_reasons": dict(outcome.relevance_drop_reasons),
                        "after_constraints": outcome.products_after_constraints,
                        "candidate_pool": (selection_diagnostics or {}).get("candidate_pool"),
                        "gemini_approved_candidates": (
                            (selection_diagnostics or {}).get("gemini_approved_candidates")
                        ),
                        "selection_diagnostics": selection_diagnostics,
                        "after_search_and_price_validation": len(after_search),
                        "after_curation": len(curated),
                        "unique_after_curation": unique_count,
                        "price_rejections": dict(price_rejections),
                        "curation_stages": curation_stages,
                        "type_counts": dict(type_counts),
                        "searched_marketplaces": outcome.searched_marketplaces,
                        "failed_marketplaces": outcome.failed_marketplaces,
                        "unavailable_marketplaces": outcome.unavailable_marketplaces,
                        "checked_at": checked_at,
                        "duration_ms": round((time.perf_counter() - item_started) * 1000),
                        "products": [
                            _product_report(product, subcategory, checked_at)
                            for product in curated
                        ],
                    }
                )
            except Exception as exc:
                logger.exception(
                    "Thematic live validation failed: %s (%d/%d)",
                    shelf.code,
                    index,
                    len(THEME_SHELVES),
                )
                results.append(
                    {
                        "code": shelf.code,
                        "collection_group": (
                            "budget_finds"
                            if shelf.code.startswith("theme_budget_")
                            else shelf.code.removeprefix("theme_")
                        ),
                        "title": shelf.title,
                        "status": "exception",
                        "error": f"{type(exc).__name__}: {exc}",
                        "duration_ms": round((time.perf_counter() - item_started) * 1000),
                        "after_curation": 0,
                        "products": [],
                    }
                )
            summary[results[-1]["status"]] += 1
            logger.info(
                "LIVE THEME %d/%d code=%s status=%s final=%d provider=%s "
                "duration_ms=%d",
                index,
                len(THEME_SHELVES),
                results[-1]["code"],
                results[-1]["status"],
                results[-1].get("after_curation", 0),
                results[-1].get("provider_status", "exception"),
                results[-1]["duration_ms"],
            )
    finally:
        for provider in search_service.providers:
            close = getattr(provider, "aclose", None)
            if callable(close):
                try:
                    await close()
                except Exception:
                    logger.warning("Could not close provider %s", provider, exc_info=True)

    # Compare product identities across independent collection searches.
    # Budget tiers are one editorial collection, so tier-to-tier repeats are
    # reported separately from duplicates between the three other themes.
    cross_collection_duplicates = []
    groups = list(identities_by_group.items())
    for index, (left_name, left) in enumerate(groups):
        for right_name, right in groups[index + 1 :]:
            if left_name == right_name:
                continue
            for identity in sorted(set(left).intersection(right)):
                cross_collection_duplicates.append(
                    {
                        "marketplace": identity[0],
                        "product_id": identity[1],
                        "left_collection": left_name,
                        "right_collection": right_name,
                        "left_shelf": left[identity],
                        "right_shelf": right[identity],
                    }
                )

    budget_tier_duplicates = []
    budget_rows = [
        row for row in results
        if row.get("collection_group") == "budget_finds"
    ]
    for index, left in enumerate(budget_rows):
        left_ids = {
            (p.get("marketplace"), p.get("product_id")): p
            for p in left.get("products", [])
        }
        for right in budget_rows[index + 1 :]:
            right_ids = {
                (p.get("marketplace"), p.get("product_id")): p
                for p in right.get("products", [])
            }
            for identity in sorted(set(left_ids).intersection(right_ids)):
                budget_tier_duplicates.append(
                    {
                        "marketplace": identity[0],
                        "product_id": identity[1],
                        "left_tier": left["code"],
                        "right_tier": right["code"],
                    }
                )

    finished = time.perf_counter()
    ready_groups = {
        row["code"]: row.get("after_curation", 0) >= MINIMUM_PRODUCTS
        for row in results
    }
    report = {
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": round(finished - started, 2),
        "marketplace": "AliExpress Affiliate API only",
        "country": country,
        "max_pages_per_route": search_service.max_search_pages,
        "selector_available": search_service.has_feed_selector,
        "editorial_collection_count": len(THEMATIC_COLLECTIONS),
        "approved_collection_titles": [item["title"] for item in THEMATIC_COLLECTIONS],
        "budget_tiers": [
            {"code": shelf.code, "max_price_usd": shelf.max_price, "title": shelf.title}
            for shelf in THEME_SHELVES
            if shelf.max_price is not None
        ],
        "minimum_products": MINIMUM_PRODUCTS,
        "target_products": TARGET_PRODUCTS,
        "summary": dict(summary),
        "provider_status_summary": dict(
            Counter(row.get("provider_status", "exception") for row in results)
        ),
        "ready_shelves": ready_groups,
        "cross_collection_duplicates": cross_collection_duplicates,
        "budget_tier_duplicates": budget_tier_duplicates,
        "price_freshness_policy": (
            "Prices are the values returned by the Affiliate API during this run. "
            "The normalized Product model exposes no per-listing update timestamp; "
            "checked_at is the retrieval timestamp, not a provider update timestamp."
        ),
        "history_policy": (
            "This diagnostic compares independent search results without writing "
            "FLOW_THEMES history. Telegram handlers separately exclude products "
            "actually shown to the user across all four editorial collections."
        ),
        "results": results,
    }

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / (
        "thematic_collections_live_"
        + started_at.strftime("%Y%m%d_%H%M%S")
        + ".json"
    )
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Checked {len(THEME_SHELVES)} thematic shelves: {dict(summary)}")
    print(f"Cross-collection duplicates: {len(cross_collection_duplicates)}")
    print(f"Budget-tier duplicates: {len(budget_tier_duplicates)}")
    print(f"Report: {output_path}")
    if summary.get("exception", 0):
        return 2
    if summary.get("not_ready", 0):
        return 1
    return 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )
    return asyncio.run(_run(_arguments()))


if __name__ == "__main__":
    raise SystemExit(main())
