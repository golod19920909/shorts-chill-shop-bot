"""AI product assistant: reasoning over REAL marketplace products only.

Architecture (deliberately boring, because it is a data-integrity
boundary):

    Telegram handler
        -> ProductAssistant          (this module: orchestration + facts)
            -> SearchService         (candidate generation, EXISTING pipeline)
            -> AIService             (optional language layer, validated)

Hard rules implemented here - the project's promises, not aspirations:

* the assistant only ever receives :class:`~models.product.Product`
  instances that a marketplace provider produced; it can never construct
  one, and never asks a model to;
* everything numerically comparable (price, orders, dates, "cheaper",
  "more orders") is computed in Python from real fields - a language model
  is used for wording and for ordering an already-validated candidate set,
  never as the source of a number;
* every product id a model returns is validated against the candidate set
  it was given (``_validated``); unknown ids are dropped and logged;
* when AI is unavailable, malformed or hallucinating, every operation has
  a deterministic answer built from the same real data. Nothing degrades
  into a crash, a fake product or an invented fact.

The module has no Telegram imports on purpose: it is unit-testable with a
stub AI service and a stub search service (see ``tests/test_ai_assistant``).
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field

from models.product import Product
from models.search_request import SearchMode, SearchRequest
from services.product_identity import deduplicate_products, product_identity
from services.text_matching import phrase_in_text
from utils.formatting import format_price

logger = logging.getLogger(__name__)

#: How many candidates are sent to a model at once. Keeps prompts small
#: (and quota use predictable) without hiding information from the user,
#: whose lists are built from the deterministic set anyway.
MAX_CANDIDATES_FOR_AI = 12

#: How many candidates a "cheaper/similar/better" screen shows.
MAX_RESULTS_SHOWN = 5

#: Words that carry no product meaning and are therefore never used to
#: build a search intent from a title.
_JUNK_WORDS = frozenset(
    {
        "hot", "sale", "new", "free", "shipping", "dropshipping", "wholesale",
        "original", "high", "quality", "best", "top", "2023", "2024", "2025",
        "2026", "2027", "акция", "скидка", "новинка", "бесплатная", "доставка",
        "опт", "оригинал", "лучший", "качество",
    }
)

_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")


@dataclass(frozen=True)
class ProductAdvice:
    """A rendered answer plus whether a language model wrote it."""

    text: str
    used_ai: bool


@dataclass(frozen=True)
class CandidateSelection:
    """Products picked for one goal, with the honest note about ordering."""

    products: list[Product] = field(default_factory=list)
    used_ai: bool = False
    #: Short, user-facing explanation when something had to degrade
    #: (``None`` when the result is exactly what was asked for).
    note: str | None = None
    #: Machine-readable reason for logs/tests: "ok", "ai_fallback",
    #: "no_candidates", "no_comparable_price", "no_criteria".
    reason: str = "ok"


def product_facts(product: Product) -> dict[str, object]:
    """Every comparable fact the marketplace actually reported.

    Only present values are returned: a missing field is absent from the
    mapping (never ``None``, ``0`` or a guess), so anything built from it -
    a deterministic text or an AI prompt - physically cannot claim data
    the marketplace did not send.
    """

    facts: dict[str, object] = {
        "id": product.product_id,
        "title": product.title,
        "marketplace": str(getattr(product.marketplace, "value", product.marketplace)),
    }
    optional = (
        "description",
        "price",
        "currency",
        "old_price",
        "discount_percent",
        "rating",
        "positive_rate",
        "orders_count",
        "shipping_cost",
        "delivery_estimate",
        "seller_name",
        "promo_code",
        "promo_benefit",
        "promo_valid_until",
    )
    for name in optional:
        value = getattr(product, name, None)
        if value is not None:
            facts[name] = value

    if product.availability.value != "unknown":
        facts["availability"] = product.availability.value
    # URLs are deliberately absent: a model must never echo or invent a
    # link, and the bot always takes the URL from the product itself.
    return facts


def title_terms(text: str | None) -> set[str]:
    """Significant, lower-case words of a title (for similarity ranking)."""

    if not text:
        return set()
    return {
        word.lower()
        for word in _WORD_RE.findall(str(text))
        if len(word) >= 4 and word.lower() not in _JUNK_WORDS
    }


def intent_from_title(title: str, *, limit: int = 6) -> str:
    """Deterministic search intent derived from a real product title.

    Used when AI is unavailable (and as the fallback when a model returns
    something unusable): brackets/marketing words are dropped, the first
    significant words are kept. It never adds a word that was not in the
    title - the intent can only ever be a *subset* of real product data.
    """

    cleaned = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", str(title or ""))
    words = [
        word
        for word in _WORD_RE.findall(cleaned)
        if word.lower() not in _JUNK_WORDS and len(word) > 2
    ]
    intent = " ".join(words[:limit]).strip()
    return intent[:100]


def numeric_tokens(text: str | None) -> set[str]:
    """Every number-like token in a text, normalized for comparison.

    Anti-hallucination helper: a model answer may only mention numbers
    that exist in the facts it was given, so the caller compares this set
    against the facts.
    """

    if not text:
        return set()
    return {match.replace(",", ".") for match in _NUMBER_RE.findall(str(text))}


def fact_lines(product: Product) -> list[str]:
    """Deterministic rendering of the real fields of one product."""

    lines: list[str] = []
    if product.price is not None:
        price = format_price(product.price, product.currency)
        if product.discount_percent is not None and product.old_price is not None:
            price += (
                f" (было {format_price(product.old_price, product.currency)}, "
                f"−{product.discount_percent:g}%)"
            )
        lines.append(f"• Цена: {price}")
    if product.positive_rate is not None:
        lines.append(f"• Положительных отзывов: {product.positive_rate:g}%")
    if product.rating is not None:
        lines.append(f"• Рейтинг: {product.rating:g} из 5")
    if product.orders_count is not None:
        lines.append(f"• Заказов: {product.orders_count}")
    if product.delivery_estimate:
        lines.append(f"• Доставка: {product.delivery_estimate}")
    if product.shipping_cost is not None:
        lines.append(
            "• Стоимость доставки: "
            f"{format_price(product.shipping_cost, product.currency)}"
        )
    if product.promo_code:
        lines.append(f"• Промокод продавца: {product.promo_code}")
        if product.promo_benefit:
            lines.append(f"• Условие промокода: {product.promo_benefit}")
        if product.promo_valid_until:
            lines.append(f"• Промокод действует до: {product.promo_valid_until}")
    elif product.promo_benefit:
        lines.append(f"• Промо-условие: {product.promo_benefit}")
    if product.seller_name:
        lines.append(f"• Продавец: {product.seller_name}")
    if not lines:
        lines.append(
            "• AliExpress не передал по этому товару ни цены, ни отзывов, ни заказов."
        )
    return lines


def observation_lines(product: Product) -> list[str]:
    """Observations computed in Python from the facts above.

    Kept separate from :func:`fact_lines` so the user can always tell
    marketplace data from the bot's own arithmetic.
    """

    lines: list[str] = []
    if product.price is not None and product.shipping_cost is not None:
        lines.append(
            "• Цена с доставкой: "
            f"{format_price(product.total_known_cost, product.currency)}"
        )
    if (
        product.price is not None
        and product.old_price is not None
        and product.old_price > product.price
    ):
        lines.append("• Скидка посчитана по прежней цене из ответа AliExpress.")
    if product.orders_count is not None:
        lines.append(
            "• Число заказов — накопленный показатель из ответа AliExpress, "
            "а не продажи за сегодня."
        )
    if not lines:
        lines.append("• Дополнительных расчётов нет: числовых полей больше нет.")
    return lines


ADVICE_DISCLAIMER = (
    "Это сводка по данным AliExpress, а не гарантия качества: ни материалы, "
    "ни скорость доставки, ни надёжность продавца API не сообщает."
)


def render_analysis(product: Product, analysis: dict | None) -> str:
    """Render an AI analysis on top of the ALWAYS-factual block.

    The marketplace facts are rendered by the same code that renders the
    deterministic answer, so the user always sees the real numbers first;
    the model may only add observations and a short recommendation that
    already passed the numeric validation in ``AIService``.
    """

    lines = [
        "🤖 <b>Стоит ли покупать?</b>",
        "",
        "<b>Данные AliExpress</b>",
        *fact_lines(product),
    ]

    observations = []
    if isinstance(analysis, dict):
        raw_items = analysis.get("observations")
        if isinstance(raw_items, list):
            observations = [str(item) for item in raw_items if item]
    if observations:
        lines.extend(["", "<b>Что заметил помощник</b>"])
        lines.extend(f"• {html.escape(item)}" for item in observations[:3])

    recommendation = ""
    if isinstance(analysis, dict) and isinstance(analysis.get("recommendation"), str):
        recommendation = analysis["recommendation"].strip()
    if recommendation:
        lines.extend(["", html.escape(recommendation)])

    lines.extend(["", "<b>Что посчитал бот</b>", *observation_lines(product)])
    lines.extend(["", ADVICE_DISCLAIMER])
    return "\n".join(lines)


def deterministic_advice(product: Product) -> str:
    """The "Стоит ли покупать?" answer built only from real fields."""

    return "\n".join(
        [
            "🤖 <b>Стоит ли покупать?</b>",
            "",
            "<b>Данные AliExpress</b>",
            *fact_lines(product),
            "",
            "<b>Что посчитал бот</b>",
            *observation_lines(product),
            "",
            ADVICE_DISCLAIMER,
            "Сравните этот товар с похожими: «💰 Найти дешевле» и "
            "«🎯 Подобрать лучше».",
        ]
    )




def _comparable_price_products(
    original: Product, candidates: list[Product]
) -> tuple[list[Product], str | None]:
    """Candidates whose price can be compared with the original's price.

    Comparison requires BOTH prices and the SAME currency - the project
    has no documented conversion mechanism, so different currencies are
    never compared numerically ("no exchange rates are invented").
    """

    if original.price is None or not original.currency:
        return [], "no_comparable_price"

    currency = original.currency.upper()
    comparable = [
        product
        for product in candidates
        if product.price is not None
        and (product.currency or "").upper() == currency
        and product_identity(product) != product_identity(original)
    ]
    if not comparable:
        return [], "no_comparable_price"
    return comparable, None


def cheaper_candidates(original: Product, candidates: list[Product]) -> list[Product]:
    """Real candidates strictly cheaper than the original, cheapest first."""

    comparable, _reason = _comparable_price_products(original, candidates)
    original_price = original.price
    if original_price is None:
        return []
    cheaper = [
        product
        for product in comparable
        if product.price is not None and product.price < original_price
    ]
    return sorted(
        cheaper,
        key=lambda product: (product.price or 0.0, -(product.orders_count or 0)),
    )


def _better_score(product: Product) -> tuple[float, float, float] | None:
    """Comparable quality signals the marketplace really reported.

    Only fields that exist count: a missing rating cannot become "0" and a
    missing order count cannot become "worse". ``None`` means "no criterion
    available", which is reported honestly instead of being guessed.
    """

    if (
        product.rating is None
        and product.positive_rate is None
        and product.orders_count is None
    ):
        return None

    rating = product.rating * 20.0 if product.rating is not None else -1.0
    positive = product.positive_rate if product.positive_rate is not None else -1.0
    orders = float(product.orders_count) if product.orders_count is not None else -1.0
    return (rating, positive, orders)


def better_candidates(original: Product, candidates: list[Product]) -> list[Product]:
    """Candidates that beat the original on at least one REAL signal."""

    original_score = _better_score(original)
    if original_score is None:
        return []

    better: list[Product] = []
    for product in candidates:
        if product_identity(product) == product_identity(original):
            continue
        score = _better_score(product)
        if score is None or score <= original_score:
            continue
        better.append(product)
    return sorted(
        better,
        key=lambda product: _better_score(product) or (0.0, 0.0, 0.0),
        reverse=True,
    )


def similar_candidates(original: Product, candidates: list[Product]) -> list[Product]:
    """Candidates for the same product concept, most similar first.

    The candidate set already comes from a marketplace search for this
    product's concept, so topicality is given; the ordering uses title word
    overlap (computed in Python, no model) and then real order counts. The
    original product is never returned as its own "similar" result.
    """

    original_terms = title_terms(original.title)
    unique = deduplicate_products(
        [
            product
            for product in candidates
            if product_identity(product) != product_identity(original)
        ]
    )

    def overlap(product: Product) -> float:
        terms = title_terms(product.title)
        if not original_terms or not terms:
            return 0.0
        return len(original_terms & terms) / len(original_terms | terms)

    return sorted(
        unique,
        key=lambda product: (overlap(product), product.orders_count or 0),
        reverse=True,
    )



#: Phrases that mean "compare prices" / "compare popularity" / "compare
#: reviews" in a product question. Checked in this order, because a
#: question can contain several ("какой из этих вариантов дешевле").
_PRICE_MARKERS = (
    "дешев", "дорог", "стоимост", "price", "cheap", "expensive",
    "how much", "cost",
)


def _is_price_question(question: str) -> bool:
    """Recognize explicit price intent without confusing 'worth choosing' with cost."""
    text = (question or "").lower().replace("ё", "е")
    if any(marker in text for marker in _PRICE_MARKERS):
        return True
    return bool(
        re.search(
            r"\bцен(?:а|ы|у|е|ой|ник)\w*\b|"
            r"\bсколько\s+(?:это\s+)?стоит\b|"
            r"\bво\s+сколько\b|"
            r"\bпо\s+какой\s+цене\b|"
            r"\bhow\s+much\s+(?:does\s+it\s+)?cost\b",
            text,
        )
    )
_ORDERS_MARKERS = ("заказ", "продаж", "популяр", "orders", "sales")
_REVIEW_MARKERS = ("рейтинг", "отзыв", "оценк", "rating", "reviews")

#: Question words that carry no product meaning: dropped before a feature
#: probe so "есть ли здесь беспроводная версия?" yields ("беспроводная",
#: "версия") and only real words are looked up in real titles.
_QUESTION_STOPWORDS = frozenset(
    {
        "есть", "ли", "здесь", "тут", "версия", "вариант", "вариантов", "какой",
        "какая", "какое", "какие", "который", "которая", "которые", "этот",
        "этих", "это", "эти", "он", "она", "оно", "они", "подходит", "подойдет",
        "подойдёт", "для", "мне", "мой", "моя", "моего", "можно", "или", "чем",
        "что", "чего", "самый", "самая", "самое", "более", "менее", "нужен",
        "нужна", "нужно", "светит", "светят", "светить", "светится", "светятся",
    }
)


def _products_with_price(products: list[Product]) -> list[Product]:
    return [product for product in products if product.price is not None]


def _extreme_per_currency(
    products: list[Product], *, cheapest: bool
) -> dict[str, Product]:
    """Best product per currency: currencies are never mixed."""

    best: dict[str, Product] = {}
    for product in _products_with_price(products):
        currency = (product.currency or "").upper() or "?"
        current = best.get(currency)
        if current is None:
            best[currency] = product
            continue
        better = (
            (product.price or 0) < (current.price or 0)
            if cheapest
            else (product.price or 0) > (current.price or 0)
        )
        if better:
            best[currency] = product
    return best


def _price_lines(products: dict[str, Product]) -> list[str]:
    return [
        f"• {product.title[:70]} — {format_price(product.price, product.currency)}"
        for product in products.values()
    ]


_COMPARISON_REQUEST_MARKERS = (
    "сравни", "сравнить", "сравнение", "сопостав", "отличаются",
    "различия", "compare", "comparison",
)


def is_explicit_comparison_request(question: str) -> bool:
    """Recognize an explicit request to compare already-shown products."""

    lowered = (question or "").casefold().replace("ё", "е")
    return any(marker in lowered for marker in _COMPARISON_REQUEST_MARKERS)


def deterministic_comparison(
    question: str,
    products: list[Product],
    *,
    evidence_differences: list[tuple[str, str]] | None = None,
) -> str | None:
    """Compare the two highest-ranked shown products using only real fields.

    The search/semantic-selection pipeline has already ordered the products.
    This renderer compares its first two distinct listings. Optional Gemini
    evidence is accepted only after exact-quote validation in AIService; all
    numeric facts and the final wording are assembled locally.
    """

    if not is_explicit_comparison_request(question):
        return None

    pair: list[Product] = []
    seen_ids: set[str] = set()
    for product in products:
        product_id = str(product.product_id)
        if product_id in seen_ids:
            continue
        seen_ids.add(product_id)
        pair.append(product)
        if len(pair) == 2:
            break

    if len(pair) < 2:
        return (
            "Для сравнения нужны минимум два товара из показанных результатов. "
            "Сейчас доступен только один подходящий вариант."
        )

    first, second = pair
    lines = [
        "Сравнил два наиболее релевантных варианта из показанных результатов.",
        "",
    ]

    for number, product in enumerate(pair, start=1):
        title = html.escape(str(product.title or "Товар")[:240])
        lines.append(f"<b>Вариант {number}</b>")
        lines.append(title)
        if product.price is not None:
            lines.append(f"• Цена: {format_price(product.price, product.currency)}")
        else:
            lines.append("• Цена: нет данных в карточке")
        if product.rating is not None:
            lines.append(f"• Рейтинг: {product.rating:g} из 5")
        if product.positive_rate is not None:
            lines.append(f"• Положительных отзывов: {product.positive_rate:g}%")
        if product.orders_count is not None:
            lines.append(f"• Заказов: {product.orders_count}")
        if product.delivery_estimate:
            lines.append(f"• Доставка: {html.escape(str(product.delivery_estimate)[:100])}")
        if product.shipping_cost is not None:
            lines.append(
                "• Стоимость доставки: "
                f"{format_price(product.shipping_cost, product.currency)}"
            )
        lines.append("")

    differences: list[str] = []
    if first.price is not None and second.price is not None:
        first_currency = (first.currency or "").upper()
        second_currency = (second.currency or "").upper()
        if first_currency and first_currency == second_currency:
            delta = abs(first.price - second.price)
            if delta > 0:
                cheaper_number = 1 if first.price < second.price else 2
                differences.append(
                    f"Вариант {cheaper_number} дешевле на "
                    f"{format_price(delta, first.currency)}."
                )
            else:
                differences.append("Цены одинаковые.")
        else:
            differences.append(
                "Не хватает одинаковых данных о валюте, поэтому цены напрямую не сравниваю."
            )

    if first.rating is not None and second.rating is not None:
        if first.rating != second.rating:
            higher = 1 if first.rating > second.rating else 2
            differences.append(
                f"У варианта {higher} выше рейтинг: "
                f"{max(first.rating, second.rating):g} против "
                f"{min(first.rating, second.rating):g} из 5."
            )
        else:
            differences.append(f"Рейтинг одинаковый: {first.rating:g} из 5.")

    if first.positive_rate is not None and second.positive_rate is not None:
        if first.positive_rate != second.positive_rate:
            higher = 1 if first.positive_rate > second.positive_rate else 2
            differences.append(
                f"У варианта {higher} выше доля положительных отзывов: "
                f"{max(first.positive_rate, second.positive_rate):g}% против "
                f"{min(first.positive_rate, second.positive_rate):g}%."
            )

    if first.orders_count is not None and second.orders_count is not None:
        if first.orders_count != second.orders_count:
            more = 1 if first.orders_count > second.orders_count else 2
            differences.append(
                f"У варианта {more} больше накопленных заказов: "
                f"{max(first.orders_count, second.orders_count)} против "
                f"{min(first.orders_count, second.orders_count)}."
            )

    verified_differences = [
        (first_quote, second_quote)
        for first_quote, second_quote in (evidence_differences or [])[:4]
        if isinstance(first_quote, str)
        and isinstance(second_quote, str)
        and first_quote.strip()
        and second_quote.strip()
    ]
    lines.append("<b>Главные отличия по данным карточек</b>")
    if differences:
        lines.extend(f"• {item}" for item in differences)
    if verified_differences:
        lines.extend(
            "• Вариант 1: «"
            + html.escape(first_quote)
            + "»; вариант 2: «"
            + html.escape(second_quote)
            + "»."
            for first_quote, second_quote in verified_differences
        )
    if not differences and not verified_differences:
        lines.append(
            "В карточках недостаточно общих характеристик, чтобы объективно "
            "назвать один вариант лучше другого. Сопоставьте сведения, "
            "указанные в названиях выше."
        )
    lines.extend([
        "",
        "Я сравниваю только сведения, которые AliExpress передал в этих карточках; "
        "неуказанные характеристики и совместимость не додумываю.",
    ])
    return "\n".join(lines)


def _feature_probe_words(question: str) -> list[str]:
    return [
        word.lower()
        for word in _WORD_RE.findall(question or "")
        if len(word) >= 4 and word.lower() not in _QUESTION_STOPWORDS
    ]



def matching_products_for_question(
    question: str, products: list[Product]
) -> list[Product]:
    """Return existing products whose REAL titles contain a requested feature.

    This is a deterministic, title-grounded selector. It never invents or
    enriches product attributes; callers may re-render the returned Product
    objects through the normal Telegram card path.
    """

    text = (question or "").lower()
    if any(
        marker in text
        for marker in (*_PRICE_MARKERS, *_ORDERS_MARKERS, *_REVIEW_MARKERS)
    ):
        return []
    if any(
        marker in text
        for marker in ("лучше", "лучший", "лучшие", "сравн", "почему")
    ):
        return []

    probes = _feature_probe_words(question)
    if not probes or not products:
        return []
    def title_matches_probe(title: str, probe: str) -> bool:
        if phrase_in_text(title, probe):
            return True
        # Russian adverbs often describe the same feature as an adjective in
        # a seller title («ярко» -> «яркий», «быстро» -> «быстрый»).
        # Compare the shortened form only at a title-word boundary.
        if len(probe) >= 4 and probe.endswith(("о", "е")):
            stem = probe[:-1]
            return any(
                token.lower().startswith(stem)
                for token in _WORD_RE.findall(title or "")
            )
        return False

    matched = [
        product
        for product in products
        if any(title_matches_probe(product.title, word) for word in probes)
    ]
    return matched[:MAX_RESULTS_SHOWN]


def _lumen_value(title: str | None) -> float | None:
    """Extract a seller-advertised lumen value from a real listing title."""
    if not title:
        return None
    match = re.search(
        r"(?<!\w)(\d{2,5}(?:[.,]\d+)?)\s*(?:lm|лм|люмен(?:а|ов)?)",
        str(title), flags=re.IGNORECASE,
    )
    if match is None:
        return None
    try:
        return float(match.group(1).replace(",", "."))
    except ValueError:
        return None


def _brightness_comparison(question: str, products: list[Product]) -> str | None:
    """Compare seller-advertised lumen values; never infer brightness."""
    lowered = (question or "").lower().replace("ё", "е")
    if not any(m in lowered for m in ("ярк", "свет", "люмен", " lm", "лм", "освещ")):
        return None
    measured = [
        (value, product) for product in products
        if (value := _lumen_value(product.title)) is not None
    ]
    if not measured:
        return None
    measured.sort(key=lambda item: (item[0], item[1].orders_count or 0), reverse=True)
    top_value = measured[0][0]
    leaders = [product for value, product in measured if value == top_value]
    lines = ["🔦 <b>По заявленным люменам ярче всего:</b>"]
    for product in leaders[:3]:
        lines.append(f"• {html.escape(product.title[:100])} — {top_value:g} лм")
    lower_values = [value for value, _product in measured if value < top_value]
    if lower_values:
        lines.append(f"• Для сравнения: следующий указанный уровень — {max(lower_values):g} лм.")
    lines.append(
        "Это цифры из названий продавцов AliExpress, а не независимое измерение: "
        "фактическую яркость они не подтверждают."
    )
    return "\n".join(lines)


def deterministic_answer(question: str, products: list[Product]) -> str | None:
    """Answer a product question from real data, or ``None`` when it cannot.

    Runs BEFORE any model call: every answer here is exact Python over
    marketplace fields (no quota spent, no chance of a hallucination).
    ``None`` means "this question needs language understanding" - the caller
    then asks AI, and if AI is unavailable the user gets an honest "AliExpress
    did not report this" instead of an invented answer.
    """

    if not products:
        return None

    text = (question or "").lower()

    if _is_price_question(text):
        cheapest = _extreme_per_currency(products, cheapest=True)
        if not cheapest:
            return None
        chosen = (
            _extreme_per_currency(products, cheapest=False)
            if any(m in text for m in ("дорог", "expensive"))
            else cheapest
        )
        lines = ["💰 <b>Цены из данных AliExpress</b>", *_price_lines(chosen)]
        if len(cheapest) > 1:
            lines.append(
                "• Цены приходят в разных валютах; конвертации нет, поэтому "
                "сравнение идёт только внутри одной валюты."
            )
        elif len(products) > 1 and len(_products_with_price(products)) < len(products):
            lines.append("• У части товаров AliExpress цену не передал.")
        return "\n".join(lines)

    if any(marker in text for marker in _ORDERS_MARKERS):
        with_orders = [
            product for product in products if product.orders_count is not None
        ]
        if not with_orders:
            return None
        best = max(with_orders, key=lambda product: product.orders_count or 0)
        return "\n".join(
            [
                "🧾 <b>Больше всего заказов</b>",
                f"• {best.title[:70]} — {best.orders_count} заказов",
                "• Число заказов берётся из ответа AliExpress как есть.",
            ]
        )

    if any(marker in text for marker in _REVIEW_MARKERS):
        scored = [
            product
            for product in products
            if product.positive_rate is not None or product.rating is not None
        ]
        if not scored:
            return None
        best = max(
            scored,
            key=lambda product: (product.rating or 0.0, product.positive_rate or 0.0),
        )
        parts = []
        if best.positive_rate is not None:
            parts.append(f"{best.positive_rate:g}% положительных отзывов")
        if best.rating is not None:
            parts.append(f"рейтинг {best.rating:g} из 5")
        return "\n".join(
            [
                "⭐ <b>Лучшие отзывы в этой выборке</b>",
                f"• {best.title[:70]} — " + ", ".join(parts),
                "• AliExpress передаёт только эти показатели: звёздный рейтинг "
                "есть не у всех товаров.",
            ]
        )

    # Numeric comparisons are answers, not product selections. Keep the
    # selector's return type strictly list[Product]; render the factual
    # comparison here, where a text answer is expected.
    brightness = _brightness_comparison(question, products)
    if brightness:
        return brightness

    # Positive feature probe: report products whose REAL titles contain the
    # asked word. A negative result returns None (a different wording may
    # still match), so the bot never claims "нет" too confidently.
    matched = matching_products_for_question(question, products)
    if matched:
        lines = ["✅ <b>Такие варианты есть в этой выборке</b>"]
        lines.extend(
            f"• {product.title[:70]} — "
            f"{format_price(product.price, product.currency)}"
            for product in matched
        )
        return "\n".join(lines)

    return None



class ProductAssistant:
    """Orchestrates the AI menu: facts first, language model second.

    ``search_service`` is the SAME
    :class:`~services.search_service.SearchService` the rest of the bot
    uses - "cheaper / similar / better" run real AliExpress searches
    through it, so every candidate is a real normalized product from the
    existing pipeline (normalization, deduplication and relevance
    filtering included). No second search architecture exists.
    """

    def __init__(self, ai_service=None, search_service=None) -> None:
        self.ai = ai_service
        self.search = search_service

    # -- internals ---------------------------------------------------------

    @property
    def ai_available(self) -> bool:
        return bool(getattr(self.ai, "is_available", False))

    def _validated(
        self, product_ids: list[str] | None, candidates: list[Product]
    ) -> list[Product]:
        """Map model-provided ids onto the candidate set.

        Ids the model invented (or copied from somewhere else) are dropped
        with a warning: a reference that is not in the candidate set can
        never become a card. This is the hard data-integrity rule.
        """

        if not product_ids:
            return []
        by_id = {product.product_id: product for product in candidates}
        picked: list[Product] = []
        seen: set[str] = set()
        for product_id in product_ids:
            if product_id in seen:
                continue
            product = by_id.get(product_id)
            if product is None:
                logger.warning(
                    "AI returned an unknown product id %r; it is ignored", product_id
                )
                continue
            seen.add(product_id)
            picked.append(product)
        return picked

    async def search_intent(self, product: Product, goal: str) -> tuple[str, bool]:
        """Search wording for a candidate search, AI first if available.

        Returns ``(intent, used_ai)``. The deterministic intent is built
        from the product's own title, so a request is always possible even
        with no AI at all.
        """

        deterministic = intent_from_title(product.title)
        if self.ai_available:
            planned = await self.ai.plan_product_search(product, goal)
            if planned:
                return planned, True
        return deterministic, False

    async def candidate_products(self, product: Product) -> list[Product]:
        """Real marketplace candidates for this product's concept."""

        if self.search is None:
            return []
        intent, _used_ai = await self.search_intent(product, "similar")
        if not intent:
            return []
        request = SearchRequest(
            original_query=intent,
            normalized_query=intent,
            mode=SearchMode.SEARCH,
            currency=product.currency,
        )
        try:
            outcome = await self.search.find_products(request)
        except Exception:  # noqa: BLE001 - a failed search must not kill the menu
            logger.exception("Candidate search failed for %r", intent)
            return []
        return outcome.products


    async def _select(
        self, goal: str, product: Product, candidates: list[Product]
    ) -> CandidateSelection:
        """Deterministic pre-filter, then optional AI ordering, all validated."""

        if not candidates:
            return CandidateSelection([], False, None, "no_candidates")

        if goal == "cheaper":
            pool = cheaper_candidates(product, candidates)
            empty_reason = "no_comparable_price"
        elif goal == "better":
            pool = better_candidates(product, candidates)
            empty_reason = "no_criteria"
        else:
            pool = similar_candidates(product, candidates)
            empty_reason = "no_candidates"

        if not pool:
            return CandidateSelection([], False, None, empty_reason)

        pool = pool[:MAX_CANDIDATES_FOR_AI]
        if self.ai_available:
            ids = await self.ai.select_candidates(goal, product, pool)
            picked = self._validated(ids, pool)
            if picked:
                return CandidateSelection(picked[:MAX_RESULTS_SHOWN], True, None, "ok")

        return CandidateSelection(
            pool[:MAX_RESULTS_SHOWN],
            False,
            "Подбор без AI: порядок детерминированный, данные те же.",
            "ai_fallback",
        )

    # -- public operations -------------------------------------------------

    async def analyze(self, product: Product) -> ProductAdvice:
        """«⭐ Стоит ли покупать?» - facts, observations, honest wording."""

        if self.ai_available:
            analysis = await self.ai.analyze_product(product)
            if analysis:
                return ProductAdvice(render_analysis(product, analysis), True)
        return ProductAdvice(deterministic_advice(product), False)

    async def cheaper(
        self, product: Product, candidates: list[Product]
    ) -> CandidateSelection:
        return await self._select("cheaper", product, candidates)

    async def similar(
        self, product: Product, candidates: list[Product]
    ) -> CandidateSelection:
        return await self._select("similar", product, candidates)

    async def better(
        self, product: Product, candidates: list[Product]
    ) -> CandidateSelection:
        return await self._select("better", product, candidates)

    def matching_products(
        self, question: str, products: list[Product]
    ) -> list[Product]:
        """Select already-shown products with title evidence for a feature question."""

        return matching_products_for_question(question, products)

    async def answer(self, question: str, products: list[Product]) -> ProductAdvice:
        """«💬 Задать свой вопрос» - exact answers first, language second."""

        if is_explicit_comparison_request(question):
            evidence_differences = None
            compare = getattr(self.ai, "extract_paired_listing_evidence", None)
            if self.ai_available and callable(compare):
                evidence_differences = await compare(question, products)
            comparison = deterministic_comparison(
                question,
                products,
                evidence_differences=evidence_differences,
            )
            if comparison:
                return ProductAdvice(comparison, bool(evidence_differences))

        deterministic = deterministic_answer(question, products)
        if deterministic:
            return ProductAdvice(deterministic, False)

        if self.ai_available:
            text = await self.ai.answer_product_question(question, products)
            if text:
                return ProductAdvice(text, True)

        return ProductAdvice(
            "По данным этих карточек не могу надёжно это подтвердить. "
            "Не хочу угадывать характеристики или совместимость.",
            False,
        )
