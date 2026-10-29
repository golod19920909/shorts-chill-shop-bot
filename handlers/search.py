"""Free-form search handler plus product-card actions.

The search flow is ONE screen:

    user text -> AIService.parse_query -> SearchService.find_products
    -> the FIRST product card (title, price, real marketplace fields)
    -> that very message is edited in place while the user browses

There is deliberately no intermediate list screen: no "Найдено товаров",
no "Страница X", no numbered link list and no «➡️ Показать ещё» page.
The card's «➡️ Следующий» walks the whole cached result set in place and
«◀️ Назад» steps one product back or leaves for the screen the card was
opened from.

Every discovery screen in ``handlers.discovery`` renders through
``send_search_results`` too, so a card behaves identically everywhere.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from uuid import uuid4

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update
from telegram.ext import ContextTypes

from curation.integration import mode_for_feed, mode_for_request
from handlers import common
from keyboards.main import back_to_menu_keyboard
from keyboards.products import (
    CB_PREFIX_FAVORITE,
    CB_PREFIX_TRACK,
    browse_card_keyboard,
)
from models.search_request import SearchMode, SearchRequest
from services import analytics
from services.conversation import ConversationState, is_count_only_follow_up
from services.product_identity import identity_pair
from services.text_matching import (
    extract_requested_result_count,
    strip_option_count as _strip_option_count,
)
from services.product_logging import log_final_pool
from services.search_service import AI_FEED_RESULTS, SearchOutcome, SearchService
from utils.formatting import CAPTION_LIMIT

from services.convsearch.contract import Outcome as ConvOutcome
from services.convsearch.engine import ConversationSearchEngine
from services.convsearch.state import ConversationEngineState

logger = logging.getLogger(__name__)

#: The conversational-search engine (adapted from
#: anything-conversational-search): the SOLE search decision-maker. One
#: instance per process; per-user state lives inside it (keyed by Telegram
#: user id, expiring like :data:`services.convsearch.state.CONTEXT_TTL_SECONDS`).
_CONV_ENGINE = ConversationSearchEngine()


def get_conversation_engine() -> ConversationSearchEngine:
    """The process-level conversational search engine."""

    return _CONV_ENGINE


def reset_conversation_engine(user_key: str) -> None:
    """Forget one user's conversational-search ledger (used by /new)."""

    _CONV_ENGINE.reset(user_key)

SEARCHING_TEXT = "🔎 Обрабатываю запрос и ищу товары…"

WELCOME_SMART_SEARCH = (
    "Привет! Я умный помощник Shorts Chill.\n\n"
    "Расскажите, что вы ищете — можете описать своими словами, "
    "указать бюджет, исключения или просто задать вопрос.\n\n"
    "Я подберу варианты, сравню их и помогу выбрать."
)

#: The ONLY text a user ever sees when nothing relevant was found. Reasons
#: and diagnostics (unavailable sources, API failures, dropped candidates)
#: belong to the internal log - the user gets a short, honest sentence and
#: the advice that actually helps: change the query or its parameters.
EMPTY_RESULT_TEXT = (
    "😕 По вашему запросу ничего не найдено. "
    "Попробуйте изменить запрос или параметры поиска."
)

#: The ONLY text a user ever sees when the search itself could not run: no
#: provider is configured or every provider failed before returning usable
#: results. It must never claim "nothing exists" - the search never
#: completed, so there is nothing to report about the catalogue.
SEARCH_UNAVAILABLE_TEXT = (
    "⚠️ Поиск временно недоступен. Попробуйте позже."
)

#: The ONLY text a user ever sees when a COMPARE/EXPLAIN/ANSWER question
#: cannot be answered because the previously shown products are gone (the
#: cache is empty, expired, evicted, or lost after a restart). It never
#: invents products, never claims a comparison happened, and never turns
#: the question into a meaningless provider search - it asks for a new
#: search whose results the bot can then compare again.
STALE_RESULTS_TEXT = (
    "😕 Предыдущие результаты уже недоступны. "
    "Выполните новый поиск, и я помогу сравнить товары."
)

#: `user_data` key holding the last effective :class:`SearchRequest`, so a
#: follow-up message can refine it (see ``services.conversation``). It is
#: per-user, in memory and expires on its own - no database change.
CONVERSATION_KEY = "search_conversation"

#: `user_data` key holding a PENDING clarifying question: the engine asked
#: "what product, for what task?" and the next message is the user's ANSWER,
#: which is combined with the original vague request instead of searched on
#: its own. Stored as ``{"question": str, "asked_at": float, "original": str}``;
#: it expires after :data:`handlers.search.CLARIFY_TTL_SECONDS`, an
#: explicit cancel drops it, and a search attempt CONSUMES it only once the
#: search completes - a failed search keeps the thread so the clarification
#: is not silently lost. Per-user, in memory - no database change.
PENDING_CLARIFY_KEY = "pending_clarify"

#: Discovery screen headline. The pipeline only reorders/drops REAL
#: marketplace listings, and the text does not claim "popular"/"best".
#: The card names the FEED only - no counter, no ranking explanation.
DISCOVERY_HEADER = "🎲 <b>Интересное</b>"

#: Mood markers: a message that asks for "something interesting".
_MOOD_MARKERS = (
    "прикольн", "необычн", "интересн", "странн", "удиви", "забавн",
    "что-нибудь", "что нибудь",
)

#: Markers that are ALWAYS a discovery request, whatever else is in the text.
_DISCOVERY_STRICT_MARKERS = (
    "удиви меня", "surprise me", "на твой вкус", "на ваш вкус", "не знаю что",
)

#: Wording that names an EXACT product, so relevance must stay the priority
#: even if the sentence also carries a mood word ("интересные наушники").
#: A budget or a review phrase is NOT in this list: "что-нибудь необычное до
#: $20" is a discovery request WITH a constraint, and the constraint is kept
#: (see ``SearchService.find_discovery``) instead of discarding the mood.
_EXACT_PRODUCT_MARKERS = (
    "наушник", "чехол", "видеорегистратор", "клавиатур", "мышк", "кабель",
    "зарядк", "power bank", "чехлы", "футляр",
)

def is_discovery_query(text: str) -> bool:
    """Whether a message asks for "something interesting" (discovery mode).

    A mood word makes it a discovery request even when a budget is attached -
    the budget is a constraint, not a product name, and the discovery pipeline
    applies it to the real candidates. A message that names a concrete
    product stays an ordinary search so relevance is never traded away.
    """

    lowered = (text or "").lower()
    if any(marker in lowered for marker in _DISCOVERY_STRICT_MARKERS):
        return True
    if any(marker in lowered for marker in _EXACT_PRODUCT_MARKERS):
        return False
    return any(marker in lowered for marker in _MOOD_MARKERS)

def get_conversation(context: ContextTypes.DEFAULT_TYPE):
    """The stored conversation context, or ``None`` when absent/expired."""

    state = context.user_data.get(CONVERSATION_KEY)
    if not isinstance(state, ConversationState):
        return None
    if state.is_expired():
        context.user_data.pop(CONVERSATION_KEY, None)
        logger.info("Conversation context expired and was dropped")
        return None
    return state

def _last_shown_session(context: ContextTypes.DEFAULT_TYPE) -> str | None:
    """The session id of the result set currently on screen, if any.

    A COMPARE/EXPLAIN/ANSWER message ("какой лучше?", "расскажи про второй")
    only makes sense over products the user can actually see, so the router is
    told whether a real, still-cached result set exists. The id is stored when
    results are rendered and lives in ``user_data`` - per user, in memory.
    """

    session_id = context.user_data.get("last_shown_session")
    if not isinstance(session_id, str) or not session_id:
        return None
    return session_id if common.get_cached_outcome(context, session_id) else None

