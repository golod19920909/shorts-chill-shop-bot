"""Multi-turn search refinement on top of the EXISTING search pipeline.

A follow-up message ("А с хорошим микрофоном?") must be able to refine the
previous search instead of being searched from scratch. This module holds
that state and the merge rules; it does NOT search anything - the handler
still calls the same ``SearchService.find_products`` used by every other
screen, so there is exactly one search architecture.

State decision (documented on purpose): the context lives in
``context.user_data`` - per user, in memory, expiring after
:data:`CONTEXT_TTL_SECONDS`. That is sufficient here:

* it only ever holds the last *request* (query words, filters, currency),
  which is the same data the bot already keeps in memory for the shown
  result session - no extra persistence layer is introduced;
* ``search_history`` in SQLite stays what it is (analytics/history) and is
  deliberately NOT read back as conversation state: a search performed on
  another device or an hour ago must not silently change a new search;
* a restarted bot simply starts a new conversation: nothing to migrate and
  nothing sensitive stored on disk.

The merge is deterministic by default and may be refined by AI when the
model is available: the AI variant is validated (it may only reuse words
the previous request or the follow-up already contained), and the
deterministic merge stays the fallback when AI is unavailable or
malformed - exactly like query parsing.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

from models.search_request import SearchMode, SearchRequest
from services.query_intent import CURRENCY_ALIASES, KNOWN_BRANDS
from services.relevance import STOPWORDS
from services.text_matching import (
    clean_feature,
    extract_requested_result_count,
    phrase_in_text,
)

logger = logging.getLogger(__name__)

#: Every currency spelling the project recognises (symbols, codes, word
#: forms in RU/UK/EN, inflected variants) - the single source is
#: ``services.query_intent.CURRENCY_ALIASES``, so the follow-up gate can
#: never drift from the parsers. A currency word scopes a budget FILTER of
#: the current product, never a new product the user switched to.
_CURRENCY_WORDS: frozenset[str] = frozenset(
    {key.casefold() for key in CURRENCY_ALIASES}
    | {value.casefold() for value in CURRENCY_ALIASES.values()}
)

#: How long a previous search stays available for refinement. Half an hour
#: is long enough for a real dialogue and short enough that a stale request
#: cannot silently survive into a new session.
CONTEXT_TTL_SECONDS = 30 * 60

#: Connectors that start a refinement ("а ...", "и ...", "ещё ...").
_REFINEMENT_STARTERS = (
    "а", "и", "ещё", "еще", "но", "только", "тоже", "также", "или", "если",
    "and", "also", "but", "only",
)

#: Short / suppletive Russian comparative forms: they only qualify the
#: previous product («лучше», «хуже», «больше», «меньше», «дешевле»,
#: «дороже», «подешевле», «подороже») and never name a new one. Kept as an
#: explicit set because their stems share no single prefix rule.
_COMPARATIVE_MODIFIERS = frozenset(
    {
        "лучше",
        "хуже",
        "больше",
        "меньше",
        "дешевле",
        "дороже",
        "подешевле",
        "подороже",
        "получше",
        "похуже",
        "побольше",
        "поменьше",
    }
)

#: Attenuating «по-» prefix of Russian comparatives («по-дешевле»,
#: «по-мощнее»). Stripped only when the bare root is itself a modifier.
_PO_PREFIX = "по"

#: Synthetic-comparative suffixes («дешев-ле», «мощн-ее», «по-выше»,
#: «по-крепче»). Applied only when the base is a known quality/feature
#: stem, so ordinary nouns keep their product reading.
_COMPARATIVE_SUFFIXES = ("ее", "ей", "е", "ше", "же", "че")

#: Word stems that only QUALIFY a product (adjectives, feature words). They
#: never make a message a new search on their own.
_MODIFIER_PREFIXES = (
    "хорош", "отличн", "лучш", "высок", "положительн", "качественн", "надёжн",
    "надежн", "дешев", "дешёв", "дорог", "недорог", "больш", "маленьк", "компактн",
    "лёгк", "легк", "мощн", "тих", "быстр", "водонепрониц", "беспроводн", "проводн",
    "микрофон", "шумоподавл", "камер", "аккумулятор", "bluetooth", "usb", "anc",
    "ярк", "тонк", "толст", "стильн", "удобн", "рейтинг", "заказ", "отзыв",
    "повыш", "поней",
)

#: Stems of Russian colour adjectives. A colour is a UNIVERSAL property of any
#: product in any category, so a colour word only ever qualifies the current
#: product («в красном цвете», «синий», «зелёный») and never names a new one -
#: exactly like the feature words above. Recognising them is what keeps a
#: colour refinement ("Найди вариант в красном цвете" for a phone case) from
#: being read as a switch to a product literally called «красном», which used
#: to RESET the conversation and drop the product the user was refining (the
#: reproduced release defect). Matched as a stem, but ONLY together with a
#: real Russian adjective ending (see ``_ADJECTIVE_ENDINGS``) so a coincidental
#: noun is never swallowed: «серьги»/«сервер» do not start a colour match even
#: though they begin with «сер», «белок»/«белка» keep their product reading,
#: and «цветок»/«цветы» (flowers) are untouched.
_COLOR_STEMS: tuple[str, ...] = (
    "красн", "оранж", "розов", "малинов", "бордов",
    "син", "голуб", "лазур", "бирюз", "зелен", "зелён", "изумруд", "салатов",
    "фиолет", "сирен", "лилов",
    "желт", "жёлт", "золот", "охр",
    "черн", "чёрн", "тёмн", "темн", "светл",
    "бел", "сер", "бежев", "коричнев", "хаки", "телесн",
)

#: The inanimate noun «цвет» (colour) in its own inflected forms. Exact tokens
#: only: «цветок»/«цветы» (flowers) share the prefix but are real products, so
#: a stem match here would be wrong. These mark «в <adj> цвете»/«<adj> цвета»
#: as a colour refinement, never a product.
_COLOR_NOUNS: frozenset[str] = frozenset(
    {"цвет", "цвете", "цвета", "цвету", "цветом"}
)

#: Russian adjective endings across cases (nominative + oblique), used to tell
#: an inflected colour adjective («красн-ом», «син-его», «красн-ого») apart
#: from an unrelated noun that merely shares a colour stem («серьги»).
_ADJECTIVE_ENDINGS: tuple[str, ...] = (
    "ый", "ий", "ой", "ая", "яя", "ое", "ые", "ие",
    "ым", "им", "ом", "ем", "ого", "его", "ому", "ему",
    "ых", "их", "ыми", "ыми", "ой", "ей", "ую", "юю", "ою", "ею",
)

#: "с X" / "со X" / "with X" adds a required characteristic. The English
#: branch masks «with two batteries» exactly like «с двумя
#: аккумуляторами», so a follow-up naming only an accessory/feature
#: («find one with two batteries») stays a refinement of the current
#: product instead of a switch to «batteries».
#:
#: A phrase never crosses a clause separator (`. ! ? , ; :`) or a line break:
#: «Беспроводные наушники с микрофоном. Покажи 3 варианта» says the feature in
#: the FIRST sentence only - the next sentence's words are not a part of it.
#: Up to three following words are captured, because a real accessory phrase
#: is often longer than two («с чехлом и кабелем»).
_REQUIRED_FEATURE_RE = re.compile(
    r"(?:^|\s)(?:с|со|with)\s+([\w\-]+(?:\s+[\w\-]+)?)", re.IGNORECASE
)

#: Separators that END a clause; a feature phrase never spans one.
_FEATURE_BOUNDARY_RE = re.compile(r"[.!?,;:\n\r\t]")

def _currency_code_in(text: str | None) -> str | None:
    """The currency CODE a message names («гривны» -> UAH), or ``None``."""

    folded = (text or "").casefold().replace("ё", "е")
    for alias in sorted(CURRENCY_ALIASES, key=len, reverse=True):
        if alias.isalpha():
            if re.search(r"(?<![^\W\d_])" + re.escape(alias) + r"(?![^\W\d_])", folded):
                return CURRENCY_ALIASES[alias]
        elif alias in folded:
            return CURRENCY_ALIASES[alias]
    return None

def _clause_bounded(pattern: re.Pattern[str], text: str) -> list[str]:
    """Captures of ``pattern`` that do not cross a clause separator."""

    phrases: list[str] = []
    for match in pattern.finditer(text or ""):
        phrase = match.group(1)
        boundary = _FEATURE_BOUNDARY_RE.search(phrase)
        if boundary is not None:
            # The separator itself is not part of group 1 (``\\w`` stops at
            # it); this guard only keeps a future pattern change honest.
            phrase = phrase[: boundary.start()]
        phrase = phrase.strip()
        if phrase:
            phrases.append(phrase)
    return phrases

#: "без X" / "не X" removes products that have it. "не" is matched only as a
#: standalone word followed by a feature ("не ударный", "не чехол"), so a
#: word that merely CONTAINS «не» ("недорогой", "непромокаемый") is never
#: read as an exclusion. Used by the follow-up merge; the INITIAL deterministic
#: parser deliberately does NOT turn negations into exclusions, because a
#: marketplace title often repeats the negated word verbatim
#: ("Пылесос БЕЗ МЕШКА") and excluding it would drop the wanted product.
_EXCLUDED_FEATURE_RE = re.compile(
    r"(?:^|\s)(?:без|не)\s+([\w\-]+(?:\s+[\w\-]+)?)", re.IGNORECASE
)

#: "любая цена", "снять ограничение" - the user drops the price limit.
_CLEAR_PRICE_MARKERS = (
    "любая цена", "любой ценой", "без ограничения цены", "снять ограничение",
    "снять лимит", "не важно сколько", "любые цены",
)

#: Characteristic words that are NOT features (quality phrases, review and
#: delivery talk): never turned into a filter, exactly like the parser never
#: turns "good reviews" into a rating.
_FEATURE_STOPWORDS = (
    "хорош", "отличн", "высок", "низк", "лучш", "положительн", "плох",
    "отзыв", "рейтинг", "оценк", "звезд", "заказ", "продаж", "доставк",
    "скидк", "цен", "стоим", "штук", "качеств", "нов", "повыше",
    # Budget talk: «бюджет до 40» is a filter, «бюджет» never names a product.
    "бюджет",
)

#: Prepositions that introduce the INTENDED USE / compatibility target of a
#: product in a follow-up: «шуруповёрт ДЛЯ сборки мебели», «лампа ДЛЯ чтения».
#: The noun after them says what the product is FOR, never a new product the
#: user switched to - the same role ``services.relevance._TARGET_INTRODUCERS``
#: plays for «крепление для …». Used to MASK those nouns so they are not read
#: as a product name (see :func:`_mask_non_product_spans`).
_USE_INTRODUCERS: tuple[str, ...] = ("для", "for")

#: «для X» / «for X» capture, mirroring the feature-phrase shape (up to two
#: words after the introducer). ё is folded by the caller's tokenizer.
_USE_TARGET_RE = re.compile(
    r"(?:^|\s)(?:для|for)\s+([\w\-]+(?:\s+[\w\-]+)?)", re.IGNORECASE
)

#: "в виде X" shape/style phrase: describes form, not a new product.
_SHAPE_RE = re.compile(
    r"(?:^|\s)(?:в\s+виде)\s+([\w\-]+(?:\s+[\w\-]+)?)", re.IGNORECASE
)

#: Generic REQUEST scaffolding that carries no product identity in ANY
#: category (not tied to one example): request verbs («найди», «подбери»),
#: the generic option noun («вариант»), and frequency/manner adverbs that only
#: describe HOW the product is used («в основном», «иногда»). A word from this
#: class never turns a follow-up into a new-product search; a real product
#: noun («отвёрток», «плазморезка», «кроссовки») is deliberately NOT here.
#: Currency words («долларов», «грн», «рублей», «USD», «UAH») likewise scope
#: a budget FILTER of the current product, never a new one: without them a
#: follow-up «а до 1500 грн» reads «грн» as a product switch and drops the
#: product the user was refining (the B2 currency-refinement defect).
_REQUEST_SCAFFOLD_STEMS: tuple[str, ...] = (
    "найд", "покаж", "подбер", "выбер", "выбир", "помог", "хоч", "давай",
    "рекоменд", "посовет", "вариант", "опци",
    "основн", "иногд", "обычн",
    # Budget/constraint qualifier adverbs: «только до 40», «желательно до 20».
    # They scope a FILTER of the current product, never name a new one, so a
    # budget refinement must not be read as a product switch (the reproduced
    # Scenario-B defect, where «Только до 40 долларов» reset the headphones).
    "тольк", "желательн", "примерн", "максим", "предел",
    # Request verbs and question/indefinite words that carry no product
    # identity in any category: «Сделай подешевле», «а есть покороче?»,
    # «посоветуй что-нибудь». A real product noun is deliberately NOT here.
    "сделай", "сделать", "есть", "что", "нибудь", "либо",
)

_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)

#: Words that frame a number but name neither a product nor a feature: request
#: verbs, the option noun, connectors and currency words. A follow-up that
#: leaves ONLY these around its number («покажи 3 варианта», «и ещё 4», «show
#: three more») asks for a different NUMBER OF SHOWN OPTIONS - not for a
#: different product. Used by :func:`is_count_only_follow_up`.
_COUNT_ONLY_WORDS: frozenset[str] = frozenset(
    {
        "покажи", "покажите", "дай", "дайте", "найди", "найдите", "найти",
        "подбери", "подберите", "выведи", "сделай", "сделайте",
        "ещё", "еще", "и", "а", "мне",
        "больше", "шт", "штук", "штуки", "штука", "пожалуйста",
        "show", "give", "find", "more", "me", "please", "another",
        "other", "again", "of", "them", "these", "those",
        "вариант", "варианта", "вариантов", "варианты", "вариантом",
        "опция", "опции", "опций", "опцию", "товар", "товара", "товаров",
        "товары", "option", "options", "variant", "variants", "item", "items",
    }
)

#: Numbers written in words that mark a follow-up as an OPTION COUNT (never a
#: unit quantity). Digit forms are captured by the shared extractor; these
#: words let «покажи ещё пару вариантов» behave like «покажи ещё 2 варианта».
_COUNT_ONLY_NUMBER_WORDS: dict[str, int] = {
    "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5,
    "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10,
}

#: Nouns that turn a bare number into an OPTION COUNT («3 варианта»).
_COUNT_NOUNS: frozenset[str] = frozenset(
    {"вариант", "варианта", "вариантов", "варианты", "опция", "опции",
     "опций", "опцию", "option", "options", "variant", "variants", "item",
     "items"}
)

#: Verbs that make a following number a REQUEST for that many options.
_COUNT_VERBS: frozenset[str] = frozenset(
    {"покажи", "покажите", "дай", "дайте", "найди", "найдите", "подбери",
     "подберите", "выведи", "show", "find", "give"}
)

def _has_count_context(words: list[str]) -> bool:
    """Whether ``words`` frame a bare number as an OPTION COUNT."""

    return any(word in _COUNT_NOUNS for word in words) or any(
        word in _COUNT_VERBS for word in words
    )

def _is_count_framing_word(word: str) -> bool:
    """Whether ``word`` only frames a requested result count."""

    lowered = word.lower().replace("ё", "е")
    if len(lowered) < 2:
        return True
    if lowered in _COUNT_ONLY_WORDS or lowered in _CURRENCY_WORDS:
        return True
    if lowered in _COUNT_ONLY_NUMBER_WORDS or lowered.startswith("пар"):
        return True
    return _is_modifier(lowered)

def is_count_only_follow_up(text: str) -> int | None:
    """The requested number of OPTIONS in a count-only message, or ``None``.

    «Покажи 5», «Покажи три варианта», «Сделай 3 варианта», «и ещё 4»,
    «покажи ещё пару вариантов» change only HOW MANY products are shown -
    never WHICH product. Such a message must therefore be applied to the
    CURRENT search (its product, filters, budget and currency all stay) and
    must never be merged as a fresh retrieval phrase.

    Conservative by construction: a message that carries ANY other content
    word («покажи 3 беспроводные наушники», «покажи 5 наушников») answers
    ``None`` and keeps the ordinary search/refinement behaviour - those
    product words are checked with the SAME qualifier reading the refinement
    gate uses. Category-independent: no product name is hardcoded.

    Digit forms go through the project's single count extractor
    (:func:`services.text_matching.extract_requested_result_count`); word
    forms (три, четыре, «пару») are accepted here because that shared
    extractor deliberately captures digits only.
    """

    if not text or not text.strip():
        return None
    words = [word.lower().replace("ё", "е") for word in _WORD_RE.findall(text)]
    if not words:
        return None
    has_digit = any(char.isdigit() for char in text)
    if (
        not any(word in _COUNT_ONLY_NUMBER_WORDS for word in words)
        and not any(word.startswith("пар") for word in words)
        and not (has_digit and _has_count_context(words))
    ):
        return None
    leftover = [
        word
        for word in words
        if len(word) >= 3 and not _is_count_framing_word(word)
    ]
    if leftover:
        return None
    count = extract_requested_result_count(text)
    if count is not None:
        return count
    # «покажи ЕЩЁ 5», «дай мне ещё 2»: a filler word between the verb and the
    # number. Safe here because every other word already passed the guard
    # above - this message names nothing but a count.
    digits = re.search(r"\d+", text)
    if digits is not None:
        value = int(digits.group(0))
        if value >= 1:
            return min(value, 100)
    # Word forms: «покажи ещё пару вариантов» / «покажи три варианта».
    for word in words:
        if word in _COUNT_ONLY_NUMBER_WORDS:
            return _COUNT_ONLY_NUMBER_WORDS[word]
        if word.startswith("пар"):
            return 2
    return None

@dataclass
class ConversationState:
    """The last effective request of one user, with an expiry stamp."""

    request: SearchRequest
    updated_at: float = field(default_factory=time.monotonic)
    welcomed: bool = False

    def is_expired(self, *, now: float | None = None) -> bool:
        return (now or time.monotonic()) - self.updated_at > CONTEXT_TTL_SECONDS

    def touch(self) -> None:
        self.updated_at = time.monotonic()

def _terms(request: SearchRequest) -> list[str]:
    """Significant words of a request (its catalog query)."""

    text = request.normalized_query or request.original_query or ""
    return [word.lower() for word in _WORD_RE.findall(text) if len(word) >= 3]

def _stem(word: str) -> str:
    """Five-letter stem used to compare words across inflections."""

    return word.lower()[:5]

def _known_stems(previous: SearchRequest) -> set[str]:
    """Every significant word the previous request is already ABOUT.

    Not only its catalog phrase: the user's own earlier sentence, the parser's
    keywords and the characteristics already required all belong to the same
    subject, so a follow-up that repeats them is still a refinement.
    """

    sources = [
        *_terms(previous),
        *_WORD_RE.findall(previous.original_query or ""),
        *previous.keywords,
        *previous.required_features,
    ]
    return {_stem(word) for word in sources if len(word) >= 3}

def _content_words(text: str | None) -> list[str]:
    """Words of a message that name a PRODUCT (no qualifiers, no prices).

    Deterministic reading of the user's own sentence: adjectives, quality and
    feature words (``_is_modifier``) and the price/rating/quantity wording of
    ``services.relevance.STOPWORDS`` are not products, so «а дешевле?» and
    «а с хорошим микрофоном» introduce nothing new - while «плазморезка» does.
    """

    words: list[str] = []
    for word in _WORD_RE.findall(text or ""):
        lowered = word.lower()
        if len(lowered) < 3 or lowered in STOPWORDS or _is_modifier(lowered):
            continue
        words.append(lowered)
    return words

def _strip_po_prefix(word: str) -> str | None:
    """Strip the attenuating «по-» of a comparative («помощнее» → «мощнее»).

    Returns ``None`` when the remainder is not itself a modifier, so an
    ordinary product noun keeps its meaning from the caller: «подарок»
    does NOT become «дарок», and unknown words are simply not modifiers.
    """

    lowered = word.lower()
    if not lowered.startswith(_PO_PREFIX) or len(lowered) <= len(_PO_PREFIX) + 2:
        return None
    rest = lowered[len(_PO_PREFIX):]
    if rest in _COMPARATIVE_MODIFIERS or rest in {
        "дешевле", "дороже", "мощнее", "выше", "ниже", "крепче", "легче",
    }:
        return rest
    if any(rest.startswith(prefix) for prefix in _MODIFIER_PREFIXES):
        return rest
    return None

def _strip_comparative_suffix(word: str) -> str | None:
    """Strip a synthetic-comparative suffix («мощнее» → «мощн», «повыше» → «повы»).

    The caller checks the base against the qualifier stems, so a plain noun
    whose ending merely looks comparative («платье») never qualifies.
    """

    lowered = word.lower()
    for suffix in _COMPARATIVE_SUFFIXES:
        if lowered.endswith(suffix) and len(lowered) > len(suffix) + 3:
            return lowered[: -len(suffix)]
    return None

def _is_comparative_modifier(word: str) -> bool:
    """Whether a word is a RELATIVE comparative («подешевле», «помощнее»).

    These qualify the previous product («найди подешевле», «а помощнее?»)
    and never name a new one. Guarded twice: explicit irregular forms, or a
    qualifier stem plus a genuine comparative ending - a bare «по+…» or a
    coincidental ending («платье») never qualifies.
    """

    lowered = word.lower()
    if lowered in _COMPARATIVE_MODIFIERS:
        return True
    stripped = _strip_po_prefix(lowered)
    if stripped is not None and (
        stripped in _COMPARATIVE_MODIFIERS or _is_comparative_modifier(stripped)
    ):
        return True
    base = _strip_comparative_suffix(lowered)
    if base is None:
        return False
    return any(base.startswith(prefix) for prefix in _MODIFIER_PREFIXES)

def _is_color_word(word: str) -> bool:
    """Whether a word is a Russian colour adjective or the colour noun «цвет».

    A colour is a universal property of any product, so it only ever qualifies
    the current product and never names a new one. Two guards keep this from
    swallowing real products:

    * the «цвет» noun is matched as EXACT tokens, so «цветок»/«цветы» (flowers)
      keep their product reading;
    * a colour adjective is matched as stem + a real Russian adjective ending,
      so a coincidental noun is never caught: «серьги»/«сервер»/«серия» (all
      begin with «сер») and «белка»/«белок» (begin with «бел») stay products,
      while the genuinely inflected «красн-ом»/«син-его»/«красн-ого» qualify.

    Category-independent: no product name is hardcoded, only colour words.
    """

    lowered = word.lower()
    if lowered in _COLOR_NOUNS:
        return True
    # ё-folded so «зелёном»/«жёлтую» match their un-folded stems too.
    folded = lowered.replace("ё", "е")
    for stem in _COLOR_STEMS:
        base = stem.replace("ё", "е")
        if lowered.startswith(base) or folded.startswith(base):
            remainder = lowered[len(base):] if lowered.startswith(base) else folded[len(base):]
            if any(remainder.endswith(ending) for ending in _ADJECTIVE_ENDINGS):
                return True
    return False

def _is_modifier(word: str) -> bool:
    """Whether a word only qualifies something (adjective/feature word)."""

    lowered = word.lower()
    if lowered in _COMPARATIVE_MODIFIERS:
        # Suppletive / short comparative forms («лучше», «хуже», «больше»,
        # «меньше», «дешевле», «дороже», «подешевле»): they only qualify the
        # previous product and never name a new one. Listed explicitly
        # because their stems do not share a prefix rule («лучше» does not
        # start with «хорош»).
        return True
    if lowered.startswith(_PO_PREFIX) and _is_modifier(lowered[len(_PO_PREFIX):]):
        # Attenuating «по-» comparatives («подешевле», «помощнее», «подороже»):
        # they never name a product either. Only the bare root is checked so
        # the rule does not leak onto ordinary nouns («поводок» keeps the
        # product reading - «водок» is not a modifier).
        return True
    # Quality/feature talk («качеством», «повыше качеством») is not a product:
    # the same stopword list the feature extractor uses keeps these words
    # from ever naming a new item, so «Повыше качеством.» refines instead.
    if any(lowered.startswith(stop) for stop in _FEATURE_STOPWORDS):
        return True
    if any(lowered.startswith(prefix) for prefix in _MODIFIER_PREFIXES):
        return True
    # A colour word (any case) is a universal qualifier, never a product name:
    # «в красном цвете» must refine the current product, not switch to «красном».
    if _is_color_word(lowered):
        return True
    # Comparative suffix: Russian synthetic comparatives («дешевле»,
    # «мощнее», «повыше», «покрепче») only qualify the previous product -
    # they describe «more/less X», never a new product type. Checked through
    # the same stem so the base must be a known quality/feature word
    # («качественнее» -> «качественн» qualifies; «плавнее» keeps its reading
    # because «плав» is not a known property).
    for suffix in _COMPARATIVE_SUFFIXES:
        if lowered.endswith(suffix) and len(lowered) > len(suffix) + 2:
            base = lowered[: -len(suffix)]
            if any(base.startswith(prefix) for prefix in _MODIFIER_PREFIXES):
                return True
    # Suffix heuristic for Russian adjectives. It can only classify a word
    # as a qualifier, and a misclassification degrades to "refinement" (the
    # user still sees a result and can simply search again).
    return lowered.endswith(("ый", "ий", "ой", "ая", "яя", "ое", "ее", "ые", "ие"))

def _is_device_target(target: str) -> bool:
    """Whether a «для X» / «for X» target names a concrete DEVICE, not a
    purpose or material.

    «шуруповёрт ДЛЯ iPhone» pins a specific device the product must fit - the
    compatibility target ``services.query_intent`` models, and a NEW device
    the previous search never mentioned (it legitimately resets). «шуруповёрт
    ДЛЯ сборки мебели» / «ДЛЯ дерева» only states the intended USE and refines
    the current product. A known brand/model token is the project's existing,
    category-independent signal for "concrete device", so the distinction
    needs no per-example word list.
    """

    lowered = (target or "").casefold().replace("ё", "е")
    return any(brand in lowered for brand in KNOWN_BRANDS)

def _mask_non_product_spans(text: str) -> str:
    """Blank out the spans that name the intended USE or a FEATURE, not a
    product: «с X»/«без X» characteristics and «для X»/«for X» use targets.

    What remains is the part of the message that could NAME a product, so a
    follow-up whose only extra words sit in these spans («для сборки мебели»,
    «с двумя аккумуляторами») is recognised as a refinement of the CURRENT
    product, not a switch to a new one. A «для X» target that names a concrete
    device/brand is NOT masked (see :func:`_is_device_target`): it is a real
    compatibility change, so it stays visible and can reset the search. Uses
    the same two-word span shape the feature extractor uses, so the two never
    disagree about where a phrase ends.
    """

    masked = list(text or "")
    spans: list[tuple[int, int]] = []
    for pattern in (_REQUIRED_FEATURE_RE, _EXCLUDED_FEATURE_RE):
        spans.extend(match.span(1) for match in pattern.finditer(text or ""))
    for match in _USE_TARGET_RE.finditer(text or ""):
        if not _is_device_target(match.group(1)):
            spans.append(match.span(1))
    for match in _SHAPE_RE.finditer(text or ""):
        spans.append(match.span())
    for start, end in spans:
        for index in range(start, end):
            masked[index] = " "
    return "".join(masked)

#: Shortest stem of every currency name the project recognises. A word that
#: starts with one of these is a CURRENCY, never a product feature.
_CURRENCY_STEMS: tuple[str, ...] = (
    "гривн", "грн\u0430", "долар", "доллар", "долл", "евро", "рубл", "юан",
    "usd", "eur", "rub", "uah", "cny", "gbp", "dollar", "euro", "ruble",
    "yuan", "pound", "hryvn",
)

#: Display wording that may accompany a currency restatement without naming
#: a product («цена», «цену», «пересчёт», «курс»).
_CURRENCY_DISPLAY_WORDS: frozenset[str] = frozenset(
    {"цена", "цены", "цену", "цене", "цен", "ценой", "стоимость", "стоимостью",
     "курс", "курса", "курсом", "пересчет", "пересчёт", "пересчете", "валюта",
     "валюту", "валюте", "валютой", "price", "prices", "rate", "currency"}
)

def _is_currency_display_word(word: str) -> bool:
    """Whether ``word`` is price/currency display wording, not a product."""

    return word in _CURRENCY_DISPLAY_WORDS

def _is_currency_restatement(word: str) -> bool:
    """Whether ``word`` only restates a currency name."""

    return any(word.startswith(stem) for stem in _CURRENCY_STEMS)

#: Tail of a characteristic phrase: «из металла», «в комплекте», «с чехлом».
#: It continues the attribute the user just named, so its words never count as
#: a product switch.
#: Tail of a characteristic phrase: «из металла», «в комплекте», «с чехлом».
#: The introducer sits INSIDE a group so consecutive tails («с чехлом в
#: комплекте») are each found; only the captured words are masked.
_FEATURE_TAIL_RE = re.compile(
    r"((?:из|в|со|с|with|in)\s+[\w\-]+(?:\s+[\w\-]+)?)",
    re.IGNORECASE,
)

#: Words that END a characteristic tail (connectors / request wording): the
#: tail never swallows the next sentence's subject.
_FEATURE_TAIL_STOP = frozenset(
    {"и", "а", "но", "для", "with", "and", "for", "the", "show", "покажи"}
)

#: Words that continue a characteristic after its captured phrase and never
#: name a product on their own.
_FEATURE_TAIL_LEAD: frozenset[str] = frozenset(
    {"из", "в", "во", "с", "со", "with", "in", "of", "и"}
)

#: Words that END a characteristic tail (connectors / request wording).
_FEATURE_TAIL_STOP: frozenset[str] = frozenset(
    {"и", "а", "но", "или", "для", "если", "покажи", "найди", "хочу",
     "with", "and", "for", "the", "show", "find"}
)

#: Whitespace and punctuation between two words of one phrase.
_FILLER_RE = re.compile(r"[\s,;:\-]+")

def _mask_feature_spans(text: str) -> str:
    """Blank out «с X» / «без X» spans and the tail of each characteristic.

    Their words never NAME a product: «с чехлом в комплекте», «с пряжкой из
    металла» describe an accessory or its material. Walking the words after
    each captured phrase (instead of matching overlapping regex spans) keeps
    consecutive tails like «в комплекте» fully covered.
    """

    source = text or ""
    masked = list(source)
    spans: list[tuple[int, int]] = []
    for pattern in (_REQUIRED_FEATURE_RE, _EXCLUDED_FEATURE_RE):
        spans.extend(match.span() for match in pattern.finditer(source))

    # Extend every span over the characteristic's tail.
    for start, end in list(spans):
        cursor = end
        while True:
            gap = _FILLER_RE.match(source, cursor)
            word = _WORD_RE.search(source, gap.end() if gap else cursor)
            if word is None:
                break
            value = word.group(0).casefold().replace("ё", "е")
            if value in _FEATURE_TAIL_STOP:
                break
            # Skip whitespace between the words; mask the word itself.
            spans.append((word.start(), word.end()))
            cursor = word.end()
            if value not in _FEATURE_TAIL_LEAD and len(value) < 3:
                break

    for start, end in spans:
        for index in range(start, end):
            masked[index] = " "
    return "".join(masked)

def _names_new_product(text: str, previous: SearchRequest) -> str | None:
    """The first genuine NEW product word a follow-up names, or ``None``.

    A word is a new product ONLY when it survives four filters that all say
    "this is NOT a product name":

    * intended use / feature spans are masked first («для сборки мебели»,
      «с двумя аккумуляторами») - those nouns qualify the current product;
    * generic request scaffolding and question/indefinite words («сделай»,
      «есть», «что», «нибудь») carry no product identity in any category;
    * a word preceded by the negation «не» refuses a feature of the current
      product («только не ударную») - it cannot become a new product;
    * the previous request already knew the word (its product or its own
      vocabulary), so repeating it introduces nothing.

    A real, unrelated product noun («отвёрток», «плазморезка», «кроссовки»)
    passes every filter and is returned, so an explicit product switch still
    starts a new search. Category-independent: no product name is hardcoded.
    """

    known = _known_stems(previous)
    # A word INSIDE a captured "с X" / "без X" span is an accessory or a
    # feature of the CURRENT product ("с чехлом в комплекте"), never a
    # product the user switched to - so those spans are masked even when a
    # stray connective («с») survives between them.
    # Both masks are needed: the USE/feature spans («для сборки мебели»)
    # and the characteristic spans with their tail («с чехлом в комплекте»).
    residual = _mask_feature_spans(_mask_non_product_spans(text or ""))
    prev_word: str | None = None
    for word in _WORD_RE.findall(residual):
        lowered = word.lower()
        if prev_word == "не":
            # «не X» refuses feature X of the current product; X is not a new
            # product the user switched to.
            prev_word = lowered
            continue
        if len(lowered) < 3 or lowered in STOPWORDS:
            prev_word = lowered
            continue
        if _is_modifier(lowered):
            prev_word = lowered
            continue
        # Attenuating «по-» comparative («покороче», «подешевле»): the root
        # may be inflected («коротк» → «короче») so the general modifier
        # rules miss it, but it still only qualifies the current product.
        if (
            lowered.startswith(_PO_PREFIX)
            and len(lowered) > len(_PO_PREFIX) + 3
            and any(
                lowered[len(_PO_PREFIX):].endswith(suffix)
                for suffix in _COMPARATIVE_SUFFIXES
            )
        ):
            prev_word = lowered
            continue
        if any(lowered.startswith(stop) for stop in _FEATURE_STOPWORDS):
            prev_word = lowered
            continue
        if any(lowered.startswith(stem) for stem in _REQUEST_SCAFFOLD_STEMS):
            prev_word = lowered
            continue
        if lowered in _CURRENCY_WORDS:
            # A currency word scopes a budget FILTER of the current product
            # («а до 1500 грн»), never a new product the user switched to.
            prev_word = lowered
            continue
        if _stem(lowered) not in known:
            return lowered
        prev_word = lowered
    return None

def is_refinement(
    previous: SearchRequest, follow_up: SearchRequest, raw_text: str
) -> bool:
    """Whether the follow-up refines ``previous`` instead of replacing it.

    The USER'S OWN WORDS decide, never where a parser or a merge model parked
    them: a message that names a product the previous search was not about is a
    NEW search, even if it also carries a connector («а плазморезка?») and even
    if the parsed request kept a generic phrase in ``normalized_query`` while
    putting the new product into ``keywords``.

    That last shape is a verified production failure, not a theory: after a
    search for a watch strap, the message «плазморезка» was merged into the
    PREVIOUS request (the strap phrase stayed the catalog query, the new word
    became a keyword), the merge looked like a refinement, and the user got a
    watch strap as the answer to a query about a plasma cutter.
    """

    known = _known_stems(previous)

    # A message whose every content word sits inside a «с X»/«без X»
    # characteristic adds or removes only a FEATURE of the previous product
    # («С двумя аккумуляторами.», «Без провода.») - it never names a new one,
    # even without a leading connector. Guarded so an AI merge that DRIFTED
    # (parked a foreign product like «кроссовки» into the follow-up's own
    # query) is still caught by the introduced-product gate below: a genuine
    # feature refinement never puts a new product word in the parsed query.
    if _only_feature_phrases(raw_text or "") and not _followup_names_product(
        previous, follow_up, raw_text or "", known
    ):
        return True

    # A follow-up REPLACES the previous search only when it names a genuine
    # NEW product. Intended use («для сборки мебели», «для дерева»), feature
    # characteristics («с двумя аккумуляторами») and generic request
    # scaffolding («найди», «вариант», «в основном») are NOT product names, so
    # they must not reset the search - that was the Stage 50 defect, where a
    # use/budget/feature follow-up dropped the screwdriver. Every source the
    # old gate read (the user's words, the parser's phrase, the parser's
    # keywords) is checked with the same product-vs-use filter, so a real
    # switch («набор отвёрток», «плазморезка») still resets while a refinement
    # never does. Category-independent: no product word is hardcoded.
    new_product = (
        _names_new_product(raw_text or "", previous)
        or _names_new_product(
            follow_up.normalized_query or follow_up.original_query or "", previous
        )
        or next(
            (
                word
                for word in follow_up.keywords
                if len(word) >= 3 and _names_new_product(word, previous)
            ),
            None,
        )
    )
    if new_product is not None:
        logger.info(
            "New product %r in %r; the previous search is replaced",
            new_product,
            raw_text,
        )
        return False

    # No new product was named anywhere (the user's words, the parser's phrase
    # or its keywords), so the message can only REFINE the previous search:
    # a concrete filter («до 40 долларов», «от 4.5»), a feature («с
    # микрофоном»), an intended use («для дерева») or a bare comparative
    # («подешевле»). There is no longer a second, weaker product detector here
    # - it used to reset on intended-use/feature vocabulary («дерева»,
    # «аккумуляторами») and was the Stage 50 defect. Nothing is invented: the
    # merge only applies what the message actually carried.
    return True

def _followup_names_product(
    previous: SearchRequest, follow_up: SearchRequest, raw_text: str, known: set[str]
) -> bool:
    """Whether the FOLLOW-UP query names a product the USER never typed.

    Distinguishes a genuine feature refinement from an AI merge that DRIFTED:
    «С двумя аккумуляторами» only repeats the user's own words, while a merge
    that returns ``кроссовки`` for «а с хорошим микрофоном» introduced a word
    absent from the raw message. Only a word that is BOTH new to the previous
    search AND absent from what the user actually wrote counts as a drifted
    product, so the feature shortcut below is not defeated by ordinary
    feature vocabulary («двумя», «аккумуляторами»).
    """

    raw_stems = {_stem(word) for word in _WORD_RE.findall(raw_text or "")}
    introduced = [
        word
        for word in (
            _content_words(follow_up.normalized_query or follow_up.original_query)
            + [word for word in follow_up.keywords if len(word) >= 3]
        )
        if _stem(word) not in known and _stem(word) not in raw_stems
    ]
    return bool(introduced)

def _only_feature_phrases(text: str) -> bool:
    """Whether every content word of ``text`` sits inside a «с X»/«без X»
    characteristic, so the message adds/removes a FEATURE and names no new
    product.

    «С двумя аккумуляторами.» and «Без провода.» refine the previous search;
    «а нужны кроссовки» does not, because «кроссовки» lies OUTSIDE every
    feature phrase. Words inside the matched spans are masked out first, and
    a refinement requires at least one feature phrase AND no leftover content
    word - an empty or pure-connector message is not treated as a refinement.
    """

    if not text:
        return False
    spans: list[tuple[int, int]] = []
    for pattern in (_REQUIRED_FEATURE_RE, _EXCLUDED_FEATURE_RE):
        spans.extend(match.span(1) for match in pattern.finditer(text))
    if not spans:
        return False
    masked = list(text)
    for start, end in spans:
        for index in range(start, end):
            masked[index] = " "
    leftover = _content_words("".join(masked))
    return not leftover

def _clean_feature(raw: str) -> str | None:
    """Accept a captured characteristic, or ``None`` when it is not one.

    Delegates to the shared :func:`services.text_matching.clean_feature`, so
    the cleanup logic (dangling-preposition stripping, bare-count rejection,
    stopword rejection) is implemented once and cannot diverge between the
    conversation merge and the query-intent fallback. A trailing dangling
    preposition is removed («с пряжкой из» -> «пряжкой») while a complete
    mid-phrase requirement is preserved («с двумя аккумуляторами»).
    """

    return clean_feature(raw, stopwords=_FEATURE_STOPWORDS)

def _extract_features(text: str) -> tuple[list[str], list[str]]:
    """Pull "с X" / "без X" characteristics out of a refinement message."""

    required = [
        feature
        for feature in (
            _clean_feature(match) for match in _clause_bounded(_REQUIRED_FEATURE_RE, text)
        )
        if feature
    ]
    excluded = [
        feature
        for feature in (
            _clean_feature(match) for match in _clause_bounded(_EXCLUDED_FEATURE_RE, text)
        )
        if feature
    ]
    return _drops_currency_feature(required), _drops_currency_feature(excluded)
#: A currency word that only RESTATES the user's price preference
#: («используй гривны», «показывай в долларах») names no feature of the
#: product. A «с X» span that says only that must never become a literal
#: ``required_features`` filter - it would demand the word «гривнах» inside a
#: marketplace title and hide every real product.
def _drops_currency_feature(features: list[str]) -> list[str]:
    """Return ``features`` without the ones that only restate a currency.

    «используй цены с гривнах» captures «цены с гривнах» as a "с X" phrase;
    demanding that phrase as a literal title requirement would hide every real
    product. A phrase whose remaining words are ALL currency names is such a
    restatement and is dropped. A phrase that also names a product or a
    material keeps its genuine words, so a real requirement is never lost.
    """

    kept: list[str] = []
    for feature in features:
        words = [
            word.casefold().replace("ё", "е")
            for word in _WORD_RE.findall(feature)
            if len(word) >= 2
        ]
        content = [
            word
            for word in words
            if not _is_currency_restatement(word)
            and word not in _CURRENCY_STEMS
            and not _is_currency_display_word(word)
        ]
        if words and not content:
            logger.debug("Currency restatement %r is not a product feature", feature)
            continue
        kept.append(feature)
    return kept

def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        lowered = value.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        result.append(value)
    return result

def _same_feature(left: str, right: str) -> bool:
    """Whether two feature phrases mean the same thing for the filter.

    "с микрофоном" followed by "без микрофона" must cancel out, but a plain
    string comparison sees two different words. The comparison therefore goes
    through the same word-level matcher the feature filter itself uses: an
    inflected form of the same feature is recognised, while a genuinely
    different feature is not ("проводные" never cancels "беспроводные").
    An empty phrase never matches, so a blank value can cancel nothing.
    """

    return phrase_in_text(left, right) or phrase_in_text(right, left)

def merge_feature_lists(
    previous: SearchRequest, raw_text: str
) -> tuple[list[str], list[str]]:
    """The feature lists a deterministic merge of ``raw_text`` into ``previous`` produces.

    Single source of truth"

    Single source of truth for the feature-merge semantics («с X» accumulates
    and cancels a same-meaning «без X», and the other way round too):
    :func:`deterministic_merge` APPLIES this result, and the AI merge
    acceptance gate (``services.ai_service.AIService._merge_is_safe``)
    COMPARES the model's lists against it - so an AI answer that omits a
    field can never erase an established constraint, and a new constraint
    only lands when this very message's «с X»/«без X» parsing produced it.
    """
    required, excluded = _extract_features(raw_text)
    merged_required = list(previous.required_features)
    merged_excluded = list(previous.excluded_features)
    if required:
        merged_required = _unique([*merged_required, *required])
        merged_excluded = [
            feature
            for feature in merged_excluded
            if not any(_same_feature(feature, item) for item in required)
        ]
    if excluded:
        merged_excluded = _unique([*merged_excluded, *excluded])
        merged_required = [
            feature
            for feature in merged_required
            if not any(_same_feature(feature, item) for item in excluded)
        ]
    return merged_required, merged_excluded

def feature_lists_equivalent(actual: list[str], expected: list[str]) -> bool:
    """Whether two feature lists mean the SAME constraints for the filter.

    Order, duplicates and inflection are irrelevant: every phrase of one
    side must find its meaning on the other side through :func:`_same_feature`
    (the very word-level matcher the feature filter itself uses, so
    «микрофон» and «микрофоном» are ONE feature while «проводные» and
    «беспроводные» are two). An empty list is equivalent only to an empty
    list: a missing constraint is a different request, not a rewording.
    """
    return all(
        any(_same_feature(item, want) for want in expected) for item in actual
    ) and all(
        any(_same_feature(item, got) for got in actual) for item in expected
    )

def deterministic_merge(
    previous: SearchRequest, follow_up: SearchRequest, raw_text: str
) -> tuple[SearchRequest, bool]:
    """Merge a follow-up into the previous request, or replace it.

    Filter fields present in the follow-up REPLACE the previous value (a new
    budget is a new budget); characteristics accumulate; the product concept
    (the catalog query) is kept from the previous request, because a
    refinement by definition does not restate it.
    """

    if not is_refinement(previous, follow_up, raw_text):
        logger.info("Conversation reset: %r starts a new search", raw_text)
        return follow_up, False

    merged = previous.model_copy(deep=True)

    count_only = is_count_only_follow_up(raw_text or "") is not None
    for name in (
        "max_price",
        "min_price",
        "min_rating",
        "min_orders",
        "quantity",
        "result_limit",
        "currency",
        "category",
        "country",
    ):
        value = getattr(follow_up, name)
        if value is not None:
            setattr(merged, name, value)
    if count_only:
        # «Покажи 5», «Сделай три варианта», «ещё 4»: only HOW MANY, never
        # WHICH. The number comes from the user's own wording through the one
        # shared count extractor, and every other constraint of the current
        # search (product, features, budget, currency) is kept byte-for-byte.
        # Nothing else of the parser's reading of such a sentence is trusted:
        # a count-only phrase is request framing, not product data.
        merged.result_limit = is_count_only_follow_up(raw_text or "")
        logger.info(
            "Result count changed to %s; the current search is preserved",
            merged.result_limit,
        )
        return merged, True
    lowered = (raw_text or "").lower()
    if any(marker in lowered for marker in _CLEAR_PRICE_MARKERS):
        merged.max_price = None
        merged.min_price = None

    merged.required_features, merged.excluded_features = merge_feature_lists(
        previous, raw_text
    )

    merged.normalized_query = previous.normalized_query or follow_up.normalized_query
    merged.original_query = raw_text
    merged.keywords = _unique([*previous.keywords, *follow_up.keywords])
    merged.preferred_marketplaces = (
        previous.preferred_marketplaces or follow_up.preferred_marketplaces
    )
    if merged.mode not in (SearchMode.SEARCH, SearchMode.DISCOVERY, SearchMode.CATEGORY):
        merged.mode = SearchMode.SEARCH
    logger.info(
        "Conversation refinement applied: %r -> query=%r max_price=%s "
        "required=%s excluded=%s",
        raw_text,
        merged.normalized_query,
        merged.max_price,
        merged.required_features,
        merged.excluded_features,
    )
    return merged, True

async def resolve_request(
    previous: SearchRequest | None,
    follow_up: SearchRequest,
    raw_text: str,
    ai_service=None,
) -> tuple[SearchRequest, bool]:
    """Effective request for a message, plus whether it refined a search.

    AI (when available) may produce a smarter merge; the result is validated
    and silently falls back to :func:`deterministic_merge` otherwise.
    """

    if previous is None:
        return follow_up, False
    # A count-only follow-up («покажи 5 вариантов») can only mean "how many
    # of the CURRENT search", so the deterministic merge is the whole
    # answer: there is nothing semantic to merge and no AI call to spend.
    if is_count_only_follow_up(raw_text or "") is not None:
        return deterministic_merge(previous, follow_up, raw_text)

    # Latency: the USER'S OWN WORDS decide a product switch (``is_refinement``
    # reads ``raw_text`` first). When they already name a NEW product, the
    # deterministic merge resets and any AI merge would be REJECTED by the very
    # same ``is_refinement`` gate below (it re-reads the same ``raw_text``), so
    # the expensive ``merge_search_requests`` call is pure waste and is skipped.
    # That call is the measured ``resolve_ms`` bottleneck (~13s in production on
    # «И еще нужен инструмент ...», a message that introduces a new product).
    # When the words do NOT name a new product the AI merge may still produce a
    # smarter refinement, so it is kept exactly as before.
    if (
        ai_service is None
        or not getattr(ai_service, "is_available", False)
        or _names_new_product(raw_text or "", previous) is not None
    ):
        return deterministic_merge(previous, follow_up, raw_text)

    merged = await ai_service.merge_search_requests(previous, follow_up, raw_text)
    if isinstance(merged, SearchRequest):
        if is_refinement(previous, merged, raw_text):
            return merged, True
        # A merge that does not refine is either a genuine product switch
        # or a model that DRIFTED into another product. Defer to the
        # deterministic merge, which preserves the previous product for
        # feature-only follow-ups and resets only on an explicit switch —
        # so a drift cannot silently drop the product the user asked for.
        logger.info(
            "AI merge did not refine; deferring to the deterministic merge"
        )
        return deterministic_merge(previous, follow_up, raw_text)
    if merged is not None:
        # A model layer that returns anything other than a validated
        # request (a string, a dict, a foreign object) is treated exactly
        # like an unusable answer: the deterministic merge takes over.
        logger.warning(
            "AI merge returned %s instead of a SearchRequest; "
            "using the deterministic merge",
            type(merged).__name__,
        )

    return deterministic_merge(previous, follow_up, raw_text)
