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
import os
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

import config
import workspace
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


async def _enqueue_project_task(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    project: str,
    project_mode: str,
    payload: str,
    target_file: Optional[str] = None,
) -> None:
    """Enqueue a claude_project task directly, bypassing Ollama triage, but
    still serialized through the FIFO Claude CLI queue."""
    ack = await update.message.reply_text("⏳ Queuing task...")

    item = TaskItem(
        chat_id=update.effective_chat.id,
        message_id=ack.message_id,
        user_prompt=payload,
        forced_route="claude_project",
        project_mode=project_mode,
        project=project,
        target_file=target_file,
    )
    position = await task_queue.enqueue(item)

    try:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=ack.message_id,
            text=f"🧠 Task queued for project '{project}' (Position #{position})...",
        )
    except TelegramError:
        logger.exception("Failed to update queue acknowledgement")


async def _require_active_project(update: Update) -> Optional[str]:
    """
    Return the active project for this chat, or None after replying with
    setup instructions (existing projects to switch to, or how to start a
    new one) if none is set yet. Caller must already have validated
    update.effective_chat/update.message are non-None.
    """
    project = workspace.get_active_project(update.effective_chat.id)
    if project is None:
        await update.message.reply_text(workspace.no_active_project_message())
    return project


async def handle_project(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /project [name] — set or show the active project for this chat. Every
    other project command (/write, /brainstorm, /braindump, /keep, /create,
    /list, /delete) operates on whichever project is active here. A name
    that doesn't exist yet is created.
    """
    if update.effective_user is None or update.effective_chat is None or update.message is None:
        return

    if not _is_authorized(update.effective_user.id):
        logger.warning("Rejected unauthorized /project attempt user_id=%s", update.effective_user.id)
        try:
            await update.message.reply_text("⛔ Unauthorized.")
        except TelegramError:
            pass
        return

    chat_id = update.effective_chat.id
    name = _command_text(update)

    if not name:
        current = workspace.get_active_project(chat_id)
        projects = workspace.list_projects()
        listing = ", ".join(projects) if projects else "(none yet)"
        if current:
            await update.message.reply_text(f"Active project: {current}\nAll projects: {listing}")
        else:
            await update.message.reply_text(workspace.no_active_project_message())
        return

    project_name = workspace.normalize_project_name(name)
    if project_name is None:
        await update.message.reply_text("Invalid project name.")
        return

    created = workspace.ensure_project(project_name)
    workspace.set_active_project(chat_id, project_name)
    verb = "Created and switched to" if created else "Switched to"
    await update.message.reply_text(f"✅ {verb} project '{project_name}'.")


async def handle_create(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/create <name> — create a new .md file in the active project (e.g. /create ending -> ending.md)."""
    if update.effective_user is None or update.effective_chat is None or update.message is None:
        return

    if not _is_authorized(update.effective_user.id):
        logger.warning("Rejected unauthorized /create attempt user_id=%s", update.effective_user.id)
        try:
            await update.message.reply_text("⛔ Unauthorized.")
        except TelegramError:
            pass
        return

    project = await _require_active_project(update)
    if project is None:
        return

    name = _command_text(update)
    if not name:
        await update.message.reply_text("Usage: /create <name> (e.g. /create ending)")
        return

    filename = workspace.normalize_md_name(name)
    if filename is None:
        await update.message.reply_text("Invalid file name.")
        return

    _, created = workspace.create_md_file(workspace.project_dir(project), filename)
    if created:
        await update.message.reply_text(f"✅ Created {filename} in '{project}'.")
    else:
        await update.message.reply_text(f"ℹ️ {filename} already exists in '{project}'.")


async def handle_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/list — list all .md files in the active project."""
    if update.effective_user is None or update.effective_chat is None or update.message is None:
        return

    if not _is_authorized(update.effective_user.id):
        logger.warning("Rejected unauthorized /list attempt user_id=%s", update.effective_user.id)
        try:
            await update.message.reply_text("⛔ Unauthorized.")
        except TelegramError:
            pass
        return

    project = await _require_active_project(update)
    if project is None:
        return

    files = workspace.list_md_files(workspace.project_dir(project))
    if not files:
        await update.message.reply_text(f"No .md files in '{project}' yet. Use /create <name> to add one.")
        return

    listing = "\n".join(f"- {f}" for f in files)
    await update.message.reply_text(f"Files in '{project}':\n{listing}")


async def handle_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/delete <name> — permanently delete a .md file from the active project. No confirmation, no undo."""
    if update.effective_user is None or update.effective_chat is None or update.message is None:
        return

    if not _is_authorized(update.effective_user.id):
        logger.warning("Rejected unauthorized /delete attempt user_id=%s", update.effective_user.id)
        try:
            await update.message.reply_text("⛔ Unauthorized.")
        except TelegramError:
            pass
        return

    project = await _require_active_project(update)
    if project is None:
        return

    name = _command_text(update)
    if not name:
        await update.message.reply_text("Usage: /delete <name> (e.g. /delete ending)")
        return

    filename = workspace.normalize_md_name(name)
    if filename is None:
        await update.message.reply_text("Invalid file name.")
        return

    deleted = workspace.delete_md_file(workspace.project_dir(project), filename)
    if deleted:
        await update.message.reply_text(f"🗑️ Deleted {filename} from '{project}'.")
    else:
        await update.message.reply_text(f"{filename} doesn't exist in '{project}'.")


async def handle_read(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/read <name> — send a .md file from the active project as a .txt document."""
    if update.effective_user is None or update.effective_chat is None or update.message is None:
        return

    if not _is_authorized(update.effective_user.id):
        logger.warning("Rejected unauthorized /read attempt user_id=%s", update.effective_user.id)
        try:
            await update.message.reply_text("⛔ Unauthorized.")
        except TelegramError:
            pass
        return

    project = await _require_active_project(update)
    if project is None:
        return

    name = _command_text(update)
    if not name:
        await update.message.reply_text("Usage: /read <name> (e.g. /read ending)")
        return

    filename = workspace.normalize_md_name(name)
    if filename is None:
        await update.message.reply_text("Invalid file name.")
        return

    file_path = workspace.project_dir(project) / filename
    if not file_path.is_file():
        await update.message.reply_text(f"{filename} doesn't exist in '{project}'.")
        return

    txt_name = f"{file_path.stem}.txt"
    document = io.BytesIO(file_path.read_bytes())
    document.name = txt_name
    try:
        await update.message.reply_document(document=document, filename=txt_name)
    except TelegramError:
        logger.exception("send_document failed for /read")
        await update.message.reply_text("Could not upload the file. Check server logs.")


async def handle_brainstorm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/brainstorm <prompt> — thinking-partner advice on the active project. Never modifies files."""
    if update.effective_user is None or update.effective_chat is None or update.message is None:
        return

    if not _is_authorized(update.effective_user.id):
        logger.warning("Rejected unauthorized /brainstorm attempt user_id=%s", update.effective_user.id)
        try:
            await update.message.reply_text("⛔ Unauthorized.")
        except TelegramError:
            pass
        return

    project = await _require_active_project(update)
    if project is None:
        return

    prompt = _command_text(update)
    if not prompt:
        await update.message.reply_text("Usage: /brainstorm <prompt>")
        return

    await _enqueue_project_task(update, context, project, "brainstorm", prompt)


async def handle_braindump(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/braindump <text> — synthesize a messy idea dump into the right file(s) in the active project."""
    if update.effective_user is None or update.effective_chat is None or update.message is None:
        return

    if not _is_authorized(update.effective_user.id):
        logger.warning("Rejected unauthorized /braindump attempt user_id=%s", update.effective_user.id)
        try:
            await update.message.reply_text("⛔ Unauthorized.")
        except TelegramError:
            pass
        return

    project = await _require_active_project(update)
    if project is None:
        return

    text = _command_text(update)
    if not text:
        await update.message.reply_text("Usage: /braindump <text>")
        return

    await _enqueue_project_task(update, context, project, "braindump", text)


async def handle_keep(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /keep [guidance] — commit chosen parts of the last /brainstorm reply (for
    the active project) into its files. Nothing from a brainstorm is ever
    applied automatically; this is the deliberate opt-in step. Reference the
    brainstorm's numbers directly, e.g. "/keep 1, 3; amend 2: ...; scrap 4".
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

    project = await _require_active_project(update)
    if project is None:
        return

    chat_id = update.effective_chat.id
    brainstorm_text = workspace.get_last_brainstorm(chat_id, project)
    if not brainstorm_text:
        await update.message.reply_text(
            f"No recent /brainstorm reply to keep for project '{project}'. Run /brainstorm "
            "first, or use /braindump to add ideas directly."
        )
        return

    guidance = _command_text(update)
    payload = (
        f"Previous /brainstorm response:\n{brainstorm_text}\n\n"
        f"User guidance on what to keep/skip/adjust: "
        f"{guidance or '(none given — use judgment: commit only concrete, decided ideas; '
        'skip speculative alternatives or open questions that were only offered as options)'}"
    )
    await _enqueue_project_task(update, context, project, "keep", payload)


async def handle_research(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /research <query> — bridges live web research into the active project.
    Two steps: (1) Claude browses the web (--chrome) to gather findings, (2)
    the findings are filed into the active project's files via the same
    synthesize-and-merge pipeline /braindump uses. Kept as its own explicit
    command — never triggered by natural language — so routine project work
    never risks launching a browser window unexpectedly.
    """
    if update.effective_user is None or update.effective_chat is None or update.message is None:
        return

    if not _is_authorized(update.effective_user.id):
        logger.warning("Rejected unauthorized /research attempt user_id=%s", update.effective_user.id)
        try:
            await update.message.reply_text("⛔ Unauthorized.")
        except TelegramError:
            pass
        return

    project = await _require_active_project(update)
    if project is None:
        return

    query = _command_text(update)
    if not query:
        await update.message.reply_text("Usage: /research <query>")
        return

    ack = await update.message.reply_text("⏳ Queuing research task...")

    item = TaskItem(
        chat_id=update.effective_chat.id,
        message_id=ack.message_id,
        user_prompt=query,
        forced_route="claude_research",
        project=project,
    )
    position = await task_queue.enqueue(item)

    try:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=ack.message_id,
            text=f"🌐 Research task queued for project '{project}' (Position #{position})...",
        )
    except TelegramError:
        logger.exception("Failed to update queue acknowledgement")


async def handle_write(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /write <name> — select (creating if needed) the .md file that /write
    continues, e.g. "/write draft" targets draft.md, then remembers it.
    /write <prompt> — continue/extend whichever file was last selected this
    way, appending Claude's new material to it.

    Disambiguation rule: a single-word argument is always treated as a file
    selection (even if it happens to also read like a one-word prompt);
    anything with more than one word is always treated as a prompt.
    """
    if update.effective_user is None or update.effective_chat is None or update.message is None:
        return

    if not _is_authorized(update.effective_user.id):
        logger.warning("Rejected unauthorized /write attempt user_id=%s", update.effective_user.id)
        try:
            await update.message.reply_text("⛔ Unauthorized.")
        except TelegramError:
            pass
        return

    project = await _require_active_project(update)
    if project is None:
        return

    chat_id = update.effective_chat.id
    arg = _command_text(update)

    if not arg:
        current = workspace.get_active_write_file(chat_id, project)
        files = workspace.list_md_files(workspace.project_dir(project))
        listing = ", ".join(files) if files else "(none yet)"
        current_line = f"Currently writing to: {current}\n" if current else ""
        await update.message.reply_text(
            f"{current_line}Files in '{project}': {listing}\n\n"
            "Usage: /write <name> to select/create a file (e.g. /write draft), "
            "then /write <prompt> to continue writing into it."
        )
        return

    if len(arg.split()) == 1:
        filename = workspace.normalize_md_name(arg)
        if filename is None:
            await update.message.reply_text("Invalid file name.")
            return
        _, created = workspace.create_md_file(workspace.project_dir(project), filename)
        workspace.set_active_write_file(chat_id, project, filename)
        verb = "Created and selected" if created else "Selected"
        await update.message.reply_text(f"✅ {verb} {filename} — /write <prompt> now continues it.")
        return

    target = workspace.get_active_write_file(chat_id, project)
    if target is None:
        files = workspace.list_md_files(workspace.project_dir(project))
        listing = ", ".join(files) if files else "(none yet)"
        await update.message.reply_text(
            f"No file selected to write to.\nFiles in '{project}': {listing}\n\n"
            "Use /write <name> to select one (e.g. /write draft) — or /write <new-name> "
            "to create and select a new one. Then /write <prompt> continues writing into it."
        )
        return

    await _enqueue_project_task(update, context, project, "write", arg, target_file=target)


async def _handle_dev_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE, dev_mode: str
) -> None:
    """
    /fix and /code — run Claude CLI directly against this bot's own source
    tree (config.PROJECT_ROOT), bypassing Ollama triage entirely. Still
    serialized through the same FIFO queue as every other Claude CLI task.
    Editing source here does not hot-reload the running bot — it needs a
    restart afterward to take effect.
    """
    if update.effective_user is None or update.effective_chat is None or update.message is None:
        return

    if not _is_authorized(update.effective_user.id):
        logger.warning(
            "Rejected unauthorized /%s attempt user_id=%s", dev_mode, update.effective_user.id
        )
        try:
            await update.message.reply_text("⛔ Unauthorized.")
        except TelegramError:
            pass
        return

    prompt = _command_text(update)
    if not prompt:
        usage = "bug description" if dev_mode == "fix" else "feature description"
        await update.message.reply_text(f"Usage: /{dev_mode} <{usage}>")
        return

    ack = await update.message.reply_text("⏳ Queuing dev task...")

    item = TaskItem(
        chat_id=update.effective_chat.id,
        message_id=ack.message_id,
        user_prompt=prompt,
        forced_route="claude_dev",
        dev_mode=dev_mode,
    )
    position = await task_queue.enqueue(item)

    try:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=ack.message_id,
            text=f"🛠️ Dev task queued (Position #{position})...",
        )
    except TelegramError:
        logger.exception("Failed to update queue acknowledgement")


async def handle_fix(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _handle_dev_command(update, context, "fix")


async def handle_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _handle_dev_command(update, context, "code")


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


async def handle_restart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Restart the whole bot process. A fresh Python process re-imports every
    module from disk, which is the only way source edits from /fix or /code
    actually take effect — editing files never hot-reloads the running bot.

    Any in-flight or queued task is dropped (same as /cancel) before restart.
    """
    if update.effective_user is None or update.effective_chat is None or update.message is None:
        return

    if not _is_authorized(update.effective_user.id):
        logger.warning("Rejected unauthorized /restart attempt user_id=%s", update.effective_user.id)
        try:
            await update.message.reply_text("⛔ Unauthorized.")
        except TelegramError:
            pass
        return

    cancelled = task_queue.cancel_current()
    drained = task_queue.clear_pending()
    note = ""
    if cancelled or drained:
        dropped = []
        if cancelled:
            dropped.append("in-flight task")
        if drained:
            dropped.append(f"{drained} queued task(s)")
        note = f" ({' and '.join(dropped)} dropped)"

    logger.info("Restart requested via Telegram by user_id=%s", update.effective_user.id)
    await update.message.reply_text(f"🔄 Restarting the bot now{note}...")

    # Give the reply a moment to actually reach Telegram before this process
    # is replaced.
    await asyncio.sleep(1.0)

    # On Windows this spawns a fresh process and exits this one immediately
    # (verified: no lingering/zombie process) — the new process re-imports
    # every module from disk, so code changes take effect.
    os.execv(sys.executable, [sys.executable] + sys.argv)


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
    workspace.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    logger.info("Projects directory: %s", workspace.PROJECTS_DIR)
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

    # Project Thinking Partner: /project selects the active project that all
    # of these operate on; /create, /list, /delete manage its .md files
    # directly (no LLM); /write, /brainstorm, /braindump, /keep each enqueue
    # a scoped Claude CLI task.
    application.add_handler(CommandHandler("project", handle_project))
    application.add_handler(CommandHandler("create", handle_create))
    application.add_handler(CommandHandler("list", handle_list))
    application.add_handler(CommandHandler("delete", handle_delete))
    application.add_handler(CommandHandler("read", handle_read))
    application.add_handler(CommandHandler("write", handle_write))
    application.add_handler(CommandHandler("brainstorm", handle_brainstorm))
    application.add_handler(CommandHandler("braindump", handle_braindump))
    application.add_handler(CommandHandler("keep", handle_keep))

    # Web-research bridge: browses with --chrome, then files findings into
    # the active project. Explicit-only, never triggered by natural language.
    application.add_handler(CommandHandler("research", handle_research))

    # Self-maintenance: edits this bot's own source tree directly (no Ollama
    # triage, no Chrome). Requires a bot restart afterward to take effect.
    application.add_handler(CommandHandler("fix", handle_fix))
    application.add_handler(CommandHandler("code", handle_code))

    # Restarts the whole bot process — needed for /fix and /code changes to
    # take effect, and usable remotely without shell access to the machine.
    application.add_handler(CommandHandler("restart", handle_restart))

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
