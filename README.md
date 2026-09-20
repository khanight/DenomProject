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
| `claude_project` | Claude CLI scoped to whichever project is currently active (see below), no browser — continuing/extending notes, brainstorming, organizing. |
| `claude_general` | Claude CLI scoped to the sandbox (no browser) — other file/terminal tasks. Destructive-looking actions (deletes, overwrites, resets, etc.) trigger a confirm/cancel prompt before running. |

There are two more routes — `claude_dev` (used by `/fix` and `/code`) and
`claude_research` (used by `/research`) — but neither is **ever** reachable
from a plain message. Qwen's routing schema excludes both entirely, so
natural language can't accidentally trigger edits to the bot's own source or
launch a browser from what looked like a routine project message. They only
run via those explicit commands.

You don't pick the route yourself — just describe what you want and the
router figures it out. If a message routes to `claude_project` with no active
project set, you'll get the same "pick a project" prompt as any other project
command. If it ever misroutes, use `/write` or `/brainstorm` directly to force it.

## Project Thinking Partner

A generic, project-agnostic notes/writing/brainstorming workspace — not tied
to any particular subject. A "project" is just a folder of `.md` files under
`SANDBOX_DIR/projects/<name>/`; there's no fixed file schema. Use it for a
novel, a research project, meeting notes, a D&D campaign, whatever — the
commands discover whatever files already exist and let Claude decide where
new content belongs, creating files as it goes.

### Choosing a project

| Command | What it does |
|---|---|
| `/project <name>` | Switch to project `<name>` for this chat, creating it if it doesn't exist yet. Every command below operates on whichever project is currently active. |
| `/project` | Show the active project (if any) and list all existing projects. |

The active project is remembered per chat, in memory — it's forgotten if the
bot restarts. Any project-scoped command run with none set replies with
existing project names to switch to, or instructions to start a new one.

### File management (instant, no AI involved)

| Command | What it does |
|---|---|
| `/create <name>` | Create `<name>.md` in the active project (`.md` is added automatically — `/create ending` makes `ending.md`). No-ops with a note if it already exists. |
| `/list` | List all `.md` files in the active project. |
| `/delete <name>` | Permanently delete `<name>.md` from the active project. No confirmation, no undo. |
| `/read <name>` | Send `<name>.md` from the active project as a `.txt` file. |

### Claude-assisted (queued, runs via Claude CLI in the active project's folder)

These bypass the Qwen router entirely and go straight to Claude, scoped to
the active project's directory with no browser access.

| Command | What it does |
|---|---|
| `/write <name>` | Select (or create) the file that `/write` continues — e.g. `/write draft` targets `draft.md` and remembers it per project. A **single-word** argument is always treated as a file selection, even if it happens to read like a one-word prompt. |
| `/write <prompt>` | (multi-word) Claude reads the currently selected file (and skims others for context), continues it per your prompt, **appends the new material**, and replies with the excerpt. If no file has been selected yet, it lists the project's files and explains the `/write <name>` step instead of guessing. |
| `/brainstorm <prompt>` | Claude reads every file in the active project for context and answers your question or helps work through a problem, as a **numbered list** (1. 2. 3. ...) — one distinct idea per number, so you can reference them later. **Does not modify any files** — advice only. |
| `/braindump <text>` | For messy, unstructured idea dumps. Claude reads the project's existing files, figures out which existing file each idea belongs in (or creates a new one — it picks a sensible filename), and **merges/rewrites it into the right place** — not a raw append. Replies with a short summary of what it changed/created and where. |
| `/panel <idea>` (alias `/council`) | A **panel of personas** discusses your idea. Claude reads the project's files and its panel roster, then writes each member's opening take in their own voice, a cross-talk round with at least one real disagreement, and a moderator's closing **numbered list of takeaways**. **Does not modify any files.** Because the takeaways are numbered, `/keep` works on a panel discussion exactly like it does on `/brainstorm` (`/keep 1, 3; scrap 2`). Real people are portrayed from their publicly known views, never with invented quotes, and every reply ends with a "Simulated personas" note. If the panel is empty it says so and points you to `/paneledit`. |
| `/panel` | (no idea) Show the current panel roster. |
| `/paneledit <changes>` | Add, remove or change panel members by describing what you want in plain words — e.g. `/paneledit add Steve Jobs, Tony Stark and a skeptical patent lawyer`, or `/paneledit drop Tony Stark and make the lawyer focus on IP risk`. Members can be **real people or fictional characters** (they need to be well known) or **generic roles/experts** like "a lawyer" or "a marketing strategist". The roster is stored as `panel.md` in the project (`## Name` + a line on their lens), so it's per-project and you can also `/read` or edit it like any other file. Aim for 3–6 members. |
| `/keep [guidance]` | Commits chosen parts of your **most recent `/brainstorm` or `/panel` reply** (for the active project) into its files — nothing from a brainstorm is ever applied automatically; this is the deliberate opt-in step. Reference the brainstorm's numbers directly: `/keep 1, 3, 5; amend 2: <replacement text>; scrap 4`. With no guidance, Claude uses judgment — committing concrete, decided ideas and leaving out anything that read as one option among several you hadn't chosen. Same synthesize-and-merge behavior as `/braindump`. Replies with what it kept and what it deliberately left out. If you haven't run `/brainstorm` or `/panel` yet for this project (or the bot restarted since), it tells you so instead of guessing. |
| `/research <query>` | **Web-research bridge.** Two steps: (1) Claude browses the web with `--chrome` to gather findings on your query, (2) the findings are filed into the active project via the same synthesize-and-merge pipeline `/braindump` uses (creating e.g. `research.md` if nothing existing fits, keeping source attributions). This is the **only** project command that opens a browser — kept as its own explicit command specifically so `/write`/`/brainstorm`/`/braindump`/`/keep` never risk launching Chrome unexpectedly. If the research step itself fails, nothing is filed. |

