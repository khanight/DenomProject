"""
Telegram bot entry point for the local Windows AI agent.

Architecture reminders:
  - Auth gate: only TELEGRAM_ALLOWED_USER_IDS may enqueue work.
  - FIFO TaskQueue: Claude CLI / Chrome Native Messaging are strictly serial.
  - Sandbox: all Claude CLI subprocesses use cwd=config.SANDBOX_DIR.
  - Payload safety: live edits truncate with "... [truncated]"; finals over
    3,500 characters ship an excerpt plus a .txt document attachment.
"""

from __future__ import annotations

import asyncio
import io
import logging
import signal
import sys
from typing import Optional

from telegram import Bot, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.error import BadRequest, RetryAfter, TelegramError

import book
import config
from memory import conversation_memory
from queue_manager import TaskItem, task_queue

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------

def _truncate_live(text: str) -> str:
    """Truncate for live message edits (Telegram 4096 hard limit)."""
    limit = config.TELEGRAM_MAX_MESSAGE_LENGTH
    if len(text) <= limit:
        return text
    header = "... [truncated]\n"
    keep = max(0, limit - len(header))
    return header + text[-keep:]


async def edit_status_message(
    bot: Bot,
    *,
    chat_id: int,
    message_id: int,
    text: str,
) -> None:
    """
    Best-effort edit of the status message.

    Ignores 'message is not modified' and backs off briefly on RetryAfter (429).
    """
    safe = _truncate_live(text)
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=safe,
            parse_mode=None,  # raw logs may contain Markdown-breaking characters
        )
    except RetryAfter as exc:
        logger.warning("Telegram 429 RetryAfter=%.1fs; sleeping", exc.retry_after)
        await asyncio.sleep(float(exc.retry_after) + 0.1)
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=safe,
            )
        except TelegramError:
            logger.exception("Retry edit_message_text failed")
    except BadRequest as exc:
        # Common when content is identical to the previous edit.
        if "message is not modified" not in str(exc).lower():
            logger.warning("edit_message_text BadRequest: %s", exc)
    except TelegramError:
        logger.exception("edit_message_text failed")


async def deliver_final_result(
    bot: Bot,
    *,
    chat_id: int,
    status_message_id: int,
    header: str,
    body: str,
    success: bool,
) -> None:
    """
    Deliver the executor result to Telegram.

    - If header+body fits within TELEGRAM_FINAL_INLINE_LIMIT (3500): edit/send text.
    - Otherwise: send a concise summary/excerpt, then attach full output as
      task_result.txt via send_document.
    """
    combined = f"{header}{body}".strip()
    limit = config.TELEGRAM_FINAL_INLINE_LIMIT

    if len(combined) <= limit:
        # Prefer updating the existing status message so the chat stays tidy.
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=status_message_id,
                text=combined,
            )
            return
        except TelegramError:
            logger.exception("Failed to edit final text; falling back to send_message")
            await bot.send_message(chat_id=chat_id, text=combined)
            return

    # Over limit → concise summary in-chat + full log as a document.
    excerpt_budget = max(200, limit - len(header) - 120)
    excerpt = body[:excerpt_budget].rstrip()
    if len(body) > excerpt_budget:
        excerpt += "\n…\n"

    summary = (
        f"{header}"
        f"{excerpt}\n"
        f"—\n"
        f"Output is {len(body):,} characters (exceeds {limit:,} inline limit). "
        f"Full log attached as task_result.txt."
    )
    summary = _truncate_live(summary)

    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=status_message_id,
            text=summary,
        )
    except TelegramError:
        logger.exception("Failed to edit summary; sending as new message")
        await bot.send_message(chat_id=chat_id, text=summary)

    document = io.BytesIO(body.encode("utf-8"))
    document.name = "task_result.txt"  # telegram relies on this for filename fallback
    try:
        await bot.send_document(
            chat_id=chat_id,
            document=document,
            filename="task_result.txt",
            caption=("✅ Full task output" if success else "⚠️ Full task output (completed with errors)"),
        )
    except TelegramError:
        logger.exception("send_document failed")
        await bot.send_message(
            chat_id=chat_id,
            text="Could not upload the full log document. Check server logs.",
        )


# ---------------------------------------------------------------------------
# Auth + handlers
# ---------------------------------------------------------------------------

