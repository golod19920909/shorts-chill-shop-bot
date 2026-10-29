"""Configuration loading tests.

Run without any real credentials and without touching the real .env:
environment variables are patched per test.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from config.settings import BASE_DIR, MarketplaceCredentials, load_settings

CLEAN_ENV = {
    "BOT_TOKEN": "",
    "GEMINI_API_KEY": "",
    "DATABASE_PATH": "",
    "LOG_LEVEL": "",
    "ALIEXPRESS_APP_KEY": "",
    "ALIEXPRESS_APP_SECRET": "",
    # Model roles must start from "unset" in every test, otherwise a
    # developer's .env would make the assertions environment-dependent.
    "GEMINI_MODEL": "",
    "GEMINI_SMART_MODEL": "",
    "GEMINI_FAST_MODEL": "",
    "GEMINI_VISION_MODEL": "",
    "GEMINI_FALLBACK_MODEL": "",
    # Retrieval-depth budgets are limits on REAL API calls: a developer's
    # .env must not decide what these tests assert.
    "SEARCH_MAX_PAGES": "",
    "PHOTO_SEARCH_PAGES": "",
    "PHOTO_SEARCH_HYPOTHESES": "",
    # G24: the «✨ Находки» budgets are hard API ceilings too.
    "FEED_OPEN_SIZE": "",
    "FEED_MAX_PAGES_PER_OPEN": "",
}


class MarketplaceCredentialsTests(unittest.TestCase):
    def test_requires_both_key_and_secret(self) -> None:
        self.assertFalse(MarketplaceCredentials(None, None).is_configured)
        self.assertFalse(MarketplaceCredentials("key", None).is_configured)
        self.assertFalse(MarketplaceCredentials(None, "secret").is_configured)
        self.assertFalse(MarketplaceCredentials("", "").is_configured)
        self.assertTrue(MarketplaceCredentials("key", "secret").is_configured)


class LoadSettingsTests(unittest.TestCase):
    def test_missing_values_are_none_not_empty_strings(self) -> None:
        with patch.dict(os.environ, CLEAN_ENV, clear=False):
            settings = load_settings()

        self.assertIsNone(settings.bot_token)
        self.assertIsNone(settings.gemini_api_key)
        self.assertFalse(settings.is_bot_token_configured)
        self.assertFalse(settings.is_gemini_configured)
        # The default is the model the Gemini API recommends for this key:
        # ``gemini-2.5-flash`` is rejected live with 404 "no longer available
        # to new users", so a stale default would silently disable the AI.
        self.assertEqual(settings.gemini_model, "gemini-3.8-flash")

    def test_defaults_for_database_and_log_level(self) -> None:
        with patch.dict(os.environ, CLEAN_ENV, clear=False):
            settings = load_settings()

        self.assertEqual(settings.database_path, "database/shop_bot.db")
        self.assertEqual(settings.log_level, "INFO")

    def test_values_are_read_from_environment(self) -> None:
        env = dict(CLEAN_ENV)
        env.update(
            {
                "BOT_TOKEN": " 123456:TEST-TOKEN ",
                "GEMINI_API_KEY": "test-gemini-key",
                "DATABASE_PATH": "tmp/test.db",
                "LOG_LEVEL": "DEBUG",
                "ALIEXPRESS_APP_KEY": "ali-key",
                "ALIEXPRESS_APP_SECRET": "ali-secret",
                "GEMINI_MODEL": "gemini-3.6-flash",
            }
        )
        with patch.dict(os.environ, env, clear=False):
            settings = load_settings()

        # Surrounding whitespace is stripped so a copy-pasted .env works.
        self.assertEqual(settings.bot_token, "123456:TEST-TOKEN")
        self.assertTrue(settings.is_bot_token_configured)
        self.assertTrue(settings.is_gemini_configured)
        self.assertEqual(settings.gemini_model, "gemini-3.6-flash")
        self.assertEqual(settings.database_path, "tmp/test.db")
        self.assertEqual(settings.logging_level, 10)  # logging.DEBUG
        self.assertTrue(settings.aliexpress.is_configured)

    def test_each_gemini_role_reads_its_own_env_model(self) -> None:
        # The four roles are independent: FAST/SMART/VISION/FALLBACK must
        # each honour their own variable, so one .env can tune them apart.
        env = dict(CLEAN_ENV)
        env.update(
            {
                "GEMINI_FAST_MODEL": "fast-x",
                "GEMINI_SMART_MODEL": "smart-x",
                "GEMINI_VISION_MODEL": "vision-x",
                "GEMINI_FALLBACK_MODEL": "fallback-x",
            }
        )
        with patch.dict(os.environ, env, clear=False):
            settings = load_settings()

        self.assertEqual(settings.gemini_fast_model, "fast-x")
        self.assertEqual(settings.gemini_smart_model, "smart-x")
        self.assertEqual(settings.gemini_vision_model, "vision-x")
        self.assertEqual(settings.gemini_fallback_model, "fallback-x")
        # Legacy alias keeps pointing at the smart role.
        self.assertEqual(settings.gemini_model, "smart-x")

    def test_legacy_gemini_model_wins_over_smart_for_old_env_files(self) -> None:
        env = dict(CLEAN_ENV)
        env.update({"GEMINI_MODEL": "legacy-x", "GEMINI_SMART_MODEL": "smart-x"})
        with patch.dict(os.environ, env, clear=False):
            settings = load_settings()

        self.assertEqual(settings.gemini_smart_model, "legacy-x")

    def test_model_roles_fall_back_to_documented_defaults(self) -> None:
        with patch.dict(os.environ, CLEAN_ENV, clear=False):
            settings = load_settings()

        self.assertEqual(settings.gemini_fast_model, "gemini-3.8-flash")
        self.assertEqual(settings.gemini_smart_model, "gemini-3.8-flash")
        self.assertEqual(settings.gemini_vision_model, "gemini-3.8-flash")
        self.assertEqual(settings.gemini_fallback_model, "gemini-3.1-flash-lite")

    def test_every_primary_role_runs_the_same_generation(self) -> None:
        """fast/smart/vision share one model; only the fallback differs.

        The three roles are separate VARIABLES (a deployment may still pin a
        cheaper model to one of them), but their documented default is one and
        the same generation: parsing, selection and photo reading are then held
        to a single quality bar.
        """

        with patch.dict(os.environ, CLEAN_ENV, clear=False):
            settings = load_settings()

        primary = {
            settings.gemini_fast_model,
            settings.gemini_smart_model,
            settings.gemini_vision_model,
        }
        self.assertEqual(primary, {"gemini-3.8-flash"})
        self.assertNotIn(settings.gemini_fallback_model, primary)

    def test_retrieval_depth_defaults_are_the_documented_ones(self) -> None:
        with patch.dict(os.environ, CLEAN_ENV, clear=False):
            settings = load_settings()

        self.assertEqual(settings.search_max_pages, 3)
        self.assertEqual(settings.photo_search_pages, 2)
        self.assertEqual(settings.photo_search_hypotheses, 4)

    def test_retrieval_depth_is_configurable(self) -> None:
        env = dict(CLEAN_ENV)
        env.update(
            {
                "SEARCH_MAX_PAGES": "7",
                "PHOTO_SEARCH_PAGES": "3",
                "PHOTO_SEARCH_HYPOTHESES": "6",
            }
        )
        with patch.dict(os.environ, env, clear=False):
            settings = load_settings()

        self.assertEqual(settings.search_max_pages, 7)
        self.assertEqual(settings.photo_search_pages, 3)
        self.assertEqual(settings.photo_search_hypotheses, 6)

    def test_a_broken_retrieval_depth_falls_back_to_the_documented_one(self) -> None:
        """Every unit is a real API call - a typo must not become a crawl."""

        for pages in ("500", "как-нибудь", "-3"):
            with self.subTest(pages=pages):
                env = dict(CLEAN_ENV)
                env.update(
                    {
                        "SEARCH_MAX_PAGES": pages,
                        "PHOTO_SEARCH_PAGES": pages,
                        "PHOTO_SEARCH_HYPOTHESES": pages,
                    }
                )
                with patch.dict(os.environ, env, clear=False):
                    settings = load_settings()

                self.assertEqual(settings.search_max_pages, 3)
                self.assertEqual(settings.photo_search_pages, 2)
                self.assertEqual(settings.photo_search_hypotheses, 4)

    def test_feed_budget_defaults_are_the_approved_g24_ones(self) -> None:
        with patch.dict(os.environ, CLEAN_ENV, clear=False):
            settings = load_settings()

        self.assertEqual(settings.feed_open_size, 20)
        self.assertEqual(settings.feed_max_pages_per_open, 2)

    def test_feed_budgets_are_configurable(self) -> None:
        env = dict(CLEAN_ENV)
        env.update(
            {
                "FEED_OPEN_SIZE": "15",
                "FEED_MAX_PAGES_PER_OPEN": "3",
            }
        )
        with patch.dict(os.environ, env, clear=False):
            settings = load_settings()

        self.assertEqual(settings.feed_open_size, 15)
        self.assertEqual(settings.feed_max_pages_per_open, 3)

    def test_a_broken_feed_budget_clamps_to_a_safe_bound(self) -> None:
        """A typo must never become an unbounded API crawl (§15).

        The project's ``_get_int`` contract (the same one
        ``SEARCH_MAX_PAGES`` follows): out-of-range AND garbage values
        both fall back to the approved G24 defaults - a developer's typo
        can never widen the budget beyond the documented numbers.
        """

        cases = (
            ("0", "0"),
            ("500", "9999"),
            ("как-нибудь", "много"),
        )
        for size, pages in cases:
            with self.subTest(size=size, pages=pages):
                env = dict(CLEAN_ENV)
                env.update(
                    {
                        "FEED_OPEN_SIZE": size,
                        "FEED_MAX_PAGES_PER_OPEN": pages,
                    }
                )
                with patch.dict(os.environ, env, clear=False):
                    settings = load_settings()

                self.assertEqual(
                    (
                        settings.feed_open_size,
                        settings.feed_max_pages_per_open,
                    ),
                    (20, 2),
                )

    def test_removed_marketplace_settings_no_longer_exist(self) -> None:
        # Alibaba has been removed completely - nothing may still read
        # its credentials, so no attribute with that name may survive.
        with patch.dict(os.environ, CLEAN_ENV, clear=False):
            settings = load_settings()

        self.assertFalse(hasattr(settings, "alibaba"))

    def test_unknown_log_level_falls_back_to_info(self) -> None:
        env = dict(CLEAN_ENV)
        env["LOG_LEVEL"] = "not-a-level"
        with patch.dict(os.environ, env, clear=False):
            settings = load_settings()

        self.assertEqual(settings.logging_level, 20)  # logging.INFO

    def test_relative_database_path_is_resolved_against_project_root(self) -> None:
        with patch.dict(os.environ, CLEAN_ENV, clear=False):
            settings = load_settings()

        self.assertEqual(
            settings.database_full_path, Path(BASE_DIR) / "database" / "shop_bot.db"
        )

    def test_absolute_database_path_is_kept(self) -> None:
        env = dict(CLEAN_ENV)
        env["DATABASE_PATH"] = str(Path(BASE_DIR) / "custom" / "x.db")
        with patch.dict(os.environ, env, clear=False):
            settings = load_settings()

        self.assertTrue(settings.database_full_path.is_absolute())
        self.assertEqual(settings.database_full_path.name, "x.db")

    def test_settings_are_immutable(self) -> None:
        with patch.dict(os.environ, CLEAN_ENV, clear=False):
            settings = load_settings()

        with self.assertRaises(Exception):
            settings.bot_token = "changed"  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
