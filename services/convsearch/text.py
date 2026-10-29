"""Text normalisation for the conversational search engine.

Adapted from ``Elinengu/anything-conversational-search`` (``src/text.py``):
the reference's *tokenisation contract* is kept — lowercase content tokens in
first-seen order, duplicates removed, boilerplate dropped — but the
vocabularies are rebuilt for the bot's real input: mixed Russian / English /
Ukrainian marketplace queries, not the reference's English-only Amazon
catalogue. The reference's ``SCAFFOLDING``/``CARE_PHRASES`` lists describe
Amazon metadata fields and are deliberately NOT carried over (the bot's
provider is AliExpress and its diagnostics already own relevance filtering).
"""

from __future__ import annotations

import re

# Tokens are unicode letter runs so Cyrillic survives (the reference's
# ``[a-z0-9]+`` would silently drop every Russian query word).
TOKEN_RE = re.compile(r"[^\W\d_][^\W_]*|\d+", re.UNICODE)

# Conversational filler. Reference STOPWORDS concept, rebuilt for RU/EN.
STOPWORDS: frozenset[str] = frozenset(
    {
        # RU
        "и", "а", "но", "или", "для", "с", "со", "в", "на", "по", "за", "из",
        "до", "от", "о", "об", "при", "что", "чтобы", "как", "это", "эта",
        "тот", "там", "мне", "меня", "я", "ты", "вы", "он", "она", "они",
        "нужен", "нужна", "нужно", "нужны", "хочу", "хотел", "хотела",
        "ищу", "искать", "найти", "найди", "покажи", "показать", "подбери",
        "посоветуй", "купить", "есть", "был", "была", "будет", "может",
        "пожалуйста", "спасибо", "только", "тоже", "также", "еще", "ещё",
        "тогда", "когда", "где", "какой", "какая", "какие", "который",
        "вариант", "варианта", "вариантов", "варианты", "товар", "товара",
        "товары", "штук", "штуки", "получше", "похуже", "подешевле",
        "подороже", "лучше", "хуже", "очень", "просто", "такой", "такая",
        # EN (kept from the reference's spirit)
        "a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from",
        "i", "in", "is", "it", "me", "my", "of", "on", "or", "please", "some",
        "that", "the", "this", "to", "want", "with", "would", "you", "have",
        "has", "not", "no", "do", "am", "need", "looking", "looking for",
        "show", "find", "give", "get", "one", "ones",
    }
)


def tokens(text: str) -> list[str]:
    """Lowercase content tokens, first-seen order, duplicates removed.

    Mirrors the reference ``terms()`` contract; the minimum length is 2 like
    the reference (Russian function words are mostly 1-2 chars and live in
    the stopword list).
    """

    out: list[str] = []
    seen: set[str] = set()
    for token in TOKEN_RE.findall(text or ""):
        lowered = token.lower().replace("ё", "е")
        if len(lowered) < 2 or lowered in STOPWORDS or lowered in seen:
            continue
        seen.add(lowered)
        out.append(lowered)
    return out


def join_tokens(text: str) -> str:
    """Token-joined normalised text for substring evidence checks.

    Same purpose as the reference's ``product["text"]`` blob: punctuation
    differences ("20000 мАч" vs "20000mAh") must not hide a literal match.
    """

    return " ".join(tokens(text))


#: Fragment separators, mirroring the reference ``constraint_spans``: a
#: constraint never crosses a clause boundary.
_CLAUSE_SPLIT_RE = re.compile(r"[.;:,\n!?]| - ")


def constraint_spans(text: str, min_words: int = 2) -> list[str]:
    """Normalised multi-word fragments of one message, newest-first caller.

    Adapted from the reference: strip a stopword run off BOTH ends of each
    chunk (their measured fix for carrier framing glued to the value), keep
    2-25 token fragments, drop empties and duplicates.
    """

    out: list[str] = []
    seen: set[str] = set()
    for chunk in _CLAUSE_SPLIT_RE.split(text or ""):
        chunk_tokens = [
            t.lower().replace("ё", "е") for t in TOKEN_RE.findall(chunk)
        ]
        while chunk_tokens and chunk_tokens[0] in STOPWORDS:
            chunk_tokens.pop(0)
        while chunk_tokens and chunk_tokens[-1] in STOPWORDS:
            chunk_tokens.pop()
        span = " ".join(chunk_tokens)
        if not span or span in seen:
            continue
        if len(chunk_tokens) < min_words or len(chunk_tokens) > 25:
            continue
        seen.add(span)
        out.append(span)
    return out
