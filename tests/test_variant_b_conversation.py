import pytest

from services import chat_router
from handlers import search as search_handlers
from services.chat_router import ChatAction


def test_greeting_returns_greeting_action():
    for text in ("привет", "hello", "кто ты"):
        decision = chat_router.classify(text, has_conversation=False, has_shown_results=False)
        assert decision.action == ChatAction.GREETING


def test_ordinary_product_request_still_searches():
    decision = chat_router.classify("нужна зарядка для ноутбука 60 ват", has_conversation=False, has_shown_results=False)
    assert decision.action == ChatAction.SEARCH


def test_welcome_constant_exists():
    assert "Привет! Я умный помощник Shorts Chill." in search_handlers.WELCOME_SMART_SEARCH


def test_advice_followup_when_shown_is_answer():
    decision = chat_router.classify("А что ты посоветуешь?", has_conversation=True, has_shown_results=True)
    assert decision.action == ChatAction.ANSWER
