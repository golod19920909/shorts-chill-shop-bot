"""Offline tests for the versioned Gemini standard paid-tier cost estimator."""

import unittest
from datetime import datetime, timezone

from services.gemini_pricing import estimate_cost_usd


class GeminiPricingTests(unittest.TestCase):
    def test_flash_uses_total_minus_prompt_for_billable_output(self):
        cost, status, version = estimate_cost_usd(
            "gemini-3.8-flash",
            {
                "prompt_token_count": 1_000,
                "candidates_token_count": 100,
                "thoughts_token_count": 200,
                "total_token_count": 1_300,
            },
            when=datetime(2026, 11, 7, tzinfo=timezone.utc),
        )
        self.assertEqual(status, "estimated")
        self.assertEqual(version, "google-gemini-standard-paid-2026-11-07")
        self.assertAlmostEqual(cost, 0.001875)

    def test_flash_lite_uses_its_own_rates(self):
        cost, status, _ = estimate_cost_usd(
            "gemini-3.1-flash-lite",
            {
                "prompt_token_count": 1_000,
                "candidates_token_count": 100,
                "thoughts_token_count": 200,
                "total_token_count": 1_300,
            },
            when=datetime(2026, 11, 7, tzinfo=timezone.utc),
        )
        self.assertEqual(status, "estimated")
        self.assertAlmostEqual(cost, 0.0007)

    def test_cached_input_uses_cached_rate(self):
        cost, status, _ = estimate_cost_usd(
            "gemini-3.8-flash",
            {
                "prompt_token_count": 1_000,
                "cached_content_token_count": 400,
                "candidates_token_count": 0,
                "thoughts_token_count": 0,
                "total_token_count": 1_000,
            },
            when=datetime(2026, 11, 7, tzinfo=timezone.utc),
        )
        self.assertEqual(status, "estimated")
        self.assertAlmostEqual(cost, 0.00048)

    def test_flash_rate_changes_on_2027_01_01(self):
        tokens = {"prompt_token_count": 1_000, "total_token_count": 2_000}
        old_cost, _, _ = estimate_cost_usd(
            "gemini-3.8-flash", tokens,
            when=datetime(2026, 12, 31, 23, 59, tzinfo=timezone.utc),
        )
        new_cost, _, _ = estimate_cost_usd(
            "gemini-3.8-flash", tokens,
            when=datetime(2027, 1, 1, 0, 0, tzinfo=timezone.utc),
        )
        self.assertAlmostEqual(old_cost, 0.0045)
        self.assertAlmostEqual(new_cost, 0.000009)

    def test_missing_usage_and_unknown_models_are_not_zero_cost(self):
        self.assertEqual(
            estimate_cost_usd("gemini-3.8-flash", None)[0:2],
            (None, "unknown_usage"),
        )
        self.assertEqual(
            estimate_cost_usd(
                "future-model",
                {"prompt_token_count": 1, "total_token_count": 1},
            )[0:2],
            (None, "unknown_model"),
        )

    def test_inconsistent_usage_is_unknown(self):
        cost, status, _ = estimate_cost_usd(
            "gemini-3.8-flash",
            {"prompt_token_count": 100, "total_token_count": 90},
        )
        self.assertIsNone(cost)
        self.assertEqual(status, "unknown_usage")


if __name__ == "__main__":
    unittest.main()
