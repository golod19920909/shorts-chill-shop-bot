"""Gemini integration - the only module in the project that talks to an LLM.


Strict rules implemented here (see the project brief):
- Gemini NEVER produces product data. It is used only for
  natural-language understanding (turning a user message into a
  :class:`~models.search_request.SearchRequest`), for PHOTO understanding
  (``analyze_image`` only reads what is visible/readable on the picture -
  type, brand, model, packaging text - never a product, price or link) and,
  optionally, for SELECTING from ALREADY-RETRIEVED candidates: the feed
  selector (``select_feed_candidates``, at most :data:`TOP_CANDIDATES` cards,
  judged by :data:`STRICT_FILTER_RULES`) and the assistant/discovery pickers
  (``select_candidates`` / ``rank_candidates``), which return only ids the
  service validates. The «🔥 Популярное» feed is NOT model-ranked: its order
  is the marketplace's own order volume (``popularity_score``), which keeps
  that feed reproducible.
- All numeric constraints that arrive from Gemini are re-applied
  programmatically later, in ``services.comparison_service``.
- If Gemini is not configured or fails, the bot keeps working: queries
  fall back to a deterministic local parser and every ranking keeps its
  deterministic fallback.
"""


from __future__ import annotations


import asyncio
import time
import json
import logging
import re


from services.gemini_telemetry import record_attempt
from models.product import Marketplace, Product
from models.search_request import SearchRequest, SortPreference
from services.query_intent import CURRENCY_ALIASES, normalize_currency
from services.conversation import (
    _REQUIRED_FEATURE_RE,
    _clean_feature,
    deterministic_merge,
    feature_lists_equivalent,
    merge_feature_lists,
)
from services.product_assistant import numeric_tokens, product_facts
from services.relevance import noun_terms
from services.text_matching import (
    extract_requested_result_count,
    is_context_clause,
    is_design_clause,
    is_negation_clause,
    strip_option_count,
)

#: Hard-constraint fields the AI merge must keep verbatim (Stage 3C).
HARD_MERGE_FIELDS = (
    "max_price",
    "min_price",
    "min_rating",
    "min_orders",
    "quantity",
    "currency",
    "category",
    "country",
    "intent_product_type",
    "intent_brand",
    "intent_model",
    "intent_generation",
)


logger = logging.getLogger(__name__)


#: Default model per ROLE. ``DEFAULT_MODEL`` is the "smart" model (the
#: reasoning-heavy one) and stays the first constructor argument, so every
#: existing caller keeps working unchanged.
DEFAULT_MODEL = "gemini-3.8-flash"
DEFAULT_FAST_MODEL = "gemini-3.8-flash"
DEFAULT_VISION_MODEL = "gemini-3.8-flash"
DEFAULT_FALLBACK_MODEL = "gemini-3.1-flash-lite"


#: How many candidates ONE selection may keep - the "строго 20 лучших" rule of
#: the feed. The model is asked for at most this many ids and the cap is ALSO
#: enforced in code (:meth:`AIService._clean_ids`), because a chatty answer must
#: never widen the carousel beyond the agreed size. A pool that holds fewer
#: usable products returns fewer: the number is a ceiling, not a quota to pad
#: with invented or repeated items.
TOP_CANDIDATES = 20


#: The rules every selection prompt shares (the assistant's candidate picker and
#: the feed's selector judge by exactly the same criteria, so one screen can
#: never be cleaned differently from another). They are written as hard
#: exclusions because the marketplace answer of a broad keyword is full of
#: accessories and spam; the model may only DROP real listings - it never
#: invents one (the ids it returns are validated against the pool it was sent).
STRICT_FILTER_RULES = """\
- исключай товары с рейтингом ниже 4.5, а также позиции с явно плохими отзывами;
- исключай НЕ ТОТ товар: аксессуары и комплектующие (чехлы, защитные стёкла,
  кабели, переходники, держатели, запчасти), когда ищется само устройство
  или вещь, а не дополнение к ней;
- исключай позиции с аномально низкой ценой: по такой цене продают спам или
  комплектующие, а не искомый товар;
- исключай дубли и повторы одной и той же позиции в разных магазинах;
- из оставшихся выбери СТРОГО не более 20 лучших и отсортируй их по
  релевантности запросу (сначала самое подходящее);"""


#: Structured Google API statuses that mean the WHOLE KEY is out of
#: quota/rate-limited. Another model cannot help, so the fallback model is
#: deliberately not attempted - that would only burn more of the same
#: account quota.
QUOTA_API_STATUSES = frozenset(
    {"RESOURCE_EXHAUSTED", "RATE_LIMITED", "TOO_MANY_REQUESTS"}
)


#: Structured Google API statuses for broken/insufficient credentials
#: (HTTP 401/403 side of the same "the request never ran" problem).
AUTH_API_STATUSES = frozenset(
    {"UNAUTHENTICATED", "PERMISSION_DENIED", "INVALID_API_KEY", "API_KEY_INVALID"}
)


#: Structured statuses that mean THIS MODEL/deployment is unavailable while
#: the key itself is fine - the one case where the fallback model is worth a
#: try. ``503 UNAVAILABLE`` stays here on purpose: that is how the original
#: marker list treated it, so the fallback policy does not change.
MODEL_UNAVAILABLE_STATUSES = frozenset(
    {"NOT_FOUND", "UNAVAILABLE", "SERVICE_UNAVAILABLE"}
)


#: Fallback (LAST RESORT) text markers, consulted ONLY when an exception
#: carries no structured HTTP/API status at all - plain exceptions and
#: transport wrappers. They are never consulted once the SDK has reported a
#: code/status; see ``_classify_gemini_error``.
QUOTA_ERROR_MARKERS = (
    "429",
    "resource_exhausted",
    "quota",
    "rate limit",
    "too_many_requests",
)


MODEL_UNAVAILABLE_MARKERS = (
    "404",
    "not_found",
    "not found",
    "no longer available",
    "503",
    "unavailable",
    "unsupported",
)


#: Exception modules that mean a transport failure rather than an API
#: answer (checked before any status logic: these never carry one).
_TRANSPORT_MODULES = frozenset(
    {"httpx", "httpcore", "aiohttp", "requests", "urllib3"}
)


#: Secret shapes that must never reach the logs. Applied to every sanitized
#: error message; the configured key is additionally removed verbatim by
#: ``_sanitize_error_message``.
_SECRET_PATTERNS = (
    re.compile(r"AIza[0-9A-Za-z_\-]{10,}"),
    re.compile(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|authorization|bearer)\b\s*[=:]\s*\S+"
    ),
)


#: Longest sanitized error message that may appear in one log record.
_ERROR_MESSAGE_LOG_LIMIT = 300




def _gemini_error_facts(
    error: BaseException,
) -> tuple[int | None, str | None, str | None]:
    """Structured HTTP/API facts about ``error`` - never guessed.


    Returns ``(http_status, api_status, api_message)``; a missing field is
    ``None``. Sources, verified against the installed ``google-genai``
    (2.24.0, ``google.genai.errors``):


    * ``google.genai.errors.APIError`` exposes ``code`` (int), ``status``
      (e.g. ``"RESOURCE_EXHAUSTED"``) and ``message``;
    * any exception carrying a plain integer ``status_code`` attribute.


    Everything else yields ``(None, None, None)``: attributes are looked up
    with ``getattr`` and type-tested, because not every exception defines
    them and some define them with a different meaning.
    """


    try:
        from google.genai.errors import APIError
    except Exception:  # pragma: no cover - SDK missing/failed to import
        APIError = None  # type: ignore[assignment,misc]
    if APIError is not None and isinstance(error, APIError):
        code = getattr(error, "code", None)
        status = getattr(error, "status", None)
        message = getattr(error, "message", None)
        return (
            code if isinstance(code, int) else None,
            str(status) if status is not None else None,
            str(message) if message is not None else None,
        )
    status_code = getattr(error, "status_code", None)
    if isinstance(status_code, int):
        return status_code, None, None
    # Transport-level wrappers (e.g. ``httpx.HTTPStatusError``) keep the
    # response on a nested object instead.
    response = getattr(error, "response", None)
    response_code = getattr(response, "status_code", None)
    if isinstance(response_code, int):
        return response_code, None, None
    return None, None, None




def _sanitize_error_message(
    message: str, *, api_key: str | None = None, limit: int = _ERROR_MESSAGE_LOG_LIMIT
) -> str:
    """Make an exception message safe to log: secrets out, bounded size.


    Removes the configured API key verbatim, redacts key/token-shaped
    substrings, collapses whitespace and truncates to ``limit``. The text
    still describes the failure - that is the point - but it can never
    carry credentials.
    """


    if not message:
        return ""
    text = str(message)
    if api_key:
        text = text.replace(api_key, "[REDACTED]")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[:limit] + "...[truncated]"
    return text




def _format_gemini_error(
    error: BaseException,
    *,
    model: str | None,
    kind: str,
    will_fallback: bool,
    api_key: str | None = None,
) -> str:
    """Build the safe detail fragment logged for one failed request.


    Carries only diagnostics that cannot leak data: model name, exception
    class, HTTP status, structured API status, where the status came from
    (``origin=api|message``), the classification, whether the fallback
    model will be attempted, and a sanitized/truncated message. Prompts,
    request contents and user text are never included.
    """


    http_status, api_status, _api_message = _gemini_error_facts(error)
    has_status = http_status is not None or api_status is not None
    parts = [
        f"model={model or 'unknown'}",
        f"class={type(error).__name__}",
        f"kind={kind}",
        f"http={http_status if http_status is not None else 'n/a'}",
        f"api_status={api_status or 'n/a'}",
        f"origin={'api' if has_status else 'message'}",
        f"fallback={'yes' if will_fallback else 'no'}",
    ]
    detail = _sanitize_error_message(str(error), api_key=api_key)
    if detail:
        parts.append(f"msg={detail!r}")
    return " ".join(parts)




