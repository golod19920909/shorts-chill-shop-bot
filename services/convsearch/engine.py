"""The conversational search engine — the single search authority.

Replaces the legacy search orchestration: ONE engine decides what a user
message means, builds the retrieval strategy, invokes the AliExpress
provider through ``services.convsearch.retrieval`` (an explicit adapter over
the existing ``SearchService.find_products`` provider machinery), validates
every candidate against the requested product type and the active
constraints, ranks the eligible pool, and returns ONE ``SearchDecision`` for
the Telegram layer to render.

Legacy components the engine does NOT call: ``services.chat_router``,
``services.conversation.resolve_request``, ``handlers.run_agent_turn`` /
``ShoppingAgent`` (the legacy Gemini agent rounds), the AI feed selector,
and any post-hoc curation that could re-filter or re-rank the engine's
decision. Shared infrastructure that IS reused deliberately: the provider
adapter in ``services.search_service`` (provider auth, retries, rate
limits, dedup), the honest ``Product`` model with its affiliate URLs, and
``services.relevance.relevance_tier`` as ONE of the ranking inputs (an
explicit, documented integration — never a hidden second decision-maker).

The engine NEVER fabricates a product, price, rating or link.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re

from models.product import Product
from services.convsearch import ranking as ranking_mod
from services.convsearch import retrieval as retrieval_mod
from services.convsearch.contract import Outcome, SearchDecision
from services.convsearch.state import (
    ConversationEngineState,
    MessageIntent,
    _is_comparative_followup,
)
from services.convsearch.type_validation import validate_product_type

logger = logging.getLogger(__name__)

#: The presentation cap. The engine never pads a smaller eligible pool to
#: reach it, and never exceeds it.
RESULT_CARD_LIMIT = 30
AI_CONVERSATION_SELECTION_LIMIT = 20
# A 20-listing semantic review is materially heavier than turn routing.
# Live telemetry showed the 8-second window repeatedly expired after the
# interpreter had already succeeded, silently replacing Gemini's whole-pool
# understanding with a deterministic fallback. Keep this bounded, but allow
# a realistic window for the selector to inspect real listing evidence.
AI_CONVERSATION_SELECTION_TIMEOUT_SECONDS = 15.0

DEFAULT_CLARIFY_QUESTION = (
    "Чтобы подобрать точнее: какой товар вас интересует и для какой задачи?"
)

_ALTERNATIVES_RE = re.compile(
    r"(?:покажи\s+(?:еще|ещё)?\s*другие|другие\s+варианты|других\s+вариантов|"
    r"что-нибудь\s+еще|что-нибудь\s+ещё|покажи\s+другое|show\s+(?:more|other)|"
    r"other\s+options|еще\s+варианты|ещё\s+варианты)",
    re.IGNORECASE,
)


def is_alternatives_request(text: str) -> bool:
    """Whether a message asks for DIFFERENT results within the current task."""

    return bool(_ALTERNATIVES_RE.search(text or ""))


# Pronouns and deictic references whose meaning depends on already-shown cards.
# A general concept question ("Что означает IP67?") is still eligible for a
# natural answer; a card reference ("А он водонепроницаемый?") must go through
# the grounded compare/product-answer path instead of free-form RESPOND.
_SHOWN_PRODUCT_REFERENCE_RE = re.compile(
    r"(?:^|\\W)(?:он|она|оно|они|его|нее|неё|него|них|ней|ним|"
    r"этот|эта|это|эти|этого|этой|этих|этим|"
    r"первый|первого|первая|второй|второго|вторая|третий|третьего|"
    r"последний|последнего|из\\s+них|среди\\s+них|"
    r"this|that|these|those|it|its|they|them|their|"
    r"first|second|third|last|among\\s+them|which\\s+one)"
    r"(?:$|\\W)",
    re.IGNORECASE,
)


def _references_shown_products(text: str) -> bool:
    """Whether this turn uses a deictic/anaphoric reference to shown cards."""

    return bool(_SHOWN_PRODUCT_REFERENCE_RE.search(text or ""))


#: Request-scaffolding and generic-count words that never name a product.
_REQUEST_FILLER: frozenset[str] = frozenset(
    {
        "покажи", "найди", "нужен", "нужна", "нужно", "нужны", "хочу",
        "ищу", "требуется", "требуются", "закажи", "закажите", "купи", "купите",
        "искать", "найти", "купить", "посоветуй", "подбери", "мне",
        "еще", "ещё", "вариант", "варианта", "вариантов", "варианты",
        "штук", "штуки", "товар", "товара", "товары", "please", "show",
        "find", "need", "want", "looking", "for", "options", "items",
        "something", "anything", "мощный", "недорогой", "недорогая",
        "хороший", "хорошая", "сделай", "сделать", "дай", "дайте",
        "покажите", "найдите", "подберите", "выведи", "подешевле",
        "подороже", "полегче", "получше", "похуже", "побольше",
        "поменьше", "покороче", "подлиннее", "быстрее", "мощнее",
        "дешевле", "дороже", "легче", "тяжелее", "лучше", "хуже",
        "используй", "показывай", "цены", "цена", "цену", "валюта",
        "другие", "других", "другое", "другой", "оставь", "выбери",
        "только", "тоже", "также", "а", "и", "но", "или", "с", "со",
        "без", "не", "до", "от", "для", "из", "в", "на", "по", "к",
        "грн", "гривн", "гривен", "гривень", "гривна", "гривны",
        "доллар", "доллара", "долларов", "долар", "доларів", "usd", "uah", "eur",
        "евро", "руб", "рубля", "рублей", "тыс", "тысяч", "тысяча", "тысячи",
        "тысячу", "тысячам", "тысячами", "тысячах", "тысяче", "тысячей",
        "thousand", "thousands", "that", "the", "with", "without",
        "not", "and", "but", "or", "more", "other", "another",
        "compare", "better", "which", "what", "about", "tell",
        "расскажи", "рассказать", "объясни", "объяснить", "подробнее",
        "про", "почему", "насчет", "насчёт", "что", "чем", "какой", "какая",
        "нибудь", "полезное", "полезный", "полезная", "интересное", "интересный",
        "интересная", "необычное", "необычный", "необычная", "любое", "любой",
        "любая", "то", "useful", "interesting", "unusual", "random",
        "какие", "какого", "какую", "каким", "зачем", "где", "когда",
        "втором", "первом", "второй",
        "первый", "третий",
        # Pronouns referring to the current product/result set are not item names.
        "его", "нее", "неё", "него", "них", "ней", "ним", "ими",
        "этот", "эта", "это", "эти", "этого", "этой", "этих", "этим",
        "такой", "такая", "такое", "такие", "того", "той",
        # Subordinate clauses introduce a characteristic of the active item,
        # not a second product identity ("Хочу, чтобы было два аккумулятора").
        "чтобы", "чтоб", "было", "были", "будут",
    }
)


#: Discourse / framing markers that OPEN or pivot a message («а теперь»,
#: «ну», «давай», «also») but never name a product. Closed-class words:
#: they are filtered from the product-type reading regardless of category.
_DISCOURSE_MARKERS: frozenset[str] = frozenset(
    {
        "теперь", "ну", "давай", "давайте", "ладно", "кстати", "вобщем",
        "вообще", "так", "итак", "тоже", "только", "сначала", "потом",
        "затем", "вместо", "вместо", "now", "well", "also", "instead",
        "actually", "maybe", "please", "самом", "деле",
    }
)


_RUSSIAN_ADJECTIVE_ENDING_RE = re.compile(
    r"(?:ый|ий|ой|ая|яя|ое|ее|ые|ие|ого|его|ому|ему|ым|им|ой|ей|"
    r"ую|юю|ых|их|ыми|ими|ше)$",
    re.IGNORECASE,
)


def _is_modifier_only_phrase(words: list[str]) -> bool:
    """Reject a phrase made only of inflected Russian modifiers, not a noun.

    This is deliberately a narrow grammatical guard, not a product catalogue:
    a lone adjective such as «автомобильный» cannot identify what to retrieve.
    A phrase with a noun head («автомобильный компрессор») remains valid.
    """
    return bool(words) and all(_RUSSIAN_ADJECTIVE_ENDING_RE.search(word) for word in words)


def _is_modifier_only_request(text: str) -> bool:
    """True when the requested-item phrase contains modifiers but no noun."""
    from services.convsearch.text import tokens

    phrase = _EXPLICIT_PRODUCT_CONTEXT_RE.split(text or "", maxsplit=1)[0]
    words = [
        token for token in tokens(phrase)
        if token not in _REQUEST_FILLER
        and token not in _DISCOURSE_MARKERS
        and not token.isdigit()
    ]
    return _is_modifier_only_phrase(words)


def _product_type_of(text: str) -> str | None:
    """The product type a NEW-search message names, or ``None``.

    The request's own head-noun reading: content tokens minus request
    scaffolding, budget and quantity words. "Нужна недорогая рыболовная
    катушка" → «рыболовная катушка"; "Покажи 5 вариантов шуруповерта" →
    «шуруповерта".
    """

    from services.convsearch.text import tokens

    # Keep relation targets (purpose, fitment, material) out of the
    # product identity. This matters especially when the user answers a
    # clarification with a noun phrase rather than a full imperative:
    # «Пылесос для дома» names a vacuum, not a product called «пылесос дома».
    phrase = _EXPLICIT_PRODUCT_CONTEXT_RE.split(text or "", maxsplit=1)[0]
    words = [
        token for token in tokens(phrase)
        if token not in _REQUEST_FILLER
        and token not in _DISCOURSE_MARKERS
        and not token.isdigit()
    ]
    if not words or _is_modifier_only_phrase(words):
        return None
    return " ".join(words)


_EXPLICIT_PRODUCT_REQUEST_RE = re.compile(
    r"\b(?:нужен|нужна|нужно|нужны|ищу|хочу|требуется|требуются|"
    r"подбери|подберите|найди|найдите|закажи|закажите|купи|купите)\s+(.+)",
    re.IGNORECASE,
)
_EXPLICIT_PRODUCT_CONTEXT_RE = re.compile(
    r"\b(?:для|с|со|из|без|под|на|до|от|в|по|к|у|о|об|чтобы|чтоб)\b",
    re.IGNORECASE,
)


def _explicit_requested_product_type(text: str) -> str | None:
    """Extract the requested item, leaving prepositional targets as context."""

    match = _EXPLICIT_PRODUCT_REQUEST_RE.search(text or "")
    if not match:
        return None
    phrase = match.group(1)
    boundary = _EXPLICIT_PRODUCT_CONTEXT_RE.search(phrase)
    if boundary:
        phrase = phrase[:boundary.start()]
    from services.convsearch.text import tokens

    words = [
        token for token in tokens(phrase)
        if token not in _REQUEST_FILLER
        and token not in _DISCOURSE_MARKERS
        and not token.isdigit()
    ]
    if words:
        if _is_modifier_only_phrase(words):
            return None
        from services.convsearch.state import _CHARACTERISTIC_NOUNS
        # A request for a feature/specification («нужна защита IP67»)
        # modifies the active product; it is not itself a product switch.
        if words[0] in _CHARACTERISTIC_NOUNS:
            return None
    return " ".join(words) or None


def _is_different_product_type(candidate: str, current: str) -> bool:
    """Compare the principal nouns, not shared descriptive modifiers.

    A shared adjective must not hide an explicit product switch:
    «сварочный аппарат» and «сварочный стол» are different items.
    Trailing/leading adjectives remain harmless because the same head-noun
    reader is used by product-type validation.
    """

    from services.convsearch.text import tokens
    from services.convsearch.type_validation import _head_noun, stems_match

    candidate_tokens = tokens(candidate)
    current_tokens = tokens(current)
    if not candidate_tokens or not current_tokens:
        return False

    candidate_head = _head_noun(candidate_tokens)
    current_head = _head_noun(current_tokens)
    if candidate_head and current_head:
        return not stems_match(candidate_head, current_head)

    # Conservative fallback for unusual token-only phrases without a
    # recoverable noun head. Do not let one shared modifier imply identity.
    return not any(
        stems_match(left, right)
        for left in candidate_tokens
        for right in current_tokens
    )


class ConversationSearchEngine:
    """The one search decision-maker. One instance per process."""

    def __init__(self, *, retrieval_backend=None) -> None:
        self._states: dict[str, ConversationEngineState] = {}
        #: The retrieval adapter (AliExpress provider machinery). Injectable
        #: for tests; defaults to the explicit adapter in
        #: ``services.convsearch.retrieval.RetrievalBackend``.
        self._backend = retrieval_backend or retrieval_mod.RetrievalBackend()
        #: Optional Gemini client for bounded turn interpretation and
        #: semantic selection of real candidates. Both calls have validated
        #: outputs and deterministic fallbacks.
        self._ai_service = None

    def bind_search_service(self, search_service) -> None:
        """Bind the real SearchService to the retrieval adapter (per turn)."""

        self._backend.bind(search_service)

    def bind_ai_service(self, ai_service) -> None:
        """Bind the optional Gemini conversation interpreter/selector.

        Tests and offline runs may omit it; deterministic intent, filtering
        and ranking remain available without a model.
        """

        self._ai_service = ai_service

    # -- state access -------------------------------------------------------

    def get_state(self, user_key: str) -> ConversationEngineState:
        state = self._states.get(user_key)
        if state is None or state.is_expired():
            state = ConversationEngineState(user_key=user_key)
            self._states[user_key] = state
        return state

    def reset(self, user_key: str) -> None:
        self._states.pop(user_key, None)

    # -- the turn -----------------------------------------------------------

    async def handle_message(
        self,
        user_key: str,
        text: str,
        *,
        exclude: set | None = None,
        result_limit: int | None = None,
        currency: str | None = None,
        state: ConversationEngineState | None = None,
        shown_products: list[Product] | None = None,
        on_search_start=None,
    ) -> SearchDecision:
        """ONE user message → ONE ``SearchDecision``. The whole search flow.

        ``state`` may be supplied by the caller (the handler keeps the
        per-user ledger in ``context.user_data`` — the bot's existing store,
        so per-user isolation and expiry follow the architecture it already
        trusts). Without one, a process-local ledger is used.
        """

        state = state or self.get_state(user_key)

        # Gemini interprets EVERY user turn against the recent conversation.
        # Its response is constrained to a validated action enum and a
        # product-type field; it never supplies products or bypasses the
        # deterministic slot ledger, AliExpress retrieval, validation, or
        # ranking. If the model is unavailable, malformed, or uncertain, the
        # state machine keeps its deterministic classification.
        ai_turn = None
        ai = self._ai_service
        interpreter = getattr(ai, "decide_conversation_turn", None) if ai is not None else None
        if callable(interpreter):
            context_payload = {
                "recent_turns": [turn.text for turn in state.turns[-6:]],
                "active_slots": [slot.as_dict() for slot in state.active()],
                "shown_product_count": len(shown_products or []),
                # Ground references such as «второй», «этот» and «из них»
                # in the actual cached cards, not only a numeric count.
                "shown_products": [
                    {
                        "id": product.product_id,
                        "title": product.title[:180],
                        "price": product.price,
                        "currency": product.currency,
                    }
                    for product in (shown_products or [])[:30]
                ],
                "last_outcome": state.last_outcome,
                "pending_question": state.last_question,
            }
            try:
                ai_turn = await interpreter(text, context=context_payload)
            except Exception:  # noqa: BLE001 - deterministic fallback must survive AI failure
                logger.info("Gemini turn interpretation failed; using deterministic intent")
                ai_turn = None

        # Natural replies are outside the shopping state machine: record the
        # utterance, but do not mutate search slots or call the marketplace.
        explicit_product_type = _explicit_requested_product_type(text)
        # A free-form natural response must never answer a question about
        # a previously shown product from model guesswork. When the message
        # refers to a shown card ("is it waterproof?", "which of these?"),
        # route it through the grounded compare/product-answer path. General
        # concept questions ("what does IP67 mean?") remain RESPOND even while
        # a shopping conversation is active.
        if (
            isinstance(ai_turn, dict)
            and ai_turn.get("action") == "respond"
            and shown_products
            and _references_shown_products(text)
        ):
            ai_turn = {
                "action": "compare",
                "confidence": ai_turn.get("confidence", 0.0),
                "product_type": None,
                "marketplace_terms": [],
                "question": None,
                "response": None,
                "slot_updates": [],
            }

        if (isinstance(ai_turn, dict) and ai_turn.get("action") == "respond"
                and not explicit_product_type
                and isinstance(ai_turn.get("response"), str)
                and ai_turn["response"].strip()):
            state.record_turn(text)
            state.last_outcome = Outcome.RESPOND.value
            return SearchDecision(
                outcome=Outcome.RESPOND,
                reply=ai_turn["response"].strip(),
                report={"intent": "respond", "snapshot": state.snapshot()},
            )

        action_to_intent = {
            "new_search": MessageIntent.NEW_SEARCH,
            "refine": MessageIntent.REFINE,
            "correction": MessageIntent.CORRECTION,
            "alternatives": MessageIntent.ALTERNATIVES,
            "compare": MessageIntent.COMPARE,
            "clarify": MessageIntent.AMBIGUOUS,
        }
        intent_override = (
            action_to_intent.get(ai_turn.get("action"))
            if isinstance(ai_turn, dict) else None
        )
        # Product continuity is decided from a grounded item identity, not
        # from a raw-token mismatch. This lets Gemini interpret a follow-up
        # naturally while still protecting a genuinely named product switch.
        explicit_product_type = _explicit_requested_product_type(text)
        current_type = state.active_by_kind("type")
        message_product_type = (
            explicit_product_type
            or (
                _product_type_of(text)
                if isinstance(ai_turn, dict)
                and ai_turn.get("action") == "new_search"
                else None
            )
        )
        explicit_product_switch = bool(
            message_product_type
            and current_type is not None
            and _is_different_product_type(message_product_type, current_type.value)
        )
        if explicit_product_switch:
            intent_override = MessageIntent.NEW_SEARCH
        elif (
            current_type is not None
            and isinstance(ai_turn, dict)
            and ai_turn.get("action") == "new_search"
            and not explicit_product_type
            and not state.last_question
        ):
            if message_product_type:
                # Gemini's proposed identity is the same item already being
                # discussed: keep the current shopping thread and refine it.
                intent_override = MessageIntent.REFINE
            else:
                # A modifier/constraint without a new item identity still
                # belongs to the active request. A bare "find something else"
                # does not: ask for the missing product instead of repeating
                # the old search under a misleading new-search label.
                refinement_signal = (
                    _is_comparative_followup(text)
                    or _is_modifier_only_request(text)
                    or bool(_EXPLICIT_PRODUCT_CONTEXT_RE.search(text))
                )
                if is_alternatives_request(text):
                    intent_override = MessageIntent.ALTERNATIVES
                elif refinement_signal:
                    intent_override = MessageIntent.REFINE
                elif not state.last_question:
                    question = "Какой именно товар вы хотите найти?"
                    state.record_turn(text)
                    state.last_outcome = Outcome.CLARIFY.value
                    state.last_question = question
                    return SearchDecision(
                        outcome=Outcome.CLARIFY,
                        question=question,
                        refined=False,
                        report={"intent": MessageIntent.AMBIGUOUS,
                                "reason": "new_search_without_product_identity",
                                "snapshot": state.snapshot()},
                    )

        # Fail closed on a modifier-only fragment when there is no active
        # product identity. A timeout or an over-eager model must not turn
        # «Нужен автомобильный» into a marketplace query for that adjective.
        # The next clarification answer remains attached to the conversation.
        if (
            current_type is None
            and _product_type_of(text) is None
            and intent_override in {None, MessageIntent.NEW_SEARCH}
        ):
            intent_override = MessageIntent.AMBIGUOUS

        # A pending clarification answer fills a gap in the CURRENT request;
        # it is not a replacement search. Gemini may correctly identify the
        # missing product in the answer and call it "new_search", but allowing
        # that action to supersede the ledger would erase constraints captured
        # before the clarification (budget, use case, exclusions, etc.).
        # last_question is deliberately retained across provider failures,
        # where last_outcome becomes PROVIDER_ERROR. It is therefore the
        # authoritative pending-clarification marker; checking last_outcome
        # here would break the retry and let a repeated answer reset the
        # already-populated ledger.
        pending_clarification = bool(state.last_question)
        if (
            pending_clarification
            and not explicit_product_switch
            and isinstance(ai_turn, dict)
            and ai_turn.get("action") == "new_search"
        ):
            current_type = state.active_by_kind("type")
            proposed_type = (
                ai_turn.get("product_type")
                if isinstance(ai_turn.get("product_type"), str) else None
            ) or _product_type_of(text)
            if current_type is None or not proposed_type:
                # The clarification is filling a missing type, or answering
                # a question about the already-active product without naming
                # a different one. Preserve the existing ledger in both cases.
                intent_override = MessageIntent.REFINE
            else:
                from services.convsearch.text import tokens
                from services.convsearch.type_validation import stems_match
                current_tokens = tokens(current_type.value)
                proposed_tokens = tokens(proposed_type)
                same_head = bool(
                    current_tokens and proposed_tokens
                    and stems_match(current_tokens[-1], proposed_tokens[-1])
                )
                if same_head:
                    # The user answered a question about the existing product;
                    # this is still the same search, not a reset.
                    intent_override = MessageIntent.REFINE
                # A different product head is an explicit switch. Keep
                # NEW_SEARCH so observe() supersedes the old product ledger,
                # budget and shown-result history instead of mixing categories.

        # ONE state transition and ONE strategy build. The model selects the
        # action; the ledger records this turn and keeps slot provenance.
        model_slot_updates = (
            ai_turn.get("slot_updates", [])
            if isinstance(ai_turn, dict)
            and ai_turn.get("action") in {"new_search", "refine", "correction"}
            and not (
                explicit_product_switch
                and ai_turn.get("action") != "new_search"
            )
            else []
        )
        observation = state.observe(
            text,
            intent_override=intent_override,
            slot_updates=model_slot_updates,
            allow_product_switch=explicit_product_switch,
        )
        # The empty-ledger first-turn shortcut in the generic state machine
        # intentionally assumes a new product request. The conversational
        # engine has stronger product-identity evidence: if the message
        # contains no noun head, do not let that shortcut promote an adjective
        # or fragment into a searchable product.
        if (
            current_type is None
            and _product_type_of(text) is None
            and observation.get("intent") == MessageIntent.NEW_SEARCH
        ):
            observation["intent"] = MessageIntent.AMBIGUOUS
        if model_slot_updates:
            observation["model_slot_updates"] = model_slot_updates
        state.record_slots(observation)
        intent = observation["intent"]

        if (
            pending_clarification
            and ai_turn is None
            and not explicit_product_switch
            and _product_type_of(text) is None
        ):
            # When Gemini cannot interpret an answer to a pending question,
            # do not let the generic state machine promote a short fragment
            # («автомобильный») into a new product search. If the turn added
            # no grounded use-case/feature/budget detail, keep the question
            # open and ask again instead of querying a broad keyword.
            current_turn_index = len(state.turns)
            has_grounded_detail = any(
                slot.status == "active"
                and slot.source_turn == current_turn_index
                and slot.kind in {
                    "use_case", "compatibility", "feature",
                    "exclusion", "preference", "budget",
                }
                for slot in state.slots
            )
            if not has_grounded_detail:
                question = state.last_question or DEFAULT_CLARIFY_QUESTION
                state.last_outcome = Outcome.CLARIFY.value
                state.last_question = question
                return SearchDecision(
                    outcome=Outcome.CLARIFY,
                    question=question,
                    refined=False,
                    report={"intent": intent, "reason": "clarification_answer_incomplete",
                            "snapshot": state.snapshot()},
                )

        if current_type is None and _is_modifier_only_request(text):
            question = (
                "Какой именно товар вам нужен? Напишите его название — "
                "и я помогу подобрать подходящий вариант."
            )
            state.last_outcome = Outcome.CLARIFY.value
            state.last_question = question
            return SearchDecision(
                outcome=Outcome.CLARIFY,
                question=question,
                refined=False,
                report={"intent": intent, "reason": "product_identity_incomplete",
                        "snapshot": state.snapshot()},
            )

        if isinstance(ai_turn, dict) and ai_turn.get("action") == "clarify":
            # If the message itself clearly introduced a new product, retain
            # that type even though Gemini needs one more detail before
            # retrieval. Otherwise the user's answer to «for what task?» would
            # have no product type to attach to and could fall back to the
            # previous product's ledger.
            if intent is MessageIntent.NEW_SEARCH:
                clarified_type = (
                    (explicit_product_type if explicit_product_switch else None)
                    or _product_type_of(text)
                )
                if clarified_type:
                    state.record_product_type(clarified_type)
            question = ai_turn.get("question") or DEFAULT_CLARIFY_QUESTION
            state.last_outcome = Outcome.CLARIFY.value
            state.last_question = question
            return SearchDecision(
                outcome=Outcome.CLARIFY,
                question=question,
                refined=False,
                report={"intent": intent, "reason": "gemini_clarify",
                        "snapshot": state.snapshot()},
            )

        # A question about the SHOWN products («какие лучше?», «чем
        # отличаются?») is answered over the already-presented candidates -
        # never as another provider search. Nothing shown yet: a bare
        # question cannot be searched for and has nothing to compare, so ask
        # ONE honest clarification.
        if intent is MessageIntent.COMPARE:
            if state.shown_product_ids:
                return SearchDecision(
                    outcome=Outcome.COMPARE, refined=True,
                    report={"intent": intent, "snapshot": state.snapshot()},
                )
            state.last_outcome = Outcome.CLARIFY.value
            state.last_question = DEFAULT_CLARIFY_QUESTION
            return SearchDecision(
                outcome=Outcome.CLARIFY,
                question=DEFAULT_CLARIFY_QUESTION,
                refined=False,
                report={"intent": intent, "snapshot": state.snapshot()},
            )

        # A NEW search names the product in THIS message: record it as the
        # active type and make the just-recorded type the hard gate for the
        # pool (an accessory or a spare part can never pass as the product).
        model_type = (
            ai_turn.get("product_type")
            if isinstance(ai_turn, dict) else None
        )
        if intent is MessageIntent.NEW_SEARCH:
            # The explicit request head is grounded in the user's own words
            # and takes precedence over a stale or contradictory model type.
            # When the user explicitly names the requested item, its
            # grammatical head is authoritative. Gemini may help interpret
            # the turn, but must not replace «насадка» with another noun from
            # the same sentence such as its compatibility target «дрель».
            if explicit_product_type and model_type:
                from services.convsearch.text import tokens
                from services.convsearch.type_validation import stems_match

                explicit_tokens = tokens(explicit_product_type)
                model_tokens = tokens(model_type)
                same_requested_head = bool(
                    explicit_tokens and model_tokens
                    and stems_match(explicit_tokens[-1], model_tokens[-1])
                )
                new_type = (
                    model_type if same_requested_head else explicit_product_type
                )
            else:
                new_type = explicit_product_type or model_type or _product_type_of(text) or None
            if new_type:
                state.record_product_type(new_type)
        elif pending_clarification and not state.active_by_kind("type"):
            # The answer supplies the missing type without resetting the
            # other slots already recorded from the original request. If
            # Gemini timed out or returned no type, derive the leading item
            # phrase deterministically; relation targets («для дома»,
            # «на дрель», «по дереву») are not product identity.
            clarification_type = (
                model_type.strip()
                if isinstance(model_type, str) and model_type.strip()
                else _product_type_of(text)
            )
            if clarification_type:
                state.record_product_type(clarification_type)

        strategy = retrieval_mod.build_retrieval_strategy(
            state, text,
            currency=currency,
            result_limit=result_limit,
        )
        if strategy is None:
            question = await self._clarification_question(text)
            state.last_outcome = Outcome.CLARIFY.value
            state.last_question = question
            return SearchDecision(
                outcome=Outcome.CLARIFY,
                question=question,
                refined=False,
                report={"intent": intent, "snapshot": state.snapshot()},
            )

        # The interpreter may provide one alternate seller-catalogue phrase
        # for any search-producing turn. It is retrieval-only: it never
        # changes the active product identity or hard requirements. The
        # query-variant layer independently checks that it is cross-script.
        if intent in {
            MessageIntent.NEW_SEARCH,
            MessageIntent.REFINE,
            MessageIntent.CORRECTION,
            MessageIntent.ALTERNATIVES,
        } and isinstance(ai_turn, dict):
            alternate_terms = ai_turn.get("marketplace_terms", [])
            if isinstance(alternate_terms, list):
                bounded_terms = [
                    " ".join(term.split()).strip()
                    for term in alternate_terms[:1]
                    if isinstance(term, str)
                    and term.strip()
                    and len(term) <= 120
                    and len(term.split()) <= 10
                ]
                if bounded_terms:
                    strategy.request = strategy.request.model_copy(
                        update={"intent_marketplace_terms": bounded_terms}
                    )

        refined = intent is not MessageIntent.NEW_SEARCH
        if callable(on_search_start):
            try:
                await on_search_start()
            except Exception:  # noqa: BLE001 - UI status must not block a search
                logger.debug("Could not send search-start status", exc_info=True)
        # Reuse the exact production evaluator after each marketplace page.
        # Pagination ends only when the current pool can already support the
        # requested number of deliverable cards, or when the configured page
        # limit / provider end-of-results signal is reached.
        candidate_target = RESULT_CARD_LIMIT
        if strategy.result_limit is not None:
            candidate_target = min(
                RESULT_CARD_LIMIT, max(1, int(strategy.result_limit))
            )

        def needs_more_candidates(products: list[Product]) -> bool:
            preview = self._evaluate_pool(
                state, products, strategy,
                refined=refined, intent=intent, observation=observation,
                exclude_shown=(intent is MessageIntent.ALTERNATIVES),
            )
            safe_count = int(preview.report.get("safe_ranked", 0))
            should_continue = safe_count < candidate_target
            logger.info(
                "convsearch page eligibility raw=%d ranked=%d verified=%d "
                "target=%d continue=%s",
                len(products),
                preview.report.get("ranked", 0),
                safe_count,
                candidate_target,
                should_continue,
            )
            return should_continue

        try:
            backend_outcome = await self._backend.retrieve(
                strategy, exclude=exclude, should_continue=needs_more_candidates
            )
        except Exception as exc:  # noqa: BLE001 - provider failures are honest
            logger.warning("convsearch retrieval failed: %s", type(exc).__name__)
            state.last_outcome = Outcome.PROVIDER_ERROR.value
            # A failed provider call does not consume a pending clarification.
            return SearchDecision(
                outcome=Outcome.PROVIDER_ERROR,
                refined=refined,
                report={"intent": intent, "error": type(exc).__name__,
                        "snapshot": state.snapshot()},
            )

        if not backend_outcome.ok:
            state.last_outcome = backend_outcome.reason
            # Keep the pending question so the user can retry the same answer.
            return SearchDecision(
                outcome=Outcome.PROVIDER_ERROR,
                refined=refined,
                request=backend_outcome.request,
                report={"intent": intent, "reason": backend_outcome.reason,
                        "snapshot": state.snapshot()},
            )

        decision = self._evaluate_pool(
            state, backend_outcome.products, strategy,
            refined=refined, intent=intent, observation=observation,
            exclude_shown=(intent is MessageIntent.ALTERNATIVES),
        )
        # Turn interpretation and post-retrieval candidate selection are
        # separate decisions with separate bounded windows. The interpreter
        # is already capped by AIService at five seconds; do not deduct that
        # time from candidate selection. The previous shared eight-second
        # budget routinely left the selector only 2-3 seconds after a normal
        # interpretation, causing deterministic mixed-pool fallbacks.
        # Preserve the exact reason a non-empty provider pool is rejected by
        # the hard identity gate. The compact action summary below is not
        # sufficient to diagnose a bad active product type or a systematic
        # title-parser false negative from live Telegram logs.
        if decision.report.get("rejected_type", 0):
            logger.warning(
                "convsearch type gate diagnostic product_type=%r active_slots=%s "
                "rejected=%d sample=%s",
                strategy.product_type,
                [(slot.kind, slot.value) for slot in state.active()],
                decision.report.get("rejected_type", 0),
                decision.report.get("type_rejections", [])[:8],
            )

        decision = await self._apply_ai_candidate_selection(
            decision, strategy, state,
            timeout_seconds=AI_CONVERSATION_SELECTION_TIMEOUT_SECONDS,
        )
        # When the catalogue search completed but yielded no safe match,
        # let Gemini phrase one grounded follow-up question. This call is
        # skipped after a selector timeout/error so a slow or unavailable
        # model cannot make an already-empty answer wait even longer.
        selection_status = decision.report.get("ai_selection")
        if (
            decision.outcome in {Outcome.NO_CANDIDATES, Outcome.NO_ELIGIBLE}
            and selection_status not in {
                "timeout_fallback", "error_fallback", "unavailable_fallback",
                "turn_budget_exhausted",
            }
        ):
            composer = getattr(self._ai_service, "compose_no_results_reply", None)
            if callable(composer):
                try:
                    async with asyncio.timeout(3.2):
                        reply = await composer(
                            text,
                            active_slots=[
                                {"kind": slot.kind, "value": slot.value}
                                for slot in state.active()
                            ],
                            reason=str(decision.report.get("reason") or decision.outcome.value),
                        )
                    if isinstance(reply, str) and reply.strip():
                        decision.reply = reply.strip()
                        decision.report["no_results_reply"] = "gemini"
                except Exception:  # noqa: BLE001 - a reply must never break search
                    logger.info(
                        "Natural no-results reply unavailable; using safe fallback",
                        exc_info=True,
                    )
        state.last_outcome = decision.outcome.value
        state.last_question = None
        # Selection is not delivery. The Telegram handler acknowledges the
        # products only after the first card was successfully sent.
        # One INFO line per turn: the intent, the action and the pool fate -
        # counts only, no user text (§7 traceability).
        logger.info(
            "convsearch pool stages raw=%d type=%d features=%d exclusions=%d "
            "budget=%d dedup=%d rank_rejected=%d ranked=%d verified=%d "
            "semantic_review=%d ai_selected=%d final=%d",
            decision.report.get("raw", 0),
            decision.report.get("after_type", 0),
            decision.report.get("after_feature", 0),
            decision.report.get("after_exclusion", 0),
            decision.report.get("after_budget", 0),
            decision.report.get("after_dedup", 0),
            decision.report.get("rank_rejected", 0),
            decision.report.get("ranked", 0),
            decision.report.get("safe_ranked", 0),
            len(decision.report.get("semantic_review_ids", [])),
            decision.report.get("ai_selected", 0),
            len(decision.products),
        )
        logger.info(
            "convsearch action=%s intent=%s reason=%s raw=%d kept=%d ai_selection=%s",
            decision.outcome.value,
            getattr(intent, "value", str(intent)),
            decision.report.get("reason", "-"),
            decision.report.get("raw", 0),
            len(decision.products),
            decision.report.get("ai_selection", "not_used"),
        )
        return decision

    async def _apply_ai_candidate_selection(
        self, decision: SearchDecision, strategy, state: ConversationEngineState,
        *, timeout_seconds: float | None = None,
    ) -> SearchDecision:
        """Use Gemini to rank a bounded slice of already validated listings.

        Deterministic product-type, exclusion, budget and verified-feature
        checks run before this method. Gemini ranks the first 20 candidates.
        A valid empty selection is an explicit semantic no-match. On timeout
        or malformed/unavailable output, deterministic candidates may remain,
        but every candidate requiring semantic identity/feature review stays
        quarantined unless Gemini explicitly confirms it.

        The AI selection window (20) is separate from the presentation cap
        (30). After the AI-ranked window, up to ten additional deterministic
        candidates may fill the remaining presentation slots.
        """
        if decision.outcome is not Outcome.RESULTS or not decision.products:
            return decision

        selector = getattr(self._ai_service, "select_conversation_candidates", None)
        semantic_review_ids = {
            product_id for product_id in decision.report.get("semantic_review_ids", [])
            if product_id
        }
        original_pool = list(decision.products)
        # Give uncertain-but-potentially-valid candidates a place in the
        # bounded review window; otherwise they could sit beyond the first
        # 20 ranked rows and never receive the semantic check they need.
        review_pool = [
            product for product in original_pool
            if product.product_id in semantic_review_ids
        ]
        ordinary_pool = [
            product for product in original_pool
            if product.product_id not in semantic_review_ids
        ]
        # Reserve room for BOTH kinds of candidates. A broad pool can
        # contain many sparse-title / feature-review rows; putting all of
        # them first used to let uncertain listings occupy the entire 20-row
        # Gemini window and hide already-verified products from consideration.
        # Split the window evenly when both groups are present, then use any
        # unused places for the group that still has candidates. Ranking order
        # is preserved inside each group.
        review_quota = min(
            len(review_pool), (AI_CONVERSATION_SELECTION_LIMIT + 1) // 2
        )
        ordinary_quota = min(
            len(ordinary_pool), AI_CONVERSATION_SELECTION_LIMIT - review_quota
        )
        remaining_quota = (
            AI_CONVERSATION_SELECTION_LIMIT - review_quota - ordinary_quota
        )
        extra_review = min(
            remaining_quota, max(0, len(review_pool) - review_quota)
        )
        review_quota += extra_review
        remaining_quota -= extra_review
        ordinary_quota += min(
            remaining_quota, max(0, len(ordinary_pool) - ordinary_quota)
        )
        selector_pool = (
            review_pool[:review_quota] + ordinary_pool[:ordinary_quota]
        )
        selector_ids = {
            product.product_id for product in selector_pool if product.product_id
        }
        decision.report["ai_review_pool"] = len(review_pool)
        decision.report["ai_ordinary_pool"] = len(ordinary_pool)
        decision.report["ai_review_in_window"] = review_quota
        decision.report["ai_ordinary_in_window"] = ordinary_quota
        presentation_limit = RESULT_CARD_LIMIT
        if strategy.result_limit is not None:
            presentation_limit = min(
                RESULT_CARD_LIMIT, max(1, int(strategy.result_limit))
            )
        wanted = min(AI_CONVERSATION_SELECTION_LIMIT, len(original_pool), presentation_limit)

        def deterministic_safe_pool() -> list[Product]:
            return [
                product for product in original_pool
                if product.product_id not in semantic_review_ids
            ]

        def finish_fallback(reason: str) -> SearchDecision:
            # Gemini selection is an optional ranking/review layer. Candidates
            # that passed every deterministic hard gate remain safe to show;
            # only candidates explicitly quarantined for semantic review stay
            # hidden when the model is unavailable.
            safe = deterministic_safe_pool()[:presentation_limit]
            decision.report["ai_selection"] = reason
            if safe:
                decision.products = safe
                decision.report["reason"] = "deterministic_fallback"
                decision.report["ai_fallback_kept"] = len(safe)
                return decision

            decision.products = []
            decision.outcome = Outcome.NO_ELIGIBLE
            if decision.report.get("feature_review_ids"):
                decision.report["reason"] = "unverified_required_features"
            elif semantic_review_ids:
                decision.report["reason"] = "unverified_product_identity"
            else:
                decision.report["reason"] = "semantic_selection_unavailable"
            return decision

        if not callable(selector):
            # No semantic selector is bound (offline deterministic mode).
            # Keep only candidates that already passed every deterministic
            # hard gate; uncertain identity/feature candidates stay excluded.
            if semantic_review_ids:
                decision.products = deterministic_safe_pool()[:presentation_limit]
                decision.report["ai_selection"] = "semantic_review_unavailable"
                if not decision.products:
                    decision.outcome = Outcome.NO_ELIGIBLE
                    if (
                        state.active_values("feature")
                        or state.active_values("compatibility")
                        or decision.report.get("feature_review_ids")
                    ):
                        decision.report["reason"] = "unverified_required_features"
                    else:
                        decision.report["reason"] = "unverified_product_identity"
            return decision

        query_parts = [strategy.request.normalized_query or strategy.request.original_query or ""]
        query_parts.extend(state.active_values("use_case"))
        query_parts.extend("Совместимость: " + value for value in state.active_values("compatibility"))
        query_parts.extend(state.active_values("feature"))
        query_parts.extend(state.active_values("preference"))
        exclusions = state.active_values("exclusion")
        if exclusions:
            query_parts.append("ИСКЛЮЧИТЬ: " + "; ".join(exclusions))
        budget = state.active_by_kind("budget")
        if budget is not None:
            query_parts.append(f"ЖЁСТКИЙ БЮДЖЕТ: до {budget.value}")
        query = " | ".join(part.strip() for part in query_parts if part and part.strip())

        selection_timeout = min(
            AI_CONVERSATION_SELECTION_TIMEOUT_SECONDS,
            max(0.0, timeout_seconds)
            if timeout_seconds is not None
            else AI_CONVERSATION_SELECTION_TIMEOUT_SECONDS,
        )
        if selection_timeout <= 0:
            return finish_fallback("turn_budget_exhausted")

        selector_kwargs = {
            "query": query,
            "wanted": wanted,
            "required_features": [
                *state.active_values("feature"),
                *state.active_values("compatibility"),
            ],
            "identity_review_ids": [
                product_id for product_id in semantic_review_ids
                if product_id
            ],
            "marketplace_terms": getattr(
                strategy.request, "intent_marketplace_terms", []
            ),
        }
        # Keep injected selectors / older adapters that implement the
        # established contract working. The production AIService accepts the
        # optional seller-language hint; test doubles and external adapters
        # that do not declare it continue receiving the original arguments.
        try:
            parameters = inspect.signature(selector).parameters.values()
            accepts_extra = any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters
            )
            parameter_names = {
                parameter.name
                for parameter in inspect.signature(selector).parameters.values()
            }
            if not accepts_extra and "marketplace_terms" not in parameter_names:
                selector_kwargs.pop("marketplace_terms")
        except (TypeError, ValueError):
            selector_kwargs.pop("marketplace_terms", None)

        try:
            async with asyncio.timeout(selection_timeout):
                selected_ids = await selector(selector_pool, **selector_kwargs)
        except TimeoutError:
            logger.info("Gemini conversation selection timed out")
            return finish_fallback("timeout_fallback")
        except Exception:
            logger.exception("Gemini conversation selection failed")
            return finish_fallback("error_fallback")

        if selected_ids is None or not isinstance(selected_ids, list):
            return finish_fallback("unavailable_fallback")

        allowed = {
            product.product_id: product
            for product in selector_pool
            if product.product_id
        }
        selected: list[Product] = []
        seen: set[str] = set()
        for product_id in selected_ids:
            if (
                isinstance(product_id, str)
                and product_id in allowed
                and product_id not in seen
            ):
                selected.append(allowed[product_id])
                seen.add(product_id)
            if len(selected) >= wanted:
                break

        # AIService distinguishes a valid empty ID list (no supplied
        # listing fits) from an unusable response (None). Respect that
        # explicit no-match decision: falling back here would re-display the
        # very candidates Gemini just rejected as irrelevant.
        if not selected and selected_ids == []:
            decision.products = []
            decision.report["ai_selected"] = 0
            decision.outcome = Outcome.NO_ELIGIBLE
            decision.report["ai_selection"] = "semantic_no_match"
            decision.report["reason"] = "semantic_no_match"
            return decision
        if not selected:
            return finish_fallback("empty_fallback")

        decision.report["ai_selected"] = len(selected)

        # The model only sees/selects the first 20. Keep its ordering.
        # Append the untouched deterministic tail ONLY when Gemini filled
        # the whole selection window. A shorter valid selection can mean
        # that fewer candidates actually fit; padding it with unseen rows
        # would silently override the model's semantic decision.
        tail = []
        if len(selected) >= wanted:
            tail = [
                product for product in original_pool
                if product.product_id not in selector_ids
                and product.product_id not in semantic_review_ids
                and product.product_id not in seen
            ]
        decision.products = (selected + tail)[:presentation_limit]
        decision.report["ai_selection"] = "selected"
        decision.report["ai_selected"] = len(selected)
        decision.report["ai_tail_added"] = min(
            len(tail), max(0, presentation_limit - len(selected))
        )
        return decision

    # -- clarification policy ------------------------------------------------

    async def _clarification_question(self, text: str) -> str:
        """ONE clarifying question for a message with nothing to search with.

        Deterministic by default; when a Gemini client is bound AND the
        message is very short (the only case where a model adds value), the
        bounded ``decide_chat_action`` decision may phrase the question.
        Everything else keeps the default question — a failing or absent
        model can never block the conversation.
        """

        ai = self._ai_service
        if (
            ai is not None
            and len((text or '').split()) <= 4
            and callable(getattr(ai, 'decide_chat_action', None))
        ):
            try:
                action = await ai.decide_chat_action(text)
                if (
                    isinstance(action, dict)
                    and action.get('action') == 'clarify'
                    and isinstance(action.get('question'), str)
                    and action['question'].strip()
                ):
                    return action['question'].strip()
            except Exception:  # noqa: BLE001 - a model failure never blocks a question
                logger.info('Clarification model unavailable; using the default question')
        return DEFAULT_CLARIFY_QUESTION

    # -- candidate evaluation -------------------------------------------------

    def _evaluate_pool(
        self,
        state: ConversationEngineState,
        products: list[Product],
        strategy,
        *,
        refined: bool,
        intent,
        observation: dict,
        exclude_shown: bool,
    ) -> SearchDecision:
        """Validate → filter → dedup → rank one provider pool.

        The report records every real stage with its OWN counts (retrieval,
        type gate, exclusions, budget, shown-exclusion, ranking) so a
        ``no_eligible`` outcome is always traceable to the stage that
        emptied the pool — never to a discarded intermediate result.
        """

        product_type = strategy.product_type
        report: dict = {
            # The Telegram handler uses this authoritative outcome metadata
            # to invalidate the previous shown-session pointer on a product
            # switch, including empty and rejected result pools.
            "intent": intent,
            "raw": len(products),
            "after_type": 0,
            "after_feature": 0,
            "after_exclusion": 0,
            "after_budget": 0,
            "after_dedup": 0,
            "after_shown": 0,
            "ranked": 0,
            "rank_rejected": 0,
            "ai_selected": 0,
            "rejected_type": 0,
            "rejected_feature": 0,
            "rejected_exclusion": 0,
            "rejected_budget": 0,
            "excluded_shown": 0,
            "type_rejections": [],
            "semantic_review_ids": [],
            "feature_review_ids": [],
        }

        if not products:
            report["reason"] = "no_candidates"
            return SearchDecision(
                outcome=Outcome.NO_CANDIDATES, refined=refined,
                request=strategy.request, report=report,
            )

        pool = list(products)
        if exclude_shown:
            before = len(pool)
            pool = retrieval_mod.exclude_shown(pool, state)
            report["excluded_shown"] = before - len(pool)
        report["after_shown"] = len(pool)

        # 1. PRODUCT TYPE — the hard requirement. Incidental mentions
        #    (accessories, spare parts, unrelated products sharing a word)
        #    are rejected here, before ranking, and can never return.
        eligible: list[Product] = []
        rejected_type: list[Product] = []
        for product in pool:
            verdict = validate_product_type(product, product_type)
            if verdict.qualifies:
                eligible.append(product)
                if any(
                    reason in verdict.reasons
                    for reason in (
                        "type_identity_requires_semantic_review",
                        "requested_component_identity_requires_semantic_review",
                        "type_mentioned_only_after_listing_separator",
                    )
                ):
                    report["semantic_review_ids"].append(product.product_id)
            else:
                rejected_type.append(product)
                report["type_rejections"].append(
                    {"id": product.product_id, "reasons": verdict.reasons[:3]}
                )
        report["rejected_type"] = len(rejected_type)
        report["after_type"] = len(eligible)

        # 2. Mandatory positive features need affirmative evidence.
        #    Missing listing evidence is UNKNOWN, not proof of mismatch:
        #    quarantine it for semantic review instead of silently dropping
        #    a potentially suitable product with a sparse AliExpress title.
        verified_features, feature_review = (
            retrieval_mod.partition_required_feature_evidence(eligible, state)
        )
        report["feature_review_ids"] = [
            product.product_id for product in feature_review if product.product_id
        ]
        report["semantic_review_ids"].extend(report["feature_review_ids"])
        eligible = verified_features + feature_review
        report["rejected_feature"] = 0
        report["after_feature"] = len(eligible)

        # 3. Explicit negative and budget constraints are hard filters.
        eligible, rejected_ex = retrieval_mod.filter_by_exclusions(eligible, state)
        report["rejected_exclusion"] = len(rejected_ex)
        report["after_exclusion"] = len(eligible)
        eligible, rejected_budget = retrieval_mod.filter_by_budget(eligible, state)
        report["rejected_budget"] = len(rejected_budget)
        report["after_budget"] = len(eligible)

        # 3. Dedup by stable identity, then requirement-aware ranking.
        eligible = retrieval_mod._dedup_by_identity(eligible)
        report["after_dedup"] = len(eligible)
        ranked, _validations = ranking_mod.rank_candidates(
            eligible, state, product_type, request=strategy.request
        )
        report["ranked"] = len(ranked)
        report["rank_rejected"] = max(0, report["after_dedup"] - len(ranked))
        review_ids = set(report["semantic_review_ids"])
        report["safe_ranked"] = sum(
            1 for product in ranked
            if product.product_id not in review_ids
        )

        # The presentation contract is an absolute 30-card ceiling.
        # An explicit user count may lower it, never raise it; otherwise the
        # no-Gemini / deterministic fallback path could bypass the cap.
        limit = RESULT_CARD_LIMIT
        if strategy.result_limit is not None:
            limit = min(
                RESULT_CARD_LIMIT, max(1, int(strategy.result_limit))
            )
        final = ranked[:limit]

        if final:
            report["reason"] = "results"
            return SearchDecision(
                outcome=Outcome.RESULTS, products=final,
                request=strategy.request, refined=refined, report=report,
            )

        # HONEST no-eligible traceability: which stage actually emptied the
        # pool. The reason names the FIRST stage that produced zero
        # survivors from a non-empty pool, so the log line can never
        # attribute an empty outcome to a stage that still had candidates.
        if ranked is not None and not ranked and not rejected_type and not rejected_ex and not rejected_budget:
            report["reason"] = "no_candidates"
            return SearchDecision(
                outcome=Outcome.NO_CANDIDATES, refined=refined,
                request=strategy.request, report=report,
            )

        if rejected_type:
            report["reason"] = "rejected_by_type_gate"
        elif rejected_ex:
            report["reason"] = "rejected_by_exclusions"
        elif rejected_budget:
            report["reason"] = "rejected_by_budget"
        elif report["excluded_shown"] and not pool:
            report["reason"] = "all_previously_shown"
        else:
            report["reason"] = "no_eligible"
        return SearchDecision(
            outcome=Outcome.NO_ELIGIBLE, refined=refined,
            request=strategy.request, report=report,
        )

    # -- compatibility shim for the previous handler wiring -------------------

    def shape_pool(
        self,
        state: ConversationEngineState,
        products: list[Product],
        product_type: str | None,
        *,
        exclude_shown: bool = False,
    ) -> tuple[list[Product], dict]:
        """Legacy signature kept ONLY until the handler migration lands.

        Delegates to the same validation/ranking the real turn uses; no
        legacy logic lives here.
        """

        eligible: list[Product] = []
        rejected_type = 0
        for product in products:
            verdict = validate_product_type(product, product_type)
            if verdict.qualifies and "type_identity_requires_semantic_review" not in verdict.reasons:
                eligible.append(product)
            else:
                rejected_type += 1
        eligible, rejected_features = retrieval_mod.filter_by_required_features(
            eligible, state
        )
        eligible, rejected_ex = retrieval_mod.filter_by_exclusions(eligible, state)
        eligible, rejected_budget = retrieval_mod.filter_by_budget(eligible, state)
        if exclude_shown:
            eligible = retrieval_mod.exclude_shown(eligible, state)
        eligible = retrieval_mod._dedup_by_identity(eligible)
        ranked, _v = ranking_mod.rank_candidates(eligible, state, product_type)
        report = {
            "raw": len(products),
            "rejected_type": rejected_type,
            "rejected_feature": len(rejected_features),
            "rejected_exclusion": len(rejected_ex),
            "rejected_budget": len(rejected_budget),
            "reason": "ok" if ranked else ("rejected" if products else "no_candidates"),
        }
        return ranked, report
