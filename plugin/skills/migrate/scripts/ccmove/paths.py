"""Windows paths: normalization, long paths and the folder name Claude Code uses."""

import os
import re

from . import Abort


def full(p):
    """Absolute, normalized path without a trailing separator."""
    return os.path.normpath(os.path.abspath(p))


def long_path(p):
    """Path with the \\\\?\\ prefix so Windows accepts more than 260 characters."""
    p = full(p)
    return p if p.startswith("\\\\?\\") else "\\\\?\\" + p


def strip_long_prefix(p):
    """Removes the \\\\?\\ prefix (or \\??\\ from junction targets) to display or compare paths."""
    if p.startswith("\\\\?\\UNC\\"):
        return "\\\\" + p[8:]
    if p.startswith(("\\\\?\\", "\\??\\")):
        return p[4:]
    return p


def to_windows(p):
    """'/c/Users/x' (Git Bash) or 'c:/Users/x' -> 'c:\\Users\\x'."""
    m = re.match(r"^/([a-zA-Z])(/.*)?$", p)
    if m:
        p = m.group(1) + ":" + (m.group(2) or "/")
    return full(p.replace("/", "\\"))


def same_path(a, b):
    """Compares paths the way Windows does: case-insensitive."""
    return os.path.normcase(full(a)) == os.path.normcase(full(b))


def is_under(p, root):
    """True if `p` is `root` or is inside it (case-insensitive)."""
    p, root = os.path.normcase(full(p)), os.path.normcase(full(root))
    return p == root or p.startswith(root.rstrip("\\") + "\\")


def project_dir_name(p):
    """Session folder in ~/.claude/projects: every non-alphanumeric character becomes '-'."""
    return re.sub(r"[^a-zA-Z0-9]", "-", p)


def match_case(letter, sample):
    """`letter` in upper or lower case according to `sample` (for the drive letter)."""
    return letter.lower() if sample.islower() else letter.upper()


def split_drive(p):
    """'C:\\a\\b' -> ('C', ['a', 'b'])."""
    drive, rest = os.path.splitdrive(full(p))
    if len(drive) != 2 or drive[1] != ":":
        raise Abort(f"Only paths with a drive letter are supported: {p}")
    return drive[0], [x for x in rest.split("\\") if x]


def list_dir(p):
    """os.listdir that returns [] if the folder does not exist or cannot be read."""
    try:
        return os.listdir(p)
    except OSError:
        return []


def write_text_atomic(path, text):
    """Writes to a temp file and renames it: never leaves the file half written."""
    tmp = path + ".cc-move.tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    os.replace(tmp, path)
