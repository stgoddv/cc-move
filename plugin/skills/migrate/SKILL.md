---
name: migrate
description: Move a project folder to a new path without losing its Claude Code sessions, settings and open VS Code tabs. Copies and verifies by hash, relinks the sessions, archives the original, then audits and repairs what the copy cannot carry over. Use when the user asks to move, migrate or relocate a project folder (or to use cc-move). Windows only.
argument-hint: "<source> <destination> [--with-deps]"
allowed-tools: Bash(python ${CLAUDE_SKILL_DIR}/scripts/cc_move.py migrate --dry-run *) Bash(python ${CLAUDE_SKILL_DIR}/scripts/cc_move.py verify *) Bash(python ${CLAUDE_SKILL_DIR}/scripts/cc_move.py status)
hooks:
  PreToolUse:
    - matcher: "Bash"
      hooks:
        - type: command
          if: "Bash(git commit *)"
          command: "python -c \"import sys; sys.stderr.write('Blocked by cc-move: no commits after a migration in this session. Open a new session to commit.'); sys.exit(2)\""
        - type: command
          if: "Bash(git push *)"
          command: "python -c \"import sys; sys.stderr.write('Blocked by cc-move: no pushes after a migration in this session. Open a new session to push.'); sys.exit(2)\""
        - type: command
          if: "Bash(npm publish *)"
          command: "python -c \"import sys; sys.stderr.write('Blocked by cc-move: no publishing during a migration.'); sys.exit(2)\""
        - type: command
          if: "Bash(terraform apply *)"
          command: "python -c \"import sys; sys.stderr.write('Blocked by cc-move: no deploys during a migration.'); sys.exit(2)\""
        - type: command
          if: "Bash(cdk deploy *)"
          command: "python -c \"import sys; sys.stderr.write('Blocked by cc-move: no deploys during a migration.'); sys.exit(2)\""
        - type: command
          if: "Bash(serverless deploy *)"
          command: "python -c \"import sys; sys.stderr.write('Blocked by cc-move: no deploys during a migration.'); sys.exit(2)\""
        - type: command
          if: "Bash(vercel *)"
          command: "python -c \"import sys; sys.stderr.write('Blocked by cc-move: no deploys during a migration.'); sys.exit(2)\""
        - type: command
          if: "Bash(firebase deploy *)"
          command: "python -c \"import sys; sys.stderr.write('Blocked by cc-move: no deploys during a migration.'); sys.exit(2)\""
---

# Move a project without losing its Claude Code sessions

Arguments: `$ARGUMENTS` — the source folder, the destination folder and optionally `--with-deps`. If either path is
missing, ask for it. The destination must not exist yet (or be an empty folder) and its parent must exist, except in
a case-only rename (see below).

The script is `${CLAUDE_SKILL_DIR}/scripts/cc_move.py`. Run it with the **Bash tool**, exactly as
`python ${CLAUDE_SKILL_DIR}/scripts/cc_move.py <command> …` (the dry run, `verify` and `status` are pre-approved in
that form). If `python` is not found, use `py -3` instead. Quote the source and destination paths.

The migration has two phases:

- **Phase 1 — the script (deterministic).** Copies the folder (without `node_modules` and virtual environments unless
  `--with-deps`), verifies every file by SHA-256, moves and rewrites the Claude Code session folders, `~/.claude.json`
  and the prompt history, copies the VS Code state of the folder (open Claude tabs) to the new path, renames the
  original to `archive-<date>-<name>` and writes a note in the project's `CLAUDE.local.md`.
- **Phase 2 — you (judgment).** Audit what the copy could not carry over and repair it, without commits or deploys.

## Before you start

