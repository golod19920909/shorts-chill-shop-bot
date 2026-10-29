"""G25.2-B Stage 1 — production structured query-intent parser.

READ-ONLY with respect to the search pipeline: this module is shipped
together with its tests only; integration into the Find Product flow is
Stage 2 (``handlers/search.py``, ``services/search_service.py``,
``services/relevance.py`` and ``models/search_request.py`` stay untouched).

``parse_query_intent(raw_query, ai_service)`` turns a free-form shopping
query into a validated :class:`StructuredQueryIntent` plus small
:class:`IntentParseMeta` diagnostics:

* **AI path** — ONE call through the EXISTING
  ``services.ai_service.AIService._generate`` (the same Gemini client and
  configuration; no second client, no changes to ``ai_service.py`` or
  ``QUERY_PARSING_PROMPT``) with the intent-only :data:`INTENT_PROMPT`;
  the answer is strictly validated and silently discarded when unusable.
* **Deterministic fallback** — the proven G25.2-A rules: budget/currency
  extraction («грн»/«₴»/«UAH» → UAH, currency words never remain search
  terms), «для X» → compatibility (never product_type), recipient/context
  stays compatibility («подарок для кота» → product «подарок»), specs
  (65W / 1 ТБ / 18V / 20000 мАч / 4K / NVMe) → required_attributes,
  general brand lexicon (iPhone → Apple), model/generation NEVER invented.

Guarantees: the parser never raises into the caller (AI failure,
malformed JSON, schema violations and internal bugs all end in a usable
intent + meta); a module circuit breaker opens after a hard AI failure
for a bounded cooldown so a failing service is not hammered — during the
cooldown calls go straight to the deterministic fallback, and after it a
single controlled probe decides whether AI intent is served again.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from models.search_request import SearchRequest
from services.text_matching import (
    clean_feature,
    is_negation_clause,
    negation_payload,
    strip_option_count,
)

logger = logging.getLogger(__name__)


class IntentBudget(BaseModel):
    """Numeric budget of a query; always structurally valid.

    Values are sanitized BEFORE construction (negative numbers are
    dropped, currency aliases normalized), so this model cannot be built
    in an invalid state by :func:`normalize_intent` / the fallback.
    """

    model_config = ConfigDict(extra="forbid")

    min: float | None = Field(default=None, ge=0)
    max: float | None = Field(default=None, ge=0)
    currency: str | None = None


class StructuredQueryIntent(BaseModel):
    """Structured shopping intent produced by :func:`parse_query_intent`.

    Scalars default to ``None``, lists to ``[]``; unknown keys are never
    accepted (``extra="forbid"``).
    """

    model_config = ConfigDict(extra="forbid")

    product_type: str | None = None
    brand: str | None = None
    model: str | None = None
    generation: str | None = None
    required_attributes: list[str] = Field(default_factory=list)
    optional_attributes: list[str] = Field(default_factory=list)
    compatibility: list[str] = Field(default_factory=list)
    budget: IntentBudget = Field(default_factory=IntentBudget)
    excluded_product_types: list[str] = Field(default_factory=list)


class IntentParseMeta(BaseModel):
    """Parse diagnostics for Stage 2+ logging (deliberately small)."""

    model_config = ConfigDict(extra="forbid")

    #: "ai" when a validated AI answer was used, else "fallback".
    source: str = "fallback"
    #: ai_unavailable | circuit_open | ai_returned_none | ai_exception:* |
    #: invalid_json | invalid_schema | empty_query | internal_error | None
    error: str | None = None
    model: str | None = None
    latency_ms: float | None = None


# ---------------------------------------------------------------------------
# Lexicons proven in G25.2-A. The brand dictionary is intentionally NOT
# expanded here (Stage 1 keeps proven behavior only).
# ---------------------------------------------------------------------------

CURRENCY_ALIASES: dict[str, str] = {
    # UAH
    "грн": "UAH", "грн.": "UAH", "гривна": "UAH", "гривны": "UAH",
    "гривен": "UAH", "₴": "UAH", "uah": "UAH",
    # USD
    "доллар": "USD", "доллара": "USD", "долларов": "USD", "доллары": "USD",
    "долар": "USD", "долара": "USD", "доларів": "USD",  # Ukrainian
    "usd": "USD", "$": "USD",
    "dollar": "USD", "dollars": "USD",
    "us dollar": "USD", "us dollars": "USD",
    # EUR
    "евро": "EUR", "евр": "EUR", "eur": "EUR", "€": "EUR",
    "євро": "EUR",  # Ukrainian
    "euro": "EUR", "euros": "EUR",
    # RUB
    "руб": "RUB", "рубля": "RUB", "рублей": "RUB", "рублей": "RUB",
    "рубль": "RUB", "рублів": "RUB",  # Ukrainian
    "rub": "RUB", "₽": "RUB",
    "ruble": "RUB", "rubles": "RUB",
    # CNY
    "юан": "CNY", "юань": "CNY", "юаня": "CNY", "юаней": "CNY", "юанів": "CNY",
    "cny": "CNY", "¥": "CNY",
    "yuan": "CNY", "yuans": "CNY",
    # GBP
    "gbp": "GBP", "£": "GBP",
    "pound": "GBP", "pounds": "GBP",
}

#: Words that may complete a spec token but are NOT grounded in the query
#: by themselves (e.g. «20000 мАч» where the query only says «20000»).
UNIT_WORDS = (
    "мАч", "мач", "mah", "ТБ", "TB", "Гц", "Hz", "Вт", "W", "V",
    "Ah", "Ач", "дюйм", "дюйма", "дюймов", "K", "k",
    # Stage 16 (A-4): units proven missing by the Stage-14.9 audit matrix
    # (#34 кг, #40 литров, #24 метров, #33 мм, #09 ГБ, #41 размер).
    # Without them the number detaches from its unit and the spec becomes
    # a bare digit the Stage-11 matcher can never confirm.
    "кг", "ГБ", "гб", "gb", "GB", "литров", "мм", "ММ", "метров",
    "размер",
)

#: General brand lexicon (NOT example-specific): used only to recognize
#: brand/model tails like «дворники Volkswagen Golf 7».
KNOWN_BRANDS = (
    "apple", "iphone", "samsung", "galaxy", "xiaomi", "huawei", "honor",
    "sony", "jbl", "anker", "baseus", "ugreen", "lenovo", "dell", "hp",
    "asus", "acer", "lg", "philips", "makita", "bosch", "dewalt",
    "milwaukee", "ryobi", "toyota", "volkswagen", "vw", "honda", "ford",
    "hyundai", "kia", "nissan", "mazda", "skoda", "audi", "bmw", "chery",
    "geely", "byd", "dyson", "roborock", "nikon", "canon", "kingston",
    "crucial", "sandisk", "intel", "amd", "nvidia", "gopro", "dji",
    "logitech", "razer", "msi", "gigabyte", "microsoft", "google",
    "toshiba", "seagate", "marshall", "bose", "sennheiser",
)

#: Strongly implied brand (product line -> maker). Used for grounding only.
IMPLICIT_BRANDS: dict[str, tuple[str, ...]] = {
    "apple": ("iphone", "ipad", "airpods", "macbook", "imac", "apple watch",
              "applewatch"),
    "sony": ("ps5", "playstation"),
}

#: Tech/interface spec tokens moved from the product phrase to attributes.
TECH_ATTRS = (
    "nvme", "sata", "m.2", "usb-c", "usb c", "type-c", "type c",
    "oled", "lcd", "ips", "qhd", "uhd", "ip67", "ip68", "tws",
)

BUDGET_MAX_MARKERS = (
    "до", "under", "up to", "less than", "cheaper than", "no more than",
    "not more than", "не более",
    "не дороже", "максимум", "макс", "<=", "<",
)
BUDGET_MIN_MARKERS = (
    "от", "from", "above", "over", "at least", "не менее", "минимум",
    "мин", ">=", ">",
)

MOOD_WORDS = (
    "что-нибудь", "что нибудь", "что-то", "чего-нибудь", "чего нибудь",
    "что угодно",
)

_LEADING_JUNK = (
    "хочу", "нужен", "нужна", "нужно", "купить", "найди", "найти",
    "подбери", "пожалуйста", "лучший", "лучшая", "лучшие", "хороший",
    "хорошая", "хорошее", "хорошие", "хорошо", "качественный",
    "качественная", "интересный", "интересная", "дешевый", "дешевая",
    "дешевую", "дешевле", "недорогой", "недорогая", "недорогие", "мне",
    "меня", "но", "надо", "нужны",
    # English command / scaffolding words of a follow-up («find one with
    # two batteries»): they never name a product, so the stranded «one»
    # cannot become a false product switch. Product content words
    # (nouns, specs like «batteries») are untouched by this list.
    "find", "show", "search", "look", "give", "get", "need", "want",
    "please", "one", "ones", "cheaper", "alternative", "alternatives",
)

#: «с X» / «со X» / «with X» — captures 1-2 word groups after the
#: introducer so «с двумя аккумуляторами», «with 12V» and «с USB-C»
#: become candidate required features (cleaned + validated by the caller).
_REQUIRED_FEATURE_RE = re.compile(
    r"(?:^|\s)(?:с|со|with)\s+([\w\-]+(?:\s+[\w\-]+)?)", re.IGNORECASE
)

#: Bare stopword stems that turn a captured «с X» span into a non-feature
#: (quality / price / delivery talk), mirrored from conversation._FEATURE_STOPWORDS.
_REQUIRED_FEATURE_STOPWORDS = frozenset({
    "хорош", "отличн", "высок", "низк", "лучш", "положительн", "плох",
    "отзыв", "рейтинг", "оценк", "звезд", "заказ", "продаж", "доставк",
    "скидк", "цен", "стоим", "штук", "качеств", "нов", "повыше", "бюджет",
})

# ---------------------------------------------------------------------------
# Circuit breaker + telemetry (kept minimal on purpose)
# ---------------------------------------------------------------------------

def _fresh_stats() -> dict[str, Any]:
    return {
        "attempted": 0,
        "ok": 0,
        "failed": 0,
        "invalid_json": 0,
        "invalid_schema": 0,
        "fallback_used": 0,
        "circuit_opens": 0,
        "last_error": None,
        "last_model": None,
    }


#: In-process counters for Stage 2 diagnostics; no external telemetry.
INTENT_AI_STATS: dict[str, Any] = _fresh_stats()

#: Bounded, time-based cooldown after a hard AI failure (``_generate``
#: returning None or raising: quota, timeout, transport - the intent layer
#: cannot tell them apart). While it lasts NO Gemini request is made for
#: intent, so a failing service costs at most ONE request per cooldown
#: window instead of one per incoming message. When the cooldown expires
#: the next message becomes a single half-open PROBE: concurrent messages
#: arriving while the probe is in flight still use the deterministic
#: fallback. A successful probe closes the circuit again; a failed probe
#: re-opens it for another cooldown. No retries are added in either
#: direction, so an outage never multiplies Gemini traffic.
INTENT_CIRCUIT_COOLDOWN_SECONDS = 60.0

#: ``monotonic`` timestamp until which AI intent calls are bypassed
#: (0.0 = circuit closed).
_CIRCUIT_OPEN_UNTIL = 0.0

#: True while the single post-cooldown recovery probe is in flight.
_CIRCUIT_PROBE_IN_FLIGHT = False


def _clock() -> float:
    """Monotonic clock behind the circuit breaker (a hook for tests)."""

    return time.monotonic()


def reset_ai_state() -> None:
    """Reset the circuit breaker and counters (tests / diagnostics)."""

    global _CIRCUIT_OPEN_UNTIL, _CIRCUIT_PROBE_IN_FLIGHT
    _CIRCUIT_OPEN_UNTIL = 0.0
    _CIRCUIT_PROBE_IN_FLIGHT = False
    INTENT_AI_STATS.clear()
    INTENT_AI_STATS.update(_fresh_stats())


def is_circuit_open() -> bool:
    """Whether AI calls are currently bypassed (cooldown or probe running)."""

    return _CIRCUIT_PROBE_IN_FLIGHT or _clock() < _CIRCUIT_OPEN_UNTIL


# ---------------------------------------------------------------------------
# Coercers — every value entering the schema passes through one of these
# ---------------------------------------------------------------------------

def _s(value: Any) -> str | None:
    """Scalar -> stripped non-empty string; containers/bools -> None.

    Stricter than the G25.2-A prototype (which stringified everything):
    a dict/list AI value must never become a search term.
    """
    if value is None or isinstance(value, (dict, list, bool)):
        return None
    if isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _as_list(value: Any) -> list[str]:
    """Coerce to a list of unique non-empty strings; invalid items dropped."""

    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        s = _s(item)
        if s is not None and s not in out:
            out.append(s)
    return out


def _to_number(value: Any) -> float | int | None:
    """Coerce budget numbers: int/float/'1 500'/'1.500,50' -> number."""

    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        raw = value.strip().replace("\xa0", " ").replace(" ", "")
        raw = raw.replace(",", ".")
        raw = re.sub(r"[^\d.]", "", raw)
        if not raw:
            return None
        try:
            num = float(raw)
        except ValueError:
            return None
        if num.is_integer():
            return int(num)
        return num
    return None


def _nonneg(value: Any) -> float | int | None:
    """Budget-safe number: coercible and >= 0, else None (no negatives)."""

    num = _to_number(value)
    if num is None or num < 0:
        return None
    return num


def normalize_currency(value: Any) -> str | None:
    """грн / ₴ / 'UAH' / 'uah.' -> 'UAH'; unknown/absent -> None."""

    s = _s(value)
    if s is None:
        return None
    key = s.strip().lower()
    if key in CURRENCY_ALIASES:
        return CURRENCY_ALIASES[key]
    up = s.strip().upper()
    if up in ("UAH", "USD", "EUR", "RUB", "CNY", "GBP"):
        return up
    # e.g. «грн.» or trailing punctuation
    stripped = key.rstrip(".")
    return CURRENCY_ALIASES.get(stripped)


# ---------------------------------------------------------------------------
# Validation / normalization of raw AI JSON
# ---------------------------------------------------------------------------

_INTENT_KEYS = (
    "product_type", "brand", "model", "generation",
    "required_attributes", "optional_attributes", "compatibility",
    "budget", "excluded_product_types",
)


def empty_intent() -> StructuredQueryIntent:
    """A structurally valid intent with every field empty."""

    return StructuredQueryIntent()


def normalize_intent(raw: Any) -> StructuredQueryIntent:
    """Validate raw AI output into the strict production schema.

    Never raises. Guarantees:

    - unknown keys dropped; missing keys default to ``None`` / ``[]``;
    - invalid scalar/list types are safely normalized (containers and
      booleans never become strings);
    - budget numbers coerced and clamped to ``>= 0``, currency aliases
      normalized (грн / ₴ → UAH);
    - compatibility NEVER overwrites product_type: a missing product_type
      stays ``None`` even when compatibility is present;
    - model/generation only when actually stated - never invented;
    - empty arrays stay ``[]`` (not None), nulls stay ``None``.
    """
    try:
        if not isinstance(raw, dict):
            return StructuredQueryIntent()

        budget_raw = raw.get("budget")
        bmin = bmax = bcur = None
        if isinstance(budget_raw, dict):
            bmin = _nonneg(budget_raw.get("min"))
            bmax = _nonneg(budget_raw.get("max"))
            bcur = normalize_currency(budget_raw.get("currency"))
        elif isinstance(budget_raw, (int, float)) and not isinstance(
                budget_raw, bool):
            bmax = _nonneg(budget_raw)

        # Negation reclassification (Stage 44-B7). A «без X» / «without X» /
        # «не X» phrase is an ABSENCE requirement, never a POSITIVE attribute to
        # match. The model sometimes emits such a phrase into required_attributes
        # (#30 «пылесос без мешка», #33 «без провода»/«без шумоподавления»), where
        # it is semantically wrong (it is not a spec a title confirms) and merely
        # pollutes the retrieval phrase. The DETERMINISTIC fallback already routes
        # «без X» to excluded_product_types (Stage 16/22, locked by tests), so this
        # guard makes the AI path CONSISTENT with it: every required_attribute
        # that LEADS with a negation is moved (its noun, marker stripped) into
        # excluded_product_types. This is non-destructive (both intent fields feed
        # RANKING only, never a hard product filter), needs no antonym dictionary,
        # and preserves the genuine exclusions the model already produced. The
        # ideal positive reading of «без провода» = «wireless» needs an antonym
        # lexicon the project deliberately does not add; excluding the named thing
        # is the safe schema-supported proxy (see services.text_matching.negation_payload).
        required_attrs = _as_list(raw.get("required_attributes"))
        excluded_types = _as_list(raw.get("excluded_product_types"))
        positive_attrs: list[str] = []
        for attribute in required_attrs:
            payload = negation_payload(attribute) if is_negation_clause(attribute) else ""
            if payload and payload.casefold() not in {
                existing.casefold() for existing in excluded_types
            }:
                excluded_types.append(payload)
            elif not payload and not is_negation_clause(attribute):
                positive_attrs.append(attribute)
            # A negation with no usable noun («без») is dropped, not kept as a
            # positive attribute and not added as an empty exclusion.

        return StructuredQueryIntent(
            product_type=_s(raw.get("product_type")),
            brand=_s(raw.get("brand")),
            model=_s(raw.get("model")),
            generation=_s(raw.get("generation")),
            required_attributes=positive_attrs,
            optional_attributes=_as_list(raw.get("optional_attributes")),
            compatibility=_as_list(raw.get("compatibility")),
            excluded_product_types=excluded_types,
            budget=IntentBudget(min=bmin, max=bmax, currency=bcur),
        )
    except Exception:  # pragma: no cover - defensive: never raise
        logger.exception("normalize_intent failed; returning empty intent")
        return StructuredQueryIntent()


# ---------------------------------------------------------------------------
# Deterministic fallback parser (G25.2-A proven rules)
# ---------------------------------------------------------------------------

def _extract_budget(text: str) -> tuple[str, dict[str, Any]]:
    """Extract budget from text. Returns (cleaned_text, budget dict).

    Handles «до 3000 грн» / «от 1500 грн» / «under $50» /
    «no more than $40» and the bare «3000 грн» form (number + currency
    word -> max budget). A currency symbol glued to the number («$40»)
    counts as the currency word for the marker form as well, so the
    budget is not lost when the marker regex consumes the number.
    """
    budget: dict[str, Any] = {"min": None, "max": None, "currency": None}
    if not text:
        return text, budget

    num_pat = r"(\d+(?:[\s\u00a0]\d{3})*(?:[.,]\d+)?)"

    # Currency word anywhere in the query (marker spans may swallow it).
    # A symbol glued to the number («$40») IS the currency: the trailing
    # boundary must not reject a digit after a pure symbol, otherwise the
    # cheapest budget form never yields a currency.
    _SYMBOL_CURRENCIES = {
        key for key in CURRENCY_ALIASES
        if len(key) == 1 and not key.isalnum()
    }
    _WORD_CURRENCIES = [
        c for c in sorted(CURRENCY_ALIASES, key=len, reverse=True)
        if c not in _SYMBOL_CURRENCIES
    ]
    _SYMBOL_PATS = "|".join(
        re.escape(c) for c in sorted(_SYMBOL_CURRENCIES, key=len, reverse=True)
    )
    cur_pats = "|".join(
        re.escape(c) for c in sorted(CURRENCY_ALIASES, key=len, reverse=True))
    word_pat = (
        r"(?<![0-9a-zа-я])(" + "|".join(
            re.escape(c) for c in _WORD_CURRENCIES
        ) + r")(?![0-9a-zа-я])"
    )
    sym_pat = (
        r"(?<![0-9a-zа-я])(" + _SYMBOL_PATS + r")(?![a-zа-я])"
        if _SYMBOL_PATS else r"(?!)"
    )
    m_cur = re.search(word_pat, text, re.IGNORECASE)
    m_sym = re.search(sym_pat, text)
    if m_cur:
        cur_code = normalize_currency(m_cur.group(1))
    elif m_sym:
        cur_code = normalize_currency(m_sym.group(1))
    else:
        cur_code = None

    def _match_marker(patterns: tuple[str, ...]) -> re.Match | None:
        pats = "|".join(re.escape(p) for p in sorted(
            patterns, key=len, reverse=True))
        # NB: alternatives MUST be wrapped - otherwise \s*num would bind
        # only to the last branch and «от» inside «кота» would match.
        # A currency symbol glued to the number («no more than $40»)
        # is skipped explicitly: it is the currency word, not part of
        # the amount, and without it the marker form never matches.
        return re.search(r"(?:" + pats + r")\s*[$€₽¥]?\s*" + num_pat,
                         text, re.IGNORECASE)

    m = _match_marker(BUDGET_MAX_MARKERS)
    if m:
        budget["max"] = _to_number(m.group(1))
        text = text[:m.start()] + " " + text[m.end():]
    else:
        m = _match_marker(BUDGET_MIN_MARKERS)
        if m:
            budget["min"] = _to_number(m.group(1))
            text = text[:m.start()] + " " + text[m.end():]

    m2 = re.search(num_pat + r"\s*(" + cur_pats + r")", text, re.IGNORECASE)
    if m2 and budget["max"] is None and budget["min"] is None:
        amount = _to_number(m2.group(1))
        if amount is not None:
            budget["max"] = amount
        text = text[:m2.start()] + " " + text[m2.end():]

    if (budget["max"] is not None or budget["min"] is not None) and \
            budget["currency"] is None:
        budget["currency"] = cur_code

    return text, budget


def _strip_currency_words(text: str) -> str:
    """Remove leftover currency words so they never remain search terms."""

    if not text:
        return ""
    cur_pats = "|".join(
        re.escape(c) for c in sorted(CURRENCY_ALIASES, key=len, reverse=True))
    text = re.sub(r"(?<![0-9a-zа-я])(" + cur_pats + r")(?![0-9a-zа-я])",
                  " ", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip()


def _extract_specs(text: str, *,
                   standalone_numbers: bool = False) -> tuple[str, list[str]]:
    """Move numeric/tech specs out of a phrase -> (phrase, specs).

    ``standalone_numbers`` extracts bare >=2-digit numbers too; it is ON
    only for the product part (compat tails keep «Camry 70», «Golf 7»).
    """
    specs: list[str] = []
    if not text:
        return "", specs
    units = "|".join(sorted((re.escape(u) for u in UNIT_WORDS),
                            key=len, reverse=True))

    def _sub(m: re.Match) -> str:
        num = m.group(1).replace(" ", "").replace("\u00a0", "")
        specs.append(f"{num} {m.group(2)}".strip())
        return " "

    text = re.sub(rf"(\d+(?:[.,]\d+)?)\s*({units})\b", _sub, text,
                  flags=re.IGNORECASE)
    for tech in TECH_ATTRS:
        pat = re.escape(tech).replace(r"\ ", r"[\s\-]+")
        if re.search(pat, text, re.IGNORECASE):
            specs.append(tech.upper() if len(tech) <= 4 else tech)
            text = re.sub(pat, " ", text, flags=re.IGNORECASE)
    if standalone_numbers:
        def _num(m: re.Match) -> str:
            specs.append(m.group(1).replace(" ", "").replace("\u00a0", ""))
            return " "
        text = re.sub(r"\b(\d{2,})\b", _num, text)
    text = re.sub(r"\s+", " ", text).strip(" ,.-")
    return text, specs


def _detect_brand(query: str) -> str | None:
    """First brand implied/mentioned in the query (general lexicon only).

    Implicit mappings run FIRST: «iPhone» is a product line, so the brand
    is Apple («iPhone -> Apple»), not "Iphone".
    """
    q = (query or "").casefold().replace("ё", "е")
    for brand, triggers in IMPLICIT_BRANDS.items():
        for trig in triggers:
            if re.search(r"(?<![a-zа-я])" + re.escape(trig) + r"(?![a-zа-я])",
                         q):
                return brand.capitalize()
    for brand in KNOWN_BRANDS:
        if re.search(r"(?<![a-zа-я])" + re.escape(brand) + r"(?![a-zа-я])",
                     q):
            return brand.capitalize()
    return None


def _detect_trailing_compat(part: str) -> tuple[str, str | None]:
    """Split trailing brand/model tail: «дворники Volkswagen Golf 7»."""
    if not part:
        return part, None
    tokens = part.split()
    q = part.casefold().replace("ё", "е")
    for brand in KNOWN_BRANDS:
        pat = re.compile(r"(?<![a-zа-я])" + re.escape(brand) + r"(?![a-zа-я])")
        m = pat.search(q)
        if not m:
            continue
        acc = 0
        idx = None
        for i, tok in enumerate(tokens):
            start = q.find(tok.casefold(), acc)
            if start < 0:
                start = acc
            if start <= m.start() < start + max(len(tok), 1):
                idx = i
                break
            acc = start + max(len(tok), 1)
        if idx is None or idx == 0:
            continue
        tail = tokens[idx:]
        if all(re.fullmatch(r"[0-9A-Za-z.\-]+", t) for t in tail):
            return " ".join(tokens[:idx]), " ".join(tail)
    return part, None


#: Function words that END an English «without X» exclusion: the excluded noun
#: phrase never continues past them («... without charger for travel» ->
#: «charger»). Articles are also listed so a stray mid-phrase article stops the
#: capture (the leading article is already consumed by the regex). Bounded and
#: conservative: it only truncates, it never turns a non-negation into one.
_ENGLISH_EXCLUSION_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "nor", "for", "with", "of", "in", "on",
    "but", "to",
})


def _clean_english_exclusion_segment(segment: str) -> str:
    """Clean ONE comma-separated English exclusion segment to its noun terms.

    Drops a leading conjunction/article («and adapter» -> «adapter»), stops at
    the first function word, splits internal hyphen/slash, and keeps only
    all-lower or ALL-CAPS parts of length >= 2 — the same rules the single-item
    «without» path uses, so a list item is protected exactly like a lone item.
    Returns "" when nothing meaningful remains.
    """
    toks = segment.split()
    while toks and toks[0].casefold() in {"and", "or", "a", "an", "the"}:
        toks = toks[1:]
    kept: list[str] = []
    for tok in toks:
        if tok.casefold() in _ENGLISH_EXCLUSION_STOPWORDS:
            break
        kept.append(tok)
    parts: list[str] = []
    for tok in kept:
        for part in re.split(r"[-/]", tok):
            if len(part) < 2:
                continue
            if part == part.casefold() or part == part.upper():
                parts.append(part)
    return " ".join(parts)


def _detect_exclusions(text: str) -> list[str]:
    """«без X» / «не нужен X» patterns -> excluded_product_types.

    Captures EVERY «без X» occurrence, not only the first one, so
    comma-separated lists («без А, без Б») and «и без»-separated clauses
    («без А и без Б») each yield their own exclusion. Each exclusion is
    bounded by a comma, sentence-ending punctuation, another «без» marker,
    or end of string. A trailing conjunction before another «без»
    («гриля и» → «гриля») is dropped.
    """
    out: list[str] = []
    if not text:
        return out

    # --- Russian «без X»: find ALL occurrences, not just the first -------
    # \b (not \s+) so a leading «без» at string start is also captured.
    for part in re.split(r"\bбез\s+", text, flags=re.IGNORECASE)[1:]:
        # Bound the exclusion at comma / sentence-ending punctuation.
        for boundary in (",", ";", ".", "!", "?"):
            idx = part.find(boundary)
            if idx >= 0:
                part = part[:idx]
        # Drop a trailing conjunction before another «без» («А и» → «А»).
        part = re.sub(r"\s+(?:и|или|а|но)\s*$", "", part,
                      flags=re.IGNORECASE)
        part = part.strip(" ,.-")
        if part and part not in out:
            out.append(part)

    m = re.search(r"\bне\s+(?:нужен|нужна|нужно)\s+([^,.;!?]+)",
                  text, re.IGNORECASE)
    if m:
        exclusion = m.group(1).strip()
        if exclusion and exclusion not in out:
            out.append(exclusion)
    # Stage 27/29: an unambiguous English «without X» is the same exclusion as
    # «без X». Stage 27 captured ONE word; Stage 29 extends it to a BOUNDED
    # noun phrase (1-3 words) so the meaningful head noun of a multi-word
    # exclusion (e.g. «power cable» -> «cable») also reaches
    # intent_excluded_product_types and the Stage-25 short-query guard
    # (query_variants._excluded_stems/_reintroduces_excluded). A single-word
    # exclusion still yields one word, so Stages 26-27 behavior is preserved.
    #
    # Stage 31 supersedes the Stage-29 case-sensitive 1-3 word rule: capture
    # the FULL bounded «without X» phrase (any case, internal hyphen/slash, up
    # to a small word bound), stopping at the first function word or
    # punctuation. A Title-Case / mixed token inside the phrase is treated
    # conservatively as a possible brand/model: it is CONSUMED into the span
    # but NOT added as an excluded product type, so the phrase continues to its
    # lower-case/ALL-CAPS head noun («Bosch battery» -> protect «battery», keep
    # «Bosch»). Only all-lower or all-upper parts become excluded types, so
    # Stages 26-29 lower-case/ALL-CAPS results are unchanged. No brand
    # dictionary, no «no»/«not» broadening.
    # Stage 31.3: a CONSERVATIVE comma-separated list («without A, B, and C»)
    # captures EVERY item, not only the first. Confined to an already-detected
    # «without» context and accepted only when the span is a CLEAR list: >= 2
    # comma segments whose FINAL segment is joined by and/or (Oxford style).
    # A lone comma boundary («without battery, brush») is NOT a list and keeps
    # the established single-item result below. Each item is cleaned with the
    # same rules as a lone «without» item, so every excluded noun reaches the
    # unchanged Stage-25 guard. No brand dictionary, no «no»/«not» broadening,
    # and positive queries (no «without») never reach this branch.
    ml = re.search(
        r"(?i:\bwithout\s+)(?i:(?:a|an|the)\s+)?([^.;!?\n]+)", text
    )
    handled_list = False
    if ml:
        segments = [s.strip() for s in ml.group(1).split(",")]
        segments = [s for s in segments if s]
        last = segments[-1].split() if segments else []
        is_clear_list = (
            len(segments) >= 2
            and bool(last)
            and last[0].casefold() in {"and", "or"}
        )
        if is_clear_list:
            items: list[str] = []
            for seg in segments:
                cleaned = _clean_english_exclusion_segment(seg)
                if cleaned and cleaned not in items:
                    items.append(cleaned)
            if items:
                out.extend(items)
                handled_list = True
    if not handled_list:
        # Stage 31.5 (Strategy C): raise the single-item bound from 5 to 8
        # content words so the TRAILING lower-case head noun of a long
        # descriptive «without» phrase is protected (e.g. «high capacity
        # rechargeable lithium ion battery» — «battery» is the 6th word and
        # used to fall outside the old {0,4} cap and leak into a derived
        # variant). Bounded (C3 safety cap = 8) and unchanged otherwise: the
        # first stop-word still truncates («...battery for home use»), the
        # case filter still keeps brands (Bosch/Samsung/AirPods, Title-Case)
        # OUT of the exclusion, and punctuation still bounds the scan. The
        # whole trailing noun phrase is captured (V1: «battery pack» is
        # excluded whole), so the unchanged Stage-25 guard protects every word.
        m = re.search(
            r"(?i:\bwithout\s+)(?i:(?:a|an|the)\s+)?"
            r"([A-Za-z][A-Za-z0-9]*(?:[-/][A-Za-z0-9]+)*"
            r"(?:\s+[A-Za-z][A-Za-z0-9]*(?:[-/][A-Za-z0-9]+)*){0,7})",
            text,
        )
        if m:
            kept: list[str] = []
            for tok in m.group(1).split():
                if tok.casefold() in _ENGLISH_EXCLUSION_STOPWORDS:
                    break
                kept.append(tok)
            parts: list[str] = []
            for tok in kept:
                for part in re.split(r"[-/]", tok):
                    if len(part) < 2:
                        continue
                    if part == part.casefold() or part == part.upper():
                        parts.append(part)
            if parts:
                out.append(" ".join(parts))
    return out


def fallback_intent(query: str) -> StructuredQueryIntent:
    """Deterministic intent parser (G25.2-A proven), never raises.

    Steps:

    1. extract budget (грн→UAH etc.) and strip currency words;
    2. split «для X» → compatibility (recipient/context preserved);
    3. split trailing brand/model tail → compatibility;
    4. extract specs (W/TB/mAh/V/tech) from both sides → required attrs;
    5. remaining left phrase → product_type; brand lexicon → brand;
    6. exclusions from «без X». model/generation are NEVER invented.
    """
    intent: dict[str, Any] = {
        "product_type": None,
        "brand": None,
        "model": None,
        "generation": None,
        "required_attributes": [],
        "optional_attributes": [],
        "compatibility": [],
        "budget": {"min": None, "max": None, "currency": None},
        "excluded_product_types": [],
    }
    try:
        text = (query or "").strip()
        if not text:
            return normalize_intent(intent)

        text, budget = _extract_budget(text.lower())
        intent["budget"] = budget
        text = _strip_currency_words(text)

        for w in _LEADING_JUNK:
            text = re.sub(r"(?<![a-zа-я])" + re.escape(w) + r"(?![a-zа-я])",
                          " ", text, flags=re.IGNORECASE)
        text = re.sub(r"\s+", " ", text).strip()

        if not text:
            # Budget-only query: all product fields stay empty.
            return normalize_intent(intent)

        is_mood = any(mw in text for mw in MOOD_WORDS)

        compat: list[str] = []
        m = re.search(r"\bдля\s+", text, re.IGNORECASE)
        if not m:
            # Stage 14.2: «зарядка на телефон» names the TARGET exactly as
            # «зарядка для телефона» does - the same compatibility slot.
            # A tail that starts with a digit is a spec/quantity («футляр
            # на 4 телефона»), never a target, so it stays in the phrase.
            m = re.search(r"\bна\s+(?=[^\d\s])", text, re.IGNORECASE)
        if m:
            tail = text[m.end():].strip()
            head = text[:m.start()].strip()
            if tail:
                compat.append(tail)
            text = head
        text, trail = _detect_trailing_compat(text)
        if trail:
            compat.append(trail)

        for c in compat:
            c_clean, c_specs = _extract_specs(c)
            intent["required_attributes"].extend(c_specs)
            c_clean = c_clean.strip(" ,.-")
            if c_clean:
                intent["compatibility"].append(c_clean)

        text, specs = _extract_specs(text, standalone_numbers=True)
        intent["required_attributes"].extend(specs)

        # Stage 3E: positive required features from «с X»/«со X»/«with X».
        # Cleaned with the SAME shared helper the conversation merge uses
        # (services.text_matching.clean_feature): no circular import because
        # text_matching depends on nothing from this package, and the cleanup
        # logic can no longer diverge between the two call sites.
        for match in _REQUIRED_FEATURE_RE.findall(text):
            feature = clean_feature(match, stopwords=_REQUIRED_FEATURE_STOPWORDS)
            if feature:
                intent["required_attributes"].append(feature)

        seen: set[str] = set()
        deduped: list[str] = []
        for s in intent["required_attributes"]:
            key = s.casefold()
            if key not in seen:
                seen.add(key)
                deduped.append(s)
        intent["required_attributes"] = deduped

        intent["excluded_product_types"] = _detect_exclusions(query or "")

        # Strip every «без X» exclusion phrase from the retrieval text so
        # the named exclusions don't pollute product_type / normalized_query.
        for exclusion in intent["excluded_product_types"]:
            text = re.sub(
                r"\bбез\s+" + re.escape(exclusion) + r"\b",
                " ", text, flags=re.IGNORECASE,
            )
        text = re.sub(r"\s+", " ", text).strip()

        pt = text.strip(" ,.-")
        # Drop a trailing conjunction left after stripping «без X» phrases
        # («микроволновка и» → «микроволновка»).
        pt = re.sub(r"\s+(?:и|или|а|но)\s*$", "", pt, flags=re.IGNORECASE)
        # Stage 55 (F2): a requested number of OPTIONS («3 варианта», «покажи 5»)
        # is how many results to show, never a product property; keep it out of
        # product_type so it cannot leak into the retrieval phrase. A
        # UNITS-per-item quantity («3 шт») is untouched by this rule.
        pt = strip_option_count(pt)
        if is_mood and not pt:
            pt = None
        intent["product_type"] = pt or None
        intent["brand"] = _detect_brand(query or "")
        intent["model"] = None
        intent["generation"] = None
        return normalize_intent(intent)
    except Exception:  # pragma: no cover - defensive: never raise
        logger.exception("fallback_intent failed for %r", query)
        return StructuredQueryIntent()


# ---------------------------------------------------------------------------
# AI path (reuses the EXISTING AIService Gemini client — no second client)
# ---------------------------------------------------------------------------

#: Intent-only prompt (G25.2-B). Isolated from QUERY_PARSING_PROMPT, which
#: stays untouched in services/ai_service.py. The braces below are literal:
#: the prompt is concatenated, never .format()-ed.
INTENT_PROMPT = """You convert a shopping query into a structured intent JSON.
You ONLY understand intent. You never search a marketplace, never select,
rank or recommend products, and never invent information.

