"""Conversation state for the conversational search engine.

Adapted from the reference ``src/state.py`` dialog-state design (utterances
with provenance, active/superseded slot ledger, override down-weighting,
productive-turn tracking), specialised for the bot's real question types:
an AliExpress product request with product type, features, exclusions,
budget+currency and optional preferences.

One authoritative state per user lives in ``context.user_data`` (the bot's
existing per-user, expiring in-memory store) — no second persistence system
and no second state source: ``ConversationState`` (services.conversation)
keeps owning the effective :class:`SearchRequest`; this ledger owns WHY.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from services.convsearch.text import constraint_spans, tokens

#: How long an idle conversation stays alive for refinement (the bot's
#: existing conversation TTL — mirrored here so the engine's ledger cannot
#: outlive the request it explains).
CONTEXT_TTL_SECONDS = 30 * 60


@dataclass
class Turn:
    """One user message plus the provenance the retrieval stages need."""

    index: int
    text: str
    #: Declines/stalls carry no product signal and are held out of every
    #: retrieval view (the reference's ``Utterance.declined`` fix).
    excluded: bool = False
    at: float = field(default_factory=time.monotonic)


@dataclass
class Slot:
    """One constraint with provenance and lifecycle status.

    Mirrors the reference ``SlotValue`` ledger: ``status`` distinguishes the
    authoritative active view from the superseded archive, so a correction
    down-weights rather than erases and stays auditable.
    """

    kind: str            # "type" | "feature" | "exclusion" | "budget" | "preference"
    value: str
    source_turn: int
    raw_text: str
    status: str = "active"          # active | superseded | rejected
    superseded_turn: int | None = None

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "value": self.value,
            "source_turn": self.source_turn,
            "status": self.status,
            "superseded_turn": self.superseded_turn,
        }


#: Message kinds the engine distinguishes (5.1 of the integration brief).
class MessageIntent:
    NEW_SEARCH = "new_search"
    REFINE = "refine"
    CORRECTION = "correction"          # replaces a previous requirement
    ALTERNATIVES = "alternatives"      # «покажи другие варианты»
    COMPARE = "compare"                # compare/explain the shown set
    UNRELATED = "unrelated"
    AMBIGUOUS = "ambiguous"


#: Explicit negation / reversal markers (RU + EN). A «не» followed by a
#: feature is an EXCLUSION of that feature, never a new product (the bot's
#: existing rule, kept); a reversal marker supersedes the whole previous
#: constraint set the way the reference's OVERRIDE_CUES does.
_NEGATION_RE = re.compile(r"(?:^|\s)(?:не|без|without|not)\s+", re.IGNORECASE)

#: «с X» / «со X» / «without X» adds a required characteristic even when it
#: is a SINGLE word («а есть с чехлом?» — the question mark ends the clause
#: before a two-word span could form). Matched clause-bounded like the
#: engine's own constraint spans.
_FEATURE_INTRO_RE = re.compile(
    r"(?:^|\s)(?:с|со|with)\s+([\w\-]+(?:\s+[\w\-]+)?)", re.IGNORECASE
)
_REVERSAL_RE = re.compile(
    r"(?:^|\s)(?:на самом деле|вообще|забудь|передумал|передумала|"
    r"не надо|не нужно|смени|замени|наоборот|actually|instead|"
    r"forget|changed my mind|no longer|never mind)\b",
    re.IGNORECASE,
)

#: Closed-class QUESTION words (RU + EN). Whether a message is a QUESTION
#: is a grammatical property, not a phrase list: a message that asks a
#: question AND names no new product is about the ongoing conversation
#: (the shown candidates), never a new search.
_QUESTION_WORDS: frozenset[str] = frozenset(
    {
        "какой", "какая", "какие", "какого", "какую", "каким",
        "что", "чем", "почему", "зачем", "сколько", "где", "когда",
        "который", "которая", "которые", "есть", "стоит", "лучше",
        "what", "which", "why", "how", "where", "who", "is", "are",
        "does", "do", "can", "better", "difference",
    }
)

#: Closed-class ALTERNATION markers (RU + EN): function words whose meaning
#: is "different/more instead of these". Generic vocabulary, category-
#: general — no product or query words.
_ALTERNATION_MARKERS: tuple[str, ...] = (
    "еще", "ещё", "друг", "иных", "иначе", "other", "more", "another",
    "different", "else",
)

#: Budget / currency framing words: they scope a price FILTER of the
#: current product and never name one («желательно до 20 долларов»,
#: «бюджет 2000 грн», «до 500 гривен»). Kept next to the currency map so
#: the qualifier guard and the slot extractor can never drift apart.
_BUDGET_FRAMING_WORDS: frozenset[str] = frozenset(
    {
        "желательно", "желательн", "примерно", "максимум", "макс",
        "предел", "бюджет", "до", "от", "меньше", "больше", "не", "более",
        "долларов", "доллар", "доллара", "usd", "uah", "eur", "евро",
        "грн", "гривн", "гривна", "гривны", "гривен", "рублей", "руб",
        "цена", "цену", "цены", "курс", "пересчет", "пересчёт",
        "доларів", "долара", "долар", "гривень", "₴", "$", "€", "₽",
        # Scale words belong to a money amount, never to a product type or feature.
        "тыс", "тысяч", "тысяча", "тысячи", "тысячу", "тысячам",
        "тысячами", "тысячах", "тысяче", "тысячей", "thousand", "thousands",
    }
)

#: Generic feature nouns: a span made ONLY of these («быстрая зарядка»,
#: «большая ёмкость») is a CHARACTERISTIC of the current product, never a
#: product switch. Product nouns are deliberately NOT here.
_COUNT_WORDS: frozenset[str] = frozenset(
    {
        "вариант", "варианта", "вариантов", "варианты", "штук", "штуки",
        "штука", "шт", "опция", "опции", "товар", "товара", "товары",
        "option", "options", "variant", "variants", "items", "item",
    }
)

#: Generic feature nouns that QUALIFY the current product («быстрая
#: зарядка», «большая ёмкость»): a span made only of these never names a
#: product switch. Real product nouns are deliberately NOT here.
_CHARACTERISTIC_NOUNS = _COUNT_WORDS | frozenset(
    {
        "зарядка", "заряд", "емкость", "ёмкость", "мощность", "вес", "весу",
        "экран", "камера", "микрофон", "батаре", "шум", "качество",
        "водонепрониц", "waterproof", "battery", "capacity", "charging",
    }
)
#: Compare / explain wording kept as ONE of the COMPARE signals (the
#: closed-class question-word check in ``observe`` is the general path).
_COMPARE_RE = re.compile(
    r"(?:как[ао][яй]? (?:лучше|выбрать|отлич)|вариант выбрать|какой выбрать|"
    r"чем отлич|сравни|разниц|чем они|котор[а-я]* (?:лучше|выбрать)|"
    r"расскажи|объясни|подробнее|почему|что насчет|что насчёт|"
    r"о втором|о первом|про второй|про первый|про третий|"
    r"который из них|какой из них|какая из них|какие из них|"
    r"which is better|compare|tell me|explain|more about)",
    re.IGNORECASE,
)

#: Budget wording: «до 500 гривен», «under $30», «бюджет 2000».
_BUDGET_RE = re.compile(
    r"(?:(?:до|under|up to|не более|меньше|less than|max|максимум)\s*)?"
    r"(\d+(?:[.,]\d+)?)\s*"
    r"(тыс(?:яч[а-я]*)?|тысяч(?:а|и|у|ам|ами|ах|е|ей)?|thousands?)?\s*"
    r"(грн|грив[а-я]*|₴|uah|\$|usd|доллар[а-я]*|долар[а-я]*|"
    r"евро|eur|€|руб[а-я]*|₽|rub| грн)",
    re.IGNORECASE,
)
_CURRENCY_BY_WORD = {
    "грн": "UAH", "грив": "UAH", "₴": "UAH", "uah": "UAH",
    "$": "USD", "usd": "USD", "доллар": "USD", "долар": "USD",
    "евро": "EUR", "eur": "EUR", "€": "EUR",
    "руб": "RUB", "₽": "RUB", "rub": "RUB",
}


@dataclass
class ConversationEngineState:
    """The engine's conversation ledger for ONE user."""

    user_key: str
    turns: list[Turn] = field(default_factory=list)
    slots: list[Slot] = field(default_factory=list)
    shown_product_ids: list[str] = field(default_factory=list)
    #: The last meaningful search outcome, honestly recorded.
    last_outcome: str | None = None   # "ok" | "no_candidates" | "rejected" | "provider_failure"
    updated_at: float = field(default_factory=time.monotonic)

    # -- lifecycle ---------------------------------------------------------

    def is_expired(self, *, now: float | None = None) -> bool:
        return (now or time.monotonic()) - self.updated_at > CONTEXT_TTL_SECONDS

    def touch(self) -> None:
        self.updated_at = time.monotonic()

    # -- observation -------------------------------------------------------

    def _is_about_current(self, message: str) -> bool:
        """Whether the message is a QUESTION about the ONGOING conversation.

        General grammar, not a phrase list: the message carries a closed-
        class question word («какой», «чем», «почему», "which", "why"…)
        AND introduces no unknown content span of its own — its content
        words are either already-known context or generic qualifiers. A
        message that names a genuinely new product inside the question
        («какой ноутбук лучше?») is NOT answered from the shown set; it is
        a new product request and falls through to search.
        """

        lowered = (message or "").lower().replace("ё", "е")
        words = re.findall(r"[^\W\d_]+", lowered)
        if not any(word in _QUESTION_WORDS for word in words):
            return False
        content = [
            token for token in tokens(message)
            if token not in _CHARACTERISTIC_NOUNS
            and token not in _QUALIFIER_WORDS
            and token not in _BUDGET_FRAMING_WORDS
            and token not in _QUESTION_WORDS
            and not is_colour_word(token)
            and not token.isdigit()
        ]
        if not content:
            return True
        known_context = {
            token
            for slot in self.active()
            for token in tokens(slot.value)
        }
        return not any(token not in known_context for token in content)

    def observe(
        self, text: str, *, intent_override: str | None = None
    ) -> dict:
        """Record one user message and return its classified intent.

        A validated Gemini action may override the deterministic intent
        classification. Slot extraction and lifecycle transitions remain
        local and deterministic; invalid/absent overrides use the fallback.
        """

        message = (text or "").strip()
        index = len(self.turns) + 1
        reversal = bool(message and _REVERSAL_RE.search(message))
        has_product_words = any(
            len(word) >= 3 for word in tokens(message)
        )
        negations = _negation_values(message)
        message_spans = constraint_spans(message)
        # A «с X» characteristic that a clause boundary truncated to one
        # word («а есть с чехлом?") is still a feature — capture it even
        # when constraint_spans dropped it for length.
        for match in _FEATURE_INTRO_RE.finditer(message):
            value = " ".join(tokens(match.group(1)))
            if value and not any(
                value in span or span.endswith(value)
                for span in message_spans
            ):
                message_spans.append(value)
        # «Подешевле.» / «А есть полегче?» carry no multi-word span but DO
        # refine the current search: a short comparative/qualifier follow-up
        # is a REFINE, never an ambiguous message and never a new product.
        # (Kept as message_spans evidence for the classifier below.)

        if self._is_about_current(message) or _COMPARE_RE.search(message):
            intent = MessageIntent.COMPARE
        elif index > 1 and _is_alternatives_request(message, self):
            intent = MessageIntent.ALTERNATIVES
        elif reversal:
            intent = MessageIntent.CORRECTION
        elif index == 1:
            intent = MessageIntent.NEW_SEARCH
        else:
            known = {slot.value for slot in self.active() if slot.kind == "feature"}
            new_spans = [span for span in message_spans if span not in known]
            type_slot = self.active_by_kind("type")
            type_tokens = set(tokens(type_slot.value)) if type_slot else set()
            span_tokens = set(tokens(" ".join(new_spans)))

            # THE CORE PRINCIPLE (replaces per-phrase keyword lists): once an
            # active search exists, a message is a REFINEMENT unless it names
            # a genuinely DIFFERENT product. "Names a different product" is
            # decided by the span's own evidence:
            #   * it shares no token with the active product type AND
            #   * its span is not purely framing (qualifiers, colours, count
            #     words, budget words, generic feature nouns), AND
            #   * it is not introduced by «с/без/для/не» (a characteristic of
            #     the current product), AND
            #   * it is not a negation.
            # Otherwise every further message accumulates into the current
            # search — exactly what a multi-turn consultant must do.
            framing = span_tokens and all(
                token in _QUALIFIER_WORDS
                or token in _BUDGET_FRAMING_WORDS
                or token in _COUNT_WORDS
                or token in _CHARACTERISTIC_NOUNS
                or token.endswith(_COMPARATIVE_SUFFIXES)
                or is_colour_word(token)
                or token.isdigit()
                for token in span_tokens
            )
            # A compatibility/fitment constraint introduced by «под/для/for»
            # («для hyundai tucson», «под iphone 15») qualifies the CURRENT
            # product — the connector makes the following noun a TARGET of
            # the product, never a product switch. Category-general: no
            # brand or model list, the connector grammar itself decides.
            characteristic = bool(re.search(
                r"(?:^|\s)(?:с|со|без|для|под|не|for)\s+", message, re.IGNORECASE
            ))
            names_other_product = bool(
                new_spans and type_tokens and not (span_tokens & type_tokens)
                and not framing
                and not characteristic
                and not negations
            )
            if names_other_product:
                intent = MessageIntent.NEW_SEARCH
            elif negations or new_spans or _budget(message):
                # A budget/currency statement («до 20 долларов») or a
                # constraint («для hyundai tucson») scopes a FILTER of the
                # current product — never a new search.
                intent = MessageIntent.REFINE
            elif not has_product_words or _is_comparative_followup(message) or _is_short_refinement(message):
                # «Подешевле.» / «А есть полегче?»: a bare comparative or a
                # connector-led follow-up QUALIFIES the current product.
                intent = MessageIntent.REFINE
            else:
                intent = MessageIntent.AMBIGUOUS

        allowed_overrides = {
            MessageIntent.NEW_SEARCH, MessageIntent.REFINE,
            MessageIntent.CORRECTION, MessageIntent.ALTERNATIVES,
            MessageIntent.COMPARE, MessageIntent.AMBIGUOUS,
        }
        if intent_override in allowed_overrides:
            intent = intent_override

        # A short follow-up must NOT reset the product context: only a NEW
        # SEARCH (a genuine new product request, reversal excluded) and an
        # explicit reset replace the ledger's active view.
        if intent is MessageIntent.NEW_SEARCH and not reversal:
            self._supersede_all(index)
        elif intent is MessageIntent.CORRECTION:
            # A correction supersedes only the slot kinds the new message
            # actually corrects. A budget correction must not erase the
            # product type; a feature correction must not erase the budget.
            corrected_kinds: set[str] = set()
            if negations:
                corrected_kinds.update({"feature", "exclusion"})
            if _budget(message):
                corrected_kinds.add("budget")
            has_feature_span = any(
                (span_tokens := tokens(span))
                and not all(
                    token in _BUDGET_FRAMING_WORDS
                    or token in _COUNT_WORDS
                    or token in _QUALIFIER_WORDS
                    or token.isdigit()
                    or is_colour_word(token)
                    for token in span_tokens
                )
                for span in message_spans
            )
            if has_feature_span:
                corrected_kinds.add("feature")
            if not corrected_kinds:
                corrected_kinds.update({"feature", "exclusion"})
            self._supersede_kinds(index, corrected_kinds)

        self.turns.append(Turn(index=index, text=message))
        self.touch()
        return {
            "intent": intent,
            "negations": negations,
            "spans": message_spans,
            "budget": _budget(message),
            "reversal": reversal,
        }

    def record_slots(self, result: dict) -> None:
        """Turn the observation result into authoritative slots."""

        index = len(self.turns)
        negations = result.get("negations") or []
        for value in negations:
            self._add_slot(Slot(kind="exclusion", value=value, source_turn=index, raw_text=""))
        # Feature spans are CONSTRAINTS of an active search. A NEW_SEARCH
        # message names a product, not a feature: recording its span
        # ("5 вариантов шуруповерта") as a required feature would demand
        # that literal phrase in every title and reject everything.
        # AMBIGUOUS messages carry no verified constraint either: recording
        # one would make a vague remark into a literal title requirement.
        if result.get("intent") not in (
            MessageIntent.NEW_SEARCH, MessageIntent.AMBIGUOUS,
            MessageIntent.COMPARE, MessageIntent.ALTERNATIVES,
        ):
            for span in result.get("spans") or []:
                # A «не X» span is an EXCLUSION (the negation slot already
                # carries it) — never a positive feature.
                if span.startswith("не ") or span.startswith("not "):
                    continue
                # A span made ONLY of qualifiers, colours, count words or
                # budget framing («красном цвете», «желательно до 20
                # долларов», «5 вариантов») scopes the current search — it
                # is NOT a literal title requirement, which would reject
                # everything. Real feature nouns («с чехлом в комплекте»,
                # «быстрая зарядка») stay requirements.
                span_tokens = tokens(span)
                if span_tokens and all(
                    token in _QUALIFIER_WORDS
                    or is_colour_word(token)
                    or token in _BUDGET_FRAMING_WORDS
                    or token in _COUNT_WORDS
                    or token.isdigit()
                    for token in span_tokens
                ):
                    continue
                self._add_slot(Slot(kind="feature", value=span, source_turn=index, raw_text=""))
        budget = result.get("budget") or {}
        if budget.get("max") is not None:
            self._add_slot(Slot(
                kind="budget",
                value=f"{budget['max']} {budget.get('currency') or ''}".strip(),
                source_turn=index, raw_text="",
            ))

    def record_product_type(self, value: str) -> None:
        """Record the active product type (from the request's own intent)."""

        if not value:
            return
        index = len(self.turns)
        existing = self.active_by_kind("type")
        if existing and existing.value == value:
            return
        # A new product type replaces the previous one — the ledger's own
        # product-type slot is exactly one thing.
        for slot in self.slots:
            if slot.kind == "type" and slot.status == "active":
                slot.status = "superseded"
                slot.superseded_turn = index
        self.slots.append(Slot(kind="type", value=value, source_turn=index, raw_text=""))

    def record_shown(self, product_ids: list[str]) -> None:
        """Remember stable product references already displayed."""

        for pid in product_ids or []:
            if pid and pid not in self.shown_product_ids:
                self.shown_product_ids.append(pid)
        del self.shown_product_ids[:-50]

    # -- views for the pipeline --------------------------------------------

    def active(self) -> list[Slot]:
        return [slot for slot in self.slots if slot.status == "active"]

    def active_by_kind(self, kind: str) -> Slot | None:
        found = [slot for slot in self.active() if slot.kind == kind]
        return found[-1] if found else None

    def active_values(self, kind: str) -> list[str]:
        return [slot.value for slot in self.active() if slot.kind == kind]

    def authoritative_text(self) -> str:
        """Compact query built from the active ledger (reference analogue)."""

        order = {"type": 0, "feature": 1, "budget": 2, "preference": 3}
        slots = sorted(
            self.active(),
            key=lambda slot: (order.get(slot.kind, 9), slot.source_turn),
        )
        return " ".join(slot.value for slot in slots)

    def requirement_terms(self) -> list[str]:
        """Tokens of every active requirement — the ranking evidence set."""

        seen: set[str] = set()
        out: list[str] = []
        for slot in self.active():
            if slot.kind in ("exclusion", "budget"):
                continue
            for token in tokens(slot.value):
                if token not in seen:
                    seen.add(token)
                    out.append(token)
        return out

    def snapshot(self) -> dict:
        """Serializable state for diagnostics (never shown to the user)."""

        return {
            "user_key": self.user_key,
            "turns": len(self.turns),
            "active_slots": [slot.as_dict() for slot in self.active()],
            "superseded": sum(1 for slot in self.slots if slot.status != "active"),
            "shown_recent": self.shown_product_ids[-10:],
            "last_outcome": self.last_outcome,
            "structured_query": self.authoritative_text(),
        }

    # -- internals -----------------------------------------------------------

    def _add_slot(self, slot: Slot) -> None:
        for existing in self.active():
            if existing.kind == slot.kind and existing.value == slot.value:
                return
        self.slots.append(slot)

    def _supersede_all(self, turn: int) -> None:
        for slot in self.slots:
            if slot.status == "active":
                slot.status = "superseded"
                slot.superseded_turn = turn

    def _supersede_kinds(self, turn: int, kinds: set[str] | None) -> None:
        for slot in self.slots:
            if slot.status == "active" and (kinds is None or slot.kind in kinds):
                slot.status = "superseded"
                slot.superseded_turn = turn


