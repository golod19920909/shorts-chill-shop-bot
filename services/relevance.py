"""Deterministic relevance filtering of marketplace results.

Marketplace search answers are not curated: a feed can contain an item that
has nothing to do with what was asked (the classic example is a religious
cross inside a popular feed). This module removes only what can be proven to
be wrong, using nothing but real API data and the user's own request. No
model, no heuristic scoring, no invented keyword blocklist.

Rules, applied in order:

R1 - the product must carry a real absolute ``http(s)`` URL. Without it the
     card cannot lead anywhere, so it is not shown;
R2 - the title must contain at least two letters. A title that is only
     digits or punctuation is not product data;
R3 - when the request describes a product (a natural-language search or a
     catalogue category), the title must contain evidence of the request:
     at least one comparable word (per title writing system, prefix-4, as
     before) - or, when the title's own language finds none, CROSS-WRITING
     evidence: every request word written in ANOTHER script is compared
     again in a romanized space (the fixed character-level transliteration
     of :func:`latin_space`), so «небулайзер» can meet "nebulizer" and
     «портативный» can meet "portable" without any vocabulary or synonym
     list. Only when BOTH comparisons find nothing is the title rejected
     as ``no_query_term_in_title`` - the donut-toy junk a keyword answer
     returns for «небулайзер» genuinely shares nothing with the request;
     For CATEGORY mode only (G24.8 P1.2) a 3-letter DECLARED stem («сад»,
     «дом», «быт») additionally matches its word-initial inflected forms
     («садовый», «для сада», «бытовой»); SEARCH keeps whole-word matching
     for every short token, so G24.6 behaviour is untouched. Also for
     CATEGORY mode only (G24.8 P1.3) a keyword the catalogue ITSELF declares
     survives the stopword list («набор» of the tools category): that curated
     word is the category's own evidence, while SEARCH drops the whole
     :data:`STOPWORDS` list exactly as G24.6 left it;
R4 - price, rating and order constraints are NOT repeated here: they are
     enforced by ``services.comparison_service.filter_products``, which
     knows how to report a constraint that could not be applied (the
     AliExpress Affiliate API reports no 0-5 star rating);
R5 - product-type gate (SEARCH mode only) now RANKS as well as rejects:
     * the title LEADS with one of the request's product nouns
       (verdict ``"exact"``) - the verified product, tier 2;
     * the title merely MENTIONS the requested product while leading with
       something else (verdict ``"mentioned"``: «футляр для … зубной
       щетки», «воздуходувка … пылесос для клавиатуры», an accessory, a
       compatible part) is KEPT at tier 1 - a related product, an
       accessory or a same-use-case sibling is still useful to the
       shopper and the funnel must not silently throw it away (live
       audit 2026: 14 of 44 candidates of «небулайзер» and 32 of 93 of
       «мини пылесос для клавиатуры» were family/accessory results
       hard-dropped here). Every exact match still outranks them;
     * a title that matched only on ADJECTIVES/modifiers (verdict
       ``"noun_miss"``: «механический коврик» for «механическая
       клавиатура», «… LED-дисплеем» for «LED лампа», a "portable"
       hair-brush for "portable nebulizer") is still REJECTED as
       ``no_product_noun_in_title``: modifier-only overlap is the
       keyword-spam signature;
     * CLEARLY UNRELATED titles are rejected by R3 (no term, no
       cross-writing evidence);
     Categories keep R3 only: one category IS many product types, so the
     gate must not narrow it;
R6 - :func:`relevance_tier` turns the same verdict into the ranking tier
     (2 = verified product type, 1 = word/product-family evidence -
     including kept related/accessory results, 0 = no comparable
     evidence), so the sort puts product identity ABOVE popularity while
     related results stay reachable below it.

Why the comparison is per title and not "one language per request": a real
request carries BOTH languages - the marketplace phrase the parser produced
(English, e.g. "wireless headphones") and the user's own words plus catalogue
keywords (Russian). Judging such a request by a single dominant language made
the two scripts cancel each other out, which silently DISABLED the whole
filter and let unrelated items (kitchen scales, socks) reach the card feed.
Per-title comparison removes that hole while keeping the honest limitation
below.

One deliberate non-rule, which keeps legitimate products: when the request
has NO word in the title's language AND no romanized cross-writing match
either (an English-only query against Russian titles that share nothing
with it), the bot cannot compare the two languages and will not pretend it
can: the title is kept as ``unverified`` (tier 0, ranked below every
judged result) and counted in the logs by :func:`filter_relevant` - a
silent bypass is not a silent bypass if it is measured and ranked last.
Feeds that have no product query of their own (popular, image) pass
through R1/R2 only: they are ordered by real marketplace signals instead.

:func:`relevance_score` exposes the same comparison as a number, so the
ranking can put relevance ABOVE popularity without any model in the loop;
G25.2-B Stage 3 adds a bounded structured-intent adjustment to it (empty
intent keeps it exactly the 0..1 word-share of old - see
:func:`intent_score_adjustment`).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from urllib.parse import urlsplit

from models.product import Marketplace, Product
from models.search_request import SearchMode, SearchRequest

from services.query_intent import CURRENCY_ALIASES, KNOWN_BRANDS

# Stage 3G.1 (A): the relevance gate reuses the Stage-46 word-aware
# matcher's canonical form instead of teaching this module a second
# tokenizer. ``phrase_in_text`` is public API (numbers match exactly,
# units fold to one key: «65W» == «65 Вт» == «65 w»); ``_UNIT_CANON`` is
# the closed table of TRUE unit spellings it folds with, imported as-is so
# exactly ONE unit list exists in the project. ``text_matching`` imports
# nothing from services, so no cycle is possible.
from services.text_matching import _UNIT_CANON, _WORD_RE, phrase_in_text

logger = logging.getLogger(__name__)

#: Words shorter than this are too ambiguous to compare at all.
MIN_TERM_LENGTH = 3

#: Prefix length used for the word comparison (allows inflections).
PREFIX_MATCH_LENGTH = 4

#: Cyrillic vowels: the ONLY continuation a short CATEGORY stem may have
#: (G24.8 P1.2). Russian nominal inflections of such a stem always start
#: with a vowel («сада», «садовый», «дома», «домашний», «бытовой»), while
#: compound/borrowed heads continue with a consonant («домкрат») - which
#: must stay rejected. Deliberately Cyrillic-only: the rule lives in the
#: Cyrillic comparison space of Cyrillic category titles, so latin terms
#: and the romanized cross-script path keep their exact G24.6 behaviour.
_SHORT_STEM_VOWELS = "аоэеиыюяё"

#: Modes whose request really describes the wanted product.
TOKEN_FILTER_MODES = frozenset({SearchMode.SEARCH, SearchMode.CATEGORY})

#: The mode in which a candidate must be the REQUESTED PRODUCT (not merely a
#: word-sharing relative). Categories legitimately contain many product
#: types, so the product-type gate of :func:`classify_product` never runs on
#: them - they keep the word-level R3 filter only. Feeds without a product
#: query (popular/discovery/image) are outside ``TOKEN_FILTER_MODES`` and
#: keep the quality rules R1/R2 alone.
PRODUCT_TYPE_MODE = SearchMode.SEARCH

_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)

_SCRIPT_PATTERNS = {
    "cyrillic": re.compile(r"[а-яё]", re.IGNORECASE),
    "latin": re.compile(r"[a-z]", re.IGNORECASE),
}

#: Words that carry no product meaning. Removing them is what stops
#: "wireless headphones under 30 dollars" from being filtered on "under".
STOPWORDS = frozenset(
    {
        # -- English ----------------------------------------------------
        "about",
        "above",
        "and",
        "any",
        "below",
        "best",
        "buy",
        "cheap",
        "cheaper",
        "deal",
        "deals",
        "dollar",
        "dollars",
        "eur",
        "euro",
        "euros",
        "find",
        "for",
        "from",
        "good",
        "high",
        "item",
        "less",
        "look",
        "more",
        "new",
        "over",
        "piece",
        "pieces",
        "popular",
        "price",
        "product",
        "rating",
        "review",
        "reviews",
        "sale",
        "search",
        "show",
        "than",
        "the",
        "top",
        "under",
        "usd",
        "want",
        "with",
        "without",
        # Anaphora / scaffolding of a multi-turn follow-up («find ONE»,
        # «cheaper alternative ...»): they frame a constraint on the
        # previous product and never name a product of their own, so the
        # switch test must skip them exactly like other stopwords.
        "one",
        "ones",
        "two",
        "but",
        "no",
        "not",
        "more",
        "than",
        "while",
        "keeping",
        "keep",
        "original",
        "requirements",
        "requirement",
        "alternative",
        "alternatives",
        "option",
        "options",
        "variant",
        "variants",
        # -- Russian ----------------------------------------------------
        "без",
        "больше",
        "бы",
        "был",
        "быть",
        "валюта",
        "вещь",
        "где",
        "дешевле",
        "дешевый",
        "для",
        "доллар",
        "доллара",
        "долларов",
        "доллары",
        "дорого",
        "если",
        "еще",
        "ещё",
        "заказ",
        "заказа",
        "заказов",
        "заказы",
        "и",
        "или",
        "искать",
        "как",
        "какой",
        "качество",
        "который",
        "купить",
        "лучше",
        "лучший",
        "менее",
        "меньше",
        "мне",
        "модель",
        # Generic commercial filler for a user's sentence - yet the tools
        # category DECLARES it as a keyword, so CATEGORY mode keeps it
        # (G24.8 P1.3: :func:`_category_declared_stopwords`).
        "набор",
        "найти",
        "наличие",
        "недорого",
        "нет",
        "новый",
        "нужен",
        "нужно",
        "о",
        "об",
        "от",
        "отзыв",
        "отзывами",
        "отзывов",
        "отзывы",
        "очень",
        "под",
        "пожалуйста",
        "покупка",
        "популярное",
        "по",
        "предмет",
        "при",
        "рейтинг",
        "рейтинга",
        "рублей",
        "рубля",
        "самый",
        "с",
        "скидка",
        "скидки",
        "со",
        "совсем",
        "товар",
        "товара",
        "товары",
        "хорошие",
        "хорошими",
        "хороший",
        "хочу",
        "цена",
        "цене",
        "цены",
        "чем",
        "что",
        "чтобы",
        "шт",
        "штук",
        "штука",
        "штуки",
        "это",
        "этой",
        "этот",
    }
)


#: Currency markers known to the PARSERS. ``query_intent.CURRENCY_ALIASES``
#: (Stage 3D) is the single authority that already owns «грн», «usd», «$»,
#: «доллар», «евро» …, reused here so the project keeps exactly ONE currency
#: list. Stage 3G.1 (C): a currency word names a PRICE, never a product -
#: the audit found «грн» inside ``query_noun_terms``/``comparable_terms``
#: (it is in neither :data:`STOPWORDS` nor :data:`_QUERY_FUNCTION_WORDS`),
#: so a title that merely quoted a price could be rescued as
#: "mentioned"/"exact" for any query. Letter-only keys matter here: symbols
#: never survive :func:`tokenize` anyway, and a multi-word marker («us
#: dollar») is covered by its single-word keys.
_CURRENCY_MARKER_WORDS = frozenset(
    word for word in CURRENCY_ALIASES if word.isalpha()
)


def tokenize(text: str | None) -> list[str]:
    """Split text into lower-cased letter-only words (``ё`` folded to ``е``).

    The ``ё``/``е`` fold is what lets «щётка» and «щетка» compare as one
    word: AliExpress titles mix both spellings, and a relevance decision
    must never depend on which of them the marketplace happened to use.
    """

    if not text:
        return []
    return [
        match.group(0).lower().replace("ё", "е")
        for match in _WORD_RE.finditer(text)
    ]


def _category_declared_stopwords(request: SearchRequest) -> frozenset[str]:
    """G24.8 P1.3: catalogue words CATEGORY may use even though they stop-word.

    A category request is fully catalogue-authored, so its ``keywords`` are
    the CURATED words of ``config.catalog`` - never text the user typed. When
    such a word is also in :data:`STOPWORDS` the ordinary word-level filter
    would drop the category's own declared evidence before any comparison:
    «набор» is generic commercial filler (so it rightfully stop-words a user's
    sentence) yet it IS the product a tools category asks for, and a tool set
    whose only catalogue word is «набор» was rejected as
    ``no_query_term_in_title``.

    Narrow by construction: SEARCH and every other mode get the EMPTY set, so
    their stopword behaviour stays byte-identical to G24.6; only words
    TOKENIZED OUT of ``request.keywords`` are ever returned (never the user's
    sentence, ``original_query`` or ``normalized_query``); and only words
    already in :data:`STOPWORDS` need the exception at all. The authority
    stays ``config.catalog`` - this module hardcodes no product word.
    """

    if request.mode is not SearchMode.CATEGORY:
        return frozenset()
    return frozenset(
        word
        for keyword in request.keywords
        for word in tokenize(keyword)
        if word in STOPWORDS
    )


def query_terms(request: SearchRequest) -> tuple[str, ...]:
    """The request's own words, in order, without stopwords or duplicates.

    Three sources, because a real request speaks more than one language and
    every one of them is needed for an honest comparison:

    * ``normalized_query`` - the cleaned marketplace phrase the parser built
      (often English, because that is how a marketplace search index is
      written). It comes first, so the product words lead;
    * ``original_query`` - what the user actually typed. This is the only
      source guaranteed to be written in the USER's language, which is the
      language the marketplace answer comes back in
      (``target_language=RU``), so it is what makes the per-title comparison
      of :func:`rejection_reason` possible at all;
    * ``keywords`` - the parser's/catalogue's own words.

    Price/rating wording ("under 30 dollars", "рейтинг выше 4.5") is dropped
    by the stopword list and the minimum length, so it never becomes a filter
    term. A CATEGORY keyword the catalogue declares ITSELF is the one
    exception (G24.8 P1.3, :func:`_category_declared_stopwords`): «набор» is
    the tools category's own word even though it stop-words a user's sentence.
    """

    declared = _category_declared_stopwords(request)

    sources = [request.normalized_query or request.original_query]
    if request.original_query:
        sources.append(request.original_query)
    sources.extend(request.keywords)

    terms: list[str] = []
    for source in sources:
        for word in tokenize(source):
            if len(word) < MIN_TERM_LENGTH:
                continue
            if word in STOPWORDS and word not in declared:
                continue
            if word in _CURRENCY_MARKER_WORDS:
                # Stage 3G.1 (C): a currency word is a price unit, never a
                # comparison term («грн» in the query must not "match" a
                # title that quotes a price).
                continue
            if word not in terms:
                terms.append(word)
    return tuple(terms)


# -- Stage 1: deterministic query understanding (product-type semantics) ----
#
# Everything below derives ONLY from the user's own words and the candidate
# title - no model, no product dictionary, no blacklist of observed items.
# It answers the one question the raw word-match could not: IS THIS CANDIDATE
# THE REQUESTED PRODUCT, or does the title merely mention the requested
# product while leading with something else (a case, an ingredient, a
# compatible accessory)?

#: Function words of a QUERY that never name the product. Deliberately much
#: smaller than :data:`STOPWORDS`: «набор» is generic filler for a word-level
#: filter, yet it IS the product when the user asks for «набор для чистки
#: мебели», so the product-type stage must keep it.
_QUERY_FUNCTION_WORDS = frozenset(
    {
        # -- English ----------------------------------------------------
        "about", "above", "and", "any", "best", "buy", "cheap", "cheaper",
        "dollars", "dollar", "find", "for", "from", "good", "great", "high",
        "less", "look", "more", "need", "over", "piece", "pieces", "please",
        "price", "rating", "review", "reviews", "sale", "search", "show",
        "than", "the", "top", "under", "usd", "eur", "want", "with",
        "without",
        # Pronouns / anaphora / quantity scaffolding of a follow-up: they
        # never name a product («find ONE with two batteries», «cheaper
        # alternative while keeping requirements»).
        "one", "ones", "it", "its", "them", "they", "that", "this",
        "these", "those", "something", "anything", "another", "other",
        "alternative", "alternatives", "option", "options", "variant",
        "variants", "version", "versions", "kind", "kinds",
        "type", "types", "sort", "thing", "things", "stuff", "item", "items",
        "product", "products", "a", "an", "me", "my", "our", "us", "while",
        "keeping", "keep", "original", "requirements", "requirement",
        "expensive",
        # -- Russian ----------------------------------------------------
        "без", "больше", "бы", "был", "быть", "в", "валюта", "вид", "вида",
        "виде", "виды", "где", "дешевле",
        "дешевый", "для", "до", "доллар", "доллара", "долларов", "доллары",
        "дорого", "если", "еще", "ещё", "за", "и", "или", "искать", "как",
        "какой", "качество", "который", "купить", "лучше", "лучший",
        "лучшая", "менее", "меньше", "мне", "меня", "модель", "над",
        "надо", "найди", "найти", "на", "наличие", "недорого", "недорогая",
        "дешевая", "нет", "нибудь", "но", "новый", "нужен", "нужна", "нужно", "нужны",
        "об", "от",
        "отзыв", "отзывами", "отзывов", "отзывы", "очень", "по", "под",
        "пожалуйста", "покажи", "покупка", "популярное", "предмет",
        "при", "рейтинг", "рейтинга", "рублей", "рубля", "самый", "скидка",
        "скидки", "совсем", "товар", "товара", "товары", "хорошие",
        "хорошая", "хорошее", "хорошо",
        "хорошими", "хороший", "хочу", "цена", "цене", "цены", "чем", "что",
        "чтобы", "шт", "штук", "штука", "штуки", "это", "этой", "этот",
    }
)

#: Prefix-like modifiers that never name a product on their own: they are
#: skipped when the HEAD noun of a query or a title is looked for, so
#: «mini keyboard» still leads with «keyboard» and «мини клавиатура» with
#: «клавиатура». Generic word-shape knowledge, not a product list.
MODIFIER_TOKENS = frozenset(
    {
        "air", "hyper", "light", "max", "micro", "mini", "nano", "plus",
        "pro", "smart", "super", "ultra",
        "макси", "микро", "мини", "нано", "про", "супер", "ультра",
    }
)

#: Generic package/container nouns (RU + EN). A word of this class names the
#: FORM of an offer - a set, a bundle, a kit - never the product inside it, so
#: on its own it can never BE the requested product type (Stage 35). This is a
#: closed grammatical/word-shape class, exactly like :data:`MODIFIER_TOKENS`,
#: NOT a per-category product list: nothing here knows what any set contains.
_CONTAINER_RU_PREFIXES = ("набо", "компл")
_CONTAINER_EN_EXACT = frozenset({"set", "sets", "kit", "kits"})


def _is_generic_container(word: str | None) -> bool:
    """Whether ``word`` is a generic package/container noun («набор», «set»).

    Russian forms match by their inflectional stem («набор/набора/наборы/
    наборов», «комплект/комплекты»); English ones by a small closed set so a
    longer word («kitchen», «settle») is never mistaken for «kit»/«set».
    """
    if not word:
        return False
    folded = word.casefold().replace("ё", "е")
    if folded.startswith(_CONTAINER_RU_PREFIXES):
        return True
    return folded in _CONTAINER_EN_EXACT

#: Adjective endings common in titles (nominative, instrumental and short
#: forms). A word ending with one of these MODIFIES a product; it never IS
#: the product, so both the query head and the title head skip it
#: («механическая клавиатура» leads with «клавиатура», «wireless headphones»
#: with «headphones», «брезентовый»/«хорошим» are modifiers wherever they
#: appear - «полотно для лобзика, с хорошим рейтингом» must not turn the
#: instrumental «хорошим» into a product noun).
_ADJECTIVE_ENDINGS_RU = (
    "ый", "ий", "ой", "ая", "яя", "ое", "ее", "ые", "ие", "ого", "его",
    "ому", "ему", "ою", "ею", "ыми", "ими", "им",
)

_ADJECTIVE_ENDINGS_EN = (
    "al", "ic", "ous", "ive", "ful", "less", "able", "ible", "ent", "ant",
    "ish", "ary", "ory", "ical", "proof",
)


def _looks_like_adjective(word: str) -> bool:
    """Whether a word modifies a product instead of naming it."""

    if word in MODIFIER_TOKENS:
        return True
    script = dominant_script(word)
    if script == "cyrillic":
        return word.endswith(_ADJECTIVE_ENDINGS_RU)
    if script == "latin":
        return word.endswith(_ADJECTIVE_ENDINGS_EN)
    return False


def noun_terms(text: str | None, script: str) -> list[str]:
    """Product-noun candidates of ``text`` written in ``script``, in order.

    Drops function words, adjectives and modifiers, keeping only words that
    can HEAD a product phrase («щетка», «клавиатура», «чехол»). Used for
    both the query (Stage 1) and the candidate title (Stage 3).
    """

    nouns: list[str] = []
    for word in tokenize(text):
        if len(word) < MIN_TERM_LENGTH or script not in scripts_of(word):
            continue
        if word in _QUERY_FUNCTION_WORDS or _looks_like_adjective(word):
            continue
        if word in _CURRENCY_MARKER_WORDS:
            # Stage 3G.1 (C): «грн»/«usd»/«евро» name a price, never a
            # product - neither in a query (a noun the request is judged
            # by) nor in a title (a head noun that could earn "exact").
            continue
        if word not in nouns:
            nouns.append(word)
    return nouns


def query_noun_terms(request: SearchRequest, script: str) -> tuple[str, ...]:
    """The request's product nouns in one writing system (user words first).

    The user's own sentence leads so «электрическая зубная щетка» always
    yields «щетка» even when the marketplace phrase the parser produced is
    in another language; keywords and the normalized phrase then add what
    only they know (catalogue words, the English phrase for a Latin title).
    """

    sources = [
        request.original_query or "",
        *request.keywords,
        request.normalized_query or "",
    ]
    nouns: list[str] = []
    for source in sources:
        for word in noun_terms(source, script):
            if word not in nouns:
                nouns.append(word)
    return tuple(nouns)


def leading_noun(title: str | None, script: str) -> str | None:
    """The noun that HEADS this title in ``script``, or ``None``.

    Titles lead with the product («Футляр для электрической зубной щетки»
    leads with «футляр», «Электрическая зубная щетка» with «щетка»), so the
    first noun is the cheapest deterministic approximation of the title's
    product type. Words of the other script (latin brands inside a Russian
    title) are skipped by :func:`noun_terms`.
    """

    nouns = noun_terms(title, script)
    return nouns[0] if nouns else None


#: Bounded product-noun typo tolerance (G25.2-F). A LONG Cyrillic product
#: noun whose head the user mistyped by ONE character («поувербанк» for
#: «повербанк» - an insertion) must still verify the product: the live
#: audit (2026-10-25, «Нужен поувербанк мощный») showed every title sharing
#: the CORRECT noun kept ranking as tier-1 "mentioned" and no tier-2
#: product existed, so rental stations and feature-mentioning devices
#: BECAME the final answer. Narrow by construction:
#:   * words shorter than :data:`_TYPO_NOUN_MIN_LENGTH` never fuzzy-match
#:     (a short word is a grammatical token, not a product noun);
#:   * exactly ONE edit (insertion, deletion or substitution) is allowed;
#:   * both words must share the same writing system, so a cross-script
#:     pair keeps the romanized path it already has;
#:   * the comparison runs on the STEM: a bounded inflectional tail (up to
#:     :data:`_TYPO_NOUN_MAX_INFLECTION` trailing letters, the regular
#:     Russian nominal ending) is stripped from BOTH words before the
#:     one-edit decision, so «поувербанк» vs «повербанка» is judged as
#:     «поувербанк» vs «повербанк» (one insertion) and never punished for
#:     the ordinary ending;
#:   * the words must agree from the FIRST character over at least
#:     :data:`_TYPO_NOUN_MIN_AGREEMENT` characters (the edit may not sit
#:     at the very head), so a genuinely different product word never
#:     passes.
_TYPO_NOUN_MIN_LENGTH = 8
_TYPO_NOUN_MAX_INFLECTION = 3
_TYPO_NOUN_MIN_AGREEMENT = 2


def _one_edit_apart(left: str, right: str) -> bool:
    """Whether two words differ by EXACTLY one edit.

    Linear-time distance-1 Levenshtein decision: one insertion, deletion
    or substitution - never a transposition (that is two edits) and never
    two of anything.
    """

    if left == right:
        return False
    if abs(len(left) - len(right)) > 1:
        return False
    if len(left) > len(right):
        left, right = right, left
    i = j = edits = 0
    while i < len(left) and j < len(right):
        if left[i] == right[j]:
            i += 1
            j += 1
            continue
        edits += 1
        if edits > 1:
            return False
        if len(left) == len(right):
            i += 1
            j += 1
        else:
            j += 1
    if i < len(left) or j < len(right):
        edits += 1
    return edits == 1


def _noun_typo_match(left: str, right: str) -> bool:
    """Whether a LONG same-script noun pair differs by ONE stem typo.

    Fires only when both words are at least
    :data:`_TYPO_NOUN_MIN_LENGTH` letters, belong to the SAME writing
    system, and ONE bounded-stem alignment of the two words differs by
    exactly one edit while agreeing from the first character. The bounded
    inflectional tail (up to :data:`_TYPO_NOUN_MAX_INFLECTION` trailing
    letters) is stripped ADAPTIVELY from BOTH words (every combination of
    trailing cuts is tried), so an ordinary Russian ending («повербанка»,
    «повербанков») is never mistaken for part of the typo, and the edit
    must sit INSIDE the stem - a genuinely different product word never
    passes. A case the prefix rule already handles («повербанк» vs
    «повербанка») never reaches this helper (the bare prefixes match
    first), so it exists only for what prefix matching cannot reach: a
    mistyped STEM (an extra «у» inside «поувербанк»).
    """

    # Cyrillic pairs ONLY: the evidenced defect is a mistyped Cyrillic
    # product noun. A Latin pair must never fuzzy-match here - the
    # romanized cross-writing space legitimately produces one-edit Latin
    # pairs that are ALPHABET differences, not typos (English "powerbank"
    # vs the transliteration "poverbank" of «повербанк» differ exactly in
    # w/v), and tolerating them would fuse two languages.
    if dominant_script(left) != "cyrillic" or dominant_script(right) != "cyrillic":
        return False

    # The bounded inflectional tail is stripped ADAPTIVELY: any trailing
    # cut of up to _TYPO_NOUN_MAX_INFLECTION letters on BOTH words is
    # tried, and the pair passes when ONE such alignment leaves stems
    # exactly one edit apart. «поувербанк"/«повербанка» needs trimming the
    # title's «-а" but not the query's bare form.
    cuts_l = sorted({max(0, len(left) - k) for k in range(_TYPO_NOUN_MAX_INFLECTION + 1)})
    cuts_r = sorted({max(0, len(right) - k) for k in range(_TYPO_NOUN_MAX_INFLECTION + 1)})
    for cut_l in cuts_l:
        left_stem = left[:cut_l]
        if len(left_stem) < _TYPO_NOUN_MIN_LENGTH - _TYPO_NOUN_MAX_INFLECTION:
            continue
        for cut_r in cuts_r:
            right_stem = right[:cut_r]
            if len(right_stem) < _TYPO_NOUN_MIN_LENGTH - _TYPO_NOUN_MAX_INFLECTION:
                continue
            if left_stem[0] != right_stem[0]:
                continue
            if not _one_edit_apart(left_stem, right_stem):
                continue
            common = 0
            for a, b in zip(left_stem, right_stem):
                if a != b:
                    break
                common += 1
            if common >= _TYPO_NOUN_MIN_AGREEMENT:
                return True
    return False


def nouns_match(left: str, right: str) -> bool:
    """Inflection-aware equality of two nouns («щетка»/«щетки»).

    Long words compare by their first :data:`PREFIX_MATCH_LENGTH` letters
    (the same prefix rule the word-level filter already uses); short words
    must stand in a prefix relation, so «чай» never matches «чая» on one
    coincidental letter. A word shorter than :data:`MIN_TERM_LENGTH` is a
    grammatical token, not a product noun («с» of «тарелка с принтом» must
    never match «снастей» merely because one starts with the other), so it
    only ever equals ITSELF.
    """

    if left == right:
        return True
    if len(left) < MIN_TERM_LENGTH or len(right) < MIN_TERM_LENGTH:
        return False
    if len(left) >= PREFIX_MATCH_LENGTH and len(right) >= PREFIX_MATCH_LENGTH:
        if left[:PREFIX_MATCH_LENGTH] == right[:PREFIX_MATCH_LENGTH]:
            return True
        # G25.2-F: ONE stem typo in a LONG noun («поувербанк»/«повербанк»)
        # still names the same product - the prefix rule cannot see past
        # the mistyped character.
        return _noun_typo_match(left, right)
    return left.startswith(right) or right.startswith(left)


def haystack_of(product: Product) -> str:
    """Normalized title used by every word-level comparison."""

    return " ".join((product.title or "").lower().replace("ё", "е").split())


def scripts_of(text: str | None) -> frozenset[str]:
    """Which writing systems a text uses (``{"cyrillic"}``, ``{"latin"}``...)."""

    if not text:
        return frozenset()
    return frozenset(
        name for name, pattern in _SCRIPT_PATTERNS.items() if pattern.search(text)
    )


def dominant_script(text: str | None) -> str | None:
    """The writing system most letters of a text are written in.

    The majority - not just "present" - matters: a Russian title mentions a
    Latin brand ("Беспроводные наушники Bluetooth") but is still a Russian
    title, and comparing it with an English query term would be a
    comparison of two different languages. ``None`` means "cannot tell"
    (no letters at all, or an exact tie between scripts), which callers
    treat as "do not compare".
    """

    if not text:
        return None

    counts: dict[str, int] = {}
    for character in text:
        for name, pattern in _SCRIPT_PATTERNS.items():
            if pattern.match(character):
                counts[name] = counts.get(name, 0) + 1
                break

    if not counts:
        return None

    best = max(counts.values())
    winners = [name for name, count in counts.items() if count == best]
    return winners[0] if len(winners) == 1 else None



def has_valid_product_url(product: Product) -> bool:
    """Whether the product carries an absolute ``http(s)`` URL with a host."""

    url = (product.product_url or "").strip()
    if not url:
        return False
    parts = urlsplit(url)
    return parts.scheme.lower() in {"http", "https"} and bool(parts.netloc)


def has_meaningful_title(product: Product) -> bool:
    """Whether the title really is text (at least two letters)."""

    letters = sum(1 for character in (product.title or "") if character.isalpha())
    return letters >= 2


def _category_short_stems(request: SearchRequest) -> bool:
    """G24.8 P1.2: short stems count as evidence in CATEGORY mode only.

    A category request is fully catalog-authored (the fixed catalogue title
    plus the curated ``keywords``), so its 3-letter terms («сад», «дом»,
    «быт») are DECLARED category stems, never arbitrary user text. A SEARCH
    request may contain any short token the user typed, so SEARCH keeps the
    whole-word rule exactly as G24.6 left it.
    """

    return request.mode is SearchMode.CATEGORY


def _short_term_match(term: str, haystack: str) -> bool:
    """Word-initial stem match for a short CATEGORY term (G24.8 P1.2).

    Two guards keep the rule narrow:

    * the term must START a word - «насадка»/«высадка» never answer «сад»
      (no accidental substring match inside an unrelated word);
    * the continuation must be a word end, a non-letter, or a Cyrillic
      vowel (:data:`_SHORT_STEM_VOWELS`) - every Russian nominal
      inflection matches («сада», «садовый», «домашний», «бытовой») while
      compound heads do not («домкрат» does not answer «дом»).

    Deterministic and list-free: no per-word dictionary, no category
    whitelist, no substring relaxation for arbitrary short tokens.
    """

    pattern = (
        rf"(?<!\w){re.escape(term)}"
        rf"(?=$|[^a-zа-яё]|[{_SHORT_STEM_VOWELS}])"
    )
    return re.search(pattern, haystack) is not None


def _product_noun_present(
    nouns: list[str] | tuple[str, ...], title: str | None, script: str
) -> bool:
    """Whether a request PRODUCT noun occurs in ``title`` as a NOUN.

    The legacy ``present`` check below reused the word-level
    :func:`_term_matches` (plain prefix-4 substring). That lets a derived
    ADJECTIVE smuggle an unrelated title through: «теле» of «телескоп»
    sits inside «телескопическая», so every «телескопическая штанга»
    looked like it *mentions* the telescope. Noun-level evidence
    (:func:`noun_terms` of the title) requires the shared word to HEAD a
    product phrase on both sides - «телескоп» vs «штанга» no longer
    match, while inflections still do («телескоп» vs «телескопа»,
    «зонт» vs «зонта»).
    """

    title_nouns = noun_terms(title, script)
    return any(
        any(nouns_match(noun, title_noun) for title_noun in title_nouns)
        for noun in nouns
    )


#: A numeric specification candidate: a number bound to a following word
#: («65W», «65 Вт», «20000 мАч», «1,5 дюйма»), glued or spaced. Digits alone
#: never match - the number must carry a unit to name a measurable spec.
_SPEC_CANDIDATE_RE = re.compile(r"\d+(?:[.,]\d+)?\s*[a-zа-яё]+", re.IGNORECASE)


def _spec_phrases(request: SearchRequest) -> tuple[str, ...]:
    """The request's numeric specs («65W», «65 Вт»), for corroboration only.

    Stage 3G.1 (A): :func:`tokenize` is letter-only, so «65W»/«65 w» vanish
    from :func:`query_terms` and the derived-adjective echo of
    :func:`_other_term_corroborated` had nothing but nouns left to
    corroborate with. A candidate is kept ONLY when its word part is a
    KNOWN unit of the Stage-46 canonical table (``text_matching._UNIT_CANON``):
    «65w», «65 вт», «20000 мач» pass, while the budget «1000 грн», a bare
    number and any arbitrary fragment do not - a price bound must never
    count as proof of product identity. The title side is judged by
    :func:`~services.text_matching.phrase_in_text` (exact numbers, folded
    units), so «65W»/«65 Вт»/«65 w» are one and the same spec while
    «265W»/«650 Вт» never answer it.
    """

    sources = [
        request.normalized_query or "",
        request.original_query,
        *request.keywords,
    ]
    phrases: list[str] = []
    for source in sources:
        for match in _SPEC_CANDIDATE_RE.finditer(source):
            phrase = " ".join(match.group(0).split()).lower().replace("ё", "е")
            unit = re.sub(r"^[\d\s]+", "", phrase)
            if unit in _UNIT_CANON and phrase not in phrases:
                phrases.append(phrase)
    return tuple(phrases)


def _other_term_corroborated(
    product: Product, request: SearchRequest, query_nouns: list[str] | tuple[str, ...]
) -> bool:
    """Whether a NON-NOUN query word occurs in the title.

    Corroboration for a title linked to the request ONLY through a
    derived adjective («аккумуляторная» vs «аккумулятор»): the brand,
    spec or modifier the user also wrote («Makita», «18V», «домашний»)
    still occurs, so the title is a related result («Дрель
    аккумуляторная Makita 18V»), not a bare adjective echo
    («Телескопическая штанга» for «телескоп» shares nothing else).
    Every script counts: a latin brand corroborates a cyrillic title.
    Stage 3G.1 (A): an explicit numeric spec the user wrote («65W»,
    «65 Вт» - :func:`_spec_phrases`) corroborates as well, matched
    word-level and number-exact by the Stage-46 matcher, so it can never
    corroborate on an arbitrary digit run.
    """

    haystack = haystack_of(product)
    others = [
        term
        for term in query_terms(request)
        if not any(nouns_match(term, noun) for noun in query_nouns)
    ]
    if any(_term_matches(term, haystack) for term in others):
        return True
    return any(phrase_in_text(haystack, spec) for spec in _spec_phrases(request))


#: Stage 3G.3 - words that declare the product SUPPORTS or is COMPATIBLE
#: WITH what follows, so what follows is not the product itself. A closed,
#: language- and category-neutral class of role markers: the observed false
#: matches were all governed by a support declaration («кейс …: поддержка
#: … зарядного хаба и зарядного устройства 65W»). «для» is deliberately
#: NOT a marker: «зарядное устройство для ноутбука» and «адаптер питания …
#: для портативного игрового зарядного устройства» name genuine chargers.
_SUPPORT_ROLE_RE = re.compile(r"(?:поддержк|support|совместим|предназнач)")


def _type_position_derived_words(
    haystack: str, stem: str, noun: str, title_nouns: set[str]
) -> list[str]:
    """Derived echoes of ``noun`` that can name the product type.

    Stage 3G.3 narrows the derived-adjective echo of Stage 3: the longer
    word on the request's root («зарядный» for «зарядка») only counts
    when it participates in the product-name phrase:

    * it directly touches a title noun («адаптер-зарядное устройство»,
      «зарядный кабель»), or reaches one through a CHAIN of adjectives
      separated by whitespace only («Аккумуляторная угловая шлифмашина»,
      «Дрель аккумуляторная»);
    * or it is a detached attribute isolated by commas on both sides
      («Электрический бесщеточный лобзик …, аккумуляторный, с
      рукояткой») - still an attribute of the product itself;

    while the same echo as a FEATURE is dropped: a с-instrumental
    construct («с быстрой зарядкой», «Bluetooth-колонкой» - the
    hyphen and the following punctuation/word break the phrase), a
    spec-tail («зарядкой PD3.1») or any echo whose path to a noun is
    blocked by a function word, digit or comma on one side only. A
    coinciding spec («65W», «20W») can no longer launder a feature into
    product identity: specification evidence must not compensate for a
    missing product noun.

    An occurrence preceded anywhere by a support/compatibility
    declaration (:data:`_SUPPORT_ROLE_RE`) is dropped as well: «поддержка
    … зарядного хаба» names a SUPPORTED item inside the case, and the
    declaration governs the whole comma list after it.

    The minimum-root rule and the corroboration requirement stay with
    the caller, so a bare echo still proves nothing on its own.
    """

    lowered = (haystack or "").lower()
    tokens = [
        (match.start(), match.end(), match.group(0))
        for match in _WORD_RE.finditer(lowered)
    ]

    def is_modifiable(word: str) -> bool:
        """An adjective/modifier that can chain toward the head noun."""
        return word not in title_nouns and (
            _looks_like_adjective(word) or word in MODIFIER_TOKENS
        )

    def reaches_title_noun(index: int, step: int) -> bool:
        """Whether a whitespace-only adjective chain from ``index`` hits a noun."""
        cursor = index + step
        while 0 <= cursor < len(tokens):
            start, end, word = tokens[cursor]
            gap = (
                lowered[end : tokens[cursor - step][0]]
                if step == -1
                else lowered[tokens[cursor - step][1] : start]
            )
            if gap.strip():
                return False
            if word in title_nouns:
                return True
            if not is_modifiable(word):
                return False
            cursor += step
        return False

    def _detached_attribute(index: int, start: int, end: int) -> bool:
        """A comma-isolated attribute («…, аккумуляторный, с рукояткой…»).

        Both gaps around the echo must be commas plus whitespace and
        nothing else, so «с быстрой зарядкой, док-станция» never
        qualifies: there the echo is glued to a с-complement on one side.
        """
        if index == 0 or index + 1 >= len(tokens):
            return False
        left_gap = lowered[tokens[index - 1][1] : start]
        right_gap = lowered[end : tokens[index + 1][0]]
        return bool(
            re.fullmatch(r"[\s,]*,[\s,]*", left_gap)
            and re.fullmatch(r"[\s,]*,[\s,]*", right_gap)
        )

    words: list[str] = []
    for index, (start, end, word) in enumerate(tokens):
        if not word.startswith(stem) or len(word) <= len(noun):
            continue
        if word in title_nouns:
            continue
        if not (
            reaches_title_noun(index, -1)
            or reaches_title_noun(index, 1)
            or _detached_attribute(index, start, end)
        ):
            continue
        if _SUPPORT_ROLE_RE.search(lowered[:start]):
            continue
        if word not in words:
            words.append(word)
    return words


def _term_matches(term: str, haystack: str, *, short_stem: bool = False) -> bool:
    """Prefix match for longer words, whole-word match for short ones.

    Prefix matching is intentional: Russian endings change often and a whole
    word would never match. It can only ever keep a borderline product, not
    remove a valid one.

    ``short_stem`` (G24.8 P1.2, CATEGORY requests only) replaces the
    whole-word rule for terms shorter than :data:`PREFIX_MATCH_LENGTH` with
    the narrow word-initial stem rule of :func:`_short_term_match`. The
    default ``False`` keeps SEARCH and every other mode byte-identical.

    G25.2-F: when the ordinary prefix rule finds nothing, a LONG term may
    still answer through ONE typo inside its stem (:func:`_noun_typo_match`
    applied to the term and every whole word of the haystack): a mistyped
    user noun («поувербанк") must not mute the whole word-level gate when
    the title carries the correctly spelled product noun. The rule inherits
    every bound of the noun-typo helper - one edit, same script, long
    words, head agreement - and it can only ever KEEP a candidate (a fuzzy
    term hit is still positive evidence), never reject one the prefix rule
    already kept.
    """

    if len(term) >= PREFIX_MATCH_LENGTH:
        if term[:PREFIX_MATCH_LENGTH] in haystack:
            return True
        if len(term) >= _TYPO_NOUN_MIN_LENGTH:
            for word in re.findall(r"[\wа-яё]+", haystack):
                if _noun_typo_match(term, word):
                    return True
        return False
    if short_stem:
        return _short_term_match(term, haystack)
    return re.search(rf"(?<!\w){re.escape(term)}(?!\w)", haystack) is not None


def comparable_terms(product_title: str | None, request: SearchRequest) -> list[str]:
    """The request terms that CAN be compared with this title.

    Comparison is only possible inside one writing system, and the title (not
    the request) decides which one: the marketplace answer is written in the
    language the provider asked for, while a request always mixes the
    marketplace phrase with the user's own words. Returns an empty list when
    nothing can be compared - callers must then filter NOTHING, so a
    cross-language search can never hide valid listings.
    """

    title_script = dominant_script(product_title)
    if title_script is None:
        return []

    return [
        term for term in query_terms(request) if title_script in scripts_of(term)
    ]


#: Fixed CHARACTER-level Cyrillic→Latin transliteration. This is
#: orthography, not vocabulary: a 33-letter alphabet map with no word of
#: any language in it, so it can bridge «небулайзер»/"nebulizer" and
#: «портативный»/"portable" (prefix comparison, same
#: :data:`PREFIX_MATCH_LENGTH` rule) without a synonym dictionary or any
#: product knowledge. Multi-letter digraphs map to their conventional
#: Latin spelling (ж→zh, ч→ch, ...).
_CYR_TO_LAT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def latin_space(text: str | None) -> str:
    """``text`` with every Cyrillic word romanized (lower-cased words).

    The shared comparison space for CROSS-WRITING evidence: one side of a
    cross-language query is translated character-by-character into the
    space of the other, so the ordinary prefix matcher can judge it. No
    vocabulary is involved - only the alphabet table above.
    """

    return " ".join(
        "".join(_CYR_TO_LAT.get(ch, ch) for ch in word)
        for word in tokenize((text or "").lower().replace("ё", "е"))
    )


def _cross_evidence(product: Product, request: SearchRequest) -> dict[str, bool]:
    """Romanized evidence of the request in this title, for OTHER-script words.

    Only request terms written in a DIFFERENT script from the title's
    dominant one are examined (same-script terms are already judged by the
    ordinary path). Returns:

    * ``leading`` - the title's head noun equals a request product noun in
      romanized space (``"exact"`` evidence);
    * ``noun`` - some request PRODUCT noun occurs in the romanized title;
    * ``any`` - some request word at all occurs (modifier-level evidence).

    Purely mechanical: romanize, then the same prefix-4 matcher.
    """

    title_script = dominant_script(product.title)
    if title_script is None:
        return {"leading": False, "noun": False, "any": False}

    hay = latin_space(haystack_of(product))
    nouns = [
        latin_space(noun)
        for script in ("cyrillic", "latin")
        for noun in query_noun_terms(request, script)
    ]
    leading = leading_noun(product.title, title_script)
    leading_l = latin_space(leading) if leading else ""

    found = {"leading": False, "noun": False, "any": False}
    if not hay:
        return found
    # Head-noun evidence is checked independently of the term loop: the
    # title may LEAD with the romanized product noun even when no full
    # request word occurs verbatim («… небулайзер» vs "nebulizer").
    if leading_l:
        found["leading"] = any(
            nouns_match(leading_l, noun) for noun in nouns
        )
    for term in query_terms(request):
        if title_script in scripts_of(term):
            continue  # same-script words belong to the ordinary comparison
        romanized = latin_space(term)
        if not _term_matches(romanized, hay):
            continue
        found["any"] = True
        if any(nouns_match(romanized, noun) for noun in nouns):
            found["noun"] = True
        if found["any"] and found["noun"]:
            break
    return found


def _has_structured_intent(request: SearchRequest) -> bool:
    """Whether Stage 2 filled any ``intent_*`` field.

    :func:`classify_product` only ever runs in PRODUCT_TYPE_MODE, so this
    alone isolates the intent-aware rules to the text Find Product search:
    categories, feeds, photo and discovery requests keep the exact G24.6
    classification they had before Stage 3.
    """
    return any(
        (
            request.intent_product_type,
            request.intent_brand,
            request.intent_model,
            request.intent_generation,
            request.intent_required_attributes,
            request.intent_compatibility,
            request.intent_excluded_product_types,
        )
    )


def _intent_product_nouns(
    request: SearchRequest, base: Sequence[str]
) -> list[str]:
    """Query nouns that name the PRODUCT, minus compatibility/exclusion.

    The word-share sources keep «iphone» of «зарядка для iPhone» for the
    GATE (such a title still passes R3/R5 as "mentioned"), but EXACT must
    never be awarded for a compatibility or excluded target: the product
    words are the base nouns minus every context phrase, plus the raw
    words of ``intent_product_type``. Context still wins over the raw
    phrase - «без чехла» pollutes the product phrase with «чехла», while
    the exclusion itself is explicit.
    """
    context = [
        *request.intent_compatibility,
        *request.intent_excluded_product_types,
    ]
    # Stage 7 (false-EXACT): a brand word never NAMES the requested
    # product - «Apple» of «зарядка для Apple iPhone» is the brand of the
    # compatibility target, not the product role, so a brand-led latin
    # title («Чехол для Apple iPhone...») must not find its leading word
    # among the product nouns.
    brand_tokens = _intent_phrase_tokens(request.intent_brand)

    def is_context(noun: str) -> bool:
        return any(
            _intent_role_match(noun, phrase) for phrase in context if phrase
        )

    def is_brand(noun: str) -> bool:
        return any(nouns_match(noun, token) for token in brand_tokens)

    nouns = [
        noun for noun in base
        if not is_context(noun) and not is_brand(noun)
    ]
    if request.intent_product_type:
        for token in _intent_phrase_tokens(request.intent_product_type):
            # Stage 14.2: a role token shorter than MIN_TERM_LENGTH is a
            # function word, not a product noun («на»/«но» prefix-match
            # «набор» through nouns_match and granted a false EXACT).
            if len(token) < MIN_TERM_LENGTH:
                continue
            if not is_context(token) and token not in nouns:
                nouns.append(token)
    return nouns


def _has_product_role_evidence(
    title: str | None, request: SearchRequest, script: str
) -> bool:
    """Whether ``title`` shares a PRODUCT noun with the request (Stage 62).

    The product words are the request's base nouns of ``script`` minus every
    compatibility/excluded target (see :func:`_intent_product_nouns`); the
    comparison is the same inflection-aware :func:`nouns_match` the gate
    uses. Cross-script product evidence («небулайзер» for "nebulizer")
    counts via :func:`_cross_evidence` ``noun``/``leading``.

    A title matching only a compatibility word («телефон» of «держатель для
    телефона» inside a «чехол для телефона») has no evidence it fulfills the
    requested product role - it stays eligible (never rejected here), but
    ranks below products that do.
    """

    if not title:
        return False
    product_nouns = _intent_product_nouns(
        request, query_noun_terms(request, script)
    )
    # Content tokens (not just noun_terms): a device-word title shares its
    # role through a derived form («зарядное» of «зарядка») that noun_terms
    # classifies as an adjective. Same inflection-aware match as the gate.
    if any(
        nouns_match(token, product_noun)
        for token in _intent_phrase_tokens(title)
        if script in scripts_of(token)
        for product_noun in product_nouns
    ):
        return True
    # Cross-script product evidence («небулайзер» for "nebulizer") counts the
    # same way the tier-1 gate counts it: a romanized noun/leading match.
    stub = Product(
        marketplace=Marketplace.ALIEXPRESS,
        product_id="stage62-role-probe",
        title=title,
        product_url="https://www.aliexpress.com/item/stage62.html",
    )
    evidence = _cross_evidence(stub, request)
    return bool(evidence["noun"] or evidence["leading"])


#: Prepositions that turn the NEXT noun into the compatibility TARGET of
#: the title: «крепление ДЛЯ видеорегистратора» - the noun says what the
#: product is FOR, not what it IS (G25.2-C Stage 7, false-EXACT guard).
_TARGET_INTRODUCERS = frozenset({"для", "for"})


def _after_target_intro(title: str | None, noun: str | None) -> bool:
    """Whether the FIRST occurrence of ``noun`` sits directly after
    «для»/«for» in ``title`` (the noun is the target, not the head)."""

    if not title or not noun:
        return False
    folded = title.casefold().replace("ё", "е")
    target = noun.casefold().replace("ё", "е")
    start = folded.find(target)
    if start < 0:
        return False
    if start and folded[start - 1].isalnum():
        # inside a longer word - not this noun's occurrence
        return False
    prefix_tokens = re.findall(r"[0-9a-zа-я]+", folded[:start])
    return bool(prefix_tokens) and prefix_tokens[-1] in _TARGET_INTRODUCERS
#: G25.2-E (false-EXACT tier-1 leak): constructions that introduce the
#: requested noun as a FEATURE/ACCESSORY of another device, never as the
#: product itself. Each introducer keeps its own grammatical meaning:
#
#: * «с повербанком» (RU instrumental)  - the device COMES WITH one;
#: * «with power bank» (EN)             - the device COMES WITH one;
#: * «powered by a power bank» (EN)     - the device RUNS on one.
#
#: «для»/«for» are NOT here: they already name a compatibility TARGET
#: (:data:`_TARGET_INTRODUCERS`), and «чехол для повербанка» must keep its
#: accessory verdict, not be re-judged by this guard.
_FEATURE_MENTION_RE = re.compile(
    r"(?:^|[^\wа-яё])"                       # word boundary before the introducer
    r"(?:с|со|with|powered\s+by)"            # the introducer itself
    r"(?:\s+(?:в\s+комплекте|комплектом|included|built[-\s]?in))?"
    r"(?:\s+(?:а|a|an|the))?$",
    re.IGNORECASE,
)


def _noun_is_feature_mention(title: str | None, noun: str) -> bool:
    """Whether EVERY occurrence of ``noun`` in ``title`` sits behind a
    feature introducer («с повербанком», «with power bank», «powered by a
    power bank») - i.e. the title names another DEVICE that merely comes
    with / runs on the requested product.

    Narrow by construction: the guard fires only when the noun occurs at
    least once behind such an introducer AND never in a free (unguarded)
    position - «Power bank with USB-C cable» keeps «power bank» free, so a
    genuine product sold WITH an accessory is untouched; «Emergency radio
    with power bank» has its only occurrence guarded, so the radio can no
    longer rank as a power-bank family result. Deterministic, list-free
    beyond the closed introducer class itself.
    """

    if not title or not noun:
        return False
    folded = re.sub(r"\s+", " ", title.casefold().replace("ё", "е")).strip()
    target = re.sub(r"\s+", " ", noun.casefold().replace("ё", "е")).strip()
    if not folded or not target:
        return False
    # Trailing letters allow the ordinary Russian nominal inflection
    # («повербанк» -> «повербанком»), the same tolerance nouns_match grants.
    pattern = re.compile(
        r"(?<![\wа-яё])" + re.escape(target) + r"[a-zа-яё]*(?![a-zа-яё])"
    )
    guarded = False
    for match in pattern.finditer(folded):
        before = folded[: match.start()].rstrip()
        if _FEATURE_MENTION_RE.search(before):
            guarded = True
        else:
            return False  # a free occurrence: the product role stands
    return guarded

def _mentions_only(product: Product, request: SearchRequest) -> bool:
    """Whether a tier-1 candidate is a MENTION-ONLY result (G25.2-F).

    True when the title LEADS with a product noun unrelated to the request
    while the requested noun occurs only elsewhere in the title - an
    accessory or another device. A title whose head noun IS the requested
    product (a genuine related result, or an exact match downgraded by a
    subtype/feature guard) answers False: the head-noun family keeps its
    unbounded verified behaviour in the final cut.

    Deterministic and list-free: the head noun is the ordinary
    :func:`leading_noun`, and "unrelated" means no query product noun
    matches it in EITHER script (inflection/typo tolerance included).
    """

    if request.mode is not PRODUCT_TYPE_MODE:
        return False
    title = product.title
    leadings: list[str] = []
    for script in ("cyrillic", "latin"):
        noun = leading_noun(title, script)
        if noun and noun not in leadings:
            leadings.append(noun)
    if not leadings:
        return False
    all_query_nouns: list[str] = []
    for script in ("cyrillic", "latin"):
        for noun in query_noun_terms(request, script):
            if noun not in all_query_nouns:
                all_query_nouns.append(noun)
    if not all_query_nouns:
        return False
    # (a) direct head match, (b) the romanized space: a cyrillic head
    # («повер» of «Повер банк») may romanize onto a latin query noun
    # ("power") - the SAME cross-writing evidence that made the classifier
    # keep the title. Either counts as head-noun evidence; the product is
    # NOT mention-only.
    for leading in leadings:
        for noun in all_query_nouns:
            if nouns_match(leading, noun):
                return False
            leading_lat = latin_space(leading)
            noun_lat = latin_space(noun)
            if leading_lat and noun_lat:
                if nouns_match(leading_lat, noun_lat):
                    return False
                # The romanized space has NO Cyrillic typo problem: its
                # match may rest on the ordinary prefix rule (the evidence
                # `_cross_evidence` "leading" accepts), so a truncated
                # romanized head («повер" -> "pover") that shares the
                # romanized noun's PREFIX ("pover"/"power" agree on 4 of 5
                # characters) still counts as head-noun evidence. Bounded:
                # the shared prefix must be at least
                # :data:`_TYPO_NOUN_MIN_AGREEMENT` characters and cover at
                # least 3 characters of the shorter word - an unrelated
                # product word («машинка" -> "mashinka", a 3-letter POS
                # acronym "pos") never passes. One-character divergence
                # inside the shared head is tolerated (the transliteration
                # of «в" is "v", English spells the same sound "w"),
                # bounded to ONE such difference.
                shared = 0
                divergence = 0
                for a, b in zip(leading_lat, noun_lat):
                    if a != b:
                        divergence += 1
                        if divergence > 1:
                            break
                        continue
                    shared += 1
                shorter = min(len(leading_lat), len(noun_lat))
                if (
                    shared >= _TYPO_NOUN_MIN_AGREEMENT + 1
                    and shared * 2 >= shorter
                    and divergence <= 1
                ):
                    return False
    # A VERY SHORT latin head (a POS terminal's «pos», a brand acronym) is
    # not a product word at all: ``noun_terms`` keeps it as a noun only
    # because letters exist. Such a head carries no product identity either
    # way - the title remains a mention-only candidate (the requested noun
    # lives in the tail).
    return True


def _title_mentions_request_as_feature(
    product_title: str | None, request: SearchRequest
) -> bool:
    """Whether the requested product noun appears in the title ONLY as a
    feature of another device («Подводный фонарь … с повербанком»,
    "Emergency radio with power bank", "Bubble machine powered by a power
    bank"). Checked for every query product noun in BOTH scripts; the
    cross-writing space is compared in romanized form, so a Cyrillic
    «повербанк» inside a Latin title is caught the same way.
    """

    if not product_title:
        return False
    nouns: list[str] = []
    for script in ("cyrillic", "latin"):
        for noun in query_noun_terms(request, script):
            if noun not in nouns:
                nouns.append(noun)
    if not nouns:
        return False
    hay_cyr = haystack_of(Product(
        marketplace=Marketplace.ALIEXPRESS,
        product_id="feature-probe",
        title=product_title,
        product_url="https://www.aliexpress.com/item/feature-probe.html",
    ))
    hay_lat = latin_space(hay_cyr)
    # (a) the WHOLE product phrase as one unit, plus (b) every single noun
    # of it. (a) is what separates "Emergency radio with power bank" (the
    # only occurrence of the phrase sits behind the introducer -> guarded)
    # from "Power bank with USB-C cable included" (the phrase LEADS the
    # title -> free -> untouched). (b) catches Cyrillic-inflected or
    # otherwise inflected single-noun mentions («с повербанком»).
    candidates: list[str] = [" ".join(nouns)]
    candidates.extend(nouns)
    return any(
        _noun_is_feature_mention(hay_cyr, noun)
        or _noun_is_feature_mention(hay_lat, latin_space(noun))
        for noun in candidates
    )


def _title_heads_product_type(title: str | None,
                              request: SearchRequest) -> bool:
    """Whether the title literally BEGINS with the requested product type.

    Stage 7: the first content word EQUALS (casefolded) a raw word of
    ``intent_product_type`` («КРЕПЛЕНИЕ для видеорегистратора» for
    pt=крепление) - the requested role verified at the head position even
    when ``noun_terms`` itself skips that word («-ние» forms are not
    nouns to it). The match is EXACT, not prefix-based: a derived
    adjective «Аккумуляторная (воздуходувка)» is not the noun
    «аккумулятор» and must not verify the product role. Leading
    prepositions are never skipped, so «Для аккумулятора Makita...» does
    not qualify here either.
    """

    if not title or not request.intent_product_type:
        return False
    tokens = _intent_phrase_tokens(request.intent_product_type)
    if not tokens:
        return False
    head_tokens = re.findall(
        r"[0-9a-zа-я]+", title.casefold().replace("ё", "е")
    )
    if not head_tokens:
        return False
    head = head_tokens[0]
    return head in tokens


#: G25.2-C Stage 10 (R1): specialised product subtypes that are
#: INCOMPATIBLE with an ordinary request even when they share its head
#: noun. Evidence: false-EXACT verdicts of Stage-9 C5/E3 - an industrial
#: machine-vision camera («Промышленная камера машинного зрения…») is not
#: the ordinary camera of «камера 4K для машины», yet the shared noun
#: «камера» alone used to award tier 2. The list stays a semantic subtype
#: declaration (a domain phrase that NAMES the subtype), never a keyword
#: blacklist of the ordinary words themselves.
_UNREQUESTED_SUBTYPE_MARKERS: tuple[str, ...] = (
    "машинного зрения",
    "машинное зрение",
    "machine vision",
    "machine-vision",
    # G25.2-E: commercial rental/charging infrastructure that LEADS with the
    # requested noun but is a DIFFERENT product (the live «мощный повербанк»
    # audit: power-bank RENTAL STATIONS and payment KIOSKS classified exact
    # because the head noun matched). Evidence-backed, closed list of the
    # observed infrastructure words - never the ordinary product words
    # themselves.
    "rental station",
    "rental",
    "аренда",
    "прокат",
    "kiosk",
    "киоск",
    "vending",
    "вендинг",
)


#: G25.2-D (O2): a title that LEADS with a REPLACEMENT-COMPONENT noun and
#: only names the requested product after «для»/«for» is a spare part, not
#: the complete product. Evidence: the Stage-48 live search for a home drill
#: returned «Редукторный механизм 125463-0 для аккумуляторной дрели-шуруповерта
#: MAKITA» at tier 1 - it shares the head noun «шуруповерт» and so looked
#: relevant, yet it is a gearbox for a drill, never the drill itself. The list
#: is a closed grammatical CLASS of component nouns (a mechanism, a brush, a
#: filter …), never a keyword blacklist of ordinary product words; a request
#: that explicitly asks for a part («редуктор для дрели») carries the same
#: noun in its own product phrase and is never blocked here.
_SPARE_PART_HEAD_STEMS: tuple[str, ...] = (
    # -- Russian internal replacement components --------------------------
    # A closed class of INTERNAL mechanism parts (the things a user replaces
    # when a device breaks), never ordinary accessories: a charger, adapter,
    # hose or filter for the requested device stays a legitimate sibling and
    # is deliberately NOT in this list.
    "редуктор", "механизм", "щетк", "щёток", "подшипник", "сальник",
    "прокладк", "уплотнител", "уплотнен", "шестерн", "шестерен", "втулк",
    "муфт", "статор", "ротор", "якор", "катушк", "карбюратор", "карбюрат",
    "ремкомплект", "запчаст", "комплектующ", "свеч",
    # -- English internal replacement components --------------------------
    "gearbox", "gear box", "bearing", "gasket", "seal", "sprocket",
    "stator", "rotor", "armature", "spare part", "spares", "carburettor",
    "carburetor", "spark plug", "replacement part", "replacement kit",
)

#: After-target introducer: «редуктор ДЛЯ дрели» - the noun before «для» is
#: the spare-part head, the noun after it is the requested complete product.
_SPARE_PART_TARGET_INTRO = "для"


def _title_heads_spare_part(title: str | None) -> bool:
    """Whether the title LEADS with a replacement-component noun.

    Only the first meaningful words of the title are inspected (the head),
    so «Зарядное устройство для дрели» (a charger IS the requested accessory
    when asked for) and a genuine complete product are not caught - the head
    of «Дрель-шуруповёрт аккумуляторная» is «дрель», never a component noun.
    """

    if not title:
        return False
    folded = title.casefold().replace("ё", "е")
    # Only the first few words can be the head noun.
    head = " ".join(folded.split()[:4])
    return any(head.startswith(stem) for stem in _SPARE_PART_HEAD_STEMS)


def _is_spare_part_leak(
    product_title: str | None, request: SearchRequest
) -> bool:
    """Whether a kept title is a SPARE PART for a request that never asked
    for one.

    True only when BOTH hold: the title leads with a component noun, AND it
    names the requested product AFTER «для»/«for» (a «редуктор для дрели»
    shape). A request that explicitly wants a part keeps the component noun in
    its own product phrase (the request text, product type, brand, model or
    compatibility already contains it), so a legit spare-part search is never
    blocked - the guard fires only on the complete-product request that this
    stage observed leaking.
    """

    if not _title_heads_spare_part(product_title):
        return False
    request_text = " ".join(
        part
        for part in (
            request.original_query or "",
            request.normalized_query or "",
            request.intent_product_type or "",
            *(request.keywords or []),
            *(request.intent_compatibility or []),
            *(request.intent_excluded_product_types or []),
        )
        if part
    ).casefold().replace("ё", "е")
    # The request itself asked for a part: keep it (never leak the guard onto
    # «редуктор для дрели» / «угольные щётки для дрели»).
    if any(stem in request_text for stem in _SPARE_PART_HEAD_STEMS):
        return False
    # «… ДЛЯ дрели-шуруповерта»: the component sits BEFORE the target
    # introducer and the requested product after it.
    folded = (product_title or "").casefold().replace("ё", "е")
    intro = folded.find(_SPARE_PART_TARGET_INTRO)
    return intro > 0



def _declares_unrequested_subtype(
    title: str | None, request: SearchRequest
) -> bool:
    """Whether the TITLE declares a specialised subtype the request never
    asked for («камера МАШИННОГО ЗРЕНИЯ» vs «камера 4K для машины»).

    Stage 10 (R1) false-EXACT guard: a head-noun match alone is NOT enough
    for EXACT when the candidate names a different product subtype domain.
    The marker must be absent from EVERY side of the REQUEST (raw and
    normalized query, keywords, the structured product type, required
    attributes and compatibility) - a legit «камера для машинного зрения»
    carries the marker in its own request text, so its EXACT verdict is
    untouched. Callers gate on :func:`_has_structured_intent`, so with no
    ``intent_*`` field filled this helper is never consulted and every
    legacy G24.6 path stays byte-identical.
    """

    if not title:
        return False
    folded_title = title.casefold().replace("ё", "е")
    markers = [
        marker for marker in _UNREQUESTED_SUBTYPE_MARKERS
        if marker in folded_title
    ]
    if not markers:
        return False
    requested = " ".join(
        part
        for part in (
            request.original_query,
            request.normalized_query or "",
            request.intent_product_type or "",
            *request.keywords,
            *request.intent_required_attributes,
            *request.intent_compatibility,
        )
        if part
    ).casefold().replace("ё", "е")
    return not any(marker in requested for marker in markers)


#: Words that introduce a DECORATIVE / thematic / possessive mention of the
#: requested use in a title, rather than a genuine product role: «…стаканы С
#: рыбами», «…наклейки НА ТЕМУ рыбалки», «…ДЕКОР …», «…УКРАШЕНИЕ …». When the
#: requested-use word appears ONLY behind one of these, the container-led
#: title is a themed decoration, not the requested product. A closed
#: grammatical/context class, not a per-category product list.
_CONTAINER_DECOR_INTRODUCERS = (
    "с", "со", "тему", "тема", "теми", "стиле", "декора",
    "декор", "украшени", "оформлени", "мотив", "принт", "рисун",
)


def _container_head_names_request(
    title: str | None, request: SearchRequest
) -> bool:
    """Whether a container-led title genuinely names the requested product.

    A generic container («набор для рыбака») describes a set FOR something;
    the container noun itself says nothing about WHAT is inside. So a title
    that LEADS with such a container earns EXACT only when the requested
    use/product word appears in a GENUINE product role - «Набор **рыболовных**
    снастей … **для рыбалки**», «Набор **инструментов** …», «Набор кистей **для
    макияжа**». A themed/decorative set that mentions the use only behind a
    decoration introducer - «…стаканы **с рыбами** … **на тему рыбалки**»,
    «…наклейки **на тему инструментов**» - does NOT qualify and stays a
    related (tier-1) result.

    The requested-use words come from the request's own compatibility /
    excluded-type phrases plus any concrete (non-container) product noun, so
    nothing here invents a product word. Words are compared with the ordinary
    prefix rule, so «рыбака» meets «рыбалка»/«рыболов…» exactly as the rest of
    the gate does. When no specific use was requested (a bare «набор») the
    guard never fires and today's behaviour is preserved.
    """
    if not title:
        return False
    # Requested-use words: compatibility / excluded targets plus any concrete
    # (non-container) product noun of the query. A generic container alone is
    # never a "use" - «набор» must not satisfy itself.
    use_words: list[str] = []
    for phrase in (
        *request.intent_compatibility,
        *request.intent_excluded_product_types,
    ):
        if not phrase:
            continue
        for token in _intent_phrase_tokens(phrase):
            if not _is_generic_container(token) and token not in use_words:
                use_words.append(token)
    for noun in query_noun_terms(request, "cyrillic"):
        if not _is_generic_container(noun) and noun not in use_words:
            use_words.append(noun)
    if not use_words:
        # Nothing more specific than the container was requested: preserve the
        # pre-Stage-35 behaviour (a bare «набор» query keeps matching sets).
        return True

    tokens = re.findall(r"[0-9a-zа-я]+", title.casefold().replace("ё", "е"))
    for index, token in enumerate(tokens):
        if not any(nouns_match(token, use) for use in use_words):
            continue
        previous = tokens[index - 1] if index else ""
        # A leading use word (index 0) or one NOT behind a decoration
        # introducer is a genuine product role.
        if index == 0 or not any(
            previous.startswith(intro) for intro in _CONTAINER_DECOR_INTRODUCERS
        ):
            return True
    return False



def _leads_with_generic_container(
    leading: str | None, product_nouns: Sequence[str]
) -> bool:
    """Whether the title's HEAD noun is a container that matched the query.

    True only when the leading noun IS a generic container AND that container
    is what earned the head-noun match (the query's product nouns contain a
    container). A title that leads with the CONCRETE product («Рыболовный
    набор …» where «набор» is not the leading noun) is unaffected.
    """
    if not _is_generic_container(leading):
        return False
    return any(_is_generic_container(noun) for noun in product_nouns)


def _is_container_audience_request(
    request: SearchRequest, product_nouns: Sequence[str]
) -> bool:
    """Whether the request is a generic-container + audience/activity-only
    query whose concrete product is NOT named - «Набор для рыбака».

    True only when ALL of these hold (Stage 39):
    * the parsed product type is itself a generic container («набор»/
      «комплект»/«set»/«kit») - the request names the FORM of the offer;
    * the only other concept is an audience/activity already captured in
      ``intent_compatibility» («рыбака»);
    * NO concrete requested product noun exists beyond the container - the
      product INSIDE the set is genuinely unstated.

    Such a request is inherently ambiguous about WHAT to buy, so a
    container-led candidate can never be the requested product on the
    container + audience match alone. Stage 37 showed a genuine set
    («Набор рыболовных снастей…») and a themed one («Набор посуды…
    на тему рыбалки») are structurally identical - telling them apart needs
    an activity→instrument lexicon this project deliberately does not
    build - so the honest, list-free correction is to withhold the EXACT
    verdict for BOTH and keep them as related (tier-1) results.

    This is a REQUEST-shape test over EXISTING parsed intent: it invents no
    product, activity or audience word and adds no schema field. A concrete
    product («удочка для рыбалки») never qualifies - its product type is
    not a container. A container that DOES name a concrete noun («набор
    инструментов», «набор посуды») is excluded by the no-concrete-noun
    condition. A bare container with no audience («набор») is excluded by
    the empty-compatibility condition, so its existing behaviour stands.
    """
    if not request.intent_product_type or not request.intent_compatibility:
        return False
    pt_tokens = _intent_phrase_tokens(request.intent_product_type)
    if not pt_tokens or not all(_is_generic_container(t) for t in pt_tokens):
        return False
    # No concrete requested product noun beyond the generic container(s):
    # every surviving product noun is itself a container.
    return all(_is_generic_container(noun) for noun in product_nouns)


def classify_product(product: Product, request: SearchRequest) -> str:
    """Stages 3+4: IS this candidate the requested product? (deterministic)

    Returns one of:

    ``"exact"``
        the title's head noun is one of the query's product nouns - the
        candidate is the requested product or a close variant of it;
    ``"mentioned"``
        the title leads with a DIFFERENT product and only mentions the
        requested one («футляр для … зубной щетки», «… вода … чай …»): an
        accessory, a case, an ingredient - never the primary product;
    ``"noun_miss"``
        the title matched only on non-noun words (an adjective or a
        modifier): no evidence at all that it is the requested product;
    ``"unverified"``
        nothing comparable to judge (no query nouns in the title's script,
        a title without nouns, or a mode the gate must not decide).

    Only :attr:`SearchMode.SEARCH` is ever classified: a category is many
    product types at once, and feeds without a product query sit outside
    ``TOKEN_FILTER_MODES`` entirely. For LATIN titles the ``"mentioned"``
    verdict additionally requires the requested noun to sit after
    "for"/"of", because a latin title routinely LEADS with the brand
    («Xiaomi … headphones») while a Cyrillic title leads with the product.
    """

    if request.mode is not PRODUCT_TYPE_MODE:
        return "unverified"

    script = dominant_script(product.title)
    if script is None:
        return "unverified"

    # Stage 10 (R1): computed ONCE - every EXACT site below consults it, so
    # a title declaring an unrequested subtype is downgraded to "mentioned"
    # (tier 1, kept) instead of earning tier 2 from the shared head noun.
    # Structured intent only: without ``intent_*`` fields nothing changes.
    structured = _has_structured_intent(request)
    subtype_conflict = structured and _declares_unrequested_subtype(
        product.title, request
    )

    query_nouns = query_noun_terms(request, script)
    if not query_nouns:
        # No product noun of THIS writing system: judge the title in the
        # romanized cross-writing space instead of passing it silently
        # (R3/R5 evidence, see latin_space/_cross_evidence). A request with
        # no product noun at all - or a title with no evidence in any
        # script - stays "unverified": kept honestly, ranked last.
        if not any(
            query_noun_terms(request, other) for other in ("cyrillic", "latin")
        ):
            return "unverified"
        cross = _cross_evidence(product, request)
        if cross["leading"]:
            # Stage 10: cross-script head match gets the same subtype guard.
            return "mentioned" if subtype_conflict else "exact"
        if cross["noun"]:
            return "mentioned"
        if cross["any"]:
            # Modifier-only overlap across scripts: the spam signature
            # (a "portable" hair-brush for "portable nebulizer").
            return "noun_miss"
        return "unverified"

    haystack = haystack_of(product)
    leading = leading_noun(product.title, script)
    # EXACT verdict is intent-aware (Stage 3): a compatibility/excluded
    # target can never BE the product, while the presence check below
    # still sees every query word - such a title stays "mentioned", it is
    # never newly rejected. Empty intent -> byte-identical G24.6 path.
    product_nouns = (
        _intent_product_nouns(request, query_nouns)
        if structured
        else query_nouns
    )
    if leading is not None and any(
        nouns_match(leading, noun) for noun in product_nouns
    ):
        # G25.2-C Stage 10 false-EXACT guard: the head noun matches, but
        # the title DECLARES a subtype the request never asked for
        # («Промышленная камера МАШИННОГО ЗРЕНИЯ…» for «камера 4K для
        # машины») - a shared noun is not enough, so the candidate is kept
        # as "mentioned" (tier 1), never rejected, never tier 2.
        if subtype_conflict:
            return "mentioned"
        # G25.2-E false-EXACT guard: the requested noun occurs in the title
        # ONLY as a feature of another device («Подводный фонарь … с
        # повербанком», "Emergency radio with power bank", "Bubble machine
        # powered by a power bank") - a shared noun is not product identity,
        # so the candidate stays "mentioned" (tier 1), never tier 2.
        # Deterministic and list-free; no intent requirement, so both the
        # legacy and the structured paths are protected.
        if _title_mentions_request_as_feature(product.title, request):
            return "mentioned"
        # G25.2-C Stage 7 false-EXACT guard (structured intent only): a
        # noun whose FIRST occurrence sits right after «для»/«for» names
        # what the product is FOR, not what it IS («Крепление ДЛЯ
        # видеорегистратора», «Для АККУМУЛЯТОРА Makita») - such a title
        # stays "mentioned" and never earns tier 2 from that word.
        if not (structured and _after_target_intro(product.title, leading)):
            # Stage 39 conservative generic-container guard: when the title
            # LEADS with a container that is what matched the query, the
            # container alone must never award EXACT.
            if _leads_with_generic_container(leading, product_nouns):
                # (a) Container + audience/activity-only request
                # («Набор для рыбака»: product_type is a container, the only
                # other concept is an audience in compatibility, and NO
                # concrete product noun is named). The set's contents are
                # simply not stated, and a genuine set cannot be told from a
                # themed one without a lexicon - so a container-led
                # candidate stays a related (tier-1) result, never a false
                # EXACT. This replaces the Stage-35 title-position heuristic
                # that the Stage-36 live pool defeated (the audience word
                # appears in ordinary keyword-stuffed tail positions).
                if _is_container_audience_request(request, product_nouns):
                    return "mentioned"
                # (b) Container + concrete-noun query («набор инструментов»):
                # the container names a real product, so a themed/decorative
                # title that only mentions it behind a decoration introducer
                # stays related, while a genuine set keeps EXACT.
                if not _container_head_names_request(product.title, request):
                    return "mentioned"
            return "exact"
    if structured:
        # A latin-dominant title may still LEAD with the cyrillic product
        # word («Аккумулятор Makita», «Дворники Volkswagen Golf») and vice
        # versa - judge the other script's head too, but only when it
        # appears EARLIER in the title: «Ноутбук ... SSD» leads with the
        # laptop, so the mentioned SSD must never become "exact".
        lowered_title = (product.title or "").casefold()
        dominant_pos = (
            lowered_title.find(leading.casefold())
            if leading is not None
            else -1
        )
        for alt_script in ("cyrillic", "latin"):
            if alt_script == script:
                continue
            alt = leading_noun(product.title, alt_script)
            if alt is None or not any(
                nouns_match(alt, noun) for noun in product_nouns
            ):
                continue
            alt_pos = lowered_title.find(alt.casefold())
            if alt_pos < 0:
                continue
            if leading is None or dominant_pos < 0 or alt_pos < dominant_pos:
                if not _after_target_intro(product.title, alt):
                    # Stage 10: the cross-script head gets the same guard.
                    if subtype_conflict:
                        return "mentioned"
                    if _title_mentions_request_as_feature(product.title, request):
                        return "mentioned"
                    return "exact"
        # Stage 7: the title literally BEGINS with the requested product
        # type («КРЕПЛЕНИЕ для видеорегистратора» for pt=крепление) - the
        # requested role verified at the head position even when
        # noun_terms skips that word. Leading prepositions are never
        # skipped, so «Для аккумулятора...» does not qualify here.
        if _title_heads_product_type(product.title, request):
            # Stage 10: a declared unrequested subtype blocks this path too.
            if subtype_conflict:
                return "mentioned"
            if _title_mentions_request_as_feature(product.title, request):
                return "mentioned"
            return "exact"

    present = _product_noun_present(query_nouns, product.title, script)
    if not present:
        # Derived-adjective echo WITH corroboration («Дрель аккумуляторная
        # Makita 18V» for «аккумулятор для Makita 18V»): the title carries
        # a longer word on the request's root (the derived adjective) AND
        # the brand/spec the user wrote still occurs, so it is a RELATED
        # result (kept at tier 1 below), never junk. A bare echo with
        # nothing else in common («Телескопическая штанга» for «телескоп»)
        # stays rejected. Corroboration may also come from the request's
        # OWN intent phrases: the audience word roots the derived form
        # («рыбацкая лодка» under «Набор для рыбака», compat=['рыбака']),
        # while a request with no other concept («Домашний телескоп») has
        # nothing to corroborate and keeps rejecting the junk pool.
        # Stage 3G.1 (B): a hyphen-joined component («адаптер-зарядное»,
        # «OEM-зарядное») is ONE whitespace token, so its matching part
        # never started the stem check above. Split the title on non-word
        # characters (the same word shape text_matching.normalize_text
        # uses) - the minimum-root rule (word.startswith(stem)) and the
        # corroboration requirement below are untouched, so an arbitrary
        # substring inside the title still proves nothing on its own.
        # Stage 3G.3: the echo must NAME the product, not describe it.
        # It only counts inside the product-name phrase - touching a
        # title noun, chaining whitespace-only adjectives to one
        # («Аккумуляторная угловая шлифмашина»), or comma-isolated
        # («…, аккумуляторный, с рукояткой») - and never when a
        # support declaration precedes it («кейс …: поддержка …
        # зарядного устройства»). A feature mention («с быстрой
        # зарядкой», «Bluetooth-колонкой», «зарядкой PD3.1») can no
        # longer become product identity once a spec coincides («65W»,
        # «20W»): spec evidence must not compensate for the missing
        # product noun. See :func:`_type_position_derived_words`.
        title_noun_set = set(noun_terms(product.title, script))
        role_phrases = [
            phrase
            for phrase in (
                *request.intent_compatibility,
                *request.intent_excluded_product_types,
            )
            if phrase
        ]
        for noun in query_nouns:
            stem = (
                noun[:PREFIX_MATCH_LENGTH]
                if len(noun) >= PREFIX_MATCH_LENGTH else noun
            )
            derived_words = _type_position_derived_words(
                haystack, stem, noun, title_noun_set
            )
            if not derived_words:
                continue
            corroborated = _other_term_corroborated(
                product, request, query_nouns
            ) or any(
                _intent_role_match(word, phrase)
                for word in derived_words
                for phrase in role_phrases
            )
            if corroborated:
                present = True
                break
    if not present and leading is not None and _has_structured_intent(request):
        # Morphology-tolerant context evidence: the title leads with a
        # compatibility/excluded target («кот» for «кота», «чехол» for
        # «чехла») - it still MENTIONS the request, so it is kept at tier
        # 1 instead of being rejected by the noun gate.
        present = any(
            _intent_role_match(leading, phrase)
            for phrase in (
                *request.intent_compatibility,
                *request.intent_excluded_product_types,
            )
            if phrase
        )
    if not present:
        return "noun_miss"
    if script == "cyrillic":
        return "mentioned"

    for preposition in re.finditer(r"\b(?:for|of)\b", haystack):
        window = haystack[preposition.end() : preposition.end() + 60]
        if any(_term_matches(noun, window) for noun in query_nouns):
            return "mentioned"
    # A latin title may lead with the brand; the product noun still fills
    # the title. Without a dictionary the bot cannot tell a brand from an
    # accessory head, so it keeps the product and leaves it to R3 + rank.
    return "unverified"


# ---------------------------------------------------------------------------
# G25.2-B Stage 3: structured-intent ranking signals (additive, SEARCH only)
# ---------------------------------------------------------------------------
# The ``intent_*`` fields are produced by ``services.query_intent`` on the
# TEXT Find Product path only (Stage 2). This section NEVER filters - it
# adds a small, explainable adjustment on top of the existing word-share
# score, which ``services.comparison_service._sort_key`` already consumes
# as the ranking key right after the G24.6 relevance tier. Guards:
#   - mode must be PRODUCT_TYPE_MODE (SEARCH) - categories, photo, popular
#     and discovery requests are byte-identical to pre-Stage-3;
#   - every intent field empty -> adjustment is exactly 0.0.
# Every weight is a fraction of the existing 0..1 word-share scale, the
# total is clamped to [-1, +1], and ONLY contradicting evidence penalizes:
# a title that merely lacks an intent token is never punished (no G24.x
# over-filtering), and nothing here can remove a product from the pool.

#: The title LEADS with the requested product type (same evidence the
#: G24.6 classifier uses for "exact") - strongest positive role signal.
INTENT_PRODUCT_TYPE_BONUS = 0.30
#: The requested brand appears in the title (prefix/alias tolerant).
INTENT_BRAND_BONUS = 0.15
#: Title carries a KNOWN brand unrelated to an EXPLICIT (product-side)
#: requested brand - only applied when the brand is part of compatibility
#: («дворники Volkswagen»), never for implicit brands (Apple of an iPhone
#: query would wrongly penalize third-party chargers).
INTENT_BRAND_MISMATCH_PENALTY = -0.20
#: Share of a compatibility/model/generation phrase present in the title.
INTENT_DESIGNATION_WEIGHT = 0.35
#: The generation digit requested sits next to its family word.
INTENT_GENERATION_BONUS = 0.10
#: A DIFFERENT pure digit sits next to the family word («Golf 5» for 7).
INTENT_GENERATION_MISMATCH_PENALTY = -0.15
#: One matched required spec («18V», «1 ТБ»), at most two count.
INTENT_ATTRIBUTE_BONUS = 0.15
_INTENT_MAX_ATTRIBUTES = 2
#: G25.2-C Stage 11 (R2): one CONFIRMED explicit numeric spec («65 w»,
#: «20000 мач», «1 тб», «18 v»), at most two count - a small EXTRA on top
#: of the generic ``INTENT_ATTRIBUTE_BONUS`` above, so that between
#: otherwise comparable candidates (same tier, same word share, same
#: other signals) the one whose title actually carries the requested
#: number+unit leads. Deliberately tiny: below the generation bonus and
#: one matched query word of any realistic query, so it reorders ties
#: only - it can never lift a weaker tier above a stronger one (the tier
#: dominates in ``_sort_key``), never overturn primary relevance, and a
#: title that merely omits the spec is never penalized.
INTENT_NUMERIC_COVERAGE_BONUS = 0.05
_INTENT_MAX_NUMERIC_ATTRIBUTES = 2
#: An EXPLICIT numeric specification: a number bound to a unit word
#: («65 w», «20000 мач», «1 тб», «1.5 дюйма»). A bare number («20000»
#: alone, a model/generation digit) or a letter-only tech token («nvme»,
#: «usb-c», «ip68») is NOT a numeric spec and never earns the Stage-11
#: tie-break - the digit without its unit names no measurable attribute.
_NUMERIC_SPEC_RE = re.compile(r"^\d+(?:[.,]\d+)?\s*[a-zа-я]{1,6}$")
#: The title LEADS with a compatibility target: it IS that thing (the
#: phone for «зарядка для iPhone», the laptop for «SSD для ноутбука»),
#: not the requested product. Never mutates intent_product_type itself.
INTENT_COMPAT_TARGET_PENALTY = -0.45
#: The title LEADS with an explicitly excluded type («без чехла»).
INTENT_EXCLUDED_TYPE_PENALTY = -0.50
INTENT_ADJUSTMENT_MAX = 1.0
INTENT_ADJUSTMENT_MIN = -1.0


def _intent_phrase_tokens(phrase: str | None) -> list[str]:
    """Content tokens of an intent phrase; digits are KEPT («golf 7»)."""

    if not phrase:
        return []
    lowered = str(phrase).casefold().replace("ё", "е")
    return [
        token
        for token in re.findall(r"[0-9a-zа-я]+", lowered)
        if token not in _QUERY_FUNCTION_WORDS
    ]


def _intent_role_match(leading: str | None, phrase: str | None) -> bool:
    """Whether a title's leading noun IS the type named by ``phrase``.

    Candidates are BOTH the phrase's raw content words (an intent
    product type like «наушники» must match even though ``noun_terms``
    classifies «-ние» endings as adjectives) and its product nouns in
    either script, all compared with the inflection-aware
    :func:`nouns_match` plus a 3-character stem relation so the regular
    о/а alternation («чехол»/«чехла») still counts.
    """
    if not leading or not phrase:
        return False
    candidates = _intent_phrase_tokens(phrase)
    candidates.extend(noun_terms(phrase, "cyrillic"))
    candidates.extend(noun_terms(phrase, "latin"))
    for noun in candidates:
        if nouns_match(leading, noun):
            return True
        if len(leading) >= 3 and len(noun) >= 3 and leading[:3] == noun[:3]:
            return True
    return False


#: Known-brand words for the initial-alias rule («vw» <-> «volkswagen»).
_KNOWN_BRAND_SET = frozenset(KNOWN_BRANDS)


def _is_initial_alias(short: str, long_word: str) -> bool:
    """Whether the short form is the initial subsequence of the long one.

    «vw» walks v...w inside «volkswagen» - the only such pair the closed
    brand lexicon needs; arbitrary words never alias (both sides must be
    known brands, checked by the caller).
    """
    iterator = iter(long_word)
    return all(character in iterator for character in short)


def _brand_related(left: str | None, right: str | None) -> bool:
    """Whether two brand words denote the same maker («vw»/«volkswagen»)."""

    a = (left or "").casefold()
    b = (right or "").casefold()
    if not a or not b:
        return False
    if a == b or a.startswith(b) or b.startswith(a):
        return True
    if min(len(a), len(b)) < 3:
        if a in _KNOWN_BRAND_SET and b in _KNOWN_BRAND_SET:
            short, long_word = (a, b) if len(a) <= len(b) else (b, a)
            return _is_initial_alias(short, long_word)
        return a[:2] == b[:2]
    return a[:3] == b[:3]


def _title_brands(haystack: str) -> set[str]:
    """Known-brand words of the title (general lexicon, model-free)."""

    found: set[str] = set()
    for word in _WORD_RE.findall(haystack):
        for brand in KNOWN_BRANDS:
            if word.startswith(brand[:4]):
                found.add(brand)
                break
    return found


def _word_share_score(product: Product, request: SearchRequest) -> float:
    """How much of the request's product wording this title carries (0.0-1.0).

    A deterministic, model-free number: the share of the comparable request
    terms (see :func:`comparable_terms`) that occur in the title. It exists to
    let the ranking put RELEVANCE above popularity - a very popular listing
    that does not describe what the user asked for must not lead the feed.

    ``0.0`` means "no evidence", never "bad": for a cross-language request (or
    a feed without a product query) every product scores 0.0, so the ranking
    falls back to the marketplace order instead of inventing a difference.
    """

    if request.mode not in TOKEN_FILTER_MODES:
        return 0.0

    terms = comparable_terms(product.title, request)
    if terms:
        haystack = haystack_of(product)
        short_stem = _category_short_stems(request)
        matched = sum(
            1
            for term in terms
            if _term_matches(term, haystack, short_stem=short_stem)
        )
        return matched / len(terms)

    # Cross-writing score: judge the OTHER-script request words in the
    # romanized space, so a kept family result carries its evidence into
    # the ranking instead of tying at 0.0 with the unverifiable tail.
    title_script = dominant_script(product.title)
    if title_script is None:
        return 0.0
    cross_terms = [
        term for term in query_terms(request) if title_script not in scripts_of(term)
    ]
    if not cross_terms:
        return 0.0
    hay = latin_space(haystack_of(product))
    matched = sum(
        1 for term in cross_terms if _term_matches(latin_space(term), hay)
    )
    return matched / len(cross_terms)


def _family_word_hit(word: str, family: str) -> bool:
    """Whether a title token IS the family word (prefix-tolerant)."""

    if word == family:
        return True
    if len(family) >= PREFIX_MATCH_LENGTH:
        return word.startswith(family[:PREFIX_MATCH_LENGTH])
    return False


def _intent_spec_matches(attribute: str, haystack: str) -> bool:
    """Digit-boundary match of one required spec («1 ТБ» vs «11 ТБ»).

    Letter-only specs reuse the existing word-prefix rule («NVMe»);
    numeric specs use a digit-guarded pattern: the number may neither sit
    inside a longer number («218V» never answers «18V», «11 ТБ» never
    answers «1 ТБ») nor merge across punctuation («1.5 ТБ» is not «1 ТБ»).
    The romanized latin space («1 тб» -> «1 tb») is tried as well, so a
    Cyrillic spec can still answer a latin title.
    """
    if not attribute:
        return False
    forms = {str(attribute).casefold().replace("ё", "е")}
    # Character-wise transliteration (NOT latin_space, whose tokenizer
    # drops digits): «1 тб» must become «1 tb», keeping the number.
    forms.add(
        "".join(
            _CYR_TO_LAT.get(ch, ch)
            for ch in str(attribute).casefold().replace("ё", "е")
        )
    )
    for form in forms:
        parts = re.findall(r"[0-9a-zа-я]+", form)
        if not parts:
            continue
        if any(any(ch.isdigit() for ch in part) for part in parts):
            pattern = (
                r"(?<![\d.,])"
                + r"\s*".join(re.escape(part) for part in parts)
                + r"(?![\d])"
            )
            if re.search(pattern, haystack, re.IGNORECASE):
                return True
        elif _term_matches(form, haystack):
            return True
    return False


def _is_numeric_attribute(attribute: str | None) -> bool:
    """Whether a required attribute is an EXPLICIT numeric specification.

    True only for a number bound to a unit word («65 w», «20000 мач»,
    «1 тб», «1.5 дюйма») - the shape the G25.2 intent parser produces for
    real specs. A bare number («20000» alone) and a letter-only tech
    token («nvme», «usb-c») name no measurable attribute and return
    False: the digit without its unit is never treated as a numeric
    specification by the Stage-11 tie-break.
    """

    if not attribute:
        return False
    folded = str(attribute).casefold().replace("ё", "е").strip()
    return bool(_NUMERIC_SPEC_RE.match(folded))


def _numeric_attribute_coverage(
    attributes: Sequence[str | None], haystack: str
) -> int:
    """How many requested explicit numeric specs the title CONFIRMS.

    Conservative by construction: the very same digit-boundary matcher
    :func:`_intent_spec_matches` decides each hit, so «65W» and «65 W»
    count as one and the same spec while «265W», «650W» or a bare
    unrelated number never do. Different units are never equated and no
    conversion is invented. Absence of evidence returns a lower count,
    never a rejection - the caller only adds a bounded bonus.
    """

    if not haystack:
        return 0
    confirmed = sum(
        1
        for attribute in attributes
        if _is_numeric_attribute(attribute)
        and _intent_spec_matches(str(attribute), haystack)
    )
    return min(confirmed, _INTENT_MAX_NUMERIC_ATTRIBUTES)


def confirms_numeric_spec(product: Product, request: SearchRequest) -> bool:
    """Whether the title CONFIRMS at least one requested explicit numeric spec.

    Written for the Stage-12 pool protection (:mod:`services.search_service`),
    and delegating the whole decision to the Stage-11 matcher -
    :func:`_is_numeric_attribute` + :func:`_numeric_attribute_coverage` over
    :func:`haystack_of` - so the score tie-break and the pool rescue always
    agree and no second numeric parser ever exists: «65W» ≡ «65 W»,
    «265W»/«650W» and a bare number never qualify, different units are never
    equated and no conversion is invented.

    ``False`` means only "no positive numeric evidence": a request without a
    structured numeric attribute answers False for every product, and the
    caller uses this to RESCUE already-eligible candidates past a truncation
    cut - never to reject, demote or filter anything.
    """

    attributes = request.intent_required_attributes
    if not any(_is_numeric_attribute(attribute) for attribute in attributes):
        return False
    return _numeric_attribute_coverage(attributes, haystack_of(product)) > 0


def intent_score_adjustment(product: Product, request: SearchRequest) -> float:
    """Bounded additive intent signal for :func:`relevance_score`.

    Returns exactly ``0.0`` for non-SEARCH modes and for requests whose
    intent fields are all empty - every pre-Stage-3 request and every
    category/photo/popular/discovery request ranks byte-identically to
    before. Only contradicting evidence penalizes: a title that merely
    lacks an intent token is never punished, and nothing is ever removed
    from the result pool.
    """
    if request.mode is not PRODUCT_TYPE_MODE:
        return 0.0
    pt = request.intent_product_type
    brand = (
        str(request.intent_brand).casefold()
        if request.intent_brand
        else None
    )
    model = request.intent_model
    generation = request.intent_generation
    attrs = request.intent_required_attributes
    compat = request.intent_compatibility
    excluded = request.intent_excluded_product_types
    if not any((pt, brand, model, generation, attrs, compat, excluded)):
        return 0.0

    haystack = haystack_of(product)
    if not haystack:
        return 0.0
    adjustment = 0.0

    # Leading nouns of the title in BOTH scripts: a latin-dominant title
    # still has its cyrillic head («Аккумулятор Makita» -> «аккумулятор»)
    # and vice versa, so a brand word can never hide the product role.
    leadings: list[str] = []
    for script_name in ("cyrillic", "latin"):
        noun = leading_noun(product.title, script_name)
        if noun and noun not in leadings:
            leadings.append(noun)

    excluded_hit = bool(leadings) and any(
        _intent_role_match(noun, phrase)
        for noun in leadings
        for phrase in excluded
        if phrase
    )
    pt_hit = bool(pt) and bool(leadings) and any(
        _intent_role_match(noun, pt) for noun in leadings
    )
    compat_hit = bool(leadings) and any(
        _intent_role_match(noun, phrase)
        for noun in leadings
        for phrase in compat
        if phrase
    )

    # 1. Role signals, strongest first. An EXCLUDED type wins outright
    #    (and suppresses the product bonus: «без чехла» pollutes the raw
    #    product phrase with «чехла», but the exclusion is explicit).
    if excluded_hit:
        adjustment += INTENT_EXCLUDED_TYPE_PENALTY
    elif pt_hit:
        adjustment += INTENT_PRODUCT_TYPE_BONUS

    # 2. A title that LEADS with a compatibility target IS that target (the
    #    phone for «зарядка для iPhone», the laptop for «SSD для ноутбука»,
    #    the cat for «подарок для кота») - never the requested product -
    #    unless it ALSO leads with the product itself («Аккумулятор Makita»
    #    leads with the brand word on its latin side). intent_product_type
    #    itself is not read or mutated by the matching.
    if compat_hit and not pt_hit and not excluded_hit:
        adjustment += INTENT_COMPAT_TARGET_PENALTY

    # 3. Compatibility / model / generation designations: the best share
    #    of one designation phrase present in the title.
    designation_phrases = [phrase for phrase in compat if phrase]
    if model:
        designation_phrases.append(str(model))
    if generation:
        designation_phrases.append(str(generation))
    best_fraction = 0.0
    family_words: list[str] = []
    intent_digits: set[str] = set()
    for phrase in designation_phrases:
        tokens = _intent_phrase_tokens(phrase)
        if not tokens:
            continue
        matched = sum(
            1 for token in tokens if _term_matches(token, haystack)
        )
        best_fraction = max(best_fraction, matched / len(tokens))
        family_words.extend(t for t in tokens if not t.isdigit())
        intent_digits.update(t for t in tokens if t.isdigit())
    if best_fraction:
        adjustment += INTENT_DESIGNATION_WEIGHT * best_fraction

    # 4. Generation digits next to the family word: the requested digit is
    #    positive evidence, a DIFFERENT pure digit («Golf 5» for Golf 7) is
    #    contradicting. Unit numbers («65W») are never pure digits here.
    if intent_digits and family_words:
        stream = re.findall(r"[0-9a-zа-я]+", haystack)
        digit_bonus = False
        digit_penalty = False
        for index, token in enumerate(stream[:-1]):
            if not any(_family_word_hit(token, w) for w in family_words):
                continue
            neighbour = stream[index + 1]
            if not neighbour.isdigit():
                continue
            if neighbour in intent_digits:
                digit_bonus = True
            else:
                digit_penalty = True
        if digit_bonus:
            adjustment += INTENT_GENERATION_BONUS
        if digit_penalty:
            adjustment += INTENT_GENERATION_MISMATCH_PENALTY

    # 5. Required attributes: positive evidence only (absence is neutral).
    if attrs:
        matched_attrs = sum(
            1
            for attribute in attrs
            if attribute and _intent_spec_matches(attribute, haystack)
        )
        adjustment += INTENT_ATTRIBUTE_BONUS * min(
            matched_attrs, _INTENT_MAX_ATTRIBUTES
        )
        # Stage 11 (R2): an EXTRA for every confirmed EXPLICIT numeric
        # spec («65 w», «20000 мач») - the evidence-based tie-break
        # between otherwise comparable candidates. Only digits bound to
        # a unit word count; a bare number or a letter-only tech token
        # adds nothing, and a title without the spec is never penalized.
        adjustment += INTENT_NUMERIC_COVERAGE_BONUS * _numeric_attribute_coverage(
            attrs, haystack
        )

    # 6. Brand: presence is a bonus in every case; a MISMATCH is penalized
    #    only for an EXPLICIT product-side brand (part of compatibility:
    #    «дворники Volkswagen ...»). An implicit brand (Apple derived from
    #    an iPhone query) must never punish third-party chargers.
    if brand:
        title_brands = _title_brands(haystack)
        if _term_matches(brand, haystack) or any(
            _brand_related(found, brand) for found in title_brands
        ):
            adjustment += INTENT_BRAND_BONUS
        compat_tokens = [
            token
            for phrase in compat
            for token in _intent_phrase_tokens(phrase)
        ]
        explicit = any(_brand_related(brand, t) for t in compat_tokens)
        if explicit:
            stray = [
                found
                for found in title_brands
                if not _brand_related(found, brand)
                and not any(
                    _brand_related(found, t) for t in compat_tokens
                )
            ]
            if stray:
                adjustment += INTENT_BRAND_MISMATCH_PENALTY

    return max(
        INTENT_ADJUSTMENT_MIN,
        min(INTENT_ADJUSTMENT_MAX, round(adjustment, 4)),
    )


def relevance_score(product: Product, request: SearchRequest) -> float:
    """Ranking score: word-share (0..1) PLUS the bounded intent adjustment.

    The base is the untouched G24.6/G24.8 word-share computed by
    :func:`_word_share_score`. G25.2-B Stage 3 adds
    :func:`intent_score_adjustment` - a deterministic, explainable sum of
    product-type / brand / model / generation / attribute / compatibility
    bonuses and contradicting-evidence penalties. With empty intent fields
    (every non-text mode, every pre-Stage-3 request) the adjustment is
    exactly ``0.0`` and the base value is returned UNCHANGED, byte for
    byte. Nothing here filters: the pool is still decided solely by
    :func:`filter_relevant`.
    """
    base = _word_share_score(product, request)
    adjustment = intent_score_adjustment(product, request)
    if adjustment == 0.0:
        return base
    return round(base + adjustment, 4)


def relevance_tier(product: Product, request: SearchRequest) -> int:
    """Stage 5: how certainly this candidate IS the requested product.

    ``2`` - the product-type gate verified the head noun (exact product or
    a close variant); ``1`` - only the word-level R3 matched (a category
    keyword, or a case the gate could not decide); ``0`` - no comparable
    evidence at all. Ranking puts the tiers ABOVE every popularity signal,
    so a product with 10 000 orders can never outrank a verified product
    type with 10 orders - while within one tier the real marketplace
    signals still order the candidates.
    """

    if request.mode not in TOKEN_FILTER_MODES:
        return 0
    if request.mode is PRODUCT_TYPE_MODE and classify_product(
        product, request
    ) == "exact":
        return 2
    # G25.2-D (O2): a rejected spare part must never outrank a kept product -
    # a «редуктор для дрели» the request never asked for is tier 0, below
    # every verified result, so ranking cannot lift it back into view.
    if rejection_reason(product, request) is not None:
        return 0

    comparable = comparable_terms(product.title, request)
    if not comparable:
        # Cross-writing product evidence ranks as word-level evidence: a
        # kept family result («небулайзер" for "nebulizer") must sort at
        # tier 1 - below every exact match, above the unverifiable tail.
        evidence = _cross_evidence(product, request)
        return 1 if (evidence["noun"] or evidence["leading"]) else 0
    haystack = haystack_of(product)
    short_stem = _category_short_stems(request)
    if not any(_term_matches(term, haystack, short_stem=short_stem) for term in comparable):
        return 0
    # Stage 62: a compatibility-only match (words of a compatibility/excluded
    # target, but no PRODUCT noun of the title's script) is kept honestly at
    # tier 0 - below every product sharing the requested role, never rejected.
    # Requires structured intent: without ``intent_*`` fields there is no
    # compatibility to reason about and the legacy tier-1 contract is kept.
    if _has_structured_intent(request):
        script = dominant_script(product.title)
        if script is not None and not _has_product_role_evidence(
            product.title, request, script
        ):
            return 0
    return 1


def _is_excluded_product_type(product: Product, request: SearchRequest) -> bool:
    """Stage 3A: whether the title IS a product type the user excluded.

    ``intent_excluded_product_types`` used to reach ONLY
    :func:`intent_score_adjustment` (``INTENT_EXCLUDED_TYPE_PENALTY``), so a
    «Чехол для наушников» listing under «наушники без чехла» stayed in the
    pool at tier 1 and merely ranked lower. The same deterministic evidence
    the penalty uses - the HEAD noun of BOTH writing systems against
    :func:`_intent_role_match` - now gates :func:`rejection_reason`.

    Only STRONG evidence may shrink the pool:

    - structured intent + :data:`PRODUCT_TYPE_MODE` only: every other mode
      and every request without ``intent_*`` fields stays byte-identical;
    - the HEAD noun decides: «Чехол для наушников» IS a case and is
      rejected, while «Кабель для наушников» (a compatible accessory of the
      wanted family) and «Пылесос с мешком» (the requested product merely
      MENTIONING the excluded word) keep the R5 family/accessory protection;
    - ambiguity tolerance (the Stage-2 ``currency is None`` precedent): when
      the same head noun is ALSO the requested product type, brand, model,
      generation or compatibility target, the evidence contradicts itself -
      «Samsung Galaxy» under «наушники без чехла Samsung» is never
      hard-rejected, the ranking penalty still expresses the doubt. The
      product-type phrase is read WITHOUT the exclusion words first («без
      чехла» pollutes the raw phrase exactly as :func:`_intent_product_nouns`
      documents), so «телефон, не нужен планшет» still rejects the tablet.
    """

    if request.mode is not PRODUCT_TYPE_MODE:
        return False
    excluded = [
        phrase for phrase in request.intent_excluded_product_types if phrase
    ]
    if not excluded:
        return False

    # Head nouns of BOTH scripts - the exact evidence the penalty uses, so
    # the excluded type cannot hide behind the other writing system
    # («Аккумулятор Makita» has «аккумулятор» on its cyrillic side and vice
    # versa).
    leadings: list[str] = []
    for script_name in ("cyrillic", "latin"):
        noun = leading_noun(product.title, script_name)
        if noun and noun not in leadings:
            leadings.append(noun)
    if not leadings:
        return False
    if not any(
        _intent_role_match(noun, phrase)
        for noun in leadings
        for phrase in excluded
    ):
        return False

    # Ambiguity guard: the requested role of the query, minus the exclusion
    # words that pollute the raw phrase («телефон, НЕ НУЖЕН ПЛАНШЕТ» guards
    # only «телефон»), plus the brand/model/generation/compatibility of the
    # request - a head naming THOSE is ambiguous evidence, never a proven
    # prohibited type.
    cleaned_type = " ".join(
        token
        for token in _intent_phrase_tokens(request.intent_product_type)
        if not any(_intent_role_match(token, phrase) for phrase in excluded)
    )
    guard_phrases = [
        phrase
        for phrase in (
            cleaned_type or None,
            request.intent_brand,
            request.intent_model,
            request.intent_generation,
            *request.intent_compatibility,
        )
        if phrase
    ]
    return not any(
        _intent_role_match(noun, phrase)
        for noun in leadings
        for phrase in guard_phrases
    )


def rejection_reason(product: Product, request: SearchRequest) -> str | None:
    """Why this product must not be shown, or ``None`` when it is acceptable.

    The returned string is a short machine-readable reason used in logs, so
    a filtering decision can always be explained after the fact.

    Stages, in order: R1 URL, R2 title, R3 evidence of the request - the
    title's own language first, then the romanized cross-writing space
    (``latin_space``); only when BOTH find nothing does
    ``no_query_term_in_title`` fire. Then the product-type gate (R5): a
    title that LEADS with the requested product is exact; a title that
    merely MENTIONS it (related product / accessory / same use case) is
    KEPT - ranked below exact matches by ``relevance_tier`` - and only a
    modifier-only overlap (``noun_miss``: adjective/«portable»/«led»
    word salad) is rejected as ``no_product_noun_in_title``. On a structured
    SEARCH request a title whose HEAD noun IS an explicitly excluded
    product type is rejected earlier still, as ``excluded_product_type``
    (Stage 3A: :func:`_is_excluded_product_type` owns the accessory and
    ambiguity guards, so everything else keeps its previous fate).
    """

    if not has_valid_product_url(product):
        return "invalid_product_url"

    if not has_meaningful_title(product):
        return "empty_title"

    if request.mode not in TOKEN_FILTER_MODES:
        return None

    # Stage 3A (hard exclusion): a title that IS an explicitly excluded
    # product type never enters the pool - «наушники без чехла» must not
    # SHOW «Чехол для наушников», however well it would rank. Structured
    # intent + PRODUCT_TYPE_MODE only; the helper owns the accessory and
    # ambiguity guards, so everything else keeps its previous fate.
    if _is_excluded_product_type(product, request):
        return "excluded_product_type"

    # The title is written in the language the API was asked for (Russian),
    # so the ordinary comparison stays per writing system. A title whose own
    # language finds NO request word is rejected only when the romanized
    # cross-writing comparison (other-script request words vs latin_space)
    # also finds nothing: then the two sides share no word in ANY script and
    # no family evidence exists (the donut-toy junk of «небулайзер»).
    comparable = comparable_terms(product.title, request)
    haystack = haystack_of(product)
    short_stem = _category_short_stems(request)
    own_match = bool(comparable) and any(
        _term_matches(term, haystack, short_stem=short_stem) for term in comparable
    )

    if comparable and not own_match:
        if not _cross_evidence(product, request)["any"]:
            return "no_query_term_in_title"
    elif not comparable:
        # No request word in the title's language at all. Cross-writing
        # evidence («небулайзер" vs "nebulizer") judges when present; with
        # none the languages truly cannot be compared - the bot keeps the
        # listing honestly as tier-0 "unverified" (counted in
        # filter_relevant's log) instead of hiding it on a guess.
        if not _cross_evidence(product, request)["any"]:
            return None

    verdict = classify_product(product, request)
    # G25.2-D (O2): a spare part for the requested COMPLETE product (the
    # Stage-48 gearbox «…для дрели-шуруповерта») shares the head noun and so
    # survives the noun gate as "mentioned"/tier-1. When the request never
    # asked for a part, drop it explicitly instead of showing a component as
    # if it were the product; a request that names the part is untouched.
    if _is_spare_part_leak(product.title, request):
        return "spare_part_not_requested_product"
    if verdict == "mentioned":
        # Related product / same family / accessory / same use case:
        # KEPT, ranked below every exact match (module docstring R5/R6).
        return None
    if verdict == "noun_miss":
        # Modifier-only overlap is the keyword-spam signature
        # («механический коврик», «… LED-дисплеем», "portable" hair-brush).
        return "no_product_noun_in_title"
    return None


def filter_relevant(
    products: Sequence[Product], request: SearchRequest
) -> tuple[list[Product], dict[str, int]]:
    """Return the products that may be shown, plus per-reason drop counts.

    The counts are returned (not logged away) so the caller can report how
    many results were hidden and why - a filter that silently eats results
    is impossible to review. Titles kept without ANY comparable evidence
    (cross-language unverified, tier 0) are counted into the log for the
    same reason: they are shown honestly, ranked last, never unnoticed.
    """

    kept: list[Product] = []
    dropped: dict[str, int] = {}
    unverified_language = 0

    for product in products:
        reason = rejection_reason(product, request)
        if reason is None:
            if (
                request.mode in TOKEN_FILTER_MODES
                and not comparable_terms(product.title, request)
                and not _cross_evidence(product, request)["any"]
            ):
                unverified_language += 1
            kept.append(product)
            continue
        dropped[reason] = dropped.get(reason, 0) + 1

    if unverified_language:
        logger.info(
            "Relevance kept %d title(s) with no request word in their "
            "language and no romanized match (cross-language unverified, "
            "tier 0, ranked below every judged result)",
            unverified_language,
        )

    return kept, dropped

