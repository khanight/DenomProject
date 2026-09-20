"""
Project Thinking Partner — generic, project-agnostic workspace management and
Claude prompt templates.

Each "project" is just a directory of markdown files under
SANDBOX_DIR/projects/<name>/ with no fixed schema — unlike a hardcoded set of
files, commands discover whatever .md files exist and let Claude decide where
new content belongs, creating files as needed. This lets the same commands
(/braindump, /brainstorm, /keep, /write, /create, /list, /delete) serve any
kind of note-taking/thinking-partner use case, not just book writing.

State lives under SANDBOX_DIR/projects/ so it coexists cleanly with the
general sandbox and never touches the web-automation (--chrome) pipeline.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import config

PROJECTS_DIR: Path = config.SANDBOX_DIR / "projects"

# Characters disallowed in a project or file name — blocks path traversal and
# nested paths. Names are single path segments only.
_INVALID_NAME_CHARS = set('/\\:*?"<>|')


def _sanitize_name(name: str) -> Optional[str]:
    """Return a safe single-path-segment name, or None if `name` is invalid."""
    name = name.strip()
    if not name or name in (".", ".."):
        return None
    if any(c in _INVALID_NAME_CHARS for c in name):
        return None
    return name


def normalize_project_name(name: str) -> Optional[str]:
    """Sanitize a project name. Returns None if invalid."""
    return _sanitize_name(name)


def normalize_md_name(name: str) -> Optional[str]:
    """Sanitize a file name and ensure it ends in .md. Returns None if invalid."""
    clean = _sanitize_name(name)
    if clean is None:
        return None
    if not clean.lower().endswith(".md"):
        clean += ".md"
    return clean


def project_dir(name: str) -> Path:
    """Path to a project's directory (may not exist yet)."""
    return PROJECTS_DIR / name


def list_projects() -> list[str]:
    """List existing project names, sorted."""
    if not PROJECTS_DIR.exists():
        return []
    return sorted(p.name for p in PROJECTS_DIR.iterdir() if p.is_dir())


def ensure_project(name: str) -> bool:
    """Create a project's directory if it doesn't exist. Returns True if it was newly created."""
    path = project_dir(name)
    created = not path.exists()
    path.mkdir(parents=True, exist_ok=True)
    return created


def list_md_files(path: Path) -> list[str]:
    """List .md file names (not full paths) directly inside `path`, sorted."""
    if not path.exists():
        return []
    return sorted(p.name for p in path.glob("*.md") if p.is_file())


def create_md_file(path: Path, filename: str) -> tuple[Path, bool]:
    """
    Create `filename` inside `path` if it doesn't already exist.

    Returns (full_path, created) — created is False if the file already existed.
    """
    path.mkdir(parents=True, exist_ok=True)
    file_path = path / filename
    created = not file_path.exists()
    if created:
        title = Path(filename).stem.replace("_", " ").replace("-", " ").strip().title() or filename
        file_path.write_text(f"# {title}\n\n", encoding="utf-8")
    return file_path, created


def delete_md_file(path: Path, filename: str) -> bool:
    """Delete `filename` inside `path` if it exists. Returns whether a file was deleted."""
    file_path = path / filename
    if file_path.is_file() and file_path.suffix.lower() == ".md" and file_path.parent == path:
        file_path.unlink()
        return True
    return False


def no_active_project_message() -> str:
    """Shared reply text for any project-scoped command run with no active project set."""
    projects = list_projects()
    if projects:
        listing = ", ".join(projects)
        return (
            "No active project selected.\n"
            f"Existing projects: {listing}\n\n"
            "Use /project <name> to switch to one of those, or /project <new-name> to start a new one."
        )
    return (
        "No active project selected, and none exist yet.\n"
        "Use /project <name> to create your first one."
    )


# --- Discussion panel (roster lives in the project as panel.md) ---

PANEL_FILENAME = "panel.md"

EMPTY_PANEL_MESSAGE = (
    "The panel for this project is empty.\n\n"
    "Use /paneledit to add members, describing what you want in plain words, e.g.:\n"
    "/paneledit add Steve Jobs, Tony Stark and a skeptical patent lawyer\n\n"
    "Members can be real people or fictional characters (as long as they're "
    "well known), or generic roles/experts like \"a lawyer\" or \"a marketing strategist\"."
)


def panel_path(project: str) -> Path:
    """Path to a project's panel roster file (may not exist yet)."""
    return project_dir(project) / PANEL_FILENAME