def reset_conversation(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Forget the current shopping context (search, results, pending question).

    Used by the ``/new`` command and by a spoken "начни заново". It only drops
    in-memory per-user keys: nothing in the database or another user's state is
    touched, and the search pipeline itself is unchanged.
    """

    # The conversational-search ledger is dropped; the shown-session cache
    # and the pending clarification follow.
    context.user_data.pop("_convsearch_state", None)
    for key in (CONVERSATION_KEY, PENDING_CLARIFY_KEY, "last_shown_session"):
        context.user_data.pop(key, None)

#: How long a pending clarifying question stays answerable (mirrors the
#: conversation TTL: half an hour).
CLARIFY_TTL_SECONDS = 30 * 60

def get_pending_clarify(context: ContextTypes.DEFAULT_TYPE) -> dict | None:
    """The pending clarifying question, or ``None`` when absent/expired."""

    pending = context.user_data.get(PENDING_CLARIFY_KEY)
    if not isinstance(pending, dict):
        return None
    asked_at = pending.get("asked_at")
    if not isinstance(asked_at, (int, float)):
        context.user_data.pop(PENDING_CLARIFY_KEY, None)
        return None
    if time.monotonic() - asked_at > CLARIFY_TTL_SECONDS:
        context.user_data.pop(PENDING_CLARIFY_KEY, None)
        logger.info("Pending clarifying question expired and was dropped")
        return None
    return pending

def set_pending_clarify(context: ContextTypes.DEFAULT_TYPE, question: str, original: str) -> None:
    """Remember that we asked ``question`` about the vague ``original`` request."""

    context.user_data[PENDING_CLARIFY_KEY] = {
        "question": question,
        "original": original,
        "asked_at": time.monotonic(),
    }

# --- narrowing the SHOWN results («оставь 5 из этих 30») -------------------

#: Function words a narrowing request may carry. Local to this module so the
#: narrowing guard does not depend on any legacy vocabulary module.
_NARROW_STOPWORDS = frozenset(
    {
        "и", "а", "но", "или", "для", "с", "со", "в", "на", "по", "за", "из",
        "до", "от", "что", "как", "это", "мне", "меня", "я", "они", "он",
        "она", "the", "a", "an", "and", "or", "for", "of", "from", "to",
        "in", "on", "with", "please", "me", "my", "only",
    }
)

#: Verbs that ask to KEEP/SELECT a subset of what is already on screen.
#: Matched as substrings of the lowered message; they always travel WITH a
#: number, so an ordinary search never matches them.
_NARROW_SELECT_MARKERS = (
    "оставь", "оставить", "выбери", "выберите", "покажи только",
    "покажите только", "хочу только", "keep only", "show only",
)

#: Phrases that explicitly reference the SHOWN result set («из этих 30»).
#: When one is present with a select verb and there is NO cached result set,
#: the bot answers honestly that the previous results are gone instead of
#: silently searching the message as new keywords.
_NARROW_SET_REFERENCE_MARKERS = (
    "из этих", "из них", "из показанных", "из списка", "из результатов",
    "из найденных", "из прошлого поиска", "of these", "from these",
    "из предыдущих",
)

#: Criterion wording the bot CAN honour from real product data: a price.
#: Anything else («самых надёжных», «популярных») is not verifiable from the
#: card fields, so the request falls through to the normal pipeline instead
#: of pretending a selection happened.
_NARROW_CHEAPEST_MARKERS = ("дешев", "дешёв")

#: Criterion wording the bot CANNOT honour (no such field on a card):
#: a request carrying one of these is NOT handled as a narrowing.
_NARROW_UNSUPPORTED_MARKERS = (
    "надёжн", "надежн", "качеств", "красив", "популярн", "рейтинг",
    "дорог", "прочн", "быстр", "лёгк", "легк", "компакт",
)

#: Filler words a narrowing request may legitimately carry besides the
#: verb, the number and the criterion. Any OTHER content word (a product
#: name like «наушники», a feature) means the message is an ordinary search
#: or refinement and must NOT be intercepted.
_NARROW_FILLER_WORDS = frozenset(
    {
        "штук", "шт", "штуки", "вариант", "варианта", "вариантов", "варианты",
        "товар", "товара", "товаров", "товары", "лучших", "лучше", "самых",
        "самые", "самый", "дешевых", "дешёвых", "дешевые", "дешёвые", "дешевых",
        "дешевые", "из", "эти", "этих", "них", "the", "of", "from", "these",
        "them", "only", "please", "me", "my", "все",
    }
)

def _parse_narrow_request(text: str) -> tuple[int, bool, bool] | None:
    """Read a «keep N of the shown ones» request, or ``None``.

    Returns ``(count, cheapest, explicit_reference)``. The FIRST number of
    the message is the requested count («оставь 5 штук из этих 30» -> 5,
    not 30); ``cheapest`` is True only when the user named a price
    criterion the card data can actually honour; ``explicit_reference``
    says the message points at the shown set («из этих»), which decides
    the honest expired-results answer when nothing is cached.
    """

    lowered = (text or "").lower().replace("ё", "е")
    if not any(marker in lowered for marker in _NARROW_SELECT_MARKERS):
        return None
    if any(marker in lowered for marker in _NARROW_UNSUPPORTED_MARKERS):
        return None  # not verifiable from card data - never fake it
    number = re.search(r"\d+", lowered)
    if number is None:
        return None
    count = int(number.group(0))
    if count < 1:
        return None
    cheapest = any(marker in lowered for marker in _NARROW_CHEAPEST_MARKERS)
    reference = any(marker in lowered for marker in _NARROW_SET_REFERENCE_MARKERS)
    # Content guard: after removing the select markers, any remaining real
    # content word (a product name, a feature) means this is an ordinary
    # search/refinement carrying a number («покажи только беспроводные
    # наушники до 5») - it must go to the normal pipeline, never be read
    # as a narrowing of the shown set.
    remaining = lowered
    for marker in _NARROW_SELECT_MARKERS + _NARROW_SET_REFERENCE_MARKERS:
        remaining = remaining.replace(marker, " ")
    leftover = [
        word
        for word in re.findall(r"[^\W\d_]+", remaining)
        if word not in _NARROW_FILLER_WORDS
        and word not in _NARROW_STOPWORDS
        and len(word) >= 3
        and not any(word.startswith(m[:5]) for m in _NARROW_CHEAPEST_MARKERS)
    ]
    if leftover:
        return None
    return min(count, 100), cheapest, reference

def _narrow_follow_up_text(
    selected: list[Product], *, total: int, cheapest: bool, wanted: int
) -> str:
    """The honest one-line summary AFTER the selected cards."""

    count = len(selected)
    if cheapest:
        action = f"Оставил {count} самых дешёвых"
    else:
        action = f"Оставил {count} из {total}"
    if count < wanted:
        action += f" (столько подходящих нашлось)"
    return f"{action}. Листайте карточки кнопкой «➡️». Могу сравнить их или сузить выбор ещё."

async def _narrow_shown_results(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
) -> bool:
    """Select a subset of the SHOWN products and re-deliver them as cards.

    The products come from the cached result set of THIS user's last search
    (the same ``Product`` objects the cards above rendered), so no new
    marketplace call happens and nothing is invented. The selected objects
    go through the EXISTING ``send_search_results`` card path (they already
    passed the curation gate when first shown, so the gate is not re-run -
    re-curating would hide products the user is looking at right now).
    Returns ``True`` when the message was fully handled.
    """

    parsed = _parse_narrow_request(text)
    if parsed is None:
        return False
    count, cheapest, explicit_reference = parsed

    message = update.effective_message
    session_id = _last_shown_session(context)
    outcome = (
        common.get_cached_outcome(context, session_id) if session_id else None
    )
    products = list(outcome.products) if outcome is not None else []
    if not products:
        # Nothing to narrow: only a message that EXPLICITLY references the
        # shown set («из этих 30») gets the honest stale answer - anything
        # else may be an ordinary request and stays on the normal path.
        if not explicit_reference:
            return False
        await message.reply_text(
            STALE_RESULTS_TEXT, reply_markup=back_to_menu_keyboard()
        )
        return True

    # The selection uses ONLY real fields of the already-shown products.
    # Cheapest: sort by real price (products without a price cannot be
    # verified as cheap and are never claimed as such). No criterion: the
    # existing pipeline ranking order is kept as is.
    if cheapest:
        priced = [p for p in products if p.price is not None]
        selected = sorted(priced, key=lambda p: p.price)[:count]
    else:
        selected = products[:count]
    if not selected:
        await message.reply_text(
            EMPTY_RESULT_TEXT, reply_markup=back_to_menu_keyboard()
        )
        return True

    narrowed = SearchOutcome(request=outcome.request, products=selected)
    user_id = await common.register_user(update, context)
    telegram_id = update.effective_user.id if update.effective_user else None
    shown = await send_search_results(
        context,
        message.chat_id,
        narrowed,
        user_id=user_id,
        telegram_id=telegram_id,
        pre_curated=True,
        follow_up=False,
    )
    # The narrow path sends its OWN honest summary already; the generic
    # count hint would only duplicate it, so it is skipped here.
    if shown:
        context.user_data["last_shown_session"] = shown
        follow_up = _narrow_follow_up_text(
            selected, total=len(products), cheapest=cheapest, wanted=count
        )
        await context.bot.send_message(
            message.chat_id,
            follow_up,
            reply_markup=back_to_menu_keyboard(),
        )
    return True

async def _answer_over_shown(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    question: str,
    session_id: str,
) -> bool:
    """Answer a COMPARE/EXPLAIN/ANSWER question over the SHOWN products.

    Reuses the SAME ``ProductAssistant`` the 🤖 menu uses, over the SAME cached
    result set - so the answer is built only from real product fields and the
    assistant says "нет данных" instead of inventing anything. Returns ``False``
    when there is nothing to answer over, so the caller can fall back to a
    normal search rather than showing an empty screen.
    """

    assistant = context.application.bot_data.get("product_assistant")
    products = common.get_cached_products(context, session_id)
    if assistant is None or not products:
        return False

    advice = await assistant.answer(question, products[:8])
    if advice is None or not getattr(advice, "text", "").strip():
        return False
    await context.bot.send_message(
        chat_id, advice.text, parse_mode="HTML", reply_markup=back_to_menu_keyboard()
    )
    return True

def _apply_user_result_count(
    request: SearchRequest,
    user_text: str | None,
    *,
    result_limit: int | None = None,
) -> SearchRequest:
    """Attach the count the USER asked for («покажи 5»), on every path.

    The requested number of cards is a deterministic reading of the user's own
    wording - never model data - so no search path may drop it. ``result_limit``
    is the value already read from the ORIGINAL message, so a step that
    rewrote the text (a clarifying answer) cannot lose it.
    """

    if result_limit is not None:
        count = result_limit
    elif user_text:
        count = extract_requested_result_count(user_text)
    else:
        return request
    if count is None or count == request.result_limit:
        return request
    return request.model_copy(update={"result_limit": count})

async def search_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle any non-command text message as a product search query."""

    # One wall-clock stamp for the whole turn: every exit path logs how long
    # the user actually waited, next to the per-stage timings the search
    # pipeline already reports (search.rid / Search outcome). Counts only -
    # no message text, no personal data.
    turned_at = time.perf_counter()

    def _turn_done(route: str) -> None:
        logger.info(
            "turn.route=%s total_ms=%.0f",
            route,
            (time.perf_counter() - turned_at) * 1000,
        )

    message = update.effective_message
    if message is None or not message.text:
        return

    text = message.text.strip()
    if not text or text.startswith("/"):
        return
    original_text = text

    # A pending clarifying question is answered FIRST: the next message is
    # the user's ANSWER to what the engine asked, combined with the original
    # vague request instead of searched on its own. The answer is fed back
    # through the same engine turn as "original + answer" so the ledger
    # records the full statement. An explicit cancel («отмена») drops it.
    pending = get_pending_clarify(context)
    if pending is not None:
        lowered = text.lower()
        if lowered in {"отмена", "отменить", "cancel", "забудь", "не надо"}:
            context.user_data.pop(PENDING_CLARIFY_KEY, None)
            await message.reply_text(
                "Хорошо, отменил. Скажите, что искать.",
                reply_markup=back_to_menu_keyboard(),
            )
            _turn_done("clarify-cancelled")
            return
        context.user_data.pop(PENDING_CLARIFY_KEY, None)
        text = f"{pending.get('original') or ''} {text}".strip()
        original_text = text

    # A question asked through «💬 Задать свой вопрос» is consumed here, so
    # it never becomes a search. The state is popped by the consumer, and
    # the import is local to avoid a module cycle (ai_assistant reuses
    # ``send_search_results`` from this module).
    from handlers import ai_assistant

    if await ai_assistant.consume_pending_question(update, context):
        return

    # A «оставь 5 из этих 30» message narrows the SHOWN result set: it is
    # deterministic, costs no model call and must never degrade into a new
    # marketplace search or a prose-only reply. Everything else continues
    # exactly as below.
    if await _narrow_shown_results(update, context, text):
        _turn_done("narrow")
        return
    # A count-only («покажи 5») or currency-only («используй гривны») follow-up
    # is answered from the products ALREADY on screen: no model call and
    # no marketplace request, and the current search context (product,
    # filters, budget, currency) is kept. Anything else falls through to the
    # conversational engine. A count-only message with no cached result set
    # has nothing to apply the count to: it is answered honestly instead of
    # being searched as the literal phrase.
    if (
        is_count_only_follow_up(text) is not None
        and _has_no_shown_results(context)
        and not extract_requested_result_count(_strip_option_count(text) or "")
    ):
        await message.reply_text(
            STALE_RESULTS_TEXT, reply_markup=back_to_menu_keyboard()
        )
        _turn_done("count-no-context")
        return
    if await _answer_from_shown(update, context, text):
        _turn_done("cache")
        return

    # Read from the message the USER sent, before any routing rewrote it.
    original_count = extract_requested_result_count(original_text)

    # THE ENGINE TURN — the one search decision-maker. The engine builds
    # the retrieval strategy from the conversation ledger, retrieves through
    # the explicit AliExpress adapter, validates every candidate against
    # the product type and the active constraints, ranks the eligible pool
    # and returns one ``SearchDecision``. There is NO fallback to a legacy
    # pipeline: a provider error stays a provider error, and an engine
    # failure surfaces honestly. The count-only / currency-only follow-ups
    # below still answer from the SHOWN cache — they touch no search
    # decision, so they stay.
    engine = get_conversation_engine()
    engine.bind_search_service(common.get_search_service(context))
    engine.bind_ai_service(common.get_ai_service(context))
    user_id = await common.register_user(update, context)
    telegram_id = update.effective_user.id if update.effective_user else None
    region = await common.get_user_region(context, user_id)
    user_key = str(telegram_id or user_id or "anon")

    # The per-user conversation ledger lives in the bot's EXISTING
    # ``context.user_data`` store (per-user isolation and expiry follow
    # the architecture the bot already trusts) — no parallel state store.
    conv_state = context.user_data.get("_convsearch_state")
    if not isinstance(conv_state, ConversationEngineState):
        conv_state = ConversationEngineState(user_key=user_key)
    conv_state.touch()

    await common.record_event(
        context,
        analytics.EVENT_SEARCH_STARTED,
        user_id=user_id,
        telegram_id=telegram_id,
        metadata={"source": "text"},
    )

    seen = await common.seen_products(context, user_id)
    await message.reply_text(SEARCHING_TEXT)

    try:
        decision = await engine.handle_message(
            user_key,
            text,
            exclude=seen,
            result_limit=original_count,
            currency=region.get("currency"),
            state=conv_state,
        )
    except Exception:
        logger.exception("Convsearch engine turn failed")
        await common.record_event(
            context,
            analytics.EVENT_SEARCH_FAILED,
            user_id=user_id,
            telegram_id=telegram_id,
            metadata={"source": "text", "reason": "engine_error"},
        )
        await message.reply_text(
            SEARCH_UNAVAILABLE_TEXT, reply_markup=back_to_menu_keyboard()
        )
        _turn_done("engine-error")
        return

    # D1 (state consistency): the request the engine built is committed
    # BEFORE the outcome decides what ships, so a provider outage must not
    # erase what the user just asked for — a later refinement (budget,
    # feature, count) refines THIS request.
    if decision.request is not None:
        context.user_data[CONVERSATION_KEY] = ConversationState(
            request=decision.request, welcomed=True
        )
    context.user_data["_convsearch_state"] = conv_state

    await _deliver_decision(
        context, message, decision,
        user_id=user_id, telegram_id=telegram_id, original_text=original_text,
    )
    _turn_done(f"engine-{decision.outcome.value}")


async def _deliver_decision(
    context: ContextTypes.DEFAULT_TYPE,
    message,
    decision,
    *,
    user_id,
    telegram_id,
    original_text: str,
) -> None:
    """Render ONE ``SearchDecision``. No re-search, no re-filtering."""

    if decision.outcome is ConvOutcome.CLARIFY:
        question = (
            decision.question
            or "Чтобы подобрать точнее: какой товар вас интересует?"
        )
        set_pending_clarify(context, question, original_text)
        await context.bot.send_message(
            message.chat_id, question, reply_markup=back_to_menu_keyboard()
        )
        return

    if decision.outcome is ConvOutcome.COMPARE:
        session_id = _last_shown_session(context)
        if session_id is not None:
            answered = await _answer_over_shown(
                context, message.chat_id, original_text, session_id
            )
            if answered:
                return
        await context.bot.send_message(
            message.chat_id, STALE_RESULTS_TEXT,
            reply_markup=back_to_menu_keyboard(),
        )
        return

    if decision.outcome is ConvOutcome.PROVIDER_ERROR:
        await common.record_event(
            context,
            analytics.EVENT_SEARCH_FAILED,
            user_id=user_id,
            telegram_id=telegram_id,
            metadata={"source": "text", "reason": "provider_error"},
        )
        await context.bot.send_message(
            message.chat_id, SEARCH_UNAVAILABLE_TEXT,
            reply_markup=back_to_menu_keyboard(),
        )
        return

    if decision.outcome in (ConvOutcome.NO_CANDIDATES, ConvOutcome.NO_ELIGIBLE):
        reason = (
            "empty" if decision.outcome is ConvOutcome.NO_CANDIDATES
            else "no_eligible"
        )
        await common.record_event(
            context,
            analytics.EVENT_SEARCH_FAILED,
            user_id=user_id,
            telegram_id=telegram_id,
            metadata={"source": "text", "reason": reason},
        )
        await context.bot.send_message(
            message.chat_id, EMPTY_RESULT_TEXT,
            reply_markup=back_to_menu_keyboard(),
        )
        return

    if decision.outcome is not ConvOutcome.RESULTS or not decision.products:
        await context.bot.send_message(
            message.chat_id, EMPTY_RESULT_TEXT,
            reply_markup=back_to_menu_keyboard(),
        )
        return

    # RESULTS: the engine already validated, filtered, deduped and ranked
    # these products; the cards carry the SAME Product objects (their real
    # affiliate URLs, prices, currencies). The curation gate is skipped
    # (``pre_curated=True``): no hidden stage may reintroduce rejected
    # candidates or reorder the engine's decision.
    outcome = SearchOutcome(
        request=decision.request or SearchRequest(original_query=original_text),
        products=list(decision.products),
    )

    if user_id is not None:
        try:
            await common.get_database(context).add_search_history(
                user_id=user_id,
                original_query=original_text,
                search_request_json=outcome.request.model_dump(mode="json"),
            )
        except Exception:
            logger.exception("Failed to store search history for user %s", user_id)

    await record_outcome_events(
        context,
        outcome,
        user_id=user_id,
        telegram_id=telegram_id,
        source="category",
    )

    shown = await send_search_results(
        context, message.chat_id, outcome,
        user_id=user_id, telegram_id=telegram_id, pre_curated=True,
    )
    if shown:
        context.user_data["last_shown_session"] = shown

# --- product card rendering ----------------------------------------------

def _result_summary_text(outcome: SearchOutcome) -> str | None:
    """The honest one-line summary AFTER the delivered product cards.

    The only facts used are the REAL counts of the delivered outcome: how
    many cards the user can browse. When two or more products were
    delivered, comparing them or looking for cheaper ones is a real offer;
    with a single product only the browsing hint is honest. Nothing about
    specific products, prices or ratings is ever written here - the cards
    above already show them, and inventing prose would risk contradicting
    them.
    """

    count = len(outcome.products)
    if count > 1:
        return (
            f"Нашёл {count} вариантов — листайте карточки кнопкой «➡️». "
            "Могу сравнить их или поискать подешевле."
        )
    if count == 1:
        return "Нашёл один подходящий вариант — листать нечего, но могу поискать ещё."
    return None


async def record_outcome_events(
    context: ContextTypes.DEFAULT_TYPE,
    outcome: SearchOutcome,
    *,
    user_id: int | None,
    telegram_id: int | None,
    source: str,
) -> None:
    """Record the outcome of one search/feed as analytics events.

    Only counts are stored, never the query text: the user's own queries
    live in ``search_history``, scoped to that user (see
    ``services/analytics.py``).
    """

    if outcome.products:
        await common.record_event(
            context,
            analytics.EVENT_SEARCH_SUCCESS,
            user_id=user_id,
            telegram_id=telegram_id,
            metadata={
                "source": source,
                "found": outcome.found_count,
                "raw": outcome.products_before_filtering,
            },
        )
        await common.record_event(
            context,
            analytics.EVENT_PRODUCTS_SHOWN,
            user_id=user_id,
            telegram_id=telegram_id,
            metadata={"source": source, "count": len(outcome.visible_products)},
        )
        return

    await common.record_event(
        context,
        analytics.EVENT_SEARCH_FAILED,
        user_id=user_id,
        telegram_id=telegram_id,
        metadata={
            "source": source,
            "reason": "empty",
            "unavailable": len(outcome.unavailable_marketplaces),
            "failed": len(outcome.failed_marketplaces),
        },
    )


def _card_headline(outcome: SearchOutcome, header: str | None) -> str:
    """The context line above a product card - a FEED name, never a query.

    A discovery/category screen supplies its own feed name («🔥 Популярное»,
    «📂 Электроника»); a plain text search supplies NO headline at all: the
    user already knows what they searched for, so the card starts directly
    with the product information. No query echo, no counter («Найдено
    товаров: 12»), no position («1/5»), no page marker and no ranking
    explanation is ever rendered here.
    """

    return header or ""

def _log_unapplied_constraints(outcome: SearchOutcome) -> None:
    """Record constraints the pipeline could not apply - in the LOG only.

    Two facts are real but are the bot's own plumbing, not the user's
    product question:

    * a rating bound that cannot be honoured, because the marketplace
      reports no 0-5 star rating at all (AliExpress ``evaluate_rate`` is a
      share of positive feedback, which is deliberately NOT converted into
      stars);
    * an inclusion feature written in a language the result set does not
      contain (see ``services.text_matching.can_ever_match``).

    Neither of them is ever shown on a screen: the final spec forbids
    exposing API limitations, filter diagnostics or internal reasons to
    the user, so the results are returned unfiltered, the fact is logged,
    and the card stays clean (see ``_card_caption``).
    """

    request = outcome.request

    if request.min_rating is not None and not request.rating_filter_applicable:
        logger.info(
            "Rating bound %.1f was not applied (no candidate carries a star "
            "rating); the results were NOT filtered on it",
            request.min_rating,
        )

    if request.ignored_features:
        logger.info(
            "Inclusion features %s were not applied (the result set holds no "
            "text in the same language); the products were NOT filtered on them",
            request.ignored_features,
        )

def _curation_mode(outcome: SearchOutcome, track_shown: bool) -> str:
    """Which curation mode THIS feed is (see ``curation.integration``).

    A rotation session («🔥 Популярное») is the popular mode - and its discount
    cycle passes its own mode explicitly, because only the rotation knows which
    step asked the marketplace for discounts. Everything else is the API's own
    relevance order, unless the request itself asked for the marketplace's
    documented discount sort, in which case the feed is a deals one.
    """

    if track_shown:
        return mode_for_feed(track_shown=True)
    return mode_for_request(outcome.request)

def _is_total_provider_failure(outcome: SearchOutcome) -> bool:
    """Whether NO provider completed the search behind ``outcome``.

    Total failure means the catalogue was never actually consulted: no
    provider answered (``searched_marketplaces`` is empty) while at least
    one reports as failed or unconfigured. Every other empty shape - zero
    rows answered, all candidates filtered, one provider down while another
    answered - is a COMPLETED search and keeps the honest "nothing found"
    message, never the outage one.
    """

    if outcome.searched_marketplaces:
        return False
    return bool(outcome.failed_marketplaces or outcome.unavailable_marketplaces)

async def send_search_results(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    outcome: SearchOutcome,
    *,
    header: str | None = None,
    wrap_up_keyboard: InlineKeyboardMarkup | None = None,
    back_callback: str | None = None,
    user_id: int | None = None,
    telegram_id: int | None = None,
    track_shown: bool = False,
    curation_mode: str | None = None,
    pre_curated: bool = False,
    follow_up: bool = True,
) -> str | None:
    """Open a result set ON the first product card.

    Every discovery flow (text search, discovery, category, popular, hot
    deals, promo campaign, photo) ends here, and this function always
    materializes exactly ONE message: the product card of the first
    result. There is no summary, no counter and no product list - the
    card's «➡️ Следующий» walks the whole deduplicated result set by
    editing that very message.

    ``header`` names the feed ("🔥 Популярное", a category title, the
    query); it is already-safe HTML and is never escaped here.
    ``wrap_up_keyboard`` is kept in the session cache for the screens that
    need their own way back, and ``back_callback`` is the callback data of
    the screen the card was opened from, so «◀️ Назад» on the first card
    returns there instead of jumping to the main menu.
    ``curation_mode`` overrides the mode the curation layer uses for the
    result set (the popular rotation passes its own, because the rotation
    knows whether this step is the marketplace's discount cycle); without
    it the mode follows the flow.
    ``pre_curated=True`` skips ONLY the curation gate: the caller (the
    «🔥 Популярное» flow, G23) already ran its own unified pipeline over
    this list, and running a second curation would both double-filter and
    lose the stage diagnostics the caller reported. History recording,
    caching and rendering below stay exactly as for every other flow.
    Returns the session id, or ``None`` when nothing was shown.
    """

    # THE CURATION GATE (step 3). Every feed of the bot passes here, so this is
    # the one place where the raw marketplace pool is turned into the list the
    # user will actually walk: the per-user view history is read, the pipeline
    # (quality -> family dedup -> novelty -> this flow's mode -> type diversity)
    # runs over it, and the survivors are what gets cached and rendered. Nothing
    # is invented: listings are only dropped or reordered, and the very same
    # objects keep flowing to the cards below. The popular rotation passes
    # ``pre_curated`` because its pipeline already includes its OWN history
    # stage (the rotation's shown-scope) and stage counters (G23).
    if not pre_curated:
        outcome.products = await common.curate_products(
            context,
            outcome.products,
            user_id=user_id,
            mode=curation_mode or _curation_mode(outcome, track_shown),
            # G24.8 P1.1: the request travels to the wiring layer, which
            # reads it for MODE_CATEGORY only (the relevance map of the
            # category ranker); every other mode ignores it.
            request=outcome.request,
        )

    if not outcome.products:
        # Technical reasons (missing keys, a failing source, how many
        # candidates the gate dropped) are for the log only: the chat must
        # never explain the bot's internals to the user.
        if outcome.unavailable_marketplaces or outcome.failed_marketplaces:
            logger.info(
                "Empty result for %r (unavailable=%s, failed=%s, "
                "dropped_as_irrelevant=%d, hidden_as_seen=%d)",
                outcome.request.original_query,
                outcome.unavailable_marketplaces or "none",
                outcome.failed_marketplaces or "none",
                outcome.products_dropped_as_irrelevant,
                outcome.products_hidden_as_seen,
            )
        if _is_total_provider_failure(outcome):
            # The search never completed: no provider answered, so claiming
            # "nothing exists" would be a lie. Every other empty shape -
            # zero rows answered, all filtered, partial answers - keeps the
            # honest "nothing found" message below.
            await context.bot.send_message(
                chat_id,
                SEARCH_UNAVAILABLE_TEXT,
                reply_markup=back_to_menu_keyboard(),
            )
            return None
        await context.bot.send_message(
            chat_id, EMPTY_RESULT_TEXT, reply_markup=back_to_menu_keyboard()
        )
        return None

    session_id = common.remember_outcome(
        context,
        outcome,
        header=header,
        wrap_up_keyboard=wrap_up_keyboard,
        back_callback=back_callback,
        track_shown=track_shown,
    )
    _log_unapplied_constraints(outcome)
    # One INFO block per opened screen: the exact ordered list Telegram is
    # about to render (rank, title, price/currency, product id) - after the
    # curation gate, so this is precisely what the user will walk. Counts
    # only would not answer "WHICH products were shown".
    log_final_pool(
        logger,
        outcome.products,
        context=f"telegram send ({outcome.request.mode.value})",
        origin="telegram-sent",
        limit=AI_FEED_RESULTS,
    )
    await _send_product_cards(
        context,
        chat_id,
        outcome,
        session_id,
        user_id=user_id,
        telegram_id=telegram_id,
    )
    # The hint AFTER the cards states the real delivered count and points at
    # the existing «➡️» browsing. No new products, prices or links are
    # invented here - only the count actually delivered above. A caller with
    # its own honest summary (the narrowing flow) passes ``follow_up=False``
    # so the two summaries are never doubled.
    if follow_up:
        hint = _result_summary_text(outcome)
        if hint:
            await context.bot.send_message(
                chat_id, hint, reply_markup=back_to_menu_keyboard()
            )
    return session_id

def _browse_keyboard(
    context: ContextTypes.DEFAULT_TYPE,
    outcome: SearchOutcome,
    session_id: str,
    index: int,
    tracked: bool = False,
) -> InlineKeyboardMarkup:
    """Keyboard of the one-card browser over the whole result set."""

    products = outcome.products
    product = products[index]
    back_callback = None
    meta = common.get_outcome_meta(context, session_id)
    back_screen = meta.get("back_callback")
    if isinstance(back_screen, str):
        back_callback = back_screen
    return browse_card_keyboard(
        session_id,
        index,
        len(products),
        product,
        back_callback=back_callback,
        tracking=tracked,
    )

def _card_caption(outcome: SearchOutcome, _index: int, header: str | None) -> str:
    """Text above a product card: the feed line (when the screen has one).

    A plain search card has NO line above the product - the user already
    knows what they searched for, and the query must never be echoed back.

    ``header`` is already HTML markup built by the caller (the discovery
    screens put ``<b>`` there themselves), so it must NOT be escaped here:
    escaping it and then escaping the assembled prefix again
    (``common.product_card_text``) made the user see ``&lt;b&gt;`` inside
    the caption. Dynamic values are escaped by whoever builds a header.

    No position ("3/20"), no counter, no page marker and NOTHING about
    filters or the marketplace API: the caption of a card is one context
    line plus the real fields of the product itself. A constraint the
    pipeline could not apply is recorded by
    ``_log_unapplied_constraints`` and never rendered here - the user
    asked for products, not for an explanation of what an API returns.

    ``_index`` stays in the signature for callers that pass the current
    card position, but no element of the caption depends on it.
    """

    headline = _card_headline(outcome, header)
    return headline or ""

async def _send_product_cards(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    outcome: SearchOutcome,
    session_id: str,
    *,
    user_id: int | None = None,
    telegram_id: int | None = None,
) -> None:
    """Send ONE card - the first product of the deduplicated result set.

    Navigation happens by editing this single message: every product of
    the result set is reachable through ◀️/➡️ without sending a new
    message. The card carries no number and no counter - only the feed
    context and the real, conditional fields of that product.
    """

    products = outcome.products
    if not products:
        return
    meta = common.get_outcome_meta(context, session_id)
    header = meta.get("header")

    await common.record_event(
        context,
        analytics.EVENT_PRODUCT_SHOWN,
        user_id=user_id,
        telegram_id=telegram_id,
        product=products[0],
    )

    # The per-user history is written for EVERY flow (search, category,
    # popular, photo) - each flow into its OWN scope: the next open of this
    # feed must skip the card the user is looking at right now, while a
    # normal search is never emptied by the popular rotation's state.
    await common.remember_shown(
        context,
        user_id,
        products[0],
        flow=common.session_flow(context, session_id),
    )

    # The SAME instant is the only honest moment to call a product "seen": the
    # card is in front of the user right now. The novelty history of the
    # curation layer is written next to the per-scope history above - it never
    # replaces it, and a failed write degrades to the old behaviour.
    await common.remember_views(context, user_id, [products[0]])

    tracked = await common.is_product_tracked(
        context, user_id, products[0].product_id
    )

    try:
        await common.send_product_card(
            context,
            chat_id,
            products[0],
            None,
            caption_prefix=_card_caption(outcome, 0, header),
            reply_markup=_browse_keyboard(
                context, outcome, session_id, 0, tracked=tracked
            ),
        )
    except Exception:
        logger.exception("Failed to send the browsing card of session %s", session_id)

async def _edit_browsed_card(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    outcome: SearchOutcome,
    session_id: str,
    index: int,
    *,
    user_id: int | None = None,
    tracked: bool = False,
) -> None:
    """Rewrite the browsed card to another product of the same session.

    Everything the user sees must belong to ONE product: the image, the
    caption text and the keyboard (including the Buy URL). A photo
    message therefore gets ``edit_message_media`` with an
    :class:`~telegram.InputMediaPhoto` that carries the NEW product's
    image URL, its caption and the fresh keyboard - editing only the
    caption would leave the old photo on screen above Product B's text,
    which is exactly the stale-image bug.

    Telegram cannot turn a photo message into a text one (or back), so
    when the new product's image presence differs from the current
    message type, the card is replaced: the stale message is deleted and
    a fresh card is sent as a single new message - the only Telegram
    operation that fits. ``tracked`` reflects the user's price-tracking
    state for the NEW product, so the 🔔/🔕 toggle stays honest in the
    same card.

    The caption is rebuilt by the very same renderer that sent the card
    (``common.product_card_text``); buttons still carry only
    ``(session_id, index)`` and no URL is ever constructed here - the
    affiliate link comes from the cached ``Product`` at render time.
    """

    product = outcome.products[index]
    meta = common.get_outcome_meta(context, session_id)
    caption = common.product_card_text(
        product,
        index=None,
        caption_prefix=_card_caption(outcome, index, meta.get("header")),
    )
    reply_markup = _browse_keyboard(
        context, outcome, session_id, index, tracked=tracked
    )

    message = getattr(query, "message", None)
    is_photo = bool(getattr(message, "photo", None))
    has_image = bool(product.image_url)

    if has_image and is_photo:
        # Replace image + caption + keyboard in ONE call.
        try:
            await query.edit_message_media(
                media=InputMediaPhoto(
                    media=product.image_url,
                    caption=common.truncate_for_caption(caption),
                    parse_mode="HTML",
                ),
                reply_markup=reply_markup,
            )
            return
        except Exception:
            # The new image URL may be unfetchable by Telegram; fall
            # back to a full card replacement instead of a stale screen.
            logger.warning(
                "Media edit failed for %s/%s; replacing the card message",
                product.marketplace,
                product.product_id,
            )
            await _replace_browsed_card(
                query, context, product, index, caption, reply_markup
            )
            return

    if has_image != is_photo:
        # Text <-> photo transition: Telegram has no edit for it.
        await _replace_browsed_card(
            query, context, product, index, caption, reply_markup
        )
        return

    if is_photo:
        await query.edit_message_caption(
            caption=common.truncate_for_caption(caption),
            parse_mode="HTML",
            reply_markup=reply_markup,
        )
    else:
        await query.edit_message_text(
            caption, parse_mode="HTML", reply_markup=reply_markup
        )

async def _replace_browsed_card(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    product,
    index: int,
    caption: str,
    reply_markup,
) -> None:
    """Replace a card whose message type cannot be edited in place.

    The old message is deleted and ONE fresh card is sent - the minimal
    Telegram-supported operation when photo/text media must change.
    """

    message = getattr(query, "message", None)
    chat_id = getattr(message, "chat_id", None)
    if chat_id is not None:
        try:
            await context.bot.delete_message(
                chat_id=chat_id, message_id=message.message_id
            )
        except Exception:  # noqa: BLE001 - deletion is best-effort
            logger.warning("Could not delete the stale browsing card", exc_info=True)
    await common.send_product_card(
        context,
        chat_id,
        product,
        None,
        caption_prefix=None,
        reply_markup=reply_markup,
        raw_caption=caption,
    )

async def browse_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``pnav:<session_id>:<index>`` - move the ONE card to another product.

    The whole visible result set stays cached under ``session_id``, so the
    same Telegram message is edited in place - content AND keyboard - with
    no new message sent while the user walks the feed. An expired session
    (cache rotated, bot restarted) is answered with an honest notice
    instead of inventing a list.
    """

    query = update.callback_query
    if query is None:
        return

    parsed = _parse_session_index(query.data or "")
    if parsed is None:
        await query.answer("Некорректная кнопка.", show_alert=True)
        return

    session_id, index = parsed
    outcome = common.get_cached_outcome(context, session_id)
    if outcome is None or not outcome.products:
        await query.answer("Результат устарел — повторите поиск.", show_alert=True)
        return

    products = outcome.products
    if not 0 <= index < len(products):
        await query.answer("Товар недоступен.", show_alert=True)
        return

    product = products[index]

    user_id = await common.register_user(update, context)

    # Feed sessions («✨ Находки») persist every card the user
    # actually browses to, so a later open skips it too. Best effort:
    # a failed history write must never break the card navigation.
    meta = common.get_outcome_meta(context, session_id)
    flow = common.session_flow(context, session_id)
    if meta.get("track_shown") and user_id is not None:
        # A FORWARD step of a feed session claims a NEW product and
        # must respect the same persistent exclusion an open does: the
        # list of an old card may have been shown meanwhile by a later
        # open, and a cached session must never bypass the history.
        # Steps back to already-visited positions stay untouched -
        # that is navigation, not a feed step.
        target = await _feed_forward_target(
            context, user_id, meta, products, index
        )
        if target is None:
            # Everything ahead was already shown: the honest end-of-
            # feed screen with the existing explicit reset.
            await query.answer()
            await _edit_to_feed_exhausted(query, session_id)
            return
        index = target
        product = products[index]

    # Whatever flow this session belongs to, the card the user is now
    # looking at is a product they have been shown - the history is the
    # single source every feed uses to avoid repeating itself. It is written
    # into the scope of THIS session's flow, so a search card never advances
    # the popular rotation and a rotation card never hides a search result.
    await common.remember_shown(context, user_id, product, flow=flow)

    # The browsed card is shown, so it joins the curation layer's novelty
    # history too: the NEXT feed - in this process or after a restart - skips
    # or defers it instead of repeating it as if it were new.
    await common.remember_views(context, user_id, [product])

    await common.record_event(
        context,
        analytics.EVENT_PRODUCT_SHOWN,
        user_id=user_id,
        telegram_id=update.effective_user.id if update.effective_user else None,
        product=product,
    )

    tracked = await common.is_product_tracked(context, user_id, product.product_id)

    await query.answer()
    try:
        await _edit_browsed_card(
            query,
            context,
            outcome,
            session_id,
            index,
            user_id=user_id,
            tracked=tracked,
        )
    except Exception:
        # "Message is not modified" is harmless; anything else is logged.
        logger.debug("Card edit skipped for session %s", session_id, exc_info=True)

    _ = product  # the product is resolved by the action buttons on demand

async def _feed_forward_target(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    meta: dict,
    products: list,
    index: int,
) -> int | None:
    """The index a forward step of a FEED session may actually show.

    ``meta["max_index"]`` remembers the furthest position this cached
    session has visited. A target BEYOND it is a claim of a NEW product:
    it must not be one the FEED already showed this user (a later open
    may have shown the rest of this old list meanwhile), so already-shown
    products are skipped - the feed's own scope, never the state of an
    unrelated search. At or behind ``max_index`` the user is re-walking their
    own session - ordinary navigation, kept intact. Returns ``None`` when
    nothing unseen remains ahead; a failed history read degrades to plain
    navigation (never to a crash or a wrong skip).
    """

    max_visited = int(meta.get("max_index") or 0)
    if index <= max_visited:
        return index
    try:
        seen = await common.feed_seen(context, user_id)
    except Exception:
        logger.warning(
            "Failed to read the feed history for %s", user_id, exc_info=True
        )
        return index
    target = index
    while target < len(products) and identity_pair(products[target]) in seen:
        target += 1
    if target >= len(products):
        return None
    meta["max_index"] = target
    return target

async def _edit_to_feed_exhausted(query, session_id: str) -> None:
    """Replace a feed card with the honest end-of-feed screen.

    The same text and explicit reset button an exhausted open shows (the
    imports are lazy because ``handlers.discovery`` imports this module),
    rendered for the message type Telegram actually has - photo cards are
    edited through their caption.
    """

    from handlers.discovery import FEED_EXHAUSTED_TEXT
    from keyboards.main import feed_exhausted_keyboard

    reply_markup = feed_exhausted_keyboard()
    try:
        message = getattr(query, "message", None)
        if getattr(message, "photo", None):
            caption, parse_mode = _summary_caption(FEED_EXHAUSTED_TEXT)
            await query.edit_message_caption(
                caption=caption, parse_mode=parse_mode, reply_markup=reply_markup
            )
        else:
            await query.edit_message_text(
                FEED_EXHAUSTED_TEXT,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
    except Exception:
        logger.warning(
            "Failed to show the feed end for session %s",
            session_id,
            exc_info=True,
        )

def _summary_caption(text: str) -> tuple[str, str | None]:
    """Adapt summary HTML to Telegram's photo-caption limits.

    Captions are capped (``CAPTION_LIMIT``) and an HTML tag cut in half
    by a naive truncation would make Telegram reject the whole edit
    ("Can't parse entities"). Summaries normally fit and keep their
    markup; on overflow the tags are dropped and the words survive as
    plain text - a degraded but honest and working screen.
    """

    if len(text) <= CAPTION_LIMIT:
        return text, "HTML"
    plain = re.sub(r"<[^<>]+>", "", text)
    return common.truncate_for_caption(plain), None

async def next_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Legacy «➡️ Показать ещё» button from a message of an earlier version.

    Paged result lists no longer exist: a feed opens straight on a product
    card and «➡️ Следующий» walks the whole cached result set inside that
    card. An old message therefore cannot be paged any more - but it must
    not be a dead end either, so the press says where the products are and
    changes nothing (the cached session, if any, stays intact).
    """

    query = update.callback_query
    if query is None:
        return

    session_id = (query.data or "").split(":", 1)[-1]
    if common.get_cached_outcome(context, session_id) is None:
        await query.answer("Результат устарел — повторите поиск.", show_alert=True)
        return

    await query.answer(
        "Листайте товары кнопкой «➡️ Следующий» на карточке товара.",
        show_alert=True,
    )

    # The browsing card is NOT re-sent per page: it keeps browsing the
    # whole result set in place (browse_callback), so paging the summary
    # never floods the chat with more product messages.

async def favorite_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``⭐ В избранное`` button on a product card."""

    query = update.callback_query
    if query is None:
        return

    parsed = _parse_session_index(query.data or "")
    if parsed is None:
        await query.answer("Некорректная кнопка.", show_alert=True)
        return

    session_id, index = parsed
    product = common.get_cached_product(context, session_id, index)
    if product is None:
        await query.answer("Результат устарел — повторите поиск.", show_alert=True)
        return

    user_id = await common.register_user(update, context)
    if user_id is None:
        await query.answer("Не удалось сохранить: ошибка базы данных.", show_alert=True)
        return

    try:
        await common.get_database(context).add_favorite(
            user_id=user_id,
            marketplace=str(getattr(product.marketplace, "value", product.marketplace)),
            product_id=product.product_id,
            product_json=common.product_to_dict(product),
        )
    except Exception:
        logger.exception("Failed to add favorite for user %s", user_id)
        await query.answer("Не удалось сохранить в избранное.", show_alert=True)
        return

    await common.record_event(
        context,
        analytics.EVENT_FAVORITE_ADDED,
        user_id=user_id,
        telegram_id=update.effective_user.id if update.effective_user else None,
        product=product,
    )
    await query.answer("⭐ Добавлено в избранное")

async def track_price_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """``🔔 Следить за ценой`` button on a product card.

    Records the product and its current price, then flips THIS card to
    the ``🔕 Не отслеживать`` state - the state always updates inside the
    same card, never as a separate message.
    """

    query = update.callback_query
    if query is None:
        return

    parsed = _parse_session_index(query.data or "")
    if parsed is None:
        await query.answer("Некорректная кнопка.", show_alert=True)
        return

    session_id, index = parsed
    outcome = common.get_cached_outcome(context, session_id)
    product = common.get_cached_product(context, session_id, index)
    if product is None:
        await query.answer("Результат устарел — повторите поиск.", show_alert=True)
        return

    user_id = await common.register_user(update, context)
    if user_id is None:
        await query.answer("Не удалось сохранить: ошибка базы данных.", show_alert=True)
        return

    try:
        await common.get_database(context).add_tracked_product(
            user_id=user_id,
            marketplace=str(getattr(product.marketplace, "value", product.marketplace)),
            product_id=product.product_id,
            title=product.title,
            product_url=product.affiliate_url or product.product_url,
            initial_price=product.price,
            currency=product.currency,
        )
    except Exception:
        logger.exception("Failed to start price tracking for user %s", user_id)
        await query.answer("Не удалось включить отслеживание.", show_alert=True)
        return

    await common.record_event(
        context,
        analytics.EVENT_TRACKING_ADDED,
        user_id=user_id,
        telegram_id=update.effective_user.id if update.effective_user else None,
        product=product,
    )
    await query.answer("🔔 Следим за ценой")

    await _rerender_card_state(query, context, outcome, session_id, index, user_id)

async def card_untrack_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """``🔕 Не отслеживать`` button on a product card.

    Stops THIS user's tracking of THIS product and flips the same card
    back to the ``🔔 Следить за ценой`` state.
    """

    query = update.callback_query
    if query is None:
        return

    parsed = _parse_session_index(query.data or "")
    if parsed is None:
        await query.answer("Некорректная кнопка.", show_alert=True)
        return

    session_id, index = parsed
    outcome = common.get_cached_outcome(context, session_id)
    product = common.get_cached_product(context, session_id, index)
    if product is None:
        await query.answer("Результат устарел — повторите поиск.", show_alert=True)
        return

    user_id = await common.register_user(update, context)
    if user_id is None:
        await query.answer("Не удалось сохранить: ошибка базы данных.", show_alert=True)
        return

    try:
        rows = await common.get_database(context).get_tracked_products(user_id)
        tracked_row = next(
            (row for row in rows if row.get("product_id") == product.product_id),
            None,
        )
        if tracked_row is not None:
            await common.get_database(context).deactivate_tracked_product(
                tracked_row["id"], user_id
            )
    except Exception:
        logger.exception("Failed to stop price tracking for user %s", user_id)
        await query.answer("Не удалось отключить отслеживание.", show_alert=True)
        return

    await common.record_event(
        context,
        analytics.EVENT_TRACKING_STOPPED,
        user_id=user_id,
        telegram_id=update.effective_user.id if update.effective_user else None,
        product=product,
    )
    await query.answer("🔕 Отслеживание остановлено")

    await _rerender_card_state(query, context, outcome, session_id, index, user_id)

async def _rerender_card_state(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    outcome: SearchOutcome,
    session_id: str,
    index: int,
    user_id: int,
) -> None:
    """Re-render the SAME card after a tracking toggle (🔔 ↔ 🔕)."""

    product = outcome.products[index]
    tracked = await common.is_product_tracked(
        context, user_id, product.product_id
    )
    try:
        await _edit_browsed_card(
            query,
            context,
            outcome,
            session_id,
            index,
            user_id=user_id,
            tracked=tracked,
        )
    except Exception:
        logger.debug(
            "Card re-render skipped for session %s", session_id, exc_info=True
        )

def _parse_session_index(data: str) -> tuple[str, int] | None:
    """Parse ``<prefix>:<session_id>:<index>`` callback data."""

    parts = data.split(":")
    if len(parts) != 3:
        return None
    try:
        return parts[1], int(parts[2])
    except ValueError:
        return None

# --- B2/B5: follow-ups answered straight from the shown results ------------

#: Words that only state WHICH currency to display («используй гривны»,
#: «показывай в долларах», «use UAH»). A message made of nothing but these
#: words and a currency name changes a PRESENTATION preference: it must not
#: become a keyword search for «гривны» and must not spend an AI call.
#: Adjectives that describe a currency rather than a product («украинские
#: гривны», «американские доллары»). Presentation wording, never a product.
_CURRENCY_ADJECTIVES: tuple[str, ...] = (
    "украинские", "украинский", "украинских", "украинская",
    "российские", "российский", "российских", "российская",
    "американские", "американский", "американских", "американская",
    "европейские", "европейский", "европейских", "европейская",
    "китайские", "китайский", "китайских", "китайская",
    "британские", "британский", "британских", "британская",
    "ukrainian", "russian", "american", "european", "chinese", "british",
)

_CURRENCY_REQUEST_VERBS: tuple[str, ...] = (
    "используй", "используйте", "использовать", "показывай", "показывайте",
    "показать", "переведи", "переводи", "смени", "сменить", "поменяй",
    "поменять", "хочу", "давай", "лучше", "цена", "цены", "цену", "цене",
    "цен", "стоимость", "стоимостью", "валюта", "валюту", "валюте",
    "отображай", "отображать", "покажи", "показывать", "все", "всё",
    "use", "show", "show me", "display", "convert", "change", "switch",
    "price", "prices", "currency", "in", "to", "please", "me", "i", "want",
    "would", "like", "the", "all", "set",
)

#: Framing that makes a message about DISPLAYING prices ("цены в ...",
#: "показывай в ..."), so an unknown currency word can be reported instead of
#: being searched as a product name.
_CURRENCY_FRAMING: tuple[str, ...] = (
    "цена", "цены", "цену", "цен", "ценах", "стоимость", "валют",
    "price", "prices", "currency",
)

def _has_currency_framing(text: str) -> bool:
    """Whether ``text`` talks about price display at all."""

    lowered = (text or "").lower().replace("ё", "е")
    return any(marker in lowered for marker in _CURRENCY_FRAMING)

def _currency_request_state(text: str) -> dict | None:
    """What a currency-restating message asks for, or ``None``.

    Returns ``{"currency": CODE | None, "amount": float | None}``. ``currency``
    is ``None`` when the user named a currency this project cannot display
    (the caller then says honestly which ones it supports). ``amount`` is an
    explicit amount the same message names, if any.

    Conservative by construction: every word must be currency vocabulary
    (a currency name spelled by PREFIX, so «долларах»/«гривнях» match), a
    request verb, a number or a stopword. Any real product or feature word
    means the message is NOT a pure currency restatement and answers ``None``.
    """

    from services.query_intent import CURRENCY_ALIASES, normalize_currency
    from services.text_matching import normalize_text

    raw = (text or "").strip()
    if not raw:
        return None
    lowered = raw.lower().replace("ё", "е")

    # Letter-only aliases, shortest usable form first, so the five-letter stem
    # matches every inflection («гривнах», «долларах», «доларах»). Symbols are
    # matched as plain substrings.
    word_aliases = sorted(
        {alias.casefold() for alias in CURRENCY_ALIASES if alias.isalpha()},
        key=len,
    )
    symbol_aliases = [
        alias for alias in CURRENCY_ALIASES if not alias.isalpha()
    ]
    prefix_by_word = {
        alias: normalize_currency(alias) for alias in word_aliases
    }

    def _is_currency_word(word: str) -> bool:
        return any(
            word.startswith(alias[:5]) and len(alias) >= 3
            for alias in word_aliases
        )

    matched: str | None = None
    for symbol in symbol_aliases:
        if symbol in lowered:
            matched = normalize_currency(symbol)
            break
    if matched is None:
        for word in normalize_text(raw):
            if _is_currency_word(word):
                candidate = next(
                    prefix_by_word[alias]
                    for alias in word_aliases
                    if word.startswith(alias[:5]) and len(alias) >= 3
                )
                if candidate is not None:
                    matched = candidate
                    break
    if matched is None:
        # No KNOWN currency name. If the message is still nothing but display
        # wording plus an unrecognised currency word, report the unsupported
        # currency honestly instead of searching the word as a product.
        unknown = [
            word
            for word in normalize_text(raw)
            if len(word) >= 3
            and word not in _NARROW_STOPWORDS
            and word not in _CURRENCY_REQUEST_VERBS
            and word not in _CURRENCY_ADJECTIVES
            and not any(
                word.startswith(verb[:5])
                for verb in _CURRENCY_REQUEST_VERBS
                if len(verb) >= 5
            )
        ]
        if unknown and _has_currency_framing(raw):
            return {"currency": None, "amount": None}
        return None

    # Content guard: anything that is not currency vocabulary, a request verb,
    # a stopword or a number means the message says something else too.
    leftover = [
        word
        for word in normalize_text(raw)
        if len(word) >= 3
        and not _is_currency_word(word)
        and word not in _NARROW_STOPWORDS
        and word not in _CURRENCY_REQUEST_VERBS
        and word not in _CURRENCY_ADJECTIVES
        and not any(
            word.startswith(verb[:5])
            for verb in _CURRENCY_REQUEST_VERBS
            if len(verb) >= 5
        )
    ]
    if leftover:
        return None

    amount = None
    number = re.search(r"\d+(?:[.,]\d+)?", raw)
    if number is not None:
        try:
            amount = float(number.group(0).replace(",", "."))
        except ValueError:
            amount = None
    currency = matched if matched in {"USD", "EUR", "RUB", "UAH", "CNY", "GBP"} else None
    return {"currency": currency, "amount": amount}

def _with_result_limit(
    request: SearchRequest, count: int | None
) -> SearchRequest:
    """``request`` carrying the explicitly requested result count."""

    if count is None or count == request.result_limit:
        return request
    return request.model_copy(update={"result_limit": count})

def _redeliver_subset(
    outcome: SearchOutcome,
    *,
    current: str | None,
    count: int | None,
    selected: list[Product] | None = None,
) -> SearchOutcome:
    """A subset of ALREADY-shown products, as a new cached browsing session.

    ``selected`` (explicit products, e.g. the cheapest ones) wins; otherwise
    the current on-screen list is cut to ``count`` in its existing order.
    Nothing is fetched and nothing is re-curated: these are the very objects
    the user is looking at.
    """

    products = list(selected) if selected is not None else list(outcome.products)
    if count is not None:
        products = products[:count]
    if current is not None and len(products) > current:
        # The display cap only ever trims the visible list.
        products = products[:current]
    return SearchOutcome(request=outcome.request, products=products)

async def _answer_from_shown(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
) -> bool:
    """Handle a follow-up that the SHOWN results can answer without any call.

    Two families are handled here, both without a Gemini request and without a
    marketplace request:

    * a COUNT change («покажи 5», «сделай три варианта», «ещё 2») - the
      current list is re-delivered cut to the requested number;
    * a CURRENCY preference («используй гривны», «покажи в долларах») - the
      cards are re-rendered from the SAME products with that currency as the
      display preference, plus an honest one-line confirmation. The project
      has no exchange rates, so a real conversion is never claimed.

    Returns ``True`` when the message was fully handled. Every other message
    returns ``False`` and continues on the ordinary pipeline.
    """

    session_id = _last_shown_session(context)
    if session_id is None:
        return False
    outcome = common.get_cached_outcome(context, session_id)
    if outcome is None or not outcome.products:
        return False

    conversation = get_conversation(context)
    count = is_count_only_follow_up(text)
    if count is not None:
        # «Покажи 5» re-delivers products ALREADY retrieved - no provider
        # call, no AI call, no re-ranking. The pool is consulted first so a
        # LARGER count can reveal products the earlier cap hid, and the new
        # count is applied last, so the delivered list never exceeds it.
        outcome = _poolled_outcome(context, outcome)
        narrowed = _redeliver_subset(outcome, current=None, count=count)
        user_id = await common.register_user(update, context)
        telegram_id = update.effective_user.id if update.effective_user else None
        shown = await send_search_results(
            context,
            update.effective_message.chat_id,
            narrowed,
            user_id=user_id,
            telegram_id=telegram_id,
            pre_curated=True,
        )
        if shown:
            context.user_data["last_shown_session"] = shown
        if conversation is not None:
            # The conversation keeps the SAME product request; only the
            # requested count moves, so the next follow-up still refines it.
            conversation.request = _with_result_limit(conversation.request, count)
            conversation.touch()
        delivered = len(narrowed.products)
        await context.bot.send_message(
            update.effective_message.chat_id,
            _count_follow_up_text(delivered, count),
            reply_markup=back_to_menu_keyboard(),
        )
        return True

    request = _currency_request_state(text)
    if request is not None:
        return await _answer_currency_preference(
            update, context, text, request, outcome, conversation
        )
    return False

def _has_no_shown_results(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Whether no cached result set is available AT ALL.

    «Покажи 3 варианта» can only be applied to products the user can see; with
    the cache gone (expired, evicted, after a restart) it must be answered
    honestly instead of being searched as the literal phrase «3 варианта».
    """

    cache = context.user_data.get(common.OUTCOME_CACHE_KEY) or {}
    return not any(
        isinstance(outcome, SearchOutcome) and outcome.products
        for outcome in cache.values()
    )

def _poolled_outcome(
    context: ContextTypes.DEFAULT_TYPE, outcome: SearchOutcome
) -> SearchOutcome:
    """The FULL result pool of the same search, when it is still cached.

    A browsing session may hold only the visible subset (the requested count
    caps it). Increasing the count later must reveal the products that were
    already retrieved - never trigger another search. The pool is the largest
    cached set whose request is the SAME search; the current set is the floor.
    """

    cache: dict = context.user_data.get(common.OUTCOME_CACHE_KEY, {})
    best = outcome
    for candidate in cache.values():
        if not isinstance(candidate, SearchOutcome):
            continue
        if candidate.request.original_query != outcome.request.original_query:
            continue
        if len(candidate.products) > len(best.products):
            best = candidate
    return best

def _count_follow_up_text(delivered: int, wanted: int) -> str:
    """Honest one-liner after a count change."""

    if delivered == 0:
        return EMPTY_RESULT_TEXT
    text = f"Показал {delivered} из {delivered}."
    if delivered < wanted:
        text = (
            f"Подходящих в текущей подборке только {delivered} - "
            f"показал все."
        )
    return f"{text} Листайте карточки кнопкой «➡️»."

async def _answer_currency_preference(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    request: dict,
    outcome: SearchOutcome,
    conversation,
) -> bool:
    """Re-render the shown products under the requested currency preference.

    The currency is a DISPLAY preference. The project holds no exchange rates,
    so prices are never converted or relabelled: the card keeps the currency
    and amount the marketplace actually returned, the existing price FILTER is
    scoped to the requested currency, and the user reads exactly that.
    """

    from services.product_logging import log_final_pool

    chat_id = update.effective_message.chat_id
    currency = request.get("currency")
    if currency is None:
        # Unsupported spelling: the bot says what it can do instead of
        # pretending the preference was applied.
        await context.bot.send_message(
            chat_id,
            "Пока могу показывать цены в USD, EUR, RUB, UAH, CNY и GBP. "
            "Скажите одну из этих валют, и я поправлю подборку.",
            reply_markup=back_to_menu_keyboard(),
        )
        return True

    base = conversation.request if conversation is not None else outcome.request
    update_fields: dict = {"currency": currency}
    amount = request.get("amount")
    if amount is not None:
        # An explicit amount in the same message («покажи в грн, вот 1000»)
        # becomes the budget bound - a real constraint the user stated.
        update_fields["max_price"] = amount
    new_request = base.model_copy(update=update_fields)

    if conversation is not None:
        conversation.request = new_request
        conversation.touch()

    # The cards are re-rendered from the SAME products (no provider call):
    # only the header/preference changes, never the real listing data.
    resolved = outcome.request
    if (
        resolved.max_price is not None
        and new_request.max_price is not None
        and new_request.max_price < resolved.max_price
    ):
        # The user also named a smaller budget: apply it to the ALREADY shown
        # real products, honestly, instead of fetching a new list.
        kept = [
            product
            for product in outcome.products
            if product.price is None or product.price <= new_request.max_price
        ]
        resolved = new_request
        outcome = SearchOutcome(
            request=new_request,
            products=kept,
            searched_marketplaces=list(outcome.searched_marketplaces),
            failed_marketplaces=list(outcome.failed_marketplaces),
            unavailable_marketplaces=list(outcome.unavailable_marketplaces),
        )
        conversation.request = new_request
        log_final_pool(
            logger, kept, context="currency re-render", origin="currency"
        )

    user_id = await common.register_user(update, context)
    telegram_id = update.effective_user.id if update.effective_user else None
    shown = await send_search_results(
        context,
        chat_id,
        outcome,
        user_id=user_id,
        telegram_id=telegram_id,
        pre_curated=True,
    )
    if shown:
        context.user_data["last_shown_session"] = shown

    note = (
        f"Ок, ориентируюсь на цены в {currency}. Цены указаны как есть в "
        "карточках: курса и пересчёта у меня нет, поэтому суммы не меняю."
    )
    await context.bot.send_message(chat_id, note, reply_markup=back_to_menu_keyboard())
    return True
