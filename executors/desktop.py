"""
Desktop and local-text executors.

- desktop → Claude CLI without Chrome, still bound to SANDBOX_DIR
- text    → Local Qwen via Ollama (no Claude CLI / no Chrome)
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Optional

import ollama

import config
from executors.claude_cli import run_claude_cli
from memory import Turn

logger = logging.getLogger(__name__)

StatusCallback = Callable[[str], Awaitable[None]]


async def run_desktop_task(
    task: str,
    status_callback: Optional[StatusCallback] = None,
    model: Optional[str] = None,
    chat_id: int = 0,
    message_id: int = 0,
) -> tuple[str, bool]:
    """
    Execute a local / desktop-oriented task via Claude CLI (no --chrome).

    The subprocess cwd is always config.SANDBOX_DIR. `chat_id`/`message_id`
    are only used to name this task's live log files under config.LOG_DIR.
    """
    if status_callback is not None:
        await status_callback("Running desktop task via Claude CLI (sandboxed)...")
    return await run_claude_cli(
        task=task,
        use_chrome=False,
        status_callback=status_callback,
        model=model,
        chat_id=chat_id,
        message_id=message_id,
    )


async def run_text_task(
    task: str,
    status_callback: Optional[StatusCallback] = None,
    history: Optional[list[Turn]] = None,
    chat_id: int = 0,
    message_id: int = 0,
) -> tuple[str, bool]:
    """
    Answer a pure-text request with the local Ollama Qwen model, streaming
    the answer as it generates.

    No Claude CLI process is spawned, so this path does not contend for
    Chrome Native Messaging. Qwen has no hidden reasoning to surface here —
    the live view is just the answer text arriving token by token.

    `history` is recent turns from this chat (oldest first), replayed as
    real prior user/assistant messages so Qwen has genuine conversational
    context; pass None/[] for a fresh chat. `chat_id`/`message_id` are only
    used to name this task's live log file under config.LOG_DIR.
    """
    if status_callback is not None:
        await status_callback(f"Answering with local model ({config.OLLAMA_MODEL})...")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = config.LOG_DIR / f"{stamp}_chat{chat_id}_msg{message_id}_text.log"
    log = open(log_path, "w", encoding="utf-8", buffering=1)
    log.write(
        f"=== Local Qwen task started {datetime.now().strftime('%H:%M:%S')} ===\n"
        f"task: {task}\n\n"
    )

    try:
        client = ollama.AsyncClient(host=config.OLLAMA_HOST)
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a helpful local assistant. Answer clearly and "
                    "concisely. Do not invent tool use; you have no browser "
                    "or filesystem access in this mode."
                ),
            },
        ]
        for turn in history or []:
            messages.append({"role": "user", "content": turn.user_prompt})
            messages.append({"role": "assistant", "content": turn.response_excerpt})
        messages.append({"role": "user", "content": task})

        content_parts: list[str] = []
        last_callback_at = 0.0
        throttle = config.STATUS_EDIT_THROTTLE_SECONDS

        stream = await client.chat(
            model=config.OLLAMA_MODEL,
            messages=messages,
            options={"temperature": 0.3},
            stream=True,
        )
        async for chunk in stream:
            piece = chunk.get("message", {}).get("content", "")
            if not piece:
                continue
            content_parts.append(piece)
            log.write(piece)
            if status_callback is not None:
                now = time.monotonic()
                if (now - last_callback_at) >= throttle:
                    last_callback_at = now
                    try:
                        await status_callback("".join(content_parts))
                    except Exception:  # noqa: BLE001
                        logger.exception("status_callback failed; continuing Qwen stream")

        content = "".join(content_parts).strip()
        log.write(f"\n\n=== finished {datetime.now().strftime('%H:%M:%S')} ===\n")

        if not content:
            content = "(Local model returned an empty response.)"
            return content, False

        if status_callback is not None:
            await status_callback(content)

        return content, True
    except Exception as exc:  # noqa: BLE001
        logger.exception("Local text task failed")
        error_msg = f"Local Qwen request failed: {exc}"
        log.write(f"\n=== error: {error_msg} ===\n")
        if status_callback is not None:
            try:
                await status_callback(error_msg)
            except Exception:  # noqa: BLE001
                logger.exception("status_callback failed after text error")
        return error_msg, False
    finally:
        log.close()
