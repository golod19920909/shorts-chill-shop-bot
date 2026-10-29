"""Grounded retrieval variants for a PRODUCT search.

Why this module exists: asking a marketplace with ONE narrow phrase is a
weak way to look for a product a human described in several words. A
descriptive request («брелок в виде строительного инструмента», «мешок для
стирки обуви») reaches ``aliexpress.affiliate.product.query`` as one long
phrase, the full-text index answers a thin, fuzzy set, the relevance gate
then correctly drops everything that is not the requested product - and the
user is left with a handful of cards instead of a pool to browse.

What this module does instead: it derives a small, BOUNDED set of extra
retrieval phrases from the request itself.

Rules - all deterministic, no model, no synonyms, no product lists:

- the FIRST variant is always the phrase the parser built
  (``normalized_query``), so the existing primary query is untouched;
- every further variant is built ONLY from the product nouns the USER
  wrote (``services.relevance.noun_terms``), in the order the user wrote
  them («брелок инструмент»). Adjectives, function words, prices and
  quality phrases never become a retrieval phrase;
- a request that names only ONE product noun has no noun phrase to vary, so
  it is broadened with the USER'S OWN WORDING instead
  (:func:`_own_phrase`): the parser may have translated the request, or
  inflated it with words the user never wrote, and the exact phrase the user
  typed is then a genuinely different - and BROADER - question for the
  marketplace index. It is added only when it really is broader
  (:func:`_is_broader_than`: no more words than the phrase already sent) and
  does not already carry the parser's wording
  (:func:`_adds_a_different_question`), so a faithful translation or a
  request whose two phrases say the same thing still costs exactly one API
  call;
- phrases are never reduced to a single generic noun. «полотно для
  лобзика» must not turn into «полотно» (fabric) and «машина для стрижки»
  must not turn into «машина» (car) - a shorter phrase is only used while
  it still names at least :data:`MIN_NOUNS_FOR_VARIANT` product nouns;
- the SECOND variant is always the CORE pair (the head noun plus the noun
  that follows it, in the user's own order) - the broadest phrase that still
  names a product. It is generated before the longer near-copies of the
  parser's phrase, because the list is capped and the loop stops as soon as
  the pool is full: generation used to walk prefixes from the longest phrase
  down, so a request naming four or more nouns spent the whole budget on
  phrases almost identical to the one already sent and never asked the broad
  core question at all;
- duplicates are dropped case-insensitively and the list is capped at
  :data:`MAX_PRODUCT_QUERY_VARIANTS`;
- a phrase that MIXES writing systems («led лампа») additionally asks its
  DOMINANT-script content words as one homogeneous question
  (:func:`_script_homogeneous_phrase`): the live marketplace index answers
  a mixed-script ``keywords`` string far worse than either language alone
  (2026, this app key: «led лампа» → 6/50 product hits, «лампа» → 49/50),
  and the homogeneous phrase contains only words the user actually wrote,
  so it can never invent a topic. The mix - not the wording - is what
  broke the lookup, so this variant is not measured by the
  "different question" coverage test below.

Nothing here invents a word: a variant is always a subset of the user's own
product nouns, so it can never retrieve a product the user did not
describe. The candidates every variant returns are merged and then judged
by the SAME relevance gate as the primary query (``services.relevance``) -
extra recall never buys extra tolerance.
"""

from __future__ import annotations
import re

from collections.abc import Sequence

from models.search_request import SearchMode, SearchRequest
from services.relevance import (
    MIN_TERM_LENGTH,
    PREFIX_MATCH_LENGTH,
    STOPWORDS,
    dominant_script,
    query_noun_terms,
    scripts_of,
    tokenize,
)
from services.text_matching import script_of, truncate_provider_phrase

#: Hard cap on how many marketplace queries ONE product search may cost.
#: Bounded on purpose: every variant is one documented
#: ``product.query`` call, so a search stays inside the affiliate rate
#: limits whatever the user typed.
MAX_PRODUCT_QUERY_VARIANTS = 3

#: How many product nouns a shortened phrase must still carry. One noun
#: alone is a different, far broader product («полотно», «машина»), so a
#: variant never goes below two.
MIN_NOUNS_FOR_VARIANT = 2

#: How much of the user's own wording the parser's phrase must already carry
#: for asking that wording again to be pointless. Below this share the extra
#: phrase asks the marketplace index a genuinely different question (the
#: parser translated the request, or added words the user never wrote), so it
#: is worth one bounded call; at or above it the extra phrase is a near-copy
#: of the phrase already sent and is skipped.
_OWN_PHRASE_COVERAGE = 0.5

