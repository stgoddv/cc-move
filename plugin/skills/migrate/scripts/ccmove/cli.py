"""cc-move command line. The full procedure is in CLAUDE.md."""

import argparse
import datetime as dt
import json
import os
import stat
import sys

from . import Abort, backups, log
from .claude_state import ClaudeState, count_transcripts, link_problems, relink, transcript_mentions
from .copying import create_link
from .finalize import (archive_original, archive_path, existing_archives, is_real_folder, rename_folder,
                       rename_hint, update_note_archive)
from .migrate import Migration
from .paths import disk_case, full, is_under, long_path, same_path
from .rename import CaseRename, is_case_rename
from .progress import DEFAULT_INTERVAL
from .registry import Registry, source_is_copy

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cc_move.py")


def default_state_dir():
    """%LOCALAPPDATA%\\cc-move: survives plugin updates and uninstallation, and can be reached
    without Claude Code (the registry is needed for verify, undo and the 6-month cleanup)."""
    local = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(local, "cc-move")


def main(argv=None):
    """Parses the arguments and runs the command. Returns the exit code."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = _parser().parse_args(argv)
    args.state_dir = full(args.state_dir)
    state = ClaudeState(args.claude_home)
    registry = Registry(args.state_dir)
    try:
        return COMMANDS[args.command](args, state, registry) or 0
    except Abort as e:
        log(f"ERROR: {e}")
        return 2


def cmd_migrate(args, state, registry):
    """Full phase 1 (or only the preview with --dry-run); a case-only rename is done in place."""
    kind = CaseRename if is_case_rename(args.src, args.dst) else Migration
    migration = kind(state, registry, args.state_dir, args.src, args.dst,
                     with_deps=args.with_deps, progress_every=args.progress_every)
    if args.dry_run:
        migration.preview()
    else:
        migration.run()
    _prune_old_backups(registry, dry=args.dry_run)
    if not args.dry_run:
        log("\nTo check it later from a normal terminal:")
        log(f'  python "{SCRIPT}" verify --all')


def _prune_old_backups(registry, dry):
    """After each migration: deletes expired backups and zips the folder backups of older versions."""
    actions = backups.prune(registry, dt.date.today(), dry=dry)
    if actions:
        log(f"\nBackups of earlier migrations (kept {backups.BACKUP_DAYS} days):")
        backups.report(actions, dry)


def cmd_prune(args, state, registry):
    """Deletes expired backups (or those older than --older-than days) and zips the remaining folder backups."""
    actions = backups.prune(registry, dt.date.today(), args.older_than, args.dry_run)
    if not actions:
        log("Nothing to prune: no backup has expired and every backup is already a zip.")
    backups.report(actions, args.dry_run)
    others = backups.unreferenced(state, registry)
    if others:
        log("Not registered by cc-move, left alone:")
        for path in others:
            log(f"  {path}")
    return 1 if any(a.get("error") for a in actions) else 0


def cmd_relink(args, state, registry):
    """Re-applies the session reassociation from `src` to `dst`."""
    src, dst = full(args.src), full(args.dst)
    state.ensure_closed(src, dst)
    log(json.dumps(relink(state, src, dst), indent=2, ensure_ascii=False))
    return _report_problems(link_problems(state, src, dst))


def cmd_verify(args, state, registry):
    """Checks a migration (or all with --all) and repairs the reassociation if needed."""
    if args.all:
        records = [r for r in registry.all() if r["phase1"].startswith("ok")]
    elif args.path:
        records = [registry.find(args.path)]
    else:
        raise Abort("Give the path of a migration or use --all.")
    if not records:
        log("There are no completed migrations.")
    failures = 0
    for record in records:
        if args.all:
            log(f"\n== {record['id']}")
        failures += _verify(state, record, _final_destination(registry, record), source_is_copy(registry.chain(record)))
    return 1 if failures else 0


def _final_destination(registry, record):
    """Where the project of `record` is now: follows the later migrations that started from its destination.

    Verifying against the record's own destination would revert them: after Proj -> proj -> Proj, the first
    rename would turn every 'Proj' back into 'proj'.
    """
    while True:
        later = registry.next_move(record)
        if not later:
            return record["dst"]
        record = later


def _verify(state, record, dst, from_copy=False):
    """Verifies a migration against the project's current path `dst`. Open Claude sessions can undo the change
    in ~/.claude.json when they close, so if it finds the old path it reassociates again.
    `from_copy`: the source was made by an earlier migration, so its archive is a copy that may be deleted.

    It repairs only what Claude Code uses to find the sessions (folders, ~/.claude.json, history), and only when no
    session is open in the project: rewriting a transcript that a session is writing could lose its last lines.
    Later mentions of the old path in transcripts are conversation text: reported, not rewritten.
    """
    src = record["src"]
    if dst != record["dst"]:
        log(f"Later moved or renamed: checking against {dst}")
    if _is_case_rename(record):
        problem = _case_rename_problem(dst)
        if problem:  # relinking now would give the sessions a case the folder no longer has
            return _report_problems([problem])
    problems = link_problems(state, src, dst, mentions=False)
    if problems:
        for p in problems:
            log(f"Detected: {p}")
        try:
            state.ensure_closed(src, dst)
        except Abort as e:
            return _report_problems(problems + [f"not repaired: {e}. Then run verify again."])
        log("Re-applying the reassociation...")
        relink(state, src, dst)
        problems = link_problems(state, src, dst, mentions=False)
    for note in transcript_mentions(state, src, dst):
        log(f"Note: {note} (conversation text written after the migration; left as it is)")
    dirs = state.session_dirs(dst)
    log(f"Session folders of {dst}: " + (", ".join(
        f"{d} ({count_transcripts(state.project_path(d))} transcripts)" for d in dirs) or "none"))
    if not _is_case_rename(record):  # a rename has no archive; its name on disk was checked above
        if not record.get("archive"):
            problems.append("the original has not been archived yet (use the archive command)")
        elif os.path.isdir(record["archive"]):
            pass
        elif from_copy:
            log(f"Note: the archived intermediate copy was deleted ({record['archive']}); "
                "the true original is archived by an earlier migration.")
        else:
            problems.append(f"the archived folder does not exist: {record['archive']}")
    return _report_problems(problems)


def _case_rename_problem(dst):
    """Why the folder of a case-only rename no longer has the name it was given (None if it does)."""
    if not os.path.isdir(dst):
        return f"the folder does not exist: {dst}"
    if disk_case(dst) != dst:
        return (f"the folder is named {os.path.basename(disk_case(dst))} on disk, not {os.path.basename(dst)}; "
                "the sessions were left as they are")
    return None


def cmd_archive(args, state, registry):
    """Retries renaming the original when it was in use before, or records where it was renamed to by hand."""
    record = registry.find(args.path)
    if _is_case_rename(record):
        raise Abort("A case-only rename keeps the folder in place: there is nothing to archive.")
    if record.get("archive") and os.path.isdir(record["archive"]) and not args.folder:
        log(f"Already archived: {record['archive']}")
        return
    if args.folder:
        target = _adopt_archive(record, full(args.folder))
    elif os.path.exists(record["src"]):
        target, error = archive_original(record["src"], record["date"])
        if error:
            raise Abort(f"Could not rename {record['src']}: {error}. {rename_hint(record['src'])}")
    else:
        target = _find_renamed_original(registry, record)
    record["archive"] = target
    if record["phase1"].startswith("ok"):
        record["phase1"] = "ok"
    registry.save(record)
    if os.path.isdir(record["dst"]):
        update_note_archive(record["dst"], record, registry.chain(record))  # it names the archive's final path
    log(f"Archived: {target}")


def _adopt_archive(record, folder):
    """Checks that `folder` (given with --path) can be the archived original and returns it."""
    if os.path.exists(record["src"]):
        raise Abort(f"The original is still at {record['src']}: run archive without --path to rename it.")
    if not is_real_folder(folder):
        raise Abort(f"Not a folder (or it is a link): {folder}")
    if is_under(folder, record["dst"]) or is_under(record["dst"], folder):
        raise Abort(f"{folder} is the migrated copy, not the original.")
    return folder


def _find_renamed_original(registry, record):
    """The archive-<date>-<name> folder the user renamed the original to, when `src` no longer exists."""
    taken = [r["archive"] for r in registry.all() if r.get("archive") and r["id"] != record["id"]]
    found = [p for p in existing_archives(record["src"], record["date"]) if not any(same_path(p, t) for t in taken)]
    if len(found) == 1:
        log(f"The original was already renamed by hand: {found[0]}")
        return found[0]
    if found:
        raise Abort("The original is gone and several folders could be its archive: " + ", ".join(found)
                    + ". Choose one with --path <folder>.")
    name = os.path.basename(archive_path(record["src"], record["date"]))
    raise Abort(f"The original is not at {record['src']} and no {name} folder exists next to it. "
                "If you renamed or moved it, give its path with --path <folder>.")


def cmd_recreate_links(args, state, registry):
    """Creates the links phase 1 could not create, now that their targets exist (after reinstalling dependencies)."""
    record = registry.find(args.path)
    dst = record["dst"]
    if not os.path.isdir(dst):
        raise Abort(f"The migrated folder no longer exists: {dst}")
    with open(os.path.join(record["report_dir"], "report.json"), encoding="utf-8") as f:
        failed = (json.load(f).get("links") or {}).get("failed", [])
    if not failed:
        log("Phase 1 created every link: nothing to do.")
    pending = 0
    for x in failed:
        link = os.path.join(dst, x["path"].replace("/", "\\"))
        if os.path.lexists(long_path(link)):
            is_link = os.lstat(long_path(link)).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
            log(f"Already there: {x['path']}" if is_link else
                f"Left alone: {x['path']} is a real folder now (npm workspaces install one there instead of a link)")
        elif not os.path.exists(x["target"]):
            pending += 1
            log(f"Still pending: {x['path']} (its target {x['target']} does not exist yet; reinstall first)")
        else:
            try:
                create_link(x["kind"], x["target"], link)
                log(f"Created: {x['path']} ({x['kind']} → {x['target']})")
            except OSError as e:
                pending += 1
                log(f"Could not create {x['path']}: {e}")
    return 1 if pending else 0


def cmd_undo(args, state, registry):
    """Returns the original to its name and the sessions to the old path. Does not delete the copy."""
    record = registry.find(args.path)
    if _is_case_rename(record):
        return _undo_case_rename(state, registry, record)
    src, dst = record["src"], record["dst"]
    state.ensure_closed(dst)
    archive = record.get("archive")
    if archive and os.path.isdir(archive) and not os.path.exists(src):
        os.rename(archive, src)
        log(f"Original restored: {src}")
    if not os.path.isdir(src):
        raise Abort(f"Cannot find the original in {src} or in the archive; the reassociation was not undone.")
    result = relink(state, dst, src)
    log(f"Sessions returned to the original path: {[m['to'] for m in result['moved_dirs']]}")
    record["phase1"] = "undone"
    registry.save(record)
    log(f"The copy in {dst} was NOT deleted; delete it by hand if you no longer need it.")
    log("The VS Code state of the original path was never modified: when you open it, everything is as before.")
    if backups.backup_of(record):
        log(f"Backup of the Claude Code state from before the migration: {backups.backup_of(record)}")


def _undo_case_rename(state, registry, record):
    """Gives the folder its old case back and returns the sessions to it."""
    src, dst = record["src"], record["dst"]
    if is_under(os.getcwd(), dst):
        raise Abort("The current directory is inside the project; run the script from another folder.")
    state.ensure_closed(dst)
    state.ensure_no_editor_window(dst)
    if disk_case(dst) == dst:
        error = rename_folder(dst, src)
        if error:
            raise Abort(f"Could not rename {dst} back ({error}); the reassociation was not undone.")
        log(f"Folder renamed back: {src}")
    elif disk_case(dst) != src:
        raise Abort(f"The folder is no longer named {dst} nor {src}; the reassociation was not undone.")
    result = relink(state, dst, src)
    log(f"Sessions returned to the original name: {[m['to'] for m in result['moved_dirs']]}")
    record["phase1"] = "undone"
    registry.save(record)
    log("The VS Code state of the original name was never modified: when you open it, everything is as before.")
    if backups.backup_of(record):
        log(f"Backup of the Claude Code state from before the rename: {backups.backup_of(record)}")


def _is_case_rename(record):
    """True if the registry record is a case-only rename in place (no copy, no archive)."""
    return record.get("kind") == "case-rename"


def cmd_phase2(args, state, registry):
    """Records the phase 2 result in the registry."""
    record = registry.find(args.path)
    record["phase2"] = args.status
    if args.note:
        record["phase2_note"] = args.note
    registry.save(record)
    log(f"{record['id']}: phase 2 = {args.status}")


def cmd_status(args, state, registry):
    """Lists the migrations, their backups and the archives that have reached their deadline."""
    today = dt.date.today().isoformat()
    records = registry.all()
    if not records:
        log("There are no registered migrations.")
    for r in records:
        archived = bool(r.get("archive")) and os.path.isdir(r["archive"])
        if _is_case_rename(r):
            archive_text = "n/a (rename)"
        elif archived:
            archive_text = "yes"
        elif r.get("archive") and source_is_copy(registry.chain(r)):
            archive_text = "deleted copy"  # the archive of an intermediate copy: the true original is further back
        else:
            archive_text = "no"
        line = (f"{r['date']}  {os.path.basename(r['src']):24} phase1={r['phase1']:10} "
                f"phase2={r['phase2']:8} archived={archive_text:12} backup={_backup_text(r)}")
        later = registry.next_move(r)
        if later:
            line += f"  -> moved on: {later['id']}"
        if archived and r.get("delete_after") and today >= r["delete_after"]:
            line += f"  ** can be deleted from {r['delete_after']}: {r['archive']}"
        log(line)


def _backup_text(record):
    """Size and expiry of a migration's backup, or why there is none."""
    path = backups.backup_of(record)
    if path and os.path.exists(path):
        kind = "folder, zipped by prune" if os.path.isdir(path) else "zip"
        return f"{backups.size_of(path) / 1e6:.0f} MB {kind}, until {backups.expiry(record)}"
    return f"deleted {record['backup_pruned']}" if record.get("backup_pruned") else "none"


