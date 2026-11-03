"""Pending Telegram clarification lifecycle regressions."""
from __future__ import annotations

import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


from handlers.search import (
    CLARIFY_TTL_SECONDS,
    PENDING_CLARIFY_KEY,
    consume_pending_clarify,
    get_pending_clarify,
    set_pending_clarify,
    _clear_stale_shown_session_for_new_search,
    _get_convsearch_state,
    _record_browsed_conversation_product,
    _send_product_cards,
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


class ConversationalDeliveryLedgerTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_initial_card_send_does_not_mark_any_product_shown(self):
        product = SimpleNamespace(product_id="product-1")
        context = SimpleNamespace(user_data={})
        outcome = SimpleNamespace(products=[product])
        with (
            patch("handlers.search.common.get_outcome_meta", return_value={"header": None}),
            patch("handlers.search.common.is_product_tracked", new=AsyncMock(return_value=False)),
            patch("handlers.search.common.send_product_card", new=AsyncMock(side_effect=RuntimeError("Telegram unavailable"))),
            patch("handlers.search.common.record_event", new=AsyncMock()) as record_event,
            patch("handlers.search.common.remember_shown", new=AsyncMock()) as remember_shown,
            patch("handlers.search.common.remember_views", new=AsyncMock()) as remember_views,
            patch("handlers.search.common.session_flow", return_value="search"),
            patch("handlers.search._card_caption", return_value="caption"),
            patch("handlers.search._browse_keyboard", return_value=None),
        ):
            delivered = await _send_product_cards(
                context, 123, outcome, "session-1"
            )

        self.assertFalse(delivered)
        record_event.assert_not_awaited()
        remember_shown.assert_not_awaited()
        remember_views.assert_not_awaited()

    async def test_successful_initial_card_send_is_acknowledged_by_caller(self):
        product = SimpleNamespace(product_id="product-1")
        context = SimpleNamespace(user_data={})
        outcome = SimpleNamespace(products=[product])
        with (
            patch("handlers.search.common.get_outcome_meta", return_value={"header": None}),
            patch("handlers.search.common.is_product_tracked", new=AsyncMock(return_value=False)),
            patch("handlers.search.common.send_product_card", new=AsyncMock()) as send_card,
            patch("handlers.search.common.record_event", new=AsyncMock()) as record_event,
            patch("handlers.search.common.remember_shown", new=AsyncMock()) as remember_shown,
            patch("handlers.search.common.remember_views", new=AsyncMock()) as remember_views,
            patch("handlers.search.common.session_flow", return_value="search"),
            patch("handlers.search._card_caption", return_value="caption"),
            patch("handlers.search._browse_keyboard", return_value=None),
        ):
            delivered = await _send_product_cards(
                context, 123, outcome, "session-1"
            )

        self.assertTrue(delivered)
        send_card.assert_awaited_once()
        record_event.assert_awaited_once()
        remember_shown.assert_awaited_once()
        remember_views.assert_awaited_once()

    async def test_browsed_product_is_recorded_only_for_current_search_session(self):
        state = ConversationEngineState(user_key="delivery-ledger")
        context = SimpleNamespace(
            user_data={
                "last_shown_session": "active-session",
                "_convsearch_state": state,
            }
        )

        await _record_browsed_conversation_product(
            context, "stale-session", "not-visible"
        )
        self.assertEqual(state.shown_product_ids, [])

        await _record_browsed_conversation_product(
            context, "active-session", "visible-after-edit"
        )
        self.assertEqual(state.shown_product_ids, ["visible-after-edit"])