def _classify_gemini_error(error: BaseException) -> str:
    """Classify a Gemini failure so ``_generate`` can pick a safe policy.


    Returns one of:


    * ``quota``    - HTTP 429 / RESOURCE_EXHAUSTED (account-wide; no fallback);
    * ``auth``     - HTTP 401/403, UNAUTHENTICATED / PERMISSION_DENIED / ...;
    * ``model``    - THIS model is unusable: 404 NOT_FOUND, 503 UNAVAILABLE
                     or an explicitly "unsupported" model - the only kind
                     that advances to the fallback model;
    * ``invalid``  - any other 4xx / INVALID_ARGUMENT (the request itself is
                     wrong, another model would reject it too);
    * ``server``   - HTTP 5xx;
    * ``timeout``  - client-side timeout (``asyncio`` / ``httpx``);
    * ``transport``- network failure before any API answer arrived;
    * ``other``    - unknown.


    Structured status wins whenever the exception carries one; the legacy
    message markers are consulted ONLY when no structured status exists, so
    an arbitrary substring in free text can never be presented as a
    confirmed quota failure (``origin=`` in the log names the source).
    """


    # Structured status always wins: an HTTP status carried by the
    # exception is real evidence, whatever the exception's class is.
    http_status, api_status, api_message = _gemini_error_facts(error)
    if http_status is not None or api_status is not None:
        status = (api_status or "").upper()
        if http_status == 429 or status in QUOTA_API_STATUSES:
            return "quota"
        if http_status in (401, 403) or status in AUTH_API_STATUSES:
            return "auth"
        if http_status in (404, 503) or status in MODEL_UNAVAILABLE_STATUSES:
            return "model"
        if http_status == 400 and (
            "unsupported" in status or "unsupported" in (api_message or "").lower()
        ):
            return "model"
        if http_status is not None and 400 <= http_status < 500:
            return "invalid"
        if http_status is not None and 500 <= http_status < 600:
            return "server"
        # Structured but unmapped: stay conservative and never downgrade to
        # a message-substring guess.
        return "other"


    # ``asyncio.TimeoutError`` is an alias of ``TimeoutError`` on 3.11+.
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, ConnectionError):
        return "transport"
    module = (type(error).__module__ or "").split(".")[0]
    if module in _TRANSPORT_MODULES:
        return "timeout" if "Timeout" in type(error).__name__ else "transport"


    # No structured status at all: keep the historical marker heuristic so
    # plain exceptions keep their old classification.
    message = str(error).lower()
    if any(marker in message for marker in QUOTA_ERROR_MARKERS):
        return "quota"
    if any(marker in message for marker in MODEL_UNAVAILABLE_MARKERS):
        return "model"
    return "other"


QUERY_PARSING_PROMPT = """Ты помощник, который извлекает параметры поиска товаров из сообщения пользователя.


Верни ТОЛЬКО JSON-объект (без markdown, без пояснений) со строго такими полями:
{{
  "original_query": "исходный текст пользователя",
  "normalized_query": "краткий поисковый запрос для маркетплейса на английском",
  "category": "категория товара или null",
  "keywords": ["ключевые", "слова"],
  "max_price": число или null,
  "min_price": число или null,
  "currency": "USD|EUR|RUB|CNY или null",
  "min_rating": число от 0 до 5 или null,
  "min_orders": целое число или null,
  "prefers_good_rating": true или false,
  "quantity": целое число или null,
  "preferred_marketplaces": ["aliexpress"],
  "required_features": ["буквальные характеристики для названия товара (материал, цвет, совместимость, опция) — без фраз о форме/дизайне вида «в виде X» и без фраз о поводе или получателе («для подарка», «для жены», «as a gift»)"],
  "excluded_features": ["исключённые характеристики"],
  "sort_preference": "relevance|price_asc|price_desc|rating_desc|orders_desc|total_cost_asc"
}}


Правила:
- Заполняй только то, что действительно следует из текста пользователя. Не выдумывай цены и характеристики.
- Если пользователь не указал бюджет, оставь max_price = null.
- preferred_marketplaces: оставь пустым списком, если пользователь не назвал маркетплейс явно.
- normalized_query — короткая фраза для поиска по каталогу, без цены и маркетплейса.
- prefers_good_rating: true, если пользователь словами просит товар с хорошим рейтингом или отзывами («с хорошим рейтингом»), но числа не называет. Это только пожелание к порядку: фильтр не ставится, оценка не выдумывается.
- required_features: только короткие буквальные свойства, которые могут стоять в названии товара («титановый», «для iPhone 15», «с микрофоном»). НЕ клади сюда фразы, описывающие форму, дизайн или стиль («в виде строительного инструмента», «в форме сердца», «в стиле ретро») — такие описания уже отражены в keywords и normalized_query, а в названиях товаров они почти никогда не повторяются дословно. НЕ клади сюда и фразы про повод или получателя («для подарка», «для жены», «на день рождения», «as a gift»): это намерение покупателя, а не свойство товара — такие фразы тоже уже отражены в keywords и normalized_query.


Сообщение пользователя:
{user_text}
"""


IMAGE_QUERY_PROMPT = """Посмотри на фотографию и определи, какой товар на ней изображён.


Верни ТОЛЬКО короткий поисковый запрос (2–5 слов) для поиска ПОХОЖЕГО товара на AliExpress.
На русском языке, одной строкой, без кавычек, без пояснений, без цен и без брендовых историй.
Если на фото несколько товаров — выбери самый заметный.
Если на фото нет товара — верни ровно слово: пусто
"""


IMAGE_ANALYSIS_PROMPT = """Посмотри на фотографию товара и опиши то, что на ней действительно видно.


Верни ТОЛЬКО JSON-объект (без markdown, без пояснений) со строго такими полями:
{
  "is_product": true или false — есть ли на фото товар,
  "category": "категория товара или null",
  "product_type": "точный тип товара, например «чай листовой», «зубная щётка электрическая», или null",
  "brand": "бренд с упаковки или корпуса или null",
  "model": "модель или артикул или null",
  "series": "серия или линейка или null",
  "product_name": "название товара с упаковки или null",
  "visible_text": "ключевой читаемый текст с фото (этикетка, упаковка), одна строка или null",
  "color": "цвет или null",
  "material": "материал или null",
  "form_factor": "форма, размер или объём упаковки, например «100 г», «4 шт», или null",
  "size_weight": "размер или вес или null",
  "compatibility": "с чем совместим товар или null",
  "distinctive": "внешний признак, отличающий товар (форма, рисунок), или null",
  "manufacturer": "производитель или null",
  "country": "страна или null"
}
Правила:
- записывай ТОЛЬКО то, что видно на фото или читается с него; ничего не выдумывай;
- если текст на фото читается — сохрани важные слова точно (это первый источник для поиска);
- если на фото нет товара — верни ровно: {"is_product": false}
"""


#: Fields a structured image analysis may carry. Everything else the model
#: might return is dropped - the analysis feeds search VALIDATION, so an
#: invented field could reject real products.
_IMAGE_ANALYSIS_STR_FIELDS = (
    "category",
    "product_type",
    "brand",
    "model",
    "series",
    "product_name",
    "visible_text",
    "color",
    "material",
    "form_factor",
    "size_weight",
    "compatibility",
    "distinctive",
    "manufacturer",
    "country",
)




def _validated_image_analysis(payload: object) -> dict[str, str] | None:
    """Accept an analysis only when it really identifies a product (§7).


    Any non-string/boolean junk is dropped, every string is whitespace-
    collapsed and length-capped, and an answer without at least a product
    type, a product name or readable text is rejected: those three are what
    the search hypotheses and the relevance validation are built from, and
    guessing them is exactly what the vision step must never do.
    """


    if not isinstance(payload, dict):
        return None
    if payload.get("is_product") is False:
        return None


    analysis: dict[str, str] = {}
    for field in _IMAGE_ANALYSIS_STR_FIELDS:
        value = payload.get(field)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            value = str(value)
        if not isinstance(value, str):
            continue
        cleaned = " ".join(value.split()).strip(" \t\"'«»")
        if cleaned and cleaned.lower() not in {"null", "none", "n/a", "-"}:
            analysis[field] = cleaned[:160]


    if not any(
        analysis.get(key) for key in ("product_type", "product_name", "visible_text")
    ):
        return None
    return analysis


RANK_PROMPT = """Ты отбираешь и упорядочиваешь УЖЕ НАЙДЕННЫЕ товары AliExpress для подборки.


Запрос пользователя: {query}


Тебе дан только список реальных товаров с их настоящими полями (JSON).
Верни ТОЛЬКО JSON-объект (без markdown, без пояснений):
{{
  "order": ["id_товара_1", "id_товара_2", ...]
}}


Правила отбора:
{rules}


Правила формата:
- Используй ТОЛЬКО id из переданного списка, каждый не более одного раза.
- Не добавляй и не выдумывай id, товары, цены, рейтинги или ссылки.
- Порядок: от более подходящего к менее подходящему; id, которые ты НЕ
  перечислил, пользователь не увидит — перечисли ровно те позиции, которые
  стоит показать.
- Если сомневаешься — сохрани исходный порядок списка.


Товары (JSON):
{products_json}
"""




ALLOWED_SORT_PREFERENCES = {preference.value for preference in SortPreference}


#: How many products a single AI question may include. Keeps the prompt
#: (and quota cost) bounded while still letting AI compare a real page.
MAX_PRODUCTS_IN_PROMPT = 8


#: Human-readable description of the AI menu goals, used inside prompts so
#: the model never has to guess what "cheaper"/"better" should mean.
GOAL_DESCRIPTIONS = {
    "cheaper": "более дешёвых вариантов того же товара",
    "similar": "похожих товаров",
    "better": "вариантов, которые лучше по данным маркетплейса (отзывы, заказы)",
}


