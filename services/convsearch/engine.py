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
import logging
import re

from models.product import Product
from services.convsearch import ranking as ranking_mod
from services.convsearch import retrieval as retrieval_mod
from services.convsearch.contract import Outcome, SearchDecision
from services.convsearch.state import (
    ConversationEngineState,
    MessageIntent,
)
from services.convsearch.type_validation import validate_product_type

logger = logging.getLogger(__name__)

#: The presentation cap. The engine never pads a smaller eligible pool to
#: reach it, and never exceeds it.
RESULT_CARD_LIMIT = 30
AI_CONVERSATION_SELECTION_LIMIT = 20
AI_CONVERSATION_SELECTION_TIMEOUT_SECONDS = 8.0

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


#: Request-scaffolding and generic-count words that never name a product.
_REQUEST_FILLER: frozenset[str] = frozenset(
    {
        "покажи", "найди", "нужен", "нужна", "нужно", "нужны", "хочу",
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


def _product_type_of(text: str) -> str | None:
    """The product type a NEW-search message names, or ``None``.

    The request's own head-noun reading: content tokens minus request
    scaffolding, budget and quantity words. "Нужна недорогая рыболовная
    катушка" → «рыболовная катушка"; "Покажи 5 вариантов шуруповерта" →
    «шуруповерта".
    """

    from services.convsearch.text import tokens

    words = [
        token for token in tokens(text)
        if token not in _REQUEST_FILLER
        and token not in _DISCOURSE_MARKERS
        and not token.isdigit()
    ]
    if not words:
        return None
    return " ".join(words)


_EXPLICIT_PRODUCT_REQUEST_RE = re.compile(
    r"\b(?:нужен|нужна|нужно|нужны|ищу|хочу|требуется|требуются|"
    r"подбери|подберите|найди|найдите|закажи|закажите|купи|купите)\s+(.+)",
    re.IGNORECASE,
)
_EXPLICIT_PRODUCT_CONTEXT_RE = re.compile(
    r"\b(?:для|с|со|из|без|под|на|до|от|в|по|к|у|о|об)\b",
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
    return " ".join(words) or None


def _is_different_product_type(candidate: str, current: str) -> bool:
    """Compare product identities by stem overlap, not sentence framing."""

    from services.convsearch.text import tokens
    from services.convsearch.type_validation import stems_match

    candidate_tokens = tokens(candidate)
    current_tokens = tokens(current)
    return bool(candidate_tokens and current_tokens) and not any(
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
                "shown_product_count": len(state.shown_product_ids),
                "last_outcome": state.last_outcome,
                "pending_question": state.last_question,
            }
            try:
                ai_turn = await interpreter(text, context=context_payload)
            except Exception:  # noqa: BLE001 - deterministic fallback must survive AI failure
                logger.info("Gemini turn interpretation failed; using deterministic intent")
                ai_turn = None

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
        # An explicit request naming a different item is a product switch
        # even when it also contains prepositional context («насадка на
        # дрель для дерева»). Do not let broad «для/на» framing or a
        # contradictory Gemini action attach it to the previous product.
        explicit_product_type = _explicit_requested_product_type(text)
        current_type = state.active_by_kind("type")
        explicit_product_switch = bool(
            explicit_product_type
            and current_type is not None
            and _is_different_product_type(explicit_product_type, current_type.value)
        )
        if explicit_product_switch:
            intent_override = MessageIntent.NEW_SEARCH

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
            and isinstance(ai_turn, dict)
            and ai_turn.get("action") == "new_search"
        ):
            current_type = state.active_by_kind("type")
            proposed_type = (
                ai_turn.get("product_type")
                if isinstance(ai_turn.get("product_type"), str) else None
            ) or _product_type_of(text)
            if current_type is None:
                # The clarification is filling the missing type in a vague
                # request. Preserve any budget/use-case context already
                # captured before the question.
                intent_override = MessageIntent.REFINE
            elif proposed_type:
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
        )
        if model_slot_updates:
            observation["model_slot_updates"] = model_slot_updates
        state.record_slots(observation)
        intent = observation["intent"]

        if isinstance(ai_turn, dict) and ai_turn.get("action") == "clarify":
            # If the message itself clearly introduced a new product, retain
            # that type even though Gemini needs one more detail before
            # retrieval. Otherwise the user's answer to «for what task?» would
            # have no product type to attach to and could fall back to the
            # previous product's ledger.
            if intent is MessageIntent.NEW_SEARCH:
                clarified_type = explicit_product_type or _product_type_of(text)
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
            new_type = explicit_product_type or model_type or _product_type_of(text) or None
            if new_type:
                state.record_product_type(new_type)
        elif (
            pending_clarification
            and not state.active_by_kind("type")
            and isinstance(model_type, str)
            and model_type.strip()
        ):
            # The answer supplies the missing type without resetting the
            # other slots already recorded from the original request.
            state.record_product_type(model_type.strip())

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

        refined = intent is not MessageIntent.NEW_SEARCH
        try:
            backend_outcome = await self._backend.retrieve(
                strategy, exclude=exclude
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
        decision = await self._apply_ai_candidate_selection(
            decision, strategy, state
        )
        state.last_outcome = decision.outcome.value
        state.last_question = None
        if decision.outcome is Outcome.RESULTS:
            state.record_shown([p.product_id for p in decision.products])
        # One INFO line per turn: the intent, the action and the pool fate -
        # counts only, no user text (§7 traceability).
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
        self, decision: SearchDecision, strategy, state: ConversationEngineState
    ) -> SearchDecision:
        """Use Gemini to rank a bounded slice of already validated listings.

        Deterministic product-type, exclusion, budget and feature checks run
        before this method. Gemini may rank the first 20 candidates, but it
        is not a second hard filter: timeouts, errors, malformed output and
        an empty selection preserve the deterministic safe pool. Candidates
        marked for semantic product-identity review are the exception: they
        remain quarantined unless Gemini explicitly confirms them.

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
        selector_pool = (
            review_pool + ordinary_pool
        )[:AI_CONVERSATION_SELECTION_LIMIT]
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
            decision.products = deterministic_safe_pool()[:presentation_limit]
            decision.report["ai_selection"] = reason
            if not decision.products:
                decision.outcome = Outcome.NO_ELIGIBLE
                feature_review_ids = set(decision.report.get("feature_review_ids", []))
                if feature_review_ids and feature_review_ids.issuperset(semantic_review_ids):
                    decision.report["reason"] = "unverified_required_features"
                else:
                    decision.report["reason"] = "unverified_product_identity"
            return decision

        if not callable(selector):
            if semantic_review_ids:
                return finish_fallback("semantic_review_unavailable")
            return decision

        query_parts = [strategy.request.normalized_query or strategy.request.original_query or ""]
        query_parts.extend(state.active_values("use_case"))
        query_parts.extend(state.active_values("feature"))
        query_parts.extend(state.active_values("preference"))
        exclusions = state.active_values("exclusion")
        if exclusions:
            query_parts.append("ИСКЛЮЧИТЬ: " + "; ".join(exclusions))
        budget = state.active_by_kind("budget")
        if budget is not None:
            query_parts.append(f"ЖЁСТКИЙ БЮДЖЕТ: до {budget.value}")
        query = " | ".join(part.strip() for part in query_parts if part and part.strip())

        try:
            async with asyncio.timeout(AI_CONVERSATION_SELECTION_TIMEOUT_SECONDS):
                selected_ids = await selector(selector_pool, query=query, wanted=wanted)
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

        # Empty/invalid model output is not proof that deterministic
        # candidates are irrelevant. Preserve safe results; only semantic
        # identity-review candidates remain quarantined.
        if not selected:
            return finish_fallback("empty_fallback")

        # The model only sees/selects the first 20. Keep its ordering, then
        # fill the remaining 30-card presentation window from the untouched
        # deterministic tail. Never leak unreviewed semantic-identity rows.
        tail = [
            product for product in original_pool[AI_CONVERSATION_SELECTION_LIMIT:]
            if product.product_id not in semantic_review_ids
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
            "after_shown": 0,
            "ranked": 0,
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
                if "type_identity_requires_semantic_review" in verdict.reasons:
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
        ranked, _validations = ranking_mod.rank_candidates(
            eligible, state, product_type, request=strategy.request
        )
        report["ranked"] = len(ranked)

        limit = strategy.result_limit or RESULT_CARD_LIMIT
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
