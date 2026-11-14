"""Regression tests for compact Gemini conversation context construction."""

from __future__ import annotations

import unittest

from services.ai_service import _bounded_conversation_context


class BoundedConversationContextTests(unittest.TestCase):
    def test_user_turn_already_in_dialogue_is_not_sent_twice(self) -> None:
        context = _bounded_conversation_context(
            {
                "recent_turns": ["нужны наушники", "до 30 долларов"],
                "recent_dialogue": [
                    {"role": "user", "text": "нужны наушники"},
                    {"role": "assistant", "text": "Для чего они нужны?"},
                    {"role": "user", "text": "до 30 долларов"},
                ],
            }
        )

        self.assertEqual(context["recent_turns"], [])
        self.assertEqual(
            [item["text"] for item in context["recent_dialogue"]],
            ["нужны наушники", "Для чего они нужны?", "до 30 долларов"],
        )

    def test_user_turn_outside_dialogue_window_is_retained(self) -> None:
        context = _bounded_conversation_context(
            {
                "recent_turns": ["старое важное уточнение", "нужны наушники"],
                "recent_dialogue": [
                    {"role": "user", "text": "нужны наушники"},
                ],
            }
        )

        self.assertEqual(context["recent_turns"], ["старое важное уточнение"])

    def test_user_only_history_is_retained_when_dialogue_is_empty(self) -> None:
        context = _bounded_conversation_context(
            {"recent_turns": ["нужны наушники"], "recent_dialogue": []}
        )

        self.assertEqual(context["recent_turns"], ["нужны наушники"])

    def test_different_long_user_turn_with_same_prefix_is_retained(self) -> None:
        prefix = "x" * 180
        context = _bounded_conversation_context(
            {
                "recent_turns": [prefix + " older detail"],
                "recent_dialogue": [
                    {"role": "user", "text": prefix + " newer detail"},
                ],
            }
        )

        self.assertEqual(context["recent_turns"], [(prefix + " older detail")[:180]])

    def test_assistant_text_does_not_remove_similar_user_turn(self) -> None:
        context = _bounded_conversation_context(
            {
                "recent_turns": ["нужны наушники"],
                "recent_dialogue": [
                    {"role": "assistant", "text": "нужны наушники"},
                ],
            }
        )

        self.assertEqual(context["recent_turns"], ["нужны наушники"])


if __name__ == "__main__":
    unittest.main()
