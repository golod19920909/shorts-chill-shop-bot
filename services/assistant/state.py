"""Bounded per-user memory for ordinary assistant conversations."""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class ChatTurn:
    role: str
    content: str
    created_at: float = field(default_factory=time.monotonic)


@dataclass
class ChatConversationState:
    """Short-lived chat history, intentionally separate from shopping slots."""

    turns: list[ChatTurn] = field(default_factory=list)
    updated_at: float = field(default_factory=time.monotonic)

    MAX_TURNS = 12
    MAX_CONTENT_CHARS = 1800
    TTL_SECONDS = 12 * 60 * 60

    def touch(self) -> None:
        self.updated_at = time.monotonic()

    def is_expired(self) -> bool:
        return time.monotonic() - self.updated_at > self.TTL_SECONDS

    def add(self, role: str, content: str) -> None:
        if role not in {"user", "assistant"} or not isinstance(content, str):
            return
        cleaned = " ".join(content.split()).strip()
        if not cleaned:
            return
        self.turns.append(
            ChatTurn(role, cleaned[: self.MAX_CONTENT_CHARS], time.monotonic())
        )
        self.turns = self.turns[-self.MAX_TURNS :]
        self.touch()

    def as_prompt_history(self) -> list[dict[str, str]]:
        return [{"role": turn.role, "content": turn.content} for turn in self.turns]
