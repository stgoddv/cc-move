"""What Claude Code stores per project and how to reassociate it to a new path.

Claude Code links sessions to the folder by its path, in three places:
- ~/.claude/projects/<encoded path>/      transcripts, subagents, tool-results and memory
- ~/.claude.json                          per-project configuration (keys = paths)
- ~/.claude/history.jsonl                 prompt history (`project` field)
"""

import ctypes
import datetime as dt
import json
import os
import shutil

from . import Abort
from .paths import full, is_under, list_dir, match_case, project_dir_name, to_windows, write_text_atomic
from .rewriter import PathRewriter, rewrite_dir

# Above this length Claude Code truncates the name and adds an undocumented hash.
MAX_DIR_NAME = 200


class ClaudeState:
    """Location of the Claude Code files under a user folder."""

    def __init__(self, home):
        home = full(home)
        self.claude_dir = os.path.join(home, ".claude")
        self.config = os.path.join(home, ".claude.json")
        self.projects = os.path.join(self.claude_dir, "projects")
        self.history = os.path.join(self.claude_dir, "history.jsonl")
        self.sessions = os.path.join(self.claude_dir, "sessions")
        self.ide_locks = os.path.join(self.claude_dir, "ide")
        self.usage_data = os.path.join(self.claude_dir, "usage-data")
        self.backups = os.path.join(self.claude_dir, "backups", "cc-move")

    def project_path(self, dir_name):
        """Full path of a session folder."""
        return os.path.join(self.projects, dir_name)

    # ---------------------------------------------------------- session folders

    def session_dirs(self, root, also_under=()):
        """Session folders of `root` and its subfolders.

        The encoded name can match that of another project ('app' is a prefix of
        'app-api'), so it is confirmed with the real cwd read from the transcript.
        `also_under` accepts transcripts whose cwd still has another path (useful mid-migration).
        """
        prefix = project_dir_name(full(root)).lower()
        roots = (root, *also_under)
        found = []
        for name in sorted(list_dir(self.projects)):
            low = name.lower()
            if (low != prefix and not low.startswith(prefix + "-")) or not os.path.isdir(self.project_path(name)):
                continue
            cwd = read_session_cwd(self.project_path(name))
            if cwd is None:
                if low == prefix:  # without a readable cwd, only an exact name is trusted
                    found.append(name)
            elif any(is_under(cwd, r) for r in roots):
                found.append(name)
        return found

    def planned_moves(self, src, dst):
        """Pairs (current folder, new folder) of sessions. Aborts if something would prevent moving them."""
        src_name, dst_name = project_dir_name(full(src)), project_dir_name(full(dst))
        moves = []
        for old in self.session_dirs(src):
            new = dst_name + old[len(src_name):]  # keeps the suffix of the subfolders
            new = match_case(new[0], old[0]) + new[1:]
            if len(new) > MAX_DIR_NAME:
                raise Abort(f"The session folder '{new}' exceeds {MAX_DIR_NAME} characters and Claude Code "
                            "would truncate it with an undocumented hash. Choose a shorter destination path.")
            if old.lower() == new.lower():
                raise Abort("The source and the destination produce the same session folder.")
            clashes = _clashing_files(self.project_path(old), self.project_path(new))
            if clashes:
                raise Abort(f"The session folder '{new}' already exists and shares files with '{old}' "
                            f"({clashes[:5]}). They must be merged by hand.")
            moves.append((old, new))
        return moves

    # ------------------------------------------------------ open sessions and windows

    def ensure_closed(self, *roots):
        """Aborts if a Claude Code session is open inside any of the paths."""
        open_sessions = [s for root in roots for s in self._live_sessions(root)]
        if open_sessions:
            raise Abort("There are open Claude Code sessions in the project; close them first: "
                        + "; ".join(open_sessions))

    def ensure_no_editor_window(self, root):
        """Aborts if a VS Code window has the project open: when closed it would save its state
        in the old folder, and the state copied to the new folder would be out of date."""
        windows = self._open_editor_windows(root)
        if windows:
            raise Abort("First close the VS Code window of this project (the others can stay "
                        "open): " + "; ".join(windows))

    def _live_sessions(self, root):
        """Sessions registered in ~/.claude/sessions whose process is still alive and whose cwd is in `root`."""
        found = []
        for data in _read_json_files(self.sessions, ".json"):
            cwd = data.get("cwd")
            if isinstance(cwd, str) and is_under(to_windows(cwd), root) and _pid_alive(data.get("pid")):
                found.append(f"pid {data.get('pid')} in {cwd}")
        return found

    def _open_editor_windows(self, root):
        """Live IDE windows (registered by the Claude extension in ~/.claude/ide/*.lock)
        that have a folder inside `root` open."""
        found = []
        for data in _read_json_files(self.ide_locks, ".lock"):
            folders = data.get("workspaceFolders")
            inside = [f for f in folders if isinstance(f, str) and is_under(f, root)] if isinstance(folders, list) else []
            if inside and _pid_alive(data.get("pid")):
                found.append(f"{data.get('ideName', 'IDE')} with {inside[0]}")
        return found

    # ------------------------------------------------------ ~/.claude.json and history

    def rewrite_config(self, rewriter, dry=False):
        """Renames the keys of `projects` and the paths of `githubRepoPaths`. Returns the changes."""
        if not os.path.isfile(self.config):
            return []
        # Read right before writing: open Claude sessions also write this file.
        with open(self.config, encoding="utf-8") as f:
            data = json.load(f)
        changes = []
        if isinstance(data.get("projects"), dict):
            data["projects"] = _rename_project_keys(data["projects"], rewriter, changes)
        if isinstance(data.get("githubRepoPaths"), dict):
            _rewrite_repo_paths(data["githubRepoPaths"], rewriter, changes)
        if changes and not dry:
            write_text_atomic(self.config, json.dumps(data, indent=2, ensure_ascii=False))
            with open(self.config, encoding="utf-8") as f:
                json.load(f)  # checks that the result is valid JSON
        return changes

    def rewrite_history(self, rewriter, dry=False):
        """Rewrites only the `project` field of the prompt history. Returns the changed entries."""
        if not os.path.isfile(self.history):
            return 0
        with open(self.history, encoding="utf-8", newline="") as f:
            lines = f.readlines()
        changed = 0
        for i, line in enumerate(lines):
            body = line.rstrip("\r\n")
            if not (body and rewriter.might_match(body)):
                continue
            try:
                entry = json.loads(body)
            except ValueError:
                continue
            if isinstance(entry, dict) and isinstance(entry.get("project"), str):
                new_project, n = rewriter.sub(entry["project"])
                if n:
                    entry["project"] = new_project
                    lines[i] = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + line[len(body):]
                    changed += 1
        if changed and not dry:
            write_text_atomic(self.history, "".join(lines))
        return changed

    # ------------------------------------------------------------------------ backup

    def backup(self, dir_names, tag):
        """Copies ~/.claude.json, the history and the given session folders. Returns the folder."""
        dest = os.path.join(self.backups, dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + tag)
        os.makedirs(dest)
        if os.path.isfile(self.config):
            shutil.copy2(self.config, os.path.join(dest, "claude.json"))
        if os.path.isfile(self.history):
            shutil.copy2(self.history, os.path.join(dest, "history.jsonl"))
        for name in dir_names:
            shutil.copytree(self.project_path(name), os.path.join(dest, "projects", name))
        return dest


