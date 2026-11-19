from __future__ import annotations

import unittest

from services.convsearch.state import ConversationEngineState, MessageIntent


class BareProductTypeSwitchTests(unittest.TestCase):
    def make_state(self) -> ConversationEngineState:
        state = ConversationEngineState(user_key="bare-switch-test")
        state.observe("наушники")
        state.record_product_type("наушники")
        return state

    def test_bare_different_catalog_type_starts_a_new_search(self):
        state = self.make_state()

        observation = state.observe("шуруповерт")

        self.assertEqual(observation["intent"], MessageIntent.NEW_SEARCH)

    def test_product_type_mentioned_as_compatibility_target_is_not_a_switch(self):
        state = self.make_state()

        observation = state.observe("для шуруповерта")

        self.assertNotEqual(observation["intent"], MessageIntent.NEW_SEARCH)

    def test_conjunction_before_a_new_product_still_switches(self):
        state = self.make_state()

        observation = state.observe("а шуруповерт")

        self.assertEqual(observation["intent"], MessageIntent.NEW_SEARCH)

    def test_new_product_after_ordinary_verb_still_switches(self):
        state = self.make_state()

        observation = state.observe("есть видеорегистратор")

        self.assertEqual(observation["intent"], MessageIntent.NEW_SEARCH)

    def test_feature_clause_does_not_switch_to_an_attribute_category(self):
        state = self.make_state()

        observation = state.observe("и чтобы была быстрая зарядка")

        self.assertNotEqual(observation["intent"], MessageIntent.NEW_SEARCH)

    def test_same_product_type_with_a_new_feature_remains_a_refinement(self):
        state = self.make_state()

        observation = state.observe("беспроводные наушники")

        self.assertEqual(observation["intent"], MessageIntent.REFINE)


if __name__ == "__main__":
    unittest.main()
