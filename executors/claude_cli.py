"""
Claude CLI executor — strictly sandboxed to config.SANDBOX_DIR.

Uses a local Claude Pro OAuth session (not Anthropic API billing). Chrome
Native Messaging cannot handle concurrent Claude CLI processes; callers must
serialize invocations through the FIFO TaskQueue worker.

Critical: ANTHROPIC_API_KEY must never reach the subprocess. If present in the
host environment it overrides OAuth, switches to developer billing, and breaks
the --chrome flag.

Live visibility: the CLI is run with --output-format stream-json so we get
real-time tool calls and streamed answer text. Note that actual chain-of-
thought content comes back redacted (empty string, token-count estimate
only) even at high effort — that's a platform-level restriction, not a bug
here. Telegram gets a small throttled excerpt (current tool + streamed
answer text); the full unfiltered event stream is written to a pair of local
log files (config.LOG_DIR) in real time for full transparency on the laptop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, TextIO

import config

logger = logging.getLogger(__name__)

StatusCallback = Callable[[str], Awaitable[None]]

# Env vars that force API / developer billing and must never reach the CLI child.
_API_KEY_ENV_VARS = ("ANTHROPIC_API_KEY",)

# asyncio's StreamReader defaults to a 64 KiB per-line buffer, which a single
# stream-json event (e.g. a long manuscript excerpt) can easily exceed. 10 MiB
# comfortably covers any realistic single event.
_STREAM_LINE_LIMIT = 10 * 1024 * 1024

# Maps router.RouteDecision.model literals to the Claude CLI's --model aliases.
# These aliases always resolve to the CLI's current model for that tier, so
# this mapping never needs to change when a new Claude generation ships.
_MODEL_ALIASES = {
    "claude-opus": "opus",
    "claude-sonnet": "sonnet",
    "claude-haiku": "haiku",
}

# Tool input keys worth surfacing as a one-line "what it's doing" summary.
_TOOL_SUMMARY_KEYS = {
    "Bash": "command",
    "PowerShell": "command",
    "Edit": "file_path",
    "Write": "file_path",
    "Read": "file_path",
    "NotebookEdit": "notebook_path",
    "Glob": "pattern",
    "Grep": "pattern",
    "WebFetch": "url",
}


def _build_command(use_chrome: bool, model: Optional[str], prompt_path: Path) -> str:
    """
    Build the Claude CLI invocation.

    The prompt is never placed on the command line — it's piped in via stdin
    redirection from `prompt_path` instead. A multi-line prompt embedded
    directly in a shell command string gets mangled by cmd.exe on Windows
    (it truncates/splits on embedded newlines), which silently drops
    everything after the first line — including every flag that follows it
    (--output-format stream-json etc.), causing Claude to fall back to plain
    text output and to receive only a partial prompt. Stdin redirection
    sidesteps this on every platform.

    Chrome path (strict pattern):
        claude -p --chrome --model <model> --output-format stream-json
            --include-partial-messages --verbose --dangerously-skip-permissions
            < "<prompt_path>"

    Desktop/book path (no Chrome Native Messaging): same, minus --chrome.

    --verbose is required by the CLI whenever --output-format=stream-json is
    combined with --print.
    """
    parts = ["claude", "-p"]
    if use_chrome:
        parts.append("--chrome")
    if model:
        cli_model = _MODEL_ALIASES.get(model, model)
        parts.extend(["--model", cli_model])
    parts.extend(
        [
            "--output-format",
            "stream-json",
            "--include-partial-messages",
            "--verbose",
        ]
    )
    parts.append("--dangerously-skip-permissions")
    parts.append(f'< "{prompt_path}"')
    return " ".join(parts)


def _oauth_safe_env() -> dict[str, str]:
    """
    Copy the host environment but strip Anthropic API-key vars.

    Claude CLI prefers ANTHROPIC_API_KEY over the local Pro OAuth session.
    Leaving the key in place silently switches to developer billing and
    disables --chrome.
    """
    env = os.environ.copy()
    stripped = [name for name in _API_KEY_ENV_VARS if name in env]
    for name in stripped:
        del env[name]
    if stripped:
        logger.info(
            "Stripped %s from Claude CLI subprocess env (Pro OAuth mode)",
            ", ".join(stripped),
        )
    return env


def _open_task_logs(chat_id: int, message_id: int) -> tuple[Path, TextIO, TextIO]:
    """
    Open a pair of per-task log files under config.LOG_DIR.

    Both are line-buffered so every write hits disk immediately, giving a
    genuinely live file to tail on the laptop.

    Returns (base_path, human_log_handle, raw_jsonl_handle).
    """
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = config.LOG_DIR / f"{stamp}_chat{chat_id}_msg{message_id}"
    human = open(base.with_suffix(".log"), "w", encoding="utf-8", buffering=1)
    raw = open(base.with_suffix(".jsonl"), "w", encoding="utf-8", buffering=1)
    return base, human, raw


def _tool_summary(name: str, raw_input: dict[str, Any]) -> str:
    """One-line human summary of a tool call's input, for status/log display."""
    key = _TOOL_SUMMARY_KEYS.get(name)
    if key and key in raw_input:
        value = str(raw_input[key])
        return value if len(value) <= 200 else value[:200] + "…"
    compact = json.dumps(raw_input, separators=(",", ":"))
    return compact if len(compact) <= 200 else compact[:200] + "…"


