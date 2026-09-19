"""
Self-maintenance executor for /fix and /code.

Runs Claude CLI directly against this bot's own source tree
(config.PROJECT_ROOT), not the sandbox — a deliberately narrow exception to
the sandbox-only guard in executors/claude_cli.py, only reachable via the
explicit /fix and /code Telegram commands (queue_manager.py forces the route;
Ollama's natural-language triage never selects it). Always runs without
--chrome. The project directory is a git repo specifically so these edits can
be diffed and reverted — see the baseline commit.

Editing source files here does not hot-reload the running bot; changes only
take effect after a restart.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Optional

import config
from executors.claude_cli import run_claude_cli

logger = logging.getLogger(__name__)

StatusCallback = Callable[[str], Awaitable[None]]

_FIX_TEMPLATE = """You are debugging this Telegram bot's own source code, in this
directory ({project_root}). The project is a git repo, so your changes can be
diffed and reverted if needed.

The user has reported the following bug. Investigate the root cause first —
check recent files under logs/ if that's relevant to what's failing — then fix
it precisely. Do not add new features, refactor unrelated code, or change
behavior beyond what's needed to fix this bug.

Note: editing these files does not hot-reload the running bot; the fix only
takes effect after it's restarted.

Reply with a short summary: the root cause, and which file(s) you changed.

Bug report: {task}
"""

_CODE_TEMPLATE = """You are implementing a new feature in this Telegram bot's own
source code, in this directory ({project_root}). The project is a git repo, so
your changes can be diffed and reverted if needed.

Follow the existing code style and architecture — check CLAUDE.md if present,
otherwise infer conventions from the surrounding modules (e.g. how executors/,
queue_manager.py, and main.py's command handlers are structured). Implement
exactly what's requested below; don't add speculative extras beyond it.

Note: editing these files does not hot-reload the running bot; the feature
only takes effect after it's restarted.

Reply with a short summary: what you built, and which file(s) you changed.

Feature request: {task}
"""


def _build_prompt(task: str, mode: str) -> str:
    template = _CODE_TEMPLATE if mode == "code" else _FIX_TEMPLATE
    return template.format(project_root=config.PROJECT_ROOT, task=task)


async def run_dev_task(
    task: str,
    mode: str = "fix",
    status_callback: Optional[StatusCallback] = None,
    model: Optional[str] = "claude-sonnet",
    chat_id: int = 0,
    message_id: int = 0,
) -> tuple[str, bool]:
    """
    Execute a self-maintenance task (bug fix or new feature) via Claude CLI,
    cwd scoped to the bot's own project root, always without --chrome.

    `mode`: "fix" (locate and correct a bug, no scope creep) or "code" (build
    a requested new feature).
    """
    prompt = _build_prompt(task, mode)

    if status_callback is not None:
        label = "fix" if mode == "fix" else "feature"
        await status_callback(f"🛠️ Working on a {label} in the bot's own source...")

    return await run_claude_cli(
        task=prompt,
        use_chrome=False,
        cwd=config.PROJECT_ROOT,
        allowed_root=config.PROJECT_ROOT,
        status_callback=status_callback,
        model=model,
        chat_id=chat_id,
        message_id=message_id,
    )
