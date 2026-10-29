"""Tests for the restored per-product INFO logging.

The pipeline must answer "WHICH products were shown, at which rank, with
which price" from the log alone, while never printing a secret:

* one line per product (rank, title, price/currency, product id/URL);
* per-stage drop counts in one compact line;
* the AI-selection outcome explicitly says AI-SELECTED vs DETERMINISTIC
  FALLBACK (operators must SEE when the AI did not decide);
* no API key, token or affiliate tracking parameter ever reaches a log
  line.

Everything runs offline against stub providers, like every other search
test in this suite.
"""

from __future__ import annotations

import asyncio
import logging
import unittest

from config.settings import MarketplaceCredentials
from handlers import search as search_handlers
from models.product import Marketplace, Product
from models.search_request import SearchRequest
from services.ai_service import AIService
from services.product_logging import (
    MAX_PRODUCTS_PER_EVENT,
    MAX_TITLE_LEN,
    format_product_line,
    log_ai_selection_outcome,
    log_final_pool,
    log_pipeline_stage_counts,
    product_lines,
)
from services.search_service import SearchService
from tests.test_handlers_ui import make_context, make_outcome
from tests.test_search_service import StubProvider

#: Distinctive secret markers: if any of these strings ever appears in a
#: captured log line, the logging layer leaked a credential or a tracking
#: parameter. The values are injected where production secrets would live
#: (provider credentials, affiliate link) purely to prove absence.
SECRET_MARKERS = (
    "MARKER_API_KEY_123",
    "MARKER_API_SECRET_456",
    "MARKER_AFF_TOKEN_789",
)


def run(coro):
    return asyncio.run(coro)


def listing(title: str, product_id: str, **overrides) -> Product:
    """A real-shaped AliExpress listing, like every other search test uses."""

    data = {
        "marketplace": Marketplace.ALIEXPRESS,
        "product_id": product_id,
        "title": title,
        "product_url": f"https://www.aliexpress.com/item/{product_id}.html",
        "price": 19.99,
        "currency": "USD",
        # The affiliate link deliberately carries a tracking token: the
        # formatter must NEVER print it (only the public product_url).
        "affiliate_url": (
            "https://s.click.aliexpress.com/e2/token?"
            f"aff_id={SECRET_MARKERS[2]}"
        ),
    }
    data.update(overrides)
    return Product(**data)


class SecretBearingProvider(StubProvider):
    """A provider whose credentials carry the secret markers."""

    def __init__(self) -> None:
        super().__init__(
            marketplace=Marketplace.ALIEXPRESS,
            display_name="AliExpress",
            products=[
                listing("Наушники спортивные bluetooth 100", "np-0"),
                listing("Наушники студийные bluetooth 200", "np-1"),
                listing("Наушники детские bluetooth 300", "np-2"),
            ],
            credentials=MarketplaceCredentials(
                SECRET_MARKERS[0], SECRET_MARKERS[1]
            ),
        )


class ProductLineFormatTests(unittest.TestCase):
    """One line per product: rank, title, price/currency, id/URL."""

    def test_line_carries_every_required_field(self) -> None:
        line = format_product_line(
            listing("Наушники bluetooth 100", "np-0", price=19.99), 1
        )

        self.assertIn("#1", line)
        self.assertIn("Наушники bluetooth 100", line)
        self.assertIn("19.99", line)
        self.assertIn("USD", line)
        self.assertIn("id=np-0", line)
        self.assertIn("url=https://www.aliexpress.com/item/np-0.html", line)

    def test_long_titles_are_truncated_to_one_readable_line(self) -> None:
        line = format_product_line(
            listing("Очень длинный заголовок " * 20, "np-long"), 3
        )

        self.assertIn("#3", line)
        # The raw title cannot appear in full; the truncation marker can.
        self.assertNotIn("заголовок " * 20, line)
        self.assertIn("…", line)
        self.assertLess(len(line), MAX_TITLE_LEN + 200)

    def test_missing_price_is_honestly_marked_not_invented(self) -> None:
        line = format_product_line(listing("Товар без цены", "np-np", price=None), 1)

        self.assertIn("price=?", line)

    def test_affiliate_tracking_never_reaches_the_line(self) -> None:
        line = format_product_line(listing("Наушники", "np-0"), 1)

        self.assertIn("www.aliexpress.com/item/np-0.html", line)
        for marker in SECRET_MARKERS:
            self.assertNotIn(marker, line)
        self.assertNotIn("aff_id", line)

    def test_product_lines_are_capped_with_1_based_ranks(self) -> None:
        products = [listing(f"Товар {i}", f"p-{i}") for i in range(5)]

        lines = product_lines(products, limit=3)

        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[0].startswith("#1 "))
        self.assertTrue(lines[2].startswith("#3 "))


