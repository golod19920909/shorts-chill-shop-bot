# -*- coding: utf-8 -*-
"""Live, read-only probe of the current assistant routing boundary.

Runs the B5 text dataset through AssistantRouter only. It does not call
AliExpress, mutate user state, or claim to benchmark the full shopping engine.
Requires GEMINI_API_KEY for meaningful live results; without it every case
reports an unavailable route rather than pretending the model was tested.
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
from services.assistant.router import AssistantRouter  # noqa: E402
from services.assistant.state import ChatConversationState  # noqa: E402
from services.convsearch.state import ConversationEngineState  # noqa: E402
from eval_b5_dataset import B5_DATASET  # noqa: E402


async def main() -> None:
    settings = load_settings()
    ai = AIService(
        api_key=settings.gemini_api_key,
        model=settings.gemini_smart_model,
        fast_model=settings.gemini_fast_model,
        vision_model=settings.gemini_vision_model,
        fallback_model=settings.gemini_fallback_model,
    )
    router = AssistantRouter(ai)
    print("ai_available:", ai.is_available)
    for case in B5_DATASET:
        query = case["query"]
        try:
            decision = await router.classify(
                query,
                chat_state=ChatConversationState(),
                shopping_state=ConversationEngineState(user_key=case["id"]),
            )
            if not isinstance(decision, dict):
                line = f"{case['id']} route=unavailable"
            else:
                line = (
                    f"{case['id']} {case['expected_outcome_hint']:<26} "
                    f"route={decision.get('action', 'invalid')} "
                    f"product_type={decision.get('product_type')!r} "
                    f"question={decision.get('question')!r}"
                )
        except Exception as exc:  # noqa: BLE001 - probe reports per-case failures
            line = f"{case['id']} ERROR {type(exc).__name__}: {exc}"
        print(line)


if __name__ == "__main__":
    asyncio.run(main())
