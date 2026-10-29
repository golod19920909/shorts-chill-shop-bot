"""Smoke test: the bot can be assembled, started up and torn down offline.

Uses a fake token, a fake Telegram bot and a temporary database, so no
Telegram or marketplace request is made. This verifies that the wiring
(settings -> database -> providers -> handlers -> command menu) works and
that a full search runs end-to-end with zero configured marketplaces.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from bot import (
    BOT_COMMANDS,
    _post_init,
    _post_shutdown,
    build_application,
    publish_command_menu,
)
from config.settings import MarketplaceCredentials, Settings
from database.database import Database

FAKE_TOKEN = "123456789:FAKE-TOKEN-FOR-TESTS"


class RecordingBot:
    """Stands in for ``telegram.Bot``: records the published menu.

    Assigning it to ``application.bot`` keeps the startup hook fully
    offline - no HTTP request leaves the process - while still exercising
    the production path (``_post_init`` -> ``publish_command_menu``).
    """

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.published: list[list] = []

    async def set_my_commands(self, commands, **kwargs) -> None:
        if self.fail:
            raise RuntimeError("Telegram is unreachable")
        self.published.append(list(commands))


class BotStartupTestCase(unittest.TestCase):
    """Base class providing an isolated database file per test."""

    def setUp(self) -> None:
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp_dir.name) / "startup.db")

    def tearDown(self) -> None:
        self._tmp_dir.cleanup()

    def make_settings(self, **overrides) -> Settings:
        data = {
            "bot_token": FAKE_TOKEN,
            "gemini_api_key": None,
            "database_path": self.db_path,
            "log_level": "CRITICAL",
            "aliexpress": MarketplaceCredentials(),
        }
        data.update(overrides)
        return Settings(**data)


class ApplicationWiringTests(BotStartupTestCase):
    def test_handlers_and_services_are_registered(self) -> None:
        application = build_application(self.make_settings())

        # /start, /help, /new, /favorites, /tracking, /stats, /admin +
        # callbacks + text
        command_names = {
            next(iter(handler.commands))
            for handler in application.handlers[0]
            if handler.__class__.__name__ == "CommandHandler"
        }
        self.assertEqual(
            command_names,
            {
                "start",
                "help",
                "new",
                "favorites",
                "tracking",
                "stats",
                "admin",
            },
        )
        self.assertGreater(len(application.handlers[0]), len(command_names))

        self.assertIn("database", application.bot_data)
        self.assertIn("search_service", application.bot_data)
        self.assertIn("ai_service", application.bot_data)

    def test_only_aliexpress_is_registered_as_a_provider(self) -> None:
        application = build_application(self.make_settings())
        search_service = application.bot_data["search_service"]

        self.assertEqual(len(search_service.providers), 1)
        provider = search_service.providers[0]
        self.assertEqual(provider.marketplace.value, "aliexpress")
        self.assertEqual(provider.display_name, "AliExpress")
        self.assertFalse(provider.is_configured)

    def test_application_build_fails_without_token(self) -> None:
        with self.assertRaises(RuntimeError):
            build_application(self.make_settings(bot_token=None))

    def test_ai_service_is_created_without_gemini_key(self) -> None:
        application = build_application(self.make_settings())
        self.assertFalse(application.bot_data["ai_service"].is_available)


class OfflineStartupAndSearchTests(BotStartupTestCase):
    """Full offline pipeline: startup hook + search + shutdown."""

    def test_startup_creates_database_and_shutdown_closes_providers(self) -> None:
        async def run() -> None:
            application = build_application(self.make_settings())
            # Keep the startup hook offline: the record bot never talks HTTP.
            application.bot = RecordingBot()

            await _post_init(application)

            database: Database = application.bot_data["database"]
            self.assertTrue(Path(self.db_path).exists())
            tables = await database.list_tables()
            self.assertIn("users", tables)
            self.assertIn("tracked_products", tables)

            await _post_shutdown(application)

        asyncio.run(run())

    def test_search_returns_empty_outcome_and_reports_sources(self) -> None:
        async def run() -> None:
            application = build_application(self.make_settings())
            search_service = application.bot_data["search_service"]

            outcome = await search_service.find_products_from_text(
                "wireless headphones under $30 with good reviews"
            )

            self.assertEqual(outcome.found_count, 0)
            self.assertEqual(outcome.request.max_price, 30.0)
            self.assertEqual(set(outcome.unavailable_marketplaces), {"AliExpress"})
            await search_service.aclose()

        asyncio.run(run())

    def test_bulk_quantity_query_uses_aliexpress_only(self) -> None:
        """Bulk wording stays a normal search of the only marketplace.

        The wholesale provider is gone, so no wording may reach a second
        marketplace - while the quantity itself is still parsed.
        """

        async def run() -> None:
            application = build_application(self.make_settings())
            search_service = application.bot_data["search_service"]

            outcome = await search_service.find_products_from_text(
                "Find 100 pieces of wireless earbuds"
            )

            self.assertEqual(outcome.request.quantity, 100)
            self.assertEqual(set(outcome.unavailable_marketplaces), {"AliExpress"})
            await search_service.aclose()

        asyncio.run(run())


class CommandMenuTests(BotStartupTestCase):
    """The Telegram command menu is registered and cannot drift from code."""

    def test_menu_covers_every_registered_command(self) -> None:
        """Whatever has a CommandHandler is reachable from Telegram's menu."""

        application = build_application(self.make_settings())
        handler_commands = {
            command
            for handler in application.handlers[0]
            if handler.__class__.__name__ == "CommandHandler"
            for command in handler.commands
        }

        self.assertEqual(
            {command.command for command in BOT_COMMANDS},
            handler_commands,
        )
        # /start is the entry point the requirement names explicitly.
        self.assertEqual(BOT_COMMANDS[0].command, "start")

    def test_menu_descriptions_are_present_and_short(self) -> None:
        for command in BOT_COMMANDS:
            self.assertTrue(command.description, command.command)
            self.assertLessEqual(len(command.description), 256)

    def test_post_init_publishes_the_menu(self) -> None:
        async def run() -> None:
            application = build_application(self.make_settings())
            bot = RecordingBot()
            application.bot = bot

            await _post_init(application)

            self.assertEqual(len(bot.published), 1)
            published = bot.published[0]
            self.assertEqual([item.command for item in published], [
                command.command for command in BOT_COMMANDS
            ])

        asyncio.run(run())

    def test_menu_failure_is_reported_without_breaking_startup(self) -> None:
        async def run() -> None:
            application = build_application(self.make_settings())
            application.bot = RecordingBot(fail=True)

            # Must not raise: the bot still has to start.
            await _post_init(application)

            self.assertTrue(Path(self.db_path).exists())
            self.assertFalse(await publish_command_menu(RecordingBot(fail=True)))
            self.assertTrue(await publish_command_menu(RecordingBot()))

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
