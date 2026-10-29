"""Static guards: obsolete features must not creep back into the code.

The final spec removed «Сравнить», «Выгодные находки», «Промо» and
«Случайное». These tests read the source files and fail as soon as any
of the deleted plumbing (modules, methods, callbacks, keyboards) is
referenced again - a regression a pure unit test of the live code would
never notice.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


class RemovedFeatureTests(unittest.TestCase):
    """Deleted features stay deleted."""

    def test_search_service_has_no_deals_promo_or_random_plumbing(self) -> None:
        code = source("services/search_service.py")
        tree = ast.parse(code)
        names = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for removed in (
            "find_deals",
            "promo_providers",
            "list_promo_campaigns",
            "find_promo_products",
            "_collect_promo",
            "find_random_promo_product",
            "compare_with_ai",
        ):
            self.assertNotIn(removed, names)
        self.assertNotIn("import random", code)

    def test_deleted_handler_and_keyboard_modules_are_absent(self) -> None:
        self.assertFalse((ROOT / "handlers" / "promotions.py").exists())
        self.assertFalse((ROOT / "keyboards" / "promotions.py").exists())
        wiring = source("bot/__init__.py")
        self.assertNotIn("from handlers import promotions", wiring)
        self.assertNotIn("keyboards.promotions", wiring)

    def test_provider_no_longer_claims_removed_capabilities(self) -> None:
        self.assertNotIn(
            "supports_deals_feed", source("services/marketplaces/aliexpress.py")
        )
        base = source("services/marketplaces/base.py")
        self.assertNotIn("supports_promotions", base)
        self.assertNotIn("def get_deals", base)
        self.assertNotIn("def get_promo_campaigns", base)
        self.assertNotIn("def get_promo_products", base)

    def test_comparison_text_builders_are_gone(self) -> None:
        comparison = source("services/comparison_service.py")
        for removed in (
            "def build_comparison_text",
            "def describe_price_leader",
            "def summarize_missing_fields",
        ):
            self.assertNotIn(removed, comparison)

    def test_database_and_admin_have_no_promo_statistics(self) -> None:
        self.assertNotIn("get_top_promotions", source("database/database.py"))
        self.assertNotIn("get_top_promotions", source("handlers/admin.py"))

    def test_ai_service_exposes_ranking_not_comparison(self) -> None:
        ai_source = source("services/ai_service.py")
        self.assertIn("def rank_candidates", ai_source)
        self.assertNotIn("def compare_products", ai_source)
        self.assertNotIn("def _product_payload", ai_source)

    def test_tombstone_pattern_covers_legacy_callbacks(self) -> None:
        keyboards = source("keyboards/main.py")
        self.assertIn("REMOVED_CALLBACK_PATTERN", keyboards)
        for legacy in ("menu:random", "menu:deals", "dfeed", "promos", "rdeal"):
            self.assertIn(legacy, keyboards)
        wiring = source("bot/__init__.py")
        self.assertIn("REMOVED_CALLBACK_PATTERN", wiring)
        self.assertIn("removed_screen_callback", wiring)


class PopularCurationContractTests(unittest.TestCase):
    """The «✨ Находки» feed must stay MECHANICAL (G24 contract)."""

    @staticmethod
    def popular_body() -> str:
        """Source of ``find_feed_page`` alone (up to the next method)."""

        code = source("services/search_service.py")
        return code.split("async def find_feed_page(")[1].split(
            "async def find_discovery("
        )[0]

    def test_find_feed_page_runs_only_the_documented_mechanics(self) -> None:
        # G24: normalize -> dedup -> technical validity -> history skip.
        # Nothing else may run: no local pipeline, no ranking stage, no
        # popularity/quality judgement of any kind.
        popular = self.popular_body()
        for mechanic in (
            "deduplicate_products",
            "identity_pair",
            "supports_feed",
        ):
            self.assertIn(mechanic, popular, f"find_feed_page must use {mechanic}")
        for stage in (
            "run_popular_pipeline",
            "feed_curation.drop_near_duplicates",
            "feed_curation.diversity_select",
            "feed_curation.penalize_recent",
            "ModeRanker",
            "QualityFilter",
        ):
            self.assertNotIn(stage, popular, f"find_feed_page must not run {stage}")

    def test_popular_ranking_is_measured_not_guessed(self) -> None:
        """The feed is never ranked - not by us, not by a model.

        The keyword-less request carries NO ``sort`` (AliExpress orders its
        own listing), and the service never calls the AI selector or any
        local ranker over the feed. A hand-written blocklist of "bad"
        product types is equally absent: it would hide real listings.
        """

        popular = self.popular_body()
        self.assertNotIn("SortPreference.POPULARITY_DESC", popular)
        # The feed never touches the AI selector of G22.
        self.assertNotIn("rank_candidates", popular)
        self.assertNotIn("apply_feed_selection", popular)

        curation = source("services/feed_curation.py")
        for banned in ("demote_junk", "is_junk_title", "_JUNK"):
            self.assertNotIn(banned, curation)
        service = source("services/search_service.py")
        for banned in ("demote_junk", "is_junk_title"):
            self.assertNotIn(banned, service)

    def test_the_feed_cursor_is_the_only_session_state(self) -> None:
        # The old RAM anti-repeat ring is gone: the SQLite feed history is
        # the single no-repeat source, and the RAM state is the page cursor.
        discovery = source("handlers/discovery.py")
        self.assertIn("FEED_PAGE_KEY", discovery)
        self.assertNotIn("POPULAR_RECENT_KEY", discovery)
        self.assertNotIn("POPULAR_RECENT_RING", discovery)
        self.assertIn("feed_seen", discovery)

    def test_text_search_forwards_user_currency_and_opens_a_card(self) -> None:
        handler = source("handlers/search.py")
        # The engine turn forwards the user's region currency (the legacy
        # direct call is replaced by the convsearch engine, same contract).
        self.assertIn('currency=region.get("currency")', handler)
        self.assertIn("engine.handle_message", handler)
        # Card-first contract: no summary screen, no paged list, no numbered
        # link list - the whole feed is browsed inside ONE card.
        self.assertNotIn("summary: bool =", handler)
        self.assertNotIn("_build_page_text", handler)
        self.assertNotIn("pagination_keyboard", handler)
        self.assertNotIn("products_summary_rows", handler)
        self.assertIn("_card_headline", handler)

    def test_nav_back_and_tombstone_handlers_are_registered(self) -> None:
        wiring = source("bot/__init__.py")
        self.assertIn("nav_back_callback", wiring)
        self.assertIn("removed_screen_callback", wiring)
        self.assertIn("hot_discount_section_callback", wiring)


if __name__ == "__main__":
    unittest.main()