---
name: clone
description: Copy a project folder to a new folder (new name and/or location) and keep the original working, untouched. Copies and verifies by hash, never touches the original's folder, Claude Code sessions or VS Code state, then audits the copy, gives the user the commands to detach its git remotes and asks before changing what still ties it to the original. Use when the user asks to copy, duplicate, clone or fork a project folder into a new folder or name while keeping the original, for example to start a similar project for another client. Not for cloning a git repository from a URL. Windows only.
argument-hint: "<source> <destination> [--with-deps]"
allowed-tools: Bash(python ${CLAUDE_SKILL_DIR}/../migrate/scripts/cc_move.py clone --dry-run *) Bash(python ${CLAUDE_SKILL_DIR}/../migrate/scripts/cc_move.py status)
hooks:
  PreToolUse:
    - matcher: "Bash|PowerShell"
      hooks:
        - type: command
          command: "python \"${CLAUDE_PLUGIN_ROOT}/hooks/guard_migration.py\""
---

# Copy a project and keep the original

Arguments: `$ARGUMENTS` — the source folder (the original), the destination folder (the copy) and optionally
`--with-deps`. If either path is missing, ask for it. The destination must not exist yet (or be an empty folder),
its parent must exist, and neither folder may be inside the other.

The script is `${CLAUDE_SKILL_DIR}/../migrate/scripts/cc_move.py` (shared with `/cc-move:migrate`). Run it with the
**Bash tool**, exactly as `python ${CLAUDE_SKILL_DIR}/../migrate/scripts/cc_move.py <command> …` (the dry run and
`status` are pre-approved in that form). If `python` is not found, use `py -3` instead. Quote the source and
destination paths.

The clone has two phases:

- **Phase 1 — the script (deterministic).** Copies the folder (without `node_modules` and virtual environments unless
  `--with-deps`), verifies every file by SHA-256, lists the copy's git remotes in the report (it does not change
  them: the copy keeps the history and, until the user removes them, the original's remotes) and writes a
  "Cloned from" note in the copy's `CLAUDE.local.md`. It touches **nothing** of the original: not its folder, its
  Claude Code sessions, `~/.claude.json`, the prompt history or its VS Code state. The copy starts with no Claude Code
  sessions.
- **Phase 2 — you (judgment).** Audit the copy, repair what the copy could not carry over, hand the user the
  commands that detach the copy from the original's git remotes, then go through what still ties the copy to the
  original with the user, item by item. No commits, no deploys, no changes to the original.

## Before you start

