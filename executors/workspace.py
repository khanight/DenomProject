"""
Project Thinking Partner executor.

Runs Claude CLI scoped to SANDBOX_DIR/projects/<name>/ and always WITHOUT
--chrome, so project/notes work never hijacks the browser window used by the
web-automation pipeline. Coexists alongside run_claude_cli's --chrome and
desktop (no-chrome, SANDBOX_DIR root) paths without altering either.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Optional

import workspace
from executors.claude_cli import run_claude_cli

logger = logging.getLogger(__name__)

StatusCallback = Callable[[str], Awaitable[None]]


async def run_workspace_task(
    task: str,
    project: str,
    mode: str = "auto",
    target_file: Optional[str] = None,
    status_callback: Optional[StatusCallback] = None,
    model: Optional[str] = "claude-sonnet",
    chat_id: int = 0,
    message_id: int = 0,
) -> tuple[str, bool]:
    """
    Execute a thinking-partner task via Claude CLI, cwd scoped to
    projects/<project>/ and always without --chrome.

    `mode`:
      - "write"      -> continue/extend `target_file` (required for this mode).
      - "brainstorm" -> read all project markdown as context, answer without
                        modifying files (used by /brainstorm).
      - "braindump"  -> synthesize raw ideas into the right existing/new file(s).
      - "keep"       -> commit chosen parts of a prior /brainstorm reply.
      - "auto"       -> Claude decides what to do based on the request (used
                        for router-driven claude_project natural-language tasks).
    """
    path = workspace.project_dir(project)
    workspace.ensure_project(project)
    existing_files = workspace.list_md_files(path)
    prompt = workspace.build_prompt(task, mode, existing_files, target_file=target_file)

    if status_callback is not None:
        await status_callback(f"🧠 Running {mode} task in project '{project}'...")

    return await run_claude_cli(
        task=prompt,
        use_chrome=False,
        cwd=path,
        status_callback=status_callback,
        model=model,
        chat_id=chat_id,
        message_id=message_id,
    )
