"""Regression tests for conversational budget-only follow-ups.

These tests stay offline: they exercise the real ledger and retrieval strategy
without calling Gemini, Telegram, or AliExpress.
"""
from __future__ import annotations

import unittest

from services.convsearch.engine import _product_type_of
from services.convsearch.retrieval import build_retrieval_strategy
from services.convsearch.state import ConversationEngineState, MessageIntent, Slot, _budget


class BudgetFollowUpTests(unittest.TestCase):
    def test_initial_search_keeps_explicit_connector_feature_without_gemini(self):
        state = ConversationEngineState(user_key="initial-explicit-feature")
        first = "Нужен аккумуляторный шуруповёрт с двумя аккумуляторами"

        observation = state.observe(first)
        state.record_slots(observation)
        state.record_product_type("аккумуляторный шуруповёрт")

        strategy = build_retrieval_strategy(state, first)

        self.assertIsNotNone(strategy)
        self.assertEqual(
            state.active_values("feature"),
            ["двумя аккумуляторами"],
        )
        self.assertIn("двумя аккумуляторами", strategy.request.normalized_query)
        self.assertIn("двумя аккумуляторами", strategy.request.required_features)

    def test_initial_search_does_not_turn_request_scaffolding_into_features(self):
        state = ConversationEngineState(user_key="initial-no-scaffolding")
        first = "Нужен шуруповёрт, покажи 5 вариантов"

        observation = state.observe(first)
        state.record_slots(observation)

        self.assertEqual(state.active_values("feature"), [])

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

    def test_negated_follow_up_does_not_become_positive_search_term(self):
        state = ConversationEngineState(user_key="negated-follow-up")
        first = "Сварочный аппарат"
        observation = state.observe(first)
        state.record_slots(observation)
        state.record_product_type(_product_type_of(first))

        follow_up = "Только не полуавтомат"
        observation = state.observe(follow_up)
        self.assertEqual(observation["intent"], MessageIntent.REFINE)
        state.record_slots(observation)

        strategy = build_retrieval_strategy(state, follow_up)
        self.assertIsNotNone(strategy)
        self.assertEqual(strategy.product_type, "сварочный аппарат")
        self.assertEqual(strategy.request.normalized_query, "сварочный аппарат")
        self.assertNotIn("полуавтомат", strategy.request.normalized_query)
        self.assertFalse(
            any("полуавтомат" in feature for feature in strategy.request.required_features)
        )
        self.assertTrue(
            any("полуавтомат" in exclusion for exclusion in strategy.request.excluded_features)
        )

    def test_exclusion_is_not_reintroduced_by_raw_query_variants(self):
        from services.query_variants import product_query_variants

        state = ConversationEngineState(user_key="negated-variant-follow-up")
        first = "Сварочный аппарат"
        observation = state.observe(first)
        state.record_slots(observation)
        state.record_product_type(_product_type_of(first))

        follow_up = "Только не полуавтомат"
        observation = state.observe(follow_up)
        state.record_slots(observation)
        strategy = build_retrieval_strategy(state, follow_up)
        self.assertIsNotNone(strategy)

        # This is the exact adapter path: primary query is the active product,
        # but original_query is the latest follow-up. Derived variants must
        # honor the conversational excluded_features field as well.
        variants = product_query_variants(strategy.request)
        self.assertEqual(variants[0], "сварочный аппарат")
        for variant in variants[1:]:
            self.assertNotIn("полуавтомат", variant.casefold())

    def test_mandatory_feature_keeps_shared_type_noun_in_retrieval_phrase(self):
        state = ConversationEngineState(user_key="feature-shared-type-noun")
        state.record_product_type("аккумуляторный шуруповерт")
        state.slots.append(Slot(
            kind="feature",
            value="двумя аккумуляторами",
            source_turn=2,
            raw_text="с двумя аккумуляторами",
        ))

        strategy = build_retrieval_strategy(state, "с двумя аккумуляторами")

        self.assertIsNotNone(strategy)
        self.assertEqual(
            strategy.request.normalized_query,
            "аккумуляторный шуруповерт двумя аккумуляторами",
        )
        self.assertEqual(
            strategy.request.required_features,
            ["двумя аккумуляторами"],
        )

    def test_compatibility_phrase_keeps_shared_type_noun_in_retrieval_phrase(self):
        state = ConversationEngineState(user_key="compatibility-shared-type-noun")
        state.record_product_type("фильтр для воды")
        state.slots.append(Slot(
            kind="compatibility",
            value="фильтр для воды в системе обратного осмоса",
            source_turn=2,
            raw_text="для системы обратного осмоса",
        ))

        strategy = build_retrieval_strategy(state, "для системы обратного осмоса")

        self.assertIsNotNone(strategy)
        self.assertIn("фильтр", strategy.request.normalized_query)
        self.assertIn("обратного осмоса", strategy.request.normalized_query)
        self.assertEqual(
            strategy.request.required_features,
            ["фильтр для воды в системе обратного осмоса"],
        )

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
        self.assertEqual(state.active_by_kind("budget").value, "500.0 UAH")

    def test_lightweight_followup_becomes_soft_search_preference(self):
        state = ConversationEngineState(user_key="lightweight-follow-up")
        first = "Сварочный аппарат"
        observation = state.observe(first)
        state.record_slots(observation)
        state.record_product_type(_product_type_of(first))

        budget = "До 2000 гривен"
        observation = state.observe(budget)
        state.record_slots(observation)

        followup = "А есть полегче?"
        observation = state.observe(followup)
        self.assertEqual(observation["intent"], MessageIntent.REFINE)
        state.record_slots(observation)

        strategy = build_retrieval_strategy(state, followup, currency="UAH")
        self.assertIsNotNone(strategy)
        self.assertEqual(strategy.product_type, "сварочный аппарат")
        self.assertEqual(strategy.request.normalized_query, "сварочный аппарат легкий")
        self.assertEqual(strategy.request.max_price, 2000.0)
        self.assertEqual(state.active_values("preference"), ["легкий"])
        self.assertFalse(strategy.request.required_features)

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


    def test_ai_correction_with_new_exclusion_preserves_unrelated_constraints(self):
        state = ConversationEngineState(user_key="correction-exclusion-preserves-ledger")
        first = "Сварочный аппарат"
        observation = state.observe(first)
        state.record_slots(observation)
        state.record_product_type(_product_type_of(first))

        observation = state.observe("не ударный")
        state.record_slots(observation)
        observation = state.observe("с двумя аккумуляторами")
        state.record_slots(observation)

        # Gemini may classify the additive exclusion as "correction".
        observation = state.observe(
            "Только не полуавтомат",
            intent_override=MessageIntent.CORRECTION,
        )
        state.record_slots(observation)

        self.assertEqual(state.active_by_kind("type").value, "сварочный аппарат")
        self.assertTrue(any("двумя аккумуляторами" in value for value in state.active_values("feature")))
        self.assertTrue(any("ударный" in value for value in state.active_values("exclusion")))
        self.assertTrue(any("полуавтомат" in value for value in state.active_values("exclusion")))


    def test_single_word_use_case_after_dlya_is_preserved_as_soft_context(self):
        state = ConversationEngineState(user_key="single-word-use-case")
        first = "Сварочный аппарат"
        observation = state.observe(first)
        state.record_slots(observation)
        state.record_product_type(_product_type_of(first))

        observation = state.observe("Для дома")
        state.record_slots(observation)

        self.assertEqual(state.active_by_kind("type").value, "сварочный аппарат")
        self.assertTrue(any("дом" in value for value in state.active_values("use_case")))
        self.assertFalse(state.active_values("feature"))

        strategy = build_retrieval_strategy(state, "Для дома", currency="UAH")
        self.assertIsNotNone(strategy)
        self.assertEqual(strategy.product_type, "сварочный аппарат")
        self.assertIn("дом", strategy.request.normalized_query)
        self.assertFalse(strategy.request.required_features)


    def test_explicit_use_case_correction_replaces_only_prior_use_case(self):
        state = ConversationEngineState(user_key="use-case-correction")
        first = "Сварочный аппарат"
        observation = state.observe(first)
        state.record_slots(observation)
        state.record_product_type(_product_type_of(first))

        observation = state.observe("Для дома")
        state.record_slots(observation)
        self.assertEqual(state.active_values("use_case"), ["дома"])

        correction = "На самом деле для гаража"
        observation = state.observe(correction)
        self.assertEqual(observation["intent"], MessageIntent.CORRECTION)
        state.record_slots(observation)

        self.assertEqual(state.active_by_kind("type").value, "сварочный аппарат")
        self.assertEqual(state.active_values("use_case"), ["гаража"])


if __name__ == "__main__":
    unittest.main()
