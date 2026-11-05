"""Conversation state for the conversational search engine.

Adapted from the reference ``src/state.py`` dialog-state design (utterances
with provenance, active/superseded slot ledger, override down-weighting,
productive-turn tracking), specialised for the bot's real question types:
an AliExpress product request with product type, soft use-case context,
required features, exclusions, budget+currency and optional preferences.

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

    kind: str            # "type" | "use_case" | "compatibility" | "feature" | "exclusion" | "budget" | "preference"
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

#: Connector-led characteristics include equipment, fitment and material
#: ("с чехлом", "под iPhone", "из нержавеющей стали", "made of steel").
#: Capture short fragments even when the general span parser drops them.
_FEATURE_INTRO_RE = re.compile(
    r"(?:^|\s)(?:(?:с|со|из|под|with)\s+|"
    r"(?:(?:сделан|изготовлен|выполнен)\s+из\s+)|"
    r"(?:made\s+(?:of|from)\s+))([\w\-]+(?:\s+[\w\-]+)?)",
    re.IGNORECASE,
)
_USE_CASE_INTRO_RE = re.compile(
    # «для X» and «по X» express the task/material the requested item is
    # intended to work with («насадка по дереву», «диск по металлу»).
    # Keep this as soft application context: marketplace titles need not
    # repeat the exact wording for a candidate to be suitable.
    r"(?:^|\s)(?:для|по|for)\s+(.+?)(?=\s+(?:с|со|из|без|под|по|with|without|made\s+(?:of|from))\s+|[,!?;:]|$)",
    re.IGNORECASE,
)
_EXPLICIT_REQUEST_RE = re.compile(
    r"\b(?:нужен|нужна|нужно|нужны|ищу|хочу|требуется|требуются|"
    r"подбери|подберите|найди|найдите|закажи|закажите|купи|купите)\s+(.+)",
    re.IGNORECASE,
)
_REQUEST_TARGET_RE = re.compile(
    r"\b(?:на|под)\s+(.+?)(?=\s+(?:на|для|с|со|из|без|под|до|от|в|по|к|у|о|об)\b|[,!?;:]|$)",
    re.IGNORECASE,
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
        "защита", "защиты", "защиту", "защитой",
        "водонепрониц", "waterproof", "battery", "capacity", "charging",
    }
)
#: Compare / explain wording kept as ONE of the COMPARE signals (the
#: closed-class question-word check in ``observe`` is the general path).
_COMPARE_RE = re.compile(
    r"(?:как[ао][яй]? (?:лучше|выбрать|отлич)|вариант выбрать|какой выбрать|"
    r"чем отлич|сравни|разниц|чем они|котор[а-я]* (?:лучше|выбрать)|"
    r"о втором|о первом|про второй|про первый|про третий|"
    r"который из них|какой из них|какая из них|какие из них|"
    r"which is better|compare)",
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
    # Role-labelled dialogue memory is separate from retrieval turns so
    # assistant messages never change slot provenance or turn numbering.
    dialogue_history: list[dict[str, str]] = field(default_factory=list)
    slots: list[Slot] = field(default_factory=list)
    shown_product_ids: list[str] = field(default_factory=list)
    #: The last meaningful search outcome, honestly recorded.
    last_outcome: str | None = None   # "ok" | "no_candidates" | "rejected" | "provider_failure" | "clarify"
    last_question: str | None = None
    updated_at: float = field(default_factory=time.monotonic)

    # -- lifecycle ---------------------------------------------------------

    def is_expired(self, *, now: float | None = None) -> bool:
        return (now or time.monotonic()) - self.updated_at > CONTEXT_TTL_SECONDS

    def touch(self) -> None:
        self.updated_at = time.monotonic()

    def record_turn(self, text: str) -> int:
        """Record a conversational utterance without changing search slots."""
        message = (text or "").strip()
        if not message:
            return len(self.turns)
        index = len(self.turns) + 1
        self.turns.append(Turn(index=index, text=message))
        self.dialogue_history.append({"role": "user", "text": message[:1000]})
        del self.dialogue_history[:-12]
        self.touch()
        return index

    def record_assistant_turn(self, text: str) -> None:
        """Remember the assistant's actual delivered words for follow-up understanding."""
        message = (text or "").strip()
        if not message:
            return
        self.dialogue_history.append({"role": "assistant", "text": message[:1000]})
        del self.dialogue_history[:-12]
        self.touch()

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
        # Deictic references («из этого») and a use-case target («для дома»)
        # do not name a new product. They can accompany a question asking us
        # to choose among the already shown candidates.
        use_case_tokens = {
            token
            for match in _USE_CASE_INTRO_RE.finditer(message)
            for token in tokens(match.group(1))
        }
        content = [
            token for token in tokens(message)
            if token not in _CHARACTERISTIC_NOUNS
            and token not in _QUALIFIER_WORDS
            and token not in _BUDGET_FRAMING_WORDS
            and token not in _QUESTION_WORDS
            and token not in use_case_tokens
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
        self,
        text: str,
        *,
        intent_override: str | None = None,
        slot_updates: list[dict] | None = None,
        allow_product_switch: bool = False,
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
        introduced_features: list[str] = []
        for match in _FEATURE_INTRO_RE.finditer(message):
            value = " ".join(tokens(match.group(1)))
            if value:
                introduced_features.append(value)
        if introduced_features:
            # The general span parser can retain the entire carrier sentence
            # ("оставь варианты из нержавеющей стали"). Replace that broad
            # span with the actual connector value so framing words never
            # become mandatory product features.
            cleaned_spans: list[str] = []
            for span in message_spans:
                residual = span
                for value in introduced_features:
                    residual = residual.replace(value, " ")
                residual_tokens = tokens(residual)
                if len(residual_tokens) >= 2:
                    cleaned_spans.append(" ".join(residual_tokens))
            message_spans = list(dict.fromkeys(
                [*cleaned_spans, *introduced_features]
            ))

        # Use-case / application context is a SOFT slot, not a mandatory
        # title feature: a household listing may be suitable even when the
        # seller never writes «для дома» in its title. Keep it separate from
        # explicit equipment/features such as «с двумя аккумуляторами».
        use_case_spans: list[str] = []
        compatibility_spans: list[str] = []
        # «X на Y» can mean a fitment relation («насадка на дрель»,
        # «чехол на телефон») or an ordinary purpose («подарок на свадьбу»).
        # Treat it as hard compatibility only when X is a generic accessory
        # product role. Unrelated activity context such as «буду работать на
        # стройке» is never parsed as a product relation.
        request_match = _EXPLICIT_REQUEST_RE.search(message)
        if request_match:
            request_phrase = request_match.group(1)
            target_matches = list(_REQUEST_TARGET_RE.finditer(request_phrase))
            if target_matches:
                from services.convsearch.type_validation import (
                    is_accessory_product_type,
                )
                requested_item = request_phrase[:target_matches[0].start()]
                is_compatibility_relation = is_accessory_product_type(requested_item)
                for target_index, match in enumerate(target_matches):
                    value = " ".join(tokens(match.group(1)))
                    if value:
                        # Only the FIRST target phrase names the accessory's
                        # host device. Later «для/на X» phrases describe its
                        # application/material («насадка на дрель для дерева»).
                        target = (
                            compatibility_spans
                            if is_compatibility_relation and target_index == 0
                            else use_case_spans
                        )
                        target.append(value)
        for match in _USE_CASE_INTRO_RE.finditer(message):
            value = " ".join(tokens(match.group(1)))
            if value:
                use_case_spans.append(value)
        compatibility_spans = list(dict.fromkeys(compatibility_spans))
        compatibility_token_sets = [
            set(tokens(value)) for value in compatibility_spans
        ]
        use_case_spans = [
            value for value in dict.fromkeys(use_case_spans)
            if not any(
                set(tokens(value)) == target_tokens
                for target_tokens in compatibility_token_sets
            )
        ]
        if use_case_spans or compatibility_spans:
            context_tokens = {
                token
                for value in [*use_case_spans, *compatibility_spans]
                for token in tokens(value)
            }
            residual_spans: list[str] = []
            for span in message_spans:
                residual = [
                    token for token in tokens(span)
                    if token not in context_tokens
                    and token not in {"с", "со", "для", "по", "for"}
                ]
                if len(residual) >= 2:
                    residual_spans.append(" ".join(residual))
            message_spans = list(dict.fromkeys(residual_spans))
        # «Подешевле.» / «А есть полегче?» carry no multi-word span but DO
        # refine the current search: a short comparative/qualifier follow-up
        # is a REFINE, never an ambiguous message and never a new product.
        # (Kept as message_spans evidence for the classifier below.)

        names_other_product = False
        comparative_followup = _is_comparative_followup(message)
        explicit_compare = bool(_COMPARE_RE.search(message))
        if (
            (self._is_about_current(message) and not comparative_followup)
            or (explicit_compare and not comparative_followup)
        ):
            intent = MessageIntent.COMPARE
        elif index > 1 and _is_alternatives_request(message, self):
            intent = MessageIntent.ALTERNATIVES
        elif reversal:
            intent = MessageIntent.CORRECTION
        elif index == 1 and not self.active():
            # A genuinely empty ledger means this is the first product request.
            # A restored/seeded ledger can contain active slots before the
            # in-memory turn transcript is reconstructed; do not discard that
            # context merely because this is the first locally observed turn.
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
                r"(?:^|\s)(?:с|со|из|без|для|под|не|for|with|made\s+(?:of|from)|сделан\s+из|изготовлен\s+из|выполнен\s+из|чтобы|чтоб|and|so\s+that)\s+",
                message, re.IGNORECASE
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
            # Gemini may interpret ambiguous turns, but it must not overturn
            # either strong deterministic boundary: a newly named product
            # remains NEW_SEARCH, and a question about the shown set remains
            # COMPARE. This protects against any contradictory model action
            # (refine/correction/alternatives/clarify as well as the opposite
            # boundary action).
            # Connector-led characteristics are strong evidence of a
            # refinement, even if Gemini mistakes a material phrase for a
            # new product type. A genuinely named new product is still
            # classified as NEW_SEARCH by the message's own evidence.
            connector_led_refinement = bool(re.search(
                r"(?:^|\s)(?:с|со|из|без|для|под|не|for|with|"
                r"made\s+(?:of|from)|сделан\s+из|изготовлен\s+из|"
                r"выполнен\s+из|чтобы|чтоб|and|so\s+that)\s+",
                message, re.IGNORECASE,
            ))
            protected_transition = (
                intent is MessageIntent.NEW_SEARCH
                and intent_override != MessageIntent.NEW_SEARCH
                and (
                    names_other_product
                    or allow_product_switch
                    or self.active_by_kind("type") is None
                )
            ) or (
                intent is MessageIntent.COMPARE
                and intent_override != MessageIntent.COMPARE
            ) or (
                intent is MessageIntent.REFINE
                and intent_override is MessageIntent.NEW_SEARCH
                and connector_led_refinement
                and not allow_product_switch
            )
            if not protected_transition:
                intent = intent_override

        # A short follow-up must NOT reset the product context: only a NEW
        # SEARCH (a genuine new product request, reversal excluded) and an
        # explicit reset replace the ledger's active view.
        if intent is MessageIntent.NEW_SEARCH and (
            not reversal or intent_override == MessageIntent.NEW_SEARCH
        ):
            # An explicit, validated new_search decision wins over a reversal
            # marker when the user is switching products («на самом деле нужен
            # пылесос»). Otherwise the old product's budget/features/exclusions
            # would survive beside the new type. Shown IDs belong to the old
            # product too, so they must not leak into alternatives/compare.
            self._supersede_all(index)
            self.shown_product_ids.clear()
        elif intent is MessageIntent.CORRECTION:
            # A negated follow-up normally ADDS an exclusion; it does not
            # replace every earlier feature and exclusion. Only supersede a
            # positive feature that is directly contradicted by this exact
            # negated phrase. This keeps unrelated requirements (and prior
            # exclusions) intact when Gemini labels an additive refinement
            # as "correction".
            if negations:
                structured_targets = {
                    " ".join(str(update.get("target") or "").split()).casefold()
                    for update in (slot_updates or [])
                    if isinstance(update, dict)
                    and update.get("operation") == "replace"
                    and update.get("kind") == "feature"
                    and isinstance(update.get("target"), str)
                }
                if structured_targets:
                    # A validated explicit replacement names the exact old
                    # feature. Do not let token overlap supersede a different
                    # feature that happens to share a stem (e.g. battery type).
                    for slot in self.slots:
                        if (
                            slot.status == "active"
                            and slot.kind == "feature"
                            and " ".join(slot.value.split()).casefold() in structured_targets
                        ):
                            slot.status = "superseded"
                            slot.superseded_turn = index
                else:
                    from services.convsearch.type_validation import stems_match

                    negated_phrases = [
                        tokens(value) for value in negations if tokens(value)
                    ]
                    for slot in self.slots:
                        if slot.status != "active" or slot.kind != "feature":
                            continue
                        existing_tokens = tokens(slot.value)
                        # Require the complete negated phrase to match this
                        # feature. A shared single stem is not enough to
                        # erase a separate requirement (e.g. «аккумуляторная
                        # батарея» when the user replaces «два аккумулятора»).
                        if any(
                            all(
                                any(stems_match(existing, negated) for existing in existing_tokens)
                                for negated in phrase
                            )
                            for phrase in negated_phrases
                        ):
                            slot.status = "superseded"
                            slot.superseded_turn = index

            # A budget statement replaces only the previous budget. New
            # feature spans and exclusions are otherwise additive unless a
            # future structured correction contract identifies a narrower
            # replacement target.
            if _budget(message):
                self._supersede_kinds(index, {"budget"})
            structured_context_targets = {
                (update.get("kind"), " ".join(str(update.get("target") or "").split()).casefold())
                for update in (slot_updates or [])
                if isinstance(update, dict)
                and update.get("operation") == "replace"
                and update.get("kind") in {"use_case", "compatibility"}
                and isinstance(update.get("target"), str)
            }
            if (use_case_spans or compatibility_spans) and (
                reversal or structured_context_targets
            ):
                if structured_context_targets:
                    for slot in self.slots:
                        if (
                            slot.status == "active"
                            and slot.kind in {"use_case", "compatibility"}
                            and (slot.kind, " ".join(slot.value.split()).casefold())
                            in structured_context_targets
                        ):
                            slot.status = "superseded"
                            slot.superseded_turn = index
                else:
                    self._supersede_kinds(index, {"use_case", "compatibility"})
        self.record_turn(message)
        preferences = [
            _PREFERENCE_QUERY_TERMS[token]
            for token in tokens(message)
            if token in _PREFERENCE_QUERY_TERMS
        ]
        # Preserve connector-led requirements on the initial product
        # request as well as on follow-ups. The general span parser is
        # intentionally not trusted to turn every phrase in a new-search
        # sentence into a mandatory feature, but an explicit construction
        # such as «с двумя аккумуляторами» is a real user constraint.
        context_spans = [*use_case_spans, *compatibility_spans]
        explicit_feature_spans = [
            value for value in dict.fromkeys(introduced_features)
            if not any(
                set(tokens(value)) == set(tokens(context))
                for context in context_spans
            )
        ]
        return {
            "intent": intent,
            "negations": negations,
            "spans": message_spans,
            "explicit_features": explicit_feature_spans,
            "budget": _budget(message),
            "preferences": list(dict.fromkeys(preferences)),
            "use_cases": list(dict.fromkeys(use_case_spans)),
            "compatibilities": list(dict.fromkeys(compatibility_spans)),
            "reversal": reversal,
        }

    def record_slots(self, result: dict) -> None:
        """Turn the observation result into authoritative slots."""

        index = len(self.turns)
        model_updates = [
            update for update in (result.get("model_slot_updates") or [])
            if isinstance(update, dict)
        ]
        structured_positive_targets = {
            (
                update.get("kind"),
                " ".join(str(update.get("target") or "").split()).casefold(),
            )
            for update in model_updates
            if update.get("operation") in {"replace", "remove"}
            and update.get("kind") in {"use_case", "compatibility", "feature", "preference"}
            and isinstance(update.get("target"), str)
        }
        # Do not let deterministic extraction recreate a slot that Gemini
        # explicitly replaced or removed in this same turn. The model update
        # is applied at the end of this method, so all extraction loops below
        # must know both sides of those operations in advance.
        structured_targets: dict[str, set[str]] = {
            kind: set()
            for kind in {"use_case", "compatibility", "feature", "exclusion", "preference"}
        }
        structured_span_target_tokens: list[set[str]] = []
        structured_exclusion_targets: set[str] = set()
        for update in model_updates:
            kind = update.get("kind")
            operation = update.get("operation", "add")
            if kind not in structured_targets:
                continue
            values: list[str] = []
            if operation in {"replace", "remove"} and isinstance(update.get("target"), str):
                values.append(update["target"])
            if operation in {"add", "replace"} and isinstance(update.get("value"), str):
                values.append(update["value"])
            for raw_value in values:
                normalized = " ".join(raw_value.split()).casefold()
                if not normalized:
                    continue
                structured_targets[kind].add(normalized)
                value_tokens = set(tokens(raw_value))
                if value_tokens:
                    structured_span_target_tokens.append(value_tokens)
            if kind == "exclusion" and operation in {"replace", "remove"}:
                target = update.get("target")
                if isinstance(target, str):
                    structured_exclusion_targets.add(" ".join(target.split()).casefold())

        negations = result.get("negations") or []
        for value in negations:
            # «Не X, а Y» is a targeted replacement, not a request to
            # permanently exclude X from all future results. The replacement
            # operation supersedes the old slot below; do not also create an
            # exclusion for the same phrase.
            negation_tokens = set(tokens(str(value)))
            normalized_negation = " ".join(str(value).split()).casefold()
            if normalized_negation in structured_exclusion_targets or any(
                kind in {"feature", "use_case", "compatibility", "preference"}
                and target_tokens
                and target_tokens.issubset(negation_tokens)
                for kind, target in structured_positive_targets
                for target_tokens in [set(tokens(target))]
            ):
                continue
            self._add_slot(Slot(kind="exclusion", value=value, source_turn=index, raw_text=""))
        # Application context is searchable and available to semantic
        # selection, but is never a hard title-evidence requirement. Do not
        # re-add the old side of an explicit use-case replacement from the
        # deterministic phrase extractor.
        structured_use_case_targets = structured_targets["use_case"]
        # If Gemini identifies a connector phrase as hard compatibility,
        # do not also keep the deterministic parser's softer use_case copy
        # of the same target. Include old/new values for compatibility
        # corrections and removals so stale duplicates cannot survive.
        structured_compatibility_targets = structured_targets["compatibility"]
        for value in result.get("use_cases") or []:
            normalized_use_case = " ".join(str(value).split()).casefold()
            if (
                normalized_use_case in structured_use_case_targets
                or normalized_use_case in structured_compatibility_targets
            ):
                continue
            self._add_slot(Slot(
                kind="use_case", value=value, source_turn=index, raw_text=""
            ))
        # Explicit accessory fitment is a hard compatibility constraint,
        # not merely soft application context («насадка на дрель» must fit a
        # drill). Missing listing evidence is quarantined for semantic review.
        for value in result.get("compatibilities") or []:
            if " ".join(str(value).split()).casefold() in structured_compatibility_targets:
                continue
            self._add_slot(Slot(
                kind="compatibility", value=value, source_turn=index, raw_text=""
            ))
        # Feature spans are CONSTRAINTS of an active search. A NEW_SEARCH
        # message names a product, not a feature: recording its span
        # ("5 вариантов шуруповерта") as a required feature would demand
        # that literal phrase in every title and reject everything.
        # AMBIGUOUS messages carry no verified constraint either: recording
        # one would make a vague remark into a literal title requirement.
        model_use_case_tokens = [
            set(tokens(update.get("value", "")))
            for update in model_updates
            if update.get("kind") in {"use_case", "compatibility"} and isinstance(update.get("value"), str)
        ]
        feature_spans = result.get("spans") or []
        if result.get("intent") is MessageIntent.NEW_SEARCH:
            # On a fresh search, retain only explicitly connector-led
            # requirements. Do not reinterpret arbitrary title/request
            # scaffolding («5 вариантов шуруповёрта») as product features.
            feature_spans = result.get("explicit_features") or []
        explicit_feature_values = {
            " ".join(str(value).split()).casefold()
            for value in (result.get("explicit_features") or [])
            if isinstance(value, str) and value.strip()
        }
        if result.get("intent") not in (
            MessageIntent.AMBIGUOUS, MessageIntent.COMPARE,
            MessageIntent.ALTERNATIVES,
        ):
            for span in feature_spans:
                # If Gemini has identified a grounded application-context
                # phrase inside a larger grammatical span («буду работать
                # на стройке»), do not misrecord the whole sentence as a
                # mandatory product feature.
                span_token_set = set(tokens(span))
                if any(
                    wanted and wanted.issubset(span_token_set)
                    for wanted in model_use_case_tokens
                ) or any(
                    wanted and wanted.issubset(span_token_set)
                    for wanted in structured_span_target_tokens
                ):
                    continue
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
                if (
                    span.casefold() not in explicit_feature_values
                    and span_tokens
                    and all(
                        token in _QUALIFIER_WORDS
                        or is_colour_word(token)
                        or token in _BUDGET_FRAMING_WORDS
                        or token in _COUNT_WORDS
                        or token.isdigit()
                        for token in span_tokens
                    )
                ):
                    continue
                self._add_slot(Slot(kind="feature", value=span, source_turn=index, raw_text=""))
        # Comparative follow-ups must survive as active preferences. They
        # are useful retrieval/ranking signals, but never hard title filters.
        for value in result.get("preferences") or []:
            if " ".join(str(value).split()).casefold() in structured_targets["preference"]:
                continue
            self._add_slot(Slot(
                kind="preference", value=value, source_turn=index, raw_text=""
            ))
        budget = result.get("budget") or {}
        if budget.get("max") is not None:
            self._add_slot(Slot(
                kind="budget",
                value=f"{budget['max']} {budget.get('currency') or ''}".strip(),
                source_turn=index, raw_text="",
            ))

        # Gemini updates are validated against this exact user turn in
        # AIService. Apply them as explicit ledger operations: an additive
        # update cannot erase state, while replace/remove can affect only the
        # named slot of the same kind (never every feature/exclusion).
        for update in result.get("model_slot_updates") or []:
            if not isinstance(update, dict):
                continue
            kind = update.get("kind")
            operation = update.get("operation", "add")
            target = update.get("target")
            value = update.get("value")
            if kind not in {"use_case", "compatibility", "feature", "exclusion", "preference"}:
                continue
            if operation not in {"add", "replace", "remove"}:
                continue
            if operation in {"replace", "remove"}:
                if not isinstance(target, str) or not target.strip():
                    continue
                target_key = " ".join(target.split()).casefold()
                for slot in self.slots:
                    if (
                        slot.status == "active"
                        and slot.kind == kind
                        and " ".join(slot.value.split()).casefold() == target_key
                    ):
                        slot.status = "superseded"
                        slot.superseded_turn = index
            if operation == "remove":
                continue
            if not isinstance(value, str):
                continue
            value = " ".join(value.split()).strip()
            if value and len(value) <= 100:
                self._add_slot(Slot(
                    kind=kind, value=value, source_turn=index, raw_text=value,
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

        order = {"type": 0, "compatibility": 1, "use_case": 2, "feature": 3, "budget": 4, "preference": 5}
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
            if slot.kind in ("exclusion", "budget", "use_case"):
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
            "last_question": self.last_question,
            "structured_query": self.authoritative_text(),
        }

    # -- internals -----------------------------------------------------------

    def _add_slot(self, slot: Slot) -> None:
        normalized_value = " ".join(slot.value.split()).casefold()
        for existing in self.active():
            if (
                existing.kind == slot.kind
                and " ".join(existing.value.split()).casefold() == normalized_value
            ):
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
    # Match offsets are relative to the full input, not group(0). Slicing
    # group(0) with an absolute offset loses the currency after framing text.
    tail = (message[match.start():match.end()] or "").lower()
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
# Canonical, searchable forms of common comparative preferences. These are
# generic product attributes, not category-specific product rules. Preferences
# influence retrieval/ranking but are NOT hard title requirements: many listings
# omit weight/size details, so the bot must not pretend to verify them.
_PREFERENCE_QUERY_TERMS: dict[str, str] = {
    "полегче": "легкий", "легче": "легкий",
    "потяжелее": "тяжелый", "тяжелее": "тяжелый",
    "подешевле": "дешевый", "дешевле": "дешевый",
    "подороже": "дорогой", "дороже": "дорогой",
    "поменьше": "компактный", "меньше": "компактный",
    "побольше": "большой", "больше": "большой",
    "помощнее": "мощный", "мощнее": "мощный",
    "покороче": "короткий", "короче": "короткий",
    "подлиннее": "длинный", "длиннее": "длинный",
    "быстрее": "быстрый",
}

_QUALIFIER_WORDS: frozenset[str] = frozenset(
    {
        # Discourse words in reversal/opening phrases («на самом деле»)
        # frame the turn; they are not product requirements.
        "самом", "деле", "нибудь", "полезное", "полезный", "полезная",
        "интересное", "интересный", "интересная", "необычное", "необычный",
        "необычная", "любое", "любой", "любая", "то",
        # Demonstratives that point back to the visible candidate set.
        "этого", "этой", "этих", "этими", "эти", "данного", "данной",
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
        and not any(token.startswith(marker) for marker in _ALTERNATION_MARKERS)
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
