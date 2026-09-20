"""
FIFO asyncio task queue for Telegram → Claude CLI serialization.

Chrome Native Messaging cannot handle concurrent Claude CLI processes, so every
inbound request is acknowledged with its queue position and processed one-by-one
by a single dedicated worker loop.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, RetryAfter, TelegramError

import config
import workspace
from executors import (
    run_claude_cli,
    run_desktop_task,
    run_dev_task,
    run_research_task,
    run_text_task,
    run_workspace_task,
)
from memory import conversation_memory
from router import route_command
from schemas import RouteDecision

if TYPE_CHECKING:
    # Imported lazily at runtime inside the worker to avoid circular imports.
    pass

logger = logging.getLogger(__name__)


@dataclass
class TaskItem:
    """One unit of work waiting in the FIFO queue."""

    chat_id: int
    message_id: int
    user_prompt: str
    # Set by explicit slash commands (e.g. /write, /brainstorm) to skip Ollama
    # triage entirely and dispatch straight to a known route.
    forced_route: Optional[str] = None
    # Only meaningful when forced_route == "claude_project": "write",
    # "brainstorm", "braindump", "keep", "panel", or "paneledit".
    project_mode: Optional[str] = None
    # The project name active at enqueue time — used by both
    # forced_route == "claude_project" and "claude_research".
    project: Optional[str] = None
    # Only meaningful when project_mode == "write": the target file within `project`.
    target_file: Optional[str] = None
    # Only meaningful when forced_route == "claude_dev": "fix" or "code".
    dev_mode: Optional[str] = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def _truncate_for_telegram(text: str, limit: int | None = None) -> str:
    """
    Fit `text` into a Telegram message.

    When truncated, earlier content is dropped and a `... [truncated]` header
    is prepended so the live-edit view always shows the newest log tail.
    """
    if limit is None:
        limit = config.TELEGRAM_MAX_MESSAGE_LENGTH
    if len(text) <= limit:
        return text

    header = "... [truncated]\n"
    # Leave room for the header.
    keep = max(0, limit - len(header))
    return header + text[-keep:]


class TaskQueue:
    """
    Single-consumer FIFO queue.

    enqueue() returns the 1-based position of the newly added item (queue depth
    after insertion). The background worker pulls items strictly in order.
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[TaskItem] = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task[None]] = None
        self._current_task: Optional[asyncio.Task[None]] = None
        self._pending_confirmations: dict[str, "asyncio.Future[bool]"] = {}
        self._stop_event = asyncio.Event()

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    def cancel_current(self) -> bool:
        """Cancel the task currently being processed, if any. /cancel and /stop use this."""
        if self._current_task is not None and not self._current_task.done():
            self._current_task.cancel()
            return True
        return False

    def clear_pending(self) -> int:
        """Drop all not-yet-started queued tasks. Returns how many were removed."""
        removed = 0
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._queue.task_done()
            removed += 1
        return removed

    def resolve_confirmation(self, confirmation_id: str, approved: bool) -> bool:
        """
        Resolve a pending destructive-action confirmation from its inline
        keyboard callback. Returns True if a confirmation was actually pending.
        """
        future = self._pending_confirmations.pop(confirmation_id, None)
        if future is None or future.done():
            return False
        future.set_result(approved)
        return True

    async def enqueue(self, item: TaskItem) -> int:
        """Place `item` on the queue and return its 1-based position / depth."""
        await self._queue.put(item)
        depth = self._queue.qsize()
        logger.info(
            "Enqueued task chat_id=%s depth=%s prompt_len=%s",
            item.chat_id,
            depth,
            len(item.user_prompt),
        )
        return depth

    def start(self, bot: Bot) -> None:
        """Launch the background worker if it is not already running."""
        if self._worker_task is not None and not self._worker_task.done():
            return
        self._stop_event.clear()
        self._worker_task = asyncio.create_task(
            self.worker(bot),
            name="task-queue-worker",
        )
        logger.info("TaskQueue worker started")

    async def stop(self, timeout: float = 30.0) -> None:
        """Signal the worker to exit and wait briefly for in-flight work."""
        self._stop_event.set()
        if self._worker_task is None:
            return

        # Unblock a waiting get() so the stop flag is observed promptly.
        try:
            self._queue.put_nowait(
                TaskItem(chat_id=0, message_id=0, user_prompt="__SHUTDOWN__")
            )
        except asyncio.QueueFull:
            pass

        try:
            await asyncio.wait_for(self._worker_task, timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("TaskQueue worker did not stop within %.1fs; cancelling", timeout)
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        finally:
            self._worker_task = None
            logger.info("TaskQueue worker stopped")

    async def worker(self, bot: Bot) -> None:
        """Continuously pull items, route, execute, and deliver results."""
        # Local import avoids circular dependency with main's delivery helpers.
        from main import deliver_final_result, edit_status_message

        logger.info("Worker loop entering")
        while not self._stop_event.is_set():
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            if item.user_prompt == "__SHUTDOWN__" or self._stop_event.is_set():
                self._queue.task_done()
                break

            status_message_id = item.message_id
            self._current_task = asyncio.create_task(
                self._process_item(bot, item, status_message_id, edit_status_message, deliver_final_result),
                name="task-queue-current-item",
            )
            try:
                await self._current_task
            except asyncio.CancelledError:
                logger.info("Task chat_id=%s cancelled by user", item.chat_id)
                try:
                    await bot.edit_message_text(
                        chat_id=item.chat_id,
                        message_id=status_message_id,
                        text="⏹ Task cancelled.",
                    )
                except TelegramError:
                    logger.exception("Failed to edit status message after cancellation")
            except Exception:  # noqa: BLE001
                logger.exception("Unhandled error processing task chat_id=%s", item.chat_id)
                try:
                    await bot.send_message(
                        chat_id=item.chat_id,
                        text="❌ Task failed due to an internal error. Check server logs.",
                        reply_to_message_id=item.message_id,
                    )
                except TelegramError:
                    logger.exception("Failed to notify user of internal error")
            finally:
                self._current_task = None
                self._queue.task_done()

        logger.info("Worker loop exiting")

    async def _process_item(
        self,
        bot: Bot,
        item: TaskItem,
        status_message_id: int,
        edit_status_message,
        deliver_final_result,
    ) -> None:
        chat_id = item.chat_id

        async def status_callback(buffer: str) -> None:
            await edit_status_message(
                bot,
                chat_id=chat_id,
                message_id=status_message_id,
                text=_truncate_for_telegram(buffer),
            )

        if item.forced_route:
            # Explicit slash command (e.g. /write, /brainstorm) — bypass Ollama
            # triage entirely and dispatch straight to the known route.
            command_label = (
                item.project_mode
                or item.dev_mode
                or ("research" if item.forced_route == "claude_research" else item.forced_route)
            )
            decision = RouteDecision(
                route=item.forced_route,
                model="claude-sonnet",
                task=item.user_prompt,
                rationale=f"Explicit /{command_label} command — routing skipped.",
                risky=False,
            )
            await edit_status_message(
                bot,
                chat_id=chat_id,
                message_id=status_message_id,
                text=(
                    f"🧠 Route: `{decision.route}` (explicit command)\n\n"
                    f"▶️ Executing..."
                ),
            )
        else:
            await edit_status_message(
                bot,
                chat_id=chat_id,
                message_id=status_message_id,
                text="🔄 Routing with local Qwen...",
            )

            history_text = conversation_memory.format_for_prompt(chat_id)
            decision = await route_command(item.user_prompt, history=history_text)

            await edit_status_message(
                bot,
                chat_id=chat_id,
                message_id=status_message_id,
                text=(
                    f"🧭 Route: `{decision.route}` | Model: `{decision.model}`\n"
                    f"Reason: {decision.rationale}\n\n"
                    f"▶️ Executing..."
                ),
            )

        if decision.route == "claude_web":
            output, success = await run_claude_cli(
                task=decision.task,
                use_chrome=True,
                status_callback=status_callback,
                model=decision.model,
                chat_id=chat_id,
                message_id=status_message_id,
            )
        elif decision.route == "claude_project":
            # item.project is set when an explicit /write, /brainstorm,
            # /braindump, or /keep command enqueued this; for a natural-
            # language message (no forced_route), fall back to whatever
            # project is currently active for this chat.
            project = item.project or workspace.get_active_project(chat_id)
            if project is None:
                output, success = (workspace.no_active_project_message(), False)
            else:
                project_mode = item.project_mode or "auto"
                output, success = await run_workspace_task(
                    task=decision.task,
                    project=project,
                    mode=project_mode,
                    target_file=item.target_file,
                    status_callback=status_callback,
                    model=decision.model,
                    chat_id=chat_id,
                    message_id=status_message_id,
                )
                if project_mode in ("brainstorm", "panel") and success:
                    # /keep draws on this later to commit chosen parts without
                    # anything from a brainstorm or panel ever being applied automatically.
                    workspace.set_last_brainstorm(chat_id, project, output)
        elif decision.route == "claude_research":
            # Only ever reached via /research's forced_route bypass — never
            # selected by Ollama triage, so routine project work never risks
            # launching a browser unexpectedly.
            project = item.project or workspace.get_active_project(chat_id)
            if project is None:
                output, success = (workspace.no_active_project_message(), False)
            else:
                output, success = await run_research_task(
                    query=decision.task,
                    project=project,
                    status_callback=status_callback,
                    model=decision.model,
                    chat_id=chat_id,
                    message_id=status_message_id,
                )
        elif decision.route == "claude_dev":
            # Only ever reached via /fix or /code's forced_route bypass —
            # TriageDecision's schema excludes this route from Ollama triage.
            output, success = await run_dev_task(
                task=decision.task,
                mode=item.dev_mode or "fix",
                status_callback=status_callback,
                model=decision.model,
                chat_id=chat_id,
                message_id=status_message_id,
            )
        elif decision.route == "claude_general":
            approved = True
            if decision.risky:
                approved = await self._confirm_destructive_action(bot, chat_id, decision.task)
            if approved:
                output, success = await run_desktop_task(
                    task=decision.task,
                    status_callback=status_callback,
                    model=decision.model,
                    chat_id=chat_id,
                    message_id=status_message_id,
                )
            else:
                output, success = (
                    "⏹ Destructive action was not confirmed; task skipped.",
                    False,
                )
        else:
            output, success = await run_text_task(
                task=decision.task,
                status_callback=status_callback,
                history=conversation_memory.get_history(chat_id),
                chat_id=chat_id,
                message_id=status_message_id,
            )

        conversation_memory.add_turn(
            chat_id,
            user_prompt=item.user_prompt,
            response=output,
            route=decision.route,
            model=decision.model,
        )

        header = (
            f"{'✅' if success else '⚠️'} Done | route=`{decision.route}` | "
            f"model=`{decision.model}`\n"
            f"_{decision.rationale}_\n\n"
        )
        if decision.route == "claude_dev" and success:
            header += "🔄 Source changed — restart the bot for this to take effect.\n\n"
        await deliver_final_result(
            bot,
            chat_id=chat_id,
            status_message_id=status_message_id,
            header=header,
            body=output,
            success=success,
        )

    async def _confirm_destructive_action(self, bot: Bot, chat_id: int, task: str) -> bool:
        """
        Ask the user to confirm a risky desktop task via an inline keyboard,
        blocking the worker until they respond or the timeout elapses.
        """
        confirmation_id = uuid.uuid4().hex
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._pending_confirmations[confirmation_id] = future

        prompt_text = (
            "⚠️ This looks like a destructive desktop action:\n\n"
            f"{task}\n\n"
            "Proceed?"
        )
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Proceed", callback_data=f"confirm:{confirmation_id}"),
                    InlineKeyboardButton("❌ Cancel", callback_data=f"reject:{confirmation_id}"),
                ]
            ]
        )

        try:
            prompt_message = await bot.send_message(chat_id=chat_id, text=prompt_text, reply_markup=keyboard)
        except TelegramError:
            logger.exception("Failed to send confirmation prompt; treating risky task as declined")
            self._pending_confirmations.pop(confirmation_id, None)
            return False

        try:
            try:
                approved = await asyncio.wait_for(
                    future, timeout=config.DESTRUCTIVE_CONFIRMATION_TIMEOUT_SECONDS
                )
                decision_text = "✅ Confirmed — proceeding." if approved else "❌ Cancelled."
            except asyncio.TimeoutError:
                approved = False
                decision_text = "⏱ Timed out — treated as cancelled."

            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=prompt_message.message_id,
                    text=f"{prompt_text}\n\n{decision_text}",
                )
            except TelegramError:
                logger.exception("Failed to edit confirmation prompt with final decision")

            return approved
        finally:
            self._pending_confirmations.pop(confirmation_id, None)


# Shared singleton used by main.py
task_queue = TaskQueue()
