"""Bounded, privacy-conscious sync of AliExpress affiliate order line items.

The Affiliate order-list endpoint is documented but marked legacy/deprecated in
some official references. This service is deliberately explicit and bounded:
it stores only allowlisted order/product/status/estimated-money fields, never
buyer details or custom_parameters. A 405 empty response remains an empty page.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from services.marketplaces.aliexpress_client import AliExpressApiError

METHOD_ORDER_LIST = "aliexpress.affiliate.order.list"
PST = timezone(timedelta(hours=-8))
ORDER_STATUSES = (
    "Payment Completed",
    "Buyer Confirmed Receipt",
    "Completed Settlement",
    "Invalid",
)
PAGE_SIZE = 50


def _text(value: Any) -> str | None:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return None
    return str(value)


def extract_order_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Unwrap the documented result.orders.order list without guessing fields."""
    result = payload.get("result")
    if not isinstance(result, dict):
        return []
    orders = result.get("orders")
    if isinstance(orders, list):
        return [row for row in orders if isinstance(row, dict)]
    if isinstance(orders, dict):
        rows = orders.get("order")
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
        if isinstance(rows, dict):
            return [rows]
    return []


def normalize_order(row: dict[str, Any], requested_status: str) -> dict[str, str] | None:
    """Keep only documented fields needed for affiliate reporting."""
    order_id = _text(row.get("order_id"))
    sub_order_id = _text(row.get("sub_order_id"))
    if not order_id or not sub_order_id:
        return None
    order = {
        "order_key": f"{order_id}:{sub_order_id}",
        "order_id": order_id,
        "sub_order_id": sub_order_id,
        "order_status": _text(row.get("order_status")) or requested_status,
    }
    for field in (
        "tracking_id", "product_id", "product_title", "commission_rate",
        "paid_amount", "estimated_paid_commission", "finished_amount",
        "estimated_finished_commission", "settled_currency", "created_time",
        "paid_time", "finished_time",
    ):
        value = _text(row.get(field))
        if value is not None:
            order[field] = value
    return order


async def sync_recent_orders(
    client,
    database,
    *,
    days: int = 30,
    max_pages_per_status: int = 2,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Fetch a bounded recent window for all documented order statuses.

    ``estimated_*_commission`` values are stored verbatim as estimates. The
    service never converts currencies or relabels estimates as paid commission.
    """
    days = max(1, min(int(days), 90))
    max_pages = max(1, min(int(max_pages_per_status), 10))
    moment = (now or datetime.now(PST)).astimezone(PST)
    start_time = (moment - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    end_time = moment.strftime("%Y-%m-%d %H:%M:%S")
    summary: dict[str, Any] = {
        "days": days, "statuses": {}, "pages_requested": 0,
        "rows_received": 0, "rows_saved": 0, "truncated_statuses": [],
    }

    for status in ORDER_STATUSES:
        status_summary = {"received": 0, "saved": 0, "pages": 0, "error": None}
        for page in range(1, max_pages + 1):
            status_summary["pages"] += 1
            summary["pages_requested"] += 1
            try:
                payload = await client.call(
                    METHOD_ORDER_LIST,
                    {
                        "start_time": start_time,
                        "end_time": end_time,
                        "status": status,
                        "page_no": page,
                        "page_size": PAGE_SIZE,
                    },
                )
            except AliExpressApiError as exc:
                status_summary["error"] = type(exc).__name__
                break

            rows = extract_order_rows(payload)
            normalized = [
                item for row in rows
                if (item := normalize_order(row, status)) is not None
            ]
            status_summary["received"] += len(rows)
            summary["rows_received"] += len(rows)
            saved = await database.upsert_affiliate_orders(normalized)
            status_summary["saved"] += saved
            summary["rows_saved"] += saved

            result = payload.get("result") if isinstance(payload, dict) else None
            total_pages = result.get("total_page_no") if isinstance(result, dict) else None
            try:
                total_pages = int(total_pages) if total_pages is not None else None
            except (TypeError, ValueError):
                total_pages = None
            if not rows or len(rows) < PAGE_SIZE or (total_pages is not None and page >= total_pages):
                break
        else:
            summary["truncated_statuses"].append(status)
        status_summary["truncated"] = status in summary["truncated_statuses"]
        summary["statuses"][status] = status_summary

    return summary
