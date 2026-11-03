"""Pending Telegram clarification lifecycle regressions."""
from __future__ import annotations

import time
import unittest
from types import SimpleNamespace

from handlers.search import (
    CLARIFY_TTL_SECONDS,
    PENDING_CLARIFY_KEY,
    consume_pending_clarify,
    get_pending_clarify,
    set_pending_clarify,
    _clear_stale_shown_session_for_new_search,
    _get_convsearch_state,
)
from services.convsearch.contract import Outcome
from services.convsearch.state import ConversationEngineState


class PendingClarificationLifecycleTests(unittest.TestCase):
    def test_expired_conversation_ledger_is_replaced_before_touch(self):
        old = ConversationEngineState(user_key="old-ledger")
        old.record_product_type("наушники")
        old.updated_at = time.monotonic() - 30 * 60 - 1
        context = SimpleNamespace(user_data={"_convsearch_state": old})

        current = _get_convsearch_state(context, "telegram-user")

        self.assertIsNot(current, old)
        self.assertIsNone(current.active_by_kind("type"))
        self.assertEqual(current.user_key, "telegram-user")
        self.assertIs(context.user_data["_convsearch_state"], current)

    def test_active_conversation_ledger_is_preserved_and_touched(self):
        current = ConversationEngineState(user_key="telegram-user")
        current.record_product_type("наушники")
        context = SimpleNamespace(user_data={"_convsearch_state": current})

        result = _get_convsearch_state(context, "telegram-user")

        self.assertIs(result, current)
        self.assertEqual(result.active_by_kind("type").value, "наушники")
        self.assertFalse(result.is_expired())

    def test_new_product_search_invalidates_previous_shown_session_pointer(self):
        context = SimpleNamespace(user_data={"last_shown_session": "old-session"})
        decision = SimpleNamespace(report={"intent": "new_search"})

        _clear_stale_shown_session_for_new_search(context, decision)

        self.assertNotIn("last_shown_session", context.user_data)

    def test_refinement_keeps_previous_shown_session_pointer(self):
        context = SimpleNamespace(user_data={"last_shown_session": "current-session"})
        decision = SimpleNamespace(report={"intent": "refine"})

        _clear_stale_shown_session_for_new_search(context, decision)

        self.assertEqual(context.user_data["last_shown_session"], "current-session")

    def test_provider_failure_keeps_pending_question_for_retry(self):
        state = ConversationEngineState(user_key="provider-error-pending")
        state.last_outcome = Outcome.CLARIFY.value
        state.last_question = "Для какой задачи нужен товар?"
        context = SimpleNamespace(user_data={"_convsearch_state": state})
        set_pending_clarify(context, "Для какой задачи нужен товар?", "Что-нибудь полезное")

        consumed = consume_pending_clarify(context, Outcome.PROVIDER_ERROR)

        self.assertFalse(consumed)
        self.assertEqual(state.last_outcome, Outcome.CLARIFY.value)
        self.assertEqual(state.last_question, "Для какой задачи нужен товар?")
        self.assertEqual(
            get_pending_clarify(context)["question"],
            "Для какой задачи нужен товар?",
        )

    def test_completed_search_consumes_pending_question(self):
        state = ConversationEngineState(user_key="completed-pending")
        state.last_outcome = Outcome.CLARIFY.value
        state.last_question = "Для какой задачи нужен товар?"
        context = SimpleNamespace(user_data={"_convsearch_state": state})
        set_pending_clarify(context, "Для какой задачи нужен товар?", "Что-нибудь полезное")

        consumed = consume_pending_clarify(context, Outcome.RESULTS)

        self.assertTrue(consumed)
        self.assertIsNone(state.last_outcome)
        self.assertIsNone(state.last_question)
        self.assertIsNone(get_pending_clarify(context))
        self.assertNotIn(PENDING_CLARIFY_KEY, context.user_data)

    def test_invalid_pending_timestamp_also_clears_ledger_question(self):
        state = ConversationEngineState(user_key="invalid-clarification")
        state.last_outcome = Outcome.CLARIFY.value
        state.last_question = "Для какой задачи нужен товар?"
        context = SimpleNamespace(
            user_data={
                "_convsearch_state": state,
                PENDING_CLARIFY_KEY: {
                    "question": state.last_question,
                    "original": "Что-нибудь полезное",
                    "asked_at": "not-a-timestamp",
                },
            }
        )

        self.assertIsNone(get_pending_clarify(context))
        self.assertIsNone(state.last_question)
        self.assertIsNone(state.last_outcome)
        self.assertNotIn(PENDING_CLARIFY_KEY, context.user_data)

    def test_expired_pending_question_also_clears_ledger_question(self):
        state = ConversationEngineState(user_key="expired-clarification")
        state.last_outcome = Outcome.CLARIFY.value
        state.last_question = "Для какой задачи нужен товар?"
        context = SimpleNamespace(
            user_data={
                "_convsearch_state": state,
                PENDING_CLARIFY_KEY: {
                    "question": state.last_question,
                    "original": "Что-нибудь полезное",
                    "asked_at": time.monotonic() - CLARIFY_TTL_SECONDS - 1,
                },
            }
        )

        self.assertIsNone(get_pending_clarify(context))
        self.assertIsNone(state.last_question)
        self.assertIsNone(state.last_outcome)
        self.assertNotIn(PENDING_CLARIFY_KEY, context.user_data)


if __name__ == "__main__":
    unittest.main()
