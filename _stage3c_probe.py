# -*- coding: utf-8 -*-
"""Stage 3C pre-implementation probe: deterministic parser / merge facts."""
import asyncio
import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from services.ai_service import AIService
from services.conversation import (
    _REQUEST_SCAFFOLD_STEMS,
    _names_new_product,
    deterministic_merge,
    is_refinement,
)

p = lambda t: asyncio.run(AIService(api_key=None).parse_query(t))

prev = p("Нужны беспроводные наушники до $30")
print("PREV:", prev.model_dump(mode="json"))
for text in (
    "а до 20 долларов",
    "а с микрофоном, до 20 долларов",
    "а с микрофоном",
    "а любая цена",
    "а цена в евро",
    "без провода",
    "а нужны кроссовки?",
    "а самый дешевый",
):
    fu = p(text)
    print("---", text)
    print(
        "  FU:",
        {
            k: v
            for k, v in fu.model_dump(mode="json").items()
            if v not in (None, [], "", 1, False, "relevance")
        },
    )
    m, ref = deterministic_merge(prev, fu, text)
    print(
        "  DETERMINISTIC refined=%s max=%s min=%s cur=%s req=%s exc=%s "
        "norm=%r sort=%s cat=%s"
        % (
            ref,
            m.max_price,
            m.min_price,
            m.currency,
            m.required_features,
            m.excluded_features,
            m.normalized_query,
            m.sort_preference.value,
            m.category,
        )
    )
    print(
        "  new_product=%r is_refinement=%s"
        % (_names_new_product(text, prev), is_refinement(prev, fu, text))
    )
print("SCAFFOLD:", sorted(_REQUEST_SCAFFOLD_STEMS))

print("== planned test messages ==")
for text in ("а до 1500 рублей", "а с хорошим рейтингом"):
    fu = p(text)
    print("---", text)
    print("  FU:", {k: v for k, v in fu.model_dump(mode="json").items()
                    if v not in (None, [], "", 1, False, "relevance")})
    m, ref = deterministic_merge(prev, fu, text)
    print("  DETERMINISTIC refined=%s max=%s cur=%s req=%s norm=%r"
          % (ref, m.max_price, m.currency, m.required_features, m.normalized_query))
    print("  new_product=%r is_refinement=%s prefers_good=%s"
          % (_names_new_product(text, prev), is_refinement(prev, fu, text),
             fu.prefers_good_rating))

