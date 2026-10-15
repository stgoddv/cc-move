"""Final steps on the folders: archive the original and leave the note in CLAUDE.local.md."""

import glob
import os
import re
import stat
import subprocess
import sys
import time

from .paths import full, list_dir
from .registry import source_is_copy
from .rewriter import PathRewriter

NOTE_START = "<!-- cc-move:start -->"
NOTE_END = "<!-- cc-move:end -->"
NOTE_FILE = "CLAUDE.local.md"
ARCHIVE_LINE = "The original was left intact in `{archive}` until {delete_after}."
ARCHIVE_IN_NOTE = re.compile(r"(The original was left intact in `)[^`]*(`)")
INTERMEDIATE_LINE = ("The intermediate copy `{src}` was archived in `{archive}`: it is only a copy, "
                     "the true original is the one above.")


def archive_path(src, date):
    """<parent>\\archive-<date>-<name>, with suffix -2, -3... if it already exists."""
    parent, base = os.path.split(full(src))
    name = f"archive-{date}-{base}"
    candidate, i = os.path.join(parent, name), 2
    while os.path.exists(candidate):
        candidate, i = os.path.join(parent, f"{name}-{i}"), i + 1
    return candidate


def existing_archives(src, date):
    """Folders that already have the archive name of `src` (archive-<date>-<name>, -2, -3...)."""
    parent, base = os.path.split(full(src))
    name = re.compile(re.escape(f"archive-{date}-{base}") + r"(-\d+)?$", re.IGNORECASE)
    return [os.path.join(parent, n) for n in sorted(list_dir(parent))
            if name.match(n) and is_real_folder(os.path.join(parent, n))]


def is_real_folder(path):
    """True if `path` is a folder and not a link or junction to one."""
    return os.path.isdir(path) and not os.lstat(path).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT


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


def rename_hint(folder):
    """What may be keeping `folder` open when Windows refuses to rename it, and how to find out."""
    hints = [f"{path} installs this project in editable mode in a global Python: Pylance (which uses that Python by "
             "default) and VS Code's file watcher then keep the folder open in every VS Code window. Uninstall the "
             "package from that Python (pip uninstall, or uv pip uninstall --python <that python>) and restart "
             "Pylance or the extension host." for path in editable_installs(folder)]
    hints.append("To see which process holds the folder: Resource Monitor (resmon) > CPU > Associated Handles, "
                 "search for its name (Explorer does not name it, and Restart Manager does not see folder handles).")
    return " ".join(hints)


def editable_installs(folder):
    """Files in the global Pythons' site-packages that install a package in editable mode from inside `folder`
    (*.pth, setuptools' __editable__ finders, direct_url.json)."""
    finder = PathRewriter(folder, folder)  # recognizes the path in every form it is written in
    found = []
    for site in python_site_dirs():
        for name in list_dir(site):
            low = name.lower()
            if low.endswith(".pth") or (low.startswith("__editable__") and low.endswith(".py")):
                path = os.path.join(site, name)
            elif low.endswith(".dist-info"):
                path = os.path.join(site, name, "direct_url.json")
            else:
                continue
            text = _read_text(path)
            if text and finder.finds(text):
                found.append(path)
    return found


