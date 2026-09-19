"""
Application configuration loaded from environment / .env.

All Claude CLI work is confined to SANDBOX_DIR. That directory is created
automatically on import if it does not already exist.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the project root (same directory as this file).
_PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(_PROJECT_ROOT / ".env")


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            f"Copy .env.example to .env and fill in the values."
        )
    return value


def _parse_allowed_user_ids(raw: str) -> frozenset[int]:
    """Parse TELEGRAM_ALLOWED_USER_IDS into a frozenset of ints."""
    ids: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError as exc:
            raise RuntimeError(
                f"Invalid user id in TELEGRAM_ALLOWED_USER_IDS: {part!r}"
            ) from exc
    if not ids:
        raise RuntimeError(
            "TELEGRAM_ALLOWED_USER_IDS must contain at least one integer user id."
        )
    return frozenset(ids)


def _resolve_sandbox_dir(raw: str | None) -> Path:
    """Resolve SANDBOX_DIR to an absolute path and ensure it exists."""
    if not raw or not raw.strip():
        path = (_PROJECT_ROOT / "sandbox").resolve()
    else:
        candidate = Path(raw.strip())
        path = candidate.resolve() if candidate.is_absolute() else (_PROJECT_ROOT / candidate).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


# --- Public settings -----------------------------------------------------------------

TELEGRAM_BOT_TOKEN: str = _require("TELEGRAM_BOT_TOKEN")
ALLOWED_USER_IDS: frozenset[int] = _parse_allowed_user_ids(
    _require("TELEGRAM_ALLOWED_USER_IDS")
)

OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "qwen2.5:7b-instruct-q4_K_M").strip()
OLLAMA_HOST: str = os.getenv("OLLAMA_HOST", "http://localhost:11434").strip()

# Short-term conversation memory: in-process only, lost on restart by design.
MEMORY_MAX_TURNS: int = int(os.getenv("MEMORY_MAX_TURNS", "8"))
MEMORY_RESPONSE_TRUNCATE_CHARS: int = int(os.getenv("MEMORY_RESPONSE_TRUNCATE_CHARS", "500"))

# How long to wait for a Telegram confirmation on a flagged destructive desktop
# task before treating it as declined.
DESTRUCTIVE_CONFIRMATION_TIMEOUT_SECONDS: int = int(
    os.getenv("DESTRUCTIVE_CONFIRMATION_TIMEOUT_SECONDS", "300")
)

SANDBOX_DIR: Path = _resolve_sandbox_dir(os.getenv("SANDBOX_DIR"))

# Full, unfiltered per-task live logs (human-readable .log + raw .jsonl for
# Claude CLI tasks). Written locally only; never sent to Telegram.
LOG_DIR: Path = (_PROJECT_ROOT / "logs").resolve()
LOG_DIR.mkdir(parents=True, exist_ok=True)

# Telegram hard limit is 4096; we stay under that for live edits and finals.
TELEGRAM_MAX_MESSAGE_LENGTH: int = 4096
TELEGRAM_FINAL_INLINE_LIMIT: int = 3500
STATUS_EDIT_THROTTLE_SECONDS: float = 1.5
