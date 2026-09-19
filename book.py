"""
Book Writing & Lore Tracker — workspace isolation, fast-capture helpers, and
Claude prompt templates for manuscript/lore work.

All state lives under SANDBOX_DIR/book_project/ so it coexists cleanly with
the general sandbox and never touches the web-automation (--chrome) pipeline.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import config

BOOK_DIR: Path = config.SANDBOX_DIR / "book_project"

DRAFT_PATH: Path = BOOK_DIR / "draft.md"
CHARACTERS_PATH: Path = BOOK_DIR / "characters.md"
WORLDBUILDING_PATH: Path = BOOK_DIR / "worldbuilding.md"
NOTES_PATH: Path = BOOK_DIR / "notes.md"

_DEFAULT_CONTENTS: dict[Path, str] = {
    DRAFT_PATH: "# Draft\n\n",
    CHARACTERS_PATH: "# Characters\n\n",
    WORLDBUILDING_PATH: "# Worldbuilding\n\n",
    NOTES_PATH: "# Notes\n\n",
}

# Maps fast-capture command name -> (target file, human label for the confirmation reply).
FAST_CAPTURE_TARGETS: dict[str, tuple[Path, str]] = {
    "note": (NOTES_PATH, "notes.md"),
    "char": (CHARACTERS_PATH, "characters.md"),
    "lore": (WORLDBUILDING_PATH, "worldbuilding.md"),
}

# Per-chat last /brainstorm response, so /keep can commit chosen parts of it
# without the user having to paste it back in. In-memory only, like
# memory.ConversationMemory — lost on restart by design.
_LAST_BRAINSTORM: dict[int, str] = {}


def set_last_brainstorm(chat_id: int, text: str) -> None:
    """Record the most recent /brainstorm reply for this chat, for /keep to draw on."""
    _LAST_BRAINSTORM[chat_id] = text


def get_last_brainstorm(chat_id: int) -> str:
    """Return the most recent /brainstorm reply for this chat, or "" if there isn't one."""
    return _LAST_BRAINSTORM.get(chat_id, "")


def ensure_book_workspace() -> None:
    """Create book_project/ and its four markdown files if they don't exist yet."""
    BOOK_DIR.mkdir(parents=True, exist_ok=True)
    for path, header in _DEFAULT_CONTENTS.items():
        if not path.exists():
            path.write_text(header, encoding="utf-8")


def append_timestamped(path: Path, text: str) -> None:
    """Append a timestamped entry to one of the book markdown files."""
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"\n## {stamp}\n{text.strip()}\n"
    with path.open("a", encoding="utf-8") as f:
        f.write(entry)


_WRITE_TEMPLATE = """You are the writing assistant for an ongoing novel project.

This directory contains: draft.md (the manuscript), characters.md (profiles,
relationships, arcs), worldbuilding.md (lore, settings, rules), and notes.md
(unstructured brainstorms).

Read characters.md, worldbuilding.md, and the relevant end of draft.md for
context, then draft or continue the scene per the instruction below. Append
the new material to draft.md (do not rewrite or delete existing content).
Reply with just the newly written excerpt.

Instruction: {task}
"""

_BRAINSTORM_TEMPLATE = """You are a creative collaborator for an ongoing novel project.

Read all markdown files in this directory (draft.md, characters.md,
worldbuilding.md, notes.md) as reference context.

Do not modify any files. Answer the creative question or help untangle the
plot blocker below, grounded in the existing lore, characters, and manuscript.

Format your reply as a numbered list (1. 2. 3. ...), one distinct idea,
suggestion, or option per number. Keep each number a single self-contained
idea — this numbering is load-bearing: the user will reply later referencing
these exact numbers (e.g. "keep 1, 3; amend 2; scrap 4") to decide what gets
committed to the files, so don't bundle multiple ideas into one number or
skip numbers. A short intro or closing line outside the list is fine, but the
substantive ideas themselves must each be their own numbered item.

Question: {task}
"""

_AUTO_TEMPLATE = """You are the writing and lore assistant for an ongoing novel project.

This directory contains: draft.md (the manuscript), characters.md, worldbuilding.md,
and notes.md.

Read whichever of these files are relevant to the request below. If the request
asks you to write, draft, or continue the manuscript, append the new material to
draft.md and reply with the new excerpt. Otherwise (brainstorming, lore questions,
plot analysis), just answer directly without modifying any files.

Request: {task}
"""

