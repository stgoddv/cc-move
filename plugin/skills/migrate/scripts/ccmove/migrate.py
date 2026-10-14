"""Phase 1 of a migration: validate, copy, verify, reassociate sessions, archive and leave the note."""

import calendar
import datetime as dt
import os
import shutil

from . import Abort, backups, copying, editor_state, finalize, log, progress, project_info
from .claude_state import count_transcripts, link_problems, relink
from .paths import full, is_under, unique_path
from .report import write_report
from .rewriter import PathRewriter, rewrite_dir

ARCHIVE_MONTHS = 6


class Migration:
    """Migrates a project from `src` to `dst`. `preview()` shows the plan; `run()` executes it."""

    DONE = "Phase 1 complete."

    def __init__(self, state, registry, state_dir, src, dst, with_deps=False,
                 progress_every=progress.DEFAULT_INTERVAL):
        self.state = state
        self.with_deps = with_deps
        self.progress_every = progress_every
        self.registry = registry
        self.state_dir = state_dir
        self.src, self.dst = full(src), full(dst)
        self.today = dt.date.today()
        self.date = self.today.isoformat()

    # ------------------------------------------------------------------- commands

    def preview(self):
        """--dry-run: validates, scans and counts what would change, without modifying anything."""
        self._prepare()
        self._show_dependency_sizes()
        log("\n[dry-run] What it would do:")
        self._preview_claude_changes()
        log(f"  archive the original as {finalize.archive_path(self.src, self.date)}")
        log(f"  write the note in {os.path.join(self.dst, finalize.NOTE_FILE)}")
        warning = finalize.agents_md_warning(self.src)
        if warning:
            log(f"  WARNING: {warning}")
        log("[dry-run] Nothing was modified.")

    def run(self):
        """Runs phase 1. Nothing of Claude Code is touched until the copy is verified."""
        self._prepare()
        self._check_disk_space()
        # Three heavy stages go over every byte: hash the source, copy, hash the destination.
        self._execute(3 * self.tree.bytes, [
            self._record_venvs,
            self._hash_source,
            self._copy,
            self._verify_copy,
            self._relink_sessions,
            self._restore_editor_state,
            self._archive_original,
            self._leave_note,
        ])

    def _execute(self, total_bytes, steps):
        """Opens the report, runs the steps in order and always saves the report and the registry."""
        self._open_report()
        progress_file = os.path.join(self.report_dir, "progress.txt")
        self.progress = progress.Progress(total_bytes, self.progress_every, progress_file)
        log(f"Progress (a line at most every {self.progress_every:g} s): {progress_file}")
        try:
            for step in steps:
                step()
            self.record["phase1"] = self._final_status()
            self.progress.finish("phase 1 finished")
        except Exception as e:
            self.record["phase1"] = "failed"
            self.record["error"] = str(e) if isinstance(e, Abort) else f"{type(e).__name__}: {e}"
            self.progress.finish(f"phase 1 FAILED: {self.record['error']}")
            raise
        finally:
            write_report(self.report_dir, self.record, self.details)
            self.registry.save(self.record)
        log(f"\n{self.DONE} Report: {os.path.join(self.report_dir, 'report.md')}")
        for warning in self.details["warnings"]:
            log(f"WARNING: {warning}")

    def _final_status(self):
        """Phase 1 status once every step has run."""
        return "ok" if self.record["archive"] else "ok (archive pending)"

    def _preview_claude_changes(self):
        """Dry-run lines for the session folders, ~/.claude.json, the history and the VS Code state."""
        for name in self.state.session_dirs(self.src):  # also those that keep their name (case-only rename)
            r = rewrite_dir(self.state.project_path(name), self.rewriter, dry=True)
            log(f"  rewrite {r['replacements']} mentions of the path in {r['files']} files of projects/{name}")
        for change in self.state.rewrite_config(self.rewriter, dry=True):
            log(f"  ~/.claude.json  {change}")
        log(f"  history.jsonl: {self.state.rewrite_history(self.rewriter, dry=True)} entries")
        for item in editor_state.saved_states(self.src):
            titles = ", ".join(f"\"{t['title']}\"" for t in item["claude_tabs"]) or "none"
            log(f"  copy the {item['editor']} state to the new folder (Claude tabs: {titles})")

    # ---------------------------------------------------------------------- steps

    def _prepare(self):
        """Validates, plans the session move and scans the source."""
        self._validate()
        self._plan_sessions()
        self.git = project_info.git_info(self.src)  # before the scan: afterwards the source must not change
        log("Scanning the source...")
        heartbeat = progress.Heartbeat("scanning the source", self.progress_every)
        self.tree = copying.scan(self.src, self.with_deps, heartbeat)
        internal = sum(1 for link in self.tree.links if link[3].startswith(copying.ROOT))
        log(f"  {len(self.tree.files)} files, {self.tree.bytes / 1e9:.2f} GB, {len(self.tree.dirs)} folders, "
            f"{len(self.tree.links)} links ({internal} point inside the project)")
        if self.tree.deps:
            action = "copied" if self.with_deps else "not copied (--with-deps to copy them)"
            log(f"  dependencies, {action}: " + ", ".join(f"{rel} ({kind})" for rel, kind in self.tree.deps))

    def _plan_sessions(self):
        """Plans which session folders move and shows the paths and the sessions."""
        self.moves = self.state.planned_moves(self.src, self.dst)
        self.rewriter = PathRewriter(self.src, self.dst)
        log(f"Source:      {self.src}\nDestination: {self.dst}")
        for old, new in self.moves:
            transcripts = count_transcripts(self.state.project_path(old))
            log(f"Sessions: projects/{old} ({transcripts} transcripts) -> projects/{new}")
        if not self.moves:
            log("Sessions: no session folder to move (none, or they already have the new name).")
        history = sum(copying.dir_stats(self.state.project_path(d))[1] for d in self.state.session_dirs(self.src))
        log(f"Session history: {history / 1e6:.0f} MB to back up and rewrite")

    def _show_dependency_sizes(self):
        """Size of each dependency folder, to decide whether --with-deps is worth it."""
        if not self.tree.deps:
            return
        log("\nDependencies:")
        total_files = total_bytes = 0
        for rel, kind in self.tree.deps:
            files, size = copying.dir_stats(os.path.join(self.src, rel.replace("/", "\\")))
            total_files, total_bytes = total_files + files, total_bytes + size
            log(f"  {rel} ({kind}): {files} files, {size / 1e9:.2f} GB")
        log(f"  total: {total_files} files, {total_bytes / 1e9:.2f} GB")

    def _validate(self):
        """Checks that source and destination are usable and that no Claude sessions are open in the project."""
        src, dst = self.src, self.dst
        self._validate_source()
        if os.path.exists(dst) and (not os.path.isdir(dst) or os.listdir(dst)):
            raise Abort(f"The destination already exists and is not an empty folder: {dst}")
        if not os.path.isdir(os.path.dirname(dst)):
            raise Abort(f"The parent folder of the destination does not exist: {os.path.dirname(dst)}")
        if is_under(dst, src) or is_under(src, dst):
            raise Abort("The source and the destination cannot be inside each other.")
        self._ensure_project_closed()

    def _validate_source(self):
        """Aborts if the source is not a real, non-archived folder."""
        src = self.src
        if not os.path.isdir(src):
            raise Abort(f"The source does not exist or is not a folder: {src}")
        if os.lstat(src).st_file_attributes & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT
            raise Abort(f"The source is a link or junction, not a real folder: {src}")
        if os.path.basename(src).lower().startswith("archive-"):
            raise Abort("The source is already an archived folder.")

    def _ensure_project_closed(self):
        """Aborts if this process, a Claude Code session or a VS Code window is using the source."""
        if is_under(os.getcwd(), self.src):
            raise Abort("The current directory is inside the source; run the script from another folder.")
        self.state.ensure_closed(self.src)
        self.state.ensure_no_editor_window(self.src)

    def _check_disk_space(self):
        """Aborts if the destination disk does not have room for the copy (the original is kept)."""
        free = shutil.disk_usage(os.path.dirname(self.dst)).free
        if free < self.tree.bytes * 1.05 + 1e9:
            raise Abort(f"Not enough space: ~{self.tree.bytes / 1e9:.1f} GB needed "
                        f"and {free / 1e9:.1f} GB free.")

    def _open_report(self):
        """Creates reports/<date>-<name>/ and the migration record."""
        name = os.path.basename(self.src)
        self.report_dir = unique_path(os.path.join(self.state_dir, "reports", f"{self.date}-{name}"))
        os.makedirs(self.report_dir)
        self.record = {
            "id": os.path.basename(self.report_dir),
            "date": self.date,
            "src": self.src,
            "dst": self.dst,
            **self._record_fields(),
            "phase1": "in progress",
            "report_dir": self.report_dir,
        }
        self.details = {**self._report_details(), "warnings": []}

    def _record_fields(self):
        """Fields of the registry record specific to a copy."""
        return {
            "archive": None,
            "delete_after": _add_months(self.today, ARCHIVE_MONTHS).isoformat(),
            "with_deps": self.with_deps,
            "dep_dirs": [rel for rel, _ in self.tree.deps],
            "phase2": "pending",
        }

    def _report_details(self):
        """What the report needs for phase 2."""
        return {
            "git": self.git,
            "with_deps": self.with_deps,
            "deps": self.tree.deps,
            "lockfiles": project_info.lockfiles(self.tree),
        }

    def _record_venvs(self):
        """Saves the Python version and packages of each excluded venv, to recreate it in phase 2."""
        self.details["venvs"] = [project_info.venv_info(self.src, rel, self.report_dir)
                                 for rel in self.tree.venv_dirs()]

    def _hash_source(self):
        """Computes the source manifest and saves it (with the exclusions, for reference)."""
        log("Computing source hashes...")
        lines = copying.manifest(self.tree, copying.hash_files(self.tree, self.progress, "hashing the source"))
        skipped = [] if self.with_deps else [f"X\t{rel}\t{kind}" for rel, kind in self.tree.deps]
        copying.write_lines(os.path.join(self.report_dir, "manifest-src.tsv"), lines + skipped)
        self.src_manifest = lines

    def _copy(self):
        """Copies with robocopy and recreates the links."""
        log("Copying with robocopy...")
        log_path = os.path.join(self.report_dir, "robocopy.log")
        copying.robocopy(self.tree, self.dst, log_path, self.progress)
        created, failed = copying.recreate_links(self.tree, self.dst)
        self.details["links"] = {"created": created, "failed": failed}

    def _verify_copy(self):
        """Compares the destination manifest with the source one; any difference aborts."""
        log("Verifying the copy...")
        src_manifest = self.src_manifest
        dst_tree = copying.scan(self.dst, self.with_deps, progress.Heartbeat("scanning the copy", self.progress_every))
        dst_manifest = copying.manifest(dst_tree, copying.hash_files(dst_tree, self.progress, "hashing the copy"))
        copying.write_lines(os.path.join(self.report_dir, "manifest-dst.tsv"), dst_manifest)
        failed_links = [link["path"] for link in self.details["links"]["failed"]]
        missing, extra = copying.compare_manifests(src_manifest, dst_manifest, failed_links)
        if missing or extra:
            diff = [f"- {x}" for x in missing] + [f"+ {x}" for x in extra]
            copying.write_lines(os.path.join(self.report_dir, "manifest-diff.txt"), diff)
            raise Abort(f"The copy does NOT match the source: {len(missing)} entries are missing or differ and "
                        f"{len(extra)} are extra (see manifest-diff.txt). Nothing of Claude Code was touched; "
                        f"the copy was left in {self.dst} for inspection.")
        self.record.update(content_sha256=copying.manifest_hash(src_manifest),
                           files=len(self.tree.files), bytes=self.tree.bytes)
        log(f"  OK: {len(self.tree.files)} identical files (hash {self.record['content_sha256'][:16]}...)")

    def _relink_sessions(self):
        """Backs up the Claude Code state and reassociates the sessions to the new path."""
        log("Backing up the Claude Code state...")
        moved = [old for old, _ in self.moves]
        self.record["backup"] = backups.write_zip(self.state, moved, os.path.basename(self.src))
        self.record["backup_expires"] = backups.expiry(self.record).isoformat()
        log("Reassociating the sessions...")
        self.details["relink"] = relink(self.state, self.src, self.dst)
        problems = link_problems(self.state, self.src, self.dst)
        if problems:
            raise Abort("The reassociation is incomplete: " + "; ".join(problems))

    def _restore_editor_state(self):
        """Copies the VS Code state (Claude tabs, open editors) to the new folder.

        It is an internal VS Code format: if something fails it becomes a warning and the migration continues.
        """
        log("Restoring the VS Code state...")
        try:
            results = editor_state.copy_state(self.src, self.dst, self.rewriter, self._stale_editor_state_dir())
        except Exception as e:  # any surprise in the internal format must not undo a verified copy
            results = []
            self.details["warnings"].append(f"Could not restore the editor state: {type(e).__name__}: {e}")
        self.details["editor_state"] = results
        if not results:
            log("  there is no saved editor state for this folder")
        for r in results:
            log(f"  {r['editor']}: {r['status']} ({len(r['claude_tabs'])} Claude tabs)")
            if not r["status"].startswith("copied"):
                self.details["warnings"].append(f"State of {r['editor']}: {r['status']}. The Claude tabs "
                                                "that were open are listed in the report.")

    def _stale_editor_state_dir(self):
        """Where an older state of the new folder may be moved to make room (None: never replace it)."""
        return None

    def _archive_original(self):
        """Renames the original to archive-<date>-<name>; if it is in use, it stays pending."""
        log("Archiving the original...")
        target, error = finalize.archive_original(self.src, self.date)
        self.record["archive"] = target
        if error:
            self.details["warnings"].append(
                f"Could not rename the original ({error}). Close whatever uses it (VS Code, terminals, "
                f"node) and run: python cc_move.py archive \"{self.dst}\"")
            log(f"  WARNING: could not rename the original: {error}")
        else:
            log(f"  {target}")

    def _leave_note(self):
        """Looks for old paths in the project, writes the note in CLAUDE.local.md and hides it from git."""
        # Before writing the note, which mentions the old path on purpose.
        self.details["stale_refs"] = project_info.stale_refs(self.dst, self.rewriter)
        finalize.write_note(self.dst, self.record)
        self.details["git_exclude"] = finalize.ignore_note_in_git(self.dst)
        warning = finalize.agents_md_warning(self.dst)
        if warning:
            self.details["warnings"].append(warning)


def _add_months(date, months):
    """Adds months to a date, adjusting the day if the month is shorter."""
    year, month = divmod(date.month - 1 + months, 12)
    year, month = date.year + year, month + 1
    return dt.date(year, month, min(date.day, calendar.monthrange(year, month)[1]))
