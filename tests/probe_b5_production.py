# -*- coding: utf-8 -*-
"""Probe: run the PRODUCTION query-understanding path for the B5 dataset.

For every dataset query it shows what the app would actually do:
  - chat_router classification (search / clarify / compare / ...)
  - AI parse (normalized_query, keywords, price bounds, currency, mode)
  - structured intent (product_type, budget, source)
  - the effective request after resolve_request + apply_intent

Read-only probe: no state is changed, no product is shown to anybody.
"""
from __future__ import annotations

import asyncio
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config.settings import load_settings  # noqa: E402
from services.ai_service import AIService  # noqa: E402
from services.chat_router import classify  # noqa: E402
from services.conversation import resolve_request  # noqa: E402
from services.query_intent import apply_intent, parse_query_intent  # noqa: E402

from eval_b5_dataset import B5_DATASET  # noqa: E402


async def main() -> None:
    settings = load_settings()
    ai = AIService(settings.gemini_api_key)
    print("ai_available:", ai.is_available)
    for case in B5_DATASET:
        query = case["query"]
        decision = classify(query)
        try:
            parsed = await ai.parse_query(query)
            intent, meta = await parse_query_intent(query, ai)
            effective, refined = await resolve_request(None, parsed, query, ai)
            source = meta.source if ai.is_available else None
            effective = apply_intent(
                effective, intent, refined=refined, intent_source=source
            )
            line = (
                f"{case['id']} {case['expected_outcome_hint']:<26} "
                f"route={decision.action.value:<8} "
                f"norm={effective.normalized_query!r} "
                f"kw={list(effective.keywords)} "
                f"min={effective.min_price} max={effective.max_price} "
                f"cur={effective.currency} "
                f"itype={effective.intent_product_type!r} "
                f"isrc={meta.source}"
            )
        except Exception as exc:  # noqa: BLE001
            line = f"{case['id']} ERROR {type(exc).__name__}: {exc}"
        print(line)


if __name__ == "__main__":
    asyncio.run(main())
