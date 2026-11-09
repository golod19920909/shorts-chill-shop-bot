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
