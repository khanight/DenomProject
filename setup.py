#!/usr/bin/env python3
"""
Setup wizard for the Telegram Agent Bot.

Run this after Python and the Claude Code CLI are installed and you're
logged in (setup.bat / setup.ps1 handle that on Windows). This script uses
only the Python standard library so it can run before any project
dependencies are installed — it installs them itself as one of its steps.

Safe to re-run any time: it never overwrites a setting you've already
configured without asking, and Ctrl+C at any point changes nothing.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
ENV_PATH = PROJECT_ROOT / ".env"
ENV_EXAMPLE_PATH = PROJECT_ROOT / ".env.example"
REQUIREMENTS_PATH = PROJECT_ROOT / "requirements.txt"

MIN_PYTHON = (3, 10)
OLLAMA_DEFAULT_MODEL = "qwen2.5:7b-instruct-q4_K_M"
OLLAMA_DEFAULT_HOST = "http://localhost:11434"

TOTAL_STEPS = 5


def _banner(text: str) -> None:
    print()
    print("=" * 64)
    print(f"  {text}")
    print("=" * 64)


def _step(n: int, text: str) -> None:
    print(f"\n[{n}/{TOTAL_STEPS}] {text}")


def _ok(text: str) -> None:
    print(f"   ok  {text}")


def _fail(text: str) -> None:
    print(f"   !!  {text}")


def check_python() -> None:
    if sys.version_info < MIN_PYTHON:
        _fail(
            f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ is required — "
            f"found {sys.version.split()[0]}."
        )
        print("        Get a current version from https://python.org/downloads, then re-run this.")
        sys.exit(1)
    _ok(f"Python {sys.version.split()[0]}")


def check_claude_cli() -> None:
    claude_path = shutil.which("claude")
    if claude_path is None:
        _fail("The 'claude' command wasn't found.")
        print()
        print("   Install Claude Code first, then run this setup again:")
        if sys.platform == "win32":
            print("     PowerShell:  irm https://claude.ai/install.ps1 | iex")
        else:
            print("     Terminal:    curl -fsSL https://claude.ai/install.sh | bash")
        print("   Full instructions: https://code.claude.com/docs/en/quickstart")
        sys.exit(1)
    _ok(f"Claude Code CLI found ({claude_path})")

    result = subprocess.run(["claude", "auth", "status"], capture_output=True, text=True)
    if result.returncode != 0:
        print()
        print("   You're not logged into Claude yet. This opens your browser so you")
        print("   can sign in with your existing Claude Pro/Max/Team account — no")
        print("   API key needed.")
        input("   Press Enter to log in...")
        login_result = subprocess.run(["claude", "auth", "login"])
        if login_result.returncode != 0:
            _fail("Login didn't complete. Run 'claude auth login' yourself, then re-run this script.")
            sys.exit(1)
        result = subprocess.run(["claude", "auth", "status"], capture_output=True, text=True)

    try:
        status = json.loads(result.stdout)
        detail = ""
        if status.get("email"):
            detail = f" as {status['email']}"
        if status.get("subscriptionType"):
            detail += f" ({status['subscriptionType']})"
        _ok(f"Logged into Claude{detail}")
    except (json.JSONDecodeError, AttributeError):
        _ok("Logged into Claude")


def install_dependencies() -> None:
    print("   Installing Python packages (python-telegram-bot, pydantic, etc.)...")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-q", "-r", str(REQUIREMENTS_PATH)]
    )
    if result.returncode != 0:
        _fail("pip install failed — see the error above and try again.")
        sys.exit(1)
    _ok("Dependencies installed")


def _telegram_api(token: str, method: str, params: dict | None = None) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8"))
        except (json.JSONDecodeError, ValueError):
            return {"ok": False, "description": f"HTTP {exc.code}"}
    except urllib.error.URLError as exc:
        return {"ok": False, "description": f"couldn't reach Telegram ({exc.reason})"}


def get_bot_token() -> str:
    print()
    print("   Now let's connect your Telegram bot. If you haven't made one yet:")
    print("     1. Open Telegram, search for @BotFather (look for the blue checkmark)")
    print("     2. Send it:  /newbot")
    print("     3. Give it a display name, then a username ending in 'bot'")
    print("     4. BotFather replies with a token that looks like:")
    print("        123456789:AAExampleTokenTextGoesHere")
    print()
    while True:
        token = input("   Paste your bot token here: ").strip()
        if not token:
            continue
        print("   Checking...")
        result = _telegram_api(token, "getMe")
        if result.get("ok"):
            bot_info = result["result"]
            _ok(f"Found your bot: {bot_info.get('first_name')} (@{bot_info.get('username')})")
            return token
        _fail(f"That didn't work: {result.get('description', 'invalid token')}. Try again.")


def get_user_id(token: str) -> int:
    print()
    print("   Last step: send ANY message to your new bot on Telegram right now")
    print("   (just say hi) so it can find your account automatically.")
    input("   Press Enter once you've sent it...")

    for attempt in range(3):
        result = _telegram_api(token, "getUpdates", {"limit": 5, "offset": -5})
        if result.get("ok") and result.get("result"):
            for update in reversed(result["result"]):
                msg = update.get("message")
                if msg and msg.get("from"):
                    user = msg["from"]
                    _ok(f"Found you: {user.get('first_name', '')} (id {user['id']})")
                    return user["id"]
        if attempt < 2:
            input("   Don't see a message yet — send it now, then press Enter to check again...")

    print("   Still nothing. You can find your numeric ID via @userinfobot instead.")
    while True:
        manual = input("   Enter your numeric Telegram ID: ").strip()
        if manual.isdigit():
            return int(manual)
        print("   That doesn't look like a number — try again.")


def setup_ollama() -> tuple[str | None, str | None]:
    print()
    answer = (
        input(
            "   Want local routing with Ollama? Optional — it can lower Claude usage\n"
            "   for casual chat, but needs Ollama installed and a ~5GB model\n"
            "   downloaded. Every /command works fine either way. [y/N]: "
        )
        .strip()
        .lower()
    )
    if answer != "y":
        print("   Skipping — you can run this script again later to add it.")
        return None, None

    if shutil.which("ollama") is None:
        print()
        print("   Ollama isn't installed. Get it from https://ollama.com/download,")
        print("   then re-run this script to finish enabling it.")
        return None, None

    print(f"   Downloading {OLLAMA_DEFAULT_MODEL} (can take a while the first time)...")
    subprocess.run(["ollama", "pull", OLLAMA_DEFAULT_MODEL])
    _ok("Ollama model ready")
    return OLLAMA_DEFAULT_MODEL, OLLAMA_DEFAULT_HOST


def write_env(token: str, user_id: int, ollama_model: str | None, ollama_host: str | None) -> None:
    lines: list[str] = []
    if ENV_EXAMPLE_PATH.exists():
        lines = ENV_EXAMPLE_PATH.read_text(encoding="utf-8").splitlines()

    def set_var(name: str, value: str) -> None:
        prefix = f"{name}="
        for i, line in enumerate(lines):
            if line.startswith(prefix):
                lines[i] = f"{name}={value}"
                return
        lines.append(f"{name}={value}")

    set_var("TELEGRAM_BOT_TOKEN", token)
    set_var("TELEGRAM_ALLOWED_USER_IDS", str(user_id))
    if ollama_model:
        set_var("OLLAMA_MODEL", ollama_model)
        set_var("OLLAMA_HOST", ollama_host or OLLAMA_DEFAULT_HOST)

    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _ok(f"Wrote {ENV_PATH.name}")


def main() -> None:
    _banner("Telegram Agent Bot -- Setup")
    print("This checks your setup, connects your Telegram bot, and gets you")
    print("ready to go. Ctrl+C at any point stops here and changes nothing.")

    if ENV_PATH.exists():
        print()
        answer = input(".env already exists. Overwrite it and reconfigure? [y/N]: ").strip().lower()
        if answer != "y":
            print("Leaving your existing setup alone. Nothing changed.")
            return

    _step(1, "Checking Python")
    check_python()

    _step(2, "Checking Claude Code")
    check_claude_cli()

    _step(3, "Installing dependencies")
    install_dependencies()

    _step(4, "Connecting your Telegram bot")
    token = get_bot_token()
    user_id = get_user_id(token)

    _step(5, "Optional: local routing")
    ollama_model, ollama_host = setup_ollama()

    write_env(token, user_id, ollama_model, ollama_host)

    _banner("All set!")
    start_cmd = "py main.py" if sys.platform == "win32" else f"{sys.executable} main.py"
    print(f"Start your bot any time with:  {start_cmd}")
    print()
    answer = input("Start it right now? [Y/n]: ").strip().lower()
    if answer != "n":
        os.chdir(PROJECT_ROOT)
        os.execv(sys.executable, [sys.executable, str(PROJECT_ROOT / "main.py")])


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nSetup cancelled -- nothing was changed. Run this again any time.")
        sys.exit(1)