- **Windows only.** If this is not Windows, stop and say so.
- This session must run **outside** the project being moved. If your working directory is inside the source, stop:
  ask the user to run `/cc-move:migrate` from a session opened elsewhere (for example the destination's parent).
- Ask the user to close **the VS Code window of that project** and any Claude Code session running in it. Other
  windows, including this one, can stay open. The script refuses to run otherwise and says what is still open.
- One project at a time. Given a list, process it in order and stop at the first phase 1 failure.
- Never edit `~/.claude`, `~/.claude.json` or VS Code's state by hand: the script does it.
- From the moment you invoke this skill, a hook blocks `git commit`, `git push` and common deploy commands for the
  rest of this session. Tell the user: to commit afterwards, open a new session.

## Phase 1

1. Dry run, and show the user the summary (sessions to move, dependency folders and their size, links, Claude tabs
   that were open, changes to `~/.claude.json`):

   `python ${CLAUDE_SKILL_DIR}/scripts/cc_move.py migrate --dry-run "<source>" "<destination>"`

   If something looks wrong (unexpected dependency folders, zero sessions for a project that has them), ask first.
   - By default `node_modules` and virtual environments are **not copied**. `--with-deps` copies them (and verifies
     them by hash). Use it only if the user asks, or suggest it when reinstalling is expensive or risky: big
     virtual environments (`torch`, CUDA), old dependencies, native builds. If you suggest it, ask.
2. Ask the user to confirm, then run it. The plugin's hook makes Claude Code ask the user's permission for this
   command (and for `undo`, `archive` and `relink`) in every permission mode, auto mode included:

   `python ${CLAUDE_SKILL_DIR}/scripts/cc_move.py migrate "<source>" "<destination>"`

   - The script prints at most one progress line per minute and keeps the latest one in `progress.txt` (its path
     is printed at the start). Small projects finish before the first line.
   - If the dry run showed more than ~2 GB, run it **in the background** and wait for the completion notice,
     **without polling** (every check costs tokens). If the user asks how it is going, read only `progress.txt`.
3. If it ends with `ERROR`, **do not improvise**: do not delete, do not copy by hand, do not retry with other
   options. Report the output. If verification failed, the report folder has `manifest-diff.txt`. Claude Code's
   state is not touched until the copy is verified.
4. If the original could not be renamed (folder in use), ask the user to close whatever uses it and run
   `python ${CLAUDE_SKILL_DIR}/scripts/cc_move.py archive "<destination>"`.

The report is in `%LOCALAPPDATA%\cc-move\reports\<date>-<name>\`: `report.md` (readable summary, start of phase 2),
`report.json`, the manifests, `robocopy.log`, `progress.txt` and `venvs\*.freeze.txt` (packages of each virtual
environment).

## Case-only rename

If the destination differs from the source only in letter case (`...\MyApp` → `...\myapp`, either direction), the
same `migrate` command detects it and the dry run starts with `Case-only rename in place`. The script renames the
folder in place (no copy, no hash, no archive, no `CLAUDE.local.md` note) and relinks the sessions,
`~/.claude.json`, the history and the VS Code state to the new case. Use the same steps (close the project's VS Code
window and sessions, dry run, confirm, run), then **skip phase 2**: nothing inside the project changes and Windows
still resolves the old spelling. Just run `verify "<destination>"` and summarize for the user. Tell them to open the
folder in VS Code with *File > Open Folder* (not *Open Recent*, which keeps the old spelling and would open the old
state): the Claude tabs that were open should reappear.

## Phase 2 — audit

Delegate the read-only audit to the **`auditor` subagent** (`cc-move:auditor`): give it the destination, the old
path and the path of `report.md`. Then check yourself, with the Bash tool:

- `git status --porcelain` in the destination must match the one in the report ("before migrating").
- `git worktree list` and `git config core.hooksPath`.

Append the findings to the "Phase 2" section of `report.md`.

## Phase 2 — repair

**Allowed:**
- Recreate virtual environments with the same Python version: `py -3.X -m venv .venv`, then
  `.venv\Scripts\python -m pip install -r <freeze file>` (or the project's own requirements if they match).
- Reinstall Node dependencies **from the lockfile**: `pnpm install --frozen-lockfile`, `npm ci`,
  `yarn install --frozen-lockfile`.
- Fix old absolute paths in **local configuration** (`.claude/settings.local.json`, `.env*`, `.vscode/`…) with the
  Edit tool, not `sed` (backslashes in Windows paths break its patterns). Leave documentation and history files
  alone, and tell the user which ones mention the old path.
- Offline checks: typecheck, lint, build.
- If a reinstall fails (package removed from the registry, native build, CUDA), copy that folder from the archived
  original with `robocopy` and note it.

**Copied virtual environments** (with `--with-deps`, or copied from the archive): `python.exe` and imports work from
the new path, but `Scripts\activate*` and the launchers in `Scripts\` (`pip.exe`, `uvicorn.exe`…) still contain the
old path and fail; `python -m venv --upgrade` does **not** fix them. Replace the old path in the `activate*` files
and regenerate the launchers the project uses with
`.venv\Scripts\python -m pip install --force-reinstall --no-deps <package>` (or use `python -m <tool>`). If the
environment is badly broken, recreate it from its freeze file. Copied `node_modules` need no repair: pnpm junctions
are already repointed; offline checks are enough.

**Forbidden:**
- Commits, pushes, switching branches, rewriting history.
- Deploys of any kind, `npm publish`, touching remote resources or databases.
- Changing dependency versions or updating lockfiles.
- Tests that call external services; leaving servers or processes running.
- Modifying the archived original.

Changes stay **uncommitted** for the user to review.

## Closing each project

1. Complete the note in `<destination>\CLAUDE.local.md`: replace the two `(pending, phase 2)` lines with what was
   recreated and what is left. Edit only between `<!-- cc-move:start -->` and `<!-- cc-move:end -->`; it is a short
   note for a future agent, not a report.
2. `python ${CLAUDE_SKILL_DIR}/scripts/cc_move.py phase2 "<destination>" --status ok|partial|failed --note "<one line>"`
3. `python ${CLAUDE_SKILL_DIR}/scripts/cc_move.py verify "<destination>"`
4. Summarize for the user: what moved, what was recreated, what is pending, what to check.

## After a batch

Tell the user to:

1. Open the new folder in VS Code (in a new window) with *File > Open Folder*, not *Open Recent* (it still has the
   old path). The Claude tabs that were open should reappear, and the
   session history should be complete. VS Code asks whether to trust the folder: trusting the destination's parent
   folder once avoids the question for every project moved there.
2. Optionally run the `verify --all` command the script printed at the end, from a regular terminal. Users have
   reported that an open session can rewrite `~/.claude.json` from memory when it closes (not reproduced so far);
   `verify --all` repairs it if it happens.

## Other commands

- `status`: lists migrations and flags archives older than 6 months, which the user may then delete together with
  the `cc-move` section of `CLAUDE.local.md`.
- `relink "<source>" "<destination>"`: re-applies only the session reassociation (idempotent).
- `undo "<destination>"`: moves the original and the sessions back to the old path. **Only if the user asks.** It
  does not delete the copy. For a case-only rename it renames the folder back.
