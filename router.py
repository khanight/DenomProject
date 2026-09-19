"""
Local Ollama / Qwen router.

Triages each incoming Telegram prompt into a RouteDecision using Pydantic
JSON-schema enforcement so the model cannot return free-form text.
"""

from __future__ import annotations

import logging

import ollama

import config
from schemas import RouteDecision

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are a routing classifier for a local Windows AI agent.

Given the user's message, decide how to handle it and return ONLY a JSON object
matching the provided schema.

Routing rules:
- "local": The task is conversational Q&A, general chatting, summarization, or
  offline reasoning that needs no tools. Always use model "local-qwen".
- "claude_web": The task needs live web browsing, filling web forms, reading
  online pages, scraping, or real-time lookups — any Chrome / web automation.
  Prefer model "claude-opus" for complex, high-stakes, or multi-step browser
  work; "claude-sonnet" for everyday browser tasks; "claude-haiku" for simple
  lookups.
- "claude_book": The task is manuscript writing/continuing a scene, deep story
  brainstorming, untangling a plot blocker, or analyzing established lore/
  characters for an ongoing novel project. Prefer "claude-opus" for complex
  plotting or long scenes; "claude-sonnet" for everyday writing/brainstorming;
  "claude-haiku" for quick lore lookups.
- "claude_general": The task is other local filesystem work, scripts, coding,
  terminal commands, or desktop-adjacent work inside the sandbox that is not
  book/manuscript related. Prefer "claude-opus" for complex or high-stakes
  coding/refactoring; "claude-sonnet" for everyday coding tasks; "claude-haiku"
  for simple file edits.

Set "risky": true only for "claude_general" tasks that are destructive or hard
to undo — deleting or overwriting files/directories, formatting drives,
force-pushing or resetting git history, recursive deletes, uninstalling
software, modifying system/network settings, or anything that could cause
data loss or system changes outside the sandbox. Otherwise set "risky": false.
Always set "risky": false for "local", "claude_web", and "claude_book" routes.

Write "task" as a clean, self-contained instruction for the executor.
Write "rationale" as one short sentence explaining the choice.
"""


async def route_command(user_message: str, history: str = "") -> RouteDecision:
    """
    Ask Ollama (Qwen) to classify `user_message` into a RouteDecision.

    `history` is an optional compact block of recent turns in this chat
    (see memory.ConversationMemory.format_for_prompt) used to disambiguate
    follow-ups like "do that again"; pass "" for a fresh chat.

    Uses format=RouteDecision.model_json_schema() and temperature=0 for
    deterministic, schema-valid JSON output.
    """
    client = ollama.AsyncClient(host=config.OLLAMA_HOST)

    user_content = user_message
    if history:
        user_content = (
            f"Recent conversation (most recent last):\n{history}\n\n"
            f"New message:\n{user_message}"
        )

    logger.info("Routing message via Ollama model=%s", config.OLLAMA_MODEL)
    response = await client.chat(
        model=config.OLLAMA_MODEL,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        format=RouteDecision.model_json_schema(),
        options={"temperature": 0},
    )

    raw = response["message"]["content"]
    decision = RouteDecision.model_validate_json(raw)
    logger.info(
        "RouteDecision route=%s model=%s rationale=%s",
        decision.route,
        decision.model,
        decision.rationale,
    )
    return decision
