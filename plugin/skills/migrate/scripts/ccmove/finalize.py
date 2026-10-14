"""Final steps on the folders: archive the original and leave the note in CLAUDE.local.md."""

import os
import subprocess
import time

from .paths import full

NOTE_START = "<!-- cc-move:start -->"
NOTE_END = "<!-- cc-move:end -->"
NOTE_FILE = "CLAUDE.local.md"


def archive_path(src, date):
    """<parent>\\archive-<date>-<name>, with suffix -2, -3... if it already exists."""
    parent, base = os.path.split(full(src))
    name = f"archive-{date}-{base}"
    candidate, i = os.path.join(parent, name), 2
    while os.path.exists(candidate):
        candidate, i = os.path.join(parent, f"{name}-{i}"), i + 1
    return candidate


def archive_original(src, date):
    """Renames the original. Returns (new path, None) or (None, error) if Windows does not allow it."""
    target = archive_path(src, date)
    error = rename_folder(src, target)
    return (None, error) if error else (target, None)


def rename_folder(src, target, attempts=5, wait=2):
    """Renames a folder. Returns None, or the error if Windows does not allow it.

    Retries for a few seconds: after closing the project window, VS Code (or the Windows indexer)
    may take a while to release the folder and the first attempt fails with "Access denied".
    """
    for attempt in range(attempts):
        try:
            os.rename(src, target)
            return None
        except OSError as e:
            error = e
            if attempt < attempts - 1:
                time.sleep(wait)
    return str(error)


def note_text(record):
    """Section of CLAUDE.local.md that explains the migration to a future agent."""
    archive = record.get("archive") or archive_path(record["src"], record["date"])
    deps = _deps_line(record)
    return "\n".join([
        NOTE_START,
        f"## Folder migration ({record['date']})",
        f"This project was moved from `{record['src']}` with cc-move. The copy was verified by hash.",
        *([deps] if deps else []),
        f"The original was left intact in `{archive}` until {record['delete_after']}.",
        "If an error appears that did not happen before (missing dependency, nonexistent path, broken link),",
        "suspect the migration first and compare with the archived original.",
        "- Recreated: (pending, phase 2)",
        "- Pending / unresolved: (pending, phase 2)",
        "Delete this section together with the archived folder.",
        NOTE_END,
    ])


def _deps_line(record):
    """Line of the note about dependency folders; None if the scan found none."""
    dirs = record.get("dep_dirs")
    if dirs is None:  # registered by cc-move 0.1.x, which did not record them
        listed = "`node_modules` and the Python virtual environments"
    elif not dirs:
        return None
    else:
        more = f" and {len(dirs) - 5} more" if len(dirs) > 5 else ""
        listed = "The dependency folders (" + ", ".join(f"`{d}`" for d in dirs[:5]) + more + ")"
    if record.get("with_deps"):
        return f"{listed} were copied as they were; they were reviewed afterwards (below)."
    return f"{listed} were not copied: they were recreated afterwards (below)."


def write_note(project, record):
    """Writes the note; if CLAUDE.local.md already exists, appends the section at the end or replaces it."""
    path = os.path.join(project, NOTE_FILE)
    section = note_text(record)
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            content = f.read()
        if NOTE_START in content and NOTE_END in content:
            start, end = content.index(NOTE_START), content.index(NOTE_END) + len(NOTE_END)
            content = content[:start] + section + content[end:]
        else:
            content = content.rstrip("\n") + "\n\n" + section + "\n"
    else:
        content = section + "\n"
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)


def ignore_note_in_git(project):
    """Ignores CLAUDE.local.md via .git/info/exclude (local, not versioned). Returns what it did."""
    git_dir = os.path.join(project, ".git")
    if os.path.isfile(git_dir):
        return ".git is a file (worktree or submodule): ignore CLAUDE.local.md by hand"
    if not os.path.isdir(git_dir):
        return "not a git repo"
    if subprocess.run(["git", "-C", project, "check-ignore", "-q", NOTE_FILE], capture_output=True).returncode == 0:
        return "already ignored by git"
    exclude = os.path.join(git_dir, "info", "exclude")
    os.makedirs(os.path.dirname(exclude), exist_ok=True)
    with open(exclude, "a", encoding="utf-8", newline="\n") as f:
        f.write(_newline_if_missing(exclude) + f"# cc-move\n{NOTE_FILE}\n")
    return "added to .git/info/exclude"


def _newline_if_missing(path):
    """'\\n' if the file does not end with a newline (so lines are not joined when appending)."""
    if not os.path.isfile(path) or not os.path.getsize(path):
        return ""
    with open(path, "rb") as f:
        f.seek(-1, os.SEEK_END)
        return "" if f.read(1) == b"\n" else "\n"


def agents_md_warning(project):
    """Warning if there is an AGENTS.md without CLAUDE.md: CLAUDE.local.md would make Claude Code ignore AGENTS.md."""
    if os.path.isfile(os.path.join(project, "AGENTS.md")) and not os.path.isfile(os.path.join(project, "CLAUDE.md")):
        return ("There is an AGENTS.md without CLAUDE.md: with CLAUDE.local.md present, Claude Code ignores AGENTS.md. "
                "Resolve in phase 2.")
    return None