class _StreamState:
    """Tracks in-flight content blocks while parsing a stream-json event feed."""

    def __init__(self) -> None:
        self.block_type: dict[int, str] = {}
        self.tool_name: dict[int, str] = {}
        self.tool_input_raw: dict[int, list[str]] = {}
        self.assistant_text: str = ""
        self.tool_events: list[str] = []
        self.thinking_active: bool = False
        self.final_result: Optional[str] = None
        self.final_success: Optional[bool] = None

    def render_status(self) -> str:
        """Curated, Telegram-sized snapshot of current progress."""
        parts: list[str] = []
        if self.thinking_active:
            parts.append("🤔 Thinking...")
        if self.tool_events:
            parts.append("\n".join(self.tool_events[-5:]))
        if self.assistant_text.strip():
            parts.append(self.assistant_text.strip())
        return "\n\n".join(parts) if parts else "⏳ Working..."


def _handle_stream_event(
    event: dict[str, Any], state: _StreamState, human_log: TextIO
) -> None:
    """Update `state` and narrate to `human_log` for one inner stream_event."""
    etype = event.get("type")

    if etype == "content_block_start":
        index = event.get("index")
        block = event.get("content_block", {})
        btype = block.get("type")
        state.block_type[index] = btype
        if btype == "tool_use":
            name = block.get("name", "?")
            state.tool_name[index] = name
            state.tool_input_raw[index] = []
            human_log.write(f"[{_now()}] 🔧 TOOL START: {name}\n")
        elif btype == "thinking":
            state.thinking_active = True
            human_log.write(f"[{_now()}] 🤔 Thinking...\n")

    elif etype == "content_block_delta":
        index = event.get("index")
        delta = event.get("delta", {})
        dtype = delta.get("type")
        if dtype == "text_delta":
            text = delta.get("text", "")
            state.assistant_text += text
            human_log.write(text)
        elif dtype == "input_json_delta":
            state.tool_input_raw.setdefault(index, []).append(delta.get("partial_json", ""))
        # thinking_delta content is redacted (empty) by the platform; nothing to show.

    elif etype == "content_block_stop":
        index = event.get("index")
        btype = state.block_type.pop(index, None)
        if btype == "tool_use":
            name = state.tool_name.pop(index, "?")
            raw_json = "".join(state.tool_input_raw.pop(index, []))
            try:
                parsed = json.loads(raw_json) if raw_json else {}
            except ValueError:
                parsed = {}
            summary = _tool_summary(name, parsed)
            line = f"🔧 {name}: {summary}"
            state.tool_events.append(line)
            human_log.write(f"[{_now()}] {line}\n    full input: {raw_json}\n")
        elif btype == "thinking":
            state.thinking_active = False
            human_log.write(f"\n[{_now()}] 🤔 Thinking complete (content not exposed by the API).\n")
        elif btype == "text":
            human_log.write("\n")
        state.tool_input_raw.pop(index, None)


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


