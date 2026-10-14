"""Backups of Claude Code's state taken before each migration: one zip each, kept 30 days, then pruned.

A backup is the last resort if rewriting the sessions or ~/.claude.json went wrong in a way `verify` cannot see;
`undo` does not need it. Its value drops fast once the user has reopened the project, so it expires.
Backups made by cc-move 0.1.x and 0.2.x are folders (registry key `backup_dir`): `prune` zips them or deletes them.
"""

import datetime as dt
import os
import shutil
import zipfile

from . import log
from .paths import list_dir, long_path, unique_path

BACKUP_DAYS = 30


def write_zip(state, dir_names, tag):
    """Zips ~/.claude.json, the history and the given session folders. Returns the path of the zip."""
    os.makedirs(state.backups, exist_ok=True)
    target = unique_path(os.path.join(state.backups, dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + tag), ".zip")

    def fill(z):
        if os.path.isfile(state.config):
            z.write(state.config, "claude.json")
        if os.path.isfile(state.history):
            z.write(state.history, "history.jsonl")
        for name in dir_names:
            _add_tree(z, state.project_path(name), f"projects/{name}/")

    _write_zip_atomic(target, fill)
    return target


def backup_of(record):
    """Path of the backup of a migration (zip, or folder for older versions); None if there is none."""
    return record.get("backup") or record.get("backup_dir")


def expiry(record):
    """Date from which the backup of a migration can be deleted."""
    if record.get("backup_expires"):
        return dt.date.fromisoformat(record["backup_expires"])
    return dt.date.fromisoformat(record["date"]) + dt.timedelta(days=BACKUP_DAYS)


def size_of(path):
    """Size in bytes of a backup (zip or folder)."""
    if os.path.isfile(path):
        return os.path.getsize(path)
    return sum(os.path.getsize(os.path.join(root, n)) for root, _, names in os.walk(long_path(path)) for n in names)


def prune(registry, today, older_than=None, dry=False):
    """Deletes expired backups (or those older than `older_than` days) and zips the remaining folder backups.

    Returns the actions: {"action": "delete" | "zip", "id", "path", "bytes", "new_bytes"}.
    """
    actions = []
    for record in registry.all():
        path = backup_of(record)
        if not path or not os.path.exists(path):
            continue
        age = (today - dt.date.fromisoformat(record["date"])).days
        delete = today >= expiry(record) or (older_than is not None and age >= older_than)
        if not delete and not os.path.isdir(path):
            continue
        action = {"action": "delete" if delete else "zip", "id": record["id"], "path": path, "bytes": size_of(path)}
        if not dry:
            try:
                _apply(record, action, today)
                registry.save(record)
            except OSError as e:  # e.g. a file in use: report it and go on with the others
                action["error"] = str(e)
        actions.append(action)
    return actions


def _apply(record, action, today):
    """Deletes or zips the backup of one record and updates the record (not yet saved)."""
    path = action["path"]
    if action["action"] == "delete":
        _remove(path)
        record.update(backup=None, backup_pruned=today.isoformat())
    else:
        record.update(backup=_zip_folder(path), backup_expires=expiry(record).isoformat())
        action["new_bytes"] = size_of(record["backup"])
    record.pop("backup_dir", None)


def report(actions, dry):
    """Prints what `prune` did (or would do)."""
    for a in actions:
        mb = a["bytes"] / 1e6
        if a.get("error"):
            log(f"  could not {a['action']} {a['path']}: {a['error']}")
        elif a["action"] == "delete":
            log(f"  {'would delete' if dry else 'deleted'} {a['path']} ({mb:.0f} MB)")
        elif dry:
            log(f"  would zip {a['path']} ({mb:.0f} MB)")
        else:
            log(f"  zipped {a['path']} ({mb:.0f} MB -> {a['new_bytes'] / 1e6:.0f} MB)")
    freed = sum(a["bytes"] - a.get("new_bytes", 0) for a in actions if not a.get("error"))
    if actions and not dry:
        log(f"  freed {freed / 1e6:.0f} MB")


def unreferenced(state, registry):
    """Items in the backups folder that no migration refers to (left alone: cc-move does not know them)."""
    known = {os.path.normcase(backup_of(r)) for r in registry.all() if backup_of(r)}
    return [os.path.join(state.backups, n) for n in sorted(list_dir(state.backups))
            if os.path.normcase(os.path.join(state.backups, n)) not in known and not n.endswith(".tmp")]


def _zip_folder(folder):
    """Zips a folder backup of an older version, checks the zip and deletes the folder. Returns the zip."""
    target = unique_path(folder, ".zip")
    _write_zip_atomic(target, lambda z: _add_tree(z, folder, ""))
    files = sum(len(names) for _, _, names in os.walk(long_path(folder)))
    with zipfile.ZipFile(target) as z:
        if z.testzip() is not None or len(z.namelist()) != files:
            os.remove(target)
            raise OSError(f"the zip of {folder} does not match the folder; the folder was kept")
    _remove(folder)
    return target


def _write_zip_atomic(target, fill):
    """Writes a zip with `fill(zipfile)` to a temporary name and renames it: never leaves half a zip."""
    tmp = target + ".tmp"
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            fill(z)
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _add_tree(z, folder, prefix):
    """Adds every file under `folder` to the zip as `prefix` + its relative path (long paths accepted)."""
    root = long_path(folder)
    for current, _, names in os.walk(root):
        for name in names:
            path = os.path.join(current, name)
            z.write(path, prefix + os.path.relpath(path, root).replace("\\", "/"))


def _remove(path):
    """Deletes a backup, zip or folder."""
    if os.path.isdir(path):
        shutil.rmtree(long_path(path))
    else:
        os.remove(path)
