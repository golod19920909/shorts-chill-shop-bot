"""Regression tests for conversational budget parsing without currency."""

from __future__ import annotations

import unittest

from services.convsearch.state import ConversationEngineState, _budget


class BudgetWithoutCurrencyParsingTests(unittest.TestCase):
    def test_explicit_ceiling_without_currency_is_a_budget(self) -> None:
        self.assertEqual(
            _budget("Спиннинг до 2000"),
            {"max": 2000.0, "currency": None},
        )

    def test_budget_word_without_currency_is_a_budget(self) -> None:
        self.assertEqual(
            _budget("Бюджет 2000"),
            {"max": 2000.0, "currency": None},
        )

    def test_scale_without_currency_is_preserved(self) -> None:
        self.assertEqual(
            _budget("До 2 тыс"),
            {"max": 2000.0, "currency": None},
        )

    def test_explicit_currency_is_preserved(self) -> None:
        self.assertEqual(
            _budget("Спиннинг до 30 долларов"),
            {"max": 30.0, "currency": "USD"},
        )

    def test_unframed_product_number_is_not_a_budget(self) -> None:
        self.assertEqual(_budget("Спиннинг 2000"), {})

    def test_budget_without_currency_is_recorded_in_ledger(self) -> None:
        state = ConversationEngineState(user_key="budget-without-currency")
        observation = state.observe("Спиннинг до 2000")
        state.record_slots(observation)

        budget = state.active_by_kind("budget")
        self.assertIsNotNone(budget)
        self.assertEqual(budget.value, "2000.0")