# ------------------------------------------------------------- reassociation and checks

def relink(state, src, dst):
    """Reassociates to `dst` everything Claude Code has registered for `src`. It is idempotent."""
    rewriter = PathRewriter(src, dst)
    moves = state.planned_moves(src, dst)
    before = {old: count_transcripts(state.project_path(old)) for old, _ in moves}
    for old, new in moves:
        if os.path.exists(state.project_path(new)):
            _merge_move(state.project_path(old), state.project_path(new))
        else:
            os.rename(state.project_path(old), state.project_path(new))
        after = count_transcripts(state.project_path(new))
        if after < before[old]:
            raise Abort(f"After moving '{old}' -> '{new}' there are {after} transcripts and before there were {before[old]}.")

    targets = sorted({new for _, new in moves} | set(state.session_dirs(dst, also_under=(src,))))
    rewritten = {name: rewrite_dir(state.project_path(name), rewriter) for name in targets}
    if os.path.isdir(state.usage_data):  # per-session metadata of some Claude Code versions
        rewritten["../usage-data"] = rewrite_dir(state.usage_data, rewriter)
    return {
        "moved_dirs": [{"from": old, "to": new, "transcripts": before[old]} for old, new in moves],
        "rewritten_dirs": rewritten,
        "config_changes": state.rewrite_config(rewriter),
        "history_entries": state.rewrite_history(rewriter),
    }


def link_problems(state, src, dst):
    """What is missing for everything to point to `dst` (empty list = reassociation complete)."""
    rewriter = PathRewriter(src, dst)
    problems = []
    leftover_dirs = state.session_dirs(src)
    if leftover_dirs:
        problems.append(f"session folders of the old path still exist: {leftover_dirs}")
    for name in state.session_dirs(dst, also_under=(src,)):
        n = rewrite_dir(state.project_path(name), rewriter, dry=True)["replacements"]
        if n:
            problems.append(f"{n} mentions of the old path in projects/{name}")
    config_changes = state.rewrite_config(rewriter, dry=True)
    if config_changes:
        problems.append(f"~/.claude.json still has the old path ({len(config_changes)} pending changes)")
    history_entries = state.rewrite_history(rewriter, dry=True)
    if history_entries:
        problems.append(f"history.jsonl has {history_entries} entries with the old path")
    return problems


