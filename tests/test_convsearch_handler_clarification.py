"""Pending Telegram clarification lifecycle regressions."""
from __future__ import annotations

import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


from handlers.search import (
    CLARIFY_TTL_SECONDS,
    PRODUCT_QUESTION_TTL_SECONDS,
    PENDING_CLARIFY_KEY,
    PENDING_PRODUCT_QUESTION_KEY,
    clear_pending_product_question,
    get_pending_product_question,
    set_pending_product_question,
    should_resume_product_question,
    reset_conversation,
    consume_pending_clarify,
    get_pending_clarify,
    set_pending_clarify,
    _clear_stale_shown_session_for_new_search,
    _answer_over_shown,
    _get_convsearch_state,
    _record_browsed_conversation_product,
    _send_product_cards,
    _no_eligible_user_message,
    SEMANTIC_SELECTION_UNAVAILABLE_TEXT,
    EMPTY_RESULT_TEXT,
    send_search_results,
)
from services.convsearch.contract import Outcome
from services.convsearch.state import ConversationEngineState


class PendingProductQuestionLifecycleTests(unittest.TestCase):
    def test_pending_question_round_trips_with_session_context(self):
        context = SimpleNamespace(user_data={})
        set_pending_product_question(context, "Какой вариант лучше?", "session-1")
        pending = get_pending_product_question(context)
        self.assertEqual(pending["question"], "Какой вариант лучше?")
        self.assertEqual(pending["session_id"], "session-1")

    def test_expired_pending_product_question_is_cleared(self):
        context = SimpleNamespace(user_data={
            PENDING_PRODUCT_QUESTION_KEY: {
                "question": "Какой вариант лучше?",
                "session_id": "session-1",
                "asked_at": time.monotonic() - PRODUCT_QUESTION_TTL_SECONDS - 1,
            }
        })
        self.assertIsNone(get_pending_product_question(context))
        self.assertNotIn(PENDING_PRODUCT_QUESTION_KEY, context.user_data)

    def test_invalid_pending_product_question_is_cleared(self):
        context = SimpleNamespace(user_data={
            PENDING_PRODUCT_QUESTION_KEY: {
                "question": "Какой вариант лучше?",
                "session_id": None,
                "asked_at": time.monotonic(),
            }
        })
        self.assertIsNone(get_pending_product_question(context))
        self.assertNotIn(PENDING_PRODUCT_QUESTION_KEY, context.user_data)

    def test_criterion_answer_resumes_comparison_but_new_task_does_not(self):
        state = ConversationEngineState(user_key="pending-product-question")
        state.record_product_type("наушники")
        self.assertTrue(should_resume_product_question("автономность", state))
        self.assertFalse(should_resume_product_question("Найди дрель", state))
        # A bare one-word reply is treated as the requested criterion; an
        # explicit new-search phrase above is how the user switches tasks.
        self.assertTrue(should_resume_product_question("дрель", state))
        self.assertFalse(should_resume_product_question("Покажи ещё варианты", state))

    def test_new_conversation_clears_pending_comparison_question(self):
        context = SimpleNamespace(user_data={
            "_convsearch_state": ConversationEngineState(user_key="reset"),
            PENDING_PRODUCT_QUESTION_KEY: {
                "question": "Какой вариант лучше?",
                "session_id": "session-1",
                "asked_at": time.monotonic(),
            },
            "last_shown_session": "session-1",
        })

        reset_conversation(context)

        self.assertNotIn(PENDING_PRODUCT_QUESTION_KEY, context.user_data)
        self.assertNotIn("_convsearch_state", context.user_data)
        self.assertNotIn("last_shown_session", context.user_data)

    def test_clear_pending_product_question_removes_only_its_key(self):
        context = SimpleNamespace(user_data={
            PENDING_PRODUCT_QUESTION_KEY: {"question": "Q", "session_id": "s", "asked_at": time.monotonic()},
            PENDING_CLARIFY_KEY: {"question": "Other", "original": "request", "asked_at": time.monotonic()},
        })
        clear_pending_product_question(context)
        self.assertNotIn(PENDING_PRODUCT_QUESTION_KEY, context.user_data)
        self.assertIn(PENDING_CLARIFY_KEY, context.user_data)