Rules:
- product_type: the main product requested, in the user's language.
  NEVER a compatibility target, recipient or purpose ("iPhone",
  "Toyota Camry 70", "кот" are NOT product types unless the user asks
  for that exact item).
- compatibility: devices/models/purposes the product must fit or serve,
  including "для X" targets ("для iPhone" -> compatibility: ["iPhone"]).
- brand: maker of the requested product, only if stated or strongly
  implied (iPhone -> Apple). Otherwise null.
- model / generation: of the requested product only; null if not stated.
  Never invent them.
- required_attributes: POSITIVE properties the user wants:
  (a) explicit specs/attributes stated outright ("1 ТБ", "65W", "18V",
      "20000 мАч", "4K", "NVMe", "wireless", "беспроводные");
  (b) a POSITIVE property unambiguously IMPLIED by a DEFINING absence - a
      SINGLE negation that governs exactly ONE noun and names a recognized
      product characteristic. Example: "пылесос без мешка" -> ["безмешковый"];
      "без провода" -> ["беспроводные"]. Only when the meaning is unambiguous
      AND a real positive product attribute exists; otherwise do NOT invent.
- optional_attributes: nice-to-have only.
- excluded_product_types: things the user explicitly does NOT want. Put the
  NAMED thing (its noun), never a made-up word:
  (a) a genuine feature/accessory to avoid ("без шумоподавления" /
      "without noise cancellation" -> ["шумоподавления"/"noise cancellation"]);
  (b) a missing component/software requested absent ("ноутбук без операционной
      системы" -> ["операционной системы"]);
  (c) EVERY item of an explicit EXCLUSION LIST: when ONE negation marker is
      followed by two or more items ("vacuum without bag, brush, and filter";
      "без A, B и C"), keep ALL listed items as exclusions and convert NONE of
      them into a positive attribute - even if a positive term (e.g. "bagless")
      would be plausible. A list under one shared negation is never a defining
      absence.
- SEPARATE CLAUSES are classified independently: when distinct clauses each
  carry their OWN negation ("наушники без провода и без шумоподавления"),
  classify each on its own - "без провода" is a defining absence -> positive
  "беспроводные"; "без шумоподавления" is a genuine exclusion ->
  ["шумоподавления"]. This is different from a single negation governing a
  comma-list, which stays all-exclusions.
- budget: numbers only. currency: "UAH" for грн/₴, "USD" for $/доллары,
  "EUR" for €/евро, "RUB" for руб. Never keep a currency word as text.
- Never invent information absent from the query. If a negative statement is
  ambiguous and you cannot determine its meaning safely, leave BOTH
  required_attributes and excluded_product_types empty for it - do not guess.
  Empty lists stay []. Unknown values are null.

Return ONLY minified JSON matching:
{"product_type":string|null,"brand":string|null,"model":string|null,
"generation":string|null,"required_attributes":string[],
"optional_attributes":string[],"compatibility":string[],
"budget":{"min":number|null,"max":number|null,"currency":string|null},
"excluded_product_types":string[]}
"""


def _extract_json(text: str) -> dict[str, Any] | None:
    """First balanced JSON object in the text (handles ```json fences)."""

    import json as _json

    if not text:
        return None
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidates = [m.group(1)] if m else []
    start = text.find("{")
    if start >= 0:
        depth = 0
        for i in range(start, len(text)):
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start:i + 1])
                    break
    for cand in candidates:
        try:
            obj = _json.loads(cand)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict):
            return obj
    return None


async def _ai_intent(
    query: str, ai: Any,
) -> tuple[StructuredQueryIntent | None, IntentParseMeta]:
    """One AI attempt with the failure circuit breaker.

    Returns ``(intent, meta)`` on success and ``(None, meta)`` with
    ``meta.source == "fallback"`` on any failure — the caller then uses
    the deterministic fallback. On a hard failure (no answer at all) the
    circuit opens for ``INTENT_CIRCUIT_COOLDOWN_SECONDS``: queries during
    the cooldown skip AI entirely, the first query after it is a single
    controlled probe, and a successful answer closes the circuit again.
    """
    global _CIRCUIT_OPEN_UNTIL, _CIRCUIT_PROBE_IN_FLIGHT

    if ai is None or not callable(getattr(ai, "_generate", None)):
        return None, IntentParseMeta(source="fallback",
                                     error="ai_unavailable")
    if hasattr(ai, "_client") and getattr(ai, "_client", None) is None:
        # The real AIService without a key: no Gemini client at all.
        return None, IntentParseMeta(source="fallback",
                                     error="ai_unavailable")
    if _CIRCUIT_PROBE_IN_FLIGHT or _clock() < _CIRCUIT_OPEN_UNTIL:
        return None, IntentParseMeta(source="fallback",
                                     error="circuit_open")

    # The cooldown has expired (or never started). Exactly one caller may
    # hold the half-open probe slot until it gets its answer, so concurrent
    # messages cannot turn the recovery into a burst of Gemini requests.
    # The slot is claimed before the first ``await``, which makes the claim
    # atomic inside the single-threaded event loop.
    is_probe = _CIRCUIT_OPEN_UNTIL > 0.0
    if is_probe:
        _CIRCUIT_PROBE_IN_FLIGHT = True

    INTENT_AI_STATS["attempted"] += 1
    started = time.perf_counter()
    try:
        text = await ai._generate(f"{INTENT_PROMPT}\n\nQuery: {query}")
        error = None if text else "ai_returned_none"
    except Exception as exc:  # noqa: BLE001 - AI failure must never escape
        text = None
        error = f"ai_exception:{type(exc).__name__}"
    finally:
        if is_probe:
            _CIRCUIT_PROBE_IN_FLIGHT = False
    latency = round((time.perf_counter() - started) * 1000.0, 1)
    model = (getattr(ai, "last_model", None)
             or getattr(ai, "fast_model", None))

    if not text:
        # Quota / transport / timeout / exception: (re)open the circuit for
        # one bounded cooldown. Nothing is retried inside it.
        INTENT_AI_STATS["failed"] += 1
        INTENT_AI_STATS["circuit_opens"] += 1
        INTENT_AI_STATS["last_error"] = error
        _CIRCUIT_OPEN_UNTIL = _clock() + INTENT_CIRCUIT_COOLDOWN_SECONDS
        logger.warning(
            "Gemini intent circuit opened; AI intent paused for %.0fs (%s)",
            INTENT_CIRCUIT_COOLDOWN_SECONDS, error,
        )
        return None, IntentParseMeta(source="fallback", error=error,
                                     model=model, latency_ms=latency)

    # The service answered, so any open cooldown is lifted: a successful
    # recovery probe fully closes the circuit again.
    if _CIRCUIT_OPEN_UNTIL:
        _CIRCUIT_OPEN_UNTIL = 0.0
        logger.info("Gemini intent circuit closed; AI intent restored")

    raw = _extract_json(text)
    if raw is None:
        INTENT_AI_STATS["invalid_json"] += 1
        INTENT_AI_STATS["last_error"] = "invalid_json"
        return None, IntentParseMeta(source="fallback",
                                     error="invalid_json",
                                     model=model, latency_ms=latency)

    if not any(key in raw for key in _INTENT_KEYS):
        # Structurally valid JSON, but not an intent payload at all:
        # reject it instead of trusting an unrelated object.
        INTENT_AI_STATS["invalid_schema"] += 1
        INTENT_AI_STATS["last_error"] = "invalid_schema"
        return None, IntentParseMeta(source="fallback",
                                     error="invalid_schema",
                                     model=model, latency_ms=latency)

    INTENT_AI_STATS["ok"] += 1
    INTENT_AI_STATS["last_error"] = None
    INTENT_AI_STATS["last_model"] = model
    return normalize_intent(raw), IntentParseMeta(
        source="ai", model=model, latency_ms=latency)


async def parse_query_intent(
    raw_query: str, ai_service: Any = None,
) -> tuple[StructuredQueryIntent, IntentParseMeta]:
    """AI-first structured intent parse with deterministic fallback.

    NEVER raises into the caller: AI unavailable, quota, exception,
    malformed JSON, schema violation and internal bugs all end in a
    usable intent + meta with ``source="fallback"``.
    """
    text = (raw_query or "").strip()
    if not text:
        return StructuredQueryIntent(), IntentParseMeta(
            source="fallback", error="empty_query")
    try:
        intent, meta = await _ai_intent(text, ai_service)
        if intent is not None:
            return intent, meta
        INTENT_AI_STATS["fallback_used"] += 1
        return fallback_intent(text), meta
    except Exception:  # pragma: no cover - defensive: never raise
        logger.exception("parse_query_intent failed for %r", text)
        INTENT_AI_STATS["fallback_used"] += 1
        try:
            return fallback_intent(text), IntentParseMeta(
                source="fallback", error="internal_error")
        except Exception:  # pragma: no cover - last resort
            return StructuredQueryIntent(), IntentParseMeta(
                source="fallback", error="internal_error")


# ---------------------------------------------------------------------------
# Application of a parsed intent to a SearchRequest (G25.2-B Stage 2)
# ---------------------------------------------------------------------------

def _retrieval_query(intent: StructuredQueryIntent) -> str:
    """Natural keyword phrase built ONLY from the structured fields.

    Token-level de-duplication keeps «дворники Volkswagen Golf 7» from
    saying "volkswagen" twice while every distinct concept survives:
    product_type first, then brand/model/generation, then compatibility
    targets, then stated specs - the shape a marketplace keyword query
    expects. Budget markers and currency words are never part of it.

    A «для X»/«на X» split (``fallback_intent``) or a spec extraction can
    leave a dangling connector INSIDE the product type («ssd на», «футляр
    на», «зарядка для»): a whole-token preposition/«и»/«с» carries no
    product meaning and a marketplace full-text index treats it as noise,
    so it is dropped wherever it appears. A real spec or connector
    («usb», «e27», «usb-c», «golf 7») is never touched: only these exact
    standalone words go, nothing is invented and no spec is removed.
    """
    parts: list[str] = []
    seen: set[str] = set()

    def add(value: str | None) -> None:
        if not value:
            return
        for token in str(value).split():
            key = token.casefold()
            if key in _LEAKED_CONNECTORS:
                continue
            if key not in seen:
                seen.add(key)
                parts.append(token)

    add(intent.product_type)
    add(intent.brand)
    add(intent.model)
    add(intent.generation)
    for target in intent.compatibility:
        add(target)
    for attribute in intent.required_attributes:
        add(attribute)
    return " ".join(parts)


def _union_terms(existing: str, addition: str) -> str:
    """``existing`` phrase first, then the tokens of ``addition`` it lacks."""

    terms: list[str] = []
    seen: set[str] = set()
    for phrase in (existing, addition):
        for token in (phrase or "").split():
            key = token.casefold()
            if key not in seen:
                seen.add(key)
                terms.append(token)
    return " ".join(terms)


def _merge_intent_list(previous: list[str], current: list[str]) -> list[str]:
    """Previous values first, then the current ones they do not already carry."""

    merged = list(previous)
    seen = {item.casefold() for item in merged}
    for item in current:
        key = item.casefold()
        if key not in seen:
            seen.add(key)
            merged.append(item)
    return merged


#: Every currency spelling that must never reach a marketplace query.
_CURRENCY_TOKENS = frozenset(
    key.casefold().rstrip(".") for key in CURRENCY_ALIASES
) | {"uah", "usd", "eur", "rub", "cny", "gbp"}

#: Function words that may dangle after such tokens were dropped.
_TRAILING_QUERY_WORDS = frozenset(
    {"для", "и", "с", "со", "к", "or", "and", "for", "with", "to", "up"}
)

#: Whole-token connectors that can dangle INSIDE the product type after a
#: «для X»/«на X» split or a spec extraction («ssd на», «футляр на»).
#: A marketplace full-text index treats them as noise, so the constructed
#: retrieval phrase drops them wherever they appear as a WHOLE word. Kept
#: deliberately small: only unambiguous Russian prepositions/conjunctions
#: that the split leaves behind - never a fragment of a real spec («usb»,
#: «e27», «usb-c», «golf 7», «nvme» are never in this set). Superset of
#: :data:`_TRAILING_QUERY_WORDS` plus «на»/«от»/«из»/«про», the ones the
#: «на X» digit-tail branch actually leaves in the head.
_LEAKED_CONNECTORS = _TRAILING_QUERY_WORDS | {
    "на", "от", "из", "про",
}


def _sanitize_query(text: str) -> str:
    """Strip constraint syntax and currency words from a retrieval phrase.

    The production parser's own cleaner (``_strip_constraint_phrases``)
    removes numeric constraints but historically leaves «грн»/«₴» behind
    (a verified G25.1 failure), so whatever phrase is considered, the
    final ``normalized_query`` is scrubbed here: «до 3000»-style
    marker+number sequences and every currency token disappear; dangling
    function words at the end are dropped. Product words, compatibility
    targets and specs («golf 7», «camry 70», «15 pro») are kept.
    """
    if not text:
        return ""
    markers = sorted(
        set(BUDGET_MAX_MARKERS) | set(BUDGET_MIN_MARKERS),
        key=len, reverse=True,
    )
    pattern = r"(?<![0-9a-zа-я])(?:" + "|".join(
        re.escape(m) for m in markers
    ) + r")\s*\d+(?:[\s\u00a0]\d{3})*(?:[.,]\d+)?"
    cleaned = re.sub(pattern, " ", text, flags=re.IGNORECASE)
    kept = [
        token for token in cleaned.split()
        if token.casefold().rstrip(".") not in _CURRENCY_TOKENS
    ]
    # Stage 16 (A-2): dangling connectors are stripped from BOTH edges.
    # The union branch («Для Toyota Camry 70», no product named) kept a
    # leading «для» in the provider phrase; a leading preposition carries
    # no product meaning and the full-text index treats it as noise.
    # The SAME small function-word set as the trailing strip — no
    # universal connector removal, nothing invented.
    while kept and kept[-1].casefold() in _TRAILING_QUERY_WORDS:
        kept.pop()
    while kept and kept[0].casefold() in _TRAILING_QUERY_WORDS:
        kept.pop(0)
    return re.sub(r"\s+", " ", " ".join(kept)).strip()


def apply_intent(
    request: SearchRequest, intent: StructuredQueryIntent, *,
    refined: bool = False, intent_source: str | None = None,
) -> SearchRequest:
    """Return a COPY of ``request`` enriched with ``intent`` (never mutates).

    - ``original_query`` is NEVER touched - it stays the user's own text;
    - budget maps onto the EXISTING ``min_price``/``max_price``/``currency``
      and only an actually parsed value overwrites them (absent budget
      preserves what the request carried; filtering stays client-side in
      comparison_service - no new API price semantics);
    - ``normalized_query`` is always scrubbed of «грн»/«₴»/currency words
      and «до 3000» constraint syntax. Fresh search: the structured
      phrase wins when the message names a product; without a product
      («для Toyota Camry 70») the parser's phrase and the structured
      terms merge. Conversation refinement: the merge's phrase is kept
      as-is (a true refinement never renames the product). An empty
      intent phrase never invents a query;
    - ``intent_source`` (``"ai"``/``"fallback"``, from the intent parse
      meta) tells whether the intent came from the model or the
      deterministic fallback. Whenever it is KNOWN (not ``None``), the
      parser's own ``normalized_query`` is treated as the retrieval core and
      is NOT overwritten by the intent's ``product_type`` (which for a
      descriptive request is a generic/descriptive phrase) nor polluted by
      cross-script compatibility appended into it; the intent's structured
      fields still enrich the ``intent_*`` state, and relevance reads those
      (and the user's own words) directly. Callers that omit ``intent_source``
      keep the legacy behaviour;
    - ``intent_*`` fields: set on a fresh search; a refinement keeps the
      previous values (lists merge over), so the current message can
      never corrupt the previous product identity - nor vice versa.

    Returns a new ``SearchRequest``; the caller's object is never mutated.
    """
    update: dict[str, Any] = {}

    # Budget -> existing price fields (only real parsed values overwrite).
    if intent.budget.min is not None:
        update["min_price"] = float(intent.budget.min)
    if intent.budget.max is not None:
        update["max_price"] = float(intent.budget.max)
    if intent.budget.currency is not None:
        update["currency"] = intent.budget.currency

    # Clean retrieval phrase (normalized_query only; never original_query).
    intent_query = _retrieval_query(intent)
    existing = request.normalized_query or ""
    if refined:
        # A true refinement never renames the product (resolve_request
        # rejects any message that introduces a new product word), so the
        # conversation merge owns the phrase; the structured echo of
        # modifier/price wording («а дешевле?») must neither replace nor
        # pollute it.
        base = existing
    elif intent_query and existing:
        if intent_source is not None:
            # Stage 57 (fallback) + Stage 59 (ai): whenever the intent parse
            # source is KNOWN, the parser already produced a useful normalized
            # product query and it stays the retrieval core. A FALLBACK
            # product_type is the whole descriptive sentence («прибор который
            # измеряет давление в шинах»); an AI product_type can be generic
            # («устройство») or descriptive too, and compatibility may be in
            # another script («шкаф» against an English phrase). Overwriting
            # the parser's phrase with either, or unioning cross-script
            # compatibility into it, turns a good query («tire pressure gauge»,
            # «pet proof cabinet lock») into a search for verbs or a
            # mixed-script noise phrase. Keep the parser's phrase; the intent's
            # brand/model/generation/compatibility/spec/exclusion still enrich
            # the ``intent_*`` state below, and relevance reads those fields
            # (and the user's own words) directly - never this phrase - so no
            # constraint is lost.
            base = existing
        else:
            # Legacy direct callers that omit ``intent_source`` keep the old
            # behaviour unchanged: the structured phrase wins when a product is
            # named; without one («для Toyota Camry 70») the parser's phrase
            # stays and the structured terms are appended.
            base = (intent_query if intent.product_type is not None
                    else _union_terms(existing, intent_query))
    elif intent_query:
        base = intent_query
    else:
        base = existing
    # Always scrub «грн»/«₴»/constraint syntax: the legacy parser leaves
    # currency words behind (the verified G25.1 D-class failure), and a
    # budget-only message must not keep them in the retrieval phrase.
    new_query = _sanitize_query(base)
    if new_query != existing:
        update["normalized_query"] = new_query

    # Structured fields: set on a fresh search; a refinement keeps the
    # previous identity (lists still merge - «без микрофона» contributes
    # its exclusion without renaming the product).
    if not refined:
        if intent.product_type is not None:
            update["intent_product_type"] = intent.product_type
        if intent.brand is not None:
            update["intent_brand"] = intent.brand
        if intent.model is not None:
            update["intent_model"] = intent.model
        if intent.generation is not None:
            update["intent_generation"] = intent.generation
    update["intent_required_attributes"] = _merge_intent_list(
        request.intent_required_attributes, intent.required_attributes)
    update["intent_compatibility"] = _merge_intent_list(
        request.intent_compatibility, intent.compatibility)
    update["intent_excluded_product_types"] = _merge_intent_list(
        request.intent_excluded_product_types, intent.excluded_product_types)

    return request.model_copy(update=update)


__all__ = [
    "CURRENCY_ALIASES",
    "INTENT_AI_STATS",
    "INTENT_CIRCUIT_COOLDOWN_SECONDS",
    "INTENT_PROMPT",
    "IntentBudget",
    "IntentParseMeta",
    "StructuredQueryIntent",
    "apply_intent",
    "empty_intent",
    "fallback_intent",
    "is_circuit_open",
    "normalize_currency",
    "normalize_intent",
    "parse_query_intent",
    "reset_ai_state",
]