def is_colour_word(token: str) -> bool:
    """A colour adjective/noun qualifies the CURRENT product in any category.

    Single source of truth: ``services.conversation._COLOR_STEMS`` (with the
    project's own adjective-ending guard), so the engine and the legacy
    conversation helpers can never disagree about what is a colour.
    """

    from services.conversation import _COLOR_STEMS, _ADJECTIVE_ENDINGS

    lowered = token.lower().replace("ё", "е")
    if lowered in {"цвет", "цвете", "цвета", "цвету", "цветом"}:
        return True
    for stem in _COLOR_STEMS:
        base = stem.replace("ё", "е")
        if lowered.startswith(base):
            remainder = lowered[len(base):]
            if any(remainder.endswith(ending) for ending in _ADJECTIVE_ENDINGS):
                return True
    return False


def _negation_values(message: str) -> list[str]:
    """The noun phrases an explicit negation excludes («не ударный» → «ударный»).

    The bot's rule stands: «не» counts only as a standalone word before a
    feature; «недорогой» is one word and never an exclusion. The excluded
    value is the words AFTER the negation marker up to a clause boundary.
    """

    out: list[str] = []
    for match in re.finditer(
        r"(?:^|\s)(?:не|без|without|not)\s+([^\s,.!?;:]+(?:\s+[^\s,.!?;:]+){0,2})",
        message or "",
        re.IGNORECASE,
    ):
        value = " ".join(
            token.lower().replace("ё", "е") for token in tokens(match.group(1))
        ).strip()
        if value:
            out.append(value)
    return out


