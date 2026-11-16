# -*- coding: utf-8 -*-
"""Legacy B5 diagnostic probe for the historical query-understanding path.

LIVE / API-COSTING: requires GEMINI_API_KEY and sends model requests for the
dataset cases; each run can incur Gemini usage charges. It is deliberately
excluded from CI and must not be treated as an offline test.

This probe uses the historical chat_router / query_intent / resolve_request
pipeline. It is NOT the current Option C ConversationSearchEngine path.
It is read-only with respect to bot state and never shows products to users.
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
