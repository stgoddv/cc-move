"""Replaces one path with another in every form Claude Code writes it."""

import os
import re
from urllib.parse import quote

from .paths import full, list_dir, match_case, project_dir_name, split_drive

TEXT_EXTS = {".jsonl", ".json", ".md", ".txt"}


class PathRewriter:
    """Replaces the path `src` with `dst` inside a text.

    Recognized forms: C:\\x, C:\\\\x (JSON), C:\\\\\\\\x (JSON inside JSON), C:/x,
    /c/x (Git Bash), c%3A/x (VS Code URI) and the encoded name in ~/.claude/projects (c--x).
    It only replaces if the path ends where the folder name ends:
    when moving 'Proj' it does not touch 'Proj2', 'Proj-old' or 'Proj.bak'.
    """

    def __init__(self, src, dst):
        self.rules = []
        s_drive, s_parts = split_drive(src)
        d_drive, d_parts = split_drive(dst)
        drive = f"(?P<d>[{s_drive.upper()}{s_drive.lower()}])"
        end = _end_of_name(src)

        for sep in ("\\\\\\\\", "\\\\", "\\", "/"):  # 4, 2 and 1 backslashes, and forward slash
            pattern = r"(?<![A-Za-z0-9])" + drive + ":" + _joined(s_parts, sep) + end
            self._add(pattern, d_drive, ":" + sep + sep.join(d_parts))

        pattern = r"(?<![\w.:/-])/" + drive + _joined(s_parts, "/") + end
        self._add(pattern, d_drive, "/" + "/".join(d_parts), prefix="/")

        # VS Code URI: file:///c%3A/Users/a%20b
        s_uri, d_uri = [uri_quote(p) for p in s_parts], [uri_quote(p) for p in d_parts]
        pattern = r"(?<![A-Za-z0-9])" + drive + "%3A" + _joined(s_uri, "/") + end
        self._add(pattern, d_drive, "%3A/" + "/".join(d_uri))

        s_enc, d_enc = project_dir_name(full(src)), project_dir_name(full(dst))
        pattern = r"(?<![A-Za-z0-9-])" + drive + re.escape(s_enc[1:]) + r"(?![A-Za-z0-9-])"
        self._add(pattern, d_enc[0], d_enc[1:])

        base = os.path.basename(full(src))
        self._needles = {base.lower(), project_dir_name(base).lower(), uri_quote(base).lower()}

    def _add(self, pattern, drive, tail, prefix=""):
        """Registers one form of the path. The replacement keeps the case of the drive letter found,
        and is all upper case if what was found was (the Python extension stores paths that way)."""
        def replacement(m):
            new = prefix + match_case(drive, m.group("d")) + tail
            return new.upper() if m.group(0).isupper() else new

        self.rules.append((re.compile(pattern, re.IGNORECASE), replacement))

    def might_match(self, text):
        """Quick filter: if the folder name does not appear, there is nothing to replace."""
        low = text.lower()
        return any(n in low for n in self._needles)

    def sub(self, text):
        """Returns (new text, number of replacements)."""
        if not self.might_match(text):
            return text, 0
        total = 0
        for regex, replacement in self.rules:
            text, n = regex.subn(replacement, text)
            total += n
        return text, total


def uri_quote(part):
    """Encodes a path part the way VS Code does in its URIs (space -> %20)."""
    return quote(part, safe="")


def _joined(parts, sep):
    """Regular expression for `sep` + parts joined by `sep`."""
    s = re.escape(sep)
    return s + s.join(re.escape(p) for p in parts)


def _end_of_name(src):
    """Lookahead that requires the folder name to end right there."""
    parent, base = os.path.split(full(src))
    # If a sibling "<base> something" exists, a space is not enough to end the name.
    has_sibling_with_space = any(n.lower().startswith(base.lower() + " ") for n in list_dir(parent))
    space = r"\t\r\n" if has_sibling_with_space else r"\s"
    return r"(?=$|[\\/\"'`)\]}>,;:|*?<#&" + space + r"]|\.(?![A-Za-z0-9_-]))"


def rewrite_file(path, rewriter, dry=False):
    """Applies the replacement to a text file, keeping its date. Returns the number of replacements."""
    with open(path, "rb") as f:
        text = f.read().decode("utf-8", "surrogateescape")
    new, n = rewriter.sub(text)
    if n and not dry:
        st = os.stat(path)
        tmp = path + ".cc-move.tmp"
        with open(tmp, "wb") as f:
            f.write(new.encode("utf-8", "surrogateescape"))
        # The session picker sorts by date and the automatic cleanup of old
        # transcripts also uses it: they must not be "made younger".
        os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
        os.replace(tmp, path)
    return n


def rewrite_dir(path, rewriter, dry=False):
    """Applies the replacement to all text files in a folder, recursively."""
    replacements = files = 0
    for root, _, names in os.walk(path):
        for name in names:
            if os.path.splitext(name)[1].lower() in TEXT_EXTS:
                n = rewrite_file(os.path.join(root, name), rewriter, dry)
                if n:
                    replacements += n
                    files += 1
    return {"replacements": replacements, "files": files}
