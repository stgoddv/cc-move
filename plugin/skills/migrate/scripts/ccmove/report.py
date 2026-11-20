"""Report of each migration: report.json (complete) and report.md (readable, starting point for phase 2)."""

import json
import os

from .project_info import remove_remote_command
from .registry import is_clone

ORIGINAL_PATHS_WARNING = (
    "**WARNING: the old paths below point INTO THE ORIGINAL, which is still in place and in use. A script, config "
    "or tool run from the copy with them would read or modify the original. Fix them first.**")


def write_report(report_dir, record, details):
    """Saves report.json and report.md in the report folder."""
    with open(os.path.join(report_dir, "report.json"), "w", encoding="utf-8") as f:
        json.dump(details, f, indent=2, ensure_ascii=False)
    if record.get("kind") == "case-rename":  # in place: nothing to review in phase 2
        sections = [_summary(record, details), _editor_state(details), _warnings(details)]
    elif is_clone(record):
        sections = _clone_sections(record, details)
    else:
        sections = _migration_sections(record, details)
    with open(os.path.join(report_dir, "report.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(line for section in sections for line in section) + "\n")


def _migration_sections(record, details):
    """Sections of the report of a copy, including what phase 2 must review."""
    return [
        _summary(record, details),
        _editor_state(details),
        ["", "## For phase 2"],
        _dependencies(details),
        _lockfiles(details),
        _links(details),
        _stale_refs(details),
        _git(details),
        _warnings(details),
        ["", "## Phase 2 (filled in by the agent)", ""],
    ]


def _clone_sections(record, details):
    """Sections of the report of a clone: what phase 2 must review, starting with what still ties it to the
    original."""
    return [
        _summary(record, details),
        ["", "## For phase 2"],
        _remotes(record, details),
        ["", ORIGINAL_PATHS_WARNING] if details.get("stale_refs") else [],
        _stale_refs(details),
        _dependencies(details),
        _lockfiles(details),
        _links(details),
        _git(details, "### Git (of the original, before cloning)"),
        _warnings(details),
        ["", "## Phase 2 (filled in by the agent)", ""],
    ]


def _summary(record, details):
    """Header: paths, status, hash and moved sessions."""
    if record.get("kind") == "case-rename":
        what = "- Case-only rename in place: nothing was copied or archived"
    elif is_clone(record):
        what = ("- Clone: the original was not touched (not archived, no sessions moved); "
                "the copy starts without Claude Code sessions")
    else:
        what = (f"- Archived original: `{record.get('archive') or '(pending)'}` — "
                f"delete from {record.get('delete_after')}")
    lines = [
        f"# cc-move: {os.path.basename(record['src'])} ({record['date']})", "",
        f"- Source: `{record['src']}`",
        f"- Destination: `{record['dst']}`",
        what,
        f"- Phase 1 status: **{record['phase1']}**" + (f" — {record['error']}" if record.get("error") else ""),
    ]
    if record.get("content_sha256"):
        lines.append(f"- Content hash (SHA-256 of the manifest): `{record['content_sha256']}` "
                     f"({record['files']} files, {record['bytes'] / 1e9:.2f} GB)")
    for move in (details.get("relink") or {}).get("moved_dirs", []):
        lines.append(f"- Sessions: `projects/{move['from']}` → `projects/{move['to']}` "
                     f"({move['transcripts']} transcripts)")
    return lines


def _editor_state(details):
    """Result of copying the VS Code state and the Claude tabs that were open.
    If the copy failed, this list lets you reopen them by hand from the session history."""
    lines = ["", "## Editor state (VS Code)"]
    results = details.get("editor_state") or []
    if not results:
        return lines + ["- there was no saved editor state for this folder"]
    for r in results:
        lines.append(f"- {r['editor']}: {r['status']} → `{r['target']}`")
        lines += [f"    - Claude tab: \"{t['title']}\" (`{t['sessionId']}`)" for t in r["claude_tabs"]]
    return lines


def _dependencies(details):
    """Dependency folders; for venvs, the Python version and the file with the packages."""
    if details.get("with_deps"):
        title = "### Dependencies copied with --with-deps (review; venvs have launchers with the old path)"
    else:
        title = "### Dependencies not copied (recreate)"
    lines = ["", title]
    venvs = {v["path"]: v for v in details.get("venvs", [])}
    for rel, kind in details.get("deps", []):
        v = venvs.get(rel)
        if v:
            lines.append(f"- `{rel}` — Python venv {v['python_version']} (home `{v['home']}`); "
                         f"{v['packages']} packages in `{v['freeze_file']}` ({v['freeze_method']})")
        else:
            lines.append(f"- `{rel}` — {kind}")
    if not details.get("deps"):
        lines.append("- (none)")
    builds = details.get("build_deps") or []
    if builds:
        lines += ["", "### Dependency folders inside build outputs (regenerated by the build: do not reinstall)"]
        lines += [f"- `{rel}` — {kind}, inside `{build}`" for rel, kind, build in builds]
    return lines


def _lockfiles(details):
    """Lockfiles found, to know which manager to reinstall with."""
    items = [f"- `{x}`" for x in details.get("lockfiles", [])]
    return ["", "### Lockfiles and dependency manifests"] + (items or ["- (none)"])


def _links(details):
    """Links recreated and those that could not be created."""
    links = details.get("links") or {}
    created, failed = links.get("created", []), links.get("failed", [])
    lines = ["", "### Links (junctions/symlinks)",
             f"- Recreated: {len(created)} (repointed to the destination: {sum(1 for x in created if x['remapped'])})"]
    for x in failed:
        if x.get("after_deps"):
            reason = ("its target is a dependency folder that was not copied: recreate it after reinstalling the "
                      "dependencies, with the `recreate-links` command")
        else:
            reason = x["error"]
        lines.append(f"- PENDING `{x['path']}` ({x['kind']} → `{x['target']}`): {reason}")
    return lines


def _stale_refs(details):
    """Project files that still have the old path written in them: configuration and code one by one, first;
    build outputs and caches grouped by folder, so hundreds of generated files do not bury the few that matter."""
    refs = details.get("stale_refs", [])
    items = [f"- `{x['path']}` ({x['count']})" for x in refs if not x.get("build")]
    lines = ["", "### Old paths written inside the project"] + (items or ["- (none)"])
    builds = {}
    for x in refs:
        if x.get("build"):
            files, count = builds.get(x["build"], (0, 0))
            builds[x["build"]] = (files + 1, count + x["count"])
    if builds:
        lines += ["", "### Old paths in build outputs and caches (regenerated by the next build: do not edit)"]
        lines += [f"- `{folder}/`: {files} files ({count} mentions)"
                  for folder, (files, count) in sorted(builds.items())]
    return lines


def _remotes(record, details):
    """The copy's git remotes, which still point to the original's repository, and the commands to remove them.
    cc-move does not run them: the user does."""
    remotes = details.get("remotes") or []
    lines = ["", "### Git remotes of the copy (still the original's)"]
    if not remotes:
        return lines + ["- (none)"]
    lines.append("- WARNING: until they are removed, a push from the copy reaches the original's repository.")
    for r in remotes:
        push = f", push `{r['push_url']}`" if r.get("push_url") else ""
        lines.append(f"- `{r['name']}`: `{r['url']}`{push}")
    lines += ["", "Commands for the user to run (cc-move does not run them):", "", "```"]
    lines += [remove_remote_command(record["dst"], r["name"]) for r in remotes]
    return lines + ["```"]


def _git(details, title="### Git (before migrating)"):
    """Git state before migrating, to compare it afterwards."""
    git = details.get("git")
    lines = ["", title]
    if not git:
        return lines + ["- not a git repo"]
    status = git["status"].splitlines() if git["status"] and not git["status"].startswith("(error") else []
    lines += [f"- HEAD `{git['head']}`, branch `{git['branch']}`",
              f"- `git status --porcelain`: {len(status)} lines" + ("" if status else " (clean tree)")]
    lines += [f"    {x}" for x in status[:50]]
    if git["git_dir_is_file"]:
        lines.append("- WARNING: `.git` is a file (worktree or submodule); check the path it contains.")
    worktrees = (git["worktrees"] or "").splitlines()
    if len(worktrees) > 1:
        lines += ["- WARNING: there are additional worktrees:"] + [f"    {x}" for x in worktrees]
    return lines


def _warnings(details):
    """General warnings of the migration."""
    warnings = details.get("warnings") or []
    return ["", "### Warnings"] + [f"- {w}" for w in warnings] if warnings else []