def _is_authorized(user_id: Optional[int]) -> bool:
    return user_id is not None and user_id in config.ALLOWED_USER_IDS


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Auth-gate incoming text, enqueue, and acknowledge with queue position."""
    if update.effective_user is None or update.effective_chat is None or update.message is None:
        return

    user = update.effective_user
    if not _is_authorized(user.id):
        logger.warning("Rejected unauthorized user_id=%s", user.id)
        # Soft reject — do not leak that a bot exists more than necessary.
        try:
            await update.message.reply_text("⛔ Unauthorized.")
        except TelegramError:
            pass
        return

    prompt = (update.message.text or "").strip()
    if not prompt:
        await update.message.reply_text("Please send a non-empty text prompt.")
        return

    # Acknowledge immediately so the user sees their place in the FIFO.
    # Position is tentative until enqueue returns the authoritative depth.
    ack = await update.message.reply_text("⏳ Queuing task...")

    item = TaskItem(
        chat_id=update.effective_chat.id,
        message_id=ack.message_id,
        user_prompt=prompt,
    )
    position = await task_queue.enqueue(item)

    try:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=ack.message_id,
            text=f"Task queued (Position #{position})...",
        )
    except TelegramError:
        logger.exception("Failed to update queue acknowledgement")


def _command_text(update: Update) -> str:
    """Return the text after the leading /command token, preserved verbatim."""
    text = (update.message.text or "").strip()
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


async def _handle_fast_capture(
    update: Update, context: ContextTypes.DEFAULT_TYPE, command: str
) -> None:
    """
    /note, /char, /lore — direct, LLM-free append to a book_project/ markdown
    file. Handled entirely in Python before any routing/LLM call.
    """
    if update.effective_user is None or update.message is None:
        return

    if not _is_authorized(update.effective_user.id):
        logger.warning("Rejected unauthorized /%s attempt user_id=%s", command, update.effective_user.id)
        try:
            await update.message.reply_text("⛔ Unauthorized.")
        except TelegramError:
            pass
        return

    text = _command_text(update)
    if not text:
        await update.message.reply_text(f"Usage: /{command} <text>")
        return

    path, label = book.FAST_CAPTURE_TARGETS[command]
    book.ensure_book_workspace()
    book.append_timestamped(path, text)
    await update.message.reply_text(f"✅ Saved to {label}")


async def handle_note(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _handle_fast_capture(update, context, "note")


async def handle_char(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _handle_fast_capture(update, context, "char")


async def handle_lore(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _handle_fast_capture(update, context, "lore")


async def _enqueue_book_task(
    update: Update, context: ContextTypes.DEFAULT_TYPE, book_mode: str, payload: str
) -> None:
    """Enqueue a claude_book task directly, bypassing Ollama triage, but still
    serialized through the FIFO Claude CLI queue."""
    ack = await update.message.reply_text("⏳ Queuing book task...")

    item = TaskItem(
        chat_id=update.effective_chat.id,
        message_id=ack.message_id,
        user_prompt=payload,
        forced_route="claude_book",
        book_mode=book_mode,
    )
    position = await task_queue.enqueue(item)

    try:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=ack.message_id,
            text=f"📖 Book task queued (Position #{position})...",
        )
    except TelegramError:
        logger.exception("Failed to update queue acknowledgement")


async def _handle_book_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE, book_mode: str
) -> None:
    """/write, /brainstorm, /braindump — prompt text comes straight from the command."""
    if update.effective_user is None or update.effective_chat is None or update.message is None:
        return

    if not _is_authorized(update.effective_user.id):
        logger.warning(
            "Rejected unauthorized /%s attempt user_id=%s", book_mode, update.effective_user.id
        )
        try:
            await update.message.reply_text("⛔ Unauthorized.")
        except TelegramError:
            pass
        return

    prompt = _command_text(update)
    if not prompt:
        await update.message.reply_text(f"Usage: /{book_mode} <prompt>")
        return

    await _enqueue_book_task(update, context, book_mode, prompt)


async def handle_write(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _handle_book_command(update, context, "write")


async def handle_brainstorm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _handle_book_command(update, context, "brainstorm")


async def handle_braindump(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _handle_book_command(update, context, "braindump")


async def handle_keep(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /keep [guidance] — commit chosen parts of the last /brainstorm reply into
    the book files. Nothing from a brainstorm is ever applied automatically;
    this is the deliberate opt-in step. Optional free-text guidance narrows
    what to keep/skip/adjust — e.g. "/keep just the bit about Toln's motive".
    """
    if update.effective_user is None or update.effective_chat is None or update.message is None:
        return

    if not _is_authorized(update.effective_user.id):
        logger.warning("Rejected unauthorized /keep attempt user_id=%s", update.effective_user.id)
        try:
            await update.message.reply_text("⛔ Unauthorized.")
        except TelegramError:
            pass
        return

    chat_id = update.effective_chat.id
    brainstorm_text = book.get_last_brainstorm(chat_id)
    if not brainstorm_text:
        await update.message.reply_text(
            "No recent /brainstorm reply to keep for this chat. Run /brainstorm first, "
            "or use /braindump to add ideas directly."
        )
        return

    guidance = _command_text(update)
    payload = (
        f"Previous /brainstorm response:\n{brainstorm_text}\n\n"
        f"User guidance on what to keep/skip/adjust: "
        f"{guidance or '(none given — use judgment: commit only concrete, decided ideas; '
        'skip speculative alternatives or open questions that were only offered as options)'}"
    )
    await _enqueue_book_task(update, context, "keep", payload)


