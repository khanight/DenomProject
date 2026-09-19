"""
Book Writing & Lore Tracker executor.

Runs Claude CLI scoped to SANDBOX_DIR/book_project/ and always WITHOUT
--chrome, so manuscript/lore work never hijacks the browser window used by
the web-automation pipeline. Coexists alongside run_claude_cli's --chrome
and desktop (no-chrome, SANDBOX_DIR root) paths without altering either.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Optional

import book
from executors.claude_cli import run_claude_cli

logger = logging.getLogger(__name__)

StatusCallback = Callable[[str], Awaitable[None]]


async def run_book_task(
    task: str,
    mode: str = "auto",
    status_callback: Optional[StatusCallback] = None,
    model: Optional[str] = "claude-sonnet",
    chat_id: int = 0,
    message_id: int = 0,
) -> tuple[str, bool]:
    """
    Execute a book-writing/brainstorming task via Claude CLI, cwd scoped to
    book_project/ and always without --chrome.

    `mode`:
      - "write"      -> read lore/character files + draft.md, continue the
                        scene, append to draft.md (used by /write).
      - "brainstorm" -> read all book markdown as context, answer without
                        modifying files (used by /brainstorm).
      - "auto"       -> Claude decides whether to update draft.md based on
                        the request (used for router-driven claude_book
                        natural-language tasks).
    """
    book.ensure_book_workspace()
    prompt = book.build_prompt(task, mode)

    if status_callback is not None:
        await status_callback(f"📖 Running book task ({mode}) in book_project/...")

    return await run_claude_cli(
        task=prompt,
        use_chrome=False,
        cwd=book.BOOK_DIR,
        status_callback=status_callback,
        model=model,
        chat_id=chat_id,
        message_id=message_id,
    )
