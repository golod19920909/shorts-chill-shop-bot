"""Regression tests for conversational budget-only follow-ups.

These tests stay offline: they exercise the real ledger and retrieval strategy
without calling Gemini, Telegram, or AliExpress.
"""
from __future__ import annotations

import unittest

from services.convsearch.engine import _product_type_of
from services.convsearch.retrieval import build_retrieval_strategy
from services.convsearch.state import ConversationEngineState, MessageIntent, _budget


class BudgetFollowUpTests(unittest.TestCase):
    def test_thousands_budget_refines_current_product_and_keeps_type(self):
        state = ConversationEngineState(user_key="budget-follow-up")
        first = "Сварочный аппарат"
        observation = state.observe(first)
        state.record_slots(observation)
        self.assertEqual(observation["intent"], MessageIntent.NEW_SEARCH)
        state.record_product_type(_product_type_of(first))

        follow_up = "До 2000 тысяч гривен"
        observation = state.observe(follow_up)
        self.assertEqual(observation["intent"], MessageIntent.REFINE)
        state.record_slots(observation)

        strategy = build_retrieval_strategy(state, follow_up, currency="UAH")
        self.assertIsNotNone(strategy)
        self.assertEqual(strategy.product_type, "сварочный аппарат")
        self.assertEqual(strategy.request.normalized_query, "сварочный аппарат")
        self.assertEqual(strategy.request.max_price, 2_000_000.0)
        self.assertFalse(strategy.request.required_features)
        self.assertNotIn("тысяч", strategy.request.normalized_query)
        self.assertNotIn("2000", strategy.request.normalized_query)

    def test_regular_budget_follow_up_does_not_switch_product(self):
        state = ConversationEngineState(user_key="budget-follow-up-2")
        first = "шуруповерт"
        observation = state.observe(first)
        state.record_slots(observation)
        state.record_product_type(_product_type_of(first))

        follow_up = "До 2000 гривен"
        observation = state.observe(follow_up)
        self.assertEqual(observation["intent"], MessageIntent.REFINE)
        state.record_slots(observation)
        strategy = build_retrieval_strategy(state, follow_up, currency="UAH")

        self.assertEqual(strategy.product_type, "шуруповерт")
        self.assertEqual(strategy.request.normalized_query, "шуруповерт")
        self.assertEqual(strategy.request.max_price, 2000.0)
        self.assertFalse(strategy.request.required_features)

    def test_budget_only_first_message_has_no_fake_product_type(self):
        text = "До 2000 тысяч гривен"
        self.assertIsNone(_product_type_of(text))

    def test_budget_scale_is_not_applied_without_scale_word(self):
        self.assertEqual(_budget("до 2000 гривен")["max"], 2000.0)
        self.assertEqual(_budget("до 2 тысяч гривен")["max"], 2000.0)

    def test_budget_correction_preserves_product_and_other_requirements(self):
        state = ConversationEngineState(user_key="budget-correction")
        first = "шуруповерт"
        observation = state.observe(first)
        state.record_slots(observation)
        state.record_product_type(_product_type_of(first))

        feature = "с чехлом в комплекте"
        observation = state.observe(feature)
        state.record_slots(observation)
        self.assertTrue(state.active_values("feature"))

        correction = "На самом деле до 500 гривен"
        observation = state.observe(correction)
        self.assertEqual(observation["intent"], MessageIntent.CORRECTION)
        state.record_slots(observation)

        self.assertEqual(state.active_by_kind("type").value, "шуруповерт")
        self.assertTrue(state.active_values("feature"))
        self.assertEqual(state.active_by_kind("budget").value, "500 UAH")

    def test_budget_amount_is_not_a_retrieval_feature(self):
        state = ConversationEngineState(user_key="budget-feature")
        first = "шуруповерт"
        observation = state.observe(first)
        state.record_slots(observation)
        state.record_product_type(_product_type_of(first))
        follow_up = "до 2000 гривен"
        observation = state.observe(follow_up)
        state.record_slots(observation)
        strategy = build_retrieval_strategy(state, follow_up, currency="UAH")
        self.assertFalse(strategy.request.required_features)
        self.assertEqual(strategy.request.normalized_query, "шуруповерт")


if __name__ == "__main__":
    unittest.main()