- **Windows only.** If this is not Windows, stop and say so.
- This session must run **outside the destination**. Prefer a session outside the original too (for example the
  destination's parent): relative paths and commands run in a session opened in the original act on the original.
  If this session is inside the original anyway, use absolute paths under the destination for everything in phase 2.
- The original **does not need to be closed**: its VS Code window and Claude Code sessions can stay open. If a file
  changes while it is being copied, verification fails and nothing is lost; run it again.
- One project at a time. Given a list, process it in order and stop at the first phase 1 failure.
- Never edit `~/.claude`, `~/.claude.json` or VS Code's state by hand. A clone does not need any of them.
- From the moment you invoke this skill, a hook blocks `git commit`, `git push` and common deploy commands for the
  rest of this session. Tell the user: to commit afterwards, open a new session.

## Phase 1

1. Dry run, and show the user the summary (what is copied, dependency folders excluded and their size, links, the git
   remotes to remove in phase 2 by the user, the note):

   `python ${CLAUDE_SKILL_DIR}/../migrate/scripts/cc_move.py clone --dry-run "<source>" "<destination>"`

   If something looks wrong (unexpected dependency folders, a `.git` that is a file), ask first.
   - By default `node_modules` and virtual environments are **not copied**. `--with-deps` copies them (and verifies
     them by hash). Use it only if the user asks, or suggest it when reinstalling is expensive or risky: big
     virtual environments (`torch`, CUDA), old dependencies, native builds. If you suggest it, ask.
2. Ask the user to confirm, then run it. The plugin's hook makes Claude Code ask the user's permission for this
   command (and for `recreate-links`) in every permission mode, auto mode included:

   `python ${CLAUDE_SKILL_DIR}/../migrate/scripts/cc_move.py clone "<source>" "<destination>"`

   - The script prints at most one progress line per minute and keeps the latest one in `progress.txt` (its path
     is printed at the start). Small projects finish before the first line.
   - If the dry run showed more than ~2 GB, run it **in the background** and wait for the completion notice,
     **without polling** (every check costs tokens). If the user asks how it is going, read only `progress.txt`.
3. If it ends with `ERROR`, **do not improvise**: do not delete, do not copy by hand, do not retry with other
   options. Report the output. If verification failed, the report folder has `manifest-diff.txt`. The original is
   never changed, whatever the outcome.
4. If the script warns that `.git` is a **file** (the project is a git worktree or a submodule), the copy shares the
   original's repository: any git command that writes in the copy (checkout, branch, stash, remote changes, commits)
   changes the original's repository, so removing "its" remotes would remove the original's. Tell the user, do not
   run such commands, and leave the choice of giving the copy its own repository to them.

The report is in `%LOCALAPPDATA%\cc-move\reports\<date>-<name>\` (phase 1 prints the folder): `report.md` (readable
summary with a "For phase 2" section, including the remotes and the commands to remove them), `report.json`, the
manifests, `robocopy.log`, `progress.txt` and `venvs\*.freeze.txt` (packages of each virtual environment).

**Old paths that the report finds in the copy point into the original.** Anything that uses them (a script, a
config file, an editable install) would read or modify the original, which is still in use.

## Phase 2 — audit

Delegate the read-only audit to the **`auditor` subagent** (`cc-move:auditor`). Tell it this is a **clone**, and give
it the destination, the original's path and the path of `report.md`. Then check yourself, with the Bash tool (all of
these only read):

- `git status --porcelain` in the destination must match the git status recorded in the report.
- `git -C "<destination>" remote -v` lists the remotes the report names: they still point to the original's
  repository until the user removes them (see the git step below).
- `git worktree list` and `git config core.hooksPath` in the destination. Worktrees listed there belong to the
  original: never run `git worktree remove` from the copy, it would delete the original's worktree. Tell the user.

Append the findings to the "Phase 2" section of `report.md`.

## Phase 2 — repair

Do this before identity changes, and fix paths into the original **first**, before reinstalling, building or running
anything in the copy: those paths would make it act on the original.

**Allowed:**
- Fix old absolute paths in **local configuration** (`.claude/settings.local.json`, `.env*`, `.vscode/`, scripts,
  docker-compose files…) so they point to the copy, with the Edit tool, not `sed` (backslashes in Windows paths break
  its patterns). Leave documentation and history files alone, and tell the user which ones mention the original's
  path.
- Recreate virtual environments with the same Python version: `py -3.X -m venv .venv`, then
  `.venv\Scripts\python -m pip install -r <freeze file>` (or the project's own requirements if they match). A freeze
  file can list the project itself as an editable install (`-e` with a path into the original): install that line
  from the copy instead, or the copy's environment would run the original's code.
- Reinstall Node dependencies **from the lockfile**: `pnpm install --frozen-lockfile`, `npm ci`,
  `yarn install --frozen-lockfile`.
- Links the report lists as PENDING because their target is a dependency folder that was not copied (for example
  `apps/x/node_modules` → `<root>/node_modules`): **after** reinstalling, run
  `python ${CLAUDE_SKILL_DIR}/../migrate/scripts/cc_move.py recreate-links "<destination>"`. In an npm workspaces
  monorepo, `npm ci` may already have created a real folder there; the command leaves it alone, and that is fine.
- To delete a folder that contains junctions or symlinks (a stale `node_modules`, a build cache), use
  `cmd /c rmdir /s /q "<folder>"`, never PowerShell 5.1 `Remove-Item -Recurse`: it can follow the links and delete
  what they point to — possibly inside the original.
- Old paths the report lists under "build outputs and caches" need no fixing: the next build regenerates them.
  Deleting those folders in the copy is allowed if a build fails because of them. Dependency folders inside build
  outputs (`cdk.out/asset.*/node_modules`) are regenerated by the build too: do not reinstall them.
- Offline checks: typecheck, lint, build.
- Links **outside** the project that point into the original (for example a junction `C:\X` → the project, made for
  MAX_PATH; the auditor looks for them) stay as they are: the original still uses them. If the copy needs one of its
  own, give the user the command with a new name, for example `cmd /c mklink /J C:\Y "<destination>"`, and the
  config change that goes with it.
- If a reinstall fails (package removed from the registry, native build, CUDA), copy that folder from the original
  with `robocopy` (reading the original is fine) and note it.

**Copied virtual environments** (with `--with-deps`, or copied from the original): `python.exe` and imports work from
the new path, but `Scripts\activate*` and the launchers in `Scripts\` (`pip.exe`, `uvicorn.exe`…) still contain the
original's path, so they run against the original's environment or fail; `python -m venv --upgrade` does **not**
fix them. Replace the old path in the `activate*` files and regenerate the launchers the project uses with
`.venv\Scripts\python -m pip install --force-reinstall --no-deps <package>` (or use `python -m <tool>`). If the
environment is badly broken, recreate it from its freeze file. Copied `node_modules` need no repair: pnpm junctions
are already repointed; offline checks are enough.

## Phase 2 — git remotes (the user runs them)

The copy still has the original's remotes: until they are removed, a push from the copy reaches the original's
repository. Detaching them is the user's call, so **do not run these commands yourself**. Show the user the remotes
from the report and the exact commands, one per line, for example:

```
git -C "<destination>" remote remove origin
git -C "<destination>" remote add origin <new project's URL>     (optional, only if they have it)
```

Tell them they can run each one in their own terminal, or here by typing it with the `!` prefix
(`!git -C "<destination>" remote remove origin`). Removing a remote also deletes its remote-tracking branches and the
upstream settings in the copy; the original is not affected. If `.git` is a file (worktree or submodule), give no
commands: removing the remote there would remove it from the original's repository (see phase 1, step 4).

When they say they are done, check with the read-only `git -C "<destination>" remote -v` and report what it lists.
If an old remote is still there, say so and record it under "Pending / unresolved" in the note.

## Phase 2 — identity

The copy still carries the original's identity. Present the auditor's **"Ties to the original"** items, then ask the
user about them **one item per question** with AskUserQuestion (up to four questions per call), most important
first. Offer your proposed change when it is obvious, "leave as is" and "decide later"; when only the user knows the
value (a domain, an account, a secret), ask for it. Apply only what the user approves, with the Edit tool. Typical
items:

- **`.env*` values and secrets:** database URLs, API keys, tokens and accounts that belong to the original or its
  client. Never invent credentials and never copy them from elsewhere; leave placeholders if the user has no value
  yet. Do not echo secret values in your messages.
- **Deploy and infrastructure config:** stack, service, function and bucket names, domains, cloud accounts and
  regions (CDK, Terraform, serverless, SAM, Vercel, Firebase, docker-compose). Deploying the copy unchanged could
  overwrite the original's resources.
- **CI workflows:** deploy targets, environments and secrets they reference.
- **Package and app names and ids:** `package.json` `name`, `pyproject.toml`, app ids and bundle identifiers.
- **Mentions of the project** in `CLAUDE.md` and `README*` (name, client, URLs).

Whatever the user declines or defers goes to "Pending / unresolved" in the note.

**Forbidden (all of phase 2):**
- **Any change to the original folder**, its git repository, its Claude Code sessions or its VS Code state. Reading
  it is fine.
- Commits, pushes, switching branches, rewriting history.
- Running `git remote` commands that change the copy's remotes: the user runs them (see the git step).
- Deploys of any kind, `npm publish`, touching remote resources or databases (the ones in the copied config belong
  to the original).
- Changing dependency versions or updating lockfiles.
- Tests that call external services; leaving servers or processes running.

Changes stay **uncommitted** for the user to review.

## Closing each project

1. Complete the note in `<destination>\CLAUDE.local.md`: replace the "Recreated" and "Pending / unresolved"
   placeholder lines with what was recreated or changed and what is left (including identity items declined or
   deferred). Edit only between `<!-- cc-move:start -->` and `<!-- cc-move:end -->`; it is a short note for a future
   agent, not a report.
2. `python ${CLAUDE_SKILL_DIR}/../migrate/scripts/cc_move.py phase2 "<destination>" --status ok|partial|failed --note "<one line>"`
3. Summarize for the user: what was copied, the copy's remotes now (`git remote -v`), what was recreated, the
   identity changes applied, what is pending, and that the original was not changed.
4. Suggest opening the copy in a **new** VS Code window with *File > Open Folder*. It starts with no Claude Code
   sessions; the note in `CLAUDE.local.md` tells the first one where it came from. VS Code asks whether to trust the
   folder.

## Other commands

- `status`: lists migrations and clones; a clone shows as `clone of <source>`.
- `recreate-links "<destination>"`: creates the links phase 1 left pending once their targets exist (idempotent).
- `verify`, `undo`, `archive` and `relink` refuse a clone: there are no sessions to relink and no archive. To discard
  a clone, delete its folder (with `cmd /c rmdir /s /q`, **only if the user asks**); the original was never changed.
