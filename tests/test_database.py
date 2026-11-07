"""Database layer tests.

Each test uses a temporary SQLite file, so the real database is never
touched and tests can run in parallel.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import aiosqlite

from database.database import Database
from database.models import FLOW_SEARCH
from models.product import Marketplace, Product


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp_dir.name) / "test.db")
        self.db = Database(self.db_path)

    def tearDown(self) -> None:
        self._tmp_dir.cleanup()

    def run_async(self, coro):
        return asyncio.run(coro)

    def test_initialize_creates_file_and_tables(self) -> None:
        self.assertFalse(Path(self.db_path).exists())
        self.run_async(self.db.initialize())

        self.assertTrue(Path(self.db_path).exists())
        tables = self.run_async(self.db.list_tables())

        for expected in (
            "users",
            "search_history",
            "favorites",
            "saved_products",
            "tracked_products",
            "price_history",
            "gemini_usage",
        ):
            with self.subTest(table=expected):
                self.assertIn(expected, tables)

    def test_gemini_usage_attempts_are_persisted_idempotently_and_aggregated(self) -> None:
        self.run_async(self.db.initialize())
        attempt = {
            "attempt_id": "attempt-1",
            "request_id": "request-1",
            "operation": "conversation_selection",
            "model": "gemini-3.1-flash-lite",
            "duration_ms": 1234.0,
            "outcome": "ok",
            "prompt_token_count": 1000,
            "candidates_token_count": 100,
            "total_token_count": 1120,
            "cached_content_token_count": 0,
            "thoughts_token_count": 20,
            "cost_usd": 0.00043,
            "cost_status": "estimated",
            "pricing_version": "test-pricing",
            "created_at": "2026-11-07T09:00:00+00:00",
        }
        self.run_async(self.db.record_gemini_attempt(attempt))
        self.run_async(self.db.record_gemini_attempt(attempt))

        stats = self.run_async(
            self.db.get_gemini_usage_stats("2026-11-07 00:00:00")
        )
        self.assertEqual(stats["totals"]["attempts"], 1)
        self.assertEqual(stats["totals"]["prompt_tokens"], 1000)
        self.assertEqual(stats["totals"]["thoughts_tokens"], 20)
        self.assertAlmostEqual(stats["totals"]["estimated_cost_usd"], 0.00043)
        self.assertEqual(stats["models"][0]["model"], "gemini-3.1-flash-lite")
        self.assertEqual(
            stats["operations"][0]["operation"], "conversation_selection"
        )

    def test_gemini_usage_requests_are_grouped_without_prompt_data(self) -> None:
        self.run_async(self.db.initialize())
        base = {
            "request_id": "request-group-1",
            "operation": "conversation_turn",
            "model": "gemini-3.1-flash-lite",
            "duration_ms": 100.0,
            "outcome": "ok",
            "prompt_token_count": 100,
            "candidates_token_count": 10,
            "total_token_count": 110,
            "cost_usd": 0.00004,
            "cost_status": "estimated",
            "pricing_version": "test",
            "created_at": "2026-11-07T09:00:00+00:00",
        }
        first = dict(base, attempt_id="group-attempt-1")
        second = dict(
            base,
            attempt_id="group-attempt-2",
            operation="conversation_selection",
            model="gemini-3.8-flash",
            prompt_token_count=200,
            candidates_token_count=20,
            total_token_count=220,
            cost_usd=0.00009,
        )
        self.run_async(self.db.record_gemini_attempt(first))
        self.run_async(self.db.record_gemini_attempt(second))

        rows = self.run_async(
            self.db.list_gemini_usage_requests("2026-11-07 00:00:00")
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["request_id"], "request-group-1")
        self.assertEqual(rows[0]["attempts"], 2)
        self.assertEqual(rows[0]["prompt_tokens"], 300)
        self.assertEqual(rows[0]["total_tokens"], 330)
        self.assertAlmostEqual(rows[0]["estimated_cost_usd"], 0.00013)
        self.assertIn("gemini-3.1-flash-lite", rows[0]["models"])
        self.assertNotIn("prompt", rows[0])

    def test_initialize_is_idempotent(self) -> None:
        self.run_async(self.db.initialize())
        self.run_async(self.db.initialize())
        self.assertTrue(Path(self.db_path).exists())

    def test_creates_missing_parent_directory(self) -> None:
        nested = Path(self._tmp_dir.name) / "nested" / "dir" / "x.db"
        self.run_async(Database(str(nested)).initialize())
        self.assertTrue(nested.exists())

    def test_get_or_create_user_is_idempotent(self) -> None:
        self.run_async(self.db.initialize())

        first_id = self.run_async(
            self.db.get_or_create_user(telegram_id=555, username="vasya", first_name="Вася")
        )
        second_id = self.run_async(
            self.db.get_or_create_user(telegram_id=555, username="vasya_new")
        )

        self.assertEqual(first_id, second_id)
        user = self.run_async(self.db.get_user(555))
        self.assertIsNotNone(user)
        self.assertEqual(user["username"], "vasya_new")


    def test_the_shown_history_keeps_one_scope_per_flow(self) -> None:
        """A search row and a rotation row are different pieces of state."""

        self.run_async(self.db.initialize())
        user_id = self.run_async(self.db.get_or_create_user(555, "vasya", "Вася"))

        self.run_async(self.db.add_shown_product(user_id, "aliexpress", "searched"))
        self.run_async(self.db.add_feed_shown(user_id, "aliexpress", "rotated"))

        self.assertEqual(
            self.run_async(self.db.get_shown_products(user_id, flow=FLOW_SEARCH)),
            [("aliexpress", "searched")],
        )
        self.assertEqual(
            self.run_async(self.db.get_feed_shown(user_id)),
            [("aliexpress", "rotated")],
        )
        # The unscoped read is the "everything this user has seen" view.
        self.assertEqual(
            self.run_async(self.db.get_shown_products(user_id)),
            [("aliexpress", "searched"), ("aliexpress", "rotated")],
        )

    def test_the_same_product_can_live_in_both_scopes(self) -> None:
        """A search never marks a product as "rotated", and the reverse."""

        self.run_async(self.db.initialize())
        user_id = self.run_async(self.db.get_or_create_user(555, "vasya", "Вася"))

        self.run_async(self.db.add_shown_product(user_id, "aliexpress", "same"))
        self.run_async(self.db.add_feed_shown(user_id, "aliexpress", "same"))

        self.assertEqual(
            self.run_async(self.db.get_shown_products(user_id, flow=FLOW_SEARCH)),
            [("aliexpress", "same")],
        )
        self.assertEqual(
            self.run_async(self.db.get_feed_shown(user_id)),
            [("aliexpress", "same")],
        )

    def test_clearing_the_rotation_keeps_the_search_history(self) -> None:
        self.run_async(self.db.initialize())
        user_id = self.run_async(self.db.get_or_create_user(555, "vasya", "Вася"))
        self.run_async(self.db.add_shown_product(user_id, "aliexpress", "searched"))
        self.run_async(self.db.add_feed_shown(user_id, "aliexpress", "rotated"))

        self.run_async(self.db.clear_feed_shown(user_id))

        self.assertEqual(self.run_async(self.db.get_feed_shown(user_id)), [])
        self.assertEqual(
            self.run_async(self.db.get_shown_products(user_id, flow=FLOW_SEARCH)),
            [("aliexpress", "searched")],
        )

    def test_the_rotation_cursor_round_trips_and_defaults_to_one(self) -> None:
        self.run_async(self.db.initialize())
        user_id = self.run_async(self.db.get_or_create_user(555, "vasya", "Вася"))

        self.assertEqual(self.run_async(self.db.get_feed_page(user_id)), 1)
        self.run_async(self.db.set_feed_page(user_id, 4))

        self.assertEqual(self.run_async(self.db.get_feed_page(user_id)), 4)
        # A fresh connection (a restarted bot) reads the persisted cursor.
        self.assertEqual(
            self.run_async(Database(self.db_path).get_feed_page(user_id)), 4
        )
        # A user without a row simply starts at the first step.
        other = self.run_async(self.db.get_or_create_user(777, "other", "Other"))
        self.assertEqual(self.run_async(self.db.get_feed_page(other)), 1)

    def test_a_legacy_shown_table_is_migrated_to_scopes(self) -> None:
        """An older database keeps its rows, read as ROTATION rows.

        The old table had no ``flow`` column and a narrower UNIQUE key, so it
        is rebuilt once. Rows that carry no scope become rotation rows: that is
        the conservative side (they can only make the feed skip a card) and it
        gives the search scope a clean start instead of inheriting state that
        was written for a different screen.
        """

        self.run_async(self.db.initialize())
        user_id = self.run_async(self.db.get_or_create_user(555, "vasya", "Вася"))
        self.run_async(self.db.add_feed_shown(user_id, "aliexpress", "legacy"))

        async def rebuild_legacy_schema() -> None:
            async with aiosqlite.connect(self.db_path) as conn:
                await conn.execute("DROP TABLE popular_shown;")
                await conn.execute(
                    "CREATE TABLE popular_shown ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    "user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,"
                    "marketplace TEXT NOT NULL,"
                    "product_id TEXT NOT NULL,"
                    "shown_at TEXT NOT NULL DEFAULT (datetime('now')),"
                    "UNIQUE (user_id, marketplace, product_id));"
                )
                await conn.execute(
                    "INSERT INTO popular_shown (user_id, marketplace, product_id) "
                    "VALUES (?, 'aliexpress', 'legacy')",
                    (user_id,),
                )
                await conn.commit()

        self.run_async(rebuild_legacy_schema())

        migrated = Database(self.db_path)
        self.run_async(migrated.initialize())
        self.run_async(migrated.initialize())  # idempotent

        self.assertEqual(
            self.run_async(migrated.get_feed_shown(user_id)),
            [("aliexpress", "legacy")],
        )
        self.assertEqual(
            self.run_async(migrated.get_shown_products(user_id, flow=FLOW_SEARCH)),
            [],
        )


if __name__ == "__main__":
    unittest.main()
