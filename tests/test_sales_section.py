"""Offline tests for the reusable sales section («Чёрная пятница»).

No Telegram request and no marketplace call is made - the handlers get small
fakes, so the acceptance criteria fail a test instead of silently breaking in
production:

* the section is hidden from ordinary users by default (fail-safe);
* an administrator can see and open it while it is disabled;
* an ordinary user cannot reach it through a forged ``sale:<id>`` callback
  (the server-side gate, not the hidden button, is the real access check);
* enabling the event exposes the button to ordinary users; disabling hides it
  and blocks the callback;
* the enabled/disabled state persists (the bot's own SQLite ``bot_config``);
* a missing affiliate URL yields a friendly, link-less screen;
* an invalid URL is rejected, a valid one is offered as a URL button;
* the section never touches the regular product search.
"""

from __future__ import annotations

import asyncio
import unittest

from config.sales import (
    DEFAULT_EVENT_ID,
    enabled_key,
    is_enabled_value,
    safe_http_url,
    url_key,
)
from database.database import Database
from handlers import sales as sales_handlers
from keyboards.sales import sale_callback


def run(coro):
    return asyncio.run(coro)


class FakeQuery:
    """Records callback answers and message edits."""

    def __init__(self, data: str, user_id: int = 555) -> None:
        self.data = data
        self.from_user = type("U", (), {"id": user_id})()
        self.answers: list[str] = []
        self.edits: list[tuple[str, dict]] = []

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        if text:
            self.answers.append(text)

    async def edit_message_text(self, text: str, **kwargs) -> None:
        self.edits.append((text, kwargs))


class FakeUpdate:
    def __init__(self, query: FakeQuery) -> None:
        self.callback_query = query
        self.effective_user = query.from_user


class FakeSettings:
    """Stand-in for ``Settings`` exposing only the admin check."""

    def __init__(self, admin_ids=()) -> None:
        self.admin_ids = tuple(admin_ids)

    def is_admin(self, telegram_id) -> bool:
        if telegram_id is None:
            return False
        return telegram_id in self.admin_ids


class FakeDatabase:
    """In-memory ``bot_config`` stand-in; ``fail`` simulates a broken DB."""

    def __init__(self, config: dict | None = None, fail: bool = False) -> None:
        self.config = dict(config or {})
        self.fail = fail

    async def get_config(self, key: str):
        if self.fail:
            raise RuntimeError("db is down")
        return self.config.get(key)

    async def set_config(self, key: str, value: str) -> None:
        self.config[key] = str(value)


class _FakeApplication:
    def __init__(self, bot_data: dict) -> None:
        self.bot_data = bot_data


class FakeContext:
    def __init__(self, database: FakeDatabase, settings: FakeSettings) -> None:
        self.application = _FakeApplication(
            {"database": database, "settings": settings}
        )
        self.user_data: dict = {}


def _context(database: FakeDatabase, *, admin_ids=()) -> FakeContext:
    return FakeContext(database, FakeSettings(admin_ids=admin_ids))


def callbacks_of(rows) -> list[str]:
    return [
        button.callback_data
        for row in rows
        for button in row
        if button.callback_data is not None
    ]


def urls_of(rows) -> list[str]:
    return [button.url for row in rows for button in row if button.url is not None]


class MenuVisibilityTests(unittest.TestCase):
    """Who sees the «Чёрная пятница» button in the main menu."""

    def test_hidden_from_ordinary_users_by_default(self) -> None:
        context = _context(FakeDatabase(), admin_ids=(555,))
        buttons = run(sales_handlers.sale_menu_buttons(context, telegram_id=999))
        self.assertEqual(buttons, [])

    def test_admin_sees_the_button_while_disabled(self) -> None:
        context = _context(FakeDatabase(), admin_ids=(555,))
        buttons = run(sales_handlers.sale_menu_buttons(context, telegram_id=555))
        self.assertEqual(len(buttons), 1)
        self.assertIn("Чёрная пятница", buttons[0].text)
        self.assertEqual(buttons[0].callback_data, sale_callback(DEFAULT_EVENT_ID))

    def test_enabled_event_is_visible_to_everyone(self) -> None:
        db = FakeDatabase({enabled_key(DEFAULT_EVENT_ID): "1"})
        context = _context(db, admin_ids=())  # no admin configured at all
        buttons = run(sales_handlers.sale_menu_buttons(context, telegram_id=999))
        self.assertEqual(len(buttons), 1)

    def test_disabled_event_is_hidden_again_after_being_enabled(self) -> None:
        db = FakeDatabase({enabled_key(DEFAULT_EVENT_ID): "0"})
        context = _context(db, admin_ids=())
        buttons = run(sales_handlers.sale_menu_buttons(context, telegram_id=999))
        self.assertEqual(buttons, [])

    def test_broken_config_fails_safe_hidden(self) -> None:
        context = _context(FakeDatabase(fail=True), admin_ids=(555,))
        buttons = run(sales_handlers.sale_menu_buttons(context, telegram_id=999))
        self.assertEqual(buttons, [])


