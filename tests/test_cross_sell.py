from types import SimpleNamespace

from handlers.cross_sell import (
    PENDING_OFFER_KEY,
    PENDING_SEARCH_KEY,
    clear_offer,
    consume_search_request,
    remember_offer,
)


class Context:
    def __init__(self):
        self.user_data = {}


def test_offer_is_remembered_with_a_bounded_plain_title():
    context = Context()
    remember_offer(context, SimpleNamespace(title="  Useful   accessory  "))
    assert context.user_data[PENDING_OFFER_KEY] == {"title": "Useful accessory"}


def test_opt_in_search_marker_is_consumed_exactly_once():
    context = Context()
    context.user_data[PENDING_OFFER_KEY] = {"title": "Product"}
    context.user_data[PENDING_SEARCH_KEY] = True

    assert consume_search_request(context) is True
    assert PENDING_SEARCH_KEY not in context.user_data
    assert PENDING_OFFER_KEY not in context.user_data
    assert consume_search_request(context) is False


def test_decline_or_cleanup_removes_all_pending_cross_sell_state():
    context = Context()
    context.user_data[PENDING_OFFER_KEY] = {"title": "Product"}
    context.user_data[PENDING_SEARCH_KEY] = True

    clear_offer(context)

    assert PENDING_OFFER_KEY not in context.user_data
    assert PENDING_SEARCH_KEY not in context.user_data


def test_ordinary_message_without_opt_in_is_not_intercepted():
    context = Context()
    context.user_data[PENDING_OFFER_KEY] = {"title": "Product"}

    assert consume_search_request(context) is False
    assert context.user_data[PENDING_OFFER_KEY] == {"title": "Product"}


import pytest
from unittest.mock import AsyncMock

from handlers.cross_sell import callback


@pytest.mark.asyncio
async def test_accepting_offer_asks_user_what_to_find_and_sets_one_shot_search():
    context = Context()
    context.user_data[PENDING_OFFER_KEY] = {"title": "Product"}
    message = SimpleNamespace(reply_text=AsyncMock())
    query = SimpleNamespace(
        data="xsell:yes",
        answer=AsyncMock(),
        message=message,
    )
    update = SimpleNamespace(callback_query=query)

    await callback(update, context)

    assert context.user_data[PENDING_SEARCH_KEY] is True
    assert PENDING_OFFER_KEY not in context.user_data
    message.reply_text.assert_awaited_once()
    assert "Напишите, что именно подобрать" in message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_declining_offer_clears_state_without_starting_search():
    context = Context()
    context.user_data[PENDING_OFFER_KEY] = {"title": "Product"}
    message = SimpleNamespace(reply_text=AsyncMock())
    query = SimpleNamespace(
        data="xsell:no",
        answer=AsyncMock(),
        message=message,
    )
    update = SimpleNamespace(callback_query=query)

    await callback(update, context)

    assert PENDING_OFFER_KEY not in context.user_data
    assert PENDING_SEARCH_KEY not in context.user_data
    message.reply_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_stale_offer_button_does_not_start_search():
    context = Context()
    query = SimpleNamespace(
        data="xsell:yes",
        answer=AsyncMock(),
        message=SimpleNamespace(reply_text=AsyncMock()),
    )
    update = SimpleNamespace(callback_query=query)

    await callback(update, context)

    assert PENDING_SEARCH_KEY not in context.user_data
    query.answer.assert_awaited_once()
    assert query.answer.await_args.kwargs.get("show_alert") is True
