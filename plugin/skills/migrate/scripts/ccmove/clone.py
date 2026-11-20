"""Clone: copies a project to a new folder and leaves the original, its sessions and its editor state untouched.

The copy is verified by hash like a migration, but nothing of Claude Code or VS Code changes: the copy starts without
sessions, the original is not archived, and the copy's git remotes are only listed (the user removes them in phase 2).
"""

import os

from . import finalize, log, project_info
from .migrate import Migration
from .rewriter import PathRewriter


class Clone(Migration):
    """Copies `src` to `dst`, verifies the copy and leaves a clone note; nothing outside `dst` changes."""

    DONE = "Clone complete (phase 1). The original was not touched."
    UNTOUCHED = "The original, its Claude Code sessions and its VS Code state are not touched."

    def preview(self):
        """--dry-run: validates, scans and says what the clone would do, without modifying anything."""
        self._prepare()
        self._show_dependency_sizes()
        log("\n[dry-run] What it would do:")
        deps = ("with the dependency folders" if self.with_deps else
                f"without {len(self.tree.deps)} dependency folders (recreated in phase 2)") if self.tree.deps else ""
        log(f"  copy {len(self.tree.files)} files to {self.dst} {deps}".rstrip() + " and verify them by SHA-256")
        self._preview_git()
        log(f"  write the clone note in {os.path.join(self.dst, finalize.NOTE_FILE)}")
        warning = finalize.agents_md_warning(self.src)
        if warning:
            log(f"  WARNING: {warning}")
        log(f"  {self.UNTOUCHED}")
        log("[dry-run] Nothing was modified.")

    def _preview_git(self):
        """Dry-run lines for the git remotes the user will remove in phase 2 (read from the original)."""
        if os.path.isfile(os.path.join(self.src, ".git")):
            log(f"  WARNING: {project_info.SHARED_REPO_WARNING}")
            return
        remotes, error = project_info.list_remotes(self.src)
        if error:
            log(f"  WARNING: {error}")
        if remotes:
            log("  remotes to remove (phase 2, by you): " + ", ".join(_describe(r) for r in remotes))

    def run(self):
        """Runs phase 1 of the clone: copy, verify, list the remotes and leave the note."""
        self._prepare()
        self._check_disk_space()
        # Three heavy stages go over every byte: hash the source, copy, hash the destination.
        self._execute(3 * self.tree.bytes, [
            self._record_venvs,
            self._hash_source,
            self._copy,
            self._verify_copy,
            self._list_remotes,
            self._leave_note,
        ])

    def _ensure_project_closed(self):
        """Nothing to check: nothing of the original is renamed or relinked, and a file that changes during the copy
        is caught by the hash check."""

    def _plan_sessions(self):
        """A clone moves no sessions; the path rewriter is built after the copy exists (see _leave_note)."""
        self.moves = []
        self.rewriter = None
        log(f"Source:      {self.src}\nDestination: {self.dst}")

    def _open_report(self):
        """Opens the report like a migration, but a clone has no previous move: it starts a history of its own."""
        super()._open_report()
        self.chain = []
        del self.record["previous"]

    def _record_fields(self):
        """Fields of the registry record of a clone: no archive and no deadline; phase 2 pending."""
        return {
            "kind": "clone",
            "archive": None,
            "delete_after": None,
            "with_deps": self.with_deps,
            "dep_dirs": [rel for rel, _ in self.tree.deps if not self.dep_builds[rel]],
            "build_dep_dirs": [rel for rel, _ in self.tree.deps if self.dep_builds[rel]],
            "remotes": [],
            "phase2": "pending",
        }

    def _report_details(self):
        """What the report needs for phase 2, plus the copy's git remotes."""
        return {**super()._report_details(), "remotes": []}

    def _final_status(self):
        """There is no archive step that could stay pending."""
        return "ok"

    def _list_remotes(self):
        """Reads the copy's git remotes (read-only): the user removes them in phase 2 with the commands in the
        report, so a push from the copy does not reach the original's repository."""
        if os.path.isfile(os.path.join(self.dst, ".git")):
            self.details["warnings"].append(project_info.SHARED_REPO_WARNING)
            return
        remotes, error = project_info.list_remotes(self.dst)
        if error:
            self.details["warnings"].append(f"Could not list the git remotes of the copy ({error}); check them "
                                            "in phase 2 with: git remote -v")
        self.record["remotes"] = self.details["remotes"] = remotes

    def _leave_note(self):
        """Looks for old paths in the copy and replaces the inherited cc-move section with the clone note."""
        # Built only now: it looks at the siblings of the source, and the copy may be one ("Proj copy").
        self.rewriter = PathRewriter(self.src, self.dst)
        self.details["stale_refs"] = project_info.stale_refs(self.dst, self.rewriter)
        finalize.write_note(self.dst, self.record, keep_earlier=False)
        self.details["git_exclude"] = finalize.ignore_note_in_git(self.dst)
        warning = finalize.agents_md_warning(self.dst)
        if warning:
            self.details["warnings"].append(warning)


def _describe(remote):
    """'origin (url)', with the push url too when it differs."""
    push = f", push {remote['push_url']}" if remote.get("push_url") else ""
    return f"{remote['name']} ({remote['url']}{push})"