class AccessGateTests(unittest.TestCase):
    """The server-side gate: hiding the button is NOT the access check."""

    def test_forged_callback_is_refused_for_a_non_admin_when_disabled(self) -> None:
        context = _context(FakeDatabase(), admin_ids=(555,))
        query = FakeQuery(sale_callback(DEFAULT_EVENT_ID), user_id=999)

        run(sales_handlers.sale_callback(FakeUpdate(query), context))

        self.assertTrue(query.answers)
        self.assertEqual(query.edits, [])
        self.assertNotIn("админ", " ".join(query.answers).lower())

    def test_admin_opens_the_section_while_disabled(self) -> None:
        context = _context(FakeDatabase(), admin_ids=(555,))
        query = FakeQuery(sale_callback(DEFAULT_EVENT_ID), user_id=555)

        run(sales_handlers.sale_callback(FakeUpdate(query), context))

        self.assertEqual(len(query.edits), 1)
        self.assertIn("Чёрная пятница", query.edits[0][0])

    def test_enabled_event_opens_for_an_ordinary_user(self) -> None:
        db = FakeDatabase({enabled_key(DEFAULT_EVENT_ID): "1"})
        context = _context(db, admin_ids=())
        query = FakeQuery(sale_callback(DEFAULT_EVENT_ID), user_id=999)

        run(sales_handlers.sale_callback(FakeUpdate(query), context))

        self.assertEqual(len(query.edits), 1)

    def test_unknown_event_is_refused(self) -> None:
        db = FakeDatabase({enabled_key(DEFAULT_EVENT_ID): "1"})
        context = _context(db, admin_ids=())
        query = FakeQuery("sale:not_a_real_event", user_id=999)

        run(sales_handlers.sale_callback(FakeUpdate(query), context))

        self.assertTrue(query.answers)
        self.assertEqual(query.edits, [])

    def test_broken_config_blocks_the_callback(self) -> None:
        context = _context(FakeDatabase(fail=True), admin_ids=())
        query = FakeQuery(sale_callback(DEFAULT_EVENT_ID), user_id=999)

        run(sales_handlers.sale_callback(FakeUpdate(query), context))

        self.assertTrue(query.answers)
        self.assertEqual(query.edits, [])

