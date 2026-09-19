"""
Pydantic schemas used for structured Ollama routing decisions.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class RouteDecision(BaseModel):
    """
    Structured triage result produced by the local Qwen router.

    route:
      - local          → Answer locally with Qwen (no Claude CLI)
      - claude_web     → Claude CLI with Chrome Native Messaging (--chrome)
      - claude_book    → Claude CLI scoped to SANDBOX_DIR/book_project/, no
                          Chrome (manuscript writing, story brainstorming,
                          lore analysis)
      - claude_general → Claude CLI sandboxed to SANDBOX_DIR, no Chrome
                          (other terminal/file tasks)
    model:
      - Which backend capability the router believes is appropriate. These are
        generation-agnostic tiers (not tied to a specific Claude version) that
        the Claude CLI resolves to its current model for that tier via --model.
    """

    route: Literal["local", "claude_web", "claude_book", "claude_general"] = Field(
        ...,
        description=(
            "Executor pathway: local Qwen chat, Claude with --chrome, Claude "
            "scoped to book_project/, or Claude general/desktop work."
        ),
    )
    model: Literal["claude-opus", "claude-sonnet", "claude-haiku", "local-qwen"] = Field(
        ...,
        description="Preferred model tier for this task.",
    )
    task: str = Field(
        ...,
        description="Clean, distilled prompt to pass to the selected executor.",
        min_length=1,
    )
    rationale: str = Field(
        ...,
        description="Brief explanation of why this route was selected.",
        min_length=1,
    )
    risky: bool = Field(
        default=False,
        description=(
            "True only for 'claude_general' tasks that are destructive or hard "
            "to undo (deleting/overwriting files, formatting, force-push, mass "
            "edits, uninstalling software, system/network changes). Always "
            "false for 'local', 'claude_web', and 'claude_book' routes."
        ),
    )
