"""Small deterministic parser for count-only follow-ups to shown results.

This helper is deliberately separate from the direct-parser merge resolver.
It only recognizes messages that change the number of already
retrieved products to display; any product, feature, color or budget criterion
must continue through the conversational search engine.
"""

from __future__ import annotations

import re

from services.query_intent import CURRENCY_ALIASES
from services.text_matching import extract_requested_result_count

_CURRENCY_WORDS: frozenset[str] = frozenset(
    {key.casefold() for key in CURRENCY_ALIASES}
    | {value.casefold() for value in CURRENCY_ALIASES.values()}
)

_COUNT_ONLY_WORDS: frozenset[str] = frozenset(
    {
        "покажи", "покажите", "дай", "дайте", "найди", "найдите", "найти",
        "подбери", "подберите", "выведи", "сделай", "сделайте",
        "ещё", "еще", "и", "а", "мне",
        "больше", "шт", "штук", "штуки", "штука", "пожалуйста",
        "show", "give", "find", "more", "me", "please", "another",
        "other", "again", "of", "them", "these", "those", "new", "fresh",
        "новый", "новая", "новое", "новые", "нового", "новую", "новых", "новыми",
        "другой", "другая", "другое", "другие", "другого", "другую", "других", "другими",
        "вариант", "варианта", "вариантов", "варианты", "вариантом",
        "опция", "опции", "опций", "опцию", "товар", "товара", "товаров",
        "товары", "option", "options", "variant", "variants", "item", "items",
    }
)

_COUNT_ONLY_NUMBER_WORDS: dict[str, int] = {
    "один": 1, "одна": 1, "одно": 1, "одну": 1, "одного": 1, "одной": 1,
    "одним": 1, "one": 1,
    "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5,
    "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10,
}

_COUNT_NOUNS: frozenset[str] = frozenset(
    {
        "вариант", "варианта", "вариантов", "варианты", "опция", "опции",
        "опций", "опцию", "option", "options", "variant", "variants", "item",
        "items",
    }
)

_COUNT_VERBS: frozenset[str] = frozenset(
    {"покажи", "покажите", "дай", "дайте", "найди", "найдите", "подбери",
     "подберите", "выведи", "show", "find", "give"}
)

_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def _has_count_context(words: list[str]) -> bool:
    """Whether words frame a bare number as an option count."""

    return any(word in _COUNT_NOUNS for word in words) or any(
        word in _COUNT_VERBS for word in words
    )


def _is_count_framing_word(word: str) -> bool:
    """Whether a word only frames a requested result count."""

    lowered = word.lower().replace("ё", "е")
    if len(lowered) < 2:
        return True
    if lowered in _COUNT_ONLY_WORDS or lowered in _CURRENCY_WORDS:
        return True
    if lowered in _COUNT_ONLY_NUMBER_WORDS or lowered.startswith("пар"):
        return True
    return False


def is_count_only_follow_up(text: str) -> int | None:
    """Return the requested number of options, or None for any real criterion.

    Examples accepted: «Покажи 5», «Покажи три варианта», «и ещё 4»,
    «покажи ещё пару вариантов», and «show three more».

    Product or filter words are intentionally not accepted as count framing:
    «покажи 3 дешёвых», «покажи 3 в чёрном цвете» and «покажи 3 с микрофоном»
    must reach the conversational search engine.
    """

    if not text or not text.strip():
        return None
    words = [word.lower().replace("ё", "е") for word in _WORD_RE.findall(text)]
    if not words:
        return None
    # A second number usually introduces a separate budget, quantity or
    # technical specification («покажи 3 до 1000 грн»), not a card-count
    # change. Never swallow that additional constraint.
    if len(re.findall(r"\d+", text)) > 1:
        return None
    has_digit = any(char.isdigit() for char in text)
    if (
        not any(word in _COUNT_ONLY_NUMBER_WORDS for word in words)
        and not any(word.startswith("пар") for word in words)
        and not (has_digit and _has_count_context(words))
        and not (has_digit and any(word in _COUNT_ONLY_WORDS for word in words))
    ):
        return None
    leftover = [
        word for word in words
        if len(word) >= 3 and not _is_count_framing_word(word)
    ]
    if leftover:
        return None
    count = extract_requested_result_count(text)
    if count is not None:
        return count
    digits = re.search(r"\d+", text)
    if digits is not None:
        value = int(digits.group(0))
        if value >= 1:
            return min(value, 100)
    for word in words:
        if word in _COUNT_ONLY_NUMBER_WORDS:
            return _COUNT_ONLY_NUMBER_WORDS[word]
        if word.startswith("пар"):
            return 2
    return None