#: Four-character stems of words that carry no product meaning at all -
#: prices, ratings, orders, quantity (``services.relevance.STOPWORDS``).
#: They are stripped from a retrieval phrase: «рейтингом» inside a query
#: narrows the marketplace index instead of broadening it, and the user's
#: quality wish is already handled by ranking (``prefers_good_rating``).
_NON_PRODUCT_STEMS = frozenset(
    word[:4] for word in STOPWORDS if len(word) >= 4
)

#: Request / comparative wording: it frames a follow-up, it never names a
#: product. A retrieval phrase containing one is not a marketplace question.
_SENTENCE_WORDING_STEMS: tuple[str, ...] = (
    "сделай", "подешевле", "подешевле", "покажи", "покажите", "найди",
    "найдите", "дай", "дайте", "пожалуйста", "хочу", "cheaper", "show",
    "find", "give",
)

def _drop_sentence_variants(variants: list[str]) -> list[str]:
    """Drop variants that only repeat the user's SENTENCE.

    «сделай подешевле шуруповерт» carries request wording the product phrase
    does not: asking the marketplace for it costs a call and can only return
    nothing relevant. The primary product phrase always survives.
    """

    clean = [variant for variant in variants if _sentence_wording_free(variant)]
    return clean or variants[:1]

def _sentence_wording_free(phrase: str | None) -> bool:
    """Whether ``phrase`` contains no request/comparative wording."""

    import re as _re

    words = {
        word.casefold().replace("ё", "е")
        for word in _re.findall(r"[^\W\d_]+", phrase or "")
    }
    return not any(
        word.startswith(stem) for word in words for stem in _SENTENCE_WORDING_STEMS
    )

def _has_product_word(text: str | None) -> bool:
    """Whether ``text`` carries a content word of its own.

    «сделай подешевле» is pure request/modifier wording - it must never become
    a marketplace question. A genuine other product («а плазморезка?») carries
    one and keeps its existing variant behaviour.
    """

    from services.conversation import _content_words

    return bool(_content_words(text))

def _excluded_stems(request: SearchRequest) -> frozenset[str]:
    """Prefix-stems of the product types the user EXCLUDED (В«Р±РµР· XВ»).

    Shared by the noun-phrase guard (Stage 16 A-3) and the short/one-noun
    guard (Stage 25) so both drop the SAME excluded stems and can never
    disagree about what the excluded product type is. Empty when the
    request excludes nothing, which is the common case.
    """

    if not request.intent_excluded_product_types:
        return frozenset()
    return frozenset(
        token.casefold()[:PREFIX_MATCH_LENGTH]
        for phrase in request.intent_excluded_product_types
        for token in tokenize(phrase)
        if len(token) >= MIN_TERM_LENGTH
    )

def _reintroduces_excluded(
    phrase: str, excluded_stems: frozenset[str]
) -> bool:
    """Whether a DERIVED phrase names an excluded type positively.

    The primary (parser) phrase carries В«Р±РµР·В» and is never passed here. A
    derived variant is built from the user's own CONTENT words only, which
    never include the function word В«Р±РµР·В» - so an excluded stem appearing
    in a derived phrase is, by construction, the exact opposite of the
    request (В«РїС‹Р»РµСЃРѕСЃ Р±РµР· РјРµС€РєР° BoschВ» в†’ В«РїС‹Р»РµСЃРѕСЃ РјРµС€РєР°В»). This is the
    short/one-noun counterpart of the Stage 16 A-3 noun guard: it only
    fires when an exclusion exists, and never drops a variant that keeps
    the excluded type out.
    """

    if not excluded_stems:
        return False
    return any(
        word.casefold()[:PREFIX_MATCH_LENGTH] in excluded_stems
        for word in tokenize(phrase)
        if len(word) >= MIN_TERM_LENGTH
    )

