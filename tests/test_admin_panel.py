"""Admin panel tests: access control, all 13 screens, navigation, safety.

Every screen renders against a REAL temporary SQLite database (no mocked
aggregates), the panel refuses non-admins, unknown buttons never dead-end,
and no secret from the settings may ever appear in any rendered screen
(spec §38-§54, §65, §71).
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path

from config.hot_discounts import HOT_DISCOUNT_SECTIONS
from config.sales import DEFAULT_EVENT_ID, enabled_key, is_enabled_value, url_key
from config.settings import MarketplaceCredentials, Settings
from database.database import Database
from handlers import admin
from keyboards.admin import ADMIN_SECTIONS, admin_main_keyboard, admin_screen_keyboard

BOT_TOKEN_SENTINEL = "999888777:SECRET-TOKEN-THAT-MUST-NEVER-APPEAR"
APP_SECRET_SENTINEL = "ali-super-secret-value"
GEMINI_SENTINEL = "gemini-secret-key-xyz"


def make_settings(db_path: str, admin_ids: tuple[int, ...] = (555,)) -> Settings:
    return Settings(
        bot_token=BOT_TOKEN_SENTINEL,
        gemini_api_key=GEMINI_SENTINEL,
        database_path=db_path,
        log_level="CRITICAL",
        aliexpress=MarketplaceCredentials("app-key", APP_SECRET_SENTINEL),
        admin_ids=admin_ids,
    )


class FakeUser:
    def __init__(self, user_id: int) -> None:
        self.id = user_id
        self.username = "tester"
        self.first_name = "Tester"


class FakeChat:
    id = 47


class FakeMessage:
    def __init__(self) -> None:
        self.chat_id = 47
        self.texts: list[str] = []
        self.kwargs: list[dict] = []

    async def reply_text(self, text: str, **kwargs) -> None:
        self.texts.append(text)
        self.kwargs.append(kwargs)


class FakeQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self.answers: list[tuple[str | None, bool]] = []
        self.edits: list[tuple[str, dict]] = []

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text: str, **kwargs) -> None:
        self.edits.append((text, kwargs))


class FakeUpdate:
    def __init__(self, *, callback_query=None, message=None, user_id: int = 555):
        self.callback_query = callback_query
        self.effective_message = message
        self.effective_chat = FakeChat() if (message or callback_query) else None
        self.effective_user = FakeUser(user_id)


class FakeSearchService:
    providers: list = []


class FakeApplication:
    def __init__(self, bot_data: dict) -> None:
        self.bot_data = bot_data


class FakeContext:
    def __init__(self, settings: Settings, database: Database) -> None:
        self.user_data: dict = {}
        self.application = FakeApplication(
            {
                "settings": settings,
                "database": database,
                "search_service": FakeSearchService(),
                "ai_service": None,
                "price_tracker": None,
            }
        )


def run(coro):
    return asyncio.run(coro)


class AdminPanelTestCase(unittest.TestCase):
    """Base: real temp database + admin configured as telegram id 555."""

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()
        self.db_path = str(Path(self._tmp) / "admin.db")
        self.db = Database(self.db_path)
        run(self.db.initialize())
        self.settings = make_settings(self.db_path)

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def make_context(self) -> FakeContext:
        return FakeContext(self.settings, self.db)


class AdminAccessTests(AdminPanelTestCase):
    def test_no_admins_configured_denies_everything(self) -> None:
        settings = make_settings(self.db_path, admin_ids=())
        context = FakeContext(settings, self.db)
        message = FakeMessage()

        run(admin.admin_command(FakeUpdate(message=message), context))

        self.assertEqual(len(message.texts), 1)
        self.assertIn("ADMIN_IDS", message.texts[0])

    def test_non_admin_gets_short_refusal_without_data(self) -> None:
        context = self.make_context()
        query = FakeQuery("adm:stats")

        run(admin.admin_callback(FakeUpdate(callback_query=query, user_id=1), context))

        self.assertEqual(len(query.answers), 1)
        text, alert = query.answers[0]
        self.assertIn("только администратору", text)
        self.assertTrue(alert)
        self.assertEqual(query.edits, [])

    def test_admin_gets_the_section_grid(self) -> None:
        context = self.make_context()
        message = FakeMessage()

        run(admin.admin_command(FakeUpdate(message=message), context))

        self.assertIn("Админ-панель", message.texts[0])
        rows = message.kwargs[0]["reply_markup"].inline_keyboard
        labels = [b.text for row in rows for b in row]
        self.assertEqual(len(labels), 15)  # 14 sections + home
        self.assertIn("📊 Статистика", labels)
        self.assertIn("⚙️ Система", labels)
        self.assertIn("🏷 Акции", labels)


class AdminScreenRenderTests(AdminPanelTestCase):
    """§71: every button opens a functional screen with real data."""

    def _render_section(self, section_key: str) -> tuple[str, object]:
        context = self.make_context()
        query = FakeQuery(f"adm:{section_key}")
        run(
            admin.admin_callback(FakeUpdate(callback_query=query), context)
        )
        self.assertTrue(query.edits, f"экран {section_key} ничего не отрисовал")
        text, kwargs = query.edits[0]
        return text, kwargs.get("reply_markup")

    def test_every_section_renders_non_empty_text(self) -> None:
        for key, _label in ADMIN_SECTIONS:
            with self.subTest(section=key):
                text, keyboard = self._render_section(key)
                self.assertTrue(text.strip())
                self.assertIsNotNone(keyboard)
                callbacks = [
                    b.callback_data
                    for row in keyboard.inline_keyboard
                    for b in row
                    if b.callback_data
                ]
                self.assertIn("adm:menu", callbacks)
                self.assertIn("menu:back", callbacks)

    def test_unknown_callback_renders_the_menu_not_a_crash(self) -> None:
        context = self.make_context()
        query = FakeQuery("adm:does-not-exist")

        run(admin.admin_callback(FakeUpdate(callback_query=query), context))

        self.assertEqual(len(query.edits), 1)
        self.assertIn("Админ-панель", query.edits[0][0])

    def test_period_switch_updates_the_stats_screen(self) -> None:
        context = self.make_context()
        run(
            admin.admin_callback(
                FakeUpdate(callback_query=FakeQuery("adm:stats")), context
            )
        )
        query_period = FakeQuery("adm:30")
        run(admin.admin_callback(FakeUpdate(callback_query=query_period), context))

        self.assertIn("30 дней", query_period.edits[0][0])
        self.assertEqual(context.user_data[admin.ADMIN_PERIOD_KEY], "30")

    def test_refresh_keeps_current_screen(self) -> None:
        context = self.make_context()
        query = FakeQuery("adm:favs")
        run(admin.admin_callback(FakeUpdate(callback_query=query), context))
        self.assertIn("Избранное", query.edits[0][0])

        query_refresh = FakeQuery("adm:refresh")
        run(admin.admin_callback(FakeUpdate(callback_query=query_refresh), context))
        self.assertIn("Избранное", query_refresh.edits[0][0])

    def test_discounts_screen_lists_exactly_four_configured_urls(self) -> None:
        self.assertEqual(len(HOT_DISCOUNT_SECTIONS), 4)
        _, _text, = None, None  # placeholder to keep flake quiet
        text, _kb = self._render_section("discounts")
        for section in HOT_DISCOUNT_SECTIONS:
            self.assertIn(section.url, text)

    def test_users_screen_is_paginated(self) -> None:
        for tid in range(1000, 1000 + admin.USERS_PER_PAGE + 1):
            run(self.db.get_or_create_user(tid))

        _, keyboard = self._render_section("users")
        callbacks = [
            b.callback_data
            for row in keyboard.inline_keyboard
            for b in row
            if b.callback_data
        ]
        self.assertIn("adm:users:p1", callbacks)

    def test_api_test_without_provider_is_honest(self) -> None:
        result = run(admin._run_api_test(self.make_context()))
        self.assertIn("нет настроенного провайдера", result)

    def test_logs_screen_renders_and_never_crashes(self) -> None:
        text, _kb = self._render_section("logs")
        self.assertIn("Логи", text)

    def test_system_screen_reports_python_version(self) -> None:
        text, _kb = self._render_section("system")
        self.assertIn("Python", text)


class AdminSalesToggleTests(AdminPanelTestCase):
    """The «Акции» section (§6.2): a live, self-describing on/off control that
    reads and writes the SAME ``bot_config`` store the ``manage_sales.py`` CLI
    uses. The displayed state always reflects the persisted value, a non-admin
    is refused even through a direct toggle callback, and toggling never
    touches the ordinary product search.
    """

    def _toggle(self, context, data: str, user_id: int = 555) -> FakeQuery:
        query = FakeQuery(data)
        run(admin.admin_callback(FakeUpdate(callback_query=query, user_id=user_id), context))
        return query

    def _render_section(self, section_key: str) -> tuple[str, object]:
        context = self.make_context()
        query = FakeQuery(f"adm:{section_key}")
        run(admin.admin_callback(FakeUpdate(callback_query=query), context))
        self.assertTrue(query.edits, f"экран {section_key} ничего не отрисовал")
        text, kwargs = query.edits[0]
        return text, kwargs.get("reply_markup")

    def test_screen_shows_disabled_state_by_default(self) -> None:
        text, _kb = self._render_section("sales")
        self.assertIn("Выключена", text)
        self.assertIn("Включить", " ".join(
            b.text for row in _kb.inline_keyboard for b in row
        ))

    def test_enable_then_disable_persists_and_reflects_real_state(self) -> None:
        context = self.make_context()

        # enable
        self._toggle(context, "adm:sale_on")
        text, _kb = self._render_section("sales")
        self.assertIn("Включена", text)

        # the state is really in the database, not just on the label
        run(self.db.initialize())
        stored = run(self.db.get_config(enabled_key(DEFAULT_EVENT_ID)))
        self.assertTrue(is_enabled_value(stored))

        # disable again
        self._toggle(context, "adm:sale_off")
        text2, _kb2 = self._render_section("sales")
        self.assertIn("Выключена", text2)
        stored2 = run(self.db.get_config(enabled_key(DEFAULT_EVENT_ID)))
        self.assertFalse(is_enabled_value(stored2))

    def test_state_survives_a_fresh_database_instance(self) -> None:
        context = self.make_context()
        self._toggle(context, "adm:sale_on")

        # Simulate a restart: a brand-new Database on the SAME file must read
        # the enabled state back.
        reopened = Database(self.db_path)
        try:
            stored = run(reopened.get_config(enabled_key(DEFAULT_EVENT_ID)))
            self.assertTrue(is_enabled_value(stored))
        finally:
            run(reopened.close())

    def test_non_admin_cannot_toggle_even_with_a_direct_callback(self) -> None:
        context = self.make_context()
        query = self._toggle(context, "adm:sale_on", user_id=999)

        self.assertIn("только администратору", query.answers[0][0])
        # nothing was written: the flag is still absent/disabled
        stored = run(self.db.get_config(enabled_key(DEFAULT_EVENT_ID)))
        self.assertFalse(is_enabled_value(stored))

    def test_screen_never_leaks_the_affiliate_url_or_secrets(self) -> None:
        context = self.make_context()
        run(self.db.set_config(url_key(DEFAULT_EVENT_ID), "https://s.click.aliexpress.com/e/_x"))
        text, _kb = self._render_section("sales")
        # the raw URL is not echoed on the admin screen
        self.assertNotIn("s.click.aliexpress.com", text)
        self.assertIn("Акции", text)


class AdminSecurityTests(AdminPanelTestCase):
    """§65: secrets never appear on any screen."""

    def _assert_no_secrets(self, text: str, where: str) -> None:
        self.assertNotIn(BOT_TOKEN_SENTINEL, text, f"токен в {where}")
        self.assertNotIn("SECRET-TOKEN-THAT-MUST-NEVER-APPEAR", text, f"токен в {where}")
        self.assertNotIn(APP_SECRET_SENTINEL, text, f"app_secret в {where}")
        self.assertNotIn(GEMINI_SENTINEL, text, f"gemini key в {where}")

    def test_no_screen_leaks_any_secret(self) -> None:
        for key, _label in ADMIN_SECTIONS:
            with self.subTest(section=key):
                context = self.make_context()
                query = FakeQuery(f"adm:{key}")
                run(admin.admin_callback(FakeUpdate(callback_query=query), context))
                text, _ = query.edits[0]
                self._assert_no_secrets(text, key)

    def test_menu_screen_leaks_no_secret(self) -> None:
        context = self.make_context()
        message = FakeMessage()
        run(admin.admin_command(FakeUpdate(message=message), context))
        self._assert_no_secrets(message.texts[0], "menu")

    def test_api_screen_never_shows_credentials(self) -> None:
        context = self.make_context()
        query = FakeQuery("adm:api")
        run(admin.admin_callback(FakeUpdate(callback_query=query), context))
        text, _ = query.edits[0]
        self._assert_no_secrets(text, "api")
        self.assertNotIn("app-key", text)
        self.assertNotIn("sign=", text)


class AdminKeyboardTests(unittest.TestCase):
    def test_main_keyboard_has_thirteen_sections_plus_home(self) -> None:
        rows = admin_main_keyboard().inline_keyboard
        callbacks = [b.callback_data for row in rows for b in row]
        expected = [f"adm:{key}" for key, _ in ADMIN_SECTIONS]
        self.assertEqual(callbacks[:-1], expected)
        self.assertEqual(callbacks[-1], "menu:back")

    def test_screen_keyboard_navigation_is_complete(self) -> None:
        for key, _label in ADMIN_SECTIONS:
            with self.subTest(section=key):
                rows = admin_screen_keyboard(key, "7").inline_keyboard
                callbacks = [
                    b.callback_data for row in rows for b in row if b.callback_data
                ]
                self.assertIn("adm:menu", callbacks)
                self.assertIn("menu:back", callbacks)
                self.assertIn("adm:refresh", callbacks)


if __name__ == "__main__":
    unittest.main()