_BRAINDUMP_TEMPLATE = """You are the lore/continuity editor for an ongoing novel project.

This directory contains: draft.md (the manuscript), characters.md (profiles,
relationships, arcs), worldbuilding.md (lore, settings, rules), and notes.md
(unstructured brainstorms).

The user has just brain-dumped raw, possibly rambling and disorganized ideas
below. Do NOT transcribe it verbatim. Instead:

1. Read characters.md, worldbuilding.md, and notes.md to see what's already there.
2. Work out which file each idea belongs in: character details, relationships,
   or arcs -> characters.md; setting, lore, or rules -> worldbuilding.md;
   anything else, loose plot ideas, or stuff that doesn't cleanly fit
   elsewhere -> notes.md. A single dump may touch more than one file.
3. Synthesize and integrate each idea into the right place in the right file:
   merge it into an existing section/character/topic if one already exists
   (don't create a duplicate entry for the same thing), or add a new, clearly
   headed section if it's genuinely new. Tighten and rewrite the prose as
   needed so the file stays coherent and readable — do not just paste the raw
   dump in verbatim.
4. Leave draft.md untouched. This command is for reference material only, not
   manuscript prose — use /write for that.
5. Reply with a short bullet list of what you updated and in which file(s).

Raw brain dump:
{task}
"""

_KEEP_TEMPLATE = """You are the lore/continuity editor for an ongoing novel project.

This directory contains: draft.md (the manuscript), characters.md (profiles,
relationships, arcs), worldbuilding.md (lore, settings, rules), and notes.md
(unstructured brainstorms).

The user previously ran /brainstorm, which replied with a numbered list of
distinct ideas (1. 2. 3. ...). Their guidance on what to actually commit is
included below, and will typically reference those same numbers — e.g. "keep
1, 3, 5; amend 2 to: <replacement text>; scrap 4" — meaning: commit ideas 1,
3, and 5 as given; commit idea 2 but replaced/modified per the amendment text
that follows it; and drop idea 4 entirely. Guidance may reference some
numbers and say nothing about others, or use different wording (e.g. "change"
for amend, "drop"/"skip" for scrap, "yes"/"go with" for keep) — interpret it
by intent, not by exact phrasing. Nothing from a brainstorm is applied
automatically — this is the deliberate, opt-in step where the user has
decided what to keep. Do NOT transcribe the brainstorm text verbatim.
Instead:

1. Read characters.md, worldbuilding.md, and notes.md to see what's already there.
2. Match each number in the guidance back to its corresponding numbered item
   in the brainstorm response below, and resolve it:
   - kept as-is -> commit that idea's substance.
   - amended -> commit the user's replacement/modification instead of (or
     blended with) the original idea, per what the amendment says.
   - scrapped -> leave it out entirely.
   If the guidance is empty, unspecific, or doesn't use numbers, use
   judgment instead: commit only concrete, decided ideas; leave out
   speculative alternatives, unweighed options, or open questions that were
   only offered as possibilities rather than chosen.
3. Explicitly leave out anything the guidance says to scrap/skip and anything
   left unmentioned that reads as just one option among several the user
   hasn't picked between.
4. Work out which file each kept idea belongs in: character details,
   relationships, or arcs -> characters.md; setting, lore, or rules ->
   worldbuilding.md; anything else, or an undecided plot direction worth
   flagging -> notes.md. A single response may touch more than one file.
5. Synthesize and integrate each kept idea into the right place in the right
   file: merge it into an existing section/character/topic if one already
   exists (don't create a duplicate entry for the same thing), or add a new,
   clearly headed section if it's genuinely new. Tighten and rewrite the
   prose as needed so the file stays coherent — do not paste the brainstorm
   text in verbatim.
6. Leave draft.md untouched. This command is for reference material only, not
   manuscript prose — use /write for that.
7. Reply with a short bullet list of what you committed and to which file(s),
   and separately note anything you deliberately left out and why.

{task}
"""

_TEMPLATES = {
    "write": _WRITE_TEMPLATE,
    "brainstorm": _BRAINSTORM_TEMPLATE,
    "auto": _AUTO_TEMPLATE,
    "braindump": _BRAINDUMP_TEMPLATE,
    "keep": _KEEP_TEMPLATE,
}


def build_prompt(task: str, mode: str = "auto") -> str:
    """Wrap a raw task/prompt with the instruction template for `mode`."""
    template = _TEMPLATES.get(mode, _AUTO_TEMPLATE)
    return template.format(task=task)
