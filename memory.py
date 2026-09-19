"""
In-process short-term conversation memory.

Lives only in this process's memory and is never written to disk, so a bot
restart starts every chat with a clean slate by design. Capped per chat via
a bounded deque so a long-running session can't grow this unbounded.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone

import config


@dataclass
class Turn:
    """One completed exchange in a chat's recent history."""

    user_prompt: str
    response_excerpt: str
    route: str
    model: str
    timestamp: datetime


class ConversationMemory:
    """Per-chat rolling window of recent turns, capped at config.MEMORY_MAX_TURNS."""

    def __init__(self) -> None:
        self._history: dict[int, deque[Turn]] = {}

    def add_turn(
        self,
        chat_id: int,
        *,
        user_prompt: str,
        response: str,
        route: str,
        model: str,
    ) -> None:
        """Record a completed turn, truncating the response for prompt reuse."""
        turns = self._history.setdefault(chat_id, deque(maxlen=config.MEMORY_MAX_TURNS))
        limit = config.MEMORY_RESPONSE_TRUNCATE_CHARS
        excerpt = response[:limit] + ("…" if len(response) > limit else "")
        turns.append(
            Turn(
                user_prompt=user_prompt,
                response_excerpt=excerpt,
                route=route,
                model=model,
                timestamp=datetime.now(timezone.utc),
            )
        )

    def get_history(self, chat_id: int) -> list[Turn]:
        """Return recent turns oldest-first; empty list if this chat has none yet."""
        return list(self._history.get(chat_id, ()))

    def clear(self, chat_id: int) -> bool:
        """Drop all remembered turns for one chat. Returns True if there was anything to clear."""
        return self._history.pop(chat_id, None) is not None

    def format_for_prompt(self, chat_id: int) -> str:
        """Render recent turns as a compact text block for prompt injection."""
        turns = self.get_history(chat_id)
        if not turns:
            return ""
        lines: list[str] = []
        for turn in turns:
            lines.append(f"User: {turn.user_prompt}")
            lines.append(f"Assistant ({turn.route}/{turn.model}): {turn.response_excerpt}")
        return "\n".join(lines)


# Shared singleton used by queue_manager.py. Safe without locking because the
# TaskQueue worker processes one item at a time.
conversation_memory = ConversationMemory()