class PendingClarificationLifecycleTests(unittest.TestCase):
    def test_semantic_selection_failure_is_not_reported_as_no_catalogue_match(self):
        decision = SimpleNamespace(report={"ai_selection": "timeout_fallback"})
        self.assertEqual(
            _no_eligible_user_message(decision),
            SEMANTIC_SELECTION_UNAVAILABLE_TEXT,
        )

    def test_real_semantic_no_match_keeps_the_empty_result_message(self):
        decision = SimpleNamespace(report={"ai_selection": "semantic_no_match"})
        self.assertEqual(_no_eligible_user_message(decision), EMPTY_RESULT_TEXT)

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

    def test_natural_reply_does_not_consume_pending_question(self):
        state = ConversationEngineState(user_key="natural-reply-pending")
        state.last_outcome = Outcome.CLARIFY.value
        state.last_question = "Для какой задачи нужен товар?"
        context = SimpleNamespace(user_data={"_convsearch_state": state})
        set_pending_clarify(context, "Для какой задачи нужен товар?", "Компрессор")

        consumed = consume_pending_clarify(context, Outcome.RESPOND)

        self.assertFalse(consumed)
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


    async def test_product_answer_uses_only_cards_actually_seen_by_user(self):
        from models.product import Marketplace, Product

        products = [
            Product(
                marketplace=Marketplace.ALIEXPRESS,
                product_id=f"item-{i}",
                title=f"Товар {i}",
                product_url=f"https://www.aliexpress.com/item/item-{i}.html",
            )
            for i in range(1, 6)
        ]
        state = ConversationEngineState(user_key="visible-answer")
        state.record_shown(["item-1"])
        assistant = SimpleNamespace(
            matching_products=lambda question, candidates: [],
            answer=AsyncMock(return_value=SimpleNamespace(text="По открытой карточке данных недостаточно.")),
        )
        context = SimpleNamespace(
            user_data={"_convsearch_state": state},
            application=SimpleNamespace(bot_data={"product_assistant": assistant}),
            bot=SimpleNamespace(send_message=AsyncMock()),
        )
        source = SimpleNamespace(request=SimpleNamespace(original_query="товар"))

        with (
            patch("handlers.search.common.get_cached_products", return_value=products),
            patch("handlers.search.common.get_cached_outcome", return_value=source),
        ):
            answered = await _answer_over_shown(
                context, 123, "Какая у него мощность?", "active-session"
            )

        self.assertTrue(answered)
        passed_products = assistant.answer.await_args.args[1]
        self.assertEqual([p.product_id for p in passed_products], ["item-1"])
        context.bot.send_message.assert_awaited_once()


    async def test_ai_comparison_question_is_saved_for_the_users_next_turn(self):
        from models.product import Marketplace, Product

        product = Product(
            marketplace=Marketplace.ALIEXPRESS,
            product_id="item-1",
            title="Wireless device",
            product_url="https://www.aliexpress.com/item/item-1.html",
        )
        state = ConversationEngineState(user_key="comparison-clarify")
        state.record_shown(["item-1"])
        assistant = SimpleNamespace(
            matching_products=lambda question, candidates: [],
            answer=AsyncMock(return_value=SimpleNamespace(
                text="Что для вас важнее — цена или автономность?", used_ai=True
            )),
        )
        context = SimpleNamespace(
            user_data={"_convsearch_state": state},
            application=SimpleNamespace(bot_data={"product_assistant": assistant}),
            bot=SimpleNamespace(send_message=AsyncMock()),
        )
        source = SimpleNamespace(request=SimpleNamespace(original_query="device"))

        with (
            patch("handlers.search.common.get_cached_products", return_value=[product]),
            patch("handlers.search.common.get_cached_outcome", return_value=source),
        ):
            answered = await _answer_over_shown(
                context, 123, "Какой вариант лучше?", "active-session",
                conversation_state=state,
            )

        self.assertTrue(answered)
        pending = get_pending_product_question(context)
        self.assertEqual(pending["question"], "Какой вариант лучше?")
        self.assertEqual(pending["session_id"], "active-session")
        self.assertEqual(state.dialogue_history[-1]["role"], "assistant")

    async def test_undelivered_comparison_question_is_not_marked_pending(self):
        from models.product import Marketplace, Product

        product = Product(
            marketplace=Marketplace.ALIEXPRESS,
            product_id="item-1",
            title="Wireless device",
            product_url="https://www.aliexpress.com/item/item-1.html",
        )
        state = ConversationEngineState(user_key="failed-comparison-question")
        state.record_shown(["item-1"])
        assistant = SimpleNamespace(
            matching_products=lambda question, candidates: [],
            answer=AsyncMock(return_value=SimpleNamespace(
                text="Что для вас важнее — цена или автономность?", used_ai=True
            )),
        )
        context = SimpleNamespace(
            user_data={"_convsearch_state": state},
            application=SimpleNamespace(bot_data={"product_assistant": assistant}),
            bot=SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError("Telegram down"))),
        )
        source = SimpleNamespace(request=SimpleNamespace(original_query="device"))

        with (
            patch("handlers.search.common.get_cached_products", return_value=[product]),
            patch("handlers.search.common.get_cached_outcome", return_value=source),
            self.assertRaises(RuntimeError),
        ):
            await _answer_over_shown(
                context, 123, "Какой вариант лучше?", "active-session",
                conversation_state=state,
            )

        self.assertIsNone(get_pending_product_question(context))

    async def test_failed_optional_hint_keeps_successfully_delivered_session(self):
        product = SimpleNamespace(product_id="delivered-product")
        outcome = SimpleNamespace(
            products=[product],
            request=SimpleNamespace(
                mode=SimpleNamespace(value="text"),
                original_query="test query",
            ),
        )
        state = ConversationEngineState(user_key="hint-failure")
        context = SimpleNamespace(
            user_data={},
            bot=SimpleNamespace(
                send_message=AsyncMock(side_effect=RuntimeError("Telegram unavailable"))
            ),
        )
        with (
            patch("handlers.search.common.remember_outcome", return_value="session-delivered"),
            patch("handlers.search._log_unapplied_constraints"),
            patch("handlers.search.log_final_pool"),
            patch("handlers.search._send_product_cards", new=AsyncMock(return_value=True)),
            patch("handlers.search._result_summary_text", return_value="optional hint"),
        ):
            session_id = await send_search_results(
                context,
                123,
                outcome,
                pre_curated=True,
                conversation_state=state,
            )

        self.assertEqual(session_id, "session-delivered")
        self.assertEqual(state.shown_product_ids, ["delivered-product"])


if __name__ == "__main__":
    unittest.main()
