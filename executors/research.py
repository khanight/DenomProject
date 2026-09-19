"""
Research bridge executor for /research.

Chains two separate Claude CLI invocations:
  1. Live web research with --chrome, scoped to SANDBOX_DIR like any other
     claude_web task.
  2. Filing the findings into the active project's files via the same
     synthesize-and-merge pipeline /braindump and /keep use.

Kept as an explicit, opt-in command rather than folded into the project
executor's default behavior, so routine project work (/write, /brainstorm,
/braindump, /keep) never risks launching a browser window unexpectedly.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Optional

import workspace
from executors.claude_cli import run_claude_cli

logger = logging.getLogger(__name__)

StatusCallback = Callable[[str], Awaitable[None]]

_RESEARCH_PROMPT_TEMPLATE = """You are doing live web research to support an ongoing project.

Research the following and gather concrete, useful findings — facts,
figures, examples, quotes, whatever is genuinely relevant. Where practical,
note the source (site/page) for each finding so it can be cited later.

Do not write to any files — just report your findings clearly.

Research request: {query}
"""


async def run_research_task(
    query: str,
    project: str,
    status_callback: Optional[StatusCallback] = None,
    model: Optional[str] = "claude-sonnet",
    chat_id: int = 0,
    message_id: int = 0,
) -> tuple[str, bool]:
    """
    Two-step research bridge for /research:

    1. Browse the web (--chrome, cwd=SANDBOX_DIR, same as any claude_web
       task) to gather findings on `query`.
    2. File the findings into `project` via the same synthesize-and-merge
       pipeline /braindump uses (workspace's "research" prompt mode) —
       merging into existing files or creating new ones.

    Returns the combined output of both steps and whether both succeeded.
    If step 1 fails, step 2 is skipped entirely (nothing to file).
    """
    if status_callback is not None:
        await status_callback(f"🌐 Researching via Chrome: {query}")

    research_prompt = _RESEARCH_PROMPT_TEMPLATE.format(query=query)
    findings, research_ok = await run_claude_cli(
        task=research_prompt,
        use_chrome=True,
        status_callback=status_callback,
        model=model,
        chat_id=chat_id,
        message_id=message_id,
    )

    if not research_ok:
        return f"🌐 Research step failed:\n{findings}", False

    if status_callback is not None:
        await status_callback(f"🧠 Filing findings into project '{project}'...")

    path = workspace.project_dir(project)
    workspace.ensure_project(project)
    existing_files = workspace.list_md_files(path)
    file_task = f"Research request: {query}\n\nFindings gathered from the web:\n{findings}"
    file_prompt = workspace.build_prompt(file_task, "research", existing_files)

    filed, file_ok = await run_claude_cli(
        task=file_prompt,
        use_chrome=False,
        cwd=path,
        status_callback=status_callback,
        model=model,
        chat_id=chat_id,
        message_id=message_id,
    )

    combined = (
        f"🌐 Research findings:\n{findings}\n\n"
        f"🧠 Filed into project '{project}':\n{filed}"
    )
    return combined, research_ok and file_ok