#: The goal line used by the FEED selector (:meth:`AIService.select_feed_candidates`):
#: the carousel of a search, category or photo feed, where there is no anchor
#: product - only the raw pool and the user's own query.
FEED_SELECTION_GOAL = (
    "отобрать для подборки лучшие товары под запрос пользователя "
    "и отсортировать их по релевантности"
)




ANALYSIS_PROMPT = """Ты помогаешь покупателю оценить ОДИН товар.


Тебе даны ТОЛЬКО реальные данные товара из AliExpress (JSON). Верни ТОЛЬКО JSON-объект:
{{
  "observations": ["1–3 коротких наблюдения по этим данным"],
  "recommendation": "1–2 предложения для покупателя"
}}


Правила:
- используй ТОЛЬКО поля из переданного JSON; чего нет в JSON, того не существует;
- НИКОГДА не упоминай качество, материалы, надёжность продавца, скорость доставки
  или гарантию, если это прямо не следует из переданных полей;
- не называй числа, которых нет в данных;
- пиши по-русски, кратко, без markdown и без ссылок.


Данные товара (JSON):
{product_json}
"""


SELECT_PROMPT = """Ты отбираешь и упорядочиваешь УЖЕ НАЙДЕННЫЕ товары AliExpress.


Задача: {goal}
Ориентир запроса: {query}


Тебе даны только реальные товары с их настоящими полями (JSON), включая id.
Верни ТОЛЬКО JSON-объект:
{{
  "ids": ["id_товара_1", "id_товара_2", "..."]
}}


Правила отбора:
{rules}


Правила формата:
- используй ТОЛЬКО id из переданного списка, каждый не более одного раза;
- не добавляй и не выдумывай id, товары, цены, рейтинги или ссылки;
- если подходящих товаров нет — верни пустой список.


Товары (JSON):
{products_json}
"""


SEARCH_PLAN_PROMPT = """Сформулируй короткий поисковый запрос (2–6 слов) для поиска
{goal} того же типа товара в каталоге AliExpress.


Используй только слова из названия товара и его реальных данных.
Верни ТОЛЬКО строку запроса, без кавычек, без пояснений, на языке каталога
(английский или русский), без цен, без ссылок и без названий магазинов.


Данные товара (JSON):
{product_json}
"""


QUESTION_PROMPT = """Ответь на вопрос покупателя о товарах, используя ТОЛЬКО переданные данные.


Правила:
- опирайся только на поля из JSON;
- если в данных нет ответа — верни ровно: нет данных
- не выдумывай характеристики, качество, материалы, доставку или ссылки;
- ответ 1–3 предложения, по-русски, без markdown.


Вопрос: {question}


Товары (JSON):
{products_json}
"""




MERGE_PROMPT = """Пользователь УТОЧНЯЕТ ранее выполненный поиск товаров.


Предыдущий запрос (JSON):
{previous_json}


Новое сообщение пользователя:
{follow_up}


Разбор нового сообщения (JSON, только явно указанные значения):
{parsed_json}


Верни ТОЛЬКО JSON-объект с полями запроса поиска (теми же, что в предыдущем JSON),
применив уточнение пользователя.


Правила:
- товар остаётся тем же: normalized_query не должен описывать другой товар;
- используй только слова из предыдущего запроса и нового сообщения;
- новые числа, характеристики и категории не выдумывай;
- если пользователь назвал новый бюджет или рейтинг, замени соответствующее поле;
- если пользователь просит характеристику ("с микрофоном") — добавь её в
  required_features; если просит без неё ("без провода") — в excluded_features;
- поля, о которых речи не было, оставь как в предыдущем запросе.
"""


#: Bounded turn planner for the conversational router. It interprets each
#: user message against the active ledger and may ask one high-value question;
#: its output is restricted to a validated action, product type and question.
CONVERSATION_TURN_PROMPT = """Ты — управляющий диалогом консультанта магазина. Проанализируй НОВОЕ сообщение вместе с контекстом предыдущих сообщений и активных ограничений.

Контекст диалога (JSON):
{context}

Новое сообщение:
{message}

Верни только один JSON-объект со строго такими полями:
{{"action":"new_search|refine|correction|alternatives|compare|clarify","confidence":0.0,"product_type":null,"question":null}}

Правила:
- new_search: пользователь хочет искать другой товар или впервые назвал конкретный товар. product_type — только ядро названия товара, без бюджета, количества, цвета, функций и слов «найди/покажи». Пример: «нужен недорогой шуруповёрт с двумя аккумуляторами» → «шуруповёрт».
- refine: пользователь уточняет активный поиск (бюджет, совместимость, цвет, характеристики, «подешевле», «полегче»); сохраняй прежний товар.
- correction: пользователь отменяет или заменяет ранее заданное ограничение.
- alternatives: просит другие варианты того же товара.
- compare: спрашивает о ранее показанных товарах или просит их сравнить/объяснить.
- clarify: товар/цель неясны настолько, что честный поиск невозможен; задай один короткий вопрос на языке пользователя.
- Если одно название товара охватывает существенно разные классы применения и без этого уточнения выдача может оказаться нерелевантной, выбери clarify и задай ОДИН вопрос о задаче/условиях использования. Например: сварочный аппарат — для дома/гаража или для профессионального производства; компрессор — для бытовых задач или промышленный. Не задавай такой вопрос, если пользователь уже указал применение или оно однозначно из запроса.
- Не считай аксессуары и смежные товары тем же товаром только из-за общего прилагательного или слова в названии: «сварочная маска» — не «сварочный аппарат».
- «до 2000 тысяч гривен» — это бюджетное уточнение, не название товара. Не превращай суммы и валюты в product_type.
- «расскажи про шуруповёрт» — новый поиск/вопрос о названном товаре, а не сравнение с предыдущей выдачей, если не указано, что речь о показанных карточках.
- Не выдумывай характеристики и не меняй продукт при обычном уточнении.
- confidence — твоя уверенность от 0 до 1. Если уверенность ниже 0.65, выбери clarify и объясни одним вопросом.
- product_type — null для refine, correction, alternatives, compare и clarify.
- Это решение только о диалоге. Не выбирай товары и не выдумывай цены/ссылки.
"""


CHAT_ACTION_PROMPT = """Пользователь написал очень короткое сообщение в магазине товаров.


Сообщение пользователя:
{message}


Определи, хватает ли уже информации, чтобы искать товар, или нужно задать
ОДИН короткий уточняющий вопрос.


Верни ТОЛЬКО JSON-объект:
- если товар/задача уже понятны и можно искать: {{"action": "search"}}
- если сообщение слишком общее и без уточнения поиск бессмыслен:
  {{"action": "clarify", "question": "<один короткий вопрос на языке пользователя>"}}


Правила:
- если в сообщении уже назван товар или задача — всегда "search";
- НЕ придумывай товар и НЕ додумывай за пользователя;
- вопрос задавай только если он реально уточнит выбор (что за товар и зачем);
- не больше одного вопроса.
"""








def _merge_terms(request: SearchRequest) -> list[str]:
    """Significant words of a request, used to validate an AI merge."""


    text = f"{request.normalized_query or ''} {request.original_query or ''}"
    return [word.lower() for word in re.findall(r"[^\W\d_]+", text) if len(word) >= 3]




def _word_stems(text: str | None) -> set[str]:
    """5-character stems of the content words of ``text``.


    The very shape the merge word-gate compares (``word[:5]``), so
    «микрофон» and «микрофоном» share «микро» while «проводной» and
    «беспроводной» stay apart."""


    return {
        word.lower()[:5]
        for word in re.findall(r"[^\W\d_]+", text or "")
        if len(word) >= 3
    }




def _phrase_nouns(text: str | None) -> set[str]:
    """The product-noun words of ``text`` in either script.


    ``services.relevance.noun_terms`` drops exactly the words that can
    never head a product phrase (function words, scaffolding, adjectives),
    so this is the product core of a retrieval phrase - the part an AI
    merge may never drop, while «нужны» or a reordered adjective may go."""


    if not text:
        return set()
    return set(noun_terms(text, "cyrillic")) | set(noun_terms(text, "latin"))




def _same_identity_item(left: str, right: str) -> bool:
    """Whether two structured-intent items mean the SAME requirement.


    Exact wording, containment («65W» / «65W fast charge») and shared word
    stems («чехол» / «чехол для телефона») all count as one item; an empty
    phrase never matches anything, so a blank value cancels nothing."""


    left_normalized = (left or "").strip().lower()
    right_normalized = (right or "").strip().lower()
    if not left_normalized or not right_normalized:
        return False
    if (
        left_normalized == right_normalized
        or left_normalized in right_normalized
        or right_normalized in left_normalized
    ):
        return True
    return bool(_word_stems(left_normalized) & _word_stems(right_normalized))




def _all_words_known(item: str, known_stems: set[str], known_text: str) -> bool:
    """Whether every content word of a candidate intent ``item`` already
    exists in the two requests (the anti-invention rule of Stage 3C).


    Spec tokens without a content word of their own («65W») fall back to
    an exact substring check against the same known text."""


    stems = _word_stems(item)
    if stems:
        return stems <= known_stems
    return (item or "").strip().lower() in known_text



# ---------------------------------------------------------------------------
# Deterministic (Gemini-free) query parsing
# ---------------------------------------------------------------------------


#: Currency markers the LOCAL parser recognizes: symbol or word -> code.
#: Kept in sync with ``services.query_intent.CURRENCY_ALIASES`` by reusing it,
#: so both parsers can never disagree about which currencies exist.
_CURRENCY_MARKERS: tuple[tuple[str, str], ...] = tuple(
    sorted(
        (
            (marker, code)
            for marker, code in CURRENCY_ALIASES.items()
            if code
        ),
        key=lambda item: len(item[0]),
        reverse=True,
    )
)


