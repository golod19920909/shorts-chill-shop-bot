"""Validated routing between general chat and the existing shopping engine."""

from __future__ import annotations


class AssistantRouter:
    """Use the existing bounded Gemini interpreter; never perform retrieval."""

    def __init__(self, ai_service) -> None:
        self._ai = ai_service

    async def classify(
        self,
        text: str,
        *,
        chat_state,
        shopping_state,
        shown_product_count: int = 0,
    ) -> dict | None:
        if self._ai is None or not getattr(self._ai, "is_available", False):
            return None

        context = {
            "recent_turns": [turn.content for turn in chat_state.turns[-8:]],
            "active_slots": [slot.as_dict() for slot in shopping_state.active()],
            "shown_product_count": max(0, int(shown_product_count)),
            "last_outcome": shopping_state.last_outcome,
            "pending_question": shopping_state.last_question,
        }
        return await self._ai.decide_conversation_turn(text, context=context)