async def handle_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear this chat's short-term conversation memory (used for routing/local-chat context)."""
    if update.effective_user is None or update.effective_chat is None or update.message is None:
        return

    if not _is_authorized(update.effective_user.id):
        logger.warning("Rejected unauthorized /reset attempt user_id=%s", update.effective_user.id)
        try:
            await update.message.reply_text("⛔ Unauthorized.")
        except TelegramError:
            pass
        return

    cleared = conversation_memory.clear(update.effective_chat.id)
    text = (
        "🧹 Short-term memory cleared for this chat."
        if cleared
        else "Nothing to clear — no memory for this chat yet."
    )
    await update.message.reply_text(text)


async def handle_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Abort the in-flight task and drop any queued ones. /cancel and /stop are aliases."""
    if update.effective_user is None or update.message is None:
        return

    user = update.effective_user
    if not _is_authorized(user.id):
        logger.warning("Rejected unauthorized cancel attempt user_id=%s", user.id)
        try:
            await update.message.reply_text("⛔ Unauthorized.")
        except TelegramError:
            pass
        return

    cancelled = task_queue.cancel_current()
    drained = task_queue.clear_pending()

    if cancelled or drained:
        parts = []
        if cancelled:
            parts.append("in-flight task cancelled")
        if drained:
            parts.append(f"{drained} queued task(s) cleared")
        text = "⏹ " + " and ".join(parts) + "."
    else:
        text = "Nothing running or queued."

    await update.message.reply_text(text)


async def handle_confirmation_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Resolve a pending destructive-action confirmation from its inline keyboard."""
    query = update.callback_query
    if query is None or query.data is None or query.from_user is None:
        return

    if not _is_authorized(query.from_user.id):
        await query.answer("⛔ Unauthorized.", show_alert=True)
        return

    action, _, confirmation_id = query.data.partition(":")
    if action not in ("confirm", "reject") or not confirmation_id:
        await query.answer()
        return

    resolved = task_queue.resolve_confirmation(confirmation_id, approved=(action == "confirm"))
    await query.answer("Got it." if resolved else "This request already expired.")


async def _on_startup(application: Application) -> None:
    logger.info("Sandbox directory: %s", config.SANDBOX_DIR)
    logger.info("Allowed Telegram user IDs: %s", sorted(config.ALLOWED_USER_IDS))
    book.ensure_book_workspace()
    logger.info("Book project directory: %s", book.BOOK_DIR)
    task_queue.start(application.bot)


async def _on_shutdown(application: Application) -> None:
    logger.info("Shutting down TaskQueue worker...")
    await task_queue.stop(timeout=60.0)
    logger.info("Shutdown complete")


def build_application() -> Application:
    """Construct the python-telegram-bot Application with handlers and lifecycle hooks."""
    application = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .post_init(_on_startup)
        .post_shutdown(_on_shutdown)
        .build()
    )

    # /cancel and /stop are aliases that both abort the in-flight task and
    # clear the queue; auth is enforced inside the handler for clear logging.
    application.add_handler(CommandHandler(["cancel", "stop"], handle_cancel))

    # Clears this chat's short-term conversation memory (routing/local-chat context).
    application.add_handler(CommandHandler("reset", handle_reset))

    # Book Writing & Lore Tracker: fast-path captures append directly with no
    # LLM involved; /write and /brainstorm enqueue a scoped Claude CLI task.
    application.add_handler(CommandHandler("note", handle_note))
    application.add_handler(CommandHandler("char", handle_char))
    application.add_handler(CommandHandler("lore", handle_lore))
    application.add_handler(CommandHandler("write", handle_write))
    application.add_handler(CommandHandler("brainstorm", handle_brainstorm))
    application.add_handler(CommandHandler("braindump", handle_braindump))
    application.add_handler(CommandHandler("keep", handle_keep))

    # Inline-keyboard responses to destructive-action confirmation prompts.
    application.add_handler(CallbackQueryHandler(handle_confirmation_callback))

    # Text messages only; auth is enforced inside the handler for clear logging.
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )
    return application


def main() -> None:
    """Run the bot until interrupted (Ctrl+C / SIGTERM)."""
    # Ensure ProactorEventLoop policy on Windows for subprocess support.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    application = build_application()

    # python-telegram-bot installs its own signal handlers via run_polling.
    # We still register a no-op friendly log on SIGINT for clarity in consoles.
    def _log_signal(signum, _frame) -> None:  # noqa: ANN001
        logger.info("Received signal %s — initiating graceful shutdown", signum)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _log_signal)
        except (ValueError, OSError):
            # Signals may be unavailable in some embedded / thread contexts.
            pass

    logger.info("Starting Telegram bot polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
