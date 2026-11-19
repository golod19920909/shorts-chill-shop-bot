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
    _get_conversation_state_for_turn,
)
from services.convsearch.contract import Outcome
from services.convsearch.state import CONTEXT_TTL_SECONDS, ConversationEngineState


class PendingClarificationLifecycleTests(unittest.TestCase):
    def test_expired_shopping_ledger_is_replaced_before_routing(self):
        old_state = ConversationEngineState(user_key="expired-shopping")
        old_state.record_product_type("шуруповерт")
        old_state.updated_at = time.monotonic() - CONTEXT_TTL_SECONDS - 1
        context = SimpleNamespace(user_data={"_convsearch_state": old_state})

        current = _get_conversation_state_for_turn(context, "expired-shopping")

        self.assertIsNot(current, old_state)
        self.assertEqual(current.active(), [])
        self.assertIs(context.user_data["_convsearch_state"], current)

    def test_getting_shopping_state_does_not_refresh_idle_ttl(self):
        state = ConversationEngineState(user_key="idle-shopping")
        state.record_product_type("шуруповерт")
        state.updated_at = time.monotonic() - 10
        previous_updated_at = state.updated_at
        context = SimpleNamespace(user_data={"_convsearch_state": state})

        current = _get_conversation_state_for_turn(context, "idle-shopping")

        self.assertIs(current, state)
        self.assertEqual(current.updated_at, previous_updated_at)

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
