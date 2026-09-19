# Telegram Commands

Everything below is sent as a normal Telegram message to the bot. Only user
IDs listed in `TELEGRAM_ALLOWED_USER_IDS` (see `.env`) can use any of this.

## Plain messages (no command)

Just type normally, with no leading `/`. The message is queued, triaged
locally by Ollama/Qwen, and routed automatically to one of:

| Route | What it does |
|---|---|
| `local` | Answered directly by the local Qwen model — general chat, offline Q&A, no tools. |
| `claude_web` | Claude CLI with `--chrome` — live browsing, scraping, form-filling, real-time lookups. |
| `claude_book` | Claude CLI scoped to `book_project/` (no browser) — manuscript writing, story brainstorming, lore analysis. |
| `claude_general` | Claude CLI scoped to the sandbox (no browser) — other file/terminal tasks. Destructive-looking actions (deletes, overwrites, resets, etc.) trigger a confirm/cancel prompt before running. |

You don't pick the route yourself — just describe what you want and the
router figures it out. If it ever misroutes something book-related, use
`/write` or `/brainstorm` directly to force it.

## Book Writing & Lore Tracker

Four files live in `SANDBOX_DIR/book_project/` and are created automatically
on first run: `draft.md`, `characters.md`, `worldbuilding.md`, `notes.md`.

### Fast capture (instant, no AI involved)

These just timestamp and append your text straight to the file — no queue,
no Claude, no Qwen. Confirmation is immediate.

| Command | Appends to | Example |
|---|---|---|
| `/note <text>` | `notes.md` | `/note idea: the villain should have a redemption arc in book 2` |
| `/char <text>` | `characters.md` | `/char Mira — younger sister, secretly a defector, arc: trust → betrayal → reconciliation` |
| `/lore <text>` | `worldbuilding.md` | `/lore the Northern Wall has stood for 900 years and has never been breached` |

### Claude-assisted (queued, runs via Claude CLI in `book_project/`)

These bypass the Qwen router entirely and go straight to Claude, scoped to
`book_project/` with no browser access.

| Command | What it does |
|---|---|
| `/write <prompt>` | Claude reads `characters.md`, `worldbuilding.md`, and `draft.md` for context, drafts/continues the scene per your prompt, **appends the new material to `draft.md`**, and replies with the excerpt it wrote. |
| `/brainstorm <prompt>` | Claude reads all four book files for context and answers your creative question or helps untangle a plot problem, as a **numbered list** (1. 2. 3. ...) — one distinct idea per number, so you can reference them later. **Does not modify any files** — advice only. |
| `/braindump <text>` | For messy, unstructured idea dumps. Claude reads `characters.md`, `worldbuilding.md`, and `notes.md`, figures out which file(s) each idea actually belongs in, and **merges/rewrites it into the right place** — not a raw append. It updates an existing character/topic instead of duplicating it where one already exists, tightens the prose so the file stays readable, and never touches `draft.md`. Replies with a short summary of what it changed and where. |
| `/keep [guidance]` | Commits chosen parts of your **most recent `/brainstorm` reply** into the book files — nothing from a brainstorm is ever applied automatically; this is the deliberate opt-in step. Reference the brainstorm's numbers directly: `/keep 1, 3, 5; amend 2: <replacement text>; scrap 4`. With no guidance, Claude uses judgment — committing concrete, decided ideas and leaving out anything that read as one option among several you hadn't chosen. Same synthesize-and-merge behavior as `/braindump` (no verbatim pasting, no `draft.md` edits). Replies with what it kept and what it deliberately left out. If you haven't run `/brainstorm` yet in this chat (or the bot restarted since), it tells you so instead of guessing. |

Examples:
```
/write Continue the scene where Mira confronts her sister at the harbor. End on a cliffhanger.
/brainstorm I've written Mira as sympathetic but she betrays the crew in ch.9 — how do I foreshadow this without telegraphing it?
/keep keep 1 and 3 as-is, amend 2 to have her hide the letters instead of burning them, scrap 4
/braindump ok so Mira's mom might actually still be alive and hiding in the northern territories, also I think the harbor city should have some kind of curfew law tied to the old war, and Mira's crewmate Toln should have a grudge against the captain from something in ch.3
```

**`/braindump` vs. `/note`/`/char`/`/lore`:** the fast-capture commands append your
text verbatim, instantly, with no AI involved — good for a quick line before you
forget it. `/braindump` is slower (it queues and runs through Claude) but actually
reads what's already written and integrates the idea properly instead of just
tacking raw text onto the end of a file.

**`/brainstorm` + `/keep`:** `/brainstorm` is pure advice — it never touches your
files, so an idea you don't like or want changed just stays in the chat and is
never applied. If something from it is worth keeping, `/keep` is the separate,
explicit step that commits it (with the same read-and-merge behavior as
`/braindump`). Only the most recent `/brainstorm` reply per chat is remembered,
and only in memory — it's gone after a bot restart.

## Queue & memory control

| Command | What it does |
|---|---|
| `/cancel` (alias: `/stop`) | Cancels the task currently running and clears everything else waiting in the queue. |
| `/reset` | Clears this chat's short-term conversation memory — the rolling window of recent turns used to give the local Qwen router and `local` chat replies conversational context (e.g. so "do that again" resolves correctly). Use it to start a clean context without restarting the bot. Doesn't touch the book files, the last `/brainstorm` reply remembered for `/keep`, or the task queue. |

## Notes on behavior

- All tasks (except `/note`, `/char`, `/lore`) run one at a time in a strict
  FIFO queue — Chrome automation and Claude CLI can't run concurrently. You'll
  get a "queued at position #N" reply and then live status updates as it runs.
- If a `claude_general` task looks destructive (delete/overwrite/reset/etc.),
  you'll get an inline **✅ Proceed / ❌ Cancel** prompt before it runs — it
  times out and auto-cancels after `DESTRUCTIVE_CONFIRMATION_TIMEOUT_SECONDS`
  (default 300s) if you don't respond.
- Long outputs (over ~3,500 characters) are sent as a short excerpt plus a
  `task_result.txt` attachment with the full text.
