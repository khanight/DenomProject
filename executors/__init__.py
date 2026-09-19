"""
Executor package: Claude CLI (browser / desktop / book) and local text helpers.
"""

from executors.book import run_book_task
from executors.claude_cli import run_claude_cli
from executors.desktop import run_desktop_task, run_text_task

__all__ = [
    "run_book_task",
    "run_claude_cli",
    "run_desktop_task",
    "run_text_task",
]