class ProductLoggingHelperTests(unittest.TestCase):
    """The three helpers: pool summary, AI outcome, stage counts."""

    def test_final_pool_logs_header_then_one_line_per_product(self) -> None:
        products = [listing(f"Товар {i}", f"p-{i}") for i in range(3)]

        probe = logging.getLogger("probe")
        with self.assertLogs(probe, level="INFO") as captured:
            log_final_pool(
                probe,
                products,
                context="наушники",
                origin="deterministic-fallback",
            )

        output = captured.output
        self.assertIn(
            "Final pool (наушники, deterministic-fallback): 3 product(s)", output[0]
        )
        per_product = [ln for ln in output if "deterministic-fallback: #" in ln]
        self.assertEqual(len(per_product), 3)
        for index in range(3):
            self.assertIn(f"p-{index}", "\n".join(output))

    def test_final_pool_counts_overflow_instead_of_flooding(self) -> None:
        products = [listing(f"Товар {i}", f"p-{i}") for i in range(6)]

        probe = logging.getLogger("probe")
        with self.assertLogs(probe, level="INFO") as captured:
            log_final_pool(
                probe, products, context="q", origin="ai-selected", limit=2
            )

        joined = "\n".join(captured.output)
        self.assertIn("... +4 more", joined)
        per_product = [ln for ln in captured.output if ": #" in ln]
        self.assertEqual(len(per_product), 2)
        self.assertNotIn("p-5", joined)

    def test_ai_outcome_line_distinguishes_selection_from_fallback(self) -> None:
        probe = logging.getLogger("probe")
        with self.assertLogs(probe, level="INFO") as captured:
            log_ai_selection_outcome(
                probe,
                context="наушники",
                pool_size=80,
                chosen_ids=["a", "b"],
                selected_size=2,
            )
        self.assertIn("AI-SELECTED", "\n".join(captured.output))

        probe = logging.getLogger("probe")
        with self.assertLogs(probe, level="INFO") as captured:
            log_ai_selection_outcome(
                probe,
                context="наушники",
                pool_size=80,
                chosen_ids=[],
                selected_size=30,
                fallback_reason="AI selector returned an empty/invalid id list",
            )
        joined = "\n".join(captured.output)
        self.assertIn("DETERMINISTIC FALLBACK", joined)
        self.assertIn("empty/invalid id list", joined)

    def test_stage_counts_report_drops_per_stage(self) -> None:
        probe = logging.getLogger("probe")
        with self.assertLogs(probe, level="INFO") as captured:
            log_pipeline_stage_counts(
                probe,
                "зарядка",
                [("relevant", 100, 40), ("shown", 40, 30)],
            )

        joined = "\n".join(captured.output)
        self.assertIn("relevant: 100->40 (dropped 60)", joined)
        self.assertIn("shown: 40->30 (dropped 10)", joined)

    def test_default_cap_is_the_ai_feed_size(self) -> None:
        self.assertEqual(MAX_PRODUCTS_PER_EVENT, 20)


def assert_no_secrets(testcase: unittest.TestCase, output: list[str]) -> None:
    """None of the injected secret markers may appear anywhere in ``output``."""

    joined = "\n".join(output)
    for marker in SECRET_MARKERS:
        testcase.assertNotIn(marker, joined)


class SearchPipelineLoggingTests(unittest.TestCase):
    """End-to-end: a stub search must produce per-product INFO lines."""

    def setUp(self) -> None:
        self.providers: list[StubProvider] = []

    def tearDown(self) -> None:
        async def close_all() -> None:
            for provider in self.providers:
                await provider.aclose()

        if self.providers:
            run(close_all())

    def test_find_products_logs_every_shown_product_at_info(self) -> None:
        provider = SecretBearingProvider()
        self.providers.append(provider)
        # No API key on the AIService: the deterministic fallback branch
        # runs, which is the branch an operator most needs to see.
        service = SearchService([provider], ai_service=AIService(api_key=None))
        request = SearchRequest(original_query="наушники")

        with self.assertLogs("services.search_service", level="INFO") as captured:
            outcome = run(service.find_products(request))

        self.assertTrue(outcome.products)
        joined = "\n".join(captured.output)

        # Per-product data: every shown product is identifiable by id,
        # with its 1-based rank on its own line.
        for rank, product in enumerate(outcome.products, start=1):
            self.assertIn(f"#{rank} ", joined)
            self.assertIn(f"id={product.product_id}", joined)
        self.assertIn("19.99", joined)
        self.assertIn("USD", joined)

        # Explicit branch visibility: the operator can SEE that the AI did
        # not decide this list, and WHY.
        self.assertIn("DETERMINISTIC FALLBACK", joined)
        self.assertIn("no AI selector configured", joined)

        # Per-stage counts in one line.
        self.assertIn("Pipeline stages for", joined)

        # No credential or tracking marker anywhere in the whole capture.
        assert_no_secrets(self, captured.output)

    def test_ai_selection_outcome_is_logged_when_a_selector_runs(self) -> None:
        provider = SecretBearingProvider()
        self.providers.append(provider)

        class SelectingAI:
            is_available = True

            async def select_feed_candidates(self, candidates, *, wanted=20, query=None):
                return ["np-1"]

        service = SearchService([provider], ai_service=SelectingAI())
        request = SearchRequest(original_query="наушники")

        with self.assertLogs("services.search_service", level="INFO") as captured:
            outcome = run(service.find_products(request))

        joined = "\n".join(captured.output)
        self.assertIn("AI-SELECTED final=", joined)
        self.assertEqual([p.product_id for p in outcome.products], ["np-1"])
        self.assertIn("id=np-1", joined)
        assert_no_secrets(self, captured.output)


class TelegramListLoggingTests(unittest.TestCase):
    """The list Telegram receives must be the one the log describes."""

    def test_send_search_results_logs_the_rendered_list(self) -> None:
        context = make_context()
        outcome = make_outcome(count=3)

        with self.assertLogs("handlers.search", level="INFO") as captured:
            session_id = run(
                search_handlers.send_search_results(context, 47, outcome)
            )

        self.assertIsNotNone(session_id)
        joined = "\n".join(captured.output)

        self.assertIn("telegram-sent", joined)
        # every rendered product, in render order, with its 1-based rank
        for rank, product in enumerate(outcome.products, start=1):
            self.assertIn(f"#{rank} ", joined)
            self.assertIn(f"id={product.product_id}", joined)
        assert_no_secrets(self, captured.output)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