_NUMBER = r"\d+(?:[.,]\d+)?"


def _currency_pattern() -> re.Pattern[str]:
    """One alternation of every known currency marker, longest first.

    Word markers require word boundaries ("dollarstore" must not hit);
    symbol markers are bounded only against letters/digits, because "$",
    "€" etc. are not ``\\w`` and would otherwise never match.
    """

    symbols = [
        marker
        for marker, _code in _CURRENCY_MARKERS
        if len(marker) == 1 and not marker.isalnum()
    ]
    words = [
        marker
        for marker, _code in _CURRENCY_MARKERS
        if marker not in symbols
    ]
    symbol_pat = (
        r"(?<![0-9a-zа-я])(" + "|".join(re.escape(s) for s in symbols) + r")"
        if symbols
        else r"(?!x)x"
    )
    word_pat = (
        r"(?<!\w)(" + "|".join(re.escape(w) for w in words) + r")(?!\w)"
    )
    return re.compile(r"(?i)(?:" + symbol_pat + r"|" + word_pat + r")")


_CURRENCY_RE = _currency_pattern()


def _detect_currency(text: str) -> str | None:
    """The currency explicitly named in ``text``, or ``None``.

    Word boundaries prevent false hits like "dollarstore"; the first
    marker (longest alternation first) wins, mirroring the intent parser.
    """

    match = _CURRENCY_RE.search(text or "")
    if not match:
        return None
    return normalize_currency(match.group(0))


#: "N pieces / N шт" is UNITS inside a product, never how many cards.
_QUANTITY_PATTERN = re.compile(
    r"(?i)(?<!\d)(\d{1,6})\s*(?:шт|pieces?|pcs|units?)\b"
)


def _match_int(text: str, pattern: re.Pattern[str]) -> int | None:
    """The first integer match of ``pattern`` in ``text`` (or ``None``)."""

    match = pattern.search(text or "")
    if not match:
        return None
    try:
        return int(match.group(1).replace(" ", ""))
    except (TypeError, ValueError):
        return None


def _match_number(text: str, pattern: re.Pattern[str]) -> float | None:
    """The first numeric match of ``pattern`` in ``text`` (or ``None``)."""

    match = pattern.search(text or "")
    if not match:
        return None
    raw = match.group(1).replace(" ", "").replace(",", ".")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None



def _marker_number_pattern(markers: tuple[str, ...], *, number_first: bool = False) -> re.Pattern[str]:
    """``<marker><number>`` (or ``<number><marker>``) for one marker set."""

    alternation = "|".join(
        re.escape(marker) for marker in sorted(markers, key=len, reverse=True)
    )
    if number_first:
        return re.compile(
            r"(?i)\b(" + _NUMBER + r")\s*(?:" + alternation + r")"
        )
    return re.compile(
        r"(?i)(?:" + alternation + r")[\s\u00a0]*(" + _NUMBER + r")"
    )


_MIN_RATING_PATTERN = _marker_number_pattern(
    ("rating above", "рейтинг выше", "рейтингом выше", "отзывов выше"),
)
_MIN_ORDERS_PATTERN = _marker_number_pattern(
    ("orders", "заказов", "заказа"), number_first=True,
)
#: A currency symbol glued to the number («$30», «до 20 €») is itself the
#: marker: the budget is explicit even without «до/under».
_MAX_SYMBOL_PATTERN = re.compile(
    r"(?i)(?<![0-9a-zа-я])[$\u20ac\u20bd\u00a5\u00a3][\s\u00a0]*(" + _NUMBER + r")"
)
_MAX_PRICE_PATTERN = _marker_number_pattern(("до", "under", "меньше чем"))
_MIN_PRICE_PATTERN = _marker_number_pattern(("от", "over", "больше чем"))
#: A currency symbol glued to the number ("headphones over €30") is itself
#: the marker: the floor is explicit without a word marker.
_MIN_SYMBOL_PATTERN = re.compile(
    r"(?i)(?<![0-9a-zа-я])[$\u20ac\u20bd\u00a5\u00a3][\s\u00a0]*(" + _NUMBER + r")"
)



def _strip_constraint_phrases(text: str) -> str:
    """Remove explicit constraint phrases from a retrieval phrase.

    Price bounds, currency words, rating/orders clauses, marketplace names,
    option-count framing and quality-rating wishes never belong in a
    marketplace full-text query: the live index answers an EMPTY result to
    phrases like «с хорошими отзывами». The product words stay.
    """

    cleaned = text or ""
    cleaned = _MIN_RATING_PATTERN.sub(" ", cleaned)
    cleaned = _MIN_ORDERS_PATTERN.sub(" ", cleaned)
    cleaned = _MAX_PRICE_PATTERN.sub(" ", cleaned)
    cleaned = _MIN_PRICE_PATTERN.sub(" ", cleaned)
    cleaned = _MAX_SYMBOL_PATTERN.sub(" ", cleaned)
    cleaned = _CURRENCY_RE.sub(" ", cleaned)
    for phrase in (
        "с хорошими отзывами",
        "с хорошим рейтингом",
        "с хорошими оценками",
        "с хорошим отзывом",
        "хорошего качества",
        "качественный",
        "хороший рейтинг",
        "хорошими отзывами",
        "хорошим рейтингом",
        "good quality",
        "with good reviews",
        "with good rating",
        "good reviews",
        "good rating",
        "алиэкспресс",
        "али экспресс",
        "aliexpress",
    ):
        cleaned = re.sub(
            r"(?i)(?<!\w)" + re.escape(phrase) + r"(?!\w)", " ", cleaned
        )
    cleaned = strip_option_count(cleaned)
    # A marker whose number was followed by a currency word («до 80 долларов»)
    # leaves the bare marker behind once the currency word is gone.
    cleaned = re.sub(
        r"(?i)(?<!\w)(?:до|от|under|over|меньше чем|больше чем)(?!\w)", " ", cleaned
    )
    cleaned = re.sub(r"[\d.,]+", " ", cleaned)
    cleaned = re.sub(r"[\s,;:]+", " ", cleaned).strip(" ,.;:-")
    return re.sub(r"\s+", " ", cleaned).strip()



def _detect_sort_preference(text: str) -> SortPreference:
    """The explicit sort wording of ``text``, or the default.

    Only unambiguous product-side wordings are recognized; anything else
    stays ``RELEVANCE`` so the bot never invents a preference.
    """

    lowered = (text or "").lower()
    if re.search(r"(?i)(?:сам[а-я]*|the)\s?(?:дешев|cheap)", lowered) or re.search(
        r"(?i)\bдешевле\b", lowered
    ):
        return SortPreference.PRICE_ASC
    if re.search(r"(?i)(?:сам[а-я]*|the)\s?(?:дорог|expensive)", lowered):
        return SortPreference.PRICE_DESC
    return SortPreference.RELEVANCE



def _has_fuzzy_quality_phrase(text: str) -> bool:
    """Whether ``text`` asks for good quality WITHOUT a number.

    Such a phrase is a ranking-only wish (``prefers_good_rating``): the
    filter never becomes active from it, because inferring a minimum
    rating from words would be guessing.
    """

    return bool(
        re.search(
            r"(?i)(?:с хорош|good|great|качественн)",
            (text or "").lower(),
        )
    )



def _split_sentences(text: str) -> list[str]:
    """Sentences of ``text`` split on the usual sentence punctuation."""

    return [
        part for part in re.split(r"(?<=[.!?…])\s+", text or "") if part.strip()
    ]



