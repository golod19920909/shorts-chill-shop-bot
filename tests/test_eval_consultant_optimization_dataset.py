"""Contract tests for the expanded blind model-comparison corpus."""

import unittest

from tests.eval_consultant_auxiliary_dataset import CHAT_ACTION_CASES, NO_RESULTS_CASES
from tests.eval_consultant_dialogue_dataset import DIALOGUE_CASES
from tests.eval_consultant_optimization_dataset import OPTIMIZATION_CASES


class ConsultantOptimizationDatasetTests(unittest.TestCase):
    def test_corpus_has_one_hundred_unique_scenarios(self):
        conversation = [*DIALOGUE_CASES, *OPTIMIZATION_CASES]
        total = len(conversation) + len(CHAT_ACTION_CASES) + len(NO_RESULTS_CASES)
        self.assertEqual(len(OPTIMIZATION_CASES), 61)
        self.assertEqual(total, 100)

    def test_optimization_cases_have_unique_ids_and_all_languages(self):
        ids = [case["id"] for case in OPTIMIZATION_CASES]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual({case["language"] for case in OPTIMIZATION_CASES}, {"ru", "uk", "en"})

    def test_every_added_case_is_a_grounded_single_turn_search(self):
        for case in OPTIMIZATION_CASES:
            self.assertEqual(len(case["turns"]), 1, case["id"])
            turn = case["turns"][0]
            expected = turn["expected"]
            self.assertEqual(expected["action"], "new_search", case["id"])
            self.assertTrue(expected["product_type"], case["id"])
            self.assertIs(expected["retrieval"], True, case["id"])
            self.assertEqual(expected["slot_updates"], [], case["id"])
            self.assertEqual(turn["context"]["recent_dialogue"], [], case["id"])


if __name__ == "__main__":
    unittest.main()
