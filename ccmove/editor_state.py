"""State that VS Code (and its derivatives) stores per folder: Claude tabs, open editors, extensions.

VS Code stores the state of each folder in %APPDATA%\\<editor>\\User\\workspaceStorage\\<id>\\, where
<id> = md5(path + creation date of the folder). When the folder is moved the <id> changes and VS Code starts from
scratch: for example, it does not reopen the Claude Code tabs. This module copies that state to the new folder.
It is an internal VS Code format, so everything here is best-effort: the old state is never modified.
"""

import hashlib
import json
import os
import pathlib
import shutil
import sqlite3
from urllib.parse import unquote

from .paths import list_dir, same_path, strip_long_prefix
from .rewriter import rewrite_dir, uri_quote

# Editors with the same workspaceStorage format; only the installed ones are used.
EDITORS = ["Code", "Code - Insiders", "Cursor", "VSCodium", "Windsurf"]
CLAUDE_EXTENSION_KEY = "Anthropic.claude-code"


def storage_roots():
    """(editor, workspaceStorage folder) of each installed editor."""
    appdata = os.environ.get("APPDATA", "")
    roots = []
    for editor in EDITORS:
        path = os.path.join(appdata, editor, "User", "workspaceStorage")
        if os.path.isdir(path):
            roots.append((editor, path))
    return roots


def vscode_path(path):
    """Path as VS Code sees it (fsPath): real case from the disk and the drive letter in lower case."""
    real = strip_long_prefix(os.path.realpath(path))
    return real[0].lower() + real[1:]


def folder_uri(path):
    """Folder URI as VS Code writes it: file:///c%3A/Users/a%20b."""
    p = vscode_path(path)
    parts = [x for x in p[3:].split("\\") if x]
    return "file:///" + p[0] + "%3A/" + "/".join(uri_quote(x) for x in parts)


def workspace_id(path):
    """workspaceStorage <id> of a folder: md5(path + creation date in milliseconds)."""
    st = os.stat(path)
    birth_ns = getattr(st, "st_birthtime_ns", None) or st.st_ctime_ns  # on Windows, ctime is the creation time
    return hashlib.md5((vscode_path(path) + str(birth_ns // 1_000_000)).encode("utf-8")).hexdigest()


def find_storage(root, path):
    """State folder of `path` inside a workspaceStorage; if there are several, the most recently used."""
    candidates = []
    for name in list_dir(root):
        storage = os.path.join(root, name)
        folder = _workspace_folder(storage)
        if folder and same_path(folder, path):
            candidates.append(storage)
    return max(candidates, key=_last_used, default=None)


def claude_tabs(storage):
    """Claude Code tabs that were open: [{'sessionId', 'title'}]."""
    value = _read_item(storage, CLAUDE_EXTENSION_KEY)
    try:
        tabs = json.loads(value).get("panelTabSessions", []) if value else []
    except (ValueError, AttributeError):
        return []
    return [{"sessionId": t.get("sessionId"), "title": t.get("title")} for t in tabs if isinstance(t, dict)]


def saved_states(src):
    """Per editor: the state folder of `src` and its Claude tabs (for the dry-run and the report)."""
    found = []
    for editor, root in storage_roots():
        storage = find_storage(root, src)
        if storage:
            found.append({"editor": editor, "storage": storage, "claude_tabs": claude_tabs(storage)})
    return found


def copy_state(src, dst, rewriter):
    """Copies the state of each editor from `src` to the folder VS Code will use for `dst`.

    It does not overwrite a new state that already exists (it means `dst` was already opened in the editor).
    Returns one result per editor with `status`: copied, skipped: … or failed: ….
    """
    new_id = workspace_id(dst)
    results = []
    for item in saved_states(src):
        target = os.path.join(os.path.dirname(item["storage"]), new_id)
        result = dict(item, target=target)
        if os.path.exists(target):
            result["status"] = "skipped: the new folder was already opened in this editor and has its own state"
        else:
            try:
                _copy_storage(item["storage"], target, dst, rewriter)
                result["status"] = "copied"
            except (OSError, sqlite3.Error, ValueError) as e:
                shutil.rmtree(target, ignore_errors=True)
                result["status"] = f"failed: {e}"
        results.append(result)
    return results


def _copy_storage(old, new, dst, rewriter):
    """Copies a state folder, points it to `dst` and rewrites the old path in its contents."""
    shutil.copytree(old, new, ignore=shutil.ignore_patterns("state.vscdb.backup", "*-journal"))
    with open(os.path.join(new, "workspace.json"), "w", encoding="utf-8") as f:
        json.dump({"folder": folder_uri(dst)}, f)
    _rewrite_database(os.path.join(new, "state.vscdb"), rewriter)
    rewrite_dir(new, rewriter)  # chatSessions/*.jsonl and other extension text files


def _rewrite_database(db, rewriter):
    """Rewrites the old path in all values of the ItemTable table of a state.vscdb."""
    if not os.path.isfile(db):
        return
    con = sqlite3.connect(db)
    try:
        for key, value in con.execute("select key, value from ItemTable").fetchall():
            is_text = isinstance(value, str)
            text = value if is_text else bytes(value).decode("utf-8", "surrogateescape")
            new, n = rewriter.sub(text)
            if n:
                stored = new if is_text else new.encode("utf-8", "surrogateescape")
                con.execute("update ItemTable set value = ? where key = ?", (stored, key))
        con.commit()
    finally:
        con.close()


def _read_item(storage, key):
    """Value (text) of a key of state.vscdb, opened read-only; None if it does not exist."""
    db = os.path.join(storage, "state.vscdb")
    if not os.path.isfile(db):
        return None
    con = sqlite3.connect(pathlib.Path(db).as_uri() + "?mode=ro", uri=True)
    try:
        row = con.execute("select value from ItemTable where key = ?", (key,)).fetchone()
    finally:
        con.close()
    if not row:
        return None
    return row[0] if isinstance(row[0], str) else bytes(row[0]).decode("utf-8", "replace")


def _workspace_folder(storage):
    """Path of the folder a state belongs to; None if it is a multi-folder workspace or cannot be read."""
    try:
        with open(os.path.join(storage, "workspace.json"), encoding="utf-8") as f:
            uri = json.load(f).get("folder")
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(uri, str) or not uri.startswith("file:///"):
        return None
    return unquote(uri[len("file:///"):]).replace("/", "\\")


def _last_used(storage):
    """Date of the last write of state.vscdb (0 if it does not exist)."""
    db = os.path.join(storage, "state.vscdb")
    return os.path.getmtime(db) if os.path.isfile(db) else 0