Examples:
```
/project ghostwriting-novel
/write draft
/write Continue the scene where Mira confronts her sister at the harbor. End on a cliffhanger.
/brainstorm I've written Mira as sympathetic but she betrays the crew in ch.9 — how do I foreshadow this without telegraphing it?
/keep keep 1 and 3 as-is, amend 2 to have her hide the letters instead of burning them, scrap 4
/braindump ok so Mira's mom might actually still be alive and hiding in the northern territories, also I think the harbor city should have some kind of curfew law tied to the old war, and Mira's crewmate Toln should have a grudge against the captain from something in ch.3
/research what 19th-century harbor curfew laws typically looked like, for the Mira subplot
```

**`/brainstorm` + `/keep`:** `/brainstorm` is pure advice — it never touches your
files, so an idea you don't like or want changed just stays in the chat and is
never applied. If something from it is worth keeping, `/keep` is the separate,
explicit step that commits it. Only the most recent `/brainstorm` reply per
(chat, project) is remembered, and only in memory — it's gone after a bot restart.

## Self-maintenance (edits the bot's own source)

These run Claude CLI directly against this project's own source tree (not
the sandbox), bypassing Ollama triage entirely — same pattern as `/write`/
`/brainstorm` but pointed at the bot's own code instead of a project folder.
No browser.

| Command | What it does |
|---|---|
| `/fix <bug description>` | Investigates and fixes the described bug — root-cause only, no new features, no unrelated refactoring. Replies with the root cause and which file(s) changed. |
| `/code <feature description>` | Implements the requested new feature, following the existing code style/architecture. Replies with a summary of what was built and which file(s) changed. |
| `/restart` | Restarts the whole bot process — no shell access needed. This is the only way `/fix`/`/code` changes actually take effect, since editing a `.py` file never hot-reloads the running process. Drops any in-flight/queued task first (like `/cancel`), replies, then re-executes itself as a fresh process that re-imports everything from disk. Expect a few seconds of downtime (mid-restart Telegram messages just won't be picked up until it's back). |

**Important:** editing source files does not hot-reload the running bot —
Python doesn't pick up changes to already-imported modules. After `/fix` or
`/code` finishes, run `/restart` for the change to take effect (the `/fix`/
`/code` reply reminds you of this).

**Safety net:** this project is a git repo specifically so these edits can be
diffed and reverted — `git log` / `git diff` / `git checkout -- <file>` all
work normally here. Nothing from `/fix` or `/code` is committed automatically;
review changes before committing them yourself.

`claude_dev` (what `/fix`/`/code` use internally) is structurally unreachable
from a plain message — see the note in the routing table above.

## Queue & memory control

| Command | What it does |
|---|---|
| `/cancel` (alias: `/stop`) | Cancels the task currently running and clears everything else waiting in the queue. |
| `/reset` | Clears this chat's short-term conversation memory — the rolling window of recent turns used to give the local Qwen router and `local` chat replies conversational context (e.g. so "do that again" resolves correctly). Use it to start a clean context without restarting the bot. Doesn't touch project files, the active project, the last `/brainstorm` reply remembered for `/keep`, or the task queue. |

## Notes on behavior

- All tasks (except `/project`, `/create`, `/list`, `/delete`, `/read`) run one at a
  time in a strict FIFO queue — Chrome automation and Claude CLI can't run
  concurrently. You'll get a "queued at position #N" reply and then live
  status updates as it runs.
- If a `claude_general` task looks destructive (delete/overwrite/reset/etc.),
  you'll get an inline **✅ Proceed / ❌ Cancel** prompt before it runs — it
  times out and auto-cancels after `DESTRUCTIVE_CONFIRMATION_TIMEOUT_SECONDS`
  (default 300s) if you don't respond.
- Long outputs (over ~3,500 characters) are sent as a short excerpt plus a
  `task_result.txt` attachment with the full text.
