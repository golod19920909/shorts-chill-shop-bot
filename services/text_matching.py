"""Word-aware matching of short feature phrases against marketplace text.

Why this module exists: feature filters (``required_features`` /
``excluded_features`` in :class:`~models.search_request.SearchRequest`)
were matched with plain substring tests, so the EXCLUDED feature
"проводные" also matched the word "беспроводные" and legitimate products
disappeared. Marketplace titles are Russian (the provider asks for
``target_language=RU``), so a suffix-only substring test is never safe.

What it does instead - without any NLP dependency:

- text is tokenized into words (letters/digits, Unicode aware), so
  punctuation, hyphens and invisible characters cannot create matches;
- a phrase matches when EVERY one of its words matches some word of the
  haystack; for words of four characters or more the match is a
  **word-start prefix** (Russian inflections: "шумоподавление" /
  "шумоподавления" / "шумоподавитель"), for shorter words it is exact;
- a prefix is always anchored at a WORD START, which is exactly what
  keeps "проводные" away from "беспроводные":
  ``"беспроводные".startswith("пров") is False``.

This is deliberately not a lemmatizer: it never claims to understand
meaning, it only stops the filter from matching inside other words.
Anything it cannot prove stays "no match", so a filter can only keep
products, never invent a reason to hide a valid one.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable

logger = logging.getLogger(__name__)

#: Words of this length or longer are compared by prefix, so Russian
#: endings do not break a match ("наушник" vs "наушники").
PREFIX_MATCH_LENGTH = 4

#: Words are runs of letters and digits. Underscores and every kind of
#: punctuation separate words, so "usb-c" is (usb, c) and "wi-fi" is
#: (wi, fi) - matching on words, not on raw character offsets.
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)

#: Russian cardinal number words that denote the SAME quantity as an Arabic
#: numeral. Small and curated on purpose - this is not a general numerology
#: module. It exists so equivalent feature phrasings match, e.g. the
#: requirement «с двумя аккумуляторами» matches a title «+ 2 аккумулятора»
#: without any broad substring matching and without an AI call.
_NUMBER_WORDS: dict[str, int] = {
    "ноль": 0,
    "один": 1, "одна": 1, "одно": 1, "одну": 1, "одного": 1, "одному": 1,
    "одним": 1, "одном": 1,
    "два": 2, "две": 2, "двумя": 2, "двух": 2, "двум": 2, "двое": 2,
    "три": 3, "тримя": 3, "трех": 3, "трем": 3, "тремя": 3, "трое": 3,
    "четыре": 4, "четырьмя": 4, "четырех": 4, "четырём": 4, "четверо": 4,
    "пять": 5, "пятерых": 5, "пятерым": 5, "пятерой": 5,
    "шесть": 6, "шестерых": 6,
    "семь": 7, "семерых": 7,
    "восемь": 8, "восьмерых": 8,
    "девять": 9,
    "десять": 10,
}

#: Writing systems this project can tell apart. A marketplace answer is
#: requested in one language while a user (or a model) may phrase a feature
#: in another, and no amount of prefix matching can bridge that gap.
_SCRIPT_PATTERNS = {
    "cyrillic": re.compile(r"[Ѐ-ӿ]"),
    "latin": re.compile(r"[A-Za-z]"),
}


def script_of(text: str | None) -> str | None:
    """The writing system of ``text``, or ``None`` when it has no letters."""

    if not text:
        return None
    for name, pattern in _SCRIPT_PATTERNS.items():
        if pattern.search(str(text)):
            return name
    return None


#: Nouns that turn a preceding number into a request for N OPTIONS (how many
#: results to SHOW), not N units of one product: «3 варианта», «несколько
#: вариантов», «5 опций», «2 options». The number of results a user wants to
#: see is not a product property and must never become a marketplace search
#: term. A pack of «3 pieces» / «3 шт» is a DIFFERENT meaning (units per item)
#: and is handled by the unit pattern - never touched here.
_OPTION_COUNT_NOUNS = (
    "варианта", "вариантов", "варианты", "вариантами", "вариантах", "вариант",
    "опция", "опции", "опций", "опцию", "опциями",
    "option", "options", "variant", "variants", "choice", "choices",
    "version", "versions",
)

#: Quantifiers that mean "some number of options" («несколько вариантов»).
_OPTION_COUNT_QUANTIFIERS = ("несколько", "пара", "пару", "парочку")

#: Request verbs after which a bare number is a result COUNT («покажи 5
#: наушников», «дай 3 шуруповерта», «show 5 earbuds»), never a unit quantity.
_REQUEST_VERBS = (
    "покажи", "покажите", "дай", "дайте", "найди", "найти", "подбери",
    "подберите", "ищи", "show", "find", "give", "gimme", "recommend",
)

#: Unit words that mark a number as UNITS-per-item, never an option count -
#: a negative lookahead keeps «Find 100 pieces» out of the option-count rule.
_UNIT_COUNT_NOUNS = (
    "pcs", "pieces", "units", "items", "packs", "pack",
    "шт", "штук", "штуки", "единиц", "единицы",
)

_QUANTIFIER_ALT = (
    r"\d+|" + "|".join(re.escape(w) for w in _OPTION_COUNT_QUANTIFIERS)
)
_NOUNS_ALT = "|".join(re.escape(w) for w in _OPTION_COUNT_NOUNS)
_VERBS_ALT = "|".join(re.escape(v) for v in _REQUEST_VERBS)
_UNITS_ALT = "|".join(re.escape(u) for u in _UNIT_COUNT_NOUNS)

#: «(verb) (N|несколько|пара) вариантов/опций/options» - the whole clause
#: (verb, quantifier and option noun) is request framing, removed entirely.
_OPTION_COUNT_CLAUSE = re.compile(
    r"(?:\b(?:" + _VERBS_ALT + r")\s+)?"
    r"(?:\bмне\s+|\bme\s+)?"
    r"(?:" + _QUANTIFIER_ALT + r")\s+(?:" + _NOUNS_ALT + r")\b",
    re.IGNORECASE,
)

#: «verb N <product>» where N is NOT a unit count - the verb and the result
#: count are framing and removed, the product words stay.
_REQUEST_COUNT = re.compile(
    r"\b(?:" + _VERBS_ALT + r")\s+(?:\bмне\s+|\bme\s+)?\d+\b"
    r"(?!\s*(?:" + _UNITS_ALT + r")\b)",
    re.IGNORECASE,
)


def strip_option_count(text: str | None) -> str:
    """Remove "show me N options" framing, keeping the product words.

    «дай 3 варианта шуруповерта» → «шуруповерта», «покажи 5 наушников» →
    «наушников», «шуруповерт, несколько вариантов» → «шуруповерт». A number of
    UNITS inside a product («3 pieces», «3 шт») is a different meaning and is
    never touched. Category-independent: no product name is referenced.
    """

    if not text:
        return text or ""
    # Clause first: «дай 3 варианта шуруповерта» must lose the WHOLE framing
    # (verb+quantifier+option-noun), otherwise the request-count rule below
    # would eat «дай 3» and orphan «варианта». Then «покажи 5 наушников» /
    # «дай 3 шуруповерта» lose just verb+count, keeping the product words.
    cleaned = _OPTION_COUNT_CLAUSE.sub(" ", text)
    cleaned = _REQUEST_COUNT.sub(" ", cleaned)
    cleaned = re.sub(r"[\s,;]+", " ", cleaned).strip(" ,.;:-")
    return re.sub(r"\s+", " ", cleaned).strip()


def extract_requested_result_count(text: str | None) -> int | None:
    """The number of result cards the user explicitly asked for, or ``None``.

    «покажи 3 варианта шуруповерта» -> 3, «покажи 5 наушников» -> 5,
    «show 5 earbuds» -> 5. The SAME request framing
    :func:`strip_option_count` removes from the retrieval phrase — so the
    count never reaches the catalog query, but it is not thrown away
    either: it becomes the delivery cap the pipeline applies
    (``SearchService.find_products``). A number of UNITS per item («3
    pieces», «3 шт») is a different meaning and is never a result count
    (unit nouns are excluded, the same negative lookahead
    ``strip_option_count`` uses). No number, no verb framing or a bare
    «несколько вариантов» answers ``None`` — the default cap stays.
    """

    if not text:
        return None
    lowered = text.lower()
    # Option-noun framing first: «3 варианта», «5 опций», «несколько вариантов».
    match = re.search(
        r"(?<!\d)(\d+)\s+(?:" + _NOUNS_ALT + r")\b", lowered
    )
    if match is None:
        # Bare «(verb) N <product>» — the same clause
        # ``strip_option_count`` removes; the number is the result count.
        match = re.search(
            r"\b(?:" + _VERBS_ALT + r")\s+(?:\bмне\s+|\bme\s+)?(\d+)\b"
            r"(?!\s*(?:" + _UNITS_ALT + r")\b)",
            lowered,
        )
    if match is None:
        return None
    count = int(match.group(1))
    if count < 1:
        return None
    return min(count, 100)


def can_ever_match(phrase: str | None, haystacks: list[str]) -> bool:
    """Whether ``phrase`` could match ANY of ``haystacks`` at all.

    Used before applying an inclusion filter: a Russian feature can never
    match an English-only result set, and treating that as "no such product"
    would empty a page of perfectly relevant real listings. Only a phrase
    written in a script that appears nowhere in the candidates is reported as
    inapplicable - a missing word in the SAME language is a genuine miss and
    keeps filtering.
    """

    script = script_of(phrase)
    if script is None:
        return True  # digits/punctuation: no cross-language question to ask
    # Stage 46: a feature whose canonical words are ALL script-neutral
    # (numbers, folded units, established cross-script terms) can match a
    # candidate in ANY script - «20000mAh» matches «20000 мАч», «блютуз»
    # matches «Bluetooth» - so it must NOT be dropped as cross-language
    # (that silently turned a hard capacity/spec constraint into optional).
    canon = _canon_words(phrase)
    if canon and all(_is_script_neutral(word) for word in canon):
        return True
    return any(script_of(text) == script for text in haystacks)



def normalize_phrase(phrase: str | None) -> list[str]:
    """Split a user/AI supplied feature into lower-case words."""

    if not phrase:
        return []
    return [word.lower() for word in _WORD_RE.findall(str(phrase))]


def normalize_text(text: str | None) -> list[str]:
    """Split marketplace text into lower-case words."""

    if not text:
        return []
    return [word.lower() for word in _WORD_RE.findall(str(text))]


# ---------------------------------------------------------------------------
# Stage 46 — canonical token form (shared by the feature matcher).
#
# The matcher used to compare raw lower-cased words, so EQUIVALENT
# representations of the SAME value failed to match and the hard required-
# feature filter rejected genuinely suitable products:
#   * number+unit spacing:  «650 мм» vs «650mm», «1 ТБ» vs «1TB»;
#   * unit case / script:   «20000 мАч» vs «20000mAh», «144 Гц» vs «144Hz»;
#   * established cross-script terms: «блютуз» vs «Bluetooth».
# A latent defect also let long NUMBERS match by 4-char PREFIX, so «20000»
# would have matched «20001» - a different capacity.
#
# The fix is a single shared canonicalisation (no second matcher, no NLP):
# split a digit+letter token into (number, unit), fold UNIT spellings to one
# key across case and script, and fold a TINY set of established cross-script
# term equivalents. Numbers then match EXACTLY (a different value still fails),
# units match only when they are true equivalents, and ordinary words keep the
# existing inflection-prefix behaviour. Nothing here treats arbitrary numbers,
# units, models or specs as interchangeable.
# ---------------------------------------------------------------------------

#: Surface unit spellings (lower-cased, letters only) -> one canonical key.
#: Only TRUE unit equivalents are listed; a word that is not a known unit is
#: never folded, so «bluetooth» is not mistaken for a unit.
_UNIT_CANON: dict[str, str] = {
    "mm": "mm", "мм": "mm",
    "cm": "cm", "см": "cm",
    "m": "m", "м": "m", "метр": "m", "метра": "m", "метров": "m",
    "mah": "mah", "мач": "mah",
    "ah": "ah", "ач": "ah",
    "hz": "hz", "гц": "hz",
    "ghz": "ghz", "ггц": "ghz",
    "gb": "gb", "гб": "gb",
    "tb": "tb", "тб": "tb",
    "kb": "kb", "кб": "kb",
    "mb": "mb", "мб": "mb",
    "kg": "kg", "кг": "kg",
    "g": "g", "гр": "g", "грамм": "g", "грамма": "g", "граммов": "g",
    "w": "w", "вт": "w", "ватт": "w",
    "kw": "kw", "квт": "kw",
    "v": "v", "volt": "v", "вольт": "v", "вольта": "v",
    "inch": "inch", "дюйм": "inch", "дюйма": "inch", "дюймов": "inch",
    "l": "l", "л": "l", "литр": "l", "литра": "l", "литров": "l",
    "ml": "ml", "мл": "ml",
}

#: Established cross-script term equivalents -> one canonical key. A SMALL,
#: curated set of widely transliterated shopping terms; never a general
#: transliterator, so an unknown word is compared as-is.
_TERM_EQUIV: dict[str, frozenset[str]] = {
    "bluetooth": frozenset({"bluetooth", "блютуз", "блутут", "блютус"}),
    "wifi": frozenset({"wifi", "вайфай", "вай фай", "вай-фай"}),
}

#: Canonical keys that carry no script of their own (numbers, folded units,
#: cross-script terms) - such a feature can match candidates in ANY script.
_SCRIPT_NEUTRAL_CANON = frozenset(_UNIT_CANON.values()) | frozenset(_TERM_EQUIV)

#: A token that is a run of digits followed by a run of letters ("650mm").
_NUM_UNIT_RE = re.compile(r"^(\d+)([a-zа-яё]+)$", re.UNICODE)


def _canon_word(word: str) -> list[str]:
    """Canonical form(s) of one lower-cased token (may split into 1-2)."""

    m = _NUM_UNIT_RE.match(word)
    if m:
        number, unit = m.group(1), m.group(2)
        return [number, _UNIT_CANON.get(unit, unit)]
    if word in _UNIT_CANON:
        return [_UNIT_CANON[word]]
    for canon, forms in _TERM_EQUIV.items():
        if word in forms:
            return [canon]
    return [word]


def _canon_words(text: str | None) -> list[str]:
    """Canonical word sequence of ``text`` (number+unit split, units/terms folded)."""

    out: list[str] = []
    for word in normalize_text(text):
        out.extend(_canon_word(word))
    return out


def _is_script_neutral(canon_word: str) -> bool:
    return canon_word.isdigit() or canon_word in _SCRIPT_NEUTRAL_CANON



def _word_matches(phrase_word: str, candidate: str) -> bool:
    """Whether one phrase word matches one haystack word.

    Comparison is anchored at the word start and uses a 4-character stem, so
    Russian inflections match ("наушник"/"наушники", "ночное"/"ночным",
    "шумоподавление"/"шумоподавления") while a match INSIDE another word is
    impossible ("проводные" never matches "беспроводные").

    Known limitation, kept deliberately: the stem is compared in one
    direction, so a feature can also match a much SHORTER word that merely
    starts the same way - requiring "wireless" also accepts "Wired
    Headphones". Tightening this (e.g. by demanding that the shorter word be
    fully consumed) was tried and REJECTED: it is structurally identical to
    the legitimate pair "ночное"/"ночным", so it would break real Russian
    morphology. Distinguishing them needs lemmatisation, not a prefix rule.
    """

    # Stage 46: a NUMBER is a hard value - two digit tokens match only when
    # equal («20000» never matches «20001»), and a digit token matches a
    # Russian number word (and vice versa) when they denote the SAME
    # quantity («2» == «двумая»). A bare digit never matches a non-number
    # word and a number word never matches a non-digit word, so there is no
    # broad substring matching here. Words keep the 4-char inflection-prefix
    # rule («аккумуляторами» matches «аккумулятора»).
    phrase_is_digit = phrase_word.isdigit()
    candidate_is_digit = candidate.isdigit()
    if phrase_is_digit or candidate_is_digit:
        phrase_num = int(phrase_word) if phrase_is_digit else _NUMBER_WORDS.get(phrase_word)
        candidate_num = int(candidate) if candidate_is_digit else _NUMBER_WORDS.get(candidate)
        if phrase_num is not None and candidate_num is not None:
            return phrase_num == candidate_num
        return False
    if len(phrase_word) >= PREFIX_MATCH_LENGTH:
        return candidate.startswith(phrase_word[:PREFIX_MATCH_LENGTH])
    return candidate == phrase_word


#: Prepositions that can dangle at the END of a captured «с X» / «без X»
#: phrase and make it incomplete («с пряжкой из» -> «пряжкой»). Only a
#: TRAILING preposition is stripped: «из кожи» mid-phrase stays, because
#: «чехол из кожи» is a complete material requirement. A feature reduced to
#: nothing but a preposition is not a feature at all.
_DANGLING_PREPOSITIONS: frozenset[str] = frozenset({
    # Russian
    "из", "из-под", "из-за", "со", "с", "на", "в", "от", "до", "у",
    "о", "об", "по", "под", "над", "перед", "за", "через",
    # English
    "for", "of", "from", "in", "on", "at", "with", "by",
})


def clean_feature(
    raw: str | None,
    *,
    stopwords: Iterable[str] = (),
    dangling_prepositions: Iterable[str] | None = None,
) -> str | None:
    """Clean a captured «с X» / «без X» feature phrase.

    Returns the cleaned phrase, or ``None`` when the capture is not a real
    feature: empty, longer than the allowed size, a bare count (a result
    count like «5» is not a product property), a dangling preposition
    fragment, or a phrase whose every word is a quality/talk stopword.

    Only a trailing preposition is removed, so a mid-phrase preposition
    («чехол из кожи») is preserved while an incomplete ending («с пряжкой
    из» -> «пряжкой») is not turned into a hard filter. The original casing
    and internal spacing of the kept phrase are preserved; callers match
    case-insensitively.
    """

    if not raw:
        return None

    feature = " ".join(raw.split()).strip(" ,.;:!?")
    if not feature or len(feature) > 30:
        return None
    # A bare digit run is a result count, not a product property, but
    # alphanumeric specs («12V», «65W», «USB-C») are kept.
    if re.fullmatch(r"[\d\s.,\-]+", feature):
        return None

    dangling = (
        set(dangling_prepositions)
        if dangling_prepositions is not None
        else set(_DANGLING_PREPOSITIONS)
    )
    # A lone preposition is sentence scaffolding, never a feature: it can
    # only appear here when the caller passed a raw word rather than a
    # «с X» / «без X» capture (the capture path strips a trailing
    # preposition first, so a lone «из» never becomes a hard filter).
    if feature.lower() in dangling:
        return None

    words = [word for word in _WORD_RE.findall(feature)]
    if not words:
        return None

    # Strip trailing dangling prepositions from the phrase end until a real
    # word remains (an incomplete ending, never a complete mid-phrase one).
    while True:
        matched = None
        for prep in sorted(dangling, key=len, reverse=True):
            if feature.lower().endswith(" " + prep):
                matched = prep
                break
        if matched is None:
            break
        feature = feature[: -len(matched) - 1].rstrip(" ,.;:!?")
        if not feature:
            return None
        words = [word for word in _WORD_RE.findall(feature)]
        if not words:
            return None

    lowered = [word.lower() for word in words]
    if stopwords and all(
        any(word.startswith(stop) for stop in stopwords) for word in lowered
    ):
        return None

    return feature


def phrase_in_text(text: str | None, phrase: str | None) -> bool:
    """Whether every word of ``phrase`` matches a word of ``text``.

    ``True`` requires a word-level match for each phrase word; an empty
    phrase never matches (a caller that filters on it would otherwise
    drop everything).

    Stage 46: both sides are reduced to their CANONICAL form first
    (:func:`_canon_words`), so equivalent representations of the same value
    match - «650 мм»/«650mm», «20000 мАч»/«20000mAh», «блютуз»/«Bluetooth» -
    while a genuinely different value (a different number or unit) still
    fails. Numbers match exactly; units and cross-script terms fold to one
    key; ordinary words keep the inflection-prefix rule.
    """

    phrase_words = _canon_words(phrase)
    if not phrase_words:
        return False

    haystack = _canon_words(text)
    if not haystack:
        return False

    return all(
        any(_word_matches(word, candidate) for candidate in haystack)
        for word in phrase_words
    )


def matching_feature(text: str | None, features: list[str] | None) -> str | None:
    """Return the first feature of ``features`` found in ``text``, if any.

    Used by the exclusion filter (and by diagnostics) to report WHICH
    feature matched instead of only hiding a product.
    """

    for feature in features or []:
        if phrase_in_text(text, feature):
            return feature
    return None


#: Multi-word constructions that describe a SHAPE, THEME or STYLE instead
#: of a literal catalogue attribute. A marketplace title almost never
#: repeats them verbatim - «брелок в виде молотка» is usually titled
#: «Ключница молоток, подарок» - so demanding them as literal inclusion
#: text hides exactly the products the user asked for. Verified live: the
#: required feature «в виде строительного инструмента» kept 1 of 24
#: relevant real keychains. Such a phrase is INTENT: it stays in
#: ``keywords`` / ``normalized_query`` where relevance and ranking use it,
#: but never becomes a literal filter. Markers are word sequences matched
#: anywhere in the phrase (inflections via the same word-prefix rule the
#: matcher uses) - a linguistic rule for a whole class of phrasings in
#: both languages, never a rule about one specific query or product.
_DESIGN_CLAUSE_MARKERS: tuple[tuple[str, ...], ...] = (
    ("в", "виде"),
    ("в", "форме"),
    ("в", "стиле"),
    ("в", "образе"),
    ("похож", "на"),
    ("напомина",),
    ("shap", "like"),
    ("in", "the", "shape", "of"),
    ("in", "the", "form", "of"),
    ("in", "the", "style", "of"),
    ("resembl",),
    ("design", "as"),
    ("look", "like"),
)


#: Multi-word constructions describing the CONTEXT OF USE (who the product is
#: bought FOR, which occasion it is bought ON) rather than a physical
#: catalogue attribute. «для подарка», «на подарок», «для жены», «для мужа»,
#: «на день рождения», «as a gift», «for birthday» say WHY the product is
#: bought; a marketplace title answers that with its own words («Подарок
#: женщине», «подарочный набор») and almost never repeats the user's phrase.
#: Enforcing such a phrase as a hard inclusion filter therefore hides exactly
#: the products the user asked for - a whole page of real listings that merely
#: worded the same intention differently.
#:
#: Deliberately NOT here: constructions that name a product CLASS and are
#: repeated verbatim in titles («для дома», «для авто», «для кухни», «для
#: работы»). They do not desynchronize the filter, so removing them would
#: only widen a result set the user narrowed on purpose.
#:
#: A context phrase is INTENT: it is skipped from hard inclusion filtering
#: (reported as not applied, see ``comparison_service``) and instead provides a
#: soft ranking bonus when a title really carries it.
_CONTEXT_CLAUSE_MARKERS: tuple[tuple[str, ...], ...] = (
    # -- gift wording ----------------------------------------------------
    ("для", "подар"),
    ("на", "подар"),
    ("в", "подар"),
    # -- the person the product is bought for ----------------------------
    ("для", "жен"),
    ("для", "муж"),
    ("для", "девушк"),
    ("для", "парн"),
    ("для", "ребен"),
    ("для", "ребён"),
    ("для", "детей"),
    ("для", "мам"),
    ("для", "пап"),
    ("для", "друг"),
    ("для", "подруг"),
    ("для", "коллег"),
    # -- the occasion ----------------------------------------------------
    ("на", "день", "рожден"),
    ("на", "день", "рождён"),
    ("на", "праздник"),
    ("на", "новый", "год"),
    ("на", "юбилей"),
    ("на", "свадьб"),
    # -- English ---------------------------------------------------------
    ("for", "gift"),
    ("as", "a", "gift"),
    ("as", "gift"),
    ("for", "wife"),
    ("for", "husband"),
    ("for", "women"),
    ("for", "men"),
    ("for", "girlfriend"),
    ("for", "boyfriend"),
    ("for", "kids"),
    ("for", "children"),
    ("for", "mom"),
    ("for", "dad"),
    ("for", "friend"),
    ("for", "birthday"),
    ("for", "party"),
    ("for", "christmas"),
)


def _matches_clause_markers(
    phrase: str | None, markers: tuple[tuple[str, ...], ...]
) -> bool:
    """Whether ``phrase`` contains one of ``markers`` as a word sequence.

    A marker word of three characters or more is compared as a WORD-START
    prefix («жен» covers «жены», «жене», «женщине»), shorter marker words
    («для», «на», «в», «for», «as», «a») must be exact - they are function
    words, and a prefix test on two characters would match half the
    dictionary. The three-character floor is deliberate and narrower than
    the matcher's :data:`PREFIX_MATCH_LENGTH`: markers are a closed list of
    Russian/English function words and stems, never a user-written feature.
    """

    def marker_matches(marker_word: str, word: str) -> bool:
        if len(marker_word) >= 3:
            return word.startswith(marker_word)
        return word == marker_word

    words = normalize_phrase(phrase)
    if not words:
        return False
    for marker in markers:
        if len(marker) > len(words):
            continue
        for start in range(len(words) - len(marker) + 1):
            if all(
                marker_matches(marker_word, words[start + offset])
                for offset, marker_word in enumerate(marker)
            ):
                return True
    return False


def is_design_clause(phrase: str | None) -> bool:
    """Whether ``phrase`` describes a shape/style rather than a literal attribute.

    See :data:`_DESIGN_CLAUSE_MARKERS` for why such a phrase must never be
    an inclusion filter. An empty phrase is not a design clause (the
    caller keeps its existing handling of blanks).
    """

    return _matches_clause_markers(phrase, _DESIGN_CLAUSE_MARKERS)


def is_context_clause(phrase: str | None) -> bool:
    """Whether ``phrase`` describes a context of use, not a product attribute.

    See :data:`_CONTEXT_CLAUSE_MARKERS`: «для подарка», «для жены», «на день
    рождения» name WHY the product is bought, and a marketplace title words
    that intention differently, so such a phrase must never be a hard
    inclusion filter. An empty phrase is not a context clause (the caller
    keeps its existing handling of blanks), and a phrase naming a product
    class or a compatibility («для дома», «для iPhone 15») is a literal
    attribute that stays a filter.
    """

    return _matches_clause_markers(phrase, _CONTEXT_CLAUSE_MARKERS)


#: Function words that turn a phrase into a NEGATION - a statement of ABSENCE
#: («без мешка», «не чехол», «without ANC», «no OS», «not a part»). A
#: marketplace title almost never repeats the user's negation word for word -
#: a bagless vacuum is titled «Пылесос циклонный» or «Vacuum Cleaner
#: Cordless» - so demanding the negated phrase as a literal inclusion filter
#: empties a page of exactly the requested products. Verified live (Stage
#: 44-B5): «пылесос без мешка» produced ``required_features=['без мешка']``
#: and «ноутбук без операционной системы» produced ``required_features=['без
#: ОС']``; no real bagless-vacuum / no-OS-laptop title carries that phrase
#: verbatim, so the filter hid the whole result set.
#:
#: It is deliberately NOT converted into an exclusion: the very word the user
#: negated often appears LITERALLY in the correct title («Пылесос БЕЗ МЕШКА»),
#: so excluding «мешок» would drop the product that was wanted. The safe,
#: general action is to refuse the destructive inclusion filter only - the
#: negated words still reach ``keywords`` / ``normalized_query`` and the
#: structured intent, where relevance and ranking use them. Markers are whole
#: closed-class RU/EN function words matched EXACTLY (never a user-written
#: attribute), so «беспроводной» / «недорогой» are never mistaken for a
#: negation - only a standalone «без» / «не» / «without» / «no» / «not» is.
_NEGATION_MARKERS = frozenset({"без", "не", "without", "no", "not"})


def is_negation_clause(phrase: str | None) -> bool:
    """Whether ``phrase`` leads with a negation («без мешка», «without ANC»).

    See :data:`_NEGATION_MARKERS` for why such a phrase must never be a
    literal inclusion filter. The test is anchored at the FIRST word, which is
    where a Russian/English negation grammatically sits («без X», «не X»,
    «without X», «no X», «not X») and which keeps a legitimate attribute that
    merely CONTAINS a negatable word («noise cancelling», «беспроводной»,
    «недорогой») from being mistaken for a negation. An empty phrase is not a
    negation clause (the caller keeps its existing handling of blanks).
    """

    words = normalize_phrase(phrase)
    return bool(words) and words[0] in _NEGATION_MARKERS


#: Articles dropped right after a negation marker so «without a bag» yields the
#: noun «bag» exactly as the deterministic fallback's «без X» capture does.
_NEGATION_ARTICLES = frozenset({"a", "an", "the"})


def negation_payload(phrase: str | None) -> str:
    """The noun phrase a negation applies to («мешка», «ANC», «OS»), or ``""``.

    :func:`is_negation_clause` decides WHETHER a phrase is a negation; this
    returns the remainder AFTER the leading marker (and any following article),
    preserving original case, so a caller can route the negated term to the
    exclusion slot exactly as the deterministic fallback does («без мешка» ->
    «мешка»). Returns ``""`` when ``phrase`` is not a negation or when nothing
    meaningful remains. It never invents an antonym: a positive-from-absence
    («без провода» -> «провода») is routed as an exclusion of the named thing,
    which is the safe, schema-supported interpretation, not the ideal «wireless».
    """

    if not is_negation_clause(phrase):
        return ""
    tokens = str(phrase).split()
    rest = tokens[1:]
    while rest and rest[0].casefold() in _NEGATION_ARTICLES:
        rest = rest[1:]
    return " ".join(rest).strip(" ,.-")


#: Maximum length of the ``keywords`` string sent to
#: ``aliexpress.affiliate.product.query``. Documented in
#: ``DOCS/PROJECT_OVERVIEW.md`` (retrieval section).
PROVIDER_KEYWORDS_LIMIT = 100


def truncate_provider_phrase(phrase: str | None) -> str:
    """Fit ``phrase`` into :data:`PROVIDER_KEYWORDS_LIMIT` characters.

    A plain ``phrase[:100]`` slice silently drops the TAIL of a long query -
    exactly where a model, a compatibility target or a numeric spec often
    sits. This helper keeps whole words only: it always keeps the leading
    words (product identity), then fills the remaining budget with trailing
    words, so both the head and the tail survive. Middle words are the ones
    dropped. Short phrases pass through unchanged; the result is
    deterministic and never exceeds the limit.
    """

    words = str(phrase or "").split()
    if not words:
        return ""
    if len(words[0]) > PROVIDER_KEYWORDS_LIMIT:
        return words[0][:PROVIDER_KEYWORDS_LIMIT]
    kept: set[int] = {0}
    # Tail words first: they carry model/compat/spec qualifiers that a blind
    # head slice drops. Scan from the end inward so the LAST words win.
    for index in range(len(words) - 1, 0, -1):
        candidate = " ".join(words[i] for i in sorted(kept | {index}))
        if len(candidate) > PROVIDER_KEYWORDS_LIMIT:
            continue
        kept.add(index)
    # Then head words in order, filling whatever budget remains.
    for index in range(1, len(words)):
        if index in kept:
            continue
        candidate = " ".join(words[i] for i in sorted(kept | {index}))
        if len(candidate) > PROVIDER_KEYWORDS_LIMIT:
            break
        kept.add(index)
    return " ".join(words[i] for i in sorted(kept))