def product_query_variants(request: SearchRequest) -> list[str]:
    """Retrieval phrases for ``request``, most specific first.

    The first element is the parser's own phrase. Additional elements are
    built from the user's own words only: either the product-noun phrases
    (:func:`_noun_phrases`, a request that names at least
    :data:`MIN_NOUNS_FOR_VARIANT` products) or - for a short/one-noun request -
    the user's own wording when the parser's phrase does not already carry it
    (:func:`_own_phrase`). A request that says the same thing in both phrases
    keeps its single query, so the common case costs no extra API call.

    Only :attr:`~models.search_request.SearchMode.SEARCH` is broadened: a
    category or a feed is not one product query (their breadth comes from
    the catalogue/feed pipeline, and splitting a category into per-type
    phrases would invent a request the user never made).
    """

    primary = " ".join((request.normalized_query or request.original_query or "").split())
    variants: list[str] = [primary] if primary else []

    if request.mode is not SearchMode.SEARCH:
        return _drop_sentence_variants(variants)

    script = dominant_script(request.original_query or primary)
    if script is None:
        return _drop_sentence_variants(variants)

    nouns = [
        word
        for word in query_noun_terms(request, script)
        if word[:4] not in _NON_PRODUCT_STEMS
    ]
    # Stage 16 (A-3): a noun the user EXCLUDED («без X») must never become a
    # positive retrieval phrase. «пылесос без мешка» produced the variant
    # «пылесос мешка» — the exact opposite of the request — because
    # query_noun_terms treats «мешка» as a product noun and «без» is a
    # function word. Dropping excluded-type nouns here OMITS the unsafe
    # variant (the primary phrase, with «без» intact, is never touched); it
    # does not replace it with an invented opposite, and it does not change
    # downstream filtering (intent_excluded_product_types keeps its meaning).
    if request.intent_excluded_product_types:
        excluded_stems = _excluded_stems(request)
        nouns = [
            word for word in nouns
            if word.casefold()[:PREFIX_MATCH_LENGTH] not in excluded_stems
        ]
    else:
        excluded_stems = frozenset()
    if len(nouns) >= MIN_NOUNS_FOR_VARIANT:
        for phrase in _noun_phrases(nouns):
            if len(variants) >= MAX_PRODUCT_QUERY_VARIANTS:
                break
            if phrase and not _already_present(phrase, variants):
                variants.append(phrase)
        return _drop_sentence_variants(variants)

    # SHORT / ONE-NOUN QUERY: there is no second noun phrase to derive, yet the
    # request still has a second GROUNDED representation whenever the parser
    # did not simply repeat the user's own words. The extra phrase is asked
    # only when it is BROADER (no longer than what the parser already sent)
    # and genuinely different - a translation of the same length or an
    # inflated phrasing is not a second question, it is the same lookup in
    # other words. It adds no word of its own, so it can never invent a
    # request the user did not make.

    # MIXED-SCRIPT PHRASE FIRST: when either representation mixes two
    # writing systems, its dominant-script content words become their own
    # homogeneous question (see :func:`_script_homogeneous_phrase` for the
    # live numbers). Language mixing - not wording - is what breaks the
    # lookup, so this variant is added on the mix alone.
    for source in (primary, request.original_query or ""):
        if len(variants) >= MAX_PRODUCT_QUERY_VARIANTS:
            break
        homogeneous = _script_homogeneous_phrase(source)
        if (
            homogeneous
            and not _already_present(homogeneous, variants)
            and not _reintroduces_excluded(homogeneous, excluded_stems)
        ):
            variants.append(homogeneous)

    if len(variants) < MAX_PRODUCT_QUERY_VARIANTS and _has_product_word(
        request.original_query
    ):
        own = _own_phrase(request.original_query)
        if (
            own
            and not _already_present(own, variants)
            and _is_broader_than(own, primary)
            and _adds_a_different_question(own, primary)
            and not _reintroduces_excluded(own, excluded_stems)
        ):
            variants.append(own)

    # Final guard: a variant built from the user's SENTENCE can carry request
    # wording («сделай», «подешевле», «покажи») that the product phrase does
    # not. Asking the marketplace for it costs a call and can only return
    # nothing relevant, so such a variant is dropped (the primary product
    # phrase always survives).
    clean = [variant for variant in variants if _sentence_wording_free(variant)]
    return clean or variants[:1]

def _script_homogeneous_phrase(text: str | None) -> str:
    """Dominant-script content words of ``text`` when it MIXES scripts.

    Returns ``""`` for a single-script or letterless phrase (the common
    case), so a homogeneous request costs no extra API call. When the
    phrase really mixes two writing systems («led лампа»), the live
    marketplace index answers it far worse than either language alone
    (2026, this app key: «led лампа» → 6/50 product hits, the homogeneous
    «лампа» → 49/50), so the dominant-script content words are worth one
    bounded, homogeneous question. Every word comes from the phrase
    itself - nothing is translated, added or invented.
    """

    if not text or len(scripts_of(text)) < 2:
        return ""
    script = dominant_script(text)
    if script is None:  # an exact tie: no honest dominant language
        return ""
    return " ".join(
        word
        for word in _searchable_words(text)
        if word[:4] not in _NON_PRODUCT_STEMS and script_of(word) == script
    )