def panel_members(project: str) -> list[str]:
    """Names of the panel's members, read from `## Name` headings in panel.md."""
    path = panel_path(project)
    if not path.is_file():
        return []
    members: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## ") and line[3:].strip():
            members.append(line[3:].strip())
    return members


# --- Per-chat state (in-memory only, like memory.ConversationMemory — lost on restart) ---

# Active project per chat.
_ACTIVE_PROJECT: dict[int, str] = {}

# Active "write target" file per (chat_id, project) — what bare `/write <name>`
# selects and subsequent `/write <prompt>` calls write into.
_ACTIVE_WRITE_FILE: dict[tuple[int, str], str] = {}

# Most recent /brainstorm reply per (chat_id, project), so /keep can commit
# chosen parts of it without the user having to paste it back in.
_LAST_BRAINSTORM: dict[tuple[int, str], str] = {}


def set_active_project(chat_id: int, name: str) -> None:
    _ACTIVE_PROJECT[chat_id] = name


def get_active_project(chat_id: int) -> Optional[str]:
    return _ACTIVE_PROJECT.get(chat_id)


def set_active_write_file(chat_id: int, project: str, filename: str) -> None:
    _ACTIVE_WRITE_FILE[(chat_id, project)] = filename


def get_active_write_file(chat_id: int, project: str) -> Optional[str]:
    return _ACTIVE_WRITE_FILE.get((chat_id, project))


def set_last_brainstorm(chat_id: int, project: str, text: str) -> None:
    _LAST_BRAINSTORM[(chat_id, project)] = text


def get_last_brainstorm(chat_id: int, project: str) -> str:
    return _LAST_BRAINSTORM.get((chat_id, project), "")


# --- Claude prompt templates ---

_WRITE_TEMPLATE = """You are a writing assistant helping continue an ongoing document
for this project.

Files currently in this project: {file_list}

The user wants to continue/extend "{target_file}". Read {target_file} (and skim
any other files here that give useful context) then, per the instruction below,
write the next part and append it to {target_file} — do not rewrite or delete
existing content. Reply with just the newly written excerpt.

Instruction: {task}
"""

_BRAINSTORM_TEMPLATE = """You are a thinking partner for this project.

Files currently in this project: {file_list}

Read all of them as reference context. Do not modify any files. Answer the
question or help work through the problem below, grounded in what's already
there.

Format your reply as a numbered list (1. 2. 3. ...), one distinct idea,
suggestion, or option per number. Keep each number a single self-contained
idea — this numbering is load-bearing: the user may reply later referencing
these exact numbers (e.g. "keep 1, 3; amend 2; scrap 4") to decide what gets
committed to the files, so don't bundle multiple ideas into one number or
skip numbers. A short intro or closing line outside the list is fine, but the
substantive ideas themselves must each be their own numbered item.

Question: {task}
"""

_BRAINDUMP_TEMPLATE = """You are the organizer for this project's notes.

Files currently in this project: {file_list}

The user has just brain-dumped raw, possibly rambling and disorganized ideas
below. Do NOT transcribe it verbatim. Instead:

1. Read the existing files to see what's already there.
2. Work out which existing file each idea belongs in, or whether it needs a
   new file — choose a short, clear filename based on the content (e.g.
   "characters.md", "timeline.md", "budget.md"). A single dump may touch more
   than one file.
3. Synthesize and integrate each idea into the right place in the right file:
   merge it into an existing section/topic if one already exists (don't
   create a duplicate entry for the same thing), or add a new, clearly headed
   section if it's genuinely new. Tighten and rewrite the prose as needed so
   the file stays coherent and readable — do not just paste the raw dump in
   verbatim.
4. Reply with a short bullet list of what you updated or created, and in
   which file(s).

Raw brain dump:
{task}
"""

_KEEP_TEMPLATE = """You are the organizer for this project's notes.

Files currently in this project: {file_list}

The user previously ran /brainstorm (a numbered list of distinct ideas) or
/panel (a panel discussion that closes with a numbered list of takeaways) —
either way, the numbers below refer to that numbered list (1. 2. 3. ...), and
any panel back-and-forth before it is context only. Their guidance on what to actually commit is
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

1. Read the existing files to see what's already there.
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
4. Work out which existing file each kept idea belongs in, or whether it
   needs a new file (choose a short, clear filename based on the content). A
   single response may touch more than one file.
5. Synthesize and integrate each kept idea into the right place in the right
   file: merge it into an existing section/topic if one already exists
   (don't create a duplicate entry for the same thing), or add a new, clearly
   headed section if it's genuinely new. Tighten and rewrite the prose as
   needed so the file stays coherent — do not paste the brainstorm text in
   verbatim.
6. Reply with a short bullet list of what you committed and to which
   file(s), and separately note anything you deliberately left out and why.

{task}
"""

