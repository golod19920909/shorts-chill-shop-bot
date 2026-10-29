"""Tests for the per-user view history (``user_view_history``).

The curation layer asks the database exactly ONE question - "has this user
already met this product?" - and the answer has to be per-user, idempotent and
able to survive a restart. Everything here runs against a REAL in-memory SQLite
database (``Database(":memory:")``), so the schema, the SQL, the uniqueness
constraint and the curation hand-off are exercised without writing a byte to
disk; one class repeats the round trip on an ordinary file-backed database.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

import aiosqlite

from curation import FRESH_ENOUGH, CurationPipeline
from database.database import MEMORY_DB_PATH, Database
from database.models import ALL_TABLES, CREATE_USER_VIEW_HISTORY_TABLE

#: Eight titles of eight different kinds - quality, dedup and diversity leave
#: them alone, so a feed test only watches the history stage.
FEED_TITLES = (
    "Механическая клавиатура RGB Red Switch",
    "Наушники беспроводные Bluetooth",
    "Чехол силиконовый для телефона",
    "Мышь вертикальная эргономичная",
    "Настольная лампа светодиодная",
    "Бутылка для воды спортивная",
    "Рюкзак городской водостойкий",
    "Термокружка с крышкой",
)


def feed(count: int) -> list[dict]:
    """A provider-shaped page of ``count`` distinct candidates."""

    return [
        {"product_id": str(index), "title": title, "price": 10.0 + index}
        for index, title in enumerate(FEED_TITLES[:count])
    ]


class InMemoryViewHistoryTests(unittest.IsolatedAsyncioTestCase):
    """The storage half: write, read, cap, isolate - all on ``:memory:``."""

    async def asyncSetUp(self) -> None:
        self.db = Database(MEMORY_DB_PATH)
        await self.db.initialize()
        self.user = await self.db.get_or_create_user(555, "vasya", "Вася")

    async def asyncTearDown(self) -> None:
        await self.db.close()

    async def columns(self, table: str) -> set[str]:
        """The real columns of a table, read from the database itself."""

        async with aiosqlite.connect(self.db.db_path, uri=self.db.is_memory) as conn:
            cursor = await conn.execute(f"PRAGMA table_info({table})")
            return {row[1] for row in await cursor.fetchall()}

    async def test_an_in_memory_database_serves_the_real_schema(self) -> None:
        self.assertTrue(self.db.is_memory)
        tables = await self.db.list_tables()

        self.assertIn("user_view_history", tables)
        # ...because the schema constant is part of the initialization list.
        self.assertIn(CREATE_USER_VIEW_HISTORY_TABLE, ALL_TABLES)

    async def test_the_table_has_the_documented_columns(self) -> None:
        self.assertEqual(
            await self.columns("user_view_history"),
            {"id", "user_id", "product_id", "viewed_at"},
        )

    async def test_views_round_trip(self) -> None:
        await self.db.add_user_views(self.user, ["100", "200", "300"])

        self.assertEqual(
            await self.db.get_user_seen_ids(self.user), {"100", "200", "300"}
        )

    async def test_every_view_is_timestamped_by_the_database(self) -> None:
        await self.db.add_user_views(self.user, ["100"])

        async with aiosqlite.connect(self.db.db_path, uri=self.db.is_memory) as conn:
            cursor = await conn.execute(
                "SELECT viewed_at FROM user_view_history WHERE user_id = ?",
                (self.user,),
            )
            row = await cursor.fetchone()

        self.assertIsNotNone(row)
        self.assertTrue(str(row[0]).strip())

    async def test_repeated_views_do_not_add_a_row(self) -> None:
        await self.db.add_user_views(self.user, ["100"])
        await self.db.add_user_views(self.user, ["100", "100"])

        counts = await self.db.table_row_counts()

        self.assertEqual(counts["user_view_history"], 1)

    async def test_the_history_is_per_user(self) -> None:
        other = await self.db.get_or_create_user(777, "other", "Другой")

        await self.db.add_user_views(self.user, ["100"])
        await self.db.add_user_views(other, ["200"])

        self.assertEqual(await self.db.get_user_seen_ids(self.user), {"100"})
        self.assertEqual(await self.db.get_user_seen_ids(other), {"200"})

    async def test_the_limit_keeps_the_newest_ids(self) -> None:
        await self.db.add_user_views(self.user, ["1", "2", "3", "4", "5"])

        self.assertEqual(await self.db.get_user_seen_ids(self.user, limit=2), {"5", "4"})
        self.assertEqual(
            await self.db.get_user_seen_ids(self.user), {"1", "2", "3", "4", "5"}
        )

    async def test_a_zero_limit_answers_that_nothing_was_seen(self) -> None:
        await self.db.add_user_views(self.user, ["100"])

        self.assertEqual(await self.db.get_user_seen_ids(self.user, limit=0), set())

    async def test_blank_and_null_ids_are_not_recorded(self) -> None:
        await self.db.add_user_views(self.user, ["", "   ", None, "100"])

        self.assertEqual(await self.db.get_user_seen_ids(self.user), {"100"})

    async def test_a_blank_batch_writes_nothing_and_raises_nothing(self) -> None:
        for batch in ([], None, [""]):
            with self.subTest(batch=batch):
                await self.db.add_user_views(self.user, batch)

        counts = await self.db.table_row_counts()

        self.assertEqual(counts["user_view_history"], 0)

    async def test_repeated_initialization_keeps_the_history(self) -> None:
        await self.db.add_user_views(self.user, ["100"])
        await self.db.initialize()

        self.assertEqual(await self.db.get_user_seen_ids(self.user), {"100"})

    async def test_two_in_memory_databases_are_isolated(self) -> None:
        other = Database(MEMORY_DB_PATH)
        await other.initialize()
        try:
            other_user = await other.get_or_create_user(1, "a", "A")
            await other.add_user_views(other_user, ["900"])

            self.assertEqual(await other.get_user_seen_ids(other_user), {"900"})
            self.assertEqual(await self.db.get_user_seen_ids(self.user), set())
        finally:
            await other.close()


class FileViewHistoryTests(unittest.TestCase):
    """The same methods on the ordinary file-backed database."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "views.db")
        self.db = Database(self.db_path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_async(self, coro):
        return asyncio.run(coro)

    def test_the_history_survives_a_restart(self) -> None:
        self.run_async(self.db.initialize())
        user = self.run_async(self.db.get_or_create_user(555, "vasya", "Вася"))
        self.run_async(self.db.add_user_views(user, ["100", "200"]))

        # A new process opens the same file: the skip list is still there.
        reopened = Database(self.db_path)
        self.run_async(reopened.initialize())

        self.assertFalse(reopened.is_memory)
        self.assertEqual(
            self.run_async(reopened.get_user_seen_ids(user)), {"100", "200"}
        )

    def test_the_file_database_holds_no_in_memory_keeper(self) -> None:
        self.assertIsNone(self.db._memory_keeper)  # noqa: SLF001 - contract test

        self.run_async(self.db.initialize())
        self.run_async(self.db.close())

        self.assertTrue(Path(self.db_path).exists())


class ViewHistoryCurationTests(unittest.IsolatedAsyncioTestCase):
    """Both halves together: a feed skips what the user has already met."""

    async def asyncSetUp(self) -> None:
        self.db = Database(MEMORY_DB_PATH)
        await self.db.initialize()
        self.user = await self.db.get_or_create_user(555, "vasya", "Вася")
        self.pipeline = CurationPipeline()

    async def asyncTearDown(self) -> None:
        await self.db.close()

    async def test_a_seen_card_is_not_shown_again_when_the_rest_fills_a_screen(self) -> None:
        batch = feed(FRESH_ENOUGH + 2)
        await self.db.add_user_views(self.user, ["0", "1"])

        seen = await self.db.get_user_seen_ids(self.user)
        kept = self.pipeline.process(batch, user_id=self.user, seen_ids=seen)

        self.assertEqual(seen, {"0", "1"})
        self.assertEqual(
            [item["product_id"] for item in kept],
            [str(index) for index in range(2, FRESH_ENOUGH + 2)],
        )

    async def test_the_seen_cards_come_back_when_little_is_new(self) -> None:
        batch = feed(FRESH_ENOUGH + 2)
        await self.db.add_user_views(self.user, [str(index) for index in range(6)])

        seen = await self.db.get_user_seen_ids(self.user)
        kept = self.pipeline.process(batch, user_id=self.user, seen_ids=seen)

        # Nothing is lost: the fresh cards come first, the seen ones follow.
        self.assertEqual(len(kept), len(batch))
        self.assertEqual(
            [item["product_id"] for item in kept][:2], ["6", "7"]
        )
        self.assertEqual(self.pipeline.last_report.dropped("history"), 0)

    async def test_what_the_feed_showed_is_what_a_later_feed_skips(self) -> None:
        batch = feed(FRESH_ENOUGH + 2)
        shown = self.pipeline.process(batch)
        await self.db.add_user_views(
            self.user, [item["product_id"] for item in shown[:2]]
        )

        seen = await self.db.get_user_seen_ids(self.user)
        later = self.pipeline.process(batch, user_id=self.user, seen_ids=seen)

        self.assertEqual(
            [item["product_id"] for item in later],
            [item["product_id"] for item in shown[2:]],
        )

    async def test_an_unknown_user_simply_sees_everything(self) -> None:
        batch = feed(FRESH_ENOUGH + 2)
        stranger = await self.db.get_or_create_user(777, "other", "Другой")

        seen = await self.db.get_user_seen_ids(stranger)
        kept = self.pipeline.process(batch, user_id=stranger, seen_ids=seen)

        self.assertEqual(seen, set())
        self.assertEqual(kept, batch)


if __name__ == "__main__":
    unittest.main()
