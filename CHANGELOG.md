# Changelog

## 0.3.1 — 2026-10-15

- The README, the plugin and marketplace descriptions and the skill now say that cc-move renames folders as well as
  moving them (a rename is a move within the same parent folder). The skill's description mentions renames and
  case changes, so asking Claude to "rename my project folder" now triggers `/cc-move:migrate`. No code changes.

## 0.3.0 — 2026-10-14

- Backups of Claude Code's state are now one zip per migration, kept **30 days**. Each `migrate` deletes expired
  backups and says so; `status` shows each backup's size and expiry. Before, every migration left an uncompressed
  copy of the project's session folders forever (hundreds of MB for projects with long histories).
- New `prune [--older-than DAYS] [--dry-run]` command to delete backups earlier. It also zips the folder backups left
  by earlier versions (or deletes them once expired), and lists without touching anything else in the backups folder.
  Like `migrate`, it asks for permission unless it is a dry run.
- Fix: the skill's safety hook blocked any command with a shell variable (`du -sh "$LOCALAPPDATA/cc-move"`) for the
  rest of the session. Claude Code runs an `if`-filtered hook anyway when the command has `$VAR`, `$(...)` or
  backticks, and those hooks always blocked. A script now reads the command and blocks only real `git commit`,
  `git push`, `npm publish` and deploy commands, in Bash and PowerShell.
- Fix: updating a migration in the registry (archive, undo, prune) moved it to the end, which broke the order
  `verify` relies on to follow later moves and renames.
- Fix: `verify` no longer repairs while a Claude Code session is open in the project (rewriting a transcript that
  a session is writing could lose its last lines); it says what is wrong and asks to close the session first.
- Fix: `verify` no longer rewrites mentions of the old path that appear in transcripts after the migration: they
  are conversation text (someone talking about the old path). It reports them and repairs only what Claude Code uses
  to find the sessions (session folders, `~/.claude.json`, history).
- The dry run shows the size of the session history to back up and rewrite; the skill runs large ones in the
  background.

## 0.2.0 — 2026-10-14

- Case-only renames (`MyApp` → `myapp`, either direction): `migrate` detects them and renames the folder in place,
  without copying, hashing or archiving, and with no phase 2. The session folders, `~/.claude.json` and the prompt
  history get the new case, and the VS Code state is copied to the id VS Code uses for the new name, so the open
  Claude tabs reappear. `undo` renames the folder back; `verify` checks the name on disk. Changing the case of a
  parent folder only is refused with a clear message. Redoing a rename after an undo replaces the VS Code state left
  by the first one when the old name's state is newer (the replaced one is kept in the report folder).
- The `CLAUDE.local.md` note mentions dependency folders only when the project has some, lists them, and says
  whether they were copied (`--with-deps`). Before, it always said `node_modules` and virtual environments were not
  copied.
- The dry run now counts the mentions to rewrite in every session folder of the project.
- `verify --all` checks each migration against the project's current path, following later migrations that moved
  or renamed its destination. Before, it relinked the sessions to the intermediate path (after `Proj` → `proj` →
  `Proj`, back to `proj`).
- `undo <path>` skips migrations already undone, so after undoing a rename it reaches the migration before it.
- The README and the skill say to open the moved folder with *File > Open Folder*: *Open Recent* keeps the old path.
- Two migrations of folders with the same name in the same second no longer collide on the backup folder name.

## 0.1.2 — 2026-10-14

- Claude can now start the `migrate` skill when you ask in your own words ("use cc-move to move X to Y"); before,
  only typing `/cc-move:migrate` worked.
- New plugin hook: Claude Code always asks your permission before a cc-move command that changes something
  (`migrate`, `undo`, `archive`, `relink`), in every permission mode, auto mode included. The dry run, `verify` and
  `status` still run without asking.

## 0.1.1 — 2026-10-14

- Paths given with 8.3 short names (`C:\Users\RUNNER~1\...`) are expanded to their long form, which is what Claude
  Code and VS Code record. Before, such a path did not find the VS Code state of the folder (and failed the tests on
  GitHub's Windows runners).
- CI uses the current `actions/checkout` and `actions/setup-python`.

## 0.1.0 — 2026-10-14

First version, tested on Windows 11 with Claude Code 2.1.299 and its VS Code extension.

- `/cc-move:migrate <source> <destination>`: two-phase migration of a project folder.
  - Phase 1 (script): copy with robocopy, verify every file by SHA-256, move and rewrite the Claude Code session
    folders, `~/.claude.json` and the prompt history, restore the VS Code state of the folder (open Claude tabs),
    archive the original as `archive-<date>-<name>`, and leave a note in `CLAUDE.local.md`.
  - Phase 2 (agent): audit and recreate what a copy cannot carry over (dependencies, absolute paths), without
    commits or deploys.
- `node_modules` and virtual environments are skipped by default; `--with-deps` copies them.
- Time-based progress lines (at most one per minute) and a `progress.txt` file.
- `verify`, `relink`, `archive`, `undo`, `phase2` and `status` commands.
