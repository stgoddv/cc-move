"""Case-only rename (Proj -> proj): the folder is renamed in place and Claude Code and VS Code follow it.

Windows sees both names as the same folder, so there is nothing to copy, hash or archive, and nothing inside the
project needs repairing. Claude Code would still find the sessions after a rename by hand, but VS Code would not: the
id of its per-folder state depends on the exact case of the path.
"""

import os

from . import Abort, log
from .finalize import rename_folder
from .migrate import Migration
from .paths import disk_case, full, same_path


def is_case_rename(src, dst):
    """True if Windows sees `src` and `dst` as the same folder (only the case can differ)."""
    return same_path(src, dst)


class CaseRename(Migration):
    """Renames a project folder whose name only changes case, and relinks its sessions and VS Code state."""

    DONE = "Rename complete (a case-only rename has no phase 2)."

    def __init__(self, state, registry, state_dir, src, dst, **options):
        super().__init__(state, registry, state_dir, src, dst, **options)
        # Not full(dst): with 8.3 short names it asks Windows for the long path, which comes back with the case on
        # disk (the old one). The new name is taken as typed; the parent folders keep their case on disk.
        typed = os.path.normpath(os.path.abspath(dst))
        self.src = disk_case(src)
        self.dst = os.path.join(os.path.dirname(self.src), os.path.basename(typed))
        self.typed_parent = full(os.path.dirname(typed))

    def preview(self):
        """--dry-run: says what the rename would change, without modifying anything."""
        self._prepare()
        log(f"\nCase-only rename in place: {os.path.basename(self.src)} -> {os.path.basename(self.dst)}. "
            "Windows sees both names as the same folder: nothing is copied, hashed or archived.")
        log("\n[dry-run] What it would do:")
        log(f"  rename the folder in place: {self.src} -> {self.dst}")
        for old, new in self.moves:
            log(f"  rename projects/{old} -> projects/{new}")
        self._preview_claude_changes()
        log("  no phase 2 and no CLAUDE.local.md note: nothing inside the project changes")
        log("[dry-run] Nothing was modified.")

    def run(self):
        """Renames the folder, then relinks the sessions and the VS Code state to the new case."""
        self._prepare()
        self._execute(0, [self._rename_folder, self._relink_sessions, self._restore_editor_state])

    def _prepare(self):
        """Validates and plans the session folders; there is no scan, the content does not move."""
        self._validate()
        self._plan_sessions()

    def _validate(self):
        """Checks that only the case of the project's own name changes and that the project is closed."""
        self._validate_source()
        old, new = os.path.basename(self.src), os.path.basename(self.dst)
        if old == new:
            if self.typed_parent[1:] != os.path.dirname(self.src)[1:]:  # [1:]: the drive letter's case is irrelevant
                raise Abort(f"The folder is already named '{old}': only the case of a parent folder differs. cc-move "
                            "only changes the case of the project folder itself; changing a parent folder would "
                            "affect every project inside it.")
            raise Abort(f"The folder is already named '{old}': there is nothing to rename.")
        self._ensure_project_closed()

    def _rename_folder(self):
        """Renames the folder in place; if Windows does not allow it, nothing of Claude Code is touched."""
        log("Renaming the folder...")
        error = rename_folder(self.src, self.dst)
        if error:
            raise Abort(f"Could not rename the folder ({error}). Close whatever uses it (VS Code, terminals, node) "
                        "and try again. Nothing of Claude Code was touched.")
        log(f"  {self.dst}")

    def _stale_editor_state_dir(self):
        """After an undo, redoing the rename finds the VS Code state of the first one; it is kept in the report."""
        return os.path.join(self.report_dir, "replaced-editor-state")

    def _record_fields(self):
        """Fields of the registry record of a rename: no archive, no deadline, no phase 2."""
        return {"kind": "case-rename", "archive": None, "delete_after": None, "with_deps": False, "phase2": "n/a"}

    def _report_details(self):
        """A rename has no dependencies, links or git state to review."""
        return {}

    def _final_status(self):
        """There is no archive step that could stay pending."""
        return "ok"
