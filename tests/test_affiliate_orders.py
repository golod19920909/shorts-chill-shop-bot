import unittest
from datetime import datetime, timezone

from database.database import Database
from services.affiliate_orders import (
    ORDER_STATUSES, extract_order_rows, normalize_order, sync_recent_orders,
)


class AffiliateOrderNormalizationTests(unittest.TestCase):
    def test_normalizer_keeps_only_allowlisted_order_fields(self):
        normalized = normalize_order({
            "order_id": 10, "sub_order_id": 20, "order_status": "Completed Settlement",
            "tracking_id": "track", "paid_amount": 1200,
            "estimated_finished_commission": 25, "settled_currency": "USD",
            "customer_parameters": "private", "custom_parameters": "private",
        }, "Payment Completed")
        self.assertEqual(normalized["order_key"], "10:20")
        self.assertEqual(normalized["estimated_finished_commission"], "25")
        self.assertNotIn("customer_parameters", normalized)
        self.assertNotIn("custom_parameters", normalized)

    def test_extracts_documented_nested_order_list(self):
        rows = extract_order_rows({"result": {"orders": {"order": [{"order_id": 1}]}}})
        self.assertEqual(rows, [{"order_id": 1}])


class AffiliateOrderSyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_sync_upserts_orders_and_handles_empty_statuses(self):
        class FakeClient:
            async def call(self, method, params):
                self.asserted_method = method
                if params["status"] == "Payment Completed":
                    return {"result": {"orders": {"order": [{
                        "order_id": 101, "sub_order_id": 202,
                        "order_status": "Payment Completed", "paid_amount": 500,
                        "estimated_paid_commission": 20, "settled_currency": "USD",
                    }]}, "total_page_no": 1}}
                return {}

        class FakeDatabase:
            def __init__(self):
                self.orders = []
            async def upsert_affiliate_orders(self, orders):
                self.orders.extend(orders)
                return len(orders)

        database = FakeDatabase()
        summary = await sync_recent_orders(
            FakeClient(), database, days=30, max_pages_per_status=1,
            now=datetime(2026, 11, 8, tzinfo=timezone.utc),
        )
        self.assertEqual(summary["rows_received"], 1)
        self.assertEqual(summary["rows_saved"], 1)
        self.assertEqual(len(database.orders), 1)
        self.assertEqual(database.orders[0]["order_key"], "101:202")
        self.assertEqual(set(summary["statuses"]), set(ORDER_STATUSES))


class AffiliateOrderDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_order_upsert_updates_status_without_duplicate_rows(self):
        database = Database(":memory:")
        await database.initialize()
        order = {
            "order_key": "1:2", "order_id": "1", "sub_order_id": "2",
            "order_status": "Payment Completed", "paid_amount": "100",
        }
        await database.upsert_affiliate_orders([order])
        updated = dict(order, order_status="Completed Settlement")
        await database.upsert_affiliate_orders([updated])
        self.assertEqual(await database.count_affiliate_orders(), 1)
        self.assertEqual(await database.get_affiliate_order_status_counts(), {"Completed Settlement": 1})
        await database.close()