_AUTO_TEMPLATE = """You are a thinking partner and organizer for this project.

Files currently in this project: {file_list}

Read whichever of these files are relevant to the request below. If the
request asks you to extend or continue a specific document, append the new
material to that file and reply with the excerpt. Otherwise (questions,
brainstorming, organizing notes), use judgment: answer directly if no file
change is called for, or update/create file(s) as appropriate.

Request: {task}
"""

_RESEARCH_TEMPLATE = """You are the organizer for this project's notes.

Files currently in this project: {file_list}

The user asked for live web research; the findings gathered are included
below. Do NOT transcribe them verbatim. Instead:

1. Read the existing files to see what's already there.
2. Work out which existing file the findings belong in, or whether they need
   a new file — choose a short, clear filename based on the content (e.g.
   "research.md", "sources.md").
3. Synthesize and integrate the findings into the right place in the right
   file: merge into an existing section/topic if one already exists (don't
   duplicate), or add a new, clearly headed section if genuinely new. Keep
   any source attributions from the findings so they stay traceable. Tighten
   and rewrite the prose so the file stays coherent and readable.
4. Reply with a short bullet list of what you added and in which file(s).

{task}
"""

_PANEL_TEMPLATE = """You are moderating a panel discussion for this project.

Files currently in this project: {file_list}

1. Read panel.md — each "## Name" heading is a panel member, followed by a line
   or two on their lens. Read the project's other files too as context. Do not
   modify any files.
2. The panel discusses the idea below. Members may be real well-known people,
   fictional characters, or generic roles/experts. For a real person, channel
   their publicly known views, priorities and communication style — never
   invent quotes or specific statements and present them as things they
   actually said. If a member has no lens written, infer a sensible one from
   who or what they are.
3. Format:
   - **Opening takes**: each member reacts in their own voice, 2-4 sentences,
     under a bold name label. Voices must be distinct.
   - **Cross-talk**: members respond to each other. At least one real
     disagreement — do not let everyone politely agree.
   - **Moderator's takeaways**: a neutral moderator closes with a numbered
     list (1. 2. 3. ...), one distinct, self-contained takeaway, risk or
     recommendation per number. This is the ONLY numbered list in your reply —
     the user will later reference these exact numbers (e.g. "keep 1, 3; scrap
     2") to decide what gets committed to the files, so don't bundle ideas or
     skip numbers.
4. End with one italic line: "Simulated personas — not real statements."

Idea for the panel: {task}
"""

_PANELEDIT_TEMPLATE = """You maintain the discussion panel roster for this project.

Files currently in this project: {file_list}

The roster lives in panel.md (create it if it doesn't exist yet). Format: one
"## Name" heading per member, followed by one or two lines on their lens —
what they focus on, care about, and how they'd push back on ideas.

Apply the user's requested changes below to panel.md — adding, removing,
renaming or re-describing members. Leave the other members untouched. Members
may be real well-known people, fictional characters, or generic roles/experts
(e.g. "Skeptical Lawyer"). For a real person, base the lens on their publicly
known views and style. If a name is not a well-known real person or character
and isn't clearly a role, don't guess who it is — leave it out and say so.
Aim for at most 6 members; mention it if the result goes past that. Do not
touch any other file.

Reply with the panel's roster as it now stands (name + a few words of lens per
member), plus one line on what changed.

Requested changes: {task}
"""

_TEMPLATES = {
    "write": _WRITE_TEMPLATE,
    "brainstorm": _BRAINSTORM_TEMPLATE,
    "auto": _AUTO_TEMPLATE,
    "braindump": _BRAINDUMP_TEMPLATE,
    "keep": _KEEP_TEMPLATE,
    "research": _RESEARCH_TEMPLATE,
    "panel": _PANEL_TEMPLATE,
    "paneledit": _PANELEDIT_TEMPLATE,
}


def build_prompt(
    task: str,
    mode: str,
    existing_files: list[str],
    target_file: Optional[str] = None,
) -> str:
    """Wrap a raw task/prompt with the instruction template for `mode`."""
    template = _TEMPLATES.get(mode, _AUTO_TEMPLATE)
    file_list = ", ".join(existing_files) if existing_files else "(no files yet)"
    return template.format(task=task, file_list=file_list, target_file=target_file)
