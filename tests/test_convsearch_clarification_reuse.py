"""The conversation interpreter must not be called twice for one clarification."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from services.convsearch.engine import ConversationSearchEngine, DEFAULT_CLARIFY_QUESTION


class ClarificationReuseTests(unittest.TestCase):
    def test_reuses_question_from_conversation_turn_without_chat_action_call(self):
        engine = ConversationSearchEngine()
        ai = SimpleNamespace(
            decide_chat_action=AsyncMock(
                return_value={"action": "clarify", "question": "A second model call"}
            )
        )
        engine.bind_ai_service(ai)

        question = engine._clarification_question({
            "action": "clarify",
            "question": "Для какой задачи нужен компрессор?",
        })

        self.assertEqual(question, "Для какой задачи нужен компрессор?")
        ai.decide_chat_action.assert_not_awaited()

    def test_invalid_or_missing_interpreter_question_uses_deterministic_default(self):
        self.assertEqual(ConversationSearchEngine._clarification_question(None), DEFAULT_CLARIFY_QUESTION)
        self.assertEqual(
            ConversationSearchEngine._clarification_question({
                "action": "new_search",
                "question": "Should not be used",
            }),
            DEFAULT_CLARIFY_QUESTION,
        )
        self.assertEqual(
            ConversationSearchEngine._clarification_question({
                "action": "clarify",
                "question": "   ",
            }),
            DEFAULT_CLARIFY_QUESTION,
        )

    def test_interpreter_question_is_bounded(self):
        question = "x" * 900
        result = ConversationSearchEngine._clarification_question({
            "action": "clarify",
            "question": question,
        })
        self.assertEqual(len(result), 500)


if __name__ == "__main__":
    unittest.main()