class AIService:
    """Thin async wrapper around the Gemini API with a safe fallback path."""


    def __init__(
        self,
        api_key: str | None,
        model: str = DEFAULT_MODEL,
        timeout_seconds: float = 20.0,
        *,
        smart_model: str | None = None,
        fast_model: str | None = None,
        vision_model: str | None = None,
        fallback_model: str | None = None,
    ) -> None:
        #: The "smart" model. ``model`` stays the first positional argument
        #: (and the value /admin reports) so existing callers are unchanged.
        self.model = model
        self.smart_model = smart_model or model
        self.fast_model = fast_model or DEFAULT_FAST_MODEL
        self.vision_model = vision_model or DEFAULT_VISION_MODEL
        self.fallback_model = fallback_model or DEFAULT_FALLBACK_MODEL
        self.timeout_seconds = timeout_seconds
        self._api_key = api_key
        self._client = None


        #: Safe diagnostics for the admin "AI" screen (§48): counters and
        #: statuses only - never the key, never raw prompts.
        self.parse_ok = 0
        self.parse_fallback = 0
        self.rank_requests = 0
        self.rank_fallback = 0
        self.image_requests = 0
        self.generate_ok = 0
        self.generate_failed = 0
        #: Which model produced the LAST successful answer.
        self.last_model: str | None = None


        if api_key:
            try:
                from google import genai


                self._client = genai.Client(api_key=api_key)
            except Exception:  # pragma: no cover - import/config failure only
                logger.exception(
                    "Gemini client initialization failed; falling back to local parsing"
                )
                self._client = None
        else:
            logger.warning(
                "GEMINI_API_KEY is not configured: natural-language parsing "
                "will use the local fallback parser"
            )


    @property
    def is_available(self) -> bool:
        return self._client is not None


    async def parse_query(self, user_text: str) -> SearchRequest:
        """Turn a free-form user message into a structured search request.


        Always returns a usable ``SearchRequest``: if Gemini is
        unavailable or returns unusable output, the deterministic
        fallback parser is used instead of failing the user's message.
        """


        text = (user_text or "").strip()
        if not text:
            raise ValueError("Cannot parse an empty search query")


        if not self.is_available:
            self.parse_fallback += 1
            return self._with_result_limit(text, self._fallback_parse(text))


        prompt = QUERY_PARSING_PROMPT.format(user_text=text)
        raw = await self._generate(prompt, models=(self.fast_model,))
        if raw is None:
            self.parse_fallback += 1
            return self._with_result_limit(text, self._fallback_parse(text))


        payload = self._extract_json_object(raw)
        if payload is None:
            logger.warning("Gemini returned non-JSON search parameters; using fallback parser")
            self.parse_fallback += 1
            return self._with_result_limit(text, self._fallback_parse(text))


        self.parse_ok += 1
        return self._with_result_limit(text, self._to_search_request(payload, fallback_text=text))


    async def rank_candidates(
        self, candidates: list[Product], *, query: str | None = None
    ) -> list[str] | None:
        """Ask Gemini to SELECT and order already-retrieved candidate ids.


        Used by the KEYWORD-driven discovery feed (``find_discovery``): the
        candidates themselves are real marketplace products - the model picks
        which of them are worth showing and in which order. The popular feed does
        NOT use this: its order is the marketplace's own signal
        (``LAST_VOLUME_DESC`` plus ``popularity_score``), which keeps that feed
        reproducible.


        ``query`` is the user's own wording; it travels into the prompt so the
        model can drop accessories and spare parts that a broad keyword returns
        for an actual product (see :data:`STRICT_FILTER_RULES`).


        Returns the ids worth showing, at most :data:`TOP_CANDIDATES` of them,
        or ``None`` (callers keep the deterministic ranking) when:


        - Gemini is unavailable or returns nothing usable;
        - the response is not a JSON object with an ``order`` list;
        - the list contains no usable strings at all.


        The returned list is NOT yet trusted: ``feed_curation.apply_selection``
        validates every id against the real candidate set, so a hallucinated id
        can neither enter nor mutate the feed.
        """


        if not self.is_available or not candidates:
            self.rank_fallback += 1
            return None


        self.rank_requests += 1
        products_json = json.dumps(
            [self._rank_payload(product) for product in candidates],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        raw = await self._generate(
            RANK_PROMPT.format(
                query=query or "не указан",
                rules=STRICT_FILTER_RULES,
                products_json=products_json,
            ),
            models=(self.fast_model,),
        )
        if raw is None:
            self.rank_fallback += 1
            return None


        payload = self._extract_json_object(raw)
        if payload is None:
            logger.warning("Gemini returned non-JSON ranking; keeping feed order")
            self.rank_fallback += 1
            return None


        ids = self._clean_ids(payload.get("order"))
        if not ids:
            logger.warning("Gemini ranking order is empty; keeping feed order")
            self.rank_fallback += 1
            return None
        return ids


    async def describe_image_query(self, image_bytes: bytes) -> str | None:
        """Turn a photo into a SHORT product-search query via Gemini vision.


        Gemini only NAMES what is on the picture - it never invents
        products, prices or links. The returned query is later executed
        as a normal AliExpress search (see handlers.discovery). Returns
        ``None`` when Gemini is unavailable, fails, or reports that there
        is no product on the picture.
        """


        if self._client is None:
            return None


        self.image_requests += 1
        try:
            from google.genai import types as genai_types
        except Exception:  # pragma: no cover - SDK missing/failed to import
            logger.exception("Photo recognition unavailable: genai SDK is missing")
            return None


        described = await self._generate(
            [
                genai_types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                IMAGE_QUERY_PROMPT,
            ],
            models=(self.vision_model,),
        )
        if described is None:
            logger.warning("Photo recognition produced no answer")
            return None


        query = " ".join(described.split()).strip(" \t\"'«»")
        lowered = query.lower()
        if not query or lowered == "пусто" or len(query) > 120:
            logger.info("Gemini vision produced no usable product query")
            return None
        return query


    async def analyze_image(self, image_bytes: bytes) -> dict[str, str] | None:
        """Structured understanding of what ONE photo really shows (§7).


        Returns a validated ``{field: value}`` analysis (product type,
        brand, model, readable packaging text, colour, material, ...) built
        ONLY from what is visible or readable on the picture - never a
        product, a price or a link, and never a guess: an answer without a
        product type/name/readable text is rejected (see
        :func:`_validated_image_analysis`). ``None`` means the photo could
        not be understood (no client, no answer, unusable JSON) and the
        caller shows the short human fallback.


        This is the photo pipeline's only vision step: the handler turns
        the analysis into a few grounded search hypotheses and runs them
        through the ordinary AliExpress search (``handlers.discovery``).
        """


        if self._client is None:
            return None
        self.image_requests += 1
        try:
            from google.genai import types as genai_types
        except Exception:  # pragma: no cover - SDK missing/failed to import
            logger.exception("Photo analysis unavailable: genai SDK is missing")
            return None


        raw = await self._generate(
            [
                genai_types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                IMAGE_ANALYSIS_PROMPT,
            ],
            models=(self.vision_model,),
        )
        if raw is None:
            logger.warning("Image analysis produced no answer")
            return None


        payload = self._extract_json_object(raw)
        analysis = (
            _validated_image_analysis(payload) if payload is not None else None
        )
        if analysis is None:
            logger.info("Image analysis was unusable JSON or named no product")
        return analysis


    # -- product assistant operations (§7-§13) ------------------------------


    async def analyze_product(self, product: Product) -> dict | None:
        """Validated analysis items for ONE real product, or ``None``.


        Returns ``{"observations": [...], "recommendation": str | None}``
        built ONLY from fields the marketplace actually reported. Any item
        that mentions a number absent from those fields is dropped (see
        :meth:`_validated_ai_text`), so a hallucinated figure can never
        reach the user; the caller renders the result together with the
        factual block it already has.
        """


        if not self.is_available:
            return None


        facts = product_facts(product)
        raw = await self._generate(
            ANALYSIS_PROMPT.format(
                product_json=json.dumps(facts, ensure_ascii=False, indent=2)
            ),
            models=(self.smart_model,),
        )
        payload = self._extract_json_object(raw) if raw else None
        if not isinstance(payload, dict):
            logger.warning("AI analysis was not usable JSON; using the factual summary")
            return None


        observations: list[str] = []
        raw_items = payload.get("observations")
        if isinstance(raw_items, list):
            for item in raw_items:
                if not isinstance(item, str):
                    continue
                validated = self._validated_ai_text(item, facts)
                if validated:
                    observations.append(validated)
        raw_recommendation = payload.get("recommendation")
        recommendation = (
            self._validated_ai_text(raw_recommendation, facts)
            if isinstance(raw_recommendation, str)
            else None
        )


        if not observations and not recommendation:
            return None
        return {"observations": observations[:3], "recommendation": recommendation}


    async def select_candidates(
        self, goal: str, product: Product, candidates: list[Product]
    ) -> list[str] | None:
        """Ordered candidate ids chosen by AI, or ``None``.


        The model judges the pool by the shared strict rules
        (:data:`STRICT_FILTER_RULES`): a listing rated below 4.5, an accessory
        that is not the searched product, an anomalously cheap spam item or a
        duplicate is dropped, and at most :data:`TOP_CANDIDATES` best positions
        survive (the cap is enforced HERE as well as in the prompt).


        The ids are NOT trusted here: ``services.product_assistant`` maps
        them onto the candidate set it actually sent, and an id the model
        invented is dropped there. This method only rejects a response that
        is not a list of non-empty strings.
        """


        if not self.is_available or not candidates:
            return None


        products_json = json.dumps(
            [self._rank_payload(candidate) for candidate in candidates],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        raw = await self._generate(
            SELECT_PROMPT.format(
                goal=GOAL_DESCRIPTIONS.get(goal, "подобрать подходящий вариант"),
                query=product.title if product is not None else "не указан",
                rules=STRICT_FILTER_RULES,
                products_json=products_json,
            ),
            models=(self.fast_model,),
        )
        payload = self._extract_json_object(raw) if raw else None
        if not isinstance(payload, dict):
            return None
        return self._clean_ids(payload.get("ids"))


    async def select_feed_candidates(
        self,
        candidates: list[Product],
        *,
        wanted: int = TOP_CANDIDATES,
        query: str | None = None,
    ) -> list[str] | None:
        """Strict selection of the cards a FEED shows (the carousel).


        The pool handed in is the RAW marketplace answer (``search_service``
        gathers up to ``RAW_POOL_TARGET`` of them, i.e. 60-80 listings), because
        choosing the best 20 out of 20 is not a selection at all. The model drops
        junk by the shared rules of :data:`STRICT_FILTER_RULES` - low-rated
        listings, accessories that are not the searched product, anomalously
        cheap spam, duplicates - and returns at most ``wanted`` (20 by default)
        positions ordered by relevance.


        Returns the chosen ids (never more than ``wanted``), or ``None`` when
        Gemini is unavailable, fails or answers with nothing usable: the caller
        then keeps its deterministic list, so an AI outage never empties a feed.


        The ids are NOT trusted here either: ``feed_curation.apply_selection``
        maps them onto the pool that was actually sent, so an invented id can
        never reach a card, and a product the model left out simply is not shown.


        ``query`` is the user's own wording (the search phrase or the category
        title); it is what makes "not the right product" decidable in the first
        place. Counters are shared with :meth:`rank_candidates`
        (``rank_requests``/``rank_fallback``): both are the same "order and
        filter real candidates" role, and the admin screen reports it as such.
        """


        if not self.is_available or not candidates:
            self.rank_fallback += 1
            return None


        limit = max(1, int(wanted or TOP_CANDIDATES))
        self.rank_requests += 1

        products_json = json.dumps(
            [self._rank_payload(candidate) for candidate in candidates],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        raw = await self._generate(
            SELECT_PROMPT.format(
                goal=FEED_SELECTION_GOAL,
                query=query or "не указан",
                rules=STRICT_FILTER_RULES,
                products_json=products_json,
            ),
            models=(self.fast_model,),
        )
        payload = self._extract_json_object(raw) if raw else None
        if not isinstance(payload, dict):
            logger.warning(
                "AI feed selection was unusable; keeping the deterministic list"
            )
            self.rank_fallback += 1
            return None
        ids = self._clean_ids(payload.get("ids"), limit=limit)
        if not ids:
            logger.warning("AI feed selection was empty; keeping the deterministic list")
            self.rank_fallback += 1
            return None
        return ids

    async def select_conversation_candidates(
        self,
        candidates: list[Product],
        *,
        query: str,
        wanted: int = TOP_CANDIDATES,
    ) -> list[str] | None:
        """Select relevant conversation-search candidates using real product rows.

        Returns only ids from the supplied pool. None means the model call or
        response was unusable; an empty list is a valid no-match decision.
        """
        if not self.is_available or not candidates:
            return None

        limit = max(1, min(int(wanted or TOP_CANDIDATES), TOP_CANDIDATES))
        products_json = json.dumps(
            [self._rank_payload(candidate) for candidate in candidates],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        rules = """- Сначала определи, является ли позиция ИМЕННО искомым товаром, а не аксессуаром, запчастью, расходником или соседней категорией.
- Не принимай товар за подходящий только потому, что в названии совпадает прилагательное или часть названия. Например, сварочная маска не является сварочным аппаратом.
- Учитывай явно указанные в запросе назначение и класс применения. Бытовой товар не заменяй промышленным оборудованием и наоборот.
- Если назначение не указано, не выдумывай его по цене, рейтингу или популярности.
- Рейтинг и количество заказов помогают упорядочить уже подходящие товары, но не превращают неподходящий товар в подходящий.
- Не выдумывай характеристики, которых нет в данных.
- Верни не более указанного количества лучших подходящих позиций; если подходящих нет, верни пустой список."""
        raw = await self._generate(
            SELECT_PROMPT.format(
                goal="отобрать только подходящие товары для текущего диалога",
                query=query[:500],
                rules=rules,
                products_json=products_json,
            ),
            models=(self.fast_model,),
            operation="conversation_selection",
        )
        payload = self._extract_json_object(raw) if raw else None
        if not isinstance(payload, dict) or not isinstance(payload.get("ids"), list):
            return None

        allowed = {
            str(candidate.product_id)
            for candidate in candidates
            if candidate.product_id
        }
        selected: list[str] = []
        for raw_id in payload["ids"]:
            if not isinstance(raw_id, str):
                continue
            product_id = raw_id.strip()
            if product_id in allowed and product_id not in selected:
                selected.append(product_id)
        return selected[:limit]

    async def plan_product_search(self, product: Product, goal: str) -> str | None:
        """A short search wording for a candidate search, or ``None``.

        Only a plain, short, link-free line is accepted: the value is used
        as marketplace keywords, so anything else would either waste a
        request or smuggle text into the catalog query.
        """

        if not self.is_available:
            return None

        facts = product_facts(product)
        raw = await self._generate(
            SEARCH_PLAN_PROMPT.format(
                goal=GOAL_DESCRIPTIONS.get(goal, "похожих товаров"),
                product_json=json.dumps(facts, ensure_ascii=False),
            ),
            models=(self.fast_model,),
        )
        if not raw:
            return None

        planned = " ".join(raw.split()).strip(" \t\"'«»`")
        planned = planned.splitlines()[0].strip() if planned else ""
        if not planned or len(planned) > 100 or "http" in planned.lower():
            logger.warning("AI search wording rejected; using the deterministic intent")
            return None
        return planned

    async def answer_product_question(
        self, question: str, products: list[Product]
    ) -> str | None:
        """A validated answer about real products, or ``None``.

        The model may only mention numbers that exist in the supplied
        product data; the documented "нет данных" answer becomes ``None``,
        so the caller shows its own honest message instead.
        """

        if not self.is_available or not products or not (question or "").strip():
            return None

        payloads = [
            product_facts(product) for product in products[:MAX_PRODUCTS_IN_PROMPT]
        ]
        raw = await self._generate(
            QUESTION_PROMPT.format(
                question=question.strip()[:300],
                products_json=json.dumps(payloads, ensure_ascii=False, indent=2),
            ),
            models=(self.smart_model,),
        )
        text = self._validated_sentences(raw, payloads)
        if not text:
            return None
        if text.strip().strip(".").lower() == "нет данных":
            return None
        return text

    def _validated_sentences(
        self, text: str | None, facts: object
    ) -> str | None:
        """Keep only the sentences whose numbers all exist in ``facts``.

        A language model writes prose, not a database row: one stray figure
        (a rounded price, a guess at delivery time) must not discard an
        otherwise correct answer. The gate therefore works per sentence -
        the unsupported ones are dropped and the verified ones are kept. If
        NOTHING survives, the caller still receives ``None`` and shows its
        honest "no such data" message, so an invented answer can never slip
        through by being surrounded by correct ones.
        """

        if not text or not text.strip():
            return None

        allowed = numeric_tokens(json.dumps(facts, ensure_ascii=False, default=str))
        kept: list[str] = []
        dropped: list[str] = []
        for sentence in _split_sentences(text):
            numbers = set(re.findall(r"\d+(?:[.,]\d+)?", sentence))
            unknown = {n for n in numbers if n not in allowed}
            if not unknown:
                kept.append(sentence.strip())
            else:
                dropped.append(sentence.strip())
        if dropped:
            logger.warning(
                "AI answer mentioned numbers absent from the supplied data "
                "(%s); the unsupported sentences are dropped",
                ", ".join(sorted(unknown)[:5]),
            )
        if kept:
            # Verified sentences survive a stray figure: one hallucinated
            # number in an unrelated sentence must not make the whole answer
            # (and the feature) vanish - that was the live failure.
            return " ".join(kept)
        return None

    async def decide_conversation_turn(
        self, user_text: str, *, context: dict | None = None
    ) -> dict | None:
        """Interpret one message against the active shopping conversation.

        Gemini chooses the conversational action and, for a new search, the
        core product type. The caller validates the action against its fixed
        enum and keeps deterministic slot extraction, retrieval, product
        validation, ranking and Telegram delivery. Any model failure returns
        None so the existing deterministic classifier remains the fallback.
        """
        if not self.is_available or not (user_text or "").strip():
            return None

        context_json = json.dumps(context or {}, ensure_ascii=False, separators=(",", ":"))
        prompt = CONVERSATION_TURN_PROMPT.format(
            context=context_json[:4000],
            message=(user_text or "").strip()[:500],
        )
        try:
            # Conversational interpretation must not make every product
            # search wait for the general 20-second AI timeout. If Gemini is
            # slow, cancel this bounded attempt and use the deterministic
            # state machine for the same turn.
            async with asyncio.timeout(min(self.timeout_seconds, 5.0)):
                raw = await self._generate(
                    prompt,
                    models=(self.fast_model,),
                    operation="conversation_turn",
                )
        except TimeoutError:
            logger.info("Gemini turn interpretation timed out; using deterministic intent")
            return None
        payload = self._extract_json_object(raw) if raw else None
        if not isinstance(payload, dict):
            return None

        action = str(payload.get("action") or "").strip().lower()
        allowed = {
            "new_search", "refine", "correction", "alternatives",
            "compare", "clarify",
        }
        if action not in allowed:
            return None
        try:
            confidence = float(payload.get("confidence", 0))
        except (TypeError, ValueError):
            return None
        if not 0.0 <= confidence <= 1.0:
            return None
        if confidence < 0.65 and action != "clarify":
            return None

        product_type = payload.get("product_type")
        if product_type is not None:
            if action != "new_search":
                return None
            if not isinstance(product_type, str):
                return None
            product_type = " ".join(product_type.split()).strip()
            if len(product_type) > 100:
                return None
            if not product_type:
                product_type = None

        question = payload.get("question")
        if question is not None and not isinstance(question, str):
            return None
        question = question.strip()[:240] if isinstance(question, str) else ""
        if action == "clarify" and not question:
            return None

        return {
            "action": action,
            "confidence": confidence,
            "product_type": product_type,
            "question": question or None,
        }

    async def decide_chat_action(self, user_text: str) -> dict | None:
        """Decide ONLY whether a short vague message needs a clarifying
        question, or ``None``.

        This is the single, narrowly-scoped Gemini call behind
        ``services.convsearch.engine``. The model returns a JSON object with
        ``action`` ("search" or "clarify") and, for "clarify", one short
        ``question``. It is validated hard: anything other than a usable
        "clarify" answer (or a plain "search") yields ``None`` so the caller
        keeps its deterministic route. The model can NEVER name a product,
        change a search or bypass any filter here - at most it asks the user
        one question.
        """

        if not self.is_available or not (user_text or "").strip():
            return None

        raw = await self._generate(
            CHAT_ACTION_PROMPT.format(message=user_text.strip()[:300]),
            models=(self.fast_model,),
        )
        payload = self._extract_json_object(raw) if raw else None
        if not isinstance(payload, dict):
            return None

        action = str(payload.get("action") or "").strip().lower()
        if action == "clarify":
            question = payload.get("question")
            question = question.strip() if isinstance(question, str) else ""
            return {"action": "clarify", "question": question}
        if action == "search":
            return {"action": "search"}
        return None

    # -- internals ----------------------------------------------------------

    async def _generate(
        self,
        contents: object,
        *,
        models: tuple[str, ...] | None = None,
        operation: str = "generate",
    ) -> str | None:
        """Call Gemini, returning text or ``None`` on any failure.

        ``models`` is the model chain for this call: the role's primary
        model first, then (implicitly) the configured fallback model. The
        chain is deliberately short and quota-aware:

        - a **quota/rate-limit** error stops immediately - the limit is
          account-wide and trying more models would only waste quota;
        - a **model-unavailable** error (404/503) moves on to the fallback
          model, which is exactly what it exists for;
        - every other error (auth, invalid request, server, timeout,
          network, safety block) is logged and swallowed: the bot must
          never crash because Gemini is down.

        Every failed attempt logs ONE detail fragment built by
        ``_format_gemini_error``: model, exception class, HTTP status,
        structured API status, classification, whether the fallback will be
        tried and a sanitized/truncated message. Prompts, request contents
        and credentials are never logged.
        """

        if self._client is None:
            return None

        chain = list(models or (self.fast_model,))
        if self.fallback_model and self.fallback_model not in chain:
            chain.append(self.fallback_model)

        for index, model in enumerate(chain):
            # Latency diagnostics: how long THIS attempt took (success or
            # failure). Model names and durations only - never prompts,
            # request contents or credentials.
            attempt_started = time.perf_counter()
            try:
                async with asyncio.timeout(self.timeout_seconds):
                    response = await self._client.aio.models.generate_content(
                        model=model, contents=contents
                    )
            except Exception as exc:
                attempt_ms = (time.perf_counter() - attempt_started) * 1000
                logger.info(
                    "Gemini attempt failed: model=%s dur_ms=%.0f limit_s=%.1f",
                    model,
                    attempt_ms,
                    self.timeout_seconds,
                )
                # Measurement only: per-attempt record + turn aggregate.
                record_attempt(
                    operation=operation,
                    model=model,
                    duration_ms=attempt_ms,
                    outcome=(
                        "timeout"
                        if _classify_gemini_error(exc) == "timeout"
                        else "error"
                    ),
                    error=True,
                )
                kind = _classify_gemini_error(exc)
                will_fallback = kind == "model" and index + 1 < len(chain)
                detail = _format_gemini_error(
                    exc,
                    model=model,
                    kind=kind,
                    will_fallback=will_fallback,
                    api_key=self._api_key,
                )
                if kind == "quota":
                    logger.warning(
                        "Gemini quota/rate limit reached; not trying another model"
                        " (%s)",
                        detail,
                    )
                    self.generate_failed += 1
                    return None
                if will_fallback:
                    logger.warning(
                        "Gemini model %s is unavailable; trying %s (%s)",
                        model,
                        chain[index + 1],
                        detail,
                    )
                    continue
                logger.exception(
                    "Gemini request failed; continuing without AI assistance (%s)",
                    detail,
                )
                self.generate_failed += 1
                return None

            text = getattr(response, "text", None)
            if not text:
                attempt_ms = (time.perf_counter() - attempt_started) * 1000
                logger.warning("Gemini returned an empty response")
                record_attempt(
                    operation=operation,
                    model=model,
                    duration_ms=attempt_ms,
                    outcome="empty",
                    response=response,
                )
                self.generate_failed += 1
                return None

            attempt_ms = (time.perf_counter() - attempt_started) * 1000
            logger.info(
                "Gemini attempt ok: model=%s dur_ms=%.0f",
                model,
                attempt_ms,
            )
            # Measurement only: per-attempt record + turn aggregate. Token
            # counts come only from response.usage_metadata; when the SDK
            # exposes none the record says so instead of guessing.
            record_attempt(
                operation=operation,
                model=model,
                duration_ms=attempt_ms,
                outcome="ok",
                response=response,
            )
            self.generate_ok += 1
            self.last_model = model
            return text.strip()

        self.generate_failed += 1
        return None

    @staticmethod
    def _clean_ids(
        payload_ids: object, *, limit: int = TOP_CANDIDATES
    ) -> list[str] | None:
        """Usable ids of a selection answer, deduplicated and capped.

        Never extends and never repairs: a non-list, an empty list and a
        list without usable strings all answer ``None`` (the caller keeps
        its deterministic order), while duplicates are dropped so one
        product cannot occupy two of the 20 slots. The cap is what makes
        "строго 20 лучших" a guarantee of the CODE rather than a hope about
        the prompt.
        """

        if not isinstance(payload_ids, list):
            return None
        cleaned: list[str] = []
        for item in payload_ids:
            if not isinstance(item, str):
                continue
            value = item.strip()
            if value and value not in cleaned:
                cleaned.append(value)
        if not cleaned:
            return None
        return cleaned[: max(1, int(limit or TOP_CANDIDATES))]

    @staticmethod
    def _rank_payload(product: Product) -> dict[str, object]:
        """Identity plus real ranking signals sent to Gemini for ordering.

        No URLs are included (the model cannot invent links), and only
        fields the marketplace actually returned are present - a missing
        value stays ``None`` instead of being imputed.
        """

        return {
            "id": product.product_id,
            "title": product.title,
            "price": product.price,
            "currency": product.currency,
            "discount_percent": product.discount_percent,
            "rating": product.rating,
            "orders_count": product.orders_count,
            "availability": (
                product.availability.value
                if hasattr(product.availability, "value")
                else product.availability
            ),
        }

    @staticmethod
    def _extract_json_object(raw: str | None) -> dict | None:
        """The FIRST JSON object of a model answer, or ``None``.

        Models answer with prose around the object ("Конечно! Вот...") or
        wrap it in a ```json fence; both shapes are accepted. A list, a
        scalar, trailing garbage after the closing brace and a non-object
        answer all yield ``None`` so the caller keeps its deterministic
        fallback - a partially usable answer is never repaired.
        """

        if not raw or not raw.strip():
            return None
        # A fenced block is tried first, then the first {...} of the text.
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
        candidates: list[str] = []
        if fenced:
            candidates.append(fenced.group(1))
        brace = raw.find("{")
        if brace != -1:
            depth = 0
            in_string = False
            escaped = False
            for index in range(brace, len(raw)):
                char = raw[index]
                if in_string:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == '"':
                        in_string = False
                    continue
                if char == '"':
                    in_string = True
                elif char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if depth == 0:
                        candidates.append(raw[brace : index + 1])
                        break
        for candidate in candidates:
            try:
                payload = json.loads(candidate)
            except (ValueError, TypeError):
                continue
            if isinstance(payload, dict):
                return payload
        return None

    @staticmethod
    def _to_search_request(
        payload: object, fallback_text: str | None = None
    ) -> SearchRequest:
        """A validated :class:`SearchRequest` from a model's JSON object.

        Only known fields are read, every value is type-checked, invalid
        values fall back to the deterministic parser of ``fallback_text``
        and unknown fields are silently dropped: a hallucinating model can
        neither inject product data nor invent a constraint. The catalog
        phrase passes the SAME constraint stripping as the local parser, so
        a quality phrase the model kept cannot empty the marketplace query.
        """

        base = AIService._fallback_parse(fallback_text or "")
        if not isinstance(payload, dict):
            return base

        def number(value: object) -> float | None:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            number_value = float(value)
            return number_value if number_value == number_value else None  # NaN

        update: dict[str, object] = {}
        if isinstance(payload.get("normalized_query"), str) and payload[
            "normalized_query"
        ].strip():
            model_phrase = " ".join(payload["normalized_query"].split())
            cleaned = _strip_constraint_phrases(model_phrase)
            # Cleaning must never throw a usable phrase away: when nothing
            # but noise is left, the model's original wording is kept.
            update["normalized_query"] = cleaned or model_phrase
        # ``original_query`` ALWAYS stays the user's own words
        # (``fallback_text``): a model echoing or rewriting it ("x", a
        # summary, a translation) must not replace the exact query the
        # conversation replay/audit depends on.
        if isinstance(payload.get("category"), str) and payload["category"].strip():
            update["category"] = payload["category"].strip()
        max_price = number(payload.get("max_price"))
        if max_price is not None and max_price > 0:
            update["max_price"] = max_price
        min_price = number(payload.get("min_price"))
        if min_price is not None and min_price > 0:
            update["min_price"] = min_price
        currency = payload.get("currency")
        if isinstance(currency, str) and normalize_currency(currency):
            update["currency"] = normalize_currency(currency)
        min_rating = number(payload.get("min_rating"))
        if min_rating is not None and 0 < min_rating <= 5:
            update["min_rating"] = min_rating
        min_orders = number(payload.get("min_orders"))
        if min_orders is not None and min_orders > 0:
            update["min_orders"] = int(min_orders)
        quantity = number(payload.get("quantity"))
        if quantity is not None and quantity > 0:
            update["quantity"] = int(quantity)
        sort = payload.get("sort_preference")
        if isinstance(sort, str) and sort.strip().lower() in ALLOWED_SORT_PREFERENCES:
            update["sort_preference"] = SortPreference(sort.strip().lower())
        if payload.get("prefers_good_rating") is True:
            update["prefers_good_rating"] = True
        marketplaces: list[Marketplace] = []
        raw_marketplaces = payload.get("preferred_marketplaces")
        if isinstance(raw_marketplaces, list):
            for name in raw_marketplaces:
                if (
                    isinstance(name, str)
                    and name.strip().lower() == Marketplace.ALIEXPRESS.value
                ):
                    if Marketplace.ALIEXPRESS not in marketplaces:
                        marketplaces.append(Marketplace.ALIEXPRESS)
        update["preferred_marketplaces"] = marketplaces

        # Feature lists: context-of-use («для жены») and design («в виде
        # сердца») phrases never become a literal inclusion filter, and a
        # phrase that LEADS with a negation states an ABSENCE - it is dropped
        # from required_features (never turned into an exclusion: the negated
        # word often appears literally in the CORRECT title).
        def clean_features(raw: object) -> list[str]:
            if not isinstance(raw, list):
                return []
            features: list[str] = []
            for item in raw:
                if not isinstance(item, str):
                    continue
                phrase = " ".join(item.split()).strip()
                if not phrase:
                    continue
                if is_context_clause(phrase) or is_design_clause(phrase):
                    continue
                if is_negation_clause(phrase):
                    continue
                if phrase not in features:
                    features.append(phrase)
            return features

        update["required_features"] = clean_features(payload.get("required_features"))
        update["excluded_features"] = clean_features(payload.get("excluded_features"))

        try:
            return base.model_copy(update=update)
        except Exception:  # noqa: BLE001 - a validation failure is a fallback
            return base

    def _validated_ai_text(self, text: object, facts: object) -> str | None:
        """A sentence of AI text whose numbers all exist in ``facts``.

        A hallucinated figure can never reach the user: a text mentioning
        any number absent from the supplied product data is dropped. Pure
        prose (no numbers at all) passes, because there is nothing to
        verify it against beyond the model's wording.
        """

        if not isinstance(text, str) or not text.strip():
            return None
        cleaned = " ".join(text.split()).strip()
        if len(cleaned) > 400:
            return None
        allowed = numeric_tokens(json.dumps(facts, ensure_ascii=False, default=str))
        numbers = set(re.findall(r"\d+(?:[.,]\d+)?", cleaned))
        if numbers and not numbers <= allowed:
            logger.warning(
                "AI text mentioned numbers absent from the supplied data "
                "(%s); the text is dropped",
                ", ".join(sorted(numbers - allowed)[:5]),
            )
            return None
        return cleaned

    @staticmethod
    def _merge_is_safe(
        previous: SearchRequest, follow_up: SearchRequest, ai_merge: SearchRequest
    ) -> bool:
        """Whether an AI merge keeps every hard constraint (Stage 3C).

        The model's answer is compared against the DETERMINISTIC merge of
        the same inputs field by field: an omitted hard constraint is
        erasure, an added/changed one is invention - both refuse the merge
        so the caller applies ``deterministic_merge`` instead. A legitimate
        user change is never rejected: the deterministic expectation
        already carries it, so an agreeing AI answer lands.
        """

        expected, _refined = deterministic_merge(previous, follow_up, follow_up.original_query or "")
        for name in HARD_MERGE_FIELDS:
            if getattr(ai_merge, name) != getattr(expected, name):
                return False
        # Plain feature lists (Stage 3A): every established constraint must
        # survive and every kept item must be backed by the deterministic
        # merge of this very message - a model answer that omits the field
        # (constructing []) or invents a phrase cannot erase or add one.
        for list_name in ("required_features", "excluded_features"):
            expected_items = list(getattr(expected, list_name) or [])
            actual_items = list(getattr(ai_merge, list_name) or [])
            if not feature_lists_equivalent(actual_items, expected_items):
                return False
        # Structured-intent lists: no established item may vanish, and a
        # word the two requests never knew may not appear (anti-invention).
        for list_name in (
            "intent_required_attributes",
            "intent_compatibility",
            "intent_excluded_product_types",
        ):
            expected_items = list(getattr(expected, list_name) or [])
            actual_items = list(getattr(ai_merge, list_name) or [])
            for item in actual_items:
                if not any(_same_identity_item(item, want) for want in expected_items):
                    return False
            for item in expected_items:
                if not any(_same_identity_item(item, got) for got in actual_items):
                    return False
        return True

    async def merge_search_requests(
        self,
        previous: SearchRequest,
        follow_up: SearchRequest,
        raw_text: str,
    ) -> SearchRequest | None:
        """An AI merge of a follow-up into the previous request, or ``None``.

        The model is given the previous request, the parsed follow-up and
        the user's own words; its answer is validated (JSON object, known
        fields only) and then passed through :meth:`_merge_is_safe` against
        the deterministic merge. ``None`` means the answer was unusable or
        unsafe and the caller must keep the deterministic merge - the AI
        can never silently erase or invent a hard constraint.
        """

        if not self.is_available:
            return None

        raw = await self._generate(
            MERGE_PROMPT.format(
                previous_json=json.dumps(
                    previous.model_dump(mode="json"), ensure_ascii=False
                ),
                follow_up=raw_text or "",
                parsed_json=json.dumps(
                    follow_up.model_dump(mode="json"), ensure_ascii=False
                ),
            ),
            models=(self.smart_model,),
        )
        payload = self._extract_json_object(raw) if raw else None
        if payload is None:
            logger.warning("AI merge returned non-JSON; using the deterministic merge")
            return None

        # A field the model OMITS is the model's implicit claim: for hard
        # constraints that is ERASURE (None) and for a feature list it is an
        # empty list - exactly what the gate must catch (Stage 3A/3C). Only
        # fields explicitly present in the payload may carry a value.
        candidate = previous.model_copy(deep=True)
        parsed = AIService._to_search_request(payload, fallback_text="")
        candidate.normalized_query = (
            parsed.normalized_query
            if isinstance(payload.get("normalized_query"), str)
            and payload["normalized_query"].strip()
            else None
        )
        candidate.category = (
            parsed.category
            if isinstance(payload.get("category"), str)
            and payload["category"].strip()
            else None
        )
        candidate.max_price = parsed.max_price if isinstance(
            payload.get("max_price"), (int, float)
        ) else None
        candidate.min_price = parsed.min_price if isinstance(
            payload.get("min_price"), (int, float)
        ) else None
        candidate.currency = parsed.currency if isinstance(
            payload.get("currency"), str
        ) else None
        candidate.min_rating = parsed.min_rating if isinstance(
            payload.get("min_rating"), (int, float)
        ) else None
        candidate.min_orders = parsed.min_orders if isinstance(
            payload.get("min_orders"), (int, float)
        ) else None
        candidate.quantity = parsed.quantity if isinstance(
            payload.get("quantity"), (int, float)
        ) else None
        candidate.country = (
            " ".join(payload["country"].split()).strip()
            if isinstance(payload.get("country"), str) and payload["country"].strip()
            else None
        )
        for name in (
            "intent_product_type",
            "intent_brand",
            "intent_model",
            "intent_generation",
        ):
            raw_value = payload.get(name)
            if isinstance(raw_value, str) and raw_value.strip():
                setattr(candidate, name, " ".join(raw_value.split()).strip())
            else:
                setattr(candidate, name, None)
        for name in (
            "required_features",
            "excluded_features",
        ):
            if isinstance(payload.get(name), list):
                setattr(candidate, name, getattr(parsed, name))
            else:
                setattr(candidate, name, [])
        for name in (
            "intent_required_attributes",
            "intent_compatibility",
            "intent_excluded_product_types",
        ):
            raw_items = payload.get(name)
            if isinstance(raw_items, list):
                setattr(
                    candidate,
                    name,
                    [item.strip() for item in raw_items if isinstance(item, str)],
                )
            else:
                setattr(candidate, name, [])
        candidate.original_query = raw_text or previous.original_query

        try:
            if not AIService._merge_is_safe(previous, follow_up, candidate):
                logger.info(
                    "AI merge changed a hard constraint; using the deterministic merge"
                )
                return None
        except Exception:  # noqa: BLE001 - a gate failure never blocks the merge
            logger.exception("AI merge safety gate failed; using the deterministic merge")
            return None
        return candidate

    @staticmethod
    def _with_result_limit(user_text: str, request: SearchRequest) -> SearchRequest:
        """Attach the explicitly requested result count, when one was asked.

        The LLM prompt never fills ``result_limit``: the deterministic count
        capture in ``services.text_matching`` is the only source, so a
        hallucinating model cannot inflate or erase how many cards the user
        asked for. When no count is present the request is returned as is
        and the default browse cap applies unchanged.
        """

        count = extract_requested_result_count(user_text)
        if count is None or count == request.result_limit:
            return request
        return request.model_copy(update={"result_limit": count})

    @staticmethod
    def _fallback_parse(text: str) -> SearchRequest:
        """Deterministic parser used when Gemini is unavailable.

        It only recognizes EXPLICIT information in the text (numbers,
        currency symbols, an explicitly named marketplace). It does not
        interpret fuzzy phrases such as "good reviews" - inventing a
        minimum rating from them would be guessing, and the project rule
        is to filter only on data we actually have. Such a phrase is
        instead recorded as the ranking-only wish ``prefers_good_rating``
        (real quality fields reorder candidates; nothing is filtered), and
        with Gemini configured the same field is filled by the prompt.
        """

        lowered = text.lower()
        normalized = re.sub(r"\s+", " ", text).strip()

        quantity = _match_int(lowered, _QUANTITY_PATTERN)
        min_rating = _match_number(lowered, _MIN_RATING_PATTERN)
        min_orders = _match_int(lowered, _MIN_ORDERS_PATTERN)

        # A rating clause also contains an amount ("rating above 4.5");
        # blank it out before price extraction so "above 4.5" is never
        # mistaken for a price bound.
        price_scope = _MIN_RATING_PATTERN.sub(" ", lowered)
        # A currency symbol glued to the number ("$30", "€30") is itself the
        # marker: the budget is explicit without a word marker.
        symbol_amount = _match_number(price_scope, _MAX_SYMBOL_PATTERN)
        max_price = _match_number(price_scope, _MAX_PRICE_PATTERN)
        min_price = _match_number(price_scope, _MIN_PRICE_PATTERN)
        if max_price is None and min_price is None:
            if re.search(r"(?i)\b(?:от|over|at least)\b", price_scope):
                # «over €30»: floor marker -> the amount is a MINIMUM.
                min_price = symbol_amount
            else:
                max_price = symbol_amount

        preferred_marketplaces: list[Marketplace] = []
        if "aliexpress" in lowered or "алиэкспресс" in lowered:
            preferred_marketplaces.append(Marketplace.ALIEXPRESS)

        required_features: list[str] = []
        for match in _REQUIRED_FEATURE_RE.findall(text):
            feature = _clean_feature(match)
            if feature:
                required_features.append(feature)

        return SearchRequest(
            original_query=text,
            normalized_query=_strip_constraint_phrases(normalized),
            max_price=max_price,
            min_price=min_price,
            currency=_detect_currency(lowered),
            min_rating=min_rating,
            min_orders=min_orders,
            quantity=quantity,
            preferred_marketplaces=preferred_marketplaces,
            required_features=required_features,
            sort_preference=_detect_sort_preference(lowered),
            prefers_good_rating=_has_fuzzy_quality_phrase(lowered),
        )