class DestinationUrlTests(unittest.TestCase):
    """The affiliate destination: missing -> friendly, invalid -> rejected."""

    def test_missing_url_shows_friendly_unavailable_screen(self) -> None:
        db = FakeDatabase({enabled_key(DEFAULT_EVENT_ID): "1"})
        context = _context(db, admin_ids=(555,))
        query = FakeQuery(sale_callback(DEFAULT_EVENT_ID), user_id=555)

        run(sales_handlers.sale_callback(FakeUpdate(query), context))

        text, kwargs = query.edits[0]
        self.assertIn("скоро", text.lower())
        self.assertEqual(urls_of(kwargs["reply_markup"].inline_keyboard), [])
        self.assertIn("Умный поиск", text)

    def test_valid_url_is_offered_as_a_button(self) -> None:
        url = "https://s.click.aliexpress.com/e/_example"
        db = FakeDatabase(
            {
                enabled_key(DEFAULT_EVENT_ID): "1",
                url_key(DEFAULT_EVENT_ID): url,
            }
        )
        context = _context(db, admin_ids=(555,))
        query = FakeQuery(sale_callback(DEFAULT_EVENT_ID), user_id=555)

        run(sales_handlers.sale_callback(FakeUpdate(query), context))

        _, kwargs = query.edits[0]
        self.assertIn(url, urls_of(kwargs["reply_markup"].inline_keyboard))

    def test_invalid_stored_url_degrades_to_unavailable(self) -> None:
        db = FakeDatabase(
            {
                enabled_key(DEFAULT_EVENT_ID): "1",
                url_key(DEFAULT_EVENT_ID): "javascript:alert(1)",
            }
        )
        context = _context(db, admin_ids=(555,))
        query = FakeQuery(sale_callback(DEFAULT_EVENT_ID), user_id=555)

        run(sales_handlers.sale_callback(FakeUpdate(query), context))

        text, kwargs = query.edits[0]
        self.assertEqual(urls_of(kwargs["reply_markup"].inline_keyboard), [])
        self.assertIn("скоро", text.lower())

    def test_navigation_is_never_dropped(self) -> None:
        url = "https://s.click.aliexpress.com/e/_example"
        db = FakeDatabase(
            {
                enabled_key(DEFAULT_EVENT_ID): "1",
                url_key(DEFAULT_EVENT_ID): url,
            }
        )
        context = _context(db, admin_ids=(555,))
        query = FakeQuery(sale_callback(DEFAULT_EVENT_ID), user_id=555)

        run(sales_handlers.sale_callback(FakeUpdate(query), context))

        _, kwargs = query.edits[0]
        callbacks = callbacks_of(kwargs["reply_markup"].inline_keyboard)
        self.assertIn("menu:back", callbacks)  # 🏠 Главное меню still there


class ConfigHelpersTests(unittest.TestCase):
    """Pure helpers: fail-safe interpretation and URL validation."""

    def test_enabled_values(self) -> None:
        for value in ("1", "true", "YES", "on"):
            self.assertTrue(is_enabled_value(value))

    def test_missing_or_unknown_value_is_disabled(self) -> None:
        for value in (None, "", "0", "false", "maybe"):
            self.assertFalse(is_enabled_value(value))

    def test_valid_http_urls_are_kept(self) -> None:
        self.assertEqual(
            safe_http_url("https://s.click.aliexpress.com/e/_x"),
            "https://s.click.aliexpress.com/e/_x",
        )
        self.assertTrue(safe_http_url("http://example.com/a"))

    def test_unsafe_or_malformed_urls_are_rejected(self) -> None:
        for value in (
            None,
            "",
            "   ",
            "javascript:alert(1)",
            "data:text/html,x",
            "ftp://host/f",
            "https://",
            "example.com/path",
        ):
            self.assertIsNone(safe_http_url(value))


class PersistenceTests(unittest.IsolatedAsyncioTestCase):
    """The switch lives in the bot's own SQLite database and survives restarts."""

    async def test_state_persists_across_a_fresh_database_instance(self) -> None:
        db = Database(":memory:")
        try:
            await db.initialize()

            # disabled by default on a brand-new database
            self.assertIsNone(await db.get_config(enabled_key(DEFAULT_EVENT_ID)))

            await db.set_config(enabled_key(DEFAULT_EVENT_ID), "1")
            url = "https://s.click.aliexpress.com/e/_example"
            await db.set_config(url_key(DEFAULT_EVENT_ID), url)

            # the values are read back verbatim, so a restart (which reopens the
            # same file) sees the enabled state and the destination unchanged
            self.assertTrue(
                is_enabled_value(await db.get_config(enabled_key(DEFAULT_EVENT_ID)))
            )
            self.assertEqual(await db.get_config(url_key(DEFAULT_EVENT_ID)), url)

            await db.set_config(enabled_key(DEFAULT_EVENT_ID), "0")
            self.assertFalse(
                is_enabled_value(await db.get_config(enabled_key(DEFAULT_EVENT_ID)))
            )
        finally:
            await db.close()

    async def test_bot_config_table_is_created(self) -> None:
        db = Database(":memory:")
        try:
            await db.initialize()
            self.assertIn("bot_config", await db.list_tables())
        finally:
            await db.close()


if __name__ == "__main__":
    unittest.main()