def python_site_dirs():
    """site-packages folders of the Python installations outside any venv: the python.org installs, this Python,
    those the py launcher knows, and the per-user site-packages."""
    prefixes = glob.glob(os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Python", "Python*"))
    prefixes += [sys.base_prefix] + launcher_pythons()
    dirs = [os.path.join(p, "Lib", "site-packages") for p in prefixes]
    dirs += glob.glob(os.path.join(os.environ.get("APPDATA", ""), "Python", "Python3*", "site-packages"))
    unique = {os.path.normcase(os.path.abspath(d)): d for d in dirs if os.path.isdir(d)}
    return sorted(unique.values(), key=str.lower)


def launcher_pythons():
    """Installation folders listed by `py -0p` (none if the py launcher is missing or fails)."""
    try:
        r = subprocess.run(["py", "-0p"], capture_output=True, text=True, errors="replace", timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return []
    exes = re.findall(r"([A-Za-z]:\\.*?\.exe)\s*$", r.stdout, re.MULTILINE)
    return [os.path.dirname(exe) for exe in exes]


def _read_text(path):
    """Contents of a small text file, or None if it does not exist or cannot be read."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(1_000_000)
    except OSError:
        return None


def note_text(record, chain=(), earlier_note=None):
    """Section of CLAUDE.local.md that explains the migration to a future agent.

    `chain` holds the earlier migrations that led to the source (Registry.chain): the note then names the first
    source and the true original, and says that this migration's archive is only an intermediate copy.
    `earlier_note` is the body of the section the copy already had, kept below so nothing written there is lost.
    """
    archive = record.get("archive") or archive_path(record["src"], record["date"])
    deps = _deps_line(record)
    return "\n".join([
        NOTE_START,
        f"## Folder migration ({record['date']})",
        *_origin_lines(record, list(chain), archive),
        *([deps] if deps else []),
        "If an error appears that did not happen before (missing dependency, nonexistent path, broken link),",
        "suspect the migration first and compare with the archived original.",
        "- Recreated: (pending, phase 2)",
        "- Pending / unresolved: (pending, phase 2)",
        "Delete this section together with the archived folder.",
        *(["", "### Earlier move", earlier_note] if earlier_note else []),
        NOTE_END,
    ])


def _origin_lines(record, chain, archive):
    """Where the project comes from and where the original is: this migration's, or the first copy's in a chain."""
    if not source_is_copy(chain):
        return [f"This project was moved from `{(chain or [record])[0]['src']}` with cc-move. "
                "The copy was verified by hash.",
                ARCHIVE_LINE.format(archive=archive, delete_after=record["delete_after"])]
    first_copy = next(r for r in chain if r.get("kind") != "case-rename")
    through = ", ".join(f"`{r['dst']}`" for r in chain)
    return [f"This project was moved from `{chain[0]['src']}` with cc-move, through {through}. "
            "Each copy was verified by hash.",
            ARCHIVE_LINE.format(archive=first_copy.get("archive") or "(not archived)",
                                delete_after=first_copy["delete_after"]),
            INTERMEDIATE_LINE.format(src=record["src"], archive=archive)]


def _deps_line(record):
    """Line of the note about dependency folders; None if the scan found none."""
    dirs = record.get("dep_dirs")
    builds = len(record.get("build_dep_dirs") or [])
    in_builds = f" ({builds} more inside build outputs are regenerated by the build)" if builds else ""
    if dirs is None:  # registered by cc-move 0.1.x, which did not record them
        listed = "`node_modules` and the Python virtual environments"
    elif not dirs:
        return f"The dependency folders inside build outputs ({builds}) are regenerated by the build." if builds else None
    else:
        more = f" and {len(dirs) - 5} more" if len(dirs) > 5 else ""
        listed = "The dependency folders (" + ", ".join(f"`{d}`" for d in dirs[:5]) + more + ")"
    if record.get("with_deps"):
        return f"{listed} were copied as they were; they were reviewed afterwards (below){in_builds}."
    return f"{listed} were not copied: they were recreated afterwards (below){in_builds}."


def write_note(project, record, chain=()):
    """Writes the note; if CLAUDE.local.md already exists, appends the section at the end or replaces it.
    A section already there belongs to an earlier move (the copy brought it along): it is kept inside the new one."""
    content = _read_note_file(project)
    if content is None:
        content = note_text(record, chain) + "\n"
    elif _section_bounds(content):
        start, end = _section_bounds(content)
        earlier = _demoted(content[start + len(NOTE_START):end - len(NOTE_END)].strip("\n"))
        content = content[:start] + note_text(record, chain, earlier) + content[end:]
    else:
        content = content.rstrip("\n") + "\n\n" + note_text(record, chain) + "\n"
    _write_note_file(project, content)


def update_note_archive(project, record, chain=()):
    """Puts the archive's final path in the note, keeping the rest of the section (phase 2 may have filled it in).
    Writes the whole note if there is no section to update."""
    content = _read_note_file(project)
    bounds = content and _section_bounds(content)
    intermediate = re.compile("(" + re.escape(INTERMEDIATE_LINE.split("{archive}")[0].format(src=record["src"]))
                              + ")[^`]*(`)")
    pattern = next((p for p in (intermediate, ARCHIVE_IN_NOTE) if bounds and p.search(content, *bounds)), None)
    if not pattern:
        return write_note(project, record, chain)
    start, end = bounds
    section = pattern.sub(lambda m: m.group(1) + record["archive"] + m.group(2), content[start:end], count=1)
    _write_note_file(project, content[:start] + section + content[end:])


def _demoted(text):
    """Markdown with every heading one level lower, to nest an earlier note inside the new one."""
    return re.sub(r"^#", "##", text, flags=re.MULTILINE)


def _section_bounds(content):
    """(start, end) of the cc-move section in the text of CLAUDE.local.md, or None."""
    if NOTE_START not in content or NOTE_END not in content:
        return None
    return content.index(NOTE_START), content.index(NOTE_END) + len(NOTE_END)


def _read_note_file(project):
    """Text of the project's CLAUDE.local.md, or None if it does not exist."""
    path = os.path.join(project, NOTE_FILE)
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        return f.read()


def _write_note_file(project, content):
    """Saves the project's CLAUDE.local.md with Unix line endings."""
    with open(os.path.join(project, NOTE_FILE), "w", encoding="utf-8", newline="\n") as f:
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