def _own_phrase(text: str | None) -> str:
    """The user's own content words, in their own order, as a retrieval phrase.

    Read with the same rules the relevance gate uses
    (``services.relevance``): function words, price/rating wording, digits and
    anything shorter than :data:`~services.relevance.MIN_TERM_LENGTH` are
    dropped, and NOTHING is added. Lower-casing and the ё/е fold come from
    :func:`~services.relevance.tokenize` and only make the phrase comparable -
    a marketplace full-text index is case-insensitive and treats «щётка» and
    «щетка» as one word.

    Stage 3F: the length fit uses
    :func:`~services.text_matching.truncate_provider_phrase` instead of a
    blind ``[:100]`` slice, so a long own-wording keeps its tail (the model
    or qualifier words a head slice drops) while staying within the same
    provider limit.
    """

    return truncate_provider_phrase(" ".join(_searchable_words(text))).strip()

def _searchable_words(phrase: str | None) -> list[str]:
    """The words of ``phrase`` a marketplace query really carries.

    The same filter for both sides of a comparison: what the user wrote and
    what the parser sent are measured by the CONTENT words they contribute,
    not by their function words or their length on the screen.
    """

    return [
        word
        for word in tokenize(phrase)
        if len(word) >= MIN_TERM_LENGTH and word not in STOPWORDS
    ]

def _is_broader_than(own: str, primary: str) -> bool:
    """Whether ``own`` is a BROADER question than ``primary``.

    Broadening is the entire point of an extra phrase, and word count is the
    only honest measure of it available without a model: the user's own
    wording feeds the marketplace index FEWER (or equally many) content words
    than the phrase the parser already sent. A LONGER phrasing is a narrower
    question - asking it adds no depth the first call did not have, so it is
    skipped and the common case still costs exactly one API call.
    """

    return len(_searchable_words(own)) <= len(_searchable_words(primary))

def _adds_a_different_question(own: str, primary: str) -> bool:
    """Whether asking ``own`` can answer something ``primary`` did not.

    Words are compared by their first
    :data:`~services.relevance.PREFIX_MATCH_LENGTH` characters - the same
    tolerant prefix rule the relevance gate uses for Russian endings - so
    «термос чая» and «термос чай» count as ONE question (the parser's phrase
    already carries it), while a translated or inflated phrase counts as a
    different one and is worth one bounded call.
    """

    own_words = _comparable_words(own)
    primary_words = _comparable_words(primary)
    if not own_words or not primary_words:
        return False

    shared = len(own_words & primary_words) / len(own_words)
    return shared < _OWN_PHRASE_COVERAGE

def _comparable_words(phrase: str) -> frozenset[str]:
    """Prefix-folded word set of a retrieval phrase (morphology tolerant)."""

    return frozenset(word[:PREFIX_MATCH_LENGTH] for word in tokenize(phrase))

def _noun_phrases(nouns: Sequence[str]) -> list[str]:
    """The user's own nouns as retrieval phrases, the CORE pair first.

    Order is the whole point, because the caller CAPS the variant list and
    stops as soon as the relevant pool is full: the longest phrase (a near
    copy of what the parser already sent) comes first, then the core pair -
    the broadest phrase that still names a product instead of a single
    generic noun - then the intermediate lengths, broadest first. Generation
    used to be a plain prefix walk (longest downwards), so a request naming
    four or more product nouns never reached its own core phrase at all.
    """

    phrases = [" ".join(nouns)]
    core = " ".join(nouns[:MIN_NOUNS_FOR_VARIANT])
    if core not in phrases:
        phrases.append(core)

    for length in range(MIN_NOUNS_FOR_VARIANT + 1, len(nouns)):
        phrase = " ".join(nouns[:length])
        if phrase not in phrases:
            phrases.append(phrase)

    return phrases

def _already_present(phrase: str, variants: Sequence[str]) -> bool:
    """Whether ``phrase`` is one of ``variants``, ignoring case."""

    folded = phrase.casefold()
    return any(folded == existing.casefold() for existing in variants)
