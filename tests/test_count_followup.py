"""Tests for the count-only shortcut used by the Telegram search handler."""

from __future__ import annotations

import unittest

from services.count_followup import is_count_only_follow_up


class CountOnlyFollowUpTests(unittest.TestCase):
    def test_count_only_phrases_are_recognized(self):
        cases = {
            "покажи 5": 5,
            "покажи один вариант": 1,
            "show one more": 1,
            "покажи три варианта": 3,
            "и ещё 4": 4,
            "покажи ещё пару вариантов": 2,
            "show 3 more": 3,
            "покажи 3 новых": 3,
            "покажи 3 других": 3,
        }
        for phrase, expected in cases.items():
            with self.subTest(phrase=phrase):
                self.assertEqual(is_count_only_follow_up(phrase), expected)

    def test_product_criteria_are_not_swallowed_by_count_shortcut(self):
        phrases = (
            "покажи 3 самых дешёвых",
            "покажи 3 в чёрном цвете",
            "покажи 3 с хорошим микрофоном",
            "покажи 3 до 1000 грн",
            "покажи 5 беспроводных наушников",
        )
        for phrase in phrases:
            with self.subTest(phrase=phrase):
                self.assertIsNone(is_count_only_follow_up(phrase))


if __name__ == "__main__":
    unittest.main()