async def run_claude_cli(
    task: str,
    use_chrome: bool = True,
    status_callback: Optional[StatusCallback] = None,
    model: Optional[str] = None,
    chat_id: int = 0,
    message_id: int = 0,
    cwd: Optional[Path] = None,
    allowed_root: Optional[Path] = None,
) -> tuple[str, bool]:
    """
    Run `claude` inside `allowed_root` (default config.SANDBOX_DIR) or a
    subdirectory of it, streaming its live tool calls and answer text to
    `status_callback` (Telegram) and its complete raw event feed to a pair of
    local log files under config.LOG_DIR (human-readable .log + raw .jsonl),
    named from `chat_id`/`message_id`.

    The subprocess never inherits ANTHROPIC_API_KEY from the host environment.

    `model` is a router.RouteDecision.model literal (e.g. "claude-sonnet")
    or a raw Claude CLI --model value; pass None to use the CLI's default.

    `cwd` optionally scopes the subprocess to a subdirectory of `allowed_root`
    (e.g. a projects/<name>/ workspace); it must resolve to `allowed_root`
    itself or a path inside it. Pass None to use `allowed_root` directly.

    `allowed_root` overrides the sandbox-only guard below. Only pass
    config.PROJECT_ROOT here for the /fix and /code dev tasks
    (executors/dev.py) — every other caller must leave this as None so
    Claude CLI stays confined to config.SANDBOX_DIR.

    Returns:
        (full_output_string, success_boolean)
    """
    root = (allowed_root or config.SANDBOX_DIR).resolve()
    root.mkdir(parents=True, exist_ok=True)

    # Hard guard: never execute outside `root`.
    target_dir = (cwd or root).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    if target_dir != root and root not in target_dir.parents:
        raise ValueError(f"Refusing to run Claude CLI outside {root}: {target_dir}")

    cwd_str = str(target_dir)
    env = _oauth_safe_env()

    log_path, human_log, raw_log = _open_task_logs(chat_id, message_id)

    # The prompt is piped in via stdin from a temp file rather than placed on
    # the command line — see _build_command for why.
    prompt_path = config.LOG_DIR / f"{log_path.name}.prompt.txt"
    prompt_path.write_text(task, encoding="utf-8")
    command = _build_command(use_chrome=use_chrome, model=model, prompt_path=prompt_path)

    human_log.write(
        f"=== Claude CLI task started {_now()} ===\n"
        f"chrome={use_chrome} model={model or 'default'}\n"
        f"task: {task}\n\n"
    )

    logger.info(
        "Starting Claude CLI (chrome=%s oauth=pro model=%s) cwd=%s task_len=%d log=%s",
        use_chrome,
        model or "default",
        cwd_str,
        len(task),
        log_path,
    )

    # On Windows, close_fds=True cannot be combined with redirected stdio pipes.
    #
    # `limit` raises the per-line buffer past asyncio's 64 KiB default. Each
    # stream-json event (a full content_block_delta, or the final `result`
    # event carrying the whole answer) arrives as one JSON line with no
    # embedded newlines, and a long scene or manuscript excerpt easily blows
    # past 64 KiB — which otherwise crashes readline() with LimitOverrunError
    # and aborts the task.
    process = await asyncio.create_subprocess_shell(
        command,
        cwd=cwd_str,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        close_fds=(os.name != "nt"),
        limit=_STREAM_LINE_LIMIT,
    )

    state = _StreamState()
    last_callback_at = 0.0
    throttle = config.STATUS_EDIT_THROTTLE_SECONDS

    assert process.stdout is not None

    try:
        while True:
            line_bytes = await process.stdout.readline()
            if not line_bytes:
                break

            line = line_bytes.decode("utf-8", errors="replace").rstrip("\n")
            if not line:
                continue

            raw_log.write(line + "\n")

            try:
                obj = json.loads(line)
            except ValueError:
                # Stray non-JSON output (rare); still fully preserved in the .jsonl.
                human_log.write(f"[{_now()}] (unparsed) {line}\n")
                continue

            obj_type = obj.get("type")
            if obj_type == "stream_event":
                _handle_stream_event(obj.get("event", {}), state, human_log)
            elif obj_type == "result":
                state.final_result = obj.get("result", "")
                state.final_success = (not obj.get("is_error", False)) and (
                    obj.get("subtype") == "success"
                )
                human_log.write(
                    f"\n[{_now()}] === result: subtype={obj.get('subtype')} "
                    f"cost_usd={obj.get('total_cost_usd')} "
                    f"duration_ms={obj.get('duration_ms')} ===\n"
                )
            # system/rate_limit_event/assistant/user/message_* events are kept
            # in the raw .jsonl only — too noisy to narrate line-by-line.

            if status_callback is not None:
                now = time.monotonic()
                if (now - last_callback_at) >= throttle:
                    last_callback_at = now
                    try:
                        await status_callback(state.render_status())
                    except Exception:  # noqa: BLE001 — never kill the CLI on Telegram errors
                        logger.exception("status_callback failed; continuing CLI read")

        returncode = await process.wait()

        if state.final_result is not None:
            full_output = state.final_result.strip() or state.assistant_text.strip()
            success = bool(state.final_success) and returncode == 0
        else:
            full_output = state.assistant_text.strip()
            success = returncode == 0

        if not full_output:
            full_output = f"(Claude CLI exited with code {returncode} and produced no output.)"

        human_log.write(f"\n=== finished {_now()} returncode={returncode} success={success} ===\n")
    finally:
        human_log.close()
        raw_log.close()
        try:
            prompt_path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Failed to remove temp prompt file %s", prompt_path)

    # Final status push so Telegram reflects the complete result.
    if status_callback is not None:
        try:
            await status_callback(full_output)
        except Exception:  # noqa: BLE001
            logger.exception("final status_callback failed")

    logger.info(
        "Claude CLI finished returncode=%s success=%s output_len=%d log=%s",
        returncode,
        success,
        len(full_output),
        log_path,
    )
    return full_output, success
