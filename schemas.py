"""
Pydantic schemas used for structured Ollama routing decisions.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class _RouteFields(BaseModel):
    """Fields shared by TriageDecision and RouteDecision."""

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
            "false for every other route."
        ),
    )


class TriageDecision(_RouteFields):
    """
    Structured triage result produced by the local Qwen router.

    route is deliberately restricted to the four natural-language routes —
    'claude_dev' (self-editing /fix and /code) is excluded from this enum at
    the schema level, not just by prompt wording, so Ollama's structured
    output can never select it even by chance. See router.py, which uses
    `TriageDecision.model_json_schema()` (not RouteDecision's) as the
    `format=` constraint for the Ollama call.

    route:
      - local          → Answer locally with Qwen (no Claude CLI)
      - claude_web     → Claude CLI with Chrome Native Messaging (--chrome)
      - claude_project → Claude CLI scoped to SANDBOX_DIR/projects/<name>/, no
                          Chrome (the active project's thinking-partner work —
                          notes, brainstorming, writing, organizing)
      - claude_general → Claude CLI sandboxed to SANDBOX_DIR, no Chrome
                          (other terminal/file tasks)
    """

    route: Literal["local", "claude_web", "claude_project", "claude_general"] = Field(
        ...,
        description=(
            "Executor pathway: local Qwen chat, Claude with --chrome, Claude "
            "scoped to the active project directory, or Claude general/desktop work."
        ),
    )


class RouteDecision(_RouteFields):
    """
    Full route set, used internally once a route has been decided — either by
    Ollama triage (a TriageDecision, converted to this) or forced directly by
    an explicit slash command (queue_manager.py's forced_route bypass, which
    is the only way 'claude_dev' and 'claude_research' are ever set).

    route adds:
      - claude_dev      → Claude CLI scoped to the bot's own project root, no
                          Chrome (bug fixes / new features in this bot's own
                          source, via /fix and /code). Never reachable through
                          natural-language triage — see TriageDecision.
      - claude_research → Two-step bridge for /research: Claude browses the
                          web (--chrome, like claude_web) to gather findings,
                          then files them into the active project via the
                          same synthesize pipeline claude_project uses. Also
                          never reachable through natural-language triage —
                          kept explicit-only so routine project work never
                          risks launching a browser unexpectedly.
    """

    route: Literal[
        "local", "claude_web", "claude_project", "claude_general", "claude_dev", "claude_research"
    ] = Field(
        ...,
        description=(
            "Executor pathway: local Qwen chat, Claude with --chrome, Claude "
            "scoped to the active project directory, Claude general/desktop "
            "work, Claude scoped to the bot's own source (claude_dev), or the "
            "web-research-into-project bridge (claude_research) — the last "
            "two are forced-route only, never selected by triage."
        ),
    )