def _budget(message: str) -> dict:
    """Numeric budget + currency from one message; {} when none."""

    match = _BUDGET_RE.search(message or "")
    if not match:
        return {}
    raw = match.group(1).replace(",", ".").replace(" ", "")
    try:
        amount = float(raw)
    except ValueError:
        return {}
    scale = (match.group(2) or "").lower()
    if scale:
        # "2 тысячи гривен" and "2 тыс. грн" mean 2,000 UAH.
        amount *= 1000
    tail = (match.group(0)[match.start(1):] or "").lower()
    currency = None
    for word, code in _CURRENCY_BY_WORD.items():
        if word in tail:
            currency = code
            break
    return {"max": amount, "currency": currency}


def _is_short_refinement(message: str) -> bool:
    """Whether a short follow-up only QUALIFIES the current product."""

    lowered = (message or "").lower().replace("ё", "е")
    for connector in (
        "но", "только", "а", "и", "еще", "ещё", "подешевле", "полегче",
        "получше", "не",
    ):
        if lowered.startswith(connector + " ") or lowered == connector:
            return True
    return False


#: Comparative/qualifier tails that refine the current search without any
#: multi-word span («Подешевле.», «А есть полегче?», «С большой ёмкостью.»).
_QUALIFIER_WORDS: frozenset[str] = frozenset(
    {
        "подешевле", "подороже", "полегче", "потяжелее", "получше", "похуже",
        "поменьше", "побольше", "покороче", "подлиннее", "компактнее",
        "мощнее", "дешевле", "дороже", "легче", "тяжелее", "быстрее",
    }
)