def _report_problems(problems):
    """Shows the problems found. Returns 1 if there are any, 0 if not."""
    for p in problems:
        log(f"PROBLEM: {p}")
    if not problems:
        log("OK: the sessions point to the new path and no references to the old one remain.")
    return 1 if problems else 0


def _parser():
    """Defines the commands and their arguments."""
    p = argparse.ArgumentParser(prog="cc_move.py", description="Moves a repo without losing its Claude Code sessions.")
    p.add_argument("--claude-home", default=os.path.expanduser("~"),
                   help="folder that contains .claude and .claude.json (for tests)")
    p.add_argument("--state-dir", default=default_state_dir(), help="where to store reports/ and migrations.jsonl")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("migrate", help="full phase 1")
    s.add_argument("src")
    s.add_argument("dst")
    s.add_argument("--dry-run", action="store_true", help="shows what it would do without changing anything")
    s.add_argument("--with-deps", action="store_true",
                   help="also copies node_modules and virtual environments (by default they are recreated in phase 2)")
    s.add_argument("--progress-every", type=float, default=DEFAULT_INTERVAL, metavar="SECONDS",
                   help=f"at most one progress line per this many seconds (default {DEFAULT_INTERVAL})")

    s = sub.add_parser("relink", help="re-applies the session reassociation (idempotent)")
    s.add_argument("src")
    s.add_argument("dst")

    s = sub.add_parser("verify", help="checks (and repairs) the reassociation")
    s.add_argument("path", nargs="?", help="source or destination path of the migration")
    s.add_argument("--all", action="store_true", help="all completed migrations")

    s = sub.add_parser("archive", help="retries renaming the original, or records the folder it was renamed to")
    s.add_argument("path", help="source or destination path of the migration")
    s.add_argument("--path", dest="folder", metavar="FOLDER",
                   help="the original was already renamed or moved by hand to this folder")

    s = sub.add_parser("undo", help="returns the original and the sessions to the old path")
    s.add_argument("path", help="source or destination path of the migration")

    s = sub.add_parser("recreate-links", help="creates the links phase 1 left pending, once their targets exist")
    s.add_argument("path", help="source or destination path of the migration")

    s = sub.add_parser("phase2", help="records the phase 2 result in the registry")
    s.add_argument("path")
    s.add_argument("--status", required=True, choices=["ok", "partial", "failed"])
    s.add_argument("--note", help="one-line summary")

    sub.add_parser("status", help="lists the migrations, their backups and the archives that can already be deleted")

    s = sub.add_parser("prune", help=f"deletes backups older than {backups.BACKUP_DAYS} days and zips older folder backups")
    s.add_argument("--older-than", type=int, metavar="DAYS", help="also deletes backups at least this many days old")
    s.add_argument("--dry-run", action="store_true", help="shows what it would do without changing anything")
    return p


COMMANDS = {
    "migrate": cmd_migrate,
    "relink": cmd_relink,
    "verify": cmd_verify,
    "archive": cmd_archive,
    "undo": cmd_undo,
    "recreate-links": cmd_recreate_links,
    "phase2": cmd_phase2,
    "status": cmd_status,
    "prune": cmd_prune,
}
