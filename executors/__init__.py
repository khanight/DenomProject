"""
Executor package: Claude CLI (browser / desktop / project / dev / research) and local text helpers.
"""

from executors.claude_cli import run_claude_cli
from executors.desktop import run_desktop_task, run_text_task
from executors.dev import run_dev_task
from executors.research import run_research_task
from executors.workspace import run_workspace_task

__all__ = [
    "run_claude_cli",
    "run_desktop_task",
    "run_dev_task",
    "run_research_task",
    "run_text_task",
    "run_workspace_task",
]