#: The RU comparative/colloquial-suffix grammar: ANY word ending in one of
#: these modifies the CURRENT product («подешевле», «помощнее», «полегче»)
#: — a productive rule instead of a fixed word list.
_COMPARATIVE_SUFFIXES: tuple[str, ...] = (
    "ше", "ее", "ей", "же", "ше", "ее", "ей",
)


def _is_alternatives_request(message: str, state: "ConversationEngineState") -> bool:
    """Whether a later message asks for DIFFERENT results in the SAME task.

    Structural, not phrase-based: the message must (a) carry an alternation
    marker («еще», «другой», «other», «more»…) AND (b) name NO new product
    of its own — its content words are either scaffolding, qualifiers,
    budget/count words, or already-known context. An alternation marker
    attached to a NEW product name («покажи другие наушники») is a new
    search, not an alternatives turn.
    """

    lowered = (message or "").lower().replace("ё", "е")
    if not any(marker in lowered for marker in _ALTERNATION_MARKERS):
        return False
    content = [
        token for token in tokens(message)
        if token not in _CHARACTERISTIC_NOUNS
        and token not in _QUALIFIER_WORDS
        and token not in _BUDGET_FRAMING_WORDS
        and not is_colour_word(token)
        and not token.isdigit()
    ]
    if not content:
        return True
    known_context = {
        token
        for slot in state.active()
        for token in tokens(slot.value)
    }
    return not any(token not in known_context for token in content)


def _is_comparative_followup(message: str) -> bool:
    """Whether the message is a bare comparative qualifier of the current search."""

    words = [
        word.lower().replace("ё", "е")
        for word in re.findall(r"[^\W\d_]+", message or "")
    ]
    return bool(words) and all(
        word in _QUALIFIER_WORDS or word in {"а", "и", "но", "есть", "ли", "с", "со"}
        or word.endswith(_COMPARATIVE_SUFFIXES)
        or word in {"большой", "большая", "большое", "ёмкость", "емкость"}
        for word in words
    )
