"""Async SQLite database access layer built on aiosqlite.

A single ``Database`` instance is created at startup (see
``main.py``) and passed to handlers/services that need persistence.
Keeping all SQL in this one module (plus the schema in
``database.models``) makes it easy to reason about the on-disk
representation without spreading raw queries across handlers.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite

from database.models import (
    ALL_INDEXES,
    ALL_TABLES,
    CREATE_POPULAR_SHOWN_TABLE,
    FEED_HISTORY_LIMIT,
    FLOW_FEED,
    FLOW_SEARCH,
)

logger = logging.getLogger(__name__)


#: The path that asks for a real IN-MEMORY database. A connection-per-call
#: layer cannot serve it directly: SQLite hands every ``:memory:`` connection
#: its OWN empty database, so a schema written by one call would be invisible
#: to the next. ``":memory:"`` is therefore translated into a named shared-cache
#: memory database kept alive for the lifetime of the instance (see
#: :meth:`Database._connect` and :meth:`Database.close`).
MEMORY_DB_PATH = ":memory:"

#: URI template of one in-memory database. The name is unique per instance ON
#: PURPOSE: two ``Database(":memory:")`` objects in one process (two tests, for
#: example) must never end up sharing rows.
MEMORY_DB_URI = "file:memorydb_{name}?mode=memory&cache=shared"


class Database:
    """Thin async wrapper around a single SQLite database file."""

    def __init__(self, db_path: str | Path) -> None:
        path = str(db_path)
        #: Whether this instance serves a real in-memory database.
        self.is_memory = path == MEMORY_DB_PATH
        self._memory_uri = (
            MEMORY_DB_URI.format(name=uuid4().hex) if self.is_memory else ""
        )
        #: The connection target: a file path, or this instance's own in-memory
        #: URI (kept in the same attribute so logs and callers see one thing).
        self.db_path: Path = Path(self._memory_uri) if self.is_memory else Path(path)

        #: An in-memory database is destroyed when its LAST connection closes,
        #: so ONE connection is held for the lifetime of this instance. It is a
        #: plain, threading-free ``sqlite3`` connection that is never used for a
        #: query: it only keeps the memory database alive, which is what lets
        #: every method keep opening and closing its own aiosqlite connection
        #: exactly like the file-backed case does. ``close()`` releases it, and
        #: a forgotten ``close()`` cannot hang the process, because no thread is
        #: involved.
        self._memory_keeper: sqlite3.Connection | None = (
            sqlite3.connect(self._memory_uri, uri=True) if self.is_memory else None
        )

    @asynccontextmanager
    async def _connect(self) -> AsyncIterator[aiosqlite.Connection]:
        """Open one connection to this database, closing it on exit.

        A file-backed database gets a NEW connection per call (unchanged
        behaviour): the layer stays stateless and every method is one short
        transaction.

        The in-memory case needs ``uri=True`` to reach the shared-cache memory
        database named in ``__init__``; the plain ``":memory:"`` name would
        silently create a private, empty database on every call.
        """

        async with aiosqlite.connect(self.db_path, uri=self.is_memory) as conn:
            yield conn

    async def close(self) -> None:
        """Release this instance's in-memory database, if it holds one.

        A no-op for a file-backed database (it owns no long-lived resource).
        Callers that created ``Database(":memory:")`` call this when done - the
        tests do it in ``asyncTearDown`` - and forgetting it only delays the
        memory being freed.
        """

        keeper = self._memory_keeper
        if keeper is not None:
            self._memory_keeper = None
            keeper.close()

    async def init_db(self) -> None:
        """Create the database file/directory and all tables if missing."""

        await self.initialize()

    async def initialize(self) -> None:
        """Create the database file/directory and all tables if missing.

        Idempotent: every statement uses ``CREATE TABLE IF NOT EXISTS``,
        so calling this on every startup is safe.
        """

        if not self.is_memory:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with self._connect() as conn:

            await conn.execute("PRAGMA foreign_keys = ON;")
            for statement in ALL_TABLES:
                await conn.execute(statement)
            await self._ensure_columns(conn)
            for statement in ALL_INDEXES:
                await conn.execute(statement)
            await conn.commit()
        logger.info("Database initialized at %s", self.db_path)

    @staticmethod
    async def _ensure_columns(conn: aiosqlite.Connection) -> None:
        """Add columns introduced after the first release of a table.

        ``CREATE TABLE IF NOT EXISTS`` does not alter an existing table,
        so a database created by an older build would miss newer
        columns (``users.updated_at``, ``users.country``,
        ``users.currency``, ``popular_shown.flow``).

        TODO: replace this with a real migration mechanism (e.g.
        numbered SQL migrations or Alembic) once the schema changes
        more often than the database is recreated.
        """

        cursor = await conn.execute("PRAGMA table_info(users);")
        existing = {row[1] for row in await cursor.fetchall()}

        if "updated_at" not in existing:
            logger.info("Migrating: adding users.updated_at column")
            await conn.execute(
                "ALTER TABLE users ADD COLUMN updated_at TEXT "
                "NOT NULL DEFAULT (datetime('now'));"
            )

        # Country/currency are optional user preferences; a NULL value
        # means "not specified", which is a fully supported state.
        for column in ("country", "currency"):
            if column not in existing:
                logger.info("Migrating: adding users.%s column", column)
                await conn.execute(f"ALTER TABLE users ADD COLUMN {column} TEXT;")

        # Onboarding flag and the price-notification preference (both have
        # honest defaults: not onboarded yet / notifications enabled).
        if "onboarded" not in existing:
            logger.info("Migrating: adding users.onboarded column")
            await conn.execute(
                "ALTER TABLE users ADD COLUMN onboarded INTEGER NOT NULL DEFAULT 0;"
            )
        if "notify_price_changes" not in existing:
            logger.info("Migrating: adding users.notify_price_changes column")
            await conn.execute(
                "ALTER TABLE users ADD COLUMN notify_price_changes "
                "INTEGER NOT NULL DEFAULT 1;"
            )

        await Database._ensure_shown_flow_column(conn)
        await Database._migrate_legacy_feed_scope(conn)

    @staticmethod
    async def _ensure_shown_flow_column(conn: aiosqlite.Connection) -> None:
        """Give an existing ``popular_shown`` table the ``flow`` column.

        The column cannot be added with ``ALTER TABLE``: the scope is part of
        the row's UNIQUE key (one product may be recorded once per flow), and
        SQLite cannot alter a table constraint. The table is therefore
        rebuilt once - rows written by an older build carry no scope, and
        they are kept as ROTATION rows (:data:`~database.models.FLOW_POPULAR`):
        that scope is the conservative one (it can only make the popular feed
        skip a product, never hide a search result), and it starts the search
        scope from a clean, honest slate instead of inheriting state written
        for a different screen.
        """

        cursor = await conn.execute("PRAGMA table_info(popular_shown);")
        columns = {row[1] for row in await cursor.fetchall()}
        if not columns or "flow" in columns:
            return

        logger.info("Migrating: rebuilding popular_shown with a flow column")
        await conn.execute("ALTER TABLE popular_shown RENAME TO popular_shown_legacy;")
        await conn.execute("DROP INDEX IF EXISTS idx_popular_shown_user;")
        await conn.execute(CREATE_POPULAR_SHOWN_TABLE)
        await conn.execute(
            "INSERT OR IGNORE INTO popular_shown "
            "(user_id, marketplace, product_id, flow, shown_at) "
            f"SELECT user_id, marketplace, product_id, '{FLOW_FEED}', shown_at "
            "FROM popular_shown_legacy;"
        )
        await conn.execute("DROP TABLE popular_shown_legacy;")

    @staticmethod
    async def _migrate_legacy_feed_scope(conn: aiosqlite.Connection) -> None:
        """Move the removed «🔥 Популярное» scope onto the feed scope.

        The G24 feed replaced the old rotation in place: its table
        (``popular_shown``) and its rows stay, only the scope VALUE changed
        ``"popular"`` -> ``"feed"``. Rewriting the rows in ``initialize``
        keeps every user's shown-history instead of letting the feed replay
        cards its owner already displayed. Idempotent (a second startup
        simply matches no rows).
        """

        cursor = await conn.execute(
            "UPDATE popular_shown SET flow = ? WHERE flow = ?",
            (FLOW_FEED, "popular"),
        )
        if cursor.rowcount:
            logger.info(
                "Migrating: %d popular-scope rows moved to the feed scope",
                cursor.rowcount,
            )


    async def list_tables(self) -> list[str]:
        """Return the names of all user tables in the database.

        Used by tests and diagnostics to verify the schema was created.
        """

        async with self._connect() as conn:
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
            rows = await cursor.fetchall()
            return [row[0] for row in rows]

    # -- users ---------------------------------------------------------

    async def get_or_create_user(
        self,
        telegram_id: int,
        username: str | None = None,
        first_name: str | None = None,
    ) -> int:
        """Return the internal user id, creating the user row if needed.

        Existing users have their profile refreshed: Telegram usernames
        and display names change, and the bot should keep showing the
        current one.
        """

        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA foreign_keys = ON;")
            cursor = await conn.execute(
                "SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)
            )
            row = await cursor.fetchone()

            if row is not None:
                await conn.execute(
                    "UPDATE users SET username = COALESCE(?, username), "
                    "first_name = COALESCE(?, first_name), "
                    "updated_at = datetime('now') WHERE telegram_id = ?",
                    (username, first_name, telegram_id),
                )
                await conn.commit()
                return int(row["id"])

            cursor = await conn.execute(
                "INSERT INTO users (telegram_id, username, first_name) VALUES (?, ?, ?)",
                (telegram_id, username, first_name),
            )
            await conn.commit()
            assert cursor.lastrowid is not None
            return int(cursor.lastrowid)

    async def get_user(self, telegram_id: int) -> dict[str, Any] | None:
        """Fetch one user by Telegram id, or ``None`` if not registered."""

        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
            )
            row = await cursor.fetchone()
            return dict(row) if row is not None else None

    async def get_user_region(self, user_id: int) -> dict[str, str | None]:
        """Return the user's optional delivery country / currency.

        Both values are ``None`` until the user picks them, and the rest
        of the bot must work exactly the same in that state.
        """

        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT country, currency FROM users WHERE id = ?", (user_id,)
            )
            row = await cursor.fetchone()

        if row is None:
            return {"country": None, "currency": None}
        return {"country": row["country"], "currency": row["currency"]}

    async def update_user_region(
        self,
        user_id: int,
        country: str | None = None,
        currency: str | None = None,
        clear_country: bool = False,
        clear_currency: bool = False,
    ) -> None:
        """Update the optional country/currency preferences.

        ``clear_*`` exists because ``None`` normally means "leave this
        field alone" - the user must still be able to go back to the
        "not specified" state.
        """

        assignments: list[str] = []
        params: list[Any] = []

        if clear_country:
            assignments.append("country = NULL")
        elif country is not None:
            assignments.append("country = ?")
            params.append(country)

        if clear_currency:
            assignments.append("currency = NULL")
        elif currency is not None:
            assignments.append("currency = ?")
            params.append(currency)

        if not assignments:
            return

        assignments.append("updated_at = datetime('now')")
        params.append(user_id)

        async with self._connect() as conn:
            await conn.execute(
                f"UPDATE users SET {', '.join(assignments)} WHERE id = ?", params
            )
            await conn.commit()

    # -- search history --------------------------------------------------

    async def add_search_history(
        self, user_id: int, original_query: str, search_request_json: dict[str, Any]
    ) -> None:
        async with self._connect() as conn:
            await conn.execute("PRAGMA foreign_keys = ON;")
            await conn.execute(
                "INSERT INTO search_history (user_id, original_query, search_request_json) "
                "VALUES (?, ?, ?)",
                (user_id, original_query, json.dumps(search_request_json, ensure_ascii=False)),
            )
            await conn.commit()

    async def get_search_history(self, user_id: int, limit: int = 10) -> list[dict[str, Any]]:
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT original_query, search_request_json, created_at "
                "FROM search_history WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                (user_id, limit),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    # -- favorites -------------------------------------------------------

    async def add_favorite(
        self, user_id: int, marketplace: str, product_id: str, product_json: dict[str, Any]
    ) -> None:
        """Save a product to favorites (idempotent per user+product).

        ``ON CONFLICT DO UPDATE`` refreshes the stored snapshot while
        keeping the row id stable, so the same product can never appear
        twice in one user's favorites.
        """

        async with self._connect() as conn:
            await conn.execute("PRAGMA foreign_keys = ON;")
            await conn.execute(
                "INSERT INTO favorites "
                "(user_id, marketplace, product_id, product_json) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (user_id, marketplace, product_id) DO UPDATE SET "
                "product_json = excluded.product_json",
                (user_id, marketplace, product_id, json.dumps(product_json, ensure_ascii=False)),
            )
            await conn.commit()

    async def remove_favorite(self, user_id: int, marketplace: str, product_id: str) -> None:
        async with self._connect() as conn:
            await conn.execute(
                "DELETE FROM favorites WHERE user_id = ? AND marketplace = ? AND product_id = ?",
                (user_id, marketplace, product_id),
            )
            await conn.commit()

    async def get_favorites(self, user_id: int) -> list[dict[str, Any]]:
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT marketplace, product_id, product_json, created_at "
                "FROM favorites WHERE user_id = ? ORDER BY id DESC",
                (user_id,),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    # -- per-user shown history -------------------------------------------

    async def add_shown_product(
        self,
        user_id: int,
        marketplace: str,
        product_id: str,
        *,
        flow: str = FLOW_SEARCH,
    ) -> None:
        """Remember one product shown to this user, in ``flow``'s own scope.

        ``popular_shown`` is the per-user history of products already
        displayed; ``flow`` decides WHICH state a row belongs to (see
        ``database.models.FLOW_SEARCH``): the novelty scope of the search-like
        flows, or the «✨ Находки» feed. One product may therefore be
        recorded once per scope - a card shown by a search no longer decides
        what the feed considers new, and vice versa.

        Idempotent (``INSERT OR IGNORE``): re-showing a card inside one
        browsing session never duplicates the history row, and the row
        survives restarts because it is plain SQLite state - the feed
        must work after a new process, not only in RAM.

        The FEED scope is BOUNDED (:data:`FEED_HISTORY_LIMIT` newest rows
        per user): the no-repeat set only answers "has this user met this
        product?", and an ever-growing table would buy nothing but slower
        reads (spec §10 retention). The search scope keeps every row.
        """

        async with self._connect() as conn:
            await conn.execute("PRAGMA foreign_keys = ON;")
            await conn.execute(
                "INSERT OR IGNORE INTO popular_shown "
                "(user_id, marketplace, product_id, flow) VALUES (?, ?, ?, ?)",
                (user_id, marketplace, product_id, flow),
            )
            if flow == FLOW_FEED:
                # Retention: keep only the NEWEST rows of this user's feed
                # history. One statement, same transaction.
                await conn.execute(
                    "DELETE FROM popular_shown "
                    "WHERE user_id = ? AND flow = ? AND id NOT IN ("
                    "SELECT id FROM popular_shown "
                    "WHERE user_id = ? AND flow = ? "
                    "ORDER BY id DESC LIMIT ?)",
                    (user_id, flow, user_id, flow, FEED_HISTORY_LIMIT),
                )
            await conn.commit()

    async def get_shown_products(
        self, user_id: int, *, flow: str | None = None
    ) -> list[tuple[str, str]]:
        """``(marketplace, product_id)`` pairs already shown to this user.

        ``flow`` restricts the answer to ONE scope (the search novelty or the
        «✨ Находки» feed); ``None`` returns every scope, which is the
        "everything this user has seen" view.
        """

        query = (
            "SELECT marketplace, product_id FROM popular_shown WHERE user_id = ?"
        )
        params: tuple = (user_id,)
        if flow is not None:
            query += " AND flow = ?"
            params = (user_id, flow)
        query += " ORDER BY id"

        async with self._connect() as conn:
            cursor = await conn.execute(query, params)
            rows = await cursor.fetchall()
            return [(str(row[0]), str(row[1])) for row in rows]

    async def clear_shown_products(self, user_id: int, *, flow: str | None = None) -> None:
        """Forget this user's history - one scope, or all of it.

        The explicit «начать заново» of the «✨ Находки» feed passes
        :data:`~database.models.FLOW_FEED`, so a restart of the feed can
        never wipe the novelty state of the ordinary flows (and vice
        versa).
        """

        query = "DELETE FROM popular_shown WHERE user_id = ?"
        params: tuple = (user_id,)
        if flow is not None:
            query += " AND flow = ?"
            params = (user_id, flow)

        async with self._connect() as conn:
            await conn.execute(query, params)
            await conn.commit()

    #: The «✨ Находки» feed keeps its own scope of the SAME table: these
    #: helpers are the feed's view of the history, never the whole of it.
    async def add_feed_shown(
        self, user_id: int, marketplace: str, product_id: str
    ) -> None:
        """Record one product as seen by the «✨ Находки» feed."""

        await self.add_shown_product(
            user_id, marketplace, product_id, flow=FLOW_FEED
        )

    async def get_feed_shown(self, user_id: int) -> list[tuple[str, str]]:
        """The products the «✨ Находки» feed has already shown to this user."""

        return await self.get_shown_products(user_id, flow=FLOW_FEED)

    async def clear_feed_shown(self, user_id: int) -> None:
        """Reset ONLY the «✨ Находки» feed: the explicit «начать заново».

        Ordinary search novelty, favorites, tracking and preferences are
        untouched - the feed is a discovery session, not a data store. The
        feed CURSOR is deliberately kept (:meth:`get_feed_page`), so the
        restarted feed continues from a NEWER part of the listing instead of
        replaying the same first pages.
        """

        await self.clear_shown_products(user_id, flow=FLOW_FEED)

    # -- persistent runtime configuration (the sales section's switches) -----

    async def get_config(self, key: str) -> str | None:
        """Read one ``bot_config`` value, or ``None`` when it is not set.

        A missing row is NOT an error: it means "not configured", and every
        caller treats that as the safe default (a sales event stays disabled,
        a destination URL stays unset). This is what makes a fresh or wiped
        database fail safe instead of exposing a hidden section.
        """

        async with self._connect() as conn:
            cursor = await conn.execute(
                "SELECT value FROM bot_config WHERE key = ?", (key,)
            )
            row = await cursor.fetchone()
        return str(row[0]) if row is not None and row[0] is not None else None

    async def set_config(self, key: str, value: str) -> None:
        """Persist one ``bot_config`` value as an idempotent upsert.

        Used by the command-line ``manage_sales.py`` to enable/disable a sale
        or set its destination URL. The value is stored verbatim as text; the
        interpretation (enabled/disabled, valid URL) lives in ``config.sales``
        so the database never has to understand what a key means.
        """

        async with self._connect() as conn:
            await conn.execute(
                "INSERT INTO bot_config (key, value, updated_at) "
                "VALUES (?, ?, datetime('now')) "
                "ON CONFLICT (key) DO UPDATE SET "
                "value = excluded.value, updated_at = excluded.updated_at",
                (key, str(value)),
            )
            await conn.commit()

    # -- «✨ Находки» feed cursor (the next page_no to ask) ----------------

    async def get_feed_page(self, user_id: int) -> int:
        """The AliExpress ``page_no`` the NEXT «✨ Находки» open starts from.

        Persisted on purpose (unlike any RAM state): the feed must keep
        moving forward after a bot restart too, instead of asking the same
        first pages again. ``1`` is the documented start for a user without
        a cursor. The column keeps its historical name ``step`` (see
        ``database.models``) - only the meaning changed with G24.
        """

        async with self._connect() as conn:
            cursor = await conn.execute(
                "SELECT step FROM popular_state WHERE user_id = ?", (user_id,)
            )
            row = await cursor.fetchone()
        return max(1, int(row[0])) if row and row[0] is not None else 1

    async def set_feed_page(self, user_id: int, page: int) -> None:
        """Remember which feed page the next open continues from."""

        async with self._connect() as conn:
            await conn.execute("PRAGMA foreign_keys = ON;")
            await conn.execute(
                "INSERT INTO popular_state (user_id, step, updated_at) "
                "VALUES (?, ?, datetime('now')) "
                "ON CONFLICT (user_id) DO UPDATE SET "
                "step = excluded.step, updated_at = excluded.updated_at",
                (user_id, max(1, int(page))),
            )
            await conn.commit()

    # -- per-user view history (the curation layer's novelty signal) --------

    async def add_user_views(self, user_id: int, product_ids: Iterable[str]) -> None:
        """Remember the products this user has just been shown.

        ``user_view_history`` answers exactly ONE question - "has this user met
        this product?" - so a row stores nothing but that pair, and re-showing
        a card is idempotent (``INSERT OR IGNORE`` on the unique pair). That is
        what makes the history safe to write on every feed open: a retry, a
        second open or a batch that lists the same item twice cannot duplicate
        it, and the skip list of a later feed stays honest.

        Ids are stored VERBATIM and a blank one is skipped rather than recorded
        as "nothing": the caller passes the listing ids it has actually
        displayed. Duplicates inside one batch collapse before the write.
        """

        unique_ids: dict[str, None] = {}
        for item in product_ids or ():
            product_id = "" if item is None else str(item).strip()
            if product_id:
                unique_ids.setdefault(product_id, None)
        if not unique_ids:
            return

        async with self._connect() as conn:
            await conn.execute("PRAGMA foreign_keys = ON;")
            await conn.executemany(
                "INSERT OR IGNORE INTO user_view_history (user_id, product_id) "
                "VALUES (?, ?)",
                [(user_id, product_id) for product_id in unique_ids],
            )
            await conn.commit()

    async def get_user_seen_ids(self, user_id: int, limit: int = 200) -> set[str]:
        """The product ids this user has already been shown, newest first.

        ``limit`` caps the answer at a browsing-relevant window (the bot never
        needs a lifetime of history to decide what is new), which is why the
        index is on ``(user_id, id DESC)``. ``limit <= 0`` answers "nothing has
        been seen", the honest reading of a zero-sized window.
        """

        if limit <= 0:
            return set()

        async with self._connect() as conn:
            cursor = await conn.execute(
                "SELECT product_id FROM user_view_history "
                "WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                (user_id, int(limit)),
            )
            rows = await cursor.fetchall()
        return {str(row[0]) for row in rows}

    # -- price tracking ---------------------------------------------------

    async def add_tracked_product(
        self,
        user_id: int,
        marketplace: str,
        product_id: str,
        title: str | None,
        product_url: str,
        initial_price: float | None,
        currency: str | None,
    ) -> None:
        """Start tracking a product's price (idempotent per user+product).

        Repeated clicks update the existing row instead of replacing it,
        so the row id stays stable for the future price-check scheduler.
        """

        async with self._connect() as conn:
            await conn.execute("PRAGMA foreign_keys = ON;")
            await conn.execute(
                "INSERT INTO tracked_products "
                "(user_id, marketplace, product_id, title, product_url, initial_price, "
                "last_checked_price, currency, last_checked_at, is_active) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), 1) "
                "ON CONFLICT (user_id, marketplace, product_id) DO UPDATE SET "
                "title = excluded.title, "
                "product_url = excluded.product_url, "
                "initial_price = COALESCE(tracked_products.initial_price, "
                "excluded.initial_price), "
                "last_checked_price = excluded.last_checked_price, "
                "currency = COALESCE(excluded.currency, tracked_products.currency), "
                "last_checked_at = datetime('now'), "
                "is_active = 1",
                (
                    user_id,
                    marketplace,
                    product_id,
                    title,
                    product_url,
                    initial_price,
                    initial_price,
                    currency,
                ),
            )
            await conn.commit()

    async def get_tracked_products(
        self, user_id: int | None = None, active_only: bool = True
    ) -> list[dict[str, Any]]:
        """Fetch tracked products, optionally scoped to one user.

        With ``user_id=None`` this returns all active tracked products
        across all users - the shape a future price-check scheduler
        would need.
        """

        query = "SELECT * FROM tracked_products WHERE 1=1"
        params: list[Any] = []
        if user_id is not None:
            query += " AND user_id = ?"
            params.append(user_id)
        if active_only:
            query += " AND is_active = 1"

        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(query, params)
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_active_tracked_with_telegram(self) -> list[dict[str, Any]]:
        """All active tracked products joined with their users' telegram ids.

        This is the exact shape the price-tracking scheduler needs: one
        row per tracked product plus the ``telegram_id`` a notification
        must be delivered to.
        """

        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            # The join also carries the user's persisted currency (the
            # scheduler must request prices in it) and the notification
            # preference (the scheduler must honour it).
            cursor = await conn.execute(
                "SELECT t.id, t.user_id, t.marketplace, t.product_id, t.title, "
                "t.product_url, t.initial_price, t.last_checked_price, t.currency, "
                "t.last_checked_at, u.telegram_id, "
                "u.currency AS user_currency, "
                "u.notify_price_changes AS notify_price_changes "
                "FROM tracked_products t JOIN users u ON u.id = t.user_id "
                "WHERE t.is_active = 1 ORDER BY t.id",
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def update_tracked_observation(
        self,
        tracked_product_id: int,
        new_price: float | None,
        currency: str | None,
    ) -> None:
        """Persist one scheduler observation of a tracked product.

        The fetched currency is authoritative when the API returned one;
        a ``None`` currency keeps the stored value (the API sometimes
        answers without a currency while the user's preference did not
        change).
        """

        async with self._connect() as conn:
            await conn.execute(
                "UPDATE tracked_products SET last_checked_price = ?, "
                "currency = COALESCE(?, currency), "
                "last_checked_at = datetime('now') WHERE id = ?",
                (new_price, currency, tracked_product_id),
            )
            await conn.commit()

    async def update_tracked_price(
        self, tracked_product_id: int, new_price: float | None
    ) -> None:
        async with self._connect() as conn:
            await conn.execute(
                "UPDATE tracked_products SET last_checked_price = ?, "
                "last_checked_at = datetime('now') WHERE id = ?",
                (new_price, tracked_product_id),
            )
            await conn.commit()

    async def invalidate_tracking_baselines(self, user_id: int) -> None:
        """Drop stored price baselines after a manual currency change.

        The old baseline lives in the previous currency and must never be
        compared with prices in the new one: the first new observation
        silently becomes the new baseline. History rows are untouched -
        each one carries its own currency.
        """

        async with self._connect() as conn:
            await conn.execute(
                "UPDATE tracked_products SET last_checked_price = NULL "
                "WHERE user_id = ?",
                (user_id,),
            )
            await conn.commit()

    async def deactivate_tracked_product(
        self, tracked_product_id: int, user_id: int | None = None
    ) -> None:
        """Stop tracking a product.

        When ``user_id`` is given, the update is scoped to that user so
        one user cannot deactivate another user's tracking entry.
        """

        query = "UPDATE tracked_products SET is_active = 0 WHERE id = ?"
        params: list[Any] = [tracked_product_id]
        if user_id is not None:
            query += " AND user_id = ?"
            params.append(user_id)

        async with self._connect() as conn:
            await conn.execute(query, params)
            await conn.commit()

    async def record_price_history(
        self,
        tracked_product_id: int,
        price: float | None,
        shipping_cost: float | None = None,
        currency: str | None = None,
    ) -> None:
        """Append one observed price for a tracked product.

        This table is what a future price-drop notifier reads from; it
        is written by hand (or by the future scheduler) rather than
        derived, so history is never lost by an update.
        """

        async with self._connect() as conn:
            await conn.execute("PRAGMA foreign_keys = ON;")
            await conn.execute(
                "INSERT INTO price_history "
                "(tracked_product_id, price, shipping_cost, currency) "
                "VALUES (?, ?, ?, ?)",
                (tracked_product_id, price, shipping_cost, currency),
            )
            await conn.commit()

    async def get_price_history(
        self, tracked_product_id: int, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Return the most recent price observations (newest first)."""

        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT price, shipping_cost, currency, checked_at "
                "FROM price_history WHERE tracked_product_id = ? "
                "ORDER BY id DESC LIMIT ?",
                (tracked_product_id, limit),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    # -- saved products ---------------------------------------------------

    async def add_saved_product(
        self,
        user_id: int,
        marketplace: str,
        product_id: str,
        product_json: dict[str, Any],
        note: str | None = None,
    ) -> None:
        """Store/refresh a product snapshot the user asked to keep."""

        async with self._connect() as conn:
            await conn.execute("PRAGMA foreign_keys = ON;")
            await conn.execute(
                "INSERT INTO saved_products "
                "(user_id, marketplace, product_id, product_json, note) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (user_id, marketplace, product_id) DO UPDATE SET "
                "product_json = excluded.product_json, "
                "note = COALESCE(excluded.note, saved_products.note), "
                "updated_at = datetime('now')",
                (
                    user_id,
                    marketplace,
                    product_id,
                    json.dumps(product_json, ensure_ascii=False),
                    note,
                ),
            )
            await conn.commit()

    async def get_saved_products(self, user_id: int) -> list[dict[str, Any]]:
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT marketplace, product_id, product_json, note, created_at, updated_at "
                "FROM saved_products WHERE user_id = ? ORDER BY id DESC",
                (user_id,),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def remove_saved_product(
        self, user_id: int, marketplace: str, product_id: str
    ) -> None:
        async with self._connect() as conn:
            await conn.execute(
                "DELETE FROM saved_products "
                "WHERE user_id = ? AND marketplace = ? AND product_id = ?",
                (user_id, marketplace, product_id),
            )
            await conn.commit()

    # -- user preferences ------------------------------------------------

    async def get_notification_pref(self, user_id: int) -> bool:
        """Price-notification preference (default: enabled)."""

        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT notify_price_changes FROM users WHERE id = ?", (user_id,)
            )
            row = await cursor.fetchone()
        if row is None:
            return True
        return bool(row["notify_price_changes"])

    async def set_notification_pref(self, user_id: int, enabled: bool) -> None:
        async with self._connect() as conn:
            await conn.execute(
                "UPDATE users SET notify_price_changes = ?, "
                "updated_at = datetime('now') WHERE id = ?",
                (int(bool(enabled)), user_id),
            )
            await conn.commit()

    async def is_onboarded(self, user_id: int) -> bool:
        async with self._connect() as conn:
            cursor = await conn.execute(
                "SELECT onboarded FROM users WHERE id = ?", (user_id,)
            )
            row = await cursor.fetchone()
        return bool(row[0]) if row is not None else False

    async def mark_onboarded(self, user_id: int) -> None:
        async with self._connect() as conn:
            await conn.execute(
                "UPDATE users SET onboarded = 1, updated_at = datetime('now') "
                "WHERE id = ?",
                (user_id,),
            )
            await conn.commit()

    # -- admin aggregates (read-only; usernames deliberately excluded) ----

    async def list_users_page(
        self, page: int = 0, per_page: int = 8
    ) -> tuple[list[dict[str, Any]], int]:
        page = max(0, int(page))
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            total_row = await conn.execute("SELECT COUNT(*) FROM users")
            total = int((await total_row.fetchone())[0])
            cursor = await conn.execute(
                "SELECT id, currency, onboarded, created_at, updated_at "
                "FROM users ORDER BY id DESC LIMIT ? OFFSET ?",
                (per_page, page * per_page),
            )
            rows = [dict(r) for r in await cursor.fetchall()]
        return rows, total

    async def count_users_active(self, since: str | None = None) -> int:
        """Distinct users with at least one recorded event in the period."""

        query = (
            "SELECT COUNT(DISTINCT user_id) FROM analytics_events "
            "WHERE user_id IS NOT NULL"
        )
        params: list[Any] = []
        if since is not None:
            query += " AND created_at >= ?"
            params.append(since)
        async with self._connect() as conn:
            cursor = await conn.execute(query, params)
            row = await cursor.fetchone()
        return int(row[0]) if row is not None else 0

    async def count_onboarded_users(self) -> int:
        async with self._connect() as conn:
            cursor = await conn.execute(
                "SELECT COUNT(*) FROM users WHERE onboarded = 1"
            )
            row = await cursor.fetchone()
        return int(row[0]) if row is not None else 0

    async def currency_distribution(self) -> list[dict[str, Any]]:
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT COALESCE(currency, '(не указана)') AS currency, "
                "COUNT(*) AS total FROM users GROUP BY currency ORDER BY total DESC"
            )
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def count_favorites(self, since: str | None = None) -> int:
        query = "SELECT COUNT(*) FROM favorites"
        params: list[Any] = []
        if since is not None:
            query += " WHERE created_at >= ?"
            params.append(since)
        async with self._connect() as conn:
            cursor = await conn.execute(query, params)
            row = await cursor.fetchone()
        return int(row[0]) if row is not None else 0

    async def count_favorites_unique(self) -> int:
        async with self._connect() as conn:
            cursor = await conn.execute(
                "SELECT COUNT(DISTINCT product_id) FROM favorites"
            )
            row = await cursor.fetchone()
        return int(row[0]) if row is not None else 0

    async def favorite_marketplace_distribution(self) -> list[dict[str, Any]]:
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT marketplace, COUNT(*) AS total FROM favorites "
                "GROUP BY marketplace ORDER BY total DESC"
            )
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def top_favorites(self, limit: int = 5) -> list[dict[str, Any]]:
        """Most-favorited products, titles parsed from stored snapshots."""

        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT product_id, marketplace, COUNT(*) AS total, "
                "MAX(product_json) AS product_json "
                "FROM favorites GROUP BY product_id, marketplace "
                "ORDER BY total DESC, product_id ASC LIMIT ?",
                (max(1, int(limit)),),
            )
            raw = await cursor.fetchall()
        result: list[dict[str, Any]] = []
        for row in raw:
            title = None
            try:
                title = json.loads(row["product_json"]).get("title")
            except (ValueError, TypeError, AttributeError):
                title = None
            result.append(
                {
                    "product_id": row["product_id"],
                    "marketplace": row["marketplace"],
                    "total": int(row["total"]),
                    "title": title,
                }
            )
        return result

    async def count_search_history(self, since: str | None = None) -> int:
        query = "SELECT COUNT(*) FROM search_history"
        params: list[Any] = []
        if since is not None:
            query += " WHERE created_at >= ?"
            params.append(since)
        async with self._connect() as conn:
            cursor = await conn.execute(query, params)
            row = await cursor.fetchone()
        return int(row[0]) if row is not None else 0

    async def top_search_queries(
        self, since: str | None = None, limit: int = 5
    ) -> list[dict[str, Any]]:
        """Most frequent queries (admin screen only, spec §42)."""

        query = (
            "SELECT original_query AS query, COUNT(*) AS total "
            "FROM search_history"
        )
        params: list[Any] = []
        if since is not None:
            query += " WHERE created_at >= ?"
            params.append(since)
        query += " GROUP BY original_query ORDER BY total DESC, query ASC LIMIT ?"
        params.append(max(1, int(limit)))
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(query, params)
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def search_mode_distribution(
        self, since: str | None = None, sample_limit: int = 2000
    ) -> dict[str, int]:
        """Parsed ``mode`` values over recent requests (sampled)."""

        query = "SELECT search_request_json FROM search_history"
        params: list[Any] = []
        if since is not None:
            query += " WHERE created_at >= ?"
            params.append(since)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(sample_limit)
        modes: dict[str, int] = {}
        async with self._connect() as conn:
            cursor = await conn.execute(query, params)
            for (payload,) in await cursor.fetchall():
                try:
                    mode = str(json.loads(payload).get("mode") or "unknown")
                except (ValueError, TypeError, AttributeError):
                    mode = "unknown"
                modes[mode] = modes.get(mode, 0) + 1
        return modes

    async def count_tracked_active(self) -> int:
        async with self._connect() as conn:
            cursor = await conn.execute(
                "SELECT COUNT(*) FROM tracked_products WHERE is_active = 1"
            )
            row = await cursor.fetchone()
        return int(row[0]) if row is not None else 0

    async def table_row_counts(self) -> dict[str, int]:
        """Row count per user table (admin DB screen)."""

        counts: dict[str, int] = {}
        tables = await self.list_tables()
        async with self._connect() as conn:
            for table in tables:
                cursor = await conn.execute(f"SELECT COUNT(*) FROM {table}")
                row = await cursor.fetchone()
                counts[table] = int(row[0]) if row is not None else 0
        return counts

    async def integrity_check(self) -> str:
        """``PRAGMA quick_check`` - a read-only health probe."""

        async with self._connect() as conn:
            cursor = await conn.execute("PRAGMA quick_check(1)")
            row = await cursor.fetchone()
        return str(row[0]) if row is not None else "unknown"

    async def product_diagnostics(self) -> dict[str, Any]:
        """Aggregate stored product snapshots (admin screen §45).

        Scans ``favorites`` + ``saved_products`` snapshots (capped so a
        huge table cannot stall the screen). Marketplace values of the
        REMOVED marketplaces are never returned by name - they are
        counted into ``legacy_hidden`` only, so a removed marketplace
        can never appear in an operational list (spec §43/§45).
        """

        stats: dict[str, Any] = {
            "rows": 0,
            "unique_ids": set(),
            "marketplaces": {"aliexpress": 0},
            "legacy_hidden": 0,
            "missing_product_url": 0,
            "missing_affiliate_url": 0,
            "malformed": 0,
        }
        scan_cap = 5000

        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            for table in ("favorites", "saved_products"):
                cursor = await conn.execute(
                    f"SELECT marketplace, product_id, product_json FROM {table} "  # noqa: S608 - table name is a literal from this file
                    f"ORDER BY id DESC LIMIT {scan_cap}"
                )
                for row in await cursor.fetchall():
                    stats["rows"] += 1
                    marketplace = str(row["marketplace"] or "").lower()
                    stats["unique_ids"].add(
                        (marketplace, str(row["product_id"]))
                    )
                    if marketplace == "aliexpress":
                        stats["marketplaces"]["aliexpress"] += 1
                    else:
                        stats["legacy_hidden"] += 1
                    try:
                        payload = json.loads(row["product_json"])
                    except (ValueError, TypeError):
                        stats["malformed"] += 1
                        continue
                    if not isinstance(payload, dict):
                        stats["malformed"] += 1
                        continue
                    if not payload.get("product_url"):
                        stats["missing_product_url"] += 1
                    if not payload.get("affiliate_url"):
                        stats["missing_affiliate_url"] += 1

        stats["unique"] = len(stats.pop("unique_ids"))
        return stats

    # -- analytics ---------------------------------------------------------

    #: Longest value kept for one metadata entry. Analytics must not turn
    #: into a second copy of user data: only short, scalar values are
    #: stored (a count, a currency code, a source name).
    METADATA_VALUE_LIMIT = 120

    async def record_event(
        self,
        event_type: str,
        *,
        user_id: int | None = None,
        telegram_id: int | None = None,
        marketplace: str | None = None,
        product_id: str | None = None,
        title: str | None = None,
        promotion_name: str | None = None,
        is_affiliate: bool | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Append one analytics event.

        Deliberately a single INSERT with no read-modify-write: recording
        an event must stay cheap, because it happens on the user's hot
        path. Handlers call it through ``handlers.common.record_event``,
        which swallows failures so analytics can never break a screen.

        Only scalar metadata values are stored, shortened to
        ``METADATA_VALUE_LIMIT`` characters. Free-form user text is never
        passed here (see ``services/analytics.py`` for the privacy rules).
        """

        cleaned = {
            str(key): str(value)[: self.METADATA_VALUE_LIMIT]
            for key, value in (metadata or {}).items()
            if isinstance(value, (str, int, float, bool))
        }

        async with self._connect() as conn:
            await conn.execute("PRAGMA foreign_keys = ON;")
            await conn.execute(
                "INSERT INTO analytics_events "
                "(user_id, telegram_id, event_type, marketplace, product_id, "
                " title, promotion_name, is_affiliate, metadata_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    telegram_id,
                    event_type,
                    marketplace,
                    product_id,
                    title,
                    promotion_name,
                    None if is_affiliate is None else int(bool(is_affiliate)),
                    json.dumps(cleaned, ensure_ascii=False) if cleaned else None,
                ),
            )
            await conn.commit()

    async def get_event_counts(self, since: str | None = None) -> dict[str, int]:
        """Number of events per type, optionally since a timestamp."""

        query = "SELECT event_type, COUNT(*) AS total FROM analytics_events"
        params: list[Any] = []
        if since is not None:
            query += " WHERE created_at >= ?"
            params.append(since)
        query += " GROUP BY event_type"

        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(query, params)
            rows = await cursor.fetchall()
        return {row["event_type"]: int(row["total"]) for row in rows}

    async def get_event_user_counts(self, since: str | None = None) -> dict[str, int]:
        """Distinct users per event type (anonymous events excluded)."""

        query = (
            "SELECT event_type, COUNT(DISTINCT user_id) AS users "
            "FROM analytics_events WHERE user_id IS NOT NULL"
        )
        params: list[Any] = []
        if since is not None:
            query += " AND created_at >= ?"
            params.append(since)
        query += " GROUP BY event_type"

        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(query, params)
            rows = await cursor.fetchall()
        return {row["event_type"]: int(row["users"]) for row in rows}

    async def count_events(
        self, event_type: str | None = None, since: str | None = None
    ) -> int:
        """Total number of events, optionally of one type and/or period."""

        query = "SELECT COUNT(*) AS total FROM analytics_events WHERE 1=1"
        params: list[Any] = []
        if event_type is not None:
            query += " AND event_type = ?"
            params.append(event_type)
        if since is not None:
            query += " AND created_at >= ?"
            params.append(since)

        async with self._connect() as conn:
            cursor = await conn.execute(query, params)
            row = await cursor.fetchone()
        return int(row[0]) if row is not None else 0

    async def count_users(self, since: str | None = None) -> int:
        """Registered users in total, or only those created since a time."""

        query = "SELECT COUNT(*) AS total FROM users"
        params: list[Any] = []
        if since is not None:
            query += " WHERE created_at >= ?"
            params.append(since)

        async with self._connect() as conn:
            cursor = await conn.execute(query, params)
            row = await cursor.fetchone()
        return int(row[0]) if row is not None else 0

    async def get_top_products(
        self, event_type: str, since: str | None = None, limit: int = 5
    ) -> list[dict[str, Any]]:
        """Most-clicked products for one event type.

        Titles come from the marketplace API, so they may be shown in the
        admin panel; a row without a title is still counted, with its
        ``title`` left ``None`` instead of a placeholder.
        """

        query = (
            "SELECT product_id, marketplace, MAX(title) AS title, "
            "COUNT(*) AS total "
            "FROM analytics_events WHERE event_type = ? "
            "AND product_id IS NOT NULL"
        )
        params: list[Any] = [event_type]
        if since is not None:
            query += " AND created_at >= ?"
            params.append(since)
        query += (
            " GROUP BY product_id, marketplace "
            "ORDER BY total DESC, product_id ASC LIMIT ?"
        )
        params.append(max(1, int(limit)))

        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(query, params)
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_daily_event_counts(
        self, days: int = 7, event_type: str | None = None
    ) -> list[dict[str, Any]]:
        """Per-day event counts for the last ``days`` days (newest first)."""

        query = (
            "SELECT date(created_at) AS day, COUNT(*) AS total "
            "FROM analytics_events WHERE created_at >= datetime('now', ?)"
        )
        params: list[Any] = [f"-{max(1, int(days))} days"]
        if event_type is not None:
            query += " AND event_type = ?"
            params.append(event_type)
        query += " GROUP BY day ORDER BY day DESC"

        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(query, params)
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]