# ------------------------------------------------------------------------- helpers

def read_session_cwd(session_dir):
    """First `cwd` that appears in the transcripts of a session folder (or None)."""
    for name in sorted(list_dir(session_dir)):
        if not name.endswith(".jsonl"):
            continue
        try:
            with open(os.path.join(session_dir, name), encoding="utf-8", errors="replace") as f:
                for line in f:
                    if '"cwd"' not in line:
                        continue
                    try:
                        cwd = json.loads(line).get("cwd")
                    except (ValueError, AttributeError):
                        continue
                    if isinstance(cwd, str) and cwd:
                        return to_windows(cwd)
        except OSError:
            continue
    return None


def _read_json_files(folder, extension):
    """Contents of the JSON files (objects) in a folder; ignores those that cannot be read."""
    for name in list_dir(folder):
        if not name.endswith(extension):
            continue
        try:
            with open(os.path.join(folder, name), encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            yield data


def count_transcripts(session_dir):
    """Number of sessions (top-level .jsonl files) in a session folder."""
    return sum(1 for n in list_dir(session_dir) if n.endswith(".jsonl"))


def _pid_alive(pid):
    """True if the process exists (Windows)."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ctypes.get_last_error() == 5  # access denied: it exists but belongs to another user
    try:
        code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        return code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _clashing_files(old_dir, new_dir):
    """Files of `old_dir` that already exist in `new_dir` (if `new_dir` exists)."""
    if not os.path.exists(new_dir):
        return []
    clashes = []
    for root, _, names in os.walk(old_dir):
        for n in names:
            rel = os.path.relpath(os.path.join(root, n), old_dir)
            if os.path.exists(os.path.join(new_dir, rel)):
                clashes.append(rel)
    return clashes


def _merge_move(old_dir, new_dir):
    """Moves the files of `old_dir` into `new_dir` (no clashes: already checked) and deletes `old_dir`."""
    for root, _, names in os.walk(old_dir):
        for n in names:
            src = os.path.join(root, n)
            dst = os.path.join(new_dir, os.path.relpath(src, old_dir))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.rename(src, dst)
    for root, _, _ in os.walk(old_dir, topdown=False):
        os.rmdir(root)


def _rename_project_keys(projects, rewriter, changes):
    """Copy of `projects` with the keys renamed; if the new key already existed, merges both."""
    renamed = {}
    for key, entry in projects.items():
        new_key, n = rewriter.sub(key)
        if n:
            entry = _rewrite_strings(entry, rewriter)
            changes.append(f"projects: {key} -> {new_key}")
        if new_key in renamed:
            renamed[new_key] = _merge_project_entries(renamed[new_key], entry)
            changes.append(f"projects: merged with the existing entry {new_key}")
        else:
            renamed[new_key] = entry
    return renamed


def _rewrite_repo_paths(repo_paths, rewriter, changes):
    """Replaces the path in the lists of `githubRepoPaths` (repo -> local paths)."""
    for repo, paths in repo_paths.items():
        if not isinstance(paths, list):
            continue
        new_paths = []
        for p in paths:
            new_p, n = rewriter.sub(p) if isinstance(p, str) else (p, 0)
            if n:
                changes.append(f"githubRepoPaths[{repo}]: {p} -> {new_p}")
            if new_p not in new_paths:
                new_paths.append(new_p)
        repo_paths[repo] = new_paths


def _merge_project_entries(kept, other):
    """Merges two project entries: keeps `kept` and adds permissions and flags from `other`."""
    merged = dict(kept)
    for key, value in other.items():
        if key not in merged:
            merged[key] = value
        elif key == "allowedTools" and isinstance(merged[key], list) and isinstance(value, list):
            merged[key] = merged[key] + [x for x in value if x not in merged[key]]
        elif isinstance(merged[key], bool) and isinstance(value, bool):
            merged[key] = merged[key] or value
    return merged


def _rewrite_strings(value, rewriter):
    """Applies the replacement to all texts of a JSON structure."""
    if isinstance(value, str):
        return rewriter.sub(value)[0]
    if isinstance(value, list):
        return [_rewrite_strings(x, rewriter) for x in value]
    if isinstance(value, dict):
        return {k: _rewrite_strings(v, rewriter) for k, v in value.items()}
    return value
